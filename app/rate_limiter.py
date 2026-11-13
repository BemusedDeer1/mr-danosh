import asyncio
import time
from collections import defaultdict, deque

from app.safety import SafetyGuard


class RateLimiter:
    """کنترل نرخ پاسخ‌گویی، جلوگیری از اسپم، تشخیص پیام تکراری و مدیریت محدودیت‌های گروهی."""

    def __init__(
        self,
        min_interval_sec: int = 5,
        max_replies_per_hour: int = 20,
        max_user_replies_per_hour: int = 8,
        max_consecutive_bot_replies: int = 2,
    ):
        self.min_interval_sec = min_interval_sec
        self.max_replies_per_hour = max_replies_per_hour
        self.max_user_replies_per_hour = max_user_replies_per_hour
        self.max_consecutive_bot_replies = max_consecutive_bot_replies

        self._lock = asyncio.Lock()
        # تاریخچه زمان پاسخ‌های ربات در هر چت (Sliding Window یک ساعته)
        self._chat_reply_timestamps: dict[int, deque[float]] = defaultdict(deque)
        # تاریخچه زمان پاسخ به هر کاربر (Sliding Window یک ساعته)
        self._user_reply_timestamps: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        # تعداد پاسخ‌های متوالی ربات بدون پیام انسانی جدید
        self._consecutive_bot_replies: dict[int, int] = defaultdict(int)
        # حافظه پیام‌های دیده‌شده برای حذف تکرار: (chat_id, message_id) و هش متن اخیر
        self._seen_message_ids: deque[tuple[int, int]] = deque(maxlen=500)
        self._recent_text_hashes: dict[tuple[int, int], tuple[str, float]] = {}

    def update_limits(self, max_replies_per_hour: int) -> None:
        self.max_replies_per_hour = max(1, max_replies_per_hour)

    async def register_human_message(self, chat_id: int, user_id: int, message_id: int, text: str) -> bool:
        """ثبت پیام انسانی جدید؛ در صورت تکراری بودن پیام False برمی‌گرداند."""
        async with self._lock:
            msg_key = (chat_id, message_id)
            if msg_key in self._seen_message_ids:
                return False
            self._seen_message_ids.append(msg_key)

            # بررسی اسپم تکراری یک کاربر در کمتر از ۱۰ ثانیه
            now = time.time()
            text_hash = SafetyGuard.compute_text_hash(text)
            user_key = (chat_id, user_id)
            last_entry = self._recent_text_hashes.get(user_key)
            if last_entry and last_entry[0] == text_hash and (now - last_entry[1]) < 10.0:
                return False
            self._recent_text_hashes[user_key] = (text_hash, now)

            # بازنشانی شمارنده پاسخ‌های متوالی ربات با رسیدن پیام انسانی جدید
            self._consecutive_bot_replies[chat_id] = 0
            return True

    async def can_reply(self, chat_id: int, user_id: int, is_definite: bool) -> tuple[bool, str]:
        """بررسی مجوز ارسال پاسخ بر اساس فاصله زمانی، سقف ساعتی و تعداد پاسخ متوالی."""
        async with self._lock:
            now = time.time()
            hour_ago = now - 3600.0

            chat_q = self._chat_reply_timestamps[chat_id]
            while chat_q and chat_q[0] < hour_ago:
                chat_q.popleft()

            user_q = self._user_reply_timestamps[(chat_id, user_id)]
            while user_q and user_q[0] < hour_ago:
                user_q.popleft()

            # ۱. حداقل فاصله بین دو پاسخ در چت
            if chat_q and (now - chat_q[-1]) < self.min_interval_sec:
                return False, "min_interval_not_elapsed"

            # ۲. حداکثر ۲ پاسخ متوالی بدون پیام انسانی جدید
            if self._consecutive_bot_replies[chat_id] >= self.max_consecutive_bot_replies:
                return False, "max_consecutive_bot_replies_reached"

            # ۳. سقف ساعتی کل چت (برای ورود تصادفی سخت‌گیرانه‌تر در ۸۰٪ ظرفیت)
            effective_chat_limit = (
                self.max_replies_per_hour if is_definite else max(1, int(self.max_replies_per_hour * 0.8))
            )
            if len(chat_q) >= effective_chat_limit:
                return False, "hourly_chat_limit_reached"

            # ۴. سقف ساعتی هر کاربر
            if len(user_q) >= self.max_user_replies_per_hour:
                return False, "hourly_user_limit_reached"

            return True, "allowed"

    async def record_bot_reply(self, chat_id: int, user_id: int) -> None:
        """ثبت انجام یک پاسخ موفق توسط ربات."""
        async with self._lock:
            now = time.time()
            self._chat_reply_timestamps[chat_id].append(now)
            self._user_reply_timestamps[(chat_id, user_id)].append(now)
            self._consecutive_bot_replies[chat_id] += 1

    async def seconds_since_last_reply(self, chat_id: int) -> float:
        async with self._lock:
            chat_q = self._chat_reply_timestamps[chat_id]
            if not chat_q:
                return 9999.0
            return max(0.0, time.time() - chat_q[-1])
