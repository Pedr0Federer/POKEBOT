"""Cloud fallback entrypoint, run on a schedule by
.github/workflows/ksp_bot.yml.

The local desktop poller (ksp_monitor_loop.py) is the primary scanner --
it's zero-latency whenever the PC is on. This script only takes over when
that poller looks inactive, so a restock still gets caught (just with cron
latency instead of near-instant) whenever the PC is off.

Freshness is judged from state.json's `last_checked` timestamp, which the
local poller pushes to this repo as a heartbeat after every full check (see
state_sync.py). If that heartbeat is recent, the local poller is presumed
alive and this run exits without scanning or alerting, to avoid sending the
same Telegram alert twice.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import ksp_monitor
import state

BASE_DIR = Path(__file__).parent
CLOUD_CONFIG_PATH = BASE_DIR / "cloud_config.json"

# Local's own reconciliation cadence tops out around 5 minutes; this must
# comfortably exceed that so a normal local cycle is never mistaken for an
# offline PC, while staying under the 15-minute cloud cron interval.
LOCAL_STALE_THRESHOLD_SECONDS = 12 * 60


def load_cloud_config() -> dict:
    with open(CLOUD_CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    config["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    config["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    config["windows_toast_enabled"] = False
    return config


def local_poller_is_active(log: logging.Logger) -> bool:
    last_checked = state.load_state().get("last_checked")
    if not last_checked:
        return False
    try:
        last_dt = datetime.fromisoformat(last_checked)
    except ValueError:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
    log.info(
        "Local heartbeat age: %.0fs (stale threshold=%ds)",
        age_seconds,
        LOCAL_STALE_THRESHOLD_SECONDS,
    )
    return age_seconds < LOCAL_STALE_THRESHOLD_SECONDS


def main() -> int:
    log = ksp_monitor.setup_logging()
    if local_poller_is_active(log):
        log.info("Local poller heartbeat is recent; skipping cloud check to avoid duplicate alerts.")
        return 0
    log.info("Local poller looks inactive; running cloud fallback check.")
    config = load_cloud_config()
    return ksp_monitor.run_full_check(config, log)


if __name__ == "__main__":
    sys.exit(main())
