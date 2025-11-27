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

# Load environment
load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN:
    logging.error("TOKEN muhit o'zgaruvchisi topilmadi.")
    raise SystemExit("TOKEN muhit o'zgaruvchisi topilmadi.")
if not DATABASE_URL:
    logging.error("DATABASE_URL topilmadi.")
    raise SystemExit("DATABASE_URL topilmadi.")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================== DATABASE ======================
pool: asyncpg.pool.Pool = None

async def setup_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        # Ensure columns exist; safe even if they already exist
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id SERIAL PRIMARY KEY,
            short_title TEXT,
            title TEXT,
            description TEXT,
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
    waiting_for_short_title = State()
    waiting_for_description = State()
    waiting_for_image = State()
    waiting_for_candidate = State()
    waiting_for_channel = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()

# ====================== HELPERS & KEYBOARDS ======================
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
        name = c["name"]
        votes = c["votes"]

        # Premium ko‘rinish: ⭐ Nomi — ⭐ ovoz
        label = f"⭐ {name} — ⭐ {votes} ovoz"

        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"vote_{c['id']}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def short_title(title: str, limit: int = 38) -> str:
    title = (title or "").strip()
    if len(title) <= limit:
        return title
    return title[:limit-1].rstrip() + "…"

def normalize_channel(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("https://t.me/"):
        path = v.replace("https://t.me/", "").strip()
        if "/" in path:
            return v
        return f"@{path}"
    if v.startswith("@"):
        return v
    return v

def join_button_for(channel: str) -> InlineKeyboardButton:
    ch = (channel or "").strip()
    if ch.startswith("@"):
        return InlineKeyboardButton(text=f"➕ {ch} ga obuna bo‘lish", url=f"https://t.me/{ch[1:]}")
    if ch.startswith("https://t.me/"):
        return InlineKeyboardButton(text="➕ Obuna bo‘lish", url=ch)
    return InlineKeyboardButton(text="➕ Kanal/guruhga obuna bo‘lish", url="https://t.me")

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
        row = await conn.fetchrow("SELECT 1 FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id)
        return row is not None

async def add_vote(survey_id: int, candidate_id: int, user_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id)
            await conn.execute("INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)", survey_id, user_id)

async def is_member(bot: Bot, user_id: int, channel_raw: str) -> bool:
    ch = (channel_raw or "").strip()
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

# ====================== USER REGISTRATION & START ======================
@dp.message(F.text == "/start")
async def start_handler(message: types.Message):
    # Save or update user
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = (message.from_user.full_name or "").strip()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, username, full_name, joined_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (id) DO UPDATE SET username=$2, full_name=$3
        """, user_id, username, full_name)

    # Admin view
    if message.from_user.id == ADMIN_ID:
        await message.answer("👨‍💼 Admin panel:", reply_markup=admin_keyboard())
        return

    # User view: show short titles as inline buttons
    surveys = await get_surveys()
    if not surveys:
        await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")
        return

    buttons = []
    for s in surveys:
        # asyncpg.Record supports mapping access; use get via dict conversion for safety
        try:
            s_map = dict(s)
        except Exception:
            s_map = s
        default_title = s_map.get('short_title') or s_map.get('title') or "So'rovnoma"
        label = short_title(default_title, limit=38)
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"open_{s_map.get('id')}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Aktiv so‘rovnomalar. Tugmani bosing va batafsil ko‘ring:", reply_markup=kb)

# ====================== ADMIN: CREATE SURVEY ======================
@dp.message(F.text == "➕ So‘rovnoma yaratish")
async def admin_create_survey_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔹 Qisqa nomni yuboring (bu tugma yorlig‘i bo‘ladi, 35–40 belgidan oshmasin):", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_short_title)

@dp.message(CreateSurvey.waiting_for_short_title)
async def process_short_title(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    short = (message.text or "").strip()
    if not short:
        await message.answer("Qisqa nom bo‘sh bo‘lmasin. Qayta yuboring.")
        return
    await state.update_data(short_title=short)
    await message.answer("✍ Endi so‘rovnoma haqida batafsil matn yuboring (bu matn surat tagida ko‘rinadi):", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_description)

@dp.message(CreateSurvey.waiting_for_description)
async def process_description(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    desc = (message.text or "").strip()
    if not desc:
        await message.answer("Batafsil matn bo‘sh bo‘lmasin. Qayta yuboring.")
        return
    data = await state.get_data()
    short = data.get('short_title')
    async with pool.acquire() as conn:
        survey = await conn.fetchrow(
            "INSERT INTO surveys (short_title, description) VALUES ($1, $2) RETURNING id",
            short, desc
        )
    await state.update_data(survey_id=survey['id'])
    await message.answer("📷 Endi rasm yuboring (ixtiyoriy) yoki '✅ Tugatish' tugmasini bosing", reply_markup=finish_keyboard())
    await state.set_state(CreateSurvey.waiting_for_image)

@dp.message(CreateSurvey.waiting_for_image)
async def process_image(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    survey_id = data.get('survey_id')
    if message.photo:
        photo_id = message.photo[-1].file_id
        async with pool.acquire() as conn:
            await conn.execute("UPDATE surveys SET image=$1 WHERE id=$2", photo_id, survey_id)
        await message.answer("✅ Rasm qo‘shildi. Endi nomzodlarni yuboring (har bir nomzodni alohida xabarda yuboring). Tugatish uchun '✅ Tugatish' tugmasini bosing.", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_candidate)
    elif message.text == "✅ Tugatish":
        await message.answer("Endi nomzodlarni yuboring (har bir nomzodni alohida xabarda yuboring). Tugatish uchun '✅ Tugatish' tugmasini bosing.", reply_markup=finish_keyboard())
        await state.set_state(CreateSurvey.waiting_for_candidate)

@dp.message(CreateSurvey.waiting_for_candidate)
async def process_candidate(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    survey_id = data.get('survey_id')

    # Agar admin Tugatishni bossа, kanal bosqichiga o‘tamiz
    if (message.text or "").strip() == "✅ Tugatish":
        await message.answer(
            "Kanal yoki guruhlarni yuboring. Har bir kanalni alohida xabarda yuboring. Tugatish uchun '✅ Tugatish' tugmasini bosing.",
            reply_markup=finish_keyboard()
        )
        await state.set_state(CreateSurvey.waiting_for_channel)
        return

    name = (message.text or "").strip()
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
    survey_id = data.get('survey_id')

    # Tugatish bu yerda yakunlashni amalga oshiradi
    if (message.text or "").strip() == "✅ Tugatish":
        await message.answer("✅ So‘rovnoma tayyor!", reply_markup=admin_keyboard())
        await state.clear()
        return

    ch = (message.text or "").strip()
    if not ch:
        await message.answer("Kanal/guruh nomi bo‘sh bo‘lishi mumkin emas.")
        return
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO required_channels (survey_id, channel) VALUES ($1, $2)", survey_id, ch)
    await message.answer(f"✅ Qo‘shildi: {ch}")

# ====================== ADMIN: LIST SURVEYS (inline) ======================
@dp.message(F.text == "📋 So‘rovnomalarni ko‘rish")
async def admin_list_surveys(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    surveys = await get_surveys()
    if not surveys:
        await message.answer("❌ Aktiv so‘rovnoma yo‘q.")
        return

    buttons = []
    for s in surveys:
        try:
            s_map = dict(s)
        except Exception:
            s_map = s
        default_title = s_map.get('short_title') or s_map.get('title') or "So'rovnoma"
        label = short_title(default_title, limit=38)
        buttons.append([InlineKeyboardButton(text=f"{s_map.get('id')}: {label}", callback_data=f"admin_open_{s_map.get('id')}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Admin: so‘rovnomani tanlang:", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_open_"))
async def admin_open_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("admin_open_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    if not survey:
        return await query.answer("So‘rovnoma topilmadi.", show_alert=True)

    # safe access
    try:
        s_map = dict(survey)
    except Exception:
        s_map = survey
    title_display = s_map.get('short_title') or s_map.get('title') or "So'rovnoma"

    text = f"🗳 So‘rovnoma: {title_display}\nID: {survey_id}\n\n"
    text += "Nomzodlar (tugmalar orqali ko‘rsatiladi):\n"
    for c in candidates:
        text += f"- {c['name']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Stop so‘rovnoma", callback_data=f"stop_{survey_id}")]
    ])
    await query.message.answer(text, reply_markup=kb)
    await query.answer()

# ====================== ADMIN: STOP & DELETE ======================
@dp.callback_query(F.data.startswith("stop_"))
async def admin_stop_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("stop_", ""))
    async with pool.acquire() as conn:
        await conn.execute("UPDATE surveys SET active=false WHERE id=$1", survey_id)
        survey = await conn.fetchrow("SELECT short_title, description FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT name, votes FROM candidates WHERE survey_id=$1 ORDER BY id", survey_id)
        voters = await conn.fetch("SELECT user_id FROM voted_users WHERE survey_id=$1", survey_id)

    # safe title
    try:
        s_map = dict(survey) if survey is not None else {}
    except Exception:
        s_map = survey or {}
    title_for_msg = s_map.get('short_title') or s_map.get('description') or "So'rovnoma"

    results_text = f"🔔 So‘rovnoma yopildi: {title_for_msg}\n\nNatijalar:\n"
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

    # Use title_for_msg to avoid f-string escape issues
    await query.message.answer(
        f"So‘rovnoma '{title_for_msg}' yopildi.\nXabar yuborildi: {sent}; muvaffaqiyatsiz: {failed}.",
        reply_markup=delete_kb
    )
    await query.answer("So‘rovnoma yopildi va qatnashganlarga xabar yuborildi.")

@dp.callback_query(F.data.startswith("delete_"))
async def admin_delete_survey_callback(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Ruxsat yo‘q.", show_alert=True)
    survey_id = int(query.data.replace("delete_", ""))
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT short_title FROM surveys WHERE id=$1", survey_id)
        if not survey:
            return await query.answer("So‘rovnoma topilmadi yoki allaqachon o‘chirib yuborilgan.", show_alert=True)
        try:
            s_map = dict(survey)
        except Exception:
            s_map = survey
        title = s_map.get('short_title') or "So'rovnoma"
        await conn.execute("DELETE FROM surveys WHERE id=$1", survey_id)

    await query.message.answer(f"✅ So‘rovnoma '{title}' (ID: {survey_id}) butunlay o‘chirildi.")
    await query.answer("So‘rovnoma o‘chirildi.")

# ====================== SUBSCRIBERS & BROADCAST ======================
@dp.message(F.text == "📋 Obunachilar")
async def admin_subscribers(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, username, full_name, joined_at FROM users ORDER BY joined_at DESC")
    if not rows:
        await message.answer("Hozircha botga a'zo foydalanuvchi yo'q.")
        return
    total = len(rows)
    text = f"👥 Botga a'zo foydalanuvchilar: {total}\n\n"
    for r in rows[:100]:
        uname = f"@{r['username']}" if r['username'] else ""
        name = r['full_name'] or ""
        text += f"- {r['id']} {uname} {name}\n"
    if total > 100:
        text += f"\n... va yana {total-100} ta foydalanuvchi."
    await message.answer(text)

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
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM users")
    if not rows:
        await message.answer("Botga a'zo foydalanuvchi topilmadi.")
        await state.clear()
        return
    user_ids = [r['id'] for r in rows]
    sent = 0
    failed = 0
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
        await message.answer("Qo'llab-quvvatlanmaydigan turdagi xabar.")
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
            await asyncio.sleep(0.05)
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

# ====================== USER VOTING HANDLERS ======================
@dp.callback_query(F.data.startswith("open_"))
async def open_survey_callback(query: types.CallbackQuery):
    survey_id = int(query.data.replace("open_", ""))
    survey, candidates, channels = await get_survey(survey_id)
    if not survey:
        await query.answer("So‘rovnoma topilmadi.", show_alert=True)
        return

    # Show image (if any) and description under it; do NOT include candidate names in text
    try:
        s_map = dict(survey)
    except Exception:
        s_map = survey
    caption = s_map.get('description') or s_map.get('title') or s_map.get('short_title') or "So'rovnoma"
    kb = candidates_keyboard(candidates)
    if s_map.get('image'):
        try:
            await query.message.answer_photo(s_map.get('image'), caption=caption, reply_markup=kb)
        except Exception:
            await query.message.answer(caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)
    await query.answer()

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
    kb = candidates_keyboard(candidates)
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
    kb = candidates_keyboard(candidates)
    try:
        s_map = dict(survey)
    except Exception:
        s_map = survey
    caption = s_map.get('description') or s_map.get('short_title') or "So'rovnoma"
    if s_map and s_map.get('image'):
        try:
            await query.message.answer_photo(s_map.get('image'), caption=caption, reply_markup=kb)
        except Exception:
            await query.message.answer(caption, reply_markup=kb)
    else:
        await query.message.answer(caption, reply_markup=kb)
    await query.answer("A’zolik tasdiqlandi. Ovoz berishingiz mumkin.", show_alert=True)

# ====================== RUN ======================
async def main():
    await setup_db()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
