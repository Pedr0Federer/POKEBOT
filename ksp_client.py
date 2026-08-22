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
import re
import time

from curl_cffi import requests as cf_requests

API_BASE = "https://ksp.co.il/m_action/api/category"
ITEM_API_TEMPLATE = "https://ksp.co.il/m_action/api/item/{uin}"
ITEM_URL_TEMPLATE = "https://ksp.co.il/web/item/{uin}"
CHALLENGE_WARMUP_URL = "https://ksp.co.il/web/"

# curl_cffi's impersonation profiles are pre-baked per Chrome major version and
# don't track the latest installed Chrome release, so the bootstrap browser's
# real version (whatever Playwright/Chrome happens to be) usually has no exact
# match. Rather than claim a version curl_cffi's TLS/HTTP2 layer can't actually
# back up, _nearest_impersonate picks the closest *available* target at or
# below the bootstrap's real major version, and the header User-Agent below is
# built to match that chosen target exactly -- keeping TLS fingerprint,
# Sec-Ch-Ua, and User-Agent all internally consistent with each other, even
# though none of them may match the bootstrap browser's own exact version.
# (A previous version of this file pinned User-Agent to a hardcoded "131"
# while IMPERSONATE claimed "chrome124" and left Sec-Ch-Ua-Platform at
# curl_cffi's default "macOS" on a Windows UA string -- a three-way
# inconsistency that's an easy bot-detection signal.)
_CURL_CFFI_CHROME_MAJORS = [99, 100, 101, 104, 107, 110, 116, 119, 120, 123, 124, 131, 133, 136, 142, 145, 146]
_DEFAULT_IMPERSONATE = "chrome146"

_UA_MAJOR_RE = re.compile(r"Chrome/(\d+)")


def _nearest_impersonate(major_version: int | None) -> str:
    if major_version is None:
        return _DEFAULT_IMPERSONATE
    candidates = [m for m in _CURL_CFFI_CHROME_MAJORS if m <= major_version]
    chosen = max(candidates) if candidates else min(_CURL_CFFI_CHROME_MAJORS)
    return f"chrome{chosen}"


def _build_headers(impersonate: str) -> dict:
    major = impersonate.removeprefix("chrome")
    user_agent = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    return {
        "User-Agent": user_agent,
        # A real browser's fetch()/XHR call to this JSON API sends Accept: */*,
        # not the document-navigation Accept curl_cffi's impersonate profile
        # defaults to.
        "Accept": "*/*",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://ksp.co.il/web/",
        "Origin": "https://ksp.co.il",
        "lang": "he",
        # curl_cffi's impersonate profile defaults these to "macOS" and to
        # document-navigation values (Sec-Fetch-Dest: document, Mode: navigate,
        # Site: none); overridden here to match this session's real platform
        # (Windows) and an actual same-origin JSON fetch call instead.
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
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


def _bootstrap_cf_cookies() -> tuple[list[dict], str | None]:
    """Load the KSP homepage in a headless browser to clear the Cloudflare
    JS challenge, and return (cookie jar, the browser's own real User-Agent).

    Deliberately does not override the page's User-Agent -- forcing a
    spoofed identity onto the browser itself (as a previous version of this
    file did, claiming "Chrome/131" on top of Playwright's actual bundled
    Chromium) makes the JS engine's real capabilities inconsistent with what
    it claims in headers, which is its own detection signal. Reporting the
    browser's true identity keeps the bootstrap step internally consistent;
    the curl_cffi handoff below picks the closest deliverable impersonation
    target from it rather than forwarding it verbatim (see _nearest_impersonate).
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for attempt in range(_BOOTSTRAP_ATTEMPTS):
                # KSP never reaches network-idle, so wait_until="networkidle" used to
                # hang every attempt for the full timeout; "domcontentloaded" plus an
                # unhandled-timeout guard below avoids both the hang and a crash that
                # would otherwise kill the whole monitor process on a slow/failed load.
                try:
                    resp = page.goto(CHALLENGE_WARMUP_URL, timeout=15_000, wait_until="domcontentloaded")
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
                    user_agent = page.evaluate("() => navigator.userAgent")
                    return page.context.cookies(), user_agent
                time.sleep(3)
            log.warning("Cloudflare challenge did not clear after %d attempts", _BOOTSTRAP_ATTEMPTS)
            user_agent = page.evaluate("() => navigator.userAgent")
            return page.context.cookies(), user_agent
        finally:
            browser.close()


def _apply_cf_cookies(session: cf_requests.Session) -> None:
    log.info("Bootstrapping Cloudflare challenge cookies via headless browser")
    cookies, browser_user_agent = _bootstrap_cf_cookies()
    log.info(
        "Bootstrap browser User-Agent: %r; captured %d cookies: %s",
        browser_user_agent,
        len(cookies),
        [c["name"] for c in cookies],
    )
    match = _UA_MAJOR_RE.search(browser_user_agent or "")
    impersonate = _nearest_impersonate(int(match.group(1)) if match else None)
    if impersonate != session.impersonate:
        log.info("Switching session impersonation target to %s (nearest match for bootstrap browser)", impersonate)
        session.impersonate = impersonate
        session.headers.update(_build_headers(impersonate))
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])


def _is_cf_challenge(resp) -> bool:
    return resp.status_code == _CF_CHALLENGE_STATUS and resp.headers.get("Cf-Mitigated") == "challenge"


def make_session() -> cf_requests.Session:
    session = cf_requests.Session(impersonate=_DEFAULT_IMPERSONATE)
    session.headers.update(_build_headers(_DEFAULT_IMPERSONATE))
    _apply_cf_cookies(session)
    return session


def _get(session: cf_requests.Session, url: str, params: dict, timeout: float):
    rebootstrapped = False
    for attempt in range(_MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=timeout)
        if _is_cf_challenge(resp) and not rebootstrapped:
            log.warning("Hit Cloudflare challenge; re-bootstrapping session and retrying")
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
