import asyncio
import logging
import random
import time
from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji

from app.ai_service import AIService
from app.config import Settings
from app.database import DatabaseManager
from app.event_detector import EventDetector
from app.human_features import HumanFeaturesManager
from app.memory_manager import MemoryManager
from app.personality import DaneshvarPersonality
from app.prompt_builder import PromptBuilder
from app.rate_limiter import RateLimiter
from app.safety import SafetyGuard
from app.trigger_engine import TriggerContext, TriggerEngine
from app.utils import get_jalali_date_str, sanitize_telegram_html

logger = logging.getLogger(__name__)


class SocialHandlers:
    """مدیریت پیام‌های گروهی و خصوصی با رفع باگ‌های گروه و سیستم قهر و آشتی."""

    NAME_TRIGGERS = ("دانشور", "دانوش", "معاون", "استاد")
    APOLOGY_WORDS = ("ببخشید", "شرمنده", "غلط کردم", "معذرت", "حلال کن", "شوخی کردم", "ناراحت نشو")

    def __init__(
        self,
        settings: Settings,
        db: DatabaseManager,
        ai_service: AIService,
        personality: DaneshvarPersonality,
        memory: MemoryManager,
        rate_limiter: RateLimiter,
        event_detector: EventDetector,
        human_features: HumanFeaturesManager,
    ):
        self.settings = settings
        self.db = db
        self.ai = ai_service
        self.personality = personality
        self.memory = memory
        self.limiter = rate_limiter
        self.detector = event_detector
        self.human = human_features
        self.router = Router(name="social_router")

        # تایمر ۱۵ دقیقه‌ای قهر و سرسنگین بودن با کاربر (user_id -> timestamp پایان قهر)
        self._grudge_until: dict[int, float] = {}
        self._bot_username_cache: Optional[str] = None

        self._register_routes()

    def _register_routes(self) -> None:
        self.router.my_chat_member.register(self.on_bot_added_to_group)

        # دستورات عمومی و حریم خصوصی
        self.router.message.register(self.cmd_start, CommandStart())
        self.router.message.register(self.cmd_about, Command("about"))
        self.router.message.register(self.cmd_privacy, Command("privacy"))
        self.router.message.register(self.cmd_mydata, Command("mydata"))
        self.router.message.register(self.cmd_forgetme, Command("forgetme"))
        self.router.message.register(self.cmd_optout, Command("optout"))
        self.router.message.register(self.cmd_optin, Command("optin"))
        self.router.message.register(self.cmd_judge, Command("judge"))

        # دستورات پرونده انضباطی، لقب و خاطرات واقعی مدرسه
        self.router.message.register(self.cmd_file, Command("file"))
        self.router.message.register(self.cmd_add_lore, Command("add_lore"))
        self.router.message.register(self.cmd_list_lore, Command("list_lore"))
        self.router.message.register(self.cmd_del_lore, Command("del_lore"))
        self.router.message.register(self.cmd_set_nick, Command("set_nick"))

        self.router.callback_query.register(self.cb_confirm_forgetme, F.data.startswith("forgetme:"))

        # پردازش تمام پیام‌های متنی عادی
        self.router.message.register(self.on_text_message, F.text)

    def _is_admin(self, user_id: Optional[int]) -> bool:
        if not user_id:
            return False
        admins = self.settings.admin_id_list
        return True if not admins else (user_id in admins)

    async def _get_bot_username(self, bot: Bot) -> str:
        if self._bot_username_cache:
            return self._bot_username_cache
        try:
            me = await bot.get_me()
            if me.username:
                self._bot_username_cache = me.username.lower()
                return self._bot_username_cache
        except Exception:
            pass
        return (self.settings.BOT_USERNAME or "").lstrip("@").lower()

    async def _is_group_allowed(self, chat: types.Chat, bot: Bot) -> bool:
        if chat.type == "private":
            return True
        allowed_id = self.settings.allowed_group_id
        if allowed_id is not None and chat.id != allowed_id:
            logger.warning(f"Unauthorized group detected (chat_id={chat.id}). Leaving chat.")
            try:
                await bot.send_message(
                    chat.id,
                    "عذرخواهی می‌کنم عزیزان، حوزه استحفاظی بنده فقط گروه تعیین‌شده توسط مدیریت است. خدانگهدار!",
                )
                await bot.leave_chat(chat.id)
            except Exception as exc:
                logger.error(f"Failed leaving unauthorized group {chat.id}: {exc}")
            return False
        return True

    async def on_bot_added_to_group(self, event: types.ChatMemberUpdated, bot: Bot) -> None:
        if event.new_chat_member.status not in ("member", "administrator"):
            return
        if not await self._is_group_allowed(event.chat, bot):
            return

        intro = (
            "بههه! درود بر همه دانش‌آموزان عزیز! آقای دانشور هستم؛ معاون سابق و معلم آمادگی دفاعی شما.\n\n"
            "📌 <b>شفافیت و حریم خصوصی:</b> بنده برای اینکه جمع‌مون باصفاتر باشه، علایق عمومی و شوخی‌های "
            "مجاز گروه رو به خاطر می‌سپرم (هیچ اطلاعات حساس یا پیام خصوصی ذخیره نمیشه). "
            "هر زمان خواستید با دستور /privacy جزئیات رو ببینید یا با /optout از پروفایل‌سازی خارج بشید."
        )
        await bot.send_message(event.chat.id, intro)

    async def cmd_start(self, message: types.Message, bot: Bot) -> None:
        if not message.from_user or not await self._is_group_allowed(message.chat, bot):
            return
        await self.db.get_or_create_user(
            message.from_user.id, message.from_user.full_name, message.from_user.username
        )
        name = sanitize_telegram_html(message.from_user.full_name)
        today = get_jalali_date_str(self.settings.TIMEZONE)
        await message.answer(
            f"بههه! آقای {name}! چه عجب پیدات شد! امروز {today} هست.\n"
            "برای آشنایی با قوانین و دستورات /about و /privacy رو بزن."
        )

    async def cmd_about(self, message: types.Message) -> None:
        text = (
            "🎓 <b>درباره آقای دانشور PRO</b>\n"
            "نسخه دیجیتال معاون سابق مدرسه و معلم آمادگی دفاعی پایه نهم!\n"
            "من اینجا هستم تا کنار بچه‌های گروه گپ بزنم، خاطرات خوب رو نگه دارم و نظم رو برقرار کنم.\n\n"
            "<b>دستورات کاربردی:</b>\n"
            "• /judge (یا ریپلای روی یک بحث): داوری رسمی آقای دانشور\n"
            "• /file : دیدن لقب و پرونده انضباطی شما (یا شخص ریپلای‌شده)\n"
            "• /mydata : مشاهده اطلاعات ذخیره‌شده درباره شما\n"
            "• /optout و /optin : توقف یا فعال‌سازی یادگیری شخصیت\n"
            "• /forgetme : پاک کردن کامل داده‌های شما\n"
            "• /add_lore و /list_lore و /del_lore : مدیریت خاطرات واقعی مدرسه (ادمین)\n"
            "• /set_nick : ثبت لقب با ریپلای روی پیام کاربر (ادمین)"
        )
        await message.answer(text)

    async def cmd_privacy(self, message: types.Message) -> None:
        text = (
            "🔒 <b>سیاست حریم خصوصی آقای دانشور</b>\n"
            "۱. <b>چه چیزی ذخیره می‌شود؟</b> فقط نام نمایشی، علایق عمومی (مثل فوتبال یا گیم)، سبک شوخی "
            "و خلاصه خاطرات جمعی گروه.\n"
            "۲. <b>خط قرمزها:</b> اطلاعات حساس (شماره تماس، کد ملی، خانواده، سلامت، مذهب، سیاست و وضعیت مالی) "
            "به‌طور خودکار فیلتر و حذف می‌شوند.\n"
            "۳. <b>ایزوله‌سازی پیام خصوصی (PV):</b> پیام‌های خصوصی شما هرگز وارد حافظه گروه نمی‌شوند "
            "و برای مدیران قابل مشاهده نیستند.\n"
            "۴. <b>کنترل کامل شما:</b> با /mydata داده‌هایتان را ببینید، با /optout یادگیری را متوقف کنید "
            "و با /forgetme همه‌چیز را برای همیشه پاک کنید."
        )
        await message.answer(text)

    async def cmd_mydata(self, message: types.Message) -> None:
        if not message.from_user:
            return
        profile = await self.db.get_user_profile(message.from_user.id)
        if not profile:
            await message.answer("هنوز هیچ پرونده‌ای برای شما در دفتر معاونت ثبت نشده است!")
            return

        interests = "، ".join(f"{i.value} ({i.confidence})" for i in profile.interests) or "ثبت نشده"
        status = "غیرفعال (Opt-out)" if profile.opted_out else "فعال"
        user_file = await self.human.get_user_file(message.from_user.id)
        nick_line = f"\n• لقب ثبت‌شده: «{sanitize_telegram_html(user_file[0])}»" if user_file else ""

        text = (
            f"📁 <b>داده‌های ثبت‌شده برای {sanitize_telegram_html(profile.full_name)}</b>\n"
            f"• وضعیت پروفایل‌سازی: {status}\n"
            f"• تعداد پیام‌های ثبت‌شده در گروه: {profile.msg_count}\n"
            f"• علایق تشخیص‌داده‌شده: {sanitize_telegram_html(interests)}\n"
            f"• سبک شوخی / ارتباط: {sanitize_telegram_html(profile.humor_style)} / {sanitize_telegram_html(profile.comm_style)}\n"
            f"• نمره انضباطی طنز: {profile.discipline_score} از ۱۰۰{nick_line}\n"
            f"• خلاصه شناخت: {sanitize_telegram_html(profile.persona_summary)}"
        )
        await message.answer(text)

    async def cmd_file(self, message: types.Message) -> None:
        if not message.from_user:
            return
        target = (
            message.reply_to_message.from_user
            if (message.reply_to_message and message.reply_to_message.from_user)
            else message.from_user
        )
        user_file = await self.human.get_user_file(target.id)
        target_name = sanitize_telegram_html(target.full_name)
        if not user_file:
            await message.answer(f"پرونده <b>{target_name}</b> فعلاً سفیده؛ هنوز سوتی خاصی نداده!")
            return
        nick, note = user_file
        note_str = f"\n• سابقه: {sanitize_telegram_html(note)}" if note else ""
        await message.answer(
            f"📋 <b>پرونده انضباطی {target_name}</b>\n"
            f"• لقب پیش آقای دانشور: «<b>{sanitize_telegram_html(nick)}</b>»{note_str}"
        )

    async def cmd_add_lore(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("این دستور فقط دست خودم و ادمین‌هاست بچه!")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer(
                "بعد از دستور، متن خاطره رو بنویس. مثال:\n"
                "<code>/add_lore پارسال سر صف صبحگاه میکروفون قطع شد</code>"
            )
            return
        lore_id = await self.human.add_lore(parts[1].strip(), message.from_user.id)
        await message.answer(f"حله! خاطره شماره <b>{lore_id}</b> رفت تو حافظه‌م، سرِ بزنگاه به روشون میارم.")

    async def cmd_list_lore(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        items = await self.human.list_all_lores()
        if not items:
            await message.answer("هنوز خاطره‌ای ثبت نکردی. با /add_lore اضافه کن.")
            return
        lines = [f"<b>{lid}.</b> {sanitize_telegram_html(txt)}" for lid, txt in items]
        await message.answer("📚 <b>لیست خاطره‌های ثبت‌شده:</b>\n\n" + "\n".join(lines))

    async def cmd_del_lore(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await message.answer("شماره خاطره رو بنویس. مثال: <code>/del_lore 2</code>")
            return
        ok = await self.human.delete_lore(int(parts[1].strip()))
        await message.answer("پاک شد رفت پی کارش." if ok else "همچین شماره‌ای پیدا نکردم.")

    async def cmd_set_nick(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            await message.answer("فقط ادمین می‌تونه لقب ثبت کنه!")
            return
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer("روی پیام کسی که می‌خوای بهش لقب بدی ریپلای کن و بنویس:\n<code>/set_nick سلطان بهانه</code>")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("لقب رو جلوی دستور بنویس. مثال: <code>/set_nick کاپیتان قلابی</code>")
            return
        target = message.reply_to_message.from_user
        nick = parts[1].strip()
        await self.human.set_user_nickname(target.id, nick, "ثبت‌شده توسط مدیر گروه")
        await message.answer(
            f"تمام! از این به بعد تو پرونده <b>{sanitize_telegram_html(target.full_name)}</b> به اسم «<b>{sanitize_telegram_html(nick)}</b>» شناخته می‌شه."
        )

    async def cmd_forgetme(self, message: types.Message) -> None:
        if not message.from_user:
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ بله، تمام داده‌های من پاک شود",
                        callback_data=f"forgetme:yes:{message.from_user.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ انصراف",
                        callback_data=f"forgetme:no:{message.from_user.id}",
                    ),
                ]
            ]
        )
        await message.answer(
            "آیا مطمئن هستید که می‌خواهید کل پروفایل و پیام‌های اخیر شما از حافظه ربات پاک شود؟",
            reply_markup=kb,
        )

    async def cb_confirm_forgetme(self, callback: types.CallbackQuery) -> None:
        if not callback.data or not callback.from_user:
            return
        parts = callback.data.split(":")
        action, target_id = parts[1], int(parts[2])

        if callback.from_user.id != target_id:
            await callback.answer("این دکمه متعلق به شخص دیگری است!", show_alert=True)
            return

        if action == "yes":
            await self.db.delete_user_data(target_id)
            await callback.message.edit_text("🗑 پرونده شما به طور کامل از دفتر آقای دانشور پاک شد.")
        else:
            await callback.message.edit_text("عملیات حذف لغو شد. پرونده شما همچنان محفوظ است.")
        await callback.answer()

    async def cmd_optout(self, message: types.Message) -> None:
        if not message.from_user:
            return
        await self.db.get_or_create_user(
            message.from_user.id, message.from_user.full_name, message.from_user.username
        )
        await self.db.set_user_opt_out(message.from_user.id, True)
        await message.answer(
            "درخواست شما ثبت شد. از این لحظه هیچ داده شخصیتی جدیدی از شما تحلیل نمی‌شود و شوخی شخصی نخواهیم داشت."
        )

    async def cmd_optin(self, message: types.Message) -> None:
        if not message.from_user:
            return
        await self.db.get_or_create_user(
            message.from_user.id, message.from_user.full_name, message.from_user.username
        )
        await self.db.set_user_opt_out(message.from_user.id, False)
        await message.answer("بههه! خوش برگشتید به جمع! پروفایل‌سازی دوستانه برای شما دوباره فعال شد.")

    async def cmd_judge(self, message: types.Message, bot: Bot) -> None:
        if not message.from_user or not await self._is_group_allowed(message.chat, bot):
            return

        is_pv = message.chat.type == "private"
        topic_text = message.text or ""
        if message.reply_to_message and message.reply_to_message.text:
            topic_text += f" | موضوع ریپلای: {message.reply_to_message.text}"

        profile, snapshot, episodes = await self.memory.retrieve_context_for_reply(
            chat_id=message.chat.id,
            is_private=is_pv,
            user_id=message.from_user.id,
            current_text=topic_text,
        )

        mode = self.personality.MODES["serious_teacher"]
        sys_inst, prompt = PromptBuilder.build_reply_prompts(
            mode=mode,
            user_profile=profile,
            relevant_episodes=episodes,
            recent_messages_summary=snapshot.formatted_history,
            reply_context="درخواست داوری رسمی با دستور /judge داده شده است. مثل یک معلم و داور بی‌طرف ولی بامزه حکم نهایی را صادر کن.",
            sender_name=message.from_user.full_name,
            current_message=topic_text,
            blocked_phrases=self.personality.get_blocked_signatures(),
            tehran_time_context=self.human.get_tehran_time_context(),
            user_file=await self.human.get_user_file(message.from_user.id),
            custom_lore=await self.human.get_random_lores(limit=2),
        )
        raw_reply = await self.ai.generate_text(
            system_instruction=sys_inst,
            prompt=prompt,
            priority=1,
            fallback_pool=self.personality.FALLBACK_RESPONSES["serious_teacher"],
        )
        safe_reply = SafetyGuard.sanitize_model_output(
            raw_reply, self.personality.get_fallback("serious_teacher")
        )
        await self._send_safe_reply(message, f"⚖️ <b>حکم آقای دانشور:</b>\n{safe_reply}")

    async def on_text_message(self, message: types.Message, bot: Bot) -> None:
        """خط اصلی پردازش پیام‌های متنی گروه و خصوصی."""
        if not message.from_user or message.from_user.is_bot or not message.text:
            return
        if not await self._is_group_allowed(message.chat, bot):
            return

        paused_flag = await self.db.get_setting("paused", "0")
        if paused_flag == "1":
            return

        chat_id = message.chat.id
        user_id = message.from_user.id
        full_name = message.from_user.full_name
        text = message.text.strip()
        lower_text = text.lower()
        is_pv = message.chat.type == "private"

        is_fresh = await self.limiter.register_human_message(chat_id, user_id, message.message_id, text)
        if not is_fresh:
            return

        # ۱. ثبت در حافظه سه‌لایه و پرونده انضباطی
        reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
        profile = await self.memory.process_incoming_message(
            chat_id=chat_id,
            is_private=is_pv,
            message_id=message.message_id,
            user_id=user_id,
            full_name=full_name,
            username=message.from_user.username,
            text=text,
            reply_to_id=reply_to_id,
        )
        await self.human.auto_update_disciplinary_file(user_id, text)

        # مدیریت قهر ۱۵ دقیقه‌ای و آشتی با عذرخواهی (فقط مورد ۴)
        if any(bad in lower_text for bad in self.human.INSULT_TRIGGERS):
            self._grudge_until[user_id] = time.time() + 900
        elif user_id in self._grudge_until and any(ap in lower_text for ap in self.APOLOGY_WORDS):
            del self._grudge_until[user_id]

        # ۲. بازیابی بافت گروه (محافظت‌شده در برابر خطا تا در گروه متوقف نشود)
        rep_text = message.reply_to_message.text if message.reply_to_message else None
        rep_sender = (
            message.reply_to_message.from_user.full_name
            if (message.reply_to_message and message.reply_to_message.from_user)
            else None
        )
        _, snapshot, episodes = await self.memory.retrieve_context_for_reply(
            chat_id=chat_id,
            is_private=is_pv,
            user_id=user_id,
            current_text=text,
            reply_to_message_id=reply_to_id,
            reply_to_text=rep_text,
            reply_to_sender=rep_sender,
        )

        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot.id
        )
        bot_username = await self._get_bot_username(bot)
        is_mentioned = bool(bot_username and f"@{bot_username}" in lower_text)
        is_name_called = any(name in lower_text for name in self.NAME_TRIGGERS)

        if not is_pv:
            try:
                event = await self.detector.inspect_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    sender_name=full_name,
                    text=text,
                    snapshot=snapshot,
                    is_opted_out=profile.opted_out,
                )
                if event.should_record_episode and event.episode_summary:
                    await self.memory.record_episode(
                        chat_id=chat_id,
                        summary=event.episode_summary,
                        user_ids=[user_id],
                        tags=event.episode_tags,
                        importance=event.importance,
                    )
                if event.discipline_delta != 0:
                    await self.db.adjust_discipline_score(user_id, event.discipline_delta)
            except Exception as exc:
                logger.warning(f"EventDetector non-fatal error in group {chat_id}: {exc}")

            # ری‌اکشن هوشمند انسانی (شامل 🖕 هنگام بی‌احترامی)
            await self.human.maybe_react(
                message,
                is_reply_to_bot=(is_reply_to_bot or is_mentioned or is_name_called),
                probability=0.45,
            )

        # ۳. تصمیم‌گیری درباره پاسخ‌گویی
        sec_since_last = await self.limiter.seconds_since_last_reply(chat_id)
        prob_str = await self.db.get_setting("response_prob")
        base_prob = float(prob_str) if prob_str is not None else self.settings.RESPONSE_PROBABILITY

        # اگر کاربر با @ یا ریپلای یا صدا زدن اسم «دانشور/دانوش/معاون» مخاطب قرارش داده، حتماً جواب بدهد
        direct_mention_flag = is_mentioned or is_name_called

        trigger_ctx = TriggerContext(
            is_private=is_pv,
            is_command=text.startswith("/"),
            is_reply_to_bot=is_reply_to_bot,
            is_mentioned=direct_mention_flag,
            text=text,
            base_probability=base_prob,
            recent_msg_count_5min=snapshot.recent_msg_count_5min,
            active_participants_5min=snapshot.active_participants_5min,
            seconds_since_last_bot_reply=sec_since_last,
            group_tone=snapshot.tone,
            is_crowded=snapshot.is_crowded,
            has_active_discussion=snapshot.has_active_discussion,
        )
        decision = TriggerEngine.evaluate(trigger_ctx)
        if not decision.should_reply:
            logger.info(
                f"Group message skipped by TriggerEngine in chat={chat_id} | "
                f"reply_to_bot={is_reply_to_bot} | mentioned={direct_mention_flag} | reason={decision.reason}"
            )
            return

        # ۴. بررسی محدودیت نرخ ارسال (Rate Limiter)
        allowed, limit_reason = await self.limiter.can_reply(chat_id, user_id, decision.is_definite)
        if not allowed:
            logger.info(f"Reply suppressed by rate limiter ({limit_reason}) in chat={chat_id}")
            return

        # ۵. انتخاب حالت رفتاری (با لحاظ کردن وضعیت قهر)
        is_distress = snapshot.tone in ("serious", "sensitive") or SafetyGuard.detect_distress_or_serious(text)
        is_bot_q = SafetyGuard.is_serious_bot_identity_question(text)

        is_grudging = time.time() < self._grudge_until.get(user_id, 0.0)
        if is_grudging and not is_distress:
            mode = self.personality.MODES["roast_troll"]
        else:
            mode = self.personality.select_mode(message_text=text, group_tone=snapshot.tone)

        blocked_phrases = self.personality.get_blocked_signatures()

        sys_inst, prompt = PromptBuilder.build_reply_prompts(
            mode=mode,
            user_profile=profile,
            relevant_episodes=episodes,
            recent_messages_summary=snapshot.formatted_history,
            reply_context=snapshot.reply_context,
            sender_name=full_name,
            current_message=text,
            blocked_phrases=blocked_phrases,
            is_serious_distress=is_distress,
            is_bot_identity_query=is_bot_q,
            tehran_time_context=self.human.get_tehran_time_context(),
            user_file=await self.human.get_user_file(user_id),
            custom_lore=await self.human.get_random_lores(limit=3),
        )
        if is_grudging and not is_distress:
            sys_inst += "\n[وضعیت روحی]: این کاربر چند دقیقه پیش به تو بی‌احترامی کرده و هنوز عذرخواهی نکرده؛ باهاش سرسنگین و تیکه‌پرون باش!"

        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

        priority = 1 if decision.is_definite else 2
        fallback_text = self.personality.get_fallback(mode.key, is_serious_distress=is_distress)

        raw_reply = await self.ai.generate_text(
            system_instruction=sys_inst,
            prompt=prompt,
            priority=priority,
            fallback_pool=self.personality.FALLBACK_RESPONSES.get(mode.key),
        )

        safe_reply = SafetyGuard.sanitize_model_output(raw_reply, fallback_text)
        self.personality.record_output_phrases(safe_reply)

        await self._send_safe_reply(message, safe_reply)
        await self.limiter.record_bot_reply(chat_id, user_id)
        logger.info(f"Replied in chat={chat_id} | mode={mode.key} | trigger={decision.reason}")

    async def _send_safe_reply(self, message: types.Message, html_text: str) -> None:
        try:
            await message.reply(html_text)
        except TelegramRetryAfter as exc:
            logger.warning(f"Telegram RetryAfter: sleeping {exc.retry_after}s")
            await asyncio.sleep(exc.retry_after)
            await message.reply(html_text)
        except TelegramBadRequest as exc:
            logger.error(f"Telegram BadRequest on reply: {exc}")

    async def _try_react(self, message: types.Message, emoji: str) -> None:
        try:
            await message.react([ReactionTypeEmoji(emoji=emoji)])
        except Exception:
            pass
