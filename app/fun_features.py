import logging
import random
from typing import Optional

from aiogram import Bot, Router, types
from aiogram.filters import Command, CommandObject

from app.ai_service import AIService
from app.config import Settings
from app.database import DatabaseManager
from app.personality import DaneshvarPersonality
from app.utils import sanitize_telegram_html

logger = logging.getLogger(__name__)


class FunFeatures:
    """قابلیت‌های طنز مدرسه‌ای با احترام کامل به حریم خصوصی و وضعیت opt-out کاربران."""

    DAILY_QUOTES = [
        "صبح بخیر دانش‌آموزان! یادتون باشه نظم اول کاره؛ حتی تو گروه تلگرام هم خط‌کش ما حاضره!",
        "امروز حواستون به اطراف باشه؛ بعضی خبرها و بازی‌ها فقط برای اینه که شما رو ببرن سر میز مذاکره!",
        "توصیه امروز آقای دانشور: به جای خرج کردن پول پای آیتم‌های بازی که حکم قمار داره، یه کتاب بخونید!",
        "آمار دفتر معاونت نشون میده امروز بعضیا زیادی ساکتن؛ مشکوکه، خیلی مشکوکه!",
    ]

    SUMMON_REASONS = [
        "بررسی پرونده انضباطی و سروصدای بیش از حد در راهروهای گروه",
        "ارائه توضیحات قانع‌کننده درباره تئوری‌های پشت‌پرده اخیر",
        "تعیین تکلیف وضعیت دفاع راست تیم و حواشی فوتبالی",
        "پاسخ‌گویی به گزارش‌های واصله مبنی بر خرید بازی و گرایش به قمار دیجیتال",
    ]

    def __init__(
        self,
        settings: Settings,
        db: DatabaseManager,
        ai_service: AIService,
        personality: DaneshvarPersonality,
    ):
        self.settings = settings
        self.db = db
        self.ai = ai_service
        self.personality = personality
        self.router = Router(name="fun_router")
        self._register_routes()

    def _register_routes(self) -> None:
        self.router.message.register(self.cmd_enzebat, Command("enzebat"))
        self.router.message.register(self.cmd_ehzar, Command("ehzar"))
        self.router.message.register(self.cmd_parvande, Command("parvande"))
        self.router.message.register(self.cmd_poll, Command("poll"))

    async def _is_enabled(self) -> bool:
        return (await self.db.get_setting("fun_features_enabled", "1")) == "1"

    def _resolve_target(self, message: types.Message) -> tuple[Optional[int], Optional[str]]:
        if message.reply_to_message and message.reply_to_message.from_user:
            u = message.reply_to_message.from_user
            return u.id, u.full_name
        if message.from_user:
            return message.from_user.id, message.from_user.full_name
        return None, None

    async def cmd_enzebat(self, message: types.Message) -> None:
        """نمایش نمره انضباطی طنز کاربر (خودش یا شخص ریپلای‌شده)."""
        if not await self._is_enabled():
            return
        target_id, target_name = self._resolve_target(message)
        if not target_id or not target_name:
            return

        profile = await self.db.get_user_profile(target_id)
        if not profile or profile.opted_out:
            await message.answer("این دانش‌آموز خارج از سیستم امتیازدهی انضباطی (Opt-out) قرار دارد.")
            return

        score = profile.discipline_score
        if score >= 95:
            status = "دانش‌آموز نمونه و مایه افتخار درس آمادگی دفاعی!"
        elif score >= 80:
            status = "قابل قبول، ولی زیر ذره‌بین خط‌کش معاونت!"
        else:
            status = "در آستانه احضار فوری به دفتر معاونت!"

        safe_name = sanitize_telegram_html(target_name)
        await message.answer(
            f"📋 <b>کارنامه انضباطی {safe_name}</b>\n"
            f"• نمره فعلی: <b>{score} از ۱۰۰</b>\n"
            f"• وضعیت: {status}"
        )

    async def cmd_ehzar(self, message: types.Message, command: CommandObject) -> None:
        """صدور احضاریه طنزآمیز به دفتر معاونت."""
        if not await self._is_enabled():
            return
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer("برای صدور احضاریه، روی پیام فرد مورد نظر ریپلای بزنید و دستور /ehzar را بفرستید.")
            return

        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.answer("معاون مدرسه که خودش رو احضار نمی‌کنه عزیزم!")
            return

        profile = await self.db.get_user_profile(target.id)
        if profile and (profile.opted_out or profile.no_personal_jokes):
            await message.answer("این کاربر در حالت بدون شوخی شخصی قرار دارد؛ احضاریه صادر نمی‌شود.")
            return

        reason = sanitize_telegram_html(command.args.strip()) if command.args else random.choice(self.SUMMON_REASONS)
        safe_name = sanitize_telegram_html(target.full_name)

        await message.answer(
            f"📜 <b>احضاریه رسمی دفتر معاونت</b>\n"
            f"دانش‌آموز گرامی <b>{safe_name}</b>؛\n"
            f"به علت: <i>«{reason}»</i>\n"
            "موظفید فوراً با لبخند و بدون بهانه در دفتر آقای دانشور حاضر شوید. خط‌کش آماده است!"
        )

    async def cmd_parvande(self, message: types.Message) -> None:
        """نمایش پرونده طنز مبتنی بر اطلاعات غیرحساس و تأییدشده کاربر."""
        if not await self._is_enabled():
            return
        target_id, target_name = self._resolve_target(message)
        if not target_id or not target_name:
            return

        profile = await self.db.get_user_profile(target_id)
        if not profile or profile.opted_out:
            await message.answer("پرونده‌ای برای این کاربر در بایگانی عمومی موجود نیست.")
            return

        confirmed_interests = [i.value for i in profile.interests if i.confidence == "confirmed"]
        interests_str = "، ".join(confirmed_interests) if confirmed_interests else "در حال رصد نامحسوس"
        safe_name = sanitize_telegram_html(target_name)

        await message.answer(
            f"🗂 <b>پرونده محرمانه-طنز آقای {safe_name}</b>\n"
            f"• سابقه حضور در جمع: {profile.msg_count} پیام\n"
            f"• علایق قطعی کشف‌شده: {sanitize_telegram_html(interests_str)}\n"
            f"• تیپ شخصیتی در کلاس: {sanitize_telegram_html(profile.humor_style)}\n"
            f"• تراز انضباطی: {profile.discipline_score}/100\n"
            f"• یادداشت معاونت: {sanitize_telegram_html(profile.persona_summary)}"
        )

    async def cmd_poll(self, message: types.Message, command: CommandObject, bot: Bot) -> None:
        """ایجاد نظرسنجی تلگرامی برای داوری جمعی."""
        if message.chat.type == "private":
            await message.answer("نظرسنجی فقط در گروه قابل اجراست.")
            return

        raw = (command.args or "").strip()
        if "|" in raw:
            parts = [p.strip() for p in raw.split("|") if p.strip()]
            question = parts[0][:250]
            options = [opt[:95] for opt in parts[1:6]]
        else:
            question = raw[:250] if raw else "به نظر شما حق با کیه و آخر این بحث به میز مذاکره می‌کشه؟"
            options = ["صددرصد حق با آقای دانشوره", "نه، قضیه چیز دیگه‌ایه", "همش بازیه!"]

        if len(options) < 2:
            options = ["موافقم", "مخالفم"]

        try:
            await bot.send_poll(
                chat_id=message.chat.id,
                question=f"⚖️ نظرسنجی دفتر معاونت: {question}",
                options=options,
                is_anonymous=False,
            )
        except Exception as exc:
            logger.error(f"Failed to send poll: {exc}")
            await message.answer("امکان ایجاد نظرسنجی در این چت وجود ندارد.")
