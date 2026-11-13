import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import jdatetime


def normalize_persian(text: str) -> str:
    """نرمال‌سازی حروف عربی به فارسی و پاک‌سازی فاصله‌های اضافی."""
    if not text:
        return ""
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "\u200c": " ",  # تبدیل نیم‌فاصله به فاصله برای جست‌وجوی یکنواخت
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def now_iso(tz_name: str = "Asia/Tehran") -> str:
    """برگرداندن زمان فعلی به صورت ISO با لحاظ کردن منطقه زمانی."""
    return datetime.now(ZoneInfo(tz_name)).isoformat()


def get_jalali_date_str(tz_name: str = "Asia/Tehran") -> str:
    """برگرداندن تاریخ جلالی خوانا برای استفاده در پرامپت و گزارش‌ها."""
    dt = datetime.now(ZoneInfo(tz_name))
    jdt = jdatetime.datetime.fromgregorian(datetime=dt)
    weekdays = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]
    months = [
        "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
        "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"
    ]
    w_name = weekdays[jdt.weekday()]
    m_name = months[jdt.month - 1]
    return f"{w_name} {jdt.day} {m_name} {jdt.year}"


def sanitize_telegram_html(text: str, max_length: int = 4000) -> str:
    """Escape کامل HTML و محدود کردن طول متن برای جلوگیری از خطای Telegram API."""
    if not text:
        return ""
    escaped = html.escape(text.strip(), quote=False)
    if len(escaped) > max_length:
        escaped = escaped[: max_length - 3] + "..."
    return escaped
