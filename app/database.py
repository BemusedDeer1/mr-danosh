import json
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from app.models import EpisodeRecord, TraitItem, UserProfile
from app.utils import normalize_persian, now_iso

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


class DatabaseManager:
    """مدیریت اتصال SQLite در حالت WAL، اجرای Migration و DAOها."""

    def __init__(self, db_path: str, timezone: str = "Asia/Tehran"):
        self.db_path = db_path
        self.timezone = timezone
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """ایجاد دایرکتوری دیتابیس، برقراری اتصال و اجرای Migration."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row

        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.conn.execute("PRAGMA busy_timeout=5000;")

        await self._migrate()
        logger.info(f"Database connected and migrated (v{CURRENT_SCHEMA_VERSION}): {self.db_path}")

    async def close(self) -> None:
        """بستن ایمن اتصال دیتابیس."""
        if self.conn:
            await self.conn.close()
            self.conn = None
            logger.info("Database connection closed.")

    async def _migrate(self) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER PRIMARY KEY, updated_at TEXT NOT NULL)"
        )
        cursor = await self.conn.execute("SELECT MAX(version) as v FROM schema_meta")
        row = await cursor.fetchone()
        current_v = row["v"] if row and row["v"] is not None else 0

        if current_v < 1:
            await self._apply_v1()
            await self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta (version, updated_at) VALUES (?, ?)",
                (1, now_iso(self.timezone)),
            )
            await self.conn.commit()

    async def _apply_v1(self) -> None:
        assert self.conn is not None
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                msg_count INTEGER DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                interests_json TEXT DEFAULT '[]',
                humor_style TEXT DEFAULT 'نامشخص',
                comm_style TEXT DEFAULT 'معمولی',
                relationships_json TEXT DEFAULT '[]',
                inside_jokes_json TEXT DEFAULT '[]',
                persona_summary TEXT DEFAULT 'هنوز شناخت زیادی از این دانش‌آموز ثبت نشده است.',
                discipline_score INTEGER DEFAULT 100,
                no_personal_jokes INTEGER DEFAULT 0,
                opted_out INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS short_term_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                text TEXT NOT NULL,
                reply_to_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stm_chat_time ON short_term_messages(chat_id, created_at);

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                normalized_summary TEXT NOT NULL,
                event_date TEXT NOT NULL,
                user_ids_json TEXT DEFAULT '[]',
                tags TEXT DEFAULT '',
                importance INTEGER DEFAULT 3,
                last_recalled_at TEXT,
                recall_count INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_chat ON episodes(chat_id, importance DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                normalized_summary,
                tags,
                content='episodes',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
                INSERT INTO episodes_fts(rowid, normalized_summary, tags)
                VALUES (new.id, new.normalized_summary, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
                INSERT INTO episodes_fts(episodes_fts, rowid, normalized_summary, tags)
                VALUES ('delete', old.id, old.normalized_summary, old.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
                INSERT INTO episodes_fts(episodes_fts, rowid, normalized_summary, tags)
                VALUES ('delete', old.id, old.normalized_summary, old.tags);
                INSERT INTO episodes_fts(rowid, normalized_summary, tags)
                VALUES (new.id, new.normalized_summary, new.tags);
            END;

            CREATE TABLE IF NOT EXISTS inside_jokes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                phrase TEXT NOT NULL,
                normalized_phrase TEXT NOT NULL,
                mention_count INTEGER DEFAULT 1,
                user_ids_json TEXT DEFAULT '[]',
                is_confirmed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jokes_chat ON inside_jokes(chat_id, normalized_phrase);

            CREATE TABLE IF NOT EXISTS api_usage (
                date_str TEXT PRIMARY KEY,
                request_count INTEGER DEFAULT 0,
                est_tokens INTEGER DEFAULT 0
            );
            """
        )

    # --- تنظیمات پویا ---
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value))
        )
        await self.conn.commit()

    # --- پروفایل کاربران ---
    async def get_or_create_user(
        self, user_id: int, full_name: str, username: Optional[str] = None
    ) -> UserProfile:
        assert self.conn is not None
        now = now_iso(self.timezone)
        cursor = await self.conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()

        if not row:
            await self.conn.execute(
                """
                INSERT INTO user_profiles (user_id, full_name, username, first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, full_name, username, now, now, now),
            )
            await self.conn.commit()
            cursor = await self.conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
        else:
            await self.conn.execute(
                "UPDATE user_profiles SET full_name = ?, username = ?, last_seen = ? WHERE user_id = ?",
                (full_name, username, now, user_id),
            )
            await self.conn.commit()

        return self._row_to_profile(row)

    async def increment_user_msg_count(self, user_id: int) -> int:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE user_profiles SET msg_count = msg_count + 1 WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()
        cursor = await self.conn.execute("SELECT msg_count FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return int(row["msg_count"]) if row else 0

    async def delete_user_data(self, user_id: int) -> bool:
        """حذف کامل داده‌های یک کاربر بنا به درخواست او یا ادمین."""
        assert self.conn is not None
        cursor = await self.conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        await self.conn.execute("DELETE FROM short_term_messages WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def set_user_opt_out(self, user_id: int, opted_out: bool) -> None:
        assert self.conn is not None
        val = 1 if opted_out else 0
        await self.conn.execute(
            "UPDATE user_profiles SET opted_out = ?, updated_at = ? WHERE user_id = ?",
            (val, now_iso(self.timezone), user_id),
        )
        await self.conn.commit()

    def _row_to_profile(self, row: aiosqlite.Row) -> UserProfile:
        interests = [TraitItem(**i) for i in json.loads(row["interests_json"] or "[]")]
        relationships = [TraitItem(**r) for r in json.loads(row["relationships_json"] or "[]")]
        inside_jokes = json.loads(row["inside_jokes_json"] or "[]")
        return UserProfile(
            user_id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
            msg_count=row["msg_count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            interests=interests,
            humor_style=row["humor_style"],
            comm_style=row["comm_style"],
            relationships=relationships,
            inside_jokes=inside_jokes,
            persona_summary=row["persona_summary"],
            discipline_score=row["discipline_score"],
            no_personal_jokes=bool(row["no_personal_jokes"]),
            opted_out=bool(row["opted_out"]),
            updated_at=row["updated_at"],
        )

    # --- خاطرات اپیزودیک و FTS5 ---
    async def add_episode(self, ep: EpisodeRecord) -> int:
        assert self.conn is not None
        norm_summary = normalize_persian(ep.summary)
        norm_tags = normalize_persian(ep.tags)
        cursor = await self.conn.execute(
            """
            INSERT INTO episodes (chat_id, summary, normalized_summary, event_date, user_ids_json, tags, importance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ep.chat_id, ep.summary, norm_summary, ep.event_date, json.dumps(ep.user_ids), norm_tags, ep.importance),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def search_episodes_fts(self, chat_id: int, query_text: str, limit: int = 3) -> list[EpisodeRecord]:
        assert self.conn is not None
        norm_q = normalize_persian(query_text)
        tokens = [t for t in norm_q.split() if len(t) >= 2]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in tokens[:6])
        try:
            cursor = await self.conn.execute(
                """
                SELECT e.* FROM episodes e
                JOIN episodes_fts fts ON e.id = fts.rowid
                WHERE e.chat_id = ? AND episodes_fts MATCH ?
                ORDER BY e.importance DESC, e.id DESC
                LIMIT ?
                """,
                (chat_id, fts_query, limit),
            )
            rows = await cursor.fetchall()
            return [
                EpisodeRecord(
                    id=r["id"],
                    chat_id=r["chat_id"],
                    summary=r["summary"],
                    event_date=r["event_date"],
                    user_ids=json.loads(r["user_ids_json"] or "[]"),
                    tags=r["tags"],
                    importance=r["importance"],
                    last_recalled_at=r["last_recalled_at"],
                    recall_count=r["recall_count"],
                )
                for r in rows
            ]
        except Exception as exc:
            logger.warning(f"FTS search warning: {exc}")
            return []

    # --- آمار مصرف API ---
    async def log_api_usage(self, date_str: str, est_tokens: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT INTO api_usage (date_str, request_count, est_tokens)
            VALUES (?, 1, ?)
            ON CONFLICT(date_str) DO UPDATE SET
                request_count = request_count + 1,
                est_tokens = est_tokens + excluded.est_tokens
            """,
            (date_str, est_tokens),
        )
        await self.conn.commit()

    async def get_daily_api_count(self, date_str: str) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT request_count FROM api_usage WHERE date_str = ?", (date_str,)
        )
        row = await cursor.fetchone()
        return int(row["request_count"]) if row else 0
