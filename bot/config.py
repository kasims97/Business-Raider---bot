from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(slots=True)
class Settings:
    bot_token: str
    chat_id: int | None
    timezone: ZoneInfo
    post_hour: int
    post_minute: int
    db_path: Path
    log_level: str = "INFO"
    openai_api_key: str | None = None
    openai_summary_model: str = "gpt-5-mini"
    salute_bot_username: str = "salutespeechbot"

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = _require_env("BOT_TOKEN")
        chat_id_raw = os.getenv("CHAT_ID")
        chat_id = int(chat_id_raw) if chat_id_raw else None
        timezone = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
        post_hour = int(os.getenv("POST_HOUR", "21"))
        post_minute = int(os.getenv("POST_MINUTE", "0"))
        db_path = Path(os.getenv("DB_PATH", "/data/bot.sqlite3"))
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        openai_api_key = os.getenv("OPENAI_API_KEY")
        openai_summary_model = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-mini")
        salute_bot_username = os.getenv("SALUTE_BOT_USERNAME", "salutespeechbot").lower()
        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            timezone=timezone,
            post_hour=post_hour,
            post_minute=post_minute,
            db_path=db_path,
            log_level=log_level,
            openai_api_key=openai_api_key,
            openai_summary_model=openai_summary_model,
            salute_bot_username=salute_bot_username,
        )

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value
