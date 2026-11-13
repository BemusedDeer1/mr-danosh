import random
import re
from dataclasses import dataclass
from typing import Optional

from app.utils import normalize_persian


@dataclass(frozen=True)
class TriggerContext:
    """ورودی خالص موتور تصمیم‌گیری برای پاسخ‌گویی."""

    is_private: bool
    is_command: bool
    is_reply_to_bot: bool
    is_mentioned: bool
    text: str
    base_probability: float
    recent_msg_count_5min: int
    active_participants_5min: int
    seconds_since_last_bot_reply: float
    group_tone: str = "friendly"  # friendly, serious, tense, sensitive
    is_crowded: bool = False
    has_active_discussion: bool = False


@dataclass(frozen=True)
class TriggerDecision:
    """خروجی تصمیم‌گیری همراه با دلیل برای ثبت در لاگ."""

    should_reply: bool
    is_definite: bool
    probability_score: float
    reason: str
    prefer_ultra_short: bool = False


class TriggerEngine:
    """موتور خالص و تست‌پذیر تشخیص زمان مناسب برای پاسخ‌گویی آقای دانشور."""

    # املاها و شکل‌های مختلف صدا زدن آقای دانشور
    NAME_PATTERNS = [
        re.compile(r"(?:^|\s)(?:اقا|اقای|آقا|آقای|جناب|استاد|معاون)?\s*دانشور(?:\s|$|[!؟?.،])"),
        re.compile(r"(?:^|\s)درود\s+بر\s+دانشور"),
        re.compile(r"(?:^|\s)دانشور\s*جان"),
    ]

    # کلیدواژه‌های مرتبط با جهان ذهنی و علایق آقای دانشور
    INTEREST_KEYWORDS = [
        "مذاکره", "میز مذاکره", "قمار", "خط کش", "دفاعی", "امادگی دفاعی",
        "معاون", "انضباط", "انضباطی", "فوتبال", "بازی", "فیفا", "پی اس",
        "تقلب", "مدرسه", "پایه نهم", "دهم", "ناو", "جنگ",
    ]

    @classmethod
    def evaluate(cls, ctx: TriggerContext, rng: Optional[random.Random] = None) -> TriggerDecision:
        """ارزیابی اینکه آیا ربات باید به پیام فعلی پاسخ دهد یا خیر."""
        r = rng or random.Random()
        norm_text = normalize_persian(ctx.text).lower()

        # ۱. بررسی تریگرهای قطعی
        if ctx.is_private:
            return TriggerDecision(True, True, 1.0, "private_chat")

        if ctx.is_command:
            return TriggerDecision(True, True, 1.0, "explicit_command")

        if ctx.is_reply_to_bot:
            return TriggerDecision(True, True, 1.0, "reply_to_bot")

        if ctx.is_mentioned:
            return TriggerDecision(True, True, 1.0, "bot_username_mentioned")

        if any(pat.search(norm_text) for pat in cls.NAME_PATTERNS):
            return TriggerDecision(True, True, 1.0, "called_daneshvar_by_name")

        # تشخیص سؤال مستقیم خطاب به جمع یا ربات (مثلاً «نظرت چیه دانشور؟» یا پیام کوتاه سؤالی بعد از پاسخ ربات)
        if ctx.seconds_since_last_bot_reply < 35 and ("؟" in norm_text or "?" in norm_text):
            return TriggerDecision(True, True, 1.0, "followup_question_after_bot_reply")

        # ۲. بررسی موانع ورود تصادفی
        if ctx.group_tone in ("sensitive", "serious"):
            return TriggerDecision(False, False, 0.0, f"suppressed_due_to_{ctx.group_tone}_tone")

        # اگر گروه در ۵ دقیقه اخیر ساکت بوده (کمتر از ۳ پیام)، ورود تصادفی صفر است
        if ctx.recent_msg_count_5min < 3:
            return TriggerDecision(False, False, 0.0, "group_is_quiet")

        # اگر همین الان ربات جواب داده (کمتر از ۴۵ ثانیه پیش)، بی‌دلیل وسط حرف نپرد
        if ctx.seconds_since_last_bot_reply < 45:
            return TriggerDecision(False, False, 0.0, "too_soon_since_last_bot_reply")

        # ۳. محاسبه امتیاز ترکیبی برای ورود تصادفی
        score = ctx.base_probability

        # ضریب پویایی گفتگو (تعداد پیام‌ها و شرکت‌کنندگان)
        if ctx.active_participants_5min >= 3 and ctx.recent_msg_count_5min >= 6:
            score *= 1.4
        elif ctx.active_participants_5min >= 2:
            score *= 1.15

        # ضریب زمان سپری‌شده از آخرین پاسخ ربات
        if ctx.seconds_since_last_bot_reply > 1800:  # بیش از نیم ساعت سکوت ربات
            score *= 1.35

        # ضریب ارتباط موضوع با علایق آقای دانشور
        keyword_hits = sum(1 for kw in cls.INTEREST_KEYWORDS if kw in norm_text)
        if keyword_hits >= 1:
            score += min(0.12, keyword_hits * 0.05)

        # ضریب جذابیت/تازگی پیام (سؤال، ادعای عجیب یا اختلاف نظر)
        if "؟" in norm_text or "?" in norm_text or "!" in norm_text:
            score += 0.03
        if any(w in norm_text for w in ["چرت", "اشتباه", "عمرا", "مگه میشه", "شرط"]):
            score += 0.04

        # کاهش احتمال هنگام شلوغی بیش از حد گروه
        if ctx.is_crowded:
            score *= 0.45

        # محدود کردن سقف امتیاز تصادفی به حداکثر ۳۵ درصد
        final_prob = max(0.0, min(0.35, score))
        roll = r.random()

        if roll < final_prob:
            return TriggerDecision(
                should_reply=True,
                is_definite=False,
                probability_score=round(final_prob, 4),
                reason=f"random_interjection_hit(roll={roll:.3f}<prob={final_prob:.3f})",
                prefer_ultra_short=ctx.has_active_discussion,
            )

        return TriggerDecision(
            should_reply=False,
            is_definite=False,
            probability_score=round(final_prob, 4),
            reason=f"random_interjection_skip(roll={roll:.3f}>=prob={final_prob:.3f})",
        )
