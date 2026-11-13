import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.database import DatabaseManager
from app.rate_limiter import RateLimiter
from app.utils import sanitize_telegram_html

logger = logging.getLogger(__name__)


class AdminPanel:
    """پنل مدیریتی اختصاصی برای ADMIN_IDS با ذخیره‌سازی پایدار تنظیمات."""

    def __init__(self, settings: Settings, db: DatabaseManager, rate_limiter: RateLimiter):
        self.settings = settings
        self.db = db
        self.limiter = rate_limiter
        self.router = Router(name="admin_router")
        # ذخیره موقت متن broadcast تا لحظه تأیید با دکمه: admin_id -> text
        self._pending_broadcasts: dict[int, str] = {}
        self._register_routes()

    def _is_admin(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.settings.admin_id_list

    def _register_routes(self) -> None:
        self.router.message.register(self.cmd_admin, Command("admin"))
        self.router.message.register(self.cmd_stats, Command("stats"))
        self.router.message.register(self.cmd_top, Command("top"))
        self.router.message.register(self.cmd_profile, Command("profile"))
        self.router.message.register(self.cmd_memory, Command("memory"))
        self.router.message.register(self.cmd_forget, Command("forget"))
        self.router.message.register(self.cmd_pause, Command("pause"))
        self.router.message.register(self.cmd_resume, Command("resume"))
        self.router.message.register(self.cmd_setprob, Command("setprob"))
        self.router.message.register(self.cmd_setlimit, Command("setlimit"))
        self.router.message.register(self.cmd_jokes, Command("jokes"))
        self.router.message.register(self.cmd_deljoke, Command("deljoke"))
        self.router.message.register(self.cmd_toggle_feature, Command("toggle"))
        self.router.message.register(self.cmd_broadcast, Command("broadcast"))

        self.router.callback_query.register(self.cb_admin_forget, F.data.startswith("adm_forget:"))
        self.router.callback_query.register(self.cb_admin_broadcast, F.data.startswith("adm_bcast:"))

    def _extract_target_user_id(self, message: types.Message, args: Optional[str]) -> Optional[int]:
        if message.reply_to_message and message.reply_to_message.from_user:
            return message.reply_to_message.from_user.id
        if args and args.strip().isdigit():
            return int(args.strip())
        return None

    async def cmd_admin(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return

        paused = await self.db.get_setting("paused", "0") == "1"
        prob = float(await self.db.get_setting("response_prob", str(self.settings.RESPONSE_PROBABILITY)))
        limit = int(await self.db.get_setting("max_replies_hour", str(self.settings.MAX_REPLIES_PER_HOUR)))
        fun_enabled = await self.db.get_setting("fun_features_enabled", "1") == "1"
        daily_quote = await self.db.get_setting("daily_quote_enabled", "0") == "1"

        status_str = "⏸ متوقف (Paused)" if paused else "▶️ فعال (Online)"
        text = (
            "🛠 <b>پنل مدیریت آقای دانشور PRO</b>\n"
            f"• وضعیت فعلی: <b>{status_str}</b>\n"
            f"• احتمال ورود تصادفی: <b>{prob * 100:.1f}%</b>\n"
            f"• سقف پاسخ در ساعت: <b>{limit}</b>\n"
            f"• قابلیت‌های طنز (نمره/احضاریه): <b>{'روشن' if fun_enabled else 'خاموش'}</b>\n"
            f"• جمله روز خودکار: <b>{'روشن' if daily_quote else 'خاموش'}</b>\n\n"
            "<b>دستورات مدیریتی:</b>\n"
            "• /stats : آمار کامل و مصرف API\n"
            "• /top : فعال‌ترین دانش‌آموزان گروه\n"
            "• /profile [ID یا ریپلای] : مشاهده پرونده کاربر\n"
            "• /memory [ID یا ریپلای] : خاطرات ثبت‌شده کاربر\n"
            "• /forget [ID یا ریپلای] : حذف کامل حافظه کاربر\n"
            "• /jokes و /deljoke [ID] : مدیریت شوخی‌های داخلی\n"
            "• /pause و /resume : توقف یا ادامه پاسخ‌دهی\n"
            "• /setprob &lt;0-100&gt; : تنظیم درصد ورود تصادفی\n"
            "• /setlimit &lt;عدد&gt; : تنظیم سقف پاسخ ساعتی\n"
            "• /toggle &lt;fun|quote&gt; : روشن/خاموش کردن قابلیت‌ها\n"
            "• /broadcast &lt;متن&gt; : ارسال اطلاعیه به گروه"
        )
        logger.info(f"Admin command /admin executed by user_id={message.from_user.id}")
        await message.answer(text)

    async def cmd_stats(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        stats = await self.db.get_total_stats()
        today_str = datetime.now(ZoneInfo(self.settings.TIMEZONE)).strftime("%Y-%m-%d")
        today_calls = await self.db.get_daily_api_count(today_str)

        text = (
            "📊 <b>آمار عملکرد و مصرف API</b>\n"
            f"• کاربران ثبت‌شده: <b>{stats['users_count']}</b> نفر\n"
            f"• کل پیام‌های پردازش‌شده در گروه: <b>{stats['total_messages']}</b>\n"
            f"• خاطرات ثبت‌شده در FTS5: <b>{stats['episodes_count']}</b>\n"
            f"• تماس‌های API امروز: <b>{today_calls} / {self.settings.DAILY_API_CALL_LIMIT}</b>\n"
            f"• کل تماس‌های API از ابتدا: <b>{stats['api_requests']}</b>\n"
            f"• مجموع توکن تخمینی مصرف‌شده: <b>{stats['est_tokens']:,}</b>"
        )
        logger.info(f"Admin command /stats executed by user_id={message.from_user.id}")
        await message.answer(text)

    async def cmd_top(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        top_users = await self.db.get_top_active_users(limit=10)
        if not top_users:
            await message.answer("هنوز آماری از فعالیت اعضای گروه ثبت نشده است.")
            return

        lines = ["🏆 <b>فعال‌ترین دانش‌آموزان گروه:</b>"]
        for idx, u in enumerate(top_users, 1):
            name = sanitize_telegram_html(u.full_name)
            lines.append(f"{idx}. {name} — {u.msg_count} پیام (نمره انضباطی: {u.discipline_score})")
        logger.info(f"Admin command /top executed by user_id={message.from_user.id}")
        await message.answer("\n".join(lines))

    async def cmd_profile(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        target_id = self._extract_target_user_id(message, command.args)
        if not target_id:
            await message.answer("لطفاً روی پیام کاربر ریپلای کنید یا شناسه عددی او را جلوی دستور بنویسید.")
            return

        profile = await self.db.get_user_profile(target_id)
        if not profile:
            await message.answer("پرونده‌ای برای این شناسه یافت نشد.")
            return
        if profile.opted_out:
            await message.answer("این کاربر گزینه Opt-out را فعال کرده و داده‌های شخصیتی او غیرفعال است.")
            return

        interests = "، ".join(f"{i.value} ({i.confidence})" for i in profile.interests) or "نامشخص"
        text = (
            f"🗂 <b>پرونده گروهی {sanitize_telegram_html(profile.full_name)}</b> (<code>{profile.user_id}</code>)\n"
            f"• تعداد پیام گروه: {profile.msg_count}\n"
            f"• علایق: {sanitize_telegram_html(interests)}\n"
            f"• سبک شوخی / ارتباط: {sanitize_telegram_html(profile.humor_style)} / {sanitize_telegram_html(profile.comm_style)}\n"
            f"• نمره انضباطی طنز: {profile.discipline_score}\n"
            f"• شوخی شخصی: {'ممنوع' if profile.no_personal_jokes else 'مجاز'}\n"
            f"• خلاصه: {sanitize_telegram_html(profile.persona_summary)}"
        )
        logger.info(f"Admin command /profile target={target_id} by admin={message.from_user.id}")
        await message.answer(text)

    async def cmd_memory(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        target_id = self._extract_target_user_id(message, command.args)
        if not target_id:
            await message.answer("روی پیام کاربر ریپلای کنید یا ID عددی بدهید.")
            return

        chat_id = self.settings.allowed_group_id or message.chat.id
        episodes = await self.db.get_user_episodes(chat_id=chat_id, user_id=target_id, limit=8)
        if not episodes:
            await message.answer("خاطره گروهی ثبت‌شده‌ای برای این کاربر پیدا نشد.")
            return

        lines = [f"🧠 <b>خاطرات گروهی مرتبط با کاربر <code>{target_id}</code>:</b>"]
        for ep in episodes:
            lines.append(f"• [{ep.event_date}] {sanitize_telegram_html(ep.summary)} (اهمیت: {ep.importance})")
        logger.info(f"Admin command /memory target={target_id} by admin={message.from_user.id}")
        await message.answer("\n".join(lines))

    async def cmd_forget(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        target_id = self._extract_target_user_id(message, command.args)
        if not target_id:
            await message.answer("روی پیام کاربر ریپلای کنید یا ID عددی او را بنویسید.")
            return

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ تأیید حذف پرونده کاربر",
                        callback_data=f"adm_forget:yes:{target_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ لغو",
                        callback_data=f"adm_forget:no:{target_id}",
                    ),
                ]
            ]
        )
        await message.answer(
            f"آیا از حذف کامل پروفایل و پیام‌های اخیر کاربر <code>{target_id}</code> مطمئن هستید؟",
            reply_markup=kb,
        )

    async def cb_admin_forget(self, callback: types.CallbackQuery) -> None:
        if not callback.from_user or not self._is_admin(callback.from_user.id) or not callback.data:
            await callback.answer("دسترسی غیرمجاز!", show_alert=True)
            return

        _, action, target_str = callback.data.split(":")
        target_id = int(target_str)
        if action == "yes":
            await self.db.delete_user_data(target_id)
            logger.info(f"Admin {callback.from_user.id} deleted data for user_id={target_id}")
            await callback.message.edit_text(f"🗑 داده‌های کاربر <code>{target_id}</code> توسط مدیر پاک شد.")
        else:
            await callback.message.edit_text("عملیات حذف لغو شد.")
        await callback.answer()

    async def cmd_pause(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        await self.db.set_setting("paused", "1")
        logger.info(f"Bot paused by admin={message.from_user.id}")
        await message.answer("⏸ پاسخ‌دهی ربات موقتاً متوقف شد. برای شروع مجدد /resume را بزنید.")

    async def cmd_resume(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        await self.db.set_setting("paused", "0")
        logger.info(f"Bot resumed by admin={message.from_user.id}")
        await message.answer("▶️ بههه! آقای دانشور دوباره برگشت سر پستش! پاسخ‌دهی فعال شد.")

    async def cmd_setprob(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        if not command.args:
            await message.answer("فرمت صحیح: <code>/setprob 10</code> (عدد بین ۰ تا ۱۰۰ درصد)")
            return
        try:
            pct = float(command.args.strip())
            if not (0.0 <= pct <= 100.0):
                raise ValueError
            prob = pct / 100.0
            await self.db.set_setting("response_prob", str(prob))
            logger.info(f"Admin {message.from_user.id} set response_prob={prob}")
            await message.answer(f"✅ احتمال پایه ورود تصادفی روی <b>{pct:.1f}%</b> تنظیم و ذخیره شد.")
        except ValueError:
            await message.answer("❌ لطفاً یک عدد معتبر بین ۰ تا ۱۰۰ وارد کنید.")

    async def cmd_setlimit(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        if not command.args or not command.args.strip().isdigit():
            await message.answer("فرمت صحیح: <code>/setlimit 25</code> (عدد بین ۱ تا ۲۰۰)")
            return
        val = int(command.args.strip())
        if not (1 <= val <= 200):
            await message.answer("❌ عدد باید بین ۱ تا ۲۰۰ باشد.")
            return
        await self.db.set_setting("max_replies_hour", str(val))
        self.limiter.update_limits(val)
        logger.info(f"Admin {message.from_user.id} set max_replies_hour={val}")
        await message.answer(f"✅ سقف پاسخ در ساعت روی <b>{val}</b> پیام تنظیم و ذخیره شد.")

    async def cmd_jokes(self, message: types.Message) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        chat_id = self.settings.allowed_group_id or message.chat.id
        jokes = await self.db.get_confirmed_inside_jokes(chat_id=chat_id, limit=15)
        if not jokes:
            await message.answer("هنوز شوخی داخلی تأییدشده‌ای ثبت نشده است.")
            return
        lines = ["😂 <b>شوخی‌های داخلی ثبت‌شده گروه:</b>"]
        for j in jokes:
            lines.append(
                f"• شناسه <code>{j['id']}</code>: «{sanitize_telegram_html(j['phrase'])}» (تکرار: {j['mention_count']})"
            )
        lines.append("\nبرای حذف هر مورد: <code>/deljoke شناسه</code>")
        await message.answer("\n".join(lines))

    async def cmd_deljoke(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        if not command.args or not command.args.strip().isdigit():
            await message.answer("فرمت صحیح: <code>/deljoke 3</code>")
            return
        jid = int(command.args.strip())
        ok = await self.db.delete_inside_joke(jid)
        if ok:
            logger.info(f"Admin {message.from_user.id} deleted inside_joke id={jid}")
            await message.answer(f"🗑 شوخی داخلی با شناسه <code>{jid}</code> حذف شد.")
        else:
            await message.answer("شوخی داخلی با این شناسه پیدا نشد.")

    async def cmd_toggle_feature(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        arg = (command.args or "").strip().lower()
        if arg == "fun":
            curr = await self.db.get_setting("fun_features_enabled", "1")
            new_val = "0" if curr == "1" else "1"
            await self.db.set_setting("fun_features_enabled", new_val)
            await message.answer(f"✅ قابلیت‌های طنز (نمره انضباطی/احضاریه) {'روشن' if new_val == '1' else 'خاموش'} شد.")
        elif arg == "quote":
            curr = await self.db.get_setting("daily_quote_enabled", "0")
            new_val = "0" if curr == "1" else "1"
            await self.db.set_setting("daily_quote_enabled", new_val)
            await message.answer(f"✅ ارسال خودکار جمله روز {'روشن' if new_val == '1' else 'خاموش'} شد.")
        else:
            await message.answer("فرمت صحیح: <code>/toggle fun</code> یا <code>/toggle quote</code>")

    async def cmd_broadcast(self, message: types.Message, command: CommandObject) -> None:
        if not message.from_user or not self._is_admin(message.from_user.id):
            return
        if not command.args or not command.args.strip():
            await message.answer("متن پیام را جلوی دستور بنویسید: <code>/broadcast سلام بچه‌ها</code>")
            return
        if not self.settings.allowed_group_id:
            await message.answer("❌ برای استفاده از broadcast باید متغیر <code>GROUP_CHAT_ID</code> در تنظیمات تعریف شده باشد.")
            return

        admin_id = message.from_user.id
        clean_preview = sanitize_telegram_html(command.args.strip()[:1000])
        self._pending_broadcasts[admin_id] = clean_preview

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📤 تأیید و ارسال به گروه", callback_data="adm_bcast:send"),
                    InlineKeyboardButton(text="❌ لغو", callback_data="adm_bcast:cancel"),
                ]
            ]
        )
        await message.answer(
            f"📢 <b>پیش‌نمایش پیام مدیریتی برای ارسال به گروه:</b>\n\n{clean_preview}",
            reply_markup=kb,
        )

    async def cb_admin_broadcast(self, callback: types.CallbackQuery, bot: Bot) -> None:
        if not callback.from_user or not self._is_admin(callback.from_user.id) or not callback.data:
            await callback.answer("دسترسی غیرمجاز!", show_alert=True)
            return

        admin_id = callback.from_user.id
        action = callback.data.split(":")[1]
        text = self._pending_broadcasts.pop(admin_id, None)

        if action == "cancel" or not text:
            await callback.message.edit_text("❌ ارسال پیام مدیریتی لغو شد.")
            await callback.answer()
            return

        target_chat = self.settings.allowed_group_id
        if not target_chat:
            await callback.message.edit_text("❌ GROUP_CHAT_ID تنظیم نشده است.")
            await callback.answer()
            return

        try:
            await bot.send_message(target_chat, f"📣 <b>اطلاعیه دفتر معاونت:</b>\n{text}")
            logger.info(f"Admin {admin_id} sent broadcast to chat_id={target_chat}")
            await callback.message.edit_text("✅ پیام با موفقیت به گروه ارسال شد.")
        except Exception as exc:
            logger.error(f"Broadcast failed: {exc}")
            await callback.message.edit_text(f"❌ خطا در ارسال پیام به گروه: {sanitize_telegram_html(str(exc))}")
        await callback.answer()
