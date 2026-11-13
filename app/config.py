import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretMaskingFilter(logging.Filter):
    """فیلتر لاگ برای ماسک کردن خودکار توکن‌ها و کلیدهای API."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self.secrets:
            if secret in msg:
                msg = msg.replace(secret, "***MASKED_SECRET***")
        # ماسک کردن الگوهای احتمالی توکن تلگرام یا کلید گوگل
        msg = re.sub(r"\b\d{8,11}:[A-Za-z0-9_-]{30,50}\b", "***MASKED_BOT_TOKEN***", msg)
        msg = re.sub(r"\bAIza[0-9A-Za-z-_]{35}\b", "***MASKED_GEMINI_KEY***", msg)
        record.msg = msg
        record.args = ()
        return True


class Settings(BaseSettings):
    """تنظیمات اعتبارسنجی‌شده پروژه از فایل محیطی."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str = Field(..., min_length=10)
    GEMINI_API_KEY: str = Field(..., min_length=10)
    BOT_USERNAME: str = Field(..., min_length=3)
    ADMIN_IDS: str = Field(..., min_length=1)
    GROUP_CHAT_ID: Optional[str] = Field(default=None)
    DATABASE_PATH: str = Field(default="data/daneshvar.db")
    AI_MODEL: str = Field(..., min_length=3)

    RESPONSE_PROBABILITY: float = Field(default=0.06, ge=0.0, le=1.0)
    MAX_REPLIES_PER_HOUR: int = Field(default=20, ge=1, le=500)
    MIN_REPLY_INTERVAL_SEC: int = Field(default=5, ge=1, le=300)
    DAILY_API_CALL_LIMIT: int = Field(default=400, ge=10, le=10000)

    AI_TEMPERATURE: float = Field(default=0.85, ge=0.0, le=2.0)
    AI_MAX_OUTPUT_TOKENS: int = Field(default=220, ge=50, le=1024)
    AI_ENABLE_SAFETY_FILTERS: bool = Field(default=True)

    TIMEZONE: str = Field(default="Asia/Tehran")
    LOG_LEVEL: str = Field(default="INFO")

    @field_validator("BOT_USERNAME")
    @classmethod
    def clean_username(cls, v: str) -> str:
        return v.strip().lstrip("@").lower()

    @property
    def admin_id_list(self) -> list[int]:
        ids: list[int] = []
        for part in self.ADMIN_IDS.split(","):
            part = part.strip()
            if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
                ids.append(int(part))
        return ids

    @property
    def allowed_group_id(self) -> Optional[int]:
        if not self.GROUP_CHAT_ID or not self.GROUP_CHAT_ID.strip():
            return None
        val = self.GROUP_CHAT_ID.strip()
        if val.lstrip("-").isdigit():
            return int(val)
        return None


def load_settings() -> Settings:
    """بارگذاری و اعتبارسنجی متغیرهای محیطی با پیام خطای شفاف فارسی."""
    load_dotenv()
    required_vars = {
        "BOT_TOKEN": "توکن ربات تلگرام",
        "GEMINI_API_KEY": "کلید API جمنای",
        "BOT_USERNAME": "نام کاربری ربات تلگرام",
        "ADMIN_IDS": "شناسه عددی مدیران",
        "AI_MODEL": "نام مدل هوش مصنوعی (AI_MODEL)",
    }

    missing = [f"- {k} ({desc})" for k, desc in required_vars.items() if not os.getenv(k, "").strip()]
    if missing:
        print("❌ خطای راه‌اندازی: متغیرهای الزامی زیر در فایل .env مقداردهی نشده‌اند:", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        print("لطفاً فایل .env.example را به .env کپی کرده و مقادیر را کامل کنید.", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = Settings()
        if not cfg.admin_id_list:
            print("❌ خطای راه‌اندازی: مقدار ADMIN_IDS باید شامل حداقل یک شناسه عددی معتبر باشد.", file=sys.stderr)
            sys.exit(1)
        return cfg
    except Exception as exc:
        print(f"❌ خطا در اعتبارسنجی تنظیمات محیطی: {exc}", file=sys.stderr)
        sys.exit(1)


def setup_logging(settings: Settings) -> None:
    """پیکربندی لاگ ساختاریافته با چرخش فایل و فیلتر امنیت."""
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    secret_filter = SecretMaskingFilter([settings.BOT_TOKEN, settings.GEMINI_API_KEY])

    file_handler = RotatingFileHandler(
        log_dir / "daneshvar.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(secret_filter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(secret_filter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # کاهش لاگ‌های پرحجم کتابخانه‌های جانبی
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
