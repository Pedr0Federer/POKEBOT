"""Thin client for KSP's internal category JSON API (m_action/api)."""

import logging
import random
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://ksp.co.il/m_action/api/category"
ITEM_API_TEMPLATE = "https://ksp.co.il/m_action/api/item/{uin}"
ITEM_URL_TEMPLATE = "https://ksp.co.il/web/item/{uin}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://ksp.co.il/web/",
    "Origin": "https://ksp.co.il",
}

REQUEST_TIMEOUT = 20
PAGE_JITTER_RANGE = (0.5, 1.5)

# The per-item endpoint answers in ~0.2s (single row lookup, no pagination),
# so a short ceiling keeps a stalled item-check from stalling a reconciliation
# cycle -- a caller that gets None back just falls back to its own heuristic.
ITEM_CHECK_TIMEOUT = 5

log = logging.getLogger("ksp_client")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_category_page(
    session: requests.Session, category_id: int, page: int, search: str | None = None
) -> dict:
    # sort=id pins a stable order across the paginated requests in one fetch cycle.
    # The default (relevance) order shifts between requests, which silently drops
    # items from the collected set and causes false "new item" detections later.
    params = {"page": page, "sort": "id"}
    if search:
        params["search"] = search
    resp = session.get(
        f"{API_BASE}/{category_id}",
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "result" not in data or "items" not in data["result"]:
        raise ValueError(f"Unexpected response shape from KSP API: {list(data.keys())}")
    return data["result"]


def fetch_products_total(
    session: requests.Session, category_id: int, search: str | None = None
) -> int:
    """Lightweight check: a single page-1 request, ignoring the item list.

    Used for fast polling -- the expensive full paginated crawl only runs
    when this cheap call indicates something may have changed.
    """
    return fetch_category_page(session, category_id, 1, search)["products_total"]


def fetch_all_items(
    category_id: int, search: str | None = None, session: requests.Session | None = None
) -> tuple[int, dict[int, dict]]:
    """Paginate through the whole category (optionally filtered by search) and
    return (products_total, {uin: item}). Reuses `session` if given, so a
    long-running caller can keep one pooled connection instead of opening a
    fresh one per crawl."""
    session = session or make_session()
    items: dict[int, dict] = {}

    first_page = fetch_category_page(session, category_id, 1, search)
    products_total = first_page["products_total"]
    _collect(items, first_page["items"])

    next_page = first_page.get("next")
    while next_page:
        time.sleep(random.uniform(*PAGE_JITTER_RANGE))
        page_data = fetch_category_page(session, category_id, next_page, search)
        _collect(items, page_data["items"])
        next_page = page_data.get("next")

    if len(items) != products_total:
        log.warning(
            "Collected %d items but products_total reported %d (category may have "
            "shifted between page fetches)",
            len(items),
            products_total,
        )

    return products_total, items


def check_item_in_stock(session: requests.Session, uin: int) -> bool | None:
    """Ground-truth stock check for a single item, used to confirm a catalog
    crawl's miss is a real stock-out rather than the pagination drift that
    `fetch_all_items` already warns about (the category listing can silently
    omit an item that's still in stock). Returns None -- never raises -- on
    any request failure, so callers can fall back to their own grace-period
    handling instead of treating a network hiccup as a confirmed answer.
    """
    try:
        resp = session.get(
            ITEM_API_TEMPLATE.format(uin=uin), timeout=ITEM_CHECK_TIMEOUT
        )
        resp.raise_for_status()
        return bool(resp.json()["result"]["data"]["addToCart"])
    except Exception:
        log.warning("Item-page stock check failed for uin=%s", uin, exc_info=True)
        return None


def _collect(items: dict[int, dict], raw_items: list[dict]) -> None:
    for raw in raw_items:
        uin = raw["uin"]
        items[uin] = {
            "uin": uin,
            "title": raw.get("name", "").strip(),
            "price": raw.get("price"),
            "img": raw.get("img"),
            "url": ITEM_URL_TEMPLATE.format(uin=uin),
        }
