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
# Warm up on the category listing page rather than the bare homepage so the
# challenge is solved (and cf_clearance minted) in the same URL scope the
# m_action/api/category calls are made from. Keep in sync with config.json's
# category_id / search.
CHALLENGE_WARMUP_URL = "https://ksp.co.il/web/cat/32394?search=pokemon%20tcg"

# curl_cffi's impersonation profiles are pre-baked per Chrome major version and
# don't track the latest installed Chrome release, so the bootstrap browser's
# real version (whatever Playwright/Chrome happens to be) usually has no exact
# match. Rather than claim a version curl_cffi's TLS/HTTP2 layer can't actually
# back up, _nearest_impersonate picks the closest *available* target at or
# below the bootstrap's real major version, and the header User-Agent is built
# to match that chosen target exactly -- keeping TLS fingerprint, Sec-Ch-Ua,
# and User-Agent all internally consistent with each other. A previous version
# pinned User-Agent to a hardcoded "131" while IMPERSONATE claimed "chrome124"
# and left Sec-Ch-Ua-Platform at curl_cffi's default "macOS" on a Windows UA
# string -- a three-way inconsistency that's an easy bot-detection signal, and
# was observed keeping every post-rebootstrap retry stuck on 403.
_CURL_CFFI_CHROME_MAJORS = [99, 100, 101, 104, 107, 110, 116, 119, 120, 123, 124, 131, 133, 136, 142, 145, 146]
_DEFAULT_IMPERSONATE = "chrome131"

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


# Back-compat module constants for out-of-tree callers (e.g. scripts/
# test_connection_probe.py) that import these directly.
IMPERSONATE = _DEFAULT_IMPERSONATE
CHROME_UA = _build_headers(_DEFAULT_IMPERSONATE)["User-Agent"]
HEADERS = _build_headers(_DEFAULT_IMPERSONATE)

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

    The only identity detail forced onto the browser is stripping the
    "HeadlessChrome" token Playwright's bundled Chromium puts in its
    User-Agent -- Cloudflare 403s that token on sight, so the challenge can
    never clear while it's present. Everything else (JS engine capabilities,
    the Chrome major version) is left as the real browser reports it; the
    curl_cffi handoff picks the closest deliverable impersonation target from
    the reported UA rather than forwarding it verbatim (see _nearest_impersonate).
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # --disable-blink-features=AutomationControlled keeps navigator.webdriver
        # false; the UA override below removes the "HeadlessChrome" token that
        # Cloudflare rejects outright.
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            page = browser.new_page()
            raw_ua = page.evaluate("() => navigator.userAgent")
            if "HeadlessChrome" in (raw_ua or ""):
                clean_ua = raw_ua.replace("HeadlessChrome", "Chrome")
                log.info("Overriding bootstrap User-Agent %r -> %r", raw_ua, clean_ua)
                page.close()
                page = browser.new_page(user_agent=clean_ua)
            for attempt in range(_BOOTSTRAP_ATTEMPTS):
                try:
                    resp = page.goto(
                        CHALLENGE_WARMUP_URL, timeout=30_000, wait_until="domcontentloaded"
                    )
                except PlaywrightTimeoutError:
                    log.warning("Bootstrap attempt %d: navigation timed out", attempt + 1)
                    time.sleep(3)
                    continue
                # Cloudflare's JS challenge runs after DOMContentLoaded and sets the
                # cf_clearance cookie asynchronously; poll for it briefly instead of
                # returning immediately with a pre-challenge cookie jar (KSP never
                # goes network-idle, so we can't just wait on that).
                for _ in range(8):
                    if any(c["name"] == "cf_clearance" for c in page.context.cookies()):
                        break
                    page.wait_for_timeout(500)
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
    log.info(
        "Session impersonation target %s (nearest curl_cffi match for bootstrap browser)",
        impersonate,
    )
    session.impersonate = impersonate
    headers = _build_headers(impersonate)
    if browser_user_agent:
        # Cloudflare binds cf_clearance to the exact User-Agent string of the
        # browser that solved the challenge -- a single character's difference
        # invalidates the cookie. Send the captured UA verbatim rather than the
        # synthetic one _build_headers derives from the (only nearest) impersonate
        # target.
        headers["User-Agent"] = browser_user_agent
    session.headers.update(headers)
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])


def _is_cf_challenge(resp) -> bool:
    # KSP's WAF doesn't consistently attach a Cf-Mitigated: challenge header
    # to a blocked response (observed in production: plain 403s with no such
    # header), so any 403 from this API is treated as a Cloudflare block --
    # a stale/expired/never-accepted challenge cookie -- rather than a
    # genuine application-level 403.
    return resp.status_code == _CF_CHALLENGE_STATUS


def make_session() -> cf_requests.Session:
    session = cf_requests.Session(impersonate=_DEFAULT_IMPERSONATE)
    session.headers.update(_build_headers(_DEFAULT_IMPERSONATE))
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
