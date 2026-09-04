"""Full-catalog check logic, shared by the one-shot CLI entrypoint (`python
ksp_monitor.py`, useful for manual runs/rebaselines) and by the fast-polling
loop in ksp_monitor_loop.py.

A full check fetches the entire current product catalog, diffs it against
the last saved state, fires notifications for any newly-appeared items, and
persists the new state -- but only after a fully successful fetch, so a
transient network or API failure never causes a false "everything is new"
alert.
"""

import json
import logging
import logging.handlers
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ksp_client
import notifier
import state

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "logs" / "monitor.log"

# A single fetch cycle sometimes misses an item that's genuinely still listed
# (live stock/ranking changes mid-crawl even with sort=id pinning page order).
# An item must be absent for this many consecutive cycles before we treat it
# as actually gone. This stops a one-cycle miss-then-reappear from looking
# like a "new" product and firing a false alert.
MISSING_STREAK_THRESHOLD = 2

# How long an existing product must have been out of stock before its return
# is announced as [NEW] instead of [RESTOCK].
RESTOCK_WINDOW = timedelta(days=7)


def merge_items(previous_items: dict, current_items: dict, confirmed_oos: set[int] = frozenset()) -> dict:
    """Build the next state's item map with missing-cycle grace tracking.

    Items present in this fetch get missing_streak reset to 0. Items absent
    this fetch but seen recently keep their old data and an incremented
    streak, so they remain part of the "known" baseline. Items absent for
    MISSING_STREAK_THRESHOLD+ consecutive cycles are dropped for good.

    `confirmed_oos` is the set of uins run_full_check already verified as
    genuinely out of stock via a direct item-page check this cycle -- those
    are dropped immediately rather than waiting out the grace streak, since
    the grace period exists only to cover *unverified* crawl misses.
    """
    merged = {}
    for uin_str, item in previous_items.items():
        uin = int(uin_str)
        if uin in current_items:
            continue  # handled below, from the fresh fetch data
        if uin in confirmed_oos:
            continue  # verified gone -- no need for the unverified-miss grace
        streak = item.get("missing_streak", 0) + 1
        if streak < MISSING_STREAK_THRESHOLD:
            merged[uin] = {**item, "missing_streak": streak}
    for uin, item in current_items.items():
        merged[uin] = {**item, "missing_streak": 0}
    return merged


def update_out_of_stock_since(
    previous_items: dict, current_items: dict, previous_out_of_stock_since: dict, now_iso: str
) -> dict:
    """Track, per uin, the timestamp an item was first observed missing.

    Set the first time an item known from the previous fetch is absent from
    this one (and left untouched on subsequent misses, so the clock keeps
    running even after `merge_items` prunes the item out of `items`).
    Cleared the moment the item is seen in stock again.
    """
    updated = dict(previous_out_of_stock_since)
    for uin_str in previous_items:
        uin = int(uin_str)
        if uin not in current_items:
            updated.setdefault(str(uin), now_iso)
    for uin in current_items:
        updated.pop(str(uin), None)
    return updated


def _is_long_absence(uin: int, out_of_stock_since: dict, now: datetime) -> bool:
    """True if `uin` was out of stock for longer than RESTOCK_WINDOW.

    An unknown/missing timestamp (e.g. the item vanished before out-of-stock
    tracking started) is treated conservatively as a long absence, so it
    alerts as [NEW] rather than risking a false [RESTOCK]."""
    ts = out_of_stock_since.get(str(uin))
    if not ts:
        return True
    try:
        went_oos = datetime.fromisoformat(ts)
    except ValueError:
        return True
    if went_oos.tzinfo is None:
        went_oos = went_oos.replace(tzinfo=timezone.utc)
    return (now - went_oos) > RESTOCK_WINDOW


