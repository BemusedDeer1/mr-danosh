import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.admin import AdminPanel
from app.ai_service import AIService
from app.config import load_settings, setup_logging
from app.database import DatabaseManager
from app.event_detector import EventDetector
from app.fun_features import FunFeatures
from app.group_context import GroupContextManager
from app.handlers import SocialHandlers
from app.memory_manager import MemoryManager
from app.personality import DaneshvarPersonality
from app.rate_limiter import RateLimiter
from app.scheduler import BotScheduler
from app.user_profiler import UserProfiler

logger = logging.getLogger(__name__)


class ProcessLock:
    """جلوگیری از اجرای هم‌زمان چند نمونه ربات با قفل فایل."""

    def __init__(self, lock_path: str = "data/daneshvar.lock"):
        self.lock_path = Path(lock_path)
        self.fp = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.lock_path, "w")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fp.write(str(os.getpid()))
            self.fp.flush()
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self.fp:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
                self.fp.close()
            except OSError:
                pass


async def run_bot() -> None:
    settings = load_settings()
    setup_logging(settings)

    lock = ProcessLock()
    if not lock.acquire():
        logger.critical("یک نمونه دیگر از ربات آقای دانشور در حال اجراست. خروج.")
        sys.exit(1)

    db = DatabaseManager(settings.DATABASE_PATH, settings.TIMEZONE)
    await db.connect()

    # بازیابی سقف ساعتی ذخیره‌شده در دیتابیس (در صورت تغییر قبلی توسط مدیر)
    saved_limit = await db.get_setting("max_replies_hour")
    hourly_limit = int(saved_limit) if saved_limit and saved_limit.isdigit() else settings.MAX_REPLIES_PER_HOUR

    ai_service = AIService(settings, db)
    await ai_service.start()

    personality = DaneshvarPersonality()
    group_ctx = GroupContextManager(db, settings.TIMEZONE)
    profiler = UserProfiler(settings, db, batch_size=15)
    memory = MemoryManager(db, group_ctx, profiler, settings.TIMEZONE)
    rate_limiter = RateLimiter(
        min_interval_sec=settings.MIN_REPLY_INTERVAL_SEC,
        max_replies_per_hour=hourly_limit,
    )
    event_detector = EventDetector(db, memory)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    admin_panel = AdminPanel(settings, db, rate_limiter)
    fun_features = FunFeatures(settings, db, ai_service, personality)
    social_handlers = SocialHandlers(
        settings=settings,
        db=db,
        ai_service=ai_service,
        personality=personality,
        memory=memory,
        rate_limiter=rate_limiter,
        event_detector=event_detector,
    )

    dp = Dispatcher()
    # ترتیب ثبت روترها: ابتدا دستورات ادمین، سپس قابلیت‌های طنز، و در نهایت پردازش پیام‌های عمومی
    dp.include_router(admin_panel.router)
    dp.include_router(fun_features.router)
    dp.include_router(social_handlers.router)

    scheduler = BotScheduler(settings, db, memory, ai_service, bot)
    scheduler.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received. Stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        logger.info("Mr. Daneshvar PRO is fully online and ready!")
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        await stop_event.wait()
        await dp.stop_polling()
        await polling_task
    finally:
        await scheduler.stop()
        await ai_service.stop()
        await bot.session.close()
        await db.close()
        lock.release()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
