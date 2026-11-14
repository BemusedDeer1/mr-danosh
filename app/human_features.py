import logging
import random
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram.types import Message, ReactionTypeEmoji

logger = logging.getLogger(__name__)


class HumanFeaturesManager:
    """مدیریت ویژگی‌های انسانی: ری‌اکشن هوشمند، درک زمان تهران، پرونده و لقب بچه‌ها، و خاطرات واقعی (Lore)."""

    DAYS_FA = {
        0: "دوشنبه",
        1: "سه‌شنبه",
        2: "چهارشنبه",
        3: "پنج‌شنبه",
        4: "جمعه",
        5: "شنبه",
        6: "یک‌شنبه",
    }

    INSULT_TRIGGERS = (
        "خفه", "چرت", "پرت", "اسکل", "گمشو", "زر", "بی‌سواد", "بی سواد",
        "احمق", "ببند", "برو بابا", "رید", "عن", "گوه", "الاغ", "گاو", "خر",
        "بیشعور", "بی شعور", "روانی", "دیوونه", "خنگ", "پیرمرد", "فسیل",
        "مفت خور", "پاچه خوار", "دهنتو", "سیکتیر", "هری", "بتمرگ", "کص", "کون", "کیر",
    )

    def __init__(self, db_path: str = "data/daneshvar.db", timezone: str = "Asia/Tehran"):
        self.db_path = db_path
        self.tz = ZoneInfo(timezone)

    async def init_tables(self) -> None:
        """ساخت جدول‌های خاطرات واقعی و لقب بچه‌ها در دیتابیس دائمی."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_lore (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    added_by INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_nicknames (
                    user_id INTEGER PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    offense_note TEXT DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()
        logger.info("HumanFeatures tables (custom_lore, user_nicknames) initialized.")

    # ==================== ۱. درک ساعت و روز هفته ====================
    def get_tehran_time_context(self) -> str:
        now = datetime.now(self.tz)
        day_name = self.DAYS_FA[now.weekday()]
        hour = now.hour
        time_str = now.strftime("%H:%M")

        if 0 <= hour < 5:
            mood_hint = "نصفه‌شب و دیروقته؛ اگه کسی بیداره گیر بده چرا نخوابیده و فردا سر کلاس چرت می‌زنه."
        elif 5 <= hour < 8:
            mood_hint = "صبح خیلی زوده؛ وقت بیدار شدن و آماده شدن برای مدرسه و صف صبحگاهه."
        elif 8 <= hour < 14:
            mood_hint = "تایم مدرسه و کلاسه؛ بگو سر کلاس گوشی دستش چیکار می‌کنه!"
        elif 14 <= hour < 18:
            mood_hint = "بعدازظهره؛ وقت استراحت و درس خوندن بعد از مدرسه‌ست."
        elif 18 <= hour < 22:
            mood_hint = "سر شبه؛ وقت گپ زدن و کل‌کل بچه‌ها تو گروهه."
        else:
            mood_hint = "آخر شبه؛ کم‌کم وقت خوابه."

        if day_name in ("پنج‌شنبه", "جمعه"):
            mood_hint += f" امروز {day_name} و آخر هفته‌ست، بچه‌ها تعطیلن و فاز تفریح دارن."

        return f"امروز {day_name}، ساعت {time_str} به وقت تهرانه. ({mood_hint})"

    # ==================== ۲. ری‌اکشن هوشمند (شامل 🖕) ====================
    def pick_reaction_emoji(self, text: str, is_reply_to_bot: bool = False) -> Optional[str]:
        t = text.strip().lower()
        if not t or t.startswith("/"):
            return None

        # اگر بی‌احترامی یا کل‌کل تند بود -> ری‌اکشن فاک!
        if any(bad in t for bad in self.INSULT_TRIGGERS):
            return "🖕"

        # اگر مستقیم به ربات تیکه انداخته ولی فحش نبود
        if is_reply_to_bot and any(w in t for w in ("برو بابا", "باشه تو راست میگی", "عمراً", "زرنگ", "الکی")):
            return random.choice(["🖕", "🤡", "🤨"])

        # خنده و مسخره‌بازی
        if any(w in t for w in ("ههه", "😂", "🤣", "جر", "پاره", "سم", "اسید", "خندیدم")):
            return random.choice(["😂", "💀"])

        # کل‌کل فوتبالی، گیم و کری‌خوانی
        if any(w in t for w in ("گل", "بردیم", "باخت", "فیفا", "پی اس", "رونالدو", "مسی", "قهرمان", "گیم")):
            return random.choice(["🔥", "👀", "🤡"])

        # بهانه‌آوردن برای درس و امتحان
        if any(w in t for w in ("امتحان", "نخوندم", "نمره", "تقلب", "مشق", "خسته", "دفاعی")):
            return random.choice(["🤨", "👎"])

        # سلام و احترام
        if any(w in t for w in ("سلام", "درود", "عشقی", "مخلصیم", "نوکرم", "دمت گرم")):
            return random.choice(["🫡", "❤️", "👍"])

        return None

    async def maybe_react(self, message: Message, is_reply_to_bot: bool = False, probability: float = 0.45) -> bool:
        """ارسال ری‌اکشن روی پیام کاربر (در صورت بی‌احترامی ۱۰۰٪ ری‌اکشن می‌زند)."""
        if not message.text:
            return False

        emoji = self.pick_reaction_emoji(message.text, is_reply_to_bot=is_reply_to_bot)
        if not emoji:
            return False

        # اگر بی‌احترامی بود همیشه ری‌اکشن فاک را بزند؛ در بقیه موارد با احتمال مشخص
        if emoji != "🖕" and random.random() > probability:
            return False

        try:
            await message.react([ReactionTypeEmoji(emoji=emoji)])
            return True
        except Exception as exc:
            # اگر در یک گروه خاص ایموجی 🖕 مجاز نبود، بلافاصله سراغ ایموجی جایگزین می‌رود
            if emoji == "🖕":
                try:
                    await message.react([ReactionTypeEmoji(emoji="🤡")])
                    return True
                except Exception:
                    pass
            logger.debug(f"Could not set reaction '{emoji}': {exc}")
            return False

    # ==================== ۳. پرونده‌سازی و لقب دادن به بچه‌ها ====================
    async def set_user_nickname(self, user_id: int, nickname: str, note: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_nicknames (user_id, nickname, offense_note, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    offense_note = CASE WHEN excluded.offense_note != '' THEN excluded.offense_note ELSE user_nicknames.offense_note END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, nickname.strip(), note.strip()),
            )
            await db.commit()

    async def get_user_file(self, user_id: int) -> Optional[tuple[str, str]]:
        """برگرداندن (لقب، سابقه پرونده) برای کاربر."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT nickname, offense_note FROM user_nicknames WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return (row[0], row[1] or "")
        return None

    async def auto_update_disciplinary_file(self, user_id: int, text: str) -> None:
        """ثبت خودکار لقب و سابقه در پرونده کاربر بر اساس رفتار و کل‌کل‌هایش."""
        t = text.lower()
        existing = await self.get_user_file(user_id)

        if any(bad in t for bad in self.INSULT_TRIGGERS):
            await self.set_user_nickname(
                user_id,
                nickname=existing[0] if existing else "زبون‌درازِ بی‌انضباط",
                note="سابقه بی‌احترامی و کل‌کل با معاون در گروه",
            )
        elif not existing:
            if any(w in t for w in ("فوتبال", "گل", "پاس", "دفاع", "مسی", "رونالدو")):
                await self.set_user_nickname(user_id, "کاپیتانِ قلابی", "ادعای زیاد در فوتبال")
            elif any(w in t for w in ("فیفا", "گیم", "بازی", "پی اس")):
                await self.set_user_nickname(user_id, "گیمرِ ولخرج", "علاقه شدید به خرج کردن پول پای بازی")
            elif any(w in t for w in ("نخوندم", "امتحان", "نمره", "تقلب")):
                await self.set_user_nickname(user_id, "سلطانِ بهانه", "همیشه از امتحان و درس فراریه")

    # ==================== ۴. خاطرات و تکیه‌کلام‌های واقعی (Lore) ====================
    async def add_lore(self, content: str, added_by: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO custom_lore (content, added_by) VALUES (?, ?)",
                (content.strip(), added_by),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_random_lores(self, limit: int = 3) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT content FROM custom_lore ORDER BY RANDOM() LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def list_all_lores(self) -> list[tuple[int, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id, content FROM custom_lore ORDER BY id DESC LIMIT 30") as cursor:
                rows = await cursor.fetchall()
                return [(r[0], r[1]) for r in rows]

    async def delete_lore(self, lore_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM custom_lore WHERE id = ?", (lore_id,))
            await db.commit()
            return cursor.rowcount > 0
