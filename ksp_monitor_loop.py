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

# Exponential backoff on consecutive errors (bootstrap failures and fast-poll
# cycle errors alike): base * 2**(consecutive_errors-1), capped at max. Prior
# flat 30s retries let a burst of failures re-hit KSP every 30-50s
# indefinitely, which contributed to a temporary WAF block during 2026-08-21
# debugging -- backing off further on repeated failures avoids repeating that.
BASE_BACKOFF_AFTER_ERROR_SECONDS = 60
MAX_BACKOFF_AFTER_ERROR_SECONDS = 900


def _backoff_seconds(consecutive_errors: int) -> int:
    return min(
        BASE_BACKOFF_AFTER_ERROR_SECONDS * (2 ** (consecutive_errors - 1)),
        MAX_BACKOFF_AFTER_ERROR_SECONDS,
    )

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
    """Identifies which device sent the startup notification (and which
    device a log line came from). Each device keeps its own untracked
    `.env` (see .env.example) -- there's no shared/committed value beyond
    falling back to "Desktop" here."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DEVICE_NAME":
                return value.strip().strip("'\"") or "Desktop"
    return "Desktop"


def _run_full_check_with_backoff(config, log, session, consecutive_errors: int) -> int:
    """Runs a full check and pushes state, returning the updated
    consecutive-error count. run_full_check() catches its own fetch
    exceptions and returns a status code rather than raising, so its
    failures need to be turned into backoff here explicitly -- otherwise
    they'd retry at the normal fast-poll interval indefinitely, exactly
    the burst pattern this backoff is meant to avoid."""
    result = ksp_monitor.run_full_check(config, log, session)
    state_sync.push_state(log)
    if result != 0:
        consecutive_errors += 1
        backoff = _backoff_seconds(consecutive_errors)
        log.warning("Full check failed; backing off %ds before next cycle", backoff)
        time.sleep(backoff)
        return consecutive_errors
    return 0


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
    log.info("Daemon starting (device=%s)", device_name)

    # Bootstrapping the Cloudflare-cleared session can fail outright (e.g. a
    # hard Cloudflare block, or Playwright navigation timing out) -- that
    # used to be an unhandled exception here, which killed the whole
    # pythonw.exe process silently (no console to show the traceback) and
    # relied on the Task Scheduler watchdog trigger to relaunch it 15
    # minutes later, resending the startup notification forever. Retrying
    # in place instead keeps the process (and its single-instance mutex)
    # alive through a transient block.
    session = None
    bootstrap_errors = 0
    while session is None:
        try:
            session = ksp_client.make_session()
        except Exception:
            bootstrap_errors += 1
            backoff = _backoff_seconds(bootstrap_errors)
            log.exception(
                "Failed to bootstrap KSP session; retrying in %ds",
                backoff,
            )
            time.sleep(backoff)

    category_id = config["category_id"]
    search = config.get("search")

    # Sent as soon as the session is bootstrapped, exactly once per process
    # launch, rather than after the initial full check below -- so a slow or
    # failing full check can't block this signal indefinitely. The
    # Cloudflare-bootstrap retry loop above is what keeps this a rare,
    # meaningful signal instead of spam: a real restart (and thus a real
    # notification) now only happens if the process genuinely exits, not on
    # every transient Cloudflare block.
    notifier.send_telegram_text(
        f"\U0001F680 KSP Bot started successfully on [{device_name}]",
        config["telegram_bot_token"],
        config["telegram_chat_id"],
    )
    log.info("Startup notification sent (device=%s)", device_name)

    state_sync.pull_latest(log)

    log.info("Startup: running an initial full check before entering fast-poll mode")
    ksp_monitor.run_full_check(config, log, session)
    state_sync.push_state(log)

    checks_since_reconciliation = 0
    consecutive_errors = 0

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
                consecutive_errors = _run_full_check_with_backoff(config, log, session, consecutive_errors)
                checks_since_reconciliation = 0
                continue

            consecutive_errors = 0
            checks_since_reconciliation += 1
            if checks_since_reconciliation >= reconciliation_every:
                log.info(
                    "Reconciliation pass (products_total unchanged at %d, %d cycles since last full check)",
                    live_total,
                    checks_since_reconciliation,
                )
                consecutive_errors = _run_full_check_with_backoff(config, log, session, consecutive_errors)
                checks_since_reconciliation = 0

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, stopping loop.")
            return 0
        except Exception:
            consecutive_errors += 1
            backoff = _backoff_seconds(consecutive_errors)
            log.exception(
                "Unhandled error in fast-poll cycle; backing off %ds and continuing",
                backoff,
            )
            time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(loop())
