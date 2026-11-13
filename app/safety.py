import hashlib
import re
from typing import Tuple

from app.utils import normalize_persian, sanitize_telegram_html


class SafetyGuard:
    """مدیریت امنیت، فیلتر اطلاعات حساس، دفاع در برابر Prompt Injection و پاک‌سازی خروجی."""

    # الگوهای اطلاعات هویتی و تماس
    PII_PATTERNS = [
        re.compile(r"\b09\d{9}\b"),  # شماره موبایل ایران
        re.compile(r"\b\+989\d{9}\b"),
        re.compile(r"\b0\d{2,3}-?\d{7,8}\b"),  # تلفن ثابت
        re.compile(r"\b\d{10}\b"),  # کد ملی یا شماره ۱۰ رقمی
        re.compile(r"\b\d{16}\b"),  # شماره کارت بانکی
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # ایمیل
    ]

    # کلیدواژه‌های حوزه‌های حساس که نباید در پروفایل یا حافظه ذخیره شوند
    SENSITIVE_KEYWORDS = [
        # سلامت جسمی و روانی
        "افسردگی", "خودکشی", "قرص اعصاب", "روانپزشک", "بیماری", "سرطان", "تراپی", "اضطراب شدید",
        # اطلاعات خانوادگی و مالی
        "طلاق", "حقوق پدرم", "بدهی", "ورشکست", "فقیر", "آدرس خونه", "کد پستی", "پلاک",
        # عقاید سیاسی و مذهبی شخصی / قومیت / گرایش
        "مذهب", "شیعه", "سنی", "بهایی", "آتئیست", "گرایش جنسی", "همجنس", "قومیت",
        "حزب", "انتخابات", "رژیم", "تظاهرات", "بسیج", "سپاه",
    ]

    # الگوهای تلاش برای Prompt Injection
    INJECTION_PATTERNS = [
        re.compile(r"ignore\s+(all\s+)?(previous|above)\s+instructions", re.IGNORECASE),
        re.compile(r"system\s*prompt", re.IGNORECASE),
        re.compile(r"دستورات\s*(قبلی|بالا)\s*را\s*(فراموش|نادیده)", re.IGNORECASE),
        re.compile(r"پرامپت\s*(سیستم|اصلی|خودت)\s*را\s*(بگو|نشان|چاپ)", re.IGNORECASE),
        re.compile(r"نقش\s*خودت\s*را\s*تغییر\s*بده", re.IGNORECASE),
        re.compile(r"از\s*این\s*به\s*بعد\s*تو\s*یک", re.IGNORECASE),
    ]

    # نشانه‌های افشای دستورالعمل سیستمی در خروجی مدل
    PROMPT_LEAK_MARKERS = [
        "SYSTEM_INSTRUCTION",
        "USER_MESSAGE_DATA",
        "DANESHVAR_PROMPT",
        "حالت رفتاری فعلی:",
        "خلاصه شناخت کاربر هدف:",
    ]

    # کلیدواژه‌های بحران یا ناراحتی جدی که شوخی را متوقف می‌کند
    DISTRESS_KEYWORDS = [
        "حالم خیلی بده", "دارم گریه میکنم", "عزادار", "فوت کرد", "تصادف کرد",
        "خودکشی", "افسرده شدم", "شکست عشقی", "مریض شده", "بیمارستان بستری",
    ]

    @classmethod
    def contains_sensitive_data(cls, text: str) -> bool:
        """بررسی وجود اطلاعات حساس یا هویتی در متن."""
        if not text:
            return False
        norm = normalize_persian(text)
        for pat in cls.PII_PATTERNS:
            if pat.search(norm):
                return True
        for kw in cls.SENSITIVE_KEYWORDS:
            if kw in norm:
                return True
        return False

    @classmethod
    def scrub_sensitive_text(cls, text: str) -> str:
        """حذف شماره تماس، کد ملی و موارد حساس پیش از ذخیره در حافظه کوتاه‌مدت یا ارسال به API."""
        if not text:
            return ""
        cleaned = text
        for pat in cls.PII_PATTERNS:
            cleaned = pat.sub("[اطلاعات محرمانه حذف شد]", cleaned)
        if cls.contains_sensitive_data(cleaned):
            return "[پیام حاوی موضوع شخصی/حساس]"
        return cleaned

    @classmethod
    def detect_distress_or_serious(cls, text: str) -> bool:
        """تشخیص وضعیت غمگین، بحرانی یا درخواست جدی توقف شوخی."""
        norm = normalize_persian(text)
        return any(kw in norm for kw in cls.DISTRESS_KEYWORDS)

    @classmethod
    def detect_stop_joke_request(cls, text: str) -> bool:
        """تشخیص اینکه کاربر صراحتاً خواسته با او شوخی نشود."""
        norm = normalize_persian(text)
        phrases = [
            "با من شوخی نکن",
            "شوخی نکن دیگه",
            "ناراحت میشم شوخی",
            "جدی میگم شوخی نکن",
            "از شوخیت خوشم نیومد",
        ]
        return any(p in norm for p in phrases)

    @classmethod
    def is_serious_bot_identity_question(cls, text: str) -> bool:
        """تشخیص سؤال جدی درباره ربات یا هوش مصنوعی بودن."""
        norm = normalize_persian(text)
        patterns = [
            "تو رباتی",
            "تو ربات هستی",
            "ربات هستی یا",
            "هوش مصنوعی هستی",
            "تو ai هستی",
            "واقعا رباتی",
        ]
        return any(p in norm for p in patterns)

    @classmethod
    def sanitize_user_input_for_prompt(cls, text: str, max_len: int = 600) -> Tuple[str, bool]:
        """پاک‌سازی متن کاربر برای قرارگیری ایمن به‌عنوان داده در پرامپت."""
        trimmed = text.strip()[:max_len]
        is_injection = any(pat.search(trimmed) for pat in cls.INJECTION_PATTERNS)
        # حذف کاراکترهای کنترل و delimiterهای جعلی
        safe_text = trimmed.replace("```", "'''").replace("<", "‹").replace(">", "›")
        return safe_text, is_injection

    @classmethod
    def sanitize_model_output(cls, raw_output: str, fallback_text: str) -> str:
        """بررسی نشت پرامپت، حذف مارکداون ناخواسته و Escape کامل HTML."""
        if not raw_output:
            return sanitize_telegram_html(fallback_text)

        for marker in cls.PROMPT_LEAK_MARKERS:
            if marker in raw_output:
                return sanitize_telegram_html(fallback_text)

        # پاک‌سازی کاراکترهای مارکداون خام که ممکن است مدل تولید کند
        cleaned = re.sub(r"[*_`#]", "", raw_output).strip()
        return sanitize_telegram_html(cleaned)

    @staticmethod
    def compute_text_hash(text: str) -> str:
        norm = normalize_persian(text).lower()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
