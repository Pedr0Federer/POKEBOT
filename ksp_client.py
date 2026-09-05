"""Thin client for KSP's internal category JSON API (m_action/api).

KSP put Cloudflare bot-challenge protection in front of the whole site on
2026-08-19, which blocks plain `requests` outright -- even with a valid
challenge cookie, plain requests' TLS/HTTP2 fingerprint alone gets flagged.
A session here is bootstrapped by loading https://ksp.co.il/web/ in a real
headless browser once to clear the JS challenge and capture its cookies,
then reused via curl_cffi (which impersonates a real Chrome TLS fingerprint)
for the actual API calls. If a cookie later expires mid-session, a single
request will get the challenge page again; that's handled by re-bootstrapping
and retrying once.
"""

import logging
import random
import time

from curl_cffi import requests as cf_requests

API_BASE = "https://ksp.co.il/m_action/api/category"
ITEM_API_TEMPLATE = "https://ksp.co.il/m_action/api/item/{uin}"
ITEM_URL_TEMPLATE = "https://ksp.co.il/web/item/{uin}"
CHALLENGE_WARMUP_URL = "https://ksp.co.il/web/"

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Must match the Chrome version claimed in CHROME_UA -- a mismatched
# TLS/HTTP2 fingerprint vs. UA string is itself a Cloudflare bot signal, and
# was observed keeping every post-rebootstrap retry stuck on 403.
IMPERSONATE = "chrome131"

HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "application/json",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://ksp.co.il/web/",
    "Origin": "https://ksp.co.il",
}

REQUEST_TIMEOUT = 20
PAGE_JITTER_RANGE = (0.5, 1.5)

# Transient upstream errors worth a short retry; Cloudflare's challenge is
# handled separately via _CF_CHALLENGE_STATUS since it needs a re-bootstrap,
# not just a delay.
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2
_CF_CHALLENGE_STATUS = 403

# The per-item endpoint answers in ~0.2s (single row lookup, no pagination),
# so a short ceiling keeps a stalled item-check from stalling a reconciliation
# cycle -- a caller that gets None back just falls back to its own heuristic.
ITEM_CHECK_TIMEOUT = 5

log = logging.getLogger("ksp_client")


_BOOTSTRAP_ATTEMPTS = 3


def _bootstrap_cf_cookies() -> list[dict]:
    """Load the KSP homepage in a headless browser to clear the Cloudflare
    JS challenge, and return the resulting cookie jar."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=CHROME_UA)
            for attempt in range(_BOOTSTRAP_ATTEMPTS):
                try:
                    resp = page.goto(
                        CHALLENGE_WARMUP_URL, timeout=30_000, wait_until="domcontentloaded"
                    )
                except PlaywrightTimeoutError:
                    log.warning("Bootstrap attempt %d: navigation timed out", attempt + 1)
                    time.sleep(3)
                    continue
                status = resp.status if resp else None
                cf_mitigated = resp.headers.get("cf-mitigated") if resp else None
                title = page.title()
                log.info(
                    "Bootstrap attempt %d: nav_status=%s cf_mitigated=%s title=%r content_len=%d",
                    attempt + 1,
                    status,
                    cf_mitigated,
                    title,
                    len(page.content()),
                )
                if status == 200 and not cf_mitigated:
                    return page.context.cookies()
                time.sleep(3)
            log.warning("Cloudflare challenge did not clear after %d attempts", _BOOTSTRAP_ATTEMPTS)
            return page.context.cookies()
        finally:
            browser.close()


def _apply_cf_cookies(session: cf_requests.Session) -> None:
    log.info("Bootstrapping Cloudflare challenge cookies via headless browser")
    for cookie in _bootstrap_cf_cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])


def _is_cf_challenge(resp) -> bool:
    # KSP's WAF doesn't consistently attach a Cf-Mitigated: challenge header
    # to a blocked response (observed in production: plain 403s with no such
    # header), so any 403 from this API is treated as a Cloudflare block --
    # a stale/expired/never-accepted challenge cookie -- rather than a
    # genuine application-level 403.
    return resp.status_code == _CF_CHALLENGE_STATUS


def make_session() -> cf_requests.Session:
    session = cf_requests.Session(impersonate=IMPERSONATE)
    session.headers.update(HEADERS)
    _apply_cf_cookies(session)
    return session


def _get(session: cf_requests.Session, url: str, params: dict, timeout: float):
    rebootstrapped = False
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except cf_requests.exceptions.RequestException as exc:
            # Covers connection resets/timeouts, which -- like a bare 403 --
            # can be a symptom of a stale Cloudflare session, so the first
            # one also gets a re-bootstrap rather than just a plain retry.
            if not rebootstrapped:
                log.warning(
                    "Request error (%s); re-bootstrapping session and retrying", exc
                )
                _apply_cf_cookies(session)
                rebootstrapped = True
                continue
            if attempt < _MAX_RETRIES:
                log.warning("Request error (%s); retrying", exc)
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            log.warning("Request error (%s) after re-bootstrap and retries; giving up this cycle", exc)
            raise
        if _is_cf_challenge(resp) and not rebootstrapped:
            log.warning("Hit Cloudflare 403; re-bootstrapping session and retrying")
            _apply_cf_cookies(session)
            rebootstrapped = True
            continue
        if resp.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        return resp
    return resp


def fetch_category_page(
    session: cf_requests.Session, category_id: int, page: int, search: str | None = None
) -> dict:
    # sort=id pins a stable order across the paginated requests in one fetch cycle.
    # The default (relevance) order shifts between requests, which silently drops
    # items from the collected set and causes false "new item" detections later.
    params = {"page": page, "sort": "id"}
    if search:
        params["search"] = search
    resp = _get(session, f"{API_BASE}/{category_id}", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "result" not in data or "items" not in data["result"]:
        raise ValueError(f"Unexpected response shape from KSP API: {list(data.keys())}")
    return data["result"]


def fetch_products_total(
    session: cf_requests.Session, category_id: int, search: str | None = None
) -> int:
    """Lightweight check: a single page-1 request, ignoring the item list.

    Used for fast polling -- the expensive full paginated crawl only runs
    when this cheap call indicates something may have changed.
    """
    return fetch_category_page(session, category_id, 1, search)["products_total"]


def fetch_all_items(
    category_id: int, search: str | None = None, session: cf_requests.Session | None = None
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


def check_item_in_stock(session: cf_requests.Session, uin: int) -> bool | None:
    """Ground-truth stock check for a single item, used to confirm a catalog
    crawl's miss is a real stock-out rather than the pagination drift that
    `fetch_all_items` already warns about (the category listing can silently
    omit an item that's still in stock). Returns None -- never raises -- on
    any request failure, so callers can fall back to their own grace-period
    handling instead of treating a network hiccup as a confirmed answer.
    """
    try:
        resp = _get(session, ITEM_API_TEMPLATE.format(uin=uin), params={}, timeout=ITEM_CHECK_TIMEOUT)
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
