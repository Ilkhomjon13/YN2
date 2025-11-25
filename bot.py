import logging
import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

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
            survey_id INT REFERENCES surveys(id),
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS voted_users (
            survey_id INT REFERENCES surveys(id),
            user_id BIGINT,
            PRIMARY KEY(survey_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS required_channels (
            survey_id INT REFERENCES surveys(id),
            channel TEXT
        );
        """)

# ====================== HELPERS ======================
async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true")

async def get_survey(survey_id: int):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1", survey_id)
        return survey, candidates, [ch['channel'] for ch in channels]

# ====================== KEYBOARDS ======================
def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ So‘rovnoma yaratish", callback_data="admin_create")],
        [InlineKeyboardButton(text="📋 So‘rovnomalarni ko‘rish", callback_data="admin_list")],
        [InlineKeyboardButton(text="📊 Natijalarni ko‘rish", callback_data="admin_results")]
    ])

def candidates_keyboard(candidates):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{c['name']} ⭐ {c['votes']}", callback_data=f"vote_{c['id']}")]
        for c in candidates
    ])

# ====================== ADMIN PANEL ======================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Siz admin emassiz!")
        return
    await message.answer("👨‍💼 Admin panel:", reply_markup=admin_main_keyboard())

# ➕ So‘rovnoma yaratish
@dp.callback_query(F.data == "admin_create")
async def admin_create(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return
    await query.message.answer("📝 So‘rovnoma nomini yuboring:")

    @dp.message()
    async def get_title(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            return
        async with pool.acquire() as conn:
            survey = await conn.fetchrow("INSERT INTO surveys (title) VALUES ($1) RETURNING id", message.text)
        await message.answer(f"✅ So‘rovnoma yaratildi (ID: {survey['id']})")

# 📋 So‘rovnomalarni ko‘rish
@dp.callback_query(F.data == "admin_list")
async def admin_list(query: types.CallbackQuery):
    surveys = await get_surveys()
    if not surveys:
        await query.message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(text=s['title'], callback_data=f"admin_open_{s['id']}")] for s in surveys]
    await query.message.answer("📋 So‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# 📊 Natijalarni ko‘rish
@dp.callback_query(F.data == "admin_results")
async def admin_results(query: types.CallbackQuery):
    surveys = await get_surveys()
    if not surveys:
        await query.message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return
    for s in surveys:
        _, candidates, _ = await get_survey(s['id'])
        text = f"🗳 {s['title']}\n"
        for c in candidates:
            text += f"- {c['name']} ⭐ {c['votes']}\n"
        await query.message.answer(text)

# ====================== RUN ======================
async def main():
    await setup_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
