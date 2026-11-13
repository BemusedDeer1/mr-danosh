import re
from dataclasses import dataclass
from typing import Optional

from app.database import DatabaseManager
from app.group_context import GroupStateSnapshot
from app.memory_manager import MemoryManager
from app.safety import SafetyGuard
from app.utils import normalize_persian


@dataclass(frozen=True)
class DetectedEvent:
    event_type: str  # conflict, football_match, major_news, banter, inside_joke, none
    should_record_episode: bool
    episode_summary: Optional[str] = None
    episode_tags: str = ""
    importance: int = 2
    discipline_delta: int = 0
    suggested_reaction: Optional[str] = None


class EventDetector:
    """تشخیص رویدادهای اجتماعی گروه و استخراج شوخی‌های داخلی با روش سریع محلی."""

    CONFLICT_MARKERS = ["خفه", "احمق", "بزنمت", "گمشو", "عوضی", "زر نزن", "حالتو میگیرم", "ببند"]
    FOOTBALL_RESULT_MARKERS = [
        "بردیم", "باختیم", "مساوی شد", "گل زد", "هت تریک", "دربی",
        "یونایتد", "استقلال", "پرسپولیس", "رئال", "بارسا", "قهرمان",
    ]
    MAJOR_NEWS_MARKERS = ["قبول شدم", "رتبه", "معدل", "خریدم", "تولدمه", "مسافرت", "المپیاد"]
    BANTER_MARKERS = ["هههه", "جر خوردم", "پاره شدم", "سم خالص", "عجب سوتی", "دهنت سرویس"]

    # الگوی تشخیص تکیه‌کلام یا اصطلاح داخل گیومه یا عبارات کوتاه پرتکرار
    QUOTED_PHRASE_RE = re.compile(r"[«\"']([^«»\"']{4,45})[»\"']")

    def __init__(self, db: DatabaseManager, memory: MemoryManager):
        self.db = db
        self.memory = memory

    async def inspect_message(
        self,
        chat_id: int,
        user_id: int,
        sender_name: str,
        text: str,
        snapshot: GroupStateSnapshot,
        is_opted_out: bool = False,
    ) -> DetectedEvent:
        """بررسی پیام برای تشخیص رویداد مهم، تغییر نمره انضباطی طنز و شوخی داخلی."""
        if SafetyGuard.contains_sensitive_data(text):
            return DetectedEvent(event_type="sensitive_skip", should_record_episode=False)

        norm = normalize_persian(text).lower()

        # ۱. بررسی کاندیدای شوخی داخلی (اگر عبارتی در گیومه یا پیام کوتاه و خاص بود)
        if not is_opted_out:
            quoted = self.QUOTED_PHRASE_RE.findall(text)
            candidate_phrase = quoted[0] if quoted else (text.strip() if 2 <= len(text.split()) <= 5 else None)
            if candidate_phrase and not SafetyGuard.contains_sensitive_data(candidate_phrase):
                confirmed_joke = await self.db.track_inside_joke_candidate(
                    chat_id=chat_id, phrase=candidate_phrase, user_id=user_id, threshold=3
                )
                if confirmed_joke:
                    ep_summary = f"اصطلاح «{confirmed_joke}» تبدیل به شوخی داخلی بچه‌های گروه شد."
                    await self.memory.record_episode(
                        chat_id=chat_id,
                        summary=ep_summary,
                        user_ids=[user_id],
                        tags=f"شوخی داخلی {confirmed_joke}",
                        importance=4,
                    )
                    return DetectedEvent(
                        event_type="inside_joke",
                        should_record_episode=False,  # همین الان ثبت شد
                        suggested_reaction="😂",
                    )

        # ۲. تشخیص دعوای لفظی یا تنش شدید (بدون شوخی؛ کاهش نمادین نمره انضباطی طنز)
        if any(w in norm for w in self.CONFLICT_MARKERS):
            return DetectedEvent(
                event_type="conflict",
                should_record_episode=False,
                discipline_delta=-3 if not is_opted_out else 0,
                suggested_reaction="🤨",
            )

        # ۳. تشخیص خبر مهم یا موفقیت کاربر
        if any(w in norm for w in self.MAJOR_NEWS_MARKERS):
            clean_snip = SafetyGuard.scrub_sensitive_text(text)[:120]
            summary = f"{sender_name} خبر مهمی در گروه داد: «{clean_snip}»"
            return DetectedEvent(
                event_type="major_news",
                should_record_episode=not is_opted_out,
                episode_summary=summary,
                episode_tags="خبر مهم موفقیت",
                importance=4,
                discipline_delta=+2 if not is_opted_out else 0,
                suggested_reaction="👏",
            )

        # ۴. تشخیص بحث داغ فوتبالی یا نتیجه بازی
        if any(w in norm for w in self.FOOTBALL_RESULT_MARKERS) and len(norm.split()) >= 4:
            clean_snip = SafetyGuard.scrub_sensitive_text(text)[:120]
            summary = f"بحث فوتبالی {sender_name}: «{clean_snip}»"
            return DetectedEvent(
                event_type="football_match",
                should_record_episode=not is_opted_out,
                episode_summary=summary,
                episode_tags="فوتبال نتیجه بازی",
                importance=3,
                suggested_reaction="⚽",
            )

        # ۵. تشخیص شوخی و خنده جمعی
        if any(w in norm for w in self.BANTER_MARKERS):
            return DetectedEvent(
                event_type="banter",
                should_record_episode=False,
                discipline_delta=+1 if not is_opted_out else 0,
                suggested_reaction="😄",
            )

        return DetectedEvent(event_type="none", should_record_episode=False)
