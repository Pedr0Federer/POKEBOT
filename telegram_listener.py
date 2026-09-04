"""Lightweight long-polling Telegram command listener for the interactive
reply-keyboard menu.

Runs as one daemon thread that long-polls getUpdates. It never touches the
Playwright/curl_cffi scraping session or the main fast-poll loop directly --
it only reads and writes state.json through state.py, exactly like
ksp_monitor.py does, so it stays fully decoupled and can never block or slow
down scraping. A malformed update or a network blip is always swallowed so
the poll loop keeps running; getUpdates uses long-polling with allowed_updates
so the process is otherwise idle between messages (a separate requests
session from the scraping one, so a Telegram hiccup can never touch the
Cloudflare-bootstrapped scraping session).

Handled buttons (exact text match against notifier.BTN_*):
  📊 סטטוס מערכת            -> last/next check, mute state, tracked total
  📦 מוצרים במלאי           -> current products_total + last scan time
  🔕 השתקת התראות (שעתיים)  -> mute new-product alerts for 2 hours
  🔔 ביטול השתקה            -> unmute
/start, /menu and /help re-send the persistent keyboard; any other free text
gets a short "use the menu buttons" nudge.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

import state
from notifier import (
    BTN_MUTE_2H,
    BTN_PRODUCTS_IN_STOCK,
    BTN_SYSTEM_STATUS,
    BTN_UNMUTE,
    MAIN_MENU_KEYBOARD,
)

log = logging.getLogger("telegram_listener")

_API_BASE = "https://api.telegram.org"

MUTE_DURATION_HOURS = 2
LONG_POLL_TIMEOUT_SECONDS = 25


def _fmt_local(iso: str | None, fmt: str = "%H:%M:%S") -> str:
    """ISO (or naive-assumed-UTC) timestamp -> local time string, '—' if unset/unparseable."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


class TelegramListener:
    def __init__(
        self,
        config: dict,
        light_check_interval_seconds: int,
        session: "requests.Session | None" = None,
    ):
        self._token = config.get("telegram_bot_token", "")
        self._chat_id = str(config.get("telegram_chat_id", ""))
        self._interval = light_check_interval_seconds
        self._session = session or requests.Session()
        self._offset = None

    # --- lifecycle ------------------------------------------------------

    def start(self, stop_event: threading.Event) -> threading.Thread:
        thread = threading.Thread(
            target=self._run, name="telegram-listener", daemon=True, args=(stop_event,)
        )
        thread.start()
        return thread

    def send_welcome(self) -> None:
        self._send("\U0001F916 *KSP Monitor פעיל*\nבחר פעולה מהתפריט למטה \U0001F447")

    # --- polling loop -----------------------------------------------------

    def _run(self, stop_event: threading.Event) -> None:
        if not self._token or self._token.startswith("PASTE_"):
            log.warning("Telegram bot token not configured; command listener not started")
            return
        try:
            self._api("deleteWebhook", drop_pending_updates=False)
        except Exception:
            log.debug("deleteWebhook failed (non-fatal)", exc_info=True)
        log.info("Telegram command listener started (long-poll %ss)", LONG_POLL_TIMEOUT_SECONDS)
        while not stop_event.is_set():
            try:
                updates = self._get_updates()
            except Exception as exc:
                log.warning("telegram getUpdates failed: %s", exc)
                stop_event.wait(timeout=5)
                continue
            for update in updates:
                self._offset = update.get("update_id", 0) + 1
                try:
                    self._handle(update)
                except Exception:
                    log.exception("failed handling telegram update %s", update.get("update_id"))
        log.info("Telegram command listener stopped")

    def _api(self, method: str, **params):
        resp = self._session.post(
            f"{_API_BASE}/bot{self._token}/{method}",
            json=params,
            timeout=LONG_POLL_TIMEOUT_SECONDS + 15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method} -> {data}")
        return data.get("result")

    def _get_updates(self):
        params = {"timeout": LONG_POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        return self._api("getUpdates", **params) or []

    def _send(self, text: str) -> None:
        try:
            self._api(
                "sendMessage",
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
        except Exception:
            log.exception("failed to send telegram reply")

    # --- dispatch ----------------------------------------------------

    def _handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self._chat_id:
            log.info("ignoring telegram message from unauthorized chat %s", chat.get("id"))
            return
        text = (message.get("text") or "").strip()
        if not text:
            return

        if text in ("/start", "/menu", "/help"):
            self.send_welcome()
        elif text == BTN_PRODUCTS_IN_STOCK:
            self._reply_products_in_stock()
        elif text == BTN_SYSTEM_STATUS:
            self._reply_status()
        elif text == BTN_MUTE_2H:
            self._reply_mute()
        elif text == BTN_UNMUTE:
            self._reply_unmute()
        else:
            self._send("⚠️ אנא השתמש בכפתורי התפריט למטה בלבד.")

    # --- handlers ---------------------------------------------------

    def _reply_products_in_stock(self) -> None:
        data = state.load_state()
        total = data.get("products_total", 0)
        last_checked = _fmt_local(data.get("last_checked"))
        self._send(f"\U0001F4E6 נכון לשעה {last_checked}, זוהו {total} מוצרים במלאי באתר KSP.")

    def _reply_status(self) -> None:
        data = state.load_state()
        last_checked_iso = data.get("last_checked")
        last_checked = _fmt_local(last_checked_iso)

        next_check = "—"
        if last_checked_iso:
            try:
                dt = datetime.fromisoformat(last_checked_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                eta = dt + timedelta(seconds=self._interval)
                next_check = eta.astimezone().strftime("%H:%M:%S")
            except ValueError:
                pass

        if state.is_muted():
            mute_line = f"🔕 מושתקות עד {_fmt_local(data.get('mute_until'))}"
        else:
            mute_line = "🔔 פעילות"

        lines = [
            "\U0001F4CA *סטטוס מערכת*",
            "",
            f"• בדיקה אחרונה: {last_checked}",
            f"• בדיקה הבאה (משוער): {next_check}",
            f"• התראות: {mute_line}",
            f"• מוצרים במעקב כעת: {data.get('products_total', 0)}",
        ]
        self._send("\n".join(lines))

    def _reply_mute(self) -> None:
        until = datetime.now(timezone.utc) + timedelta(hours=MUTE_DURATION_HOURS)
        state.set_mute_until(until.isoformat())
        self._send(
            f"\U0001F515 התראות הושתקו למשך {MUTE_DURATION_HOURS} שעות "
            f"(עד {_fmt_local(until.isoformat())})."
        )

    def _reply_unmute(self) -> None:
        state.set_mute_until(None)
        self._send("\U0001F514 ההשתקה בוטלה. התראות פעילות שוב.")
