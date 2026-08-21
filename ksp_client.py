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
from pathlib import Path

from curl_cffi import requests as cf_requests

# Dedicated persistent Chrome profile for the bootstrap browser -- NOT the
# user's real Chrome user-data dir. Reusing the same profile across bootstrap
# calls (including across separate process runs) lets Chrome's own on-disk
# cookie jar carry a still-valid cf_clearance forward, so a fresh bootstrap
# after a process restart can skip the JS challenge entirely if the previous
# cookie hasn't expired yet -- giving Cloudflare a consistent "returning
# device" profile instead of a brand-new anonymous browser every time.
# Pointing this at the user's actual Chrome profile instead was considered
# and rejected: Chrome refuses to open a second instance against a
# user-data-dir that's already locked by a running Chrome window, so it would
# fail unpredictably depending on whether the user happened to have Chrome
# open, and it would mix bot automation into a profile holding the user's
# real saved logins/extensions/browsing session.
PROFILE_DIR = Path(__file__).parent / ".chrome_profile"

API_BASE = "https://ksp.co.il/m_action/api/category"
ITEM_API_TEMPLATE = "https://ksp.co.il/m_action/api/item/{uin}"
ITEM_URL_TEMPLATE = "https://ksp.co.il/web/item/{uin}"
CHALLENGE_WARMUP_URL = "https://ksp.co.il/web/"

# curl_cffi's newest bundled impersonation target as of this writing is
# chrome146 -- it auto-generates a TLS/HTTP2 fingerprint plus a fully
# consistent header set (User-Agent, Sec-Ch-Ua*) matching that exact Chrome
# version. Deliberately claiming a *different* Chrome version in headers
# than the TLS fingerprint actually negotiates (e.g. an installed browser's
# real version) creates a fingerprint mismatch that's an easy bot-detection
# signal, so headers here are pinned to match chrome146 throughout rather
# than the real Chrome install used for bootstrap (see _bootstrap_cf_cookies,
# which intentionally does NOT override that browser's own identity).
IMPERSONATE = "chrome146"
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# The preset above defaults to claiming macOS; overridden to Windows here to
# match the platform of the real-Chrome bootstrap session whose cookies this
# client reuses. Values otherwise mirror a genuine Chrome/146 Windows client
# (captured from a real browser's request headers against this same API).
HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "*/*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://ksp.co.il/web/",
    "Origin": "https://ksp.co.il",
    "lang": "he",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="146", "Chromium";v="146"',
    "sec-ch-ua-full-version-list": (
        '"Not=A?Brand";v="99.0.0.0", "Google Chrome";v="146.0.0.0", "Chromium";v="146.0.0.0"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"19.0.0"',
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-model": '""',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-gpc": "1",
    "priority": "u=1, i",
}

REQUEST_TIMEOUT = 20
PAGE_JITTER_RANGE = (0.5, 1.5)

# Transient upstream errors worth a short retry; Cloudflare's challenge is
# handled separately via _CF_CHALLENGE_STATUS since it needs a re-bootstrap,
# not just a delay. Backoff here is deliberately generous -- KSP's WAF is
# sensitive to request bursts (a burst of retries during debugging on
# 2026-08-21 contributed to a temporary hard block), so a failed request
# should back off meaningfully rather than hammer the endpoint again in a
# couple of seconds.
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 8
_CF_CHALLENGE_STATUS = 403

# The per-item endpoint answers in ~0.2s (single row lookup, no pagination),
# so a short ceiling keeps a stalled item-check from stalling a reconciliation
# cycle -- a caller that gets None back just falls back to its own heuristic.
ITEM_CHECK_TIMEOUT = 5

log = logging.getLogger("ksp_client")


_BOOTSTRAP_ATTEMPTS = 3


def _bootstrap_cf_cookies() -> list[dict]:
    """Load the KSP homepage in a real (not Playwright's bundled headless-shell)
    Chrome browser to clear the Cloudflare JS challenge, and return the
    resulting cookie jar.

    Playwright's default headless=True launches a stripped-down
    "headless shell" build that self-identifies as HeadlessChrome via the
    Sec-Ch-Ua Client Hints header regardless of launch flags -- confirmed via
    live network capture on 2026-08-21, this alone is enough for KSP's WAF to
    block every API call even after the JS challenge nominally clears.
    channel="chrome" launches the actual installed Google Chrome instead,
    which in new-headless mode reports a real, internally consistent
    fingerprint -- but that alone still wasn't enough (confirmed via live
    testing on 2026-08-21: KSP's Cloudflare Bot Management blocked even a
    real-Chrome-driven session on its very first request). The remaining
    signal is the CDP (Chrome DevTools Protocol) automation connection
    itself, which plain Playwright leaves detectable. patchright is a
    Playwright fork that patches those specific CDP leaks (Runtime.enable
    artifacts etc.) while keeping the same sync_api -- drop-in otherwise.

    Launched via launch_persistent_context against PROFILE_DIR rather than a
    fresh throwaway context each time (still blocked as of 2026-08-21, ~2h
    after the previous block -- see ksp-api-hard-block-2026-08-21 memory):
    a brand-new, history-less browser profile is itself a bot signal, and it
    throws away a still-valid cf_clearance on every single bootstrap. Reusing
    one on-disk profile lets Chrome's own cookie jar carry clearance forward
    across bootstrap calls, including across separate process restarts.
    """
    from patchright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=True,
            args=["--headless=new"],
            # No user_agent override here (unlike the curl_cffi client below) --
            # letting the real Chrome install report its own true version keeps
            # its UA string, Sec-Ch-Ua Client Hints, and TLS fingerprint mutually
            # consistent, which a spoofed override would break.
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for attempt in range(_BOOTSTRAP_ATTEMPTS):
                resp = page.goto(CHALLENGE_WARMUP_URL, timeout=15_000, wait_until="domcontentloaded")
                # Cloudflare's JS challenge runs after DOMContentLoaded and sets the
                # cf_clearance cookie asynchronously; poll for it briefly instead of
                # waiting for networkidle (KSP never goes network-idle, which used
                # to hang this call for the full 30s timeout on every attempt).
                for _ in range(8):
                    if any(c["name"] == "cf_clearance" for c in context.cookies()):
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
                    return context.cookies()
                time.sleep(3)
            log.warning("Cloudflare challenge did not clear after %d attempts", _BOOTSTRAP_ATTEMPTS)
            return context.cookies()
        finally:
            context.close()


def _apply_cf_cookies(session: cf_requests.Session) -> None:
    log.info("Bootstrapping Cloudflare challenge cookies via headless browser")
    for cookie in _bootstrap_cf_cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])


def _is_cf_challenge(resp) -> bool:
    return resp.status_code == _CF_CHALLENGE_STATUS and resp.headers.get("Cf-Mitigated") == "challenge"


def make_session() -> cf_requests.Session:
    session = cf_requests.Session(impersonate=IMPERSONATE)
    session.headers.update(HEADERS)
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
            # A brief pause before reusing the freshly re-bootstrapped session
            # avoids an immediate second hit right on the heels of the
            # bootstrap request itself.
            time.sleep(_RETRY_BACKOFF_SECONDS)
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
