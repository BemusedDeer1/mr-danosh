import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from app.database import DatabaseManager
from app.safety import SafetyGuard
from app.utils import normalize_persian, now_iso


@dataclass
class BufferedMessage:
    message_id: int
    user_id: int
    sender_name: str
    text: str
    reply_to_id: Optional[int]
    timestamp: float


@dataclass
class GroupStateSnapshot:
    recent_msg_count_5min: int
    active_participants_5min: int
    tone: str  # friendly, serious, tense, sensitive
    current_topic: str
    is_crowded: bool
    has_active_discussion: bool
    formatted_history: str
    reply_context: Optional[str]


class GroupContextManager:
    """مدیریت بافر کوتاه‌مدت گروه و تشخیص سریع و ارزان (Heuristic) لحن و فضای گفتگو."""

    TOPIC_KEYWORDS = {
        "فوتبال و ورزش": ["فوتبال", "گل", "تیم", "استقلال", "پرسپولیس", "یونایتد", "لیگ", "مسی", "رونالدو", "بازی"],
        "گیم و بازی ویدیویی": ["گیم", "فیفا", "پی اس", "پلی استیشن", "اکانت", "خرید بازی", "سیستم", "کانتر"],
        "درس و مدرسه": ["درس", "امتحان", "نمره", "تست", "زیست", "شیمی", "ریاضی", "معلم", "مدرسه", "کنکور", "دفاعی"],
        "سیاست و اخبار": ["جنگ", "مذاکره", "ناو", "دلار", "اخبار", "اقتصاد", "تحریم"],
    }

    TENSE_WORDS = ["خفه", "چرت", "احمق", "بزن", "دعوا", "گمشو", "عوضی", "زر نزن", "حوصله"]

    def __init__(self, db: DatabaseManager, timezone: str = "Asia/Tehran", max_buffer: int = 40):
        self.db = db
        self.timezone = timezone
        self.max_buffer = max_buffer
        self._buffers: dict[int, deque[BufferedMessage]] = defaultdict(lambda: deque(maxlen=max_buffer))
        self._loaded_chats: set[int] = set()

    async def ensure_loaded(self, chat_id: int) -> None:
        """بازیابی پیام‌های اخیر از SQLite پس از Restart در اولین تعامل."""
        if chat_id in self._loaded_chats:
            return
        rows = await self.db.load_recent_short_term_messages(chat_id, limit=self.max_buffer)
        buf = self._buffers[chat_id]
        buf.clear()
        now_ts = time.time()
        for idx, r in enumerate(rows):
            # تخمین زمان نسبی برای پیام‌های بازیابی‌شده
            buf.append(
                BufferedMessage(
                    message_id=r["message_id"],
                    user_id=r["user_id"],
                    sender_name=r["sender_name"],
                    text=r["text"],
                    reply_to_id=r["reply_to_id"],
                    timestamp=now_ts - (len(rows) - idx) * 60,
                )
            )
        self._loaded_chats.add(chat_id)

    async def add_message(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        sender_name: str,
        raw_text: str,
        reply_to_id: Optional[int] = None,
        persist: bool = True,
    ) -> None:
        """افزودن پیام پاک‌سازی‌شده به بافر حافظه و ذخیره در DB."""
        await self.ensure_loaded(chat_id)
        scrubbed = SafetyGuard.scrub_sensitive_text(raw_text)[:300]
        msg = BufferedMessage(
            message_id=message_id,
            user_id=user_id,
            sender_name=sender_name,
            text=scrubbed,
            reply_to_id=reply_to_id,
            timestamp=time.time(),
        )
        self._buffers[chat_id].append(msg)

        if persist:
            await self.db.save_short_term_message(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                sender_name=sender_name,
                text=scrubbed,
                reply_to_id=reply_to_id,
                created_at=now_iso(self.timezone),
            )

    async def get_snapshot(
        self,
        chat_id: int,
        current_text: str,
        reply_to_message_id: Optional[int] = None,
        reply_to_text: Optional[str] = None,
        reply_to_sender: Optional[str] = None,
    ) -> GroupStateSnapshot:
        """تحلیل محلی (Heuristic) وضعیت فعلی گروه برای تصمیم‌گیری و ساخت پرامپت."""
        await self.ensure_loaded(chat_id)
        buf = list(self._buffers[chat_id])
        now = time.time()

        recent_5m = [m for m in buf if (now - m.timestamp) <= 300]
        recent_1m = [m for m in buf if (now - m.timestamp) <= 60]
        participants_5m = {m.user_id for m in recent_5m}

        # تشخیص لحن
        tone = "friendly"
        if SafetyGuard.contains_sensitive_data(current_text):
            tone = "sensitive"
        elif SafetyGuard.detect_distress_or_serious(current_text):
            tone = "serious"
        else:
            recent_combined = normalize_persian(" ".join(m.text for m in recent_5m[-6:]) + " " + current_text)
            if any(w in recent_combined for w in self.TENSE_WORDS):
                tone = "tense"

        # تشخیص موضوع جاری با شمارش کلیدواژه‌ها
        topic_scores: Counter[str] = Counter()
        sample_text = normalize_persian(" ".join(m.text for m in buf[-10:]) + " " + current_text)
        for topic, kws in self.TOPIC_KEYWORDS.items():
            for kw in kws:
                if kw in sample_text:
                    topic_scores[topic] += 1
        current_topic = topic_scores.most_common(1)[0][0] if topic_scores else "گپ آزاد روزمره"

        # ساخت متن فشرده ۱۵ پیام آخر برای ارسال به پرامپت
        last_15 = buf[-15:]
        history_lines = [f"- {m.sender_name}: {m.text}" for m in last_15]
        formatted_history = (
            f"موضوع کلی فعلی: {current_topic} | لحن فعلی: {tone}\n" + "\n".join(history_lines)
        )

        # اطلاعات ریپلای
        reply_ctx = None
        if reply_to_text and reply_to_sender:
            clean_rep = SafetyGuard.scrub_sensitive_text(reply_to_text)[:200]
            reply_ctx = f"کاربر در پاسخ (Reply) به پیام «{reply_to_sender}» که گفته بود «{clean_rep}» این پیام را فرستاده است."
        elif reply_to_message_id:
            for m in reversed(buf):
                if m.message_id == reply_to_message_id:
                    reply_ctx = f"کاربر در پاسخ به «{m.sender_name}» («{m.text}») این پیام را فرستاده است."
                    break

        return GroupStateSnapshot(
            recent_msg_count_5min=len(recent_5m),
            active_participants_5min=len(participants_5m),
            tone=tone,
            current_topic=current_topic,
            is_crowded=len(recent_1m) >= 12,
            has_active_discussion=len(recent_5m) >= 5 and len(participants_5m) >= 2,
            formatted_history=formatted_history,
            reply_context=reply_ctx,
        )
