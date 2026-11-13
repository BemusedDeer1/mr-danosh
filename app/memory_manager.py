import logging
from typing import Optional

from app.database import DatabaseManager
from app.group_context import GroupContextManager, GroupStateSnapshot
from app.models import EpisodeRecord, UserProfile
from app.safety import SafetyGuard
from app.user_profiler import UserProfiler
from app.utils import get_jalali_date_str

logger = logging.getLogger(__name__)


class MemoryManager:
    """هماهنگ‌کننده سه لایه حافظه: بلندمدت کاربران، کوتاه‌مدت گروه و اپیزودیک FTS5."""

    def __init__(
        self,
        db: DatabaseManager,
        group_ctx: GroupContextManager,
        profiler: UserProfiler,
        timezone: str = "Asia/Tehran",
    ):
        self.db = db
        self.group_ctx = group_ctx
        self.profiler = profiler
        self.timezone = timezone

    async def process_incoming_message(
        self,
        chat_id: int,
        is_private: bool,
        message_id: int,
        user_id: int,
        full_name: str,
        username: Optional[str],
        text: str,
        reply_to_id: Optional[int] = None,
    ) -> UserProfile:
        """ثبت امن پیام در لایه‌های حافظه با رعایت کامل ایزوله‌سازی PV و opt-out."""
        profile = await self.db.get_or_create_user(user_id, full_name, username)

        # سیاست حریم خصوصی: پیام‌های PV وارد پروفایل گروهی یا خاطرات مشترک نمی‌شوند
        if is_private:
            return profile

        await self.db.increment_user_msg_count(user_id)

        # اگر کاربر درخواست توقف شوخی شخصی داده، وضعیتش در پروفایل ثبت می‌شود
        if SafetyGuard.detect_stop_joke_request(text) and not profile.no_personal_jokes:
            await self.db.set_user_no_personal_jokes(user_id, True)
            profile.no_personal_jokes = True

        # ثبت در بافر کوتاه‌مدت گروه (با حذف خودکار اطلاعات حساس)
        await self.group_ctx.add_message(
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
            sender_name=full_name,
            raw_text=text,
            reply_to_id=reply_to_id,
            persist=True,
        )

        # اگر کاربر opt-out کرده باشد، هیچ داده جدیدی برای تحلیل شخصیت جمع‌آوری نمی‌شود
        if not profile.opted_out:
            scrubbed = SafetyGuard.scrub_sensitive_text(text)
            if self.profiler.enqueue_message(user_id, scrubbed):
                self.profiler.schedule_analysis(user_id)

        return profile

    async def retrieve_context_for_reply(
        self,
        chat_id: int,
        is_private: bool,
        user_id: int,
        current_text: str,
        reply_to_message_id: Optional[int] = None,
        reply_to_text: Optional[str] = None,
        reply_to_sender: Optional[str] = None,
    ) -> tuple[Optional[UserProfile], GroupStateSnapshot, list[EpisodeRecord]]:
        """بازیابی هم‌زمان پروفایل، بافت گروه و ۲ تا ۳ خاطره مرتبط از FTS5."""
        profile = await self.db.get_user_profile(user_id)

        if is_private:
            # در چت خصوصی خاطرات گروهی مشترک و تاریخچه گروه بارگذاری نمی‌شود
            empty_snap = GroupStateSnapshot(
                recent_msg_count_5min=1,
                active_participants_5min=1,
                tone="serious" if SafetyGuard.detect_distress_or_serious(current_text) else "friendly",
                current_topic="گفتگوی خصوصی",
                is_crowded=False,
                has_active_discussion=False,
                formatted_history="گفتگوی خصوصی یک‌به‌یک با کاربر.",
                reply_context=reply_to_text,
            )
            return profile, empty_snap, []

        snapshot = await self.group_ctx.get_snapshot(
            chat_id=chat_id,
            current_text=current_text,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            reply_to_sender=reply_to_sender,
        )

        episodes: list[EpisodeRecord] = []
        if not (profile and profile.opted_out):
            episodes = await self.db.search_episodes_fts(
                chat_id=chat_id, query_text=current_text, user_id=user_id, limit=3
            )
            if episodes:
                ep_ids = [e.id for e in episodes if e.id is not None]
                await self.db.mark_episodes_recalled(ep_ids)

        return profile, snapshot, episodes

    async def record_episode(
        self,
        chat_id: int,
        summary: str,
        user_ids: list[int],
        tags: str = "",
        importance: int = 3,
    ) -> Optional[int]:
        """ثبت یک خاطره مهم در حافظه اپیزودیک پس از فیلتر اطلاعات حساس."""
        if SafetyGuard.contains_sensitive_data(summary):
            logger.warning("Blocked saving episode containing sensitive keywords.")
            return None
        clean_summary = SafetyGuard.scrub_sensitive_text(summary)[:250]
        ep = EpisodeRecord(
            chat_id=chat_id,
            summary=clean_summary,
            event_date=get_jalali_date_str(self.timezone),
            user_ids=user_ids,
            tags=tags,
            importance=max(1, min(5, importance)),
        )
        return await self.db.add_episode(ep)

    async def run_maintenance(self) -> None:
        """پاک‌سازی دوره‌ای پیام‌های کوتاه‌مدت منقضی‌شده و هرس خاطرات کم‌اهمیت."""
        deleted_msgs = await self.db.cleanup_old_short_term_messages(ttl_hours=24)
        deleted_eps = await self.db.prune_old_episodes(max_keep=300)
        logger.info(f"Memory maintenance completed: pruned {deleted_msgs} old messages, {deleted_eps} old episodes.")
