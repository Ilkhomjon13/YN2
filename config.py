# config.py
import os

TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # Sizning ID
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:parol@host:port/dbname"
)
