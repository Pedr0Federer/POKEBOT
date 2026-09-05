"""Fast-polling entrypoint: runs indefinitely, checking KSP roughly every
15-30 seconds instead of every 5 minutes.

Strategy per cycle ("smart fast polling"):
  1. One lightweight request (page 1 only) to read products_total.
  2. If it went up, immediately run a full paginated crawl to identify the
     new item(s) and alert.
  3. Otherwise, only every RECONCILIATION_INTERVAL_CHECKS cycles, run a full
     crawl anyway. This is a safety net: if KSP delists a sold-out item at
     the same moment it lists a new one, products_total doesn't move at all,
     so the lightweight check alone would miss it. The reconciliation pass
     runs at roughly the same ~5-minute cadence as the old scheduled-task
     design, so this is strictly an upgrade, not a regression, on that case.

A single Windows named mutex enforces that only one instance of this loop
runs at a time (e.g. if the scheduled task fires again at a later logon
while a manually-started instance is still running) -- otherwise two
pollers would double-count requests and send duplicate alerts.
"""

import ctypes
import logging
import sys
import threading
import time
from datetime import datetime, timezone

from curl_cffi import requests as cf_requests

import ksp_client
import ksp_monitor
import notifier
import state
import state_sync
import telegram_listener

DEFAULT_LIGHT_CHECK_INTERVAL_SECONDS = 20
DEFAULT_RECONCILIATION_INTERVAL_CHECKS = 15  # ~5 min at the default 20s interval
BACKOFF_AFTER_ERROR_SECONDS = 30

MUTEX_NAME = "Global\\KSPPokemonMonitorMutex"
ERROR_ALREADY_EXISTS = 183

# Keeping a reference alive for the process lifetime is what keeps the lock
# held; Windows releases it automatically if the process dies, so there's no
# stale-lock-file cleanup problem to worry about.
_mutex_handle = None


def acquire_single_instance_lock(log: logging.Logger) -> bool:
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        log.error("Another instance of the KSP monitor loop is already running; exiting.")
        return False
    return True


