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
            [KeyboardButton(text="📋 So‘rovnomalarni ko‘rish"), KeyboardButton(text="📊 Natijalarni ko‘rish")],
            [KeyboardButton(text="➕ Nomzod qo‘shish"), KeyboardButton(text="📢 Kanal qo‘shish")],
            [KeyboardButton(text="📤 CSV eksport")]
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

# ====================== START (admin + user) ======================
@dp.message(F.text == "/start")
async def start_handler(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👨‍💼 Admin panel:", reply_markup=admin_keyboard())
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return
    buttons = [[InlineKeyboardButton(text=s['title'], callback_data=f"open_{s['id']}")] for s in surveys]
    await message.answer("Aktiv so‘rovnomalar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ====================== ADMIN PANEL HANDLERS ======================
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
    # Admin inline list: har birini tanlash mumkin
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s['id']}: {s['title']}", callback_data=f"admin_open_{s['id']}")]
        for s in surveys
    ])
    await message.answer("Admin: so‘rovnomani tanlang:", reply_markup=kb)

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
    await message.answer("✍ Kanal/guruh nomini yuboring (@kanal, https://t.me/kanal yoki -100id):", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_channel)

@dp.message(F.text == "📤 CSV eksport")
async def admin_export_csv(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Aktiv so‘rovnoma yo‘q.")
        return
    s = surveys[0]
    _, candidates, _ = await get_survey(s['id'])
    import csv
    from io import StringIO
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["Candidate", "Votes"])
    for c in candidates:
        w.writerow([c['name'], c['votes']])
    buf.seek(0)
    await message.answer_document(types.InputFile.from_buffer(buf.getvalue().encode(), filename=f"survey_{s['id']}_results.csv"))

# ====================== FSM HANDLERS ======================
@dp.message(CreateSurvey.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    title = message.text.strip()
    if not title:
        await message.answer("Nom bo‘sh bo‘lmasin. Qayta yuboring.")
        return
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("INSERT INTO surveys (title) VALUES ($1) RETURNING id", title)
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
        await message.answer("📢 Kanal/guruh nomini yuboring (@kanal, https://t.me/kanal yoki -100id):", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_channel)
    else:
        name = message.text.strip()
        if not name:
            await message.answer("Nomzod nomi bo‘sh bo‘lmasin.")
            return
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO candidates (survey_id, name) VALUES ($1, $2)", survey_id, name)
        await message.answer(f"✅ Nomzod qo‘shildi: {name}")

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
        ch = message.text.strip()
        if not ch:
            await message.answer("Kanal/guruh nomi bo‘sh bo‘lmasin.")
            return
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO required_channels (survey_id, channel) VALUES ($1, $2)", survey_id, ch)
        await message.answer(f"✅ Qo‘shildi: {ch}")

# ====================== USER VOTING (premium UI for all) ======================
@dp.callback_query(F.data.startswith("open_"))
async def open_survey_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("open_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    caption = f"🌟 PREMIUM KO'RINISH: {survey['title']}\n\nSizga boyroq ko‘rinish taqdim etildi."
    if channels:
        caption += "\n\nTalab kanallar/guruhlar:\n" + "\n".join([f"- {row['channel']}" for row in channels])
    kb = candidates_keyboard_premium(candidates)
    if survey and survey['image']:
        await query.message.answer_photo(survey['image'], caption=caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)

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

    _, _, channels = await get_survey(survey_id)
    not_joined = []
    for row in channels:
        ch_raw = row['channel']
        ok = await is_member(bot, query.from_user.id, ch_raw)
        if not ok:
            not_joined.append(ch_raw)

    if not_joined:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[join_button_for(ch)] for ch in not_joined] +
                            [[InlineKeyboardButton(text="🔄 Tekshirish", callback_data=f"recheck_{survey_id}")]]
        )
        await query.message.answer(
            "Ovoz berish uchun quyidagi kanal yoki guruhlarga obuna bo‘lish majburiy. Iltimos, obuna bo‘ling va keyin Tekshirish tugmasini bosing.",
            reply_markup=kb
        )
        await query.answer("Avval talab qilingan kanallarga obuna bo‘ling.", show_alert=True)
        return

    await add_vote(survey_id, candidate_id, query.from_user.id)
    _, candidates, _ = await get_survey(survey_id)
    kb = candidates_keyboard_premium(candidates)

    try:
        await query.message.edit_reply_markup(kb)
    except Exception:
        await query.message.answer("Yangi natijalar:", reply_markup=kb)

    await query.answer("✔ Ovoz berildi!")

