import asyncio
import logging
import random
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.ai_service import AIService
from app.config import Settings
from app.database import DatabaseManager
from app.fun_features import FunFeatures
from app.memory_manager import MemoryManager
from app.utils import sanitize_telegram_html

logger = logging.getLogger(__name__)


class BotScheduler:
    """زمان‌بند وظایف پس‌زمینه: پاک‌سازی حافظه قدیمی و ارسال جمله روز (در صورت فعال بودن)."""

    def __init__(
        self,
        settings: Settings,
        db: DatabaseManager,
        memory: MemoryManager,
        ai_service: AIService,
        bot: Bot,
    ):
        self.settings = settings
        self.db = db
        self.memory = memory
        self.ai = ai_service
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_quote_date: Optional[str] = None
        self._last_maintenance_hour: Optional[int] = None

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("BotScheduler started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        logger.info("BotScheduler stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now(ZoneInfo(self.settings.TIMEZONE))
                today_str = now.strftime("%Y-%m-%d")

                # ۱. نگهداری و پاک‌سازی حافظه هر ۶ ساعت یک‌بار
                if now.hour % 6 == 0 and self._last_maintenance_hour != now.hour:
                    self._last_maintenance_hour = now.hour
                    await self.memory.run_maintenance()

                # ۲. ارسال جمله روز ساعت ۱۰ صبح (فقط در صورت روشن بودن تنظیم daily_quote_enabled)
                if now.hour == 10 and self._last_quote_date != today_str:
                    self._last_quote_date = today_str
                    await self._maybe_send_daily_quote()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Scheduler loop error: {exc}")

            await asyncio.sleep(45)

    async def _maybe_send_daily_quote(self) -> None:
        enabled = await self.db.get_setting("daily_quote_enabled", "0")
        paused = await self.db.get_setting("paused", "0")
        target_chat = self.settings.allowed_group_id

        if enabled != "1" or paused == "1" or not target_chat:
            return

        fallback = random.choice(FunFeatures.DAILY_QUOTES)
        raw_quote = await self.ai.generate_text(
            system_instruction="تو آقای دانشور معاون سابق مدرسه و معلم دفاعی هستی. یک جمله کوتاه و بامزه صبح‌بخیر و انضباطی برای شروع روز بچه‌های گروه بنویس (حداکثر ۲ جمله).",
            prompt="جمله روز امروز را بنویس.",
            priority=3,
            fallback_pool=FunFeatures.DAILY_QUOTES,
        )
        safe_quote = sanitize_telegram_html(raw_quote or fallback)
        try:
            await self.bot.send_message(target_chat, f"☀️ <b>پیام صبحگاهی دفتر معاونت:</b>\n{safe_quote}")
            logger.info("Sent scheduled daily quote to group.")
        except Exception as exc:
            logger.error(f"Failed to send daily quote: {exc}")
