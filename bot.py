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
            image TEXT,
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
        CREATE TABLE IF NOT EXISTS voted_users (
            survey_id INT REFERENCES surveys(id),
            user_id BIGINT,
            PRIMARY KEY(survey_id, user_id)
        );
        """)

# ====================== FSM ======================
class CreateSurvey(StatesGroup):
    waiting_for_title = State()
    waiting_for_image = State()
    waiting_for_candidate = State()
    waiting_for_channel = State()
    confirm = State()

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

def finish_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Tugatish")]],
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
        await state.set_state(CreateSurvey.waiting_for_title)

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
        await message.answer("✍ Nomzod nomini yuboring:", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_candidate)

    # 📢 Kanal qo‘shish
    elif message.text == "📢 Kanal qo‘shish":
        await message.answer("✍ Kanal nomini yuboring (@kanal):", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_channel)

# ====================== FSM HANDLERS ======================
@dp.message(CreateSurvey.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("INSERT INTO surveys (title) VALUES ($1) RETURNING id", message.text)
    await state.update_data(survey_id=survey['id'])
    await message.answer("📷 Rasm yuboring yoki '✅ Tugatish' tugmasini bosing", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_image)

@dp.message(CreateSurvey.waiting_for_image)
async def process_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    survey_id = data['survey_id']
    if message.photo:
        photo_id = message.photo[-1].file_id
        async with pool.acquire() as conn:
            await conn.execute("UPDATE surveys SET image=$1 WHERE id=$2", photo_id, survey_id)
        await message.answer("✅ Rasm qo‘shildi.")
    elif message.text == "✅ Tugatish":
        await message.answer("✍ Nomzod nomini yuboring:", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_candidate)

@dp.message(CreateSurvey.waiting_for_candidate)
async def process_candidate(message: types.Message, state: FSMContext):
    data = await state.get_data()
    survey_id = data['survey_id']
    if message.text == "✅ Tugatish":
        await message.answer("📢 Kanal nomini yuboring (@kanal):", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_channel)
    else:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO candidates (survey_id, name) VALUES ($1, $2)", survey_id, message.text)
        await message.answer(f"✅ Nomzod qo‘shildi: {message.text}")

@dp.message(CreateSurvey.waiting_for_channel)
async def process_channel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    survey_id = data['survey_id']
    if message.text == "✅ Tugatish":
        await message.answer("✅ So‘rovnoma tayyor!", reply_markup=admin_keyboard())
        await state.clear()
    else:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO required_channels (survey_id, channel) VALUES ($1, $2)", survey_id, message.text)
        await message.answer(f"✅ Kanal qo‘shildi: {message.text}")

# ====================== RUN ======================
async def main():
    await setup_db()
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
