from curl_cffi import requests  # ספרייה מתקדמת לעקיפת חסימות
import json
import os
from bs4 import BeautifulSoup

# ==============================
# הגדרות - הפרטים שלך כבר בפנים
# ==============================
TELEGRAM_TOKEN = "8928782534:AAF4LVamJjVG67RItKSzZcRCXOeSPi9jr1A"   
TELEGRAM_CHAT_ID = "6127963507"  
STATE_FILE = "ksp_state.json"
KSP_URL = "https://ksp.co.il/web/cat/pokemon"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
}

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[Telegram] הודעה נשלחה: {message}")
    except Exception as e:
        print(f"[Telegram] שגיאה בשליחה: {e}")

def get_product_count() -> tuple[int, list[str]]:
    try:
        # כאן הוספנו impersonate="chrome" שמחקה דפדפן אמיתי ב-100%
        resp = requests.get(KSP_URL, headers=HEADERS, timeout=15, impersonate="chrome")
        resp.raise_for_status()
    except Exception as e:
        print(f"[KSP] שגיאה בגישה לאתר: {e}")
        return -1, []

    soup = BeautifulSoup(resp.text, "html.parser")
    count_el = (
        soup.find(class_="cat-total-items")
        or soup.find(class_="total-items")
        or soup.find("span", string=lambda t: t and "מוצר" in t)
    )

    if count_el:
        text = count_el.get_text(strip=True)
        digits = "".join(filter(str.isdigit, text))
        count = int(digits) if digits else -1
    else:
        count = len(soup.select(".product-item, .item, [class*='product']"))

    names = [
        el.get_text(strip=True)
        for el in soup.select(".product-title, .name, h3.title")[:5]
    ]
    return count, names

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_count": None}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def main():
    print("🤖 מתחיל סריקת KSP חד-פעמית ומאובטחת...")
    state = load_state()
    count, names = get_product_count()

    if count == -1:
        print("לא הצלחתי לקרוא את מספר המוצרים, הריצה נעצרה.")
        return

    print(f"מוצרים שנמצאו כעת: {count}")
    last = state.get("last_count")

    if last is None:
        msg = (
            f"📦 <b>KSP Pokemon — סנכרון ראשוני בענן</b>\n"
            f"נמצאו <b>{count}</b> מוצרים בקטלוג."
        )
        send_telegram(msg)
    elif count != last:
        diff = count - last
        emoji = "🆕" if diff > 0 else "🔴"
        direction = "נוסף" if diff > 0 else "הוסר"
        sample = "\n• " + "\n• ".join(names) if names else ""

        msg = (
            f"{emoji} <b>שינוי בקטלוג KSP Pokemon!</b>\n"
            f"לפני: {last} מוצרים\n"
            f"עכשיו: <b>{count}</b> מוצרים ({direction} {abs(diff)})\n"
            f"🔗 {KSP_URL}"
            f"{sample}"
        )
        send_telegram(msg)
    else:
        print("אין שינוי במספר המוצרים.")

    state["last_count"] = count
    save_state(state)

if __name__ == "__main__":
    main()
