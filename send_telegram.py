import os
import sys
import requests

# این مقادیر از GitHub Secrets خونده می‌شن، نه از خود کد
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@Atrbezan")

def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    response = requests.post(url, data=payload, timeout=15)
    result = response.json()
    if not result.get("ok"):
        print("خطا در ارسال پیام:", result)
        sys.exit(1)
    print("پیام با موفقیت ارسال شد:", result["result"]["message_id"])

if __name__ == "__main__":
    # فعلاً یه پیام تستی روزانه می‌فرستیم
    # بعداً این بخش رو با منطق واقعی (مثلاً چک‌کردن پست جدید) جایگزین می‌کنیم
    message = sys.argv[1] if len(sys.argv) > 1 else "✅ ربات فعال است — این یک پیام خودکار آزمایشی از GitHub Actions است."
    send_message(message)