def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    return logging.getLogger("ksp_monitor")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def run_full_check(config: dict, log: logging.Logger, session=None) -> int:
    """Run one full paginated crawl, diff it against saved state, notify on
    genuinely new items, and persist the merged state. Safe to call both as
    a one-shot check and repeatedly from a long-running poller -- a fresh
    `session` is created if none is passed in."""
    category_id = config["category_id"]
    search = config.get("search")
    session = session or ksp_client.make_session()

    previous = state.load_state()
    previous_items = previous.get("items", {})
    previous_all_time_seen = set(previous.get("all_time_seen_uins", []))
    previous_out_of_stock_since = previous.get("out_of_stock_since", {})
    # all_time_seen_uins is never pruned, so it's a more reliable "have we
    # ever completed a scan before" signal than previous_items, which can
    # legitimately be empty for reasons other than a true first run.
    is_first_run = not previous_all_time_seen

    try:
        products_total, current_items = ksp_client.fetch_all_items(category_id, search, session)
        if is_first_run:
            # A lone crawl can transiently miss an item (live stock/ranking
            # changes mid-crawl). For a fresh baseline there's no prior state
            # to give a miss the benefit of the doubt, so do a confirmatory
            # second crawl and union the results before treating it as ground
            # truth -- this is what a single seeding fetch got wrong before.
            log.info("First run: doing a confirmatory second crawl before seeding baseline")
            time.sleep(3)
            products_total_2, current_items_2 = ksp_client.fetch_all_items(
                category_id, search, session
            )
            products_total = max(products_total, products_total_2)
            current_items = {**current_items_2, **current_items}
    except Exception:
        log.exception("Failed to fetch KSP category %s; leaving state untouched", category_id)
        return 1

    previous_uins = {int(uin) for uin in previous_items.keys()}
    current_uins = set(current_items.keys())
    new_uins = current_uins - previous_uins
    now = datetime.now(timezone.utc)
    if is_first_run:
        log.info(
            "First run: seeding state with %d items (total=%d), no alerts sent",
            len(current_items),
            products_total,
        )
    elif new_uins:
        log.info("Detected %d changed item(s): %s", len(new_uins), sorted(new_uins))
        total_delta = products_total - previous.get("products_total", 0)
        bot_token = config.get("telegram_bot_token", "")
        chat_id = config.get("telegram_chat_id", "")
        toast_enabled = config.get("windows_toast_enabled", True)
        restock_alerts_enabled = config.get("restock_alerts_enabled", True)
        if not bot_token or bot_token.startswith("PASTE_"):
            log.warning("Telegram bot token not configured; skipping Telegram alerts")
        for uin in new_uins:
            item = current_items[uin]
            ever_seen_before = uin in previous_all_time_seen
            # Brand new products always alert as [NEW]. Existing products
            # coming back into stock alert as [NEW] if they'd been out of
            # stock for more than RESTOCK_WINDOW, otherwise [RESTOCK].
            is_restock = ever_seen_before and not _is_long_absence(
                uin, previous_out_of_stock_since, now
            )
            if is_restock and not restock_alerts_enabled:
                log.info("Suppressed restock alert for uin=%s (restock_alerts_enabled=false)", uin)
                continue
            if bot_token and not bot_token.startswith("PASTE_"):
                notifier.notify_new_item(
                    item, bot_token, chat_id, toast_enabled, is_restock,
                    products_total=products_total, total_delta=total_delta,
                )
            elif toast_enabled:
                toast_ok = notifier.send_windows_toast(item, is_restock)
                log.info(
                    "Toast-only notify for uin=%s is_restock=%s toast_ok=%s",
                    uin, is_restock, toast_ok,
                )
    else:
        log.info(
            "No new items (total=%d, unchanged from previous check)", products_total
        )

    # Items that dropped out of *this* crawl but were tracked before are only
    # candidates for going missing -- fetch_all_items's own pagination drift
    # (see its "category may have shifted" warning) routinely drops 1-4 items
    # from a crawl that are still actually in stock. Runs strictly after the
    # alert block above, so it never delays a [NEW]/[RESTOCK] notification.
    confirmed_oos: set[int] = set()
    if not is_first_run:
        candidate_missing = previous_uins - current_uins
        for uin in sorted(candidate_missing):
            in_stock = ksp_client.check_item_in_stock(session, uin)
            if in_stock is True:
                current_items[uin] = {
                    k: v for k, v in previous_items[str(uin)].items() if k != "missing_streak"
                }
                log.info(
                    "Drift correction: uin=%s absent from crawl but item-page check confirms still in stock",
                    uin,
                )
            elif in_stock is False:
                confirmed_oos.add(uin)
                log.info("Confirmed OOS via item-page check: uin=%s", uin)
            else:
                log.warning(
                    "Item-page stock check inconclusive for uin=%s; falling back to streak grace",
                    uin,
                )
        current_uins = set(current_items.keys())

    merged_items = merge_items(previous_items, current_items, confirmed_oos)
    updated_all_time_seen = previous_all_time_seen | current_uins
    now_iso = now.isoformat()
    updated_out_of_stock_since = update_out_of_stock_since(
        previous_items, current_items, previous_out_of_stock_since, now_iso
    )
    state.save_state(
        products_total,
        merged_items,
        updated_all_time_seen,
        now_iso,
        updated_out_of_stock_since,
        previous.get("mute_until"),
    )
    return 0


def run() -> int:
    """One-shot entrypoint: single full check, then exit. Used for manual
    runs/testing and as the target of the old-style periodic invocation."""
    log = setup_logging()
    try:
        config = load_config()
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load config.json")
        return 1
    return run_full_check(config, log)


if __name__ == "__main__":
    sys.exit(run())
