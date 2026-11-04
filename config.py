# File: config.py
import os
from pathlib import Path
from typing import Optional
from urllib.parse import unquote
from dotenv import load_dotenv

load_dotenv()

USERNAME_PREFIX: str = "Holoween"
START_SEQUENCE: int = 111  # 00111


class AppConfig:
    def __init__(self) -> None:
        self.bot_token: str = os.getenv("BOT_TOKEN", "8501538404:AAHhJsmPGNW1DD4U34E5RyMG5BVyR9O_oGA").strip()
        admin_raw: str = os.getenv("ADMIN_TELEGRAM_ID", "8951751786").strip()
        self.x_bearer_token: str = unquote(os.getenv("X_API_BEARER_TOKEN", "").strip())
        self.db_path: Path = Path(os.getenv("DB_PATH", "holoween_bot.db").strip())
        self.log_path: Path = Path(os.getenv("LOG_FILE", "holoween_bot.log").strip())

        if not self.bot_token:
            raise ValueError("Missing required environment variable: BOT_TOKEN")
        if not admin_raw or not admin_raw.lstrip("-").isdigit():
            raise ValueError("Missing or invalid integer for ADMIN_TELEGRAM_ID")
        if not self.x_bearer_token:
            raise ValueError("Missing required environment variable: X_API_BEARER_TOKEN")

        self.admin_id: int = int(admin_raw)
