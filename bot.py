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

# Load environment
load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN:
    raise SystemExit("TOKEN muhit o'zgaruvchisi topilmadi.")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL topilmadi.")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================== DATABASE ======================
async def setup_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:

        # CREATE surveys WITHOUT title column
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id SERIAL PRIMARY KEY,
            short_title TEXT,
            description TEXT,
            image TEXT,
            active BOOLEAN DEFAULT TRUE
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            channel TEXT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS voted_users (
            survey_id INT REFERENCES surveys(id) ON DELETE CASCADE,
            user_id BIGINT,
            PRIMARY KEY(survey_id, user_id)
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT now()
        );
        """)

# ====================== FSM ======================
class CreateSurvey(StatesGroup):
    waiting_for_short_title = State()
    waiting_for_description = State()
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
            [KeyboardButton(text="📋 So‘rovnomalarni ko‘rish"), KeyboardButton(text="📋 Obunachilar")],
            [KeyboardButton(text="✉️ Xabar yuborish"), KeyboardButton(text="📢 Kanal qo‘shish")]
        ],
        resize_keyboard=True
    )

def finish_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Tugatish")]],
        resize_keyboard=True
    )

def candidates_keyboard(candidates):
    buttons = []
    for c in candidates:
        buttons.append([InlineKeyboardButton(text=f"{c['name']}", callback_data=f"vote_{c['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def normalize_channel(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("https://t.me/"):
        path = v.replace("https://t.me/", "").strip()
        if "/" in path:
            return v
        return f"@{path}"
    if v.startswith("@"):
        return v
    return "@" + v

def join_button_for(channel: str) -> InlineKeyboardButton:
    if channel.startswith("@"):
        return InlineKeyboardButton(text=f"➕ {channel} ga obuna bo‘lish", url=f"https://t.me/{channel[1:]}")
    return InlineKeyboardButton(text="➕ Obuna bo‘lish", url="https://t.me")

# ====================== DB HELPERS ======================
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
        r = await conn.fetchrow("SELECT 1 FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id)
        return r is not None

async def add_vote(survey_id: int, candidate_id: int, user_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id)
            await conn.execute("INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)", survey_id, user_id)

async def is_member(bot: Bot, user_id: int, channel_raw: str) -> bool:
    ch = normalize_channel(channel_raw)
    try:
        member = await bot.get_chat_member(ch, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

# ====================== START ======================
@dp.message(F.text == "/start")
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name or ""

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, username, full_name)
            VALUES ($1,$2,$3)
            ON CONFLICT(id) DO UPDATE SET username=$2, full_name=$3
        """, user_id, username, fullname)

    if user_id == ADMIN_ID:
        return await message.answer("👨‍💼 Admin panel:", reply_markup=admin_keyboard())

    surveys = await get_surveys()
    if not surveys:
        return await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")

    buttons = []
    for s in surveys:
        buttons.append([
            InlineKeyboardButton(text=s["short_title"], callback_data=f"open_{s['id']}")
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=kb)

# ====================== ADMIN: CREATE SURVEY ======================
@dp.message(F.text == "➕ So‘rovnoma yaratish")
async def admin_create(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Qisqa nomni yuboring:", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_short_title)

@dp.message(CreateSurvey.waiting_for_short_title)
async def process_short(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    short = (message.text or "").strip()
    if not short:
        return await message.answer("Bo‘sh bo‘lmasin.")
    await state.update_data(short=short)
    await message.answer("To‘liq tavsifni yuboring:")
    await state.set_state(CreateSurvey.waiting_for_description)

@dp.message(CreateSurvey.waiting_for_description)
async def process_desc(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    desc = (message.text or "").strip()

    data = await state.get_data()
    short = data["short"]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO surveys (short_title, description) VALUES ($1,$2) RETURNING id",
            short, desc
        )

    await state.update_data(survey_id=row["id"])
    await message.answer("Rasm yuboring yoki 'Tugatish' bosing:", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_image)

@dp.message(CreateSurvey.waiting_for_image)
async def process_image(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    survey_id = data["survey_id"]

    if message.photo:
        file_id = message.photo[-1].file_id
        async with pool.acquire() as conn:
            await conn.execute("UPDATE surveys SET image=$1 WHERE id=$2", file_id, survey_id)

    await message.answer("Nomzod yuboring. Tugatish uchun 'Tugatish'.")
    await state.set_state(CreateSurvey.waiting_for_candidate)

@dp.message(CreateSurvey.waiting_for_candidate)
async def process_candidate(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "✅ Tugatish":
        await message.answer("Kanal yoki guruhlarni yuboring.")
        await state.set_state(CreateSurvey.waiting_for_channel)
        return

    name = (message.text or "").strip()
    data = await state.get_data()
    survey_id = data["survey_id"]

    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO candidates (survey_id,name) VALUES ($1,$2)", survey_id, name)

    await message.answer("Nomzod qo‘shildi.")

@dp.message(CreateSurvey.waiting_for_channel)
async def process_channel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "✅ Tugatish":
        await message.answer("So‘rovnoma yaratildi!", reply_markup=admin_keyboard())
        await state.clear()
        return

    ch = normalize_channel(message.text)
    data = await state.get_data()
    survey_id = data["survey_id"]

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO required_channels (survey_id,channel) VALUES ($1,$2)",
            survey_id, ch
        )

    await message.answer("Kanal qo‘shildi.")

# ====================== USER VIEW ======================
@dp.callback_query(F.data.startswith("open_"))
async def open_survey(query: types.CallbackQuery):
    survey_id = int(query.data.split("_")[1])
    survey, cands, chans = await get_survey(survey_id)

    caption = survey["description"]
    kb = candidates_keyboard(cands)

    if survey["image"]:
        await query.message.answer_photo(survey["image"], caption=caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)

    await query.answer()

@dp.callback_query(F.data.startswith("vote_"))
async def vote(query: types.CallbackQuery):
    cand_id = int(query.data.split("_")[1])

    async with pool.acquire() as conn:
        cand = await conn.fetchrow("SELECT * FROM candidates WHERE id=$1", cand_id)

    survey_id = cand["survey_id"]

    if await user_has_voted(survey_id, query.from_user.id):
        return await query.answer("Siz allaqachon ovoz bergansiz!", show_alert=True)

    _, _, chans = await get_survey(survey_id)

    not_joined = []
    for ch in chans:
        if not await is_member(bot, query.from_user.id, ch["channel"]):
            not_joined.append(ch["channel"])

    if not_joined:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [join_button_for(ch)] for ch in not_joined
        ] + [[InlineKeyboardButton(text="🔄 Tekshirish", callback_data=f"recheck_{survey_id}")]])

        await query.message.answer("Obuna shart!", reply_markup=kb)
        return await query.answer()

    await add_vote(survey_id, cand_id, query.from_user.id)

    _, cands, _ = await get_survey(survey_id)
    kb = candidates_keyboard(cands)

    try:
        await query.message.edit_reply_markup(kb)
    except:
        await query.message.answer("Yangilandi:", reply_markup=kb)

    await query.answer("Ovoz berildi!")

@dp.callback_query(F.data.startswith("recheck_"))
async def recheck(query: types.CallbackQuery):
    survey_id = int(query.data.split("_")[1])
    survey, cands, chans = await get_survey(survey_id)

    for ch in chans:
        if not await is_member(bot, query.from_user.id, ch["channel"]):
            return await query.answer("Hali obuna bo‘lmagansiz!", show_alert=True)

    kb = candidates_keyboard(cands)
    caption = survey["description"]

    if survey["image"]:
        await query.message.answer_photo(survey["image"], caption=caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)

    await query.answer("Obuna tasdiqlandi!", show_alert=True)

# ====================== RUN ======================
async def main():
    await setup_db()
    logging.info("Bot ishga tushdi…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
