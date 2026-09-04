"""Read-only connection & health probe for the KSP Pokemon monitor.

Standalone diagnostic. This module performs live probes ONLY -- it never
writes state.json, never starts/stops the monitor process, never touches the
scheduled task, and never sends a notification.

What it reports (as a single JSON object on stdout, for
scripts/test_connection_helper.ps1 to render):

  * ip_status  -- a quick live fetch against KSP's category API using the
                  monitor's own headers / TLS-impersonation profile
                  (ksp_client.HEADERS + ksp_client.IMPERSONATE), to tell
                  whether this IP/endpoint is CLEAN or BLOCKED.
  * scan_*     -- scan-loop health + next-run ETA, derived from the tail of
                  logs/monitor.log.
  * log_tail   -- the last few log lines, for the caller to echo.

Pass --deep to additionally replay the monitor's headless-browser cookie
bootstrap and confirm a real catalog fetch (slower; default is the quick
single-request probe).
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "logs" / "monitor.log"

PROBE_TIMEOUT = 15
LOG_TAIL_LINES = 6

# Mirrors ksp_monitor_loop.py's own defaults, used only to estimate the next
# visible scan time from the log timestamp -- never to change behaviour.
DEFAULT_LIGHT_CHECK_INTERVAL_SECONDS = 20
DEFAULT_RECONCILIATION_INTERVAL_CHECKS = 15
BACKOFF_AFTER_ERROR_SECONDS = 30

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
_BACKOFF_RE = re.compile(r"backing off \d+s and continuing|Unhandled error in fast-poll cycle", re.I)

# Keep the probe silent on stderr: ksp_client's cookie bootstrap logs via the
# logging module, and we want stdout to be pure JSON for the PS caller.
import logging  # noqa: E402

logging.disable(logging.CRITICAL)

try:
    import ksp_client

    _IMPORT_ERROR = None
except Exception as exc:  # curl_cffi / import problems -- reported, not raised
    ksp_client = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _looks_like_catalog(data: object) -> bool:
    return isinstance(data, dict) and "items" in (data.get("result") or {})


def probe_network(config: dict, deep: bool) -> tuple[str, str]:
    """Return (status, detail) where status is 'CLEAN' or 'BLOCKED'."""
    if ksp_client is None:
        return "BLOCKED", f"cannot import ksp_client ({_IMPORT_ERROR}) - network probe unavailable"

    from curl_cffi import requests as cf_requests

    category_id = config.get("category_id", 32394)
    search = config.get("search")
    url = f"{ksp_client.API_BASE}/{category_id}"
    params = {"page": 1, "sort": "id"}
    if search:
        params["search"] = search

    session = cf_requests.Session(impersonate=ksp_client.IMPERSONATE)
    session.headers.update(ksp_client.HEADERS)

    try:
        resp = session.get(url, params=params, timeout=PROBE_TIMEOUT)
    except Exception as exc:
        return "BLOCKED", f"no response from KSP ({type(exc).__name__}: {exc})"

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = None
        if _looks_like_catalog(data):
            total = (data.get("result") or {}).get("products_total")
            return "CLEAN", (
                f"HTTP 200 from category API (products_total={total}); "
                "impersonated session reached KSP directly"
            )
        return "BLOCKED", "HTTP 200 but body was not the expected catalog JSON (interstitial / block page)"

    if ksp_client._is_cf_challenge(resp):
        # A *served* Cloudflare JS challenge means the IP itself is not blocked
        # -- Cloudflare has protected the whole site this way since 2026-08-19,
        # and the monitor clears it by bootstrapping cookies in a headless
        # browser. Treat it as CLEAN unless --deep asks us to actually confirm.
        if not deep:
            return "CLEAN", (
                "Cloudflare JS challenge served (HTTP 403 cf-mitigated=challenge); "
                "IP is not blocked - the monitor clears this via headless bootstrap. "
                "Re-run with --deep to verify the full bootstrap path."
            )
        try:
            boot = cf_requests.Session(impersonate=ksp_client.IMPERSONATE)
            boot.headers.update(ksp_client.HEADERS)
            ksp_client._apply_cf_cookies(boot)
            resp2 = boot.get(url, params=params, timeout=PROBE_TIMEOUT)
            if resp2.status_code == 200 and _looks_like_catalog(resp2.json()):
                return "CLEAN", "Cloudflare challenge cleared via headless bootstrap; catalog fetch OK"
            return "BLOCKED", (
                f"Cloudflare challenge did not clear after bootstrap (HTTP {resp2.status_code})"
            )
        except Exception as exc:
            return "BLOCKED", f"Cloudflare challenge; bootstrap failed ({type(exc).__name__}: {exc})"

    if resp.status_code == 429:
        return "BLOCKED", "rate-limited by KSP / Cloudflare (HTTP 429)"
    if resp.status_code == 403:
        return "BLOCKED", "HTTP 403 with no challenge header - IP likely blocked"
    if resp.status_code in (500, 502, 503, 504):
        return "BLOCKED", f"upstream error (HTTP {resp.status_code})"
    return "BLOCKED", f"unexpected HTTP {resp.status_code} from category API"


def _read_tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-n:]


def inspect_log(config: dict) -> tuple[list[str], str, str]:
    """Return (log_tail, scan_health, scan_detail)."""
    interval = config.get("light_check_interval_seconds", DEFAULT_LIGHT_CHECK_INTERVAL_SECONDS)
    recon_every = config.get(
        "reconciliation_interval_checks", DEFAULT_RECONCILIATION_INTERVAL_CHECKS
    )

    path = LOG_PATH
    if not path.exists():
        rotated = LOG_PATH.with_name(LOG_PATH.name + ".1")
        path = rotated if rotated.exists() else path
    if not path.exists():
        return [], "Unknown", "no log file at logs/monitor.log"

    tail = _read_tail(path, LOG_TAIL_LINES)
    window = _read_tail(path, 80)

    last_ts = None
    for ln in reversed(window):
        m = _TS_RE.match(ln)
        if m:
            last_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            break
    if last_ts is None:
        return tail, "Unknown", "no timestamped lines in log"

    now = datetime.now()
    in_backoff = any(_BACKOFF_RE.search(ln) for ln in window[-10:])
    if in_backoff:
        eta = last_ts + timedelta(seconds=BACKOFF_AFTER_ERROR_SECONDS)
        return tail, "Backoff", f"next scan ~{eta:%H:%M}"

    # Only reconciliation passes and real detections are logged; quiet light
    # checks are silent, so the next *visible* scan is roughly one
    # reconciliation interval after the last log line.
    eta = last_ts + timedelta(seconds=recon_every * interval)
    silent_for = now - last_ts
    if silent_for > timedelta(seconds=recon_every * interval) + timedelta(minutes=3):
        mins = int(silent_for.total_seconds() // 60)
        return tail, "Stalled", f"no log activity for {mins} min (last line {last_ts:%H:%M})"
    if eta <= now:
        return tail, "Healthy", "next scan due now"
    return tail, "Healthy", f"next scan ~{eta:%H:%M}"


def main(argv: list[str]) -> int:
    deep = "--deep" in argv
    config = load_config()

    ip_status, ip_detail = probe_network(config, deep)
    log_tail, scan_health, scan_detail = inspect_log(config)

    print(
        json.dumps(
            {
                "ip_status": ip_status,
                "ip_detail": ip_detail,
                "scan_health": scan_health,
                "scan_detail": scan_detail,
                "log_tail": log_tail,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
