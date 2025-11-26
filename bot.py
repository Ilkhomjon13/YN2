# bot.py
import logging
import asyncio
import os
from datetime import datetime

import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# Load .env
load_dotenv()

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN:
    logging.error("TOKEN muhit o'zgaruvchisi topilmadi. Iltimos TOKEN ni o'rnating.")
    raise SystemExit("TOKEN muhit o'zgaruvchisi topilmadi. Iltimos TOKEN ni o'rnating.")
if not DATABASE_URL:
    logging.error("DATABASE_URL topilmadi. Iltimos DATABASE_URL ni o'rnating.")
    raise SystemExit("DATABASE_URL topilmadi. Iltimos DATABASE_URL ni o'rnating.")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================== DATABASE ======================
pool: asyncpg.pool.Pool = None

async def setup_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            image TEXT,
            active BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS required_channels (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            channel TEXT
        );
        CREATE TABLE IF NOT EXISTS voted_users (
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            user_id BIGINT,
            PRIMARY KEY(survey_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT now()
        );
        """)

# ====================== FSM ======================
class CreateSurvey(StatesGroup):
    waiting_for_title = State()
    waiting_for_image = State()
    waiting_for_candidate = State()
    waiting_for_channel = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()

# ====================== KEYBOARDS ======================
def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ So‘rovnoma yaratish")],
            [KeyboardButton(text="📋 Obunachilar"), KeyboardButton(text="✉️ Xabar yuborish")],
            [KeyboardButton(text="📢 Kanal qo‘shish")]
        ],
        resize_keyboard=True
    )

def finish_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Tugatish")]],
        resize_keyboard=True
    )

def candidates_keyboard_premium(candidates):
    buttons = []
    for c in candidates:
        label = f"✨ {c['name']} — {c['votes']} ovoz"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"vote_{c['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ====================== HELPERS ======================
def normalize_channel(value: str) -> str:
    v = value.strip()
    if v.startswith("https://t.me/"):
        path = v.replace("https://t.me/", "").strip()
        if "/" in path:
            return v
        return f"@{path}"
    if v.startswith("@"):
        return v
    return v

def join_button_for(channel: str) -> InlineKeyboardButton:
    ch = channel.strip()
    if ch.startswith("@"):
        return InlineKeyboardButton(text=f"➕ {ch} ga obuna bo‘lish", url=f"https://t.me/{ch[1:]}")
    if ch.startswith("https://t.me/"):
        return InlineKeyboardButton(text="➕ Obuna bo‘lish", url=ch)
    return InlineKeyboardButton(text="➕ Kanal/guruhga obuna bo‘lish", url="https://t.me")

async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true ORDER BY id DESC")

async def get_survey(survey_id: int):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1 ORDER BY id", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1 ORDER BY channel", survey_id)
        return survey, candidates, channels

async def user_has_voted(survey_id: int, user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id)
        return row is not None

async def add_vote(survey_id: int, candidate_id: int, user_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id)
            await conn.execute("INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)", survey_id, user_id)

async def is_member(bot: Bot, user_id: int, channel_raw: str) -> bool:
    ch = channel_raw.strip()
    if ch.startswith("-100") or (ch.lstrip("-").isdigit() and len(ch) > 3):
        try:
            member = await bot.get_chat_member(int(ch), user_id)
            return member.status in ("member", "administrator", "creator")
        except Exception:
            return False
    if ch.startswith("https://t.me/"):
        path = ch.replace("https://t.me/", "").strip()
        if "/" in path:
            return False
        ch = "@" + path
    if not ch.startswith("@"):
        ch = "@" + ch
    try:
        member = await bot.get_chat_member(ch, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ====================== USER REGISTRATION ON START ======================
@dp.message(F.text == "/start")
async def start_handler(message: types.Message):
    # Save user to users table (upsert)
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = (message.from_user.full_name or "").strip()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, username, full_name, joined_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (id) DO UPDATE SET username=$2, full_name=$3
        """, user_id, username, full_name)
    if message.from_user.id == ADMIN_ID:
        await message.answer("👨‍💼 Admin panel:", reply_markup=admin_keyboard())
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(text=s['title'], callback_data=f"open_{s['id']}")] for s in surveys]
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ====================== ADMIN: Obunachilar (subscribers) ======================
@dp.message(F.text == "📋 Obunachilar")
async def admin_subscribers(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, username, full_name, joined_at FROM users ORDER BY joined_at DESC")
    if not rows:
        await message.answer("Hozircha botga a'zo foydalanuvchi yo'q.")
        return
    # Show count and first 100 users (soddalashtirilgan)
    total = len(rows)
    text = f"👥 Botga a'zo foydalanuvchilar: {total}\n\n"
    # Limit output to 100 lines to avoid huge messages
    for r in rows[:100]:
        uname = f"@{r['username']}" if r['username'] else ""
        name = r['full_name'] or ""
        text += f"- {r['id']} {uname} {name}\n"
    if total > 100:
        text += f"\n... va yana {total-100} ta foydalanuvchi."
    await message.answer(text)

# ====================== ADMIN: Broadcast (Xabar yuborish) ======================
@dp.message(F.text == "✉️ Xabar yuborish")
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📣 Yuboriladigan xabar matnini yuboring. Matn, rasm yoki fayl yuborishingiz mumkin. Bekor qilish uchun /cancel yozing.")
    await state.set_state(Broadcast.waiting_for_message)

@dp.message(Broadcast.waiting_for_message)
async def admin_broadcast_receive(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    # Gather recipients
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM users")
    if not rows:
        await message.answer("Botga a'zo foydalanuvchi topilmadi.")
        await state.clear()
        return

    user_ids = [r['id'] for r in rows]
    sent = 0
    failed = 0

    # Prepare content: support text, photo, document
    # Text
    if message.text:
        content_type = "text"
        text = message.text
    elif message.photo:
        content_type = "photo"
        photo = message.photo[-1].file_id
        caption = message.caption or ""
    elif message.document:
        content_type = "document"
        doc = message.document.file_id
        caption = message.caption or ""
    else:
        await message.answer("Qo'llab-quvvatlanmaydigan turdagi xabar. Iltimos matn, rasm yoki fayl yuboring.")
        await state.clear()
        return

    await message.answer(f"Xabar {len(user_ids)} ta foydalanuvchiga yuborilmoqda. Iltimos kuting...")

    for uid in user_ids:
        try:
            if content_type == "text":
                await bot.send_message(uid, text)
            elif content_type == "photo":
                await bot.send_photo(uid, photo, caption=caption)
            elif content_type == "document":
                await bot.send_document(uid, doc, caption=caption)
            sent += 1
            await asyncio.sleep(0.05)  # rate limit pause
        except Exception:
            failed += 1
            logging.exception(f"Broadcast yuborishda xato: user_id={uid}")
            await asyncio.sleep(0.05)

    await message.answer(f"Xabar yuborildi. Muvaffaqiyatli: {sent}; muvaffaqiyatsiz: {failed}.")
    await state.clear()

@dp.message(F.text == "/cancel")
async def cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Amal bekor qilindi.")

# ====================== (qolgan admin va user handlerlar: survey yaratish, vote, recheck, stop, delete) ======================
# ... (bu yerda avvalgi survey, vote, recheck, admin stop/delete va boshqa handlerlar bo'lishi kerak)
# Agar siz hozirgi faylingizda ular mavjud bo'lsa, yuqoridagi yangi funksiyalarni shu faylga qo'shing.
# Agar kerak bo'lsa, men butun faylni to'liq yangilab yuboraman (sizning oxirgi versiyangizga moslab).

# ====================== RUN ======================
async def main():
    await setup_db()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