def loop() -> int:
    log = ksp_monitor.setup_logging()
    if not acquire_single_instance_lock(log):
        return 1

    try:
        config = ksp_monitor.load_config()
    except Exception:
        log.exception("Failed to load config.json")
        return 1

    session = ksp_client.make_session()
    category_id = config["category_id"]
    search = config.get("search")

    state_sync.pull_latest(log)

    # Telegram command listener: a fully separate daemon thread, its own
    # requests session, and its own long-poll loop. It only reads/writes
    # state.json (via state.py) and never touches ksp_client's scraping
    # session, so a Telegram outage or slow reply can never delay a scrape.
    # Guarded so a listener startup failure never prevents scraping itself
    # from starting.
    stop_event = threading.Event()
    listener_thread = None
    try:
        light_check_interval = config.get(
            "light_check_interval_seconds", DEFAULT_LIGHT_CHECK_INTERVAL_SECONDS
        )
        listener = telegram_listener.TelegramListener(config, light_check_interval)
        listener_thread = listener.start(stop_event)
    except Exception:
        log.exception("could not start Telegram command listener - continuing without it")

    log.info("Startup: running an initial full check before entering fast-poll mode")
    ksp_monitor.run_full_check(config, log, session)
    state_sync.push_state(log)

    checks_since_reconciliation = 0
    # Set when a lightweight check sees the site's total counter rise by more
    # than the following full crawl actually turned up -- KSP's CDN sometimes
    # lists the new total a beat before the item itself is paginated. Holds
    # the still-unexplained item count until the next cycle's recheck either
    # accounts for it (reconciled) or confirms it's really missing (alerted).
    pending_discrepancy = None

    while True:
        try:
            config = ksp_monitor.load_config()
            interval = config.get(
                "light_check_interval_seconds", DEFAULT_LIGHT_CHECK_INTERVAL_SECONDS
            )
            reconciliation_every = config.get(
                "reconciliation_interval_checks", DEFAULT_RECONCILIATION_INTERVAL_CHECKS
            )
            category_id = config["category_id"]
            search = config.get("search")

            time.sleep(interval)

            current_state = state.load_state()
            baseline_total = current_state.get("products_total", 0)
            try:
                live_total = ksp_client.fetch_products_total(session, category_id, search)
            except cf_requests.exceptions.RequestException as exc:
                # ksp_client already re-bootstrapped Cloudflare cookies and
                # retried once internally; a failure here means that didn't
                # recover. Don't let it kill the loop -- skip this cycle and
                # let the next one try again with the (now refreshed) session.
                log.warning(
                    "Lightweight check failed after re-bootstrap/retry (%s); "
                    "skipping this cycle",
                    exc,
                )
                continue

            if live_total < baseline_total:
                # Items went out of stock: rebase the baseline silently, no
                # full crawl and no Telegram alert.
                log.info(
                    "Lightweight check: products_total %d -> %d (decrease), rebasing baseline silently",
                    baseline_total,
                    live_total,
                )
                state.update_products_total(live_total, datetime.now(timezone.utc).isoformat())
                pending_discrepancy = None
                checks_since_reconciliation += 1
                if checks_since_reconciliation >= reconciliation_every:
                    log.info(
                        "Reconciliation pass (products_total unchanged at %d, %d cycles since last full check)",
                        live_total,
                        checks_since_reconciliation,
                    )
                    ksp_monitor.run_full_check(config, log, session)
                    state_sync.push_state(log)
                    checks_since_reconciliation = 0
                continue

            counter_delta = live_total - baseline_total
            # Re-run even with no fresh delta if a discrepancy is pending --
            # that's this cycle's grace-period recheck.
            if counter_delta > 0 or pending_discrepancy is not None:
                if counter_delta > 0:
                    log.info(
                        "Lightweight check: products_total %d -> %d, triggering full check",
                        baseline_total,
                        live_total,
                    )
                previously_tracked_uins = {
                    int(uin) for uin in current_state.get("items", {}).keys()
                }

                ksp_monitor.run_full_check(config, log, session)
                state_sync.push_state(log)

                after_state = state.load_state()
                newly_tracked_uins = {int(uin) for uin in after_state.get("items", {}).keys()}
                detected_new = len(newly_tracked_uins - previously_tracked_uins)

                if pending_discrepancy is not None:
                    still_missing = pending_discrepancy["missing_count"] - detected_new
                    if still_missing > 0:
                        bot_token = config.get("telegram_bot_token", "")
                        chat_id = config.get("telegram_chat_id", "")
                        if bot_token and not bot_token.startswith("PASTE_"):
                            notifier.send_discrepancy_alert(bot_token, chat_id, still_missing)
                        log.warning(
                            "Discrepancy alert: %d product(s) still not detected after grace period",
                            still_missing,
                        )
                    else:
                        log.info("Discrepancy reconciled: missing product(s) now accounted for")
                    pending_discrepancy = None
                elif counter_delta > detected_new:
                    gap = counter_delta - detected_new
                    pending_discrepancy = {"missing_count": gap}
                    log.info(
                        "Discrepancy detected: counter rose by %d but only %d new product(s) found; "
                        "deferring alert for 1 grace cycle",
                        counter_delta,
                        detected_new,
                    )

                checks_since_reconciliation = 0
                continue

            checks_since_reconciliation += 1
            if checks_since_reconciliation >= reconciliation_every:
                log.info(
                    "Reconciliation pass (products_total unchanged at %d, %d cycles since last full check)",
                    live_total,
                    checks_since_reconciliation,
                )
                ksp_monitor.run_full_check(config, log, session)
                state_sync.push_state(log)
                checks_since_reconciliation = 0

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, stopping loop.")
            stop_event.set()
            if listener_thread is not None:
                listener_thread.join(timeout=10)
            return 0
        except Exception:
            log.exception(
                "Unhandled error in fast-poll cycle; backing off %ds and continuing",
                BACKOFF_AFTER_ERROR_SECONDS,
            )
            time.sleep(BACKOFF_AFTER_ERROR_SECONDS)


if __name__ == "__main__":
    sys.exit(loop())
