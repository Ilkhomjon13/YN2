import logging
import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

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
            active BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id),
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS required_channels (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id),
            channel TEXT
        );
        """)

# ====================== FSM ======================
class AdminStates(StatesGroup):
    waiting_for_survey_title = State()
    waiting_for_candidate_name = State()
    waiting_for_channel_name = State()

# ====================== KEYBOARD ======================
def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ So‘rovnoma yaratish")],
            [KeyboardButton(text="📋 So‘rovnomalarni ko‘rish")],
            [KeyboardButton(text="📊 Natijalarni ko‘rish")],
            [KeyboardButton(text="➕ Nomzod qo‘shish")],
            [KeyboardButton(text="📢 Kanal qo‘shish")]
        ],
        resize_keyboard=True
    )

# ====================== HELPERS ======================
async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true")

async def get_survey(survey_id: int):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1", survey_id)
        return survey, candidates, channels

# ====================== ADMIN PANEL ======================
@dp.message(F.text)
async def admin_panel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    # ➕ So‘rovnoma yaratish
    if message.text == "➕ So‘rovnoma yaratish":
        await message.answer("📝 So‘rovnoma nomini yuboring:")
        await state.set_state(AdminStates.waiting_for_survey_title)

    # 📋 So‘rovnomalarni ko‘rish
    elif message.text == "📋 So‘rovnomalarni ko‘rish":
        surveys = await get_surveys()
        if not surveys:
            await message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        else:
            text = "📋 So‘rovnomalar:\n"
            for s in surveys:
                text += f"- {s['id']}: {s['title']}\n"
            await message.answer(text)

    # 📊 Natijalarni ko‘rish
    elif message.text == "📊 Natijalarni ko‘rish":
        surveys = await get_surveys()
        if not surveys:
            await message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        else:
            for s in surveys:
                _, candidates, _ = await get_survey(s['id'])
                text = f"🗳 {s['title']}\n"
                for c in candidates:
                    text += f"- {c['name']} ⭐ {c['votes']}\n"
                await message.answer(text)

    # ➕ Nomzod qo‘shish
    elif message.text == "➕ Nomzod qo‘shish":
        await message.answer("✍ Nomzod nomini yuboring:")
        await state.set_state(AdminStates.waiting_for_candidate_name)

    # 📢 Kanal qo‘shish
    elif message.text == "📢 Kanal qo‘shish":
        await message.answer("✍ Kanal nomini yuboring (masalan: @kanal):")
        await state.set_state(AdminStates.waiting_for_channel_name)

# ====================== FSM HANDLERS ======================
@dp.message(AdminStates.waiting_for_survey_title)
async def process_survey_title(message: types.Message, state: FSMContext):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("INSERT INTO surveys (title) VALUES ($1) RETURNING id", message.text)
    await message.answer(f"✅ So‘rovnoma yaratildi (ID: {survey['id']})", reply_markup=admin_keyboard())
    await state.clear()

@dp.message(AdminStates.waiting_for_candidate_name)
async def process_candidate_name(message: types.Message, state: FSMContext):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT id FROM surveys ORDER BY id DESC LIMIT 1")
        await conn.execute("INSERT INTO candidates (survey_id, name) VALUES ($1, $2)", survey['id'], message.text)
    await message.answer(f"✅ Nomzod qo‘shildi: {message.text}", reply_markup=admin_keyboard())
    await state.clear()

@dp.message(AdminStates.waiting_for_channel_name)
async def process_channel_name(message: types.Message, state: FSMContext):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT id FROM surveys ORDER BY id DESC LIMIT 1")
        await conn.execute("INSERT INTO required_channels (survey_id, channel) VALUES ($1, $2)", survey['id'], message.text)
    await message.answer(f"✅ Kanal qo‘shildi: {message.text}", reply_markup=admin_keyboard())
    await state.clear()

# ====================== RUN ======================
async def main():
    await setup_db()
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
