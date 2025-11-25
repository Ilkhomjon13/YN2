from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if TOKEN is None:
    raise ValueError("❌ BOT_TOKEN aniqlanmagan! .env faylni tekshiring.")
