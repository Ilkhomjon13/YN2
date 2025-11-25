import logging
import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
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
        """)

# ====================== FSM ======================
class CreateSurvey(StatesGroup):
    waiting_for_title = State()
    waiting_for_image = State()
    waiting_for_candidate = State()
    waiting_for_channel = State()

# ====================== KEYBOARDS ======================
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

def candidates_keyboard(candidates):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{c['name']} ⭐ {c['votes']}", callback_data=f"vote_{c['id']}")]
        for c in candidates
    ])

# ====================== HELPERS ======================
async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true ORDER BY id DESC")

async def get_survey(survey_id: int):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1 ORDER BY id", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1 ORDER BY id", survey_id)
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

# ====================== ADMIN WELCOME (reply keyboard doimiy) ======================
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👨‍💼 Admin panel:", reply_markup=admin_keyboard())
        return
    # foydalanuvchilar uchun ro‘yxat
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(text=s['title'], callback_data=f"open_{s['id']}")] for s in surveys]
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ====================== ADMIN PANEL (functional) ======================
@dp.message(F.text == "➕ So‘rovnoma yaratish")
async def admin_create_survey(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📝 So‘rovnoma nomini yuboring:")
    await state.set_state(CreateSurvey.waiting_for_title)

@dp.message(F.text == "📋 So‘rovnomalarni ko‘rish")
async def admin_list_surveys(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return
    text = "📋 So‘rovnomalar:\n"
    for s in surveys:
        text += f"- {s['id']}: {s['title']}\n"
    await message.answer(text)

@dp.message(F.text == "📊 Natijalarni ko‘rish")
async def admin_results(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return
    for s in surveys:
        _, candidates, _ = await get_survey(s['id'])
        text = f"🗳 {s['title']}\n"
        for c in candidates:
            text += f"- {c['name']} ⭐ {c['votes']}\n"
        await message.answer(text)

@dp.message(F.text == "➕ Nomzod qo‘shish")
async def admin_add_candidate(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("✍ Nomzod nomini yuboring:", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_candidate)

@dp.message(F.text == "📢 Kanal qo‘shish")
async def admin_add_channel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("✍ Kanal nomini yuboring (@kanal):", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_channel)

# ====================== FSM HANDLERS ======================
@dp.message(CreateSurvey.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("INSERT INTO surveys (title) VALUES ($1) RETURNING id", message.text)
    await state.update_data(survey_id=survey['id'])
    await message.answer("📷 Rasm yuboring yoki '✅ Tugatish' tugmasini bosing", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_image)

@dp.message(CreateSurvey.waiting_for_image)
async def process_image(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
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
    if message.from_user.id != ADMIN_ID:
        return
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
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    survey_id = data['survey_id']
    if message.text == "✅ Tugatish":
        await message.answer("✅ So‘rovnoma tayyor!", reply_markup=admin_keyboard())
        await state.clear()
    else:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO required_channels (survey_id, channel) VALUES ($1, $2)", survey_id, message.text)
        await message.answer(f"✅ Kanal qo‘shildi: {message.text}")

# ====================== USER VOTING ======================
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(text=s['title'], callback_data=f"open_{s['id']}")] for s in surveys]
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("open_"))
async def open_survey_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("open_", ""))
    survey, candidates, _ = await get_survey(survey_id)
    kb = candidates_keyboard(candidates)
    if survey and survey['image']:
        await query.message.answer_photo(survey['image'], caption=survey['title'], reply_markup=kb)
    else:
        await query.message.answer(survey['title'] if survey else "So‘rovnoma topilmadi.", reply_markup=kb)

@dp.callback_query(F.data.startswith("vote_"))
async def vote_callback(query: types.CallbackQuery):
    candidate_id = int(query.data.replace("vote_", ""))
    async with pool.acquire() as conn:
        cand = await conn.fetchrow("SELECT id, survey_id, name FROM candidates WHERE id=$1", candidate_id)
    if not cand:
        await query.answer("Nomzod topilmadi.", show_alert=True)
        return

    survey_id = cand['survey_id']
    if await user_has_voted(survey_id, query.from_user.id):
        await query.answer("❗ Siz allaqachon ovoz berdingiz!", show_alert=True)
        return

    await add_vote(survey_id, candidate_id, query.from_user.id)
    _, candidates, _ = await get_survey(survey_id)
    kb = candidates_keyboard(candidates)
    # Tugmalarni yangilaymiz (vozlar soni o‘zgardi)
    try:
        await query.message.edit_reply_markup(kb)
    except Exception:
        # Agar xabar edit bo‘lmasa (masalan, media yoki boshqa sabab), yangisini yuboramiz
        await query.message.answer("Yangi natijalar:", reply_markup=kb)

    await query.answer("✔ Ovoz berildi!")

# ====================== RUN ======================
async def main():
    await setup_db()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
