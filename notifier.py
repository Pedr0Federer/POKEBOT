"""Notification channels: Telegram (primary) and Windows toast (local backup)."""

import logging
import re

import requests

log = logging.getLogger("notifier")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

NEW_ITEM_PREFIX = "\U0001F6A8 [NEW]"
RESTOCK_PREFIX = "\U0001F504 [RESTOCK]"

# Matches any run of Hebrew-block characters (letters, niqqud, punctuation
# like maqaf/geresh/gershayim), wherever they fall in the title -- KSP titles
# mix a Hebrew prefix/suffix around the official English product name.
_HEBREW_RUN_RE = re.compile(r"[֐-׿]+")
_REPEATED_DASH_RE = re.compile(r"(?:\s*-\s*){2,}")
_EDGE_PUNCT_RE = re.compile(r"^[\s\-:–—]+|[\s\-:–—]+$")


def _clean_title(title: str) -> str:
    """Strip Hebrew text out of a product title, leaving only the English
    product name. Falls back to the raw title if nothing English remains,
    so an all-Hebrew title never turns into a blank alert."""
    no_hebrew = _HEBREW_RUN_RE.sub(" ", title)
    no_hebrew = re.sub(r"\s+", " ", no_hebrew).strip()
    no_hebrew = _REPEATED_DASH_RE.sub(" - ", no_hebrew)
    no_hebrew = _EDGE_PUNCT_RE.sub("", no_hebrew).strip()
    return no_hebrew or title.strip()


def _build_caption(item: dict, is_restock: bool) -> str:
    prefix = RESTOCK_PREFIX if is_restock else NEW_ITEM_PREFIX
    title = _clean_title(item["title"])
    return "\n".join([f"{prefix} {title}", f"Price: ₪{item['price']}", item["url"]])


def send_telegram(item: dict, bot_token: str, chat_id: str, is_restock: bool = False) -> bool:
    caption = _build_caption(item, is_restock)
    try:
        if item.get("img"):
            url = TELEGRAM_API_BASE.format(token=bot_token, method="sendPhoto")
            payload = {"chat_id": chat_id, "photo": item["img"], "caption": caption}
        else:
            url = TELEGRAM_API_BASE.format(token=bot_token, method="sendMessage")
            payload = {"chat_id": chat_id, "text": caption}

        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        log.exception("Failed to send Telegram notification for uin=%s", item.get("uin"))
        return False


def send_windows_toast(item: dict, is_restock: bool = False) -> bool:
    try:
        from winotify import Notification, audio
    except ImportError:
        log.warning("winotify not installed; skipping Windows toast")
        return False

    prefix = RESTOCK_PREFIX if is_restock else NEW_ITEM_PREFIX
    title = _clean_title(item["title"])
    try:
        toast = Notification(
            app_id="KSP Monitor",
            title=prefix,
            msg=f"{title}\n₪{item['price']}",
            duration="long",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.add_actions(label="Open product page", launch=item["url"])
        toast.show()
        return True
    except Exception:
        log.exception("Failed to show Windows toast for uin=%s", item.get("uin"))
        return False


def notify_new_item(
    item: dict,
    bot_token: str,
    chat_id: str,
    windows_toast_enabled: bool,
    is_restock: bool = False,
) -> None:
    telegram_ok = send_telegram(item, bot_token, chat_id, is_restock)
    toast_ok = send_windows_toast(item, is_restock) if windows_toast_enabled else None
    log.info(
        "Notified for uin=%s is_restock=%s telegram_ok=%s toast_ok=%s",
        item.get("uin"),
        is_restock,
        telegram_ok,
        toast_ok,
    )
