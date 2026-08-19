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
import time
from pathlib import Path

import ksp_client
import ksp_monitor
import notifier
import state
import state_sync

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"

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


def read_device_name() -> str:
    """Identifies which device sent the startup notification. Each device
    keeps its own untracked `.env` (see .env.example) -- there's no
    shared/committed value beyond falling back to "Desktop" here."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DEVICE_NAME":
                return value.strip().strip("'\"") or "Desktop"
    return "Desktop"


def loop() -> int:
    log = ksp_monitor.setup_logging()
    if not acquire_single_instance_lock(log):
        return 1

    try:
        config = ksp_monitor.load_config()
    except Exception:
        log.exception("Failed to load config.json")
        return 1

    device_name = read_device_name()
    notifier.send_telegram_text(
        f"\U0001F680 KSP Bot started successfully on [{device_name}]",
        config["telegram_bot_token"],
        config["telegram_chat_id"],
    )
    log.info("Startup notification sent (device=%s)", device_name)

    session = ksp_client.make_session()
    category_id = config["category_id"]
    search = config.get("search")

    state_sync.pull_latest(log)

    log.info("Startup: running an initial full check before entering fast-poll mode")
    ksp_monitor.run_full_check(config, log, session)
    state_sync.push_state(log)

    checks_since_reconciliation = 0

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

            baseline_total = state.load_state().get("products_total", 0)
            live_total = ksp_client.fetch_products_total(session, category_id, search)

            if live_total > baseline_total:
                log.info(
                    "Lightweight check: products_total %d -> %d, triggering full check",
                    baseline_total,
                    live_total,
                )
                ksp_monitor.run_full_check(config, log, session)
                state_sync.push_state(log)
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
            return 0
        except Exception:
            log.exception(
                "Unhandled error in fast-poll cycle; backing off %ds and continuing",
                BACKOFF_AFTER_ERROR_SECONDS,
            )
            time.sleep(BACKOFF_AFTER_ERROR_SECONDS)


if __name__ == "__main__":
    sys.exit(loop())
