import logging
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    GEMINI_API_KEY: str
    BOT_USERNAME: str

    # موتور پشتیبان Groq
    GROQ_API_KEY: Optional[str] = None
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    ADMIN_IDS: str = ""
    GROUP_CHAT_ID: Optional[str] = None

    AI_MODEL: str = "gemini-3.8-flash"
    AI_TEMPERATURE: float = 0.65
    AI_MAX_OUTPUT_TOKENS: int = 1000
    AI_ENABLE_SAFETY_FILTERS: bool = False

    DATABASE_PATH: str = "data/daneshvar.db"
    TIMEZONE: str = "Asia/Tehran"

    # افزایش سقف پاسخ‌دهی در گروه تا وسط چت قفل نکند
    RESPONSE_PROBABILITY: float = 0.12
    MIN_REPLY_INTERVAL_SEC: int = 2
    MAX_REPLIES_PER_HOUR: int = 150
    DAILY_API_CALL_LIMIT: int = 1500

    LOG_LEVEL: str = "INFO"

    @property
    def admin_id_list(self) -> list[int]:
        if not self.ADMIN_IDS:
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip().isdigit()]

    @property
    def allowed_group_id(self) -> Optional[int]:
        if not self.GROUP_CHAT_ID or not self.GROUP_CHAT_ID.strip():
            return None
        try:
            return int(self.GROUP_CHAT_ID.strip())
        except ValueError:
            return None


def load_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.BOT_USERNAME = settings.BOT_USERNAME.lstrip("@").lower()
    return settings


def setup_logging(settings: Settings) -> None:
    log_dir = Path(settings.DATABASE_PATH).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daneshvar.log"

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)
