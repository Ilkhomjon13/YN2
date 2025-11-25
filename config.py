from dotenv import load_dotenv
import os

load_dotenv()  # .env faylni o'qish

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