@dp.callback_query(F.data.startswith("recheck_"))
async def recheck_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("recheck_", ""))
    survey, candidates, channels = await get_survey(survey_id)

    not_joined = []
    for row in channels:
        ch_raw = row['channel']
        ok = await is_member(bot, query.from_user.id, ch_raw)
        if not ok:
            not_joined.append(ch_raw)

    if not_joined:
        text = "Siz hali ham quyidagi kanal/guruhlarga obuna bo‘lmagansiz:\n" + "\n".join([f"- {c}" for c in not_joined])
        await query.answer(text, show_alert=True)
        return

    kb = candidates_keyboard_premium(candidates)
    caption = survey['title'] if survey else "So‘rovnoma topilmadi."
    await query.message.answer("A’zolik tasdiqlandi. Endi ovoz bera olasiz.", reply_markup=None)
    if survey and survey.get('image'):
        await query.message.answer_photo(survey['image'], caption=caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)
    await query.answer("A’zolik tasdiqlandi. Ovoz berishingiz mumkin.", show_alert=True)

# ====================== ADMIN: open selected survey, stop, delete ======================
@dp.callback_query(F.data.startswith("admin_open_"))
async def admin_open_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("admin_open_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    if not survey:
        return await query.answer("So‘rovnoma topilmadi.", show_alert=True)

    text = f"🗳 So‘rovnoma: {survey['title']}\nID: {survey_id}\n\nNomzodlar:\n"
    for c in candidates:
        text += f"- {c['name']} : {c['votes']} ovoz\n"
    if channels:
        text += "\nTalab kanallar:\n" + "\n".join([f"- {r['channel']}" for r in channels])

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Stop so‘rovnoma", callback_data=f"stop_{survey_id}")]
    ])
    await query.message.answer(text, reply_markup=kb)
    await query.answer()

@dp.callback_query(F.data.startswith("stop_"))
async def admin_stop_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("stop_", ""))
    # deactivate survey
    async with pool.acquire() as conn:
        await conn.execute("UPDATE surveys SET active=false WHERE id=$1", survey_id)

    # gather results and voters
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT title FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT name, votes FROM candidates WHERE survey_id=$1 ORDER BY id", survey_id)
        voters = await conn.fetch("SELECT user_id FROM voted_users WHERE survey_id=$1", survey_id)

    results_text = f"🔔 So‘rovnoma yopildi: {survey['title']}\n\nNatijalar:\n"
    for c in candidates:
        results_text += f"- {c['name']}: {c['votes']} ovoz\n"

    sent = 0
    failed = 0
    for row in voters:
        user_id = row['user_id']
        try:
            await bot.send_message(user_id, results_text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            logging.exception(f"Xabar yuborishda xato: user_id={user_id}")

    delete_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Delete so‘rovnoma (butunlay o‘chirish)", callback_data=f"delete_{survey_id}")]
    ])
    await query.message.answer(
        f"So‘rovnoma '{survey['title']}' yopildi.\nXabar yuborildi: {sent}; muvaffaqiyatsiz: {failed}.",
        reply_markup=delete_kb
    )
    await query.answer("So‘rovnoma yopildi va qatnashganlarga xabar yuborildi.")

@dp.callback_query(F.data.startswith("delete_"))
async def admin_delete_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("delete_", ""))
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT title FROM surveys WHERE id=$1", survey_id)
        if not survey:
            return await query.answer("So‘rovnoma topilmadi yoki allaqachon o‘chirib yuborilgan.", show_alert=True)
        title = survey['title']
        await conn.execute("DELETE FROM surveys WHERE id=$1", survey_id)

    await query.message.answer(f"✅ So‘rovnoma '{title}' (ID: {survey_id}) butunlay o‘chirildi.")
    await query.answer("So‘rovnoma o‘chirildi.")

# ====================== RUN ======================
async def main():
    await setup_db()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
