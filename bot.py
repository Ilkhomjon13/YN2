import logging
import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher, types
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

async def user_has_voted(survey_id: int, user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id)
        return row is not None

async def add_vote(survey_id: int, candidate_id: int, user_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id)
            await conn.execute("INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)", survey_id, user_id)

async def check_channels(user_id: int, channels: list):
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception:
            return False
    return True

# ====================== KEYBOARDS ======================
def candidates_keyboard(candidates):
    buttons = [
        [InlineKeyboardButton(f"{cand['name']} ⭐ {cand['votes']}", callback_data=f"vote_{cand['id']}")]
        for cand in candidates
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("➕ So‘rovnoma yaratish", callback_data="admin_create")],
        [InlineKeyboardButton("📋 So‘rovnomalarni ko‘rish", callback_data="admin_list")],
        [InlineKeyboardButton("📊 Natijalarni ko‘rish", callback_data="admin_results")]
    ])

# ====================== USER HANDLERS ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(s['title'], callback_data=f"open_{s['id']}")] for s in surveys]
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(lambda c: c.data.startswith("open_"))
async def open_survey_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("open_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    user_id = query.from_user.id

    if not await check_channels(user_id, channels):
        buttons = [[InlineKeyboardButton(f"📢 {ch} ga qo‘shilish", url=f"https://t.me/{ch[1:]}")] for ch in channels]
        buttons.append([InlineKeyboardButton("✔ Tekshirish", callback_data=f"check_{survey_id}")])
        await query.message.answer("❗ Ushbu so‘rovnomada qatnashish uchun kanallarga a’zo bo‘ling:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    kb = candidates_keyboard(candidates)
    if survey['image']:
        await query.message.answer_photo(survey['image'], caption=survey['title'], reply_markup=kb)
    else:
        await query.message.answer(survey['title'], reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("check_"))
async def check_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("check_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    user_id = query.from_user.id

    if not await check_channels(user_id, channels):
        await query.answer("❗ Hali barcha kanallarga a’zo bo‘lmagansiz!", show_alert=True)
        return

    kb = candidates_keyboard(candidates)
    await query.message.answer("🎉 Endi ovoz berishingiz mumkin!", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("vote_"))
async def vote_callback(query: types.CallbackQuery):
    candidate_id = int(query.data.replace("vote_", ""))
    async with pool.acquire() as conn:
        cand = await conn.fetchrow("SELECT * FROM candidates WHERE id=$1", candidate_id)
        survey_id = cand['survey_id']

    if await user_has_voted(survey_id, query.from_user.id):
        await query.answer("❗ Siz allaqachon ovoz berdingiz!", show_alert=True)
        return

    await add_vote(survey_id, candidate_id, query.from_user.id)
    _, candidates, _ = await get_survey(survey_id)
    kb = candidates_keyboard(candidates)

    await query.message.edit_reply_markup(kb)
    await query.answer("✔ Ovoz berildi!")

# ====================== ADMIN HANDLERS ======================
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Siz admin emassiz!")
        return
    await message.answer("👨‍💼 Admin panel:", reply_markup=admin_main_keyboard())

@dp.callback_query(lambda c: c.data == "admin_create")
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

@dp.callback_query(lambda c: c.data == "admin_list")
async def admin_list(query: types.CallbackQuery):
    surveys = await get_surveys()
    if not surveys:
        await query.message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(f"{s['title']}", callback_data=f"admin_open_{s['id']}")] for s in surveys]
    await query.message.answer("📋 So‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(lambda c: c.data == "admin_results")
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
