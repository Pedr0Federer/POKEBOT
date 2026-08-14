"""Atomic local persistence of the last-known KSP category state."""

import json
import os
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "products_total": 0,
            "items": {},
            "all_time_seen_uins": [],
            "out_of_stock_since": {},
            "last_checked": None,
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if "all_time_seen_uins" not in data:
        # Migrating from a pre-history-tracking state.json: backfill from the
        # currently-tracked items rather than treating this as a first run
        # (which would suppress alerts and redo the baseline unnecessarily).
        data["all_time_seen_uins"] = [int(uin) for uin in data.get("items", {}).keys()]
    if "out_of_stock_since" not in data:
        data["out_of_stock_since"] = {}
    return data


def save_state(
    products_total: int,
    items: dict[int, dict],
    all_time_seen_uins,
    last_checked: str,
    out_of_stock_since: dict[str, str] | None = None,
) -> None:
    payload = {
        "products_total": products_total,
        "items": {str(uin): item for uin, item in items.items()},
        # Permanent record of every uin ever observed. Unlike `items`, this is
        # never pruned -- it's what lets a later full check tell a genuinely
        # new product apart from a restock of something seen before.
        "all_time_seen_uins": sorted({int(uin) for uin in all_time_seen_uins}),
        # uin (str) -> ISO timestamp of the moment the item was first observed
        # missing from a fetch. Cleared once the item is seen in stock again.
        # Kept independent of `items` pruning so the clock survives past the
        # missing-streak grace window, all the way until the item restocks.
        "out_of_stock_since": dict(out_of_stock_since or {}),
        "last_checked": last_checked,
    }
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_PATH)
