import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart

from app.ai_service import AIService
from app.config import load_settings, setup_logging
from app.database import DatabaseManager
from app.utils import get_jalali_date_str, sanitize_telegram_html

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


def build_core_router(ai_service: AIService, db: DatabaseManager, tz: str) -> Router:
    """روتر پایه مرحله ۱ برای تست اتصال و پاسخ‌گویی هسته."""
    router = Router(name="core_smoke_router")

    @router.message(CommandStart())
    async def cmd_start(message: types.Message) -> None:
        if not message.from_user:
            return
        await db.get_or_create_user(
            message.from_user.id, message.from_user.full_name, message.from_user.username
        )
        today_j = get_jalali_date_str(tz)
        text = (
            f"بههه! آقای {sanitize_telegram_html(message.from_user.full_name)}! "
            f"امروز {today_j} هست و سامانه انضباطی آقای دانشور آماده به کاره!"
        )
        await message.answer(text)

    @router.message(F.text)
    async def core_echo_ai(message: types.Message) -> None:
        if not message.from_user or message.from_user.is_bot or not message.text:
            return
        # در مرحله ۱ فقط در PV یا ریپلای پاسخ می‌دهد تا در مرحله ۲ موتور Trigger جایگزین شود
        is_pv = message.chat.type == "private"
        is_reply = (
            message.reply_to_message is not None
            and message.reply_to_message.from_user is not None
            and message.reply_to_message.from_user.is_bot
        )
        if not (is_pv or is_reply):
            return

        await db.get_or_create_user(
            message.from_user.id, message.from_user.full_name, message.from_user.username
        )
        await db.increment_user_msg_count(message.from_user.id)

        sys_prompt = (
            "تو آقای دانشور معاون سابق مدرسه و معلم دفاعی پایه نهم هستی. "
            "کوتاه، طنزآمیز، خودمونی و در حد ۱ تا ۲ جمله جواب بده."
        )
        reply = await ai_service.generate_text(
            system_instruction=sys_prompt,
            prompt=f"پیام کاربر ({message.from_user.full_name}): {message.text[:500]}",
            priority=1,
        )
        await message.reply(sanitize_telegram_html(reply))

    return router


async def run_bot() -> None:
    settings = load_settings()
    setup_logging(settings)

    lock = ProcessLock()
    if not lock.acquire():
        logger.critical("یک نمونه دیگر از ربات آقای دانشور در حال اجراست. خروج.")
        sys.exit(1)

    db = DatabaseManager(settings.DATABASE_PATH, settings.TIMEZONE)
    await db.connect()

    ai_service = AIService(settings, db)
    await ai_service.start()

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(build_core_router(ai_service, db, settings.TIMEZONE))

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received. Stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # ویندوز

    try:
        logger.info("Starting Mr. Daneshvar PRO (Stage 1 Core)...")
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        await stop_event.wait()
        await dp.stop_polling()
        await polling_task
    finally:
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
