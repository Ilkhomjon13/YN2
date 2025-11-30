import asyncio
import logging
import os
from datetime import datetime

import asyncpg
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


# =========================================================
# ENV
# =========================================================
load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MONITOR_KEY = os.getenv("MONITOR_KEY", "SECURE123")
MONITOR_BASE_URL = os.getenv("MONITOR_BASE_URL", "https://yourdomain.com")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# =========================================================
# DB
# =========================================================
pool: asyncpg.pool.Pool = None


async def setup_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
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
        CREATE TABLE IF NOT EXISTS start_screen (
            id SERIAL PRIMARY KEY,
            photo TEXT,
            caption TEXT
        );
        INSERT INTO start_screen (id, photo, caption)
        VALUES (1, '', 'Aktiv so‘rovnomalar. Tugma orqali kirishingiz mumkin:')
        ON CONFLICT (id) DO NOTHING;
        """)


# =========================================================
# STATES
# =========================================================
class CreateSurvey(StatesGroup):
    short = State()
    title = State()
    description = State()
    image = State()
    candidate = State()
    channel = State()


class Broadcast(StatesGroup):
    waiting = State()


class StartScreen(StatesGroup):
    photo = State()
    caption = State()


# =========================================================
# UTIL FUNCTIONS
# =========================================================
def normalize_channel(ch: str) -> str:
    ch = (ch or "").strip()

    if ch.startswith("https://t.me/"):
        path = ch.replace("https://t.me/", "").strip("/")
        if "/" in path:
            return ch  # Can't normalize
        return f"@{path}"

    if ch.startswith("t.me/"):
        path = ch.replace("t.me/", "").strip("/")
        return f"@{path}"

    if ch.startswith("@"):
        return ch

    if ch.startswith("-100"):
        return ch

    return "@" + ch


def admin_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton("➕ So‘rovnoma yaratish"), KeyboardButton("🖼 Foydalanuvchi oynasi")],
        [KeyboardButton("📋 So‘rovnomalarni ko‘rish"), KeyboardButton("📋 Obunachilar")],
        [KeyboardButton("✉️ Xabar yuborish"), KeyboardButton("📢 Kanal qo‘shish")],
        [KeyboardButton("📡 Live monitoring")],
    ])


def finish_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton("✅ Tugatish")]
    ])


def candidates_kb(candidates):
    buttons = [
        [
            InlineKeyboardButton(
                text=f"⭐ {c['name']} — {c['votes']} ovoz",
                callback_data=f"vote_{c['id']}"
            )
        ] for c in candidates
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def is_member(user_id: int, ch: str) -> bool:
    ch = normalize_channel(ch)

    try:
        m = await bot.get_chat_member(ch, user_id)
        return m.status in ("member", "administrator", "creator")
    except:
        return False


# =========================================================
# DB Helpers
# =========================================================
async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true ORDER BY id DESC")


async def get_survey(survey_id: int):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1", survey_id)
        return survey, candidates, channels


async def user_voted(survey_id: int, user_id: int):
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT 1 FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id
        )
        return r is not None


async def add_vote(user_id: int, survey_id: int, candidate_id: int):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id
            )
            await conn.execute(
                "INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)",
                survey_id, user_id
            )


# =========================================================
# ROUTERS
# =========================================================
router = Router()


# =========================================================
# USER: /start
# =========================================================
@router.message(F.text == "/start")
async def start(message: Message):
    uid = message.from_user.id

    # Save user
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (id, username, full_name)
            VALUES ($1,$2,$3)
            ON CONFLICT (id) DO UPDATE SET username=$2, full_name=$3
        """, uid, message.from_user.username, message.from_user.full_name)

    # Admin panel
    if uid == ADMIN_ID:
        return await message.answer("👨‍💼 Admin panel:", reply_markup=admin_kb())

    # User view
    surveys = await get_surveys()
    if not surveys:
        return await message.answer("Hozircha aktiv so‘rovnoma yo‘q.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=s['short_title'] or "So‘rovnoma",
            callback_data=f"open_{s['id']}"
        )]
        for s in surveys
    ])

    # load start screen
    async with pool.acquire() as conn:
        scr = await conn.fetchrow("SELECT photo, caption FROM start_screen WHERE id=1")

    caption = scr["caption"]
    photo = scr["photo"]

    if photo:
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


# =========================================================
# USER: OPEN SURVEY
# =========================================================
@router.callback_query(F.data.startswith("open_"))
async def open_survey(q: CallbackQuery):
    survey_id = int(q.data.replace("open_", ""))
    survey, candidates, channels = await get_survey(survey_id)

    caption = survey["description"] or survey["title"] or survey["short_title"]
    photo = survey["image"]

    kb = candidates_kb(candidates)

    if photo:
        await q.message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:
        await q.message.answer(caption, reply_markup=kb)

    await q.answer()


# =========================================================
# USER: VOTE
# =========================================================
@router.callback_query(F.data.startswith("vote_"))
async def vote(q: CallbackQuery):
    candidate_id = int(q.data.replace("vote_", ""))
    uid = q.from_user.id

    async with pool.acquire() as conn:
        cand = await conn.fetchrow("SELECT * FROM candidates WHERE id=$1", candidate_id)

    if not cand:
        return await q.answer("Nomzod topilmadi.", show_alert=True)

    survey_id = cand["survey_id"]

    if await user_voted(survey_id, uid):
        return await q.answer("Siz allaqachon ovoz bergansiz!", show_alert=True)

    _, _, channels = await get_survey(survey_id)

    # Check membership
    for ch in channels:
        if not await is_member(uid, ch["channel"]):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton("➕ Obuna bo‘lish", url=f"https://t.me/{ch['channel'].replace('@','')}")],
                [InlineKeyboardButton("🔄 Tekshirish", callback_data=f"retry_{survey_id}")]
            ])
            await q.message.answer("Ovoz berish uchun avval obuna bo‘ling:", reply_markup=kb)
            return await q.answer("Obuna shart!", show_alert=True)

    # Process vote
    await add_vote(uid, survey_id, candidate_id)

    # Update UI
    _, candidates, _ = await get_survey(survey_id)
    kb = candidates_kb(candidates)

    try:
        await q.message.edit_reply_markup(kb)
    except:
        await q.message.answer("Natijalar yangilandi:", reply_markup=kb)

    await q.answer("✔ Ovoz qabul qilindi!")


# =========================================================
# USER: RETRY MEMBERSHIP
# =========================================================
@router.callback_query(F.data.startswith("retry_"))
async def retry(q: CallbackQuery):
    survey_id = int(q.data.replace("retry_", ""))

    _, candidates, channels = await get_survey(survey_id)

    for ch in channels:
        if not await is_member(q.from_user.id, ch["channel"]):
            return await q.answer("Hali ham obuna bo‘lmagansiz!", show_alert=True)

    caption = "Obuna tasdiqlandi. Endi ovoz berishingiz mumkin."
    kb = candidates_kb(candidates)

    await q.message.answer(caption, reply_markup=kb)
    await q.answer("Tasdiqlandi")


# =========================================================
# ADMIN: CREATE SURVEY
# =========================================================
@router.message(F.text == "➕ So‘rovnoma yaratish")
async def cs1(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Qisqa nomni yuboring:", reply_markup=finish_kb())
    await state.set_state(CreateSurvey.short)


@router.message(CreateSurvey.short)
async def cs2(message: Message, state: FSMContext):
    await state.update_data(short=message.text.strip())
    await message.answer("To‘liq nomni yuboring:")
    await state.set_state(CreateSurvey.title)


@router.message(CreateSurvey.title)
async def cs3(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Tavsif (description) yuboring:")
    await state.set_state(CreateSurvey.description)


@router.message(CreateSurvey.description)
async def cs4(message: Message, state: FSMContext):
    data = await state.get_data()
    short, title = data["short"], data["title"]

    async with pool.acquire() as conn:
        s = await conn.fetchrow("""
            INSERT INTO surveys (short_title, title, description)
            VALUES ($1,$2,$3) RETURNING id
        """, short, title, message.text.strip())

    await state.update_data(survey_id=s["id"])
    await message.answer("Rasm yuboring yoki 'Tugatish' →", reply_markup=finish_kb())
    await state.set_state(CreateSurvey.image)


@router.message(CreateSurvey.image)
async def cs5(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["survey_id"]

    if message.photo:
        photo = message.photo[-1].file_id
        async with pool.acquire() as conn:
            await conn.execute("UPDATE surveys SET image=$1 WHERE id=$2", photo, sid)

    await message.answer("Nomzodlarni yuboring. Har biri alohida xabar →", reply_markup=finish_kb())
    await state.set_state(CreateSurvey.candidate)


@router.message(CreateSurvey.candidate)
async def cs6(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    sid = data["survey_id"]

    if text == "✅ Tugatish":
        await message.answer("Kanal/guruhlarni yuboring →", reply_markup=finish_kb())
        return await state.set_state(CreateSurvey.channel)

    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO candidates (survey_id, name) VALUES ($1,$2)", sid, text)

    await message.answer(f"Nomzod qo‘shildi: {text}")


@router.message(CreateSurvey.channel)
async def cs7(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    sid = data["survey_id"]

    if text == "✅ Tugatish":
        await state.clear()
        return await message.answer("So‘rovnoma tayyor!", reply_markup=admin_kb())

    ch = normalize_channel(text)

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO required_channels (survey_id, channel) VALUES ($1,$2)",
            sid, ch
        )

    await message.answer(f"Kanal qo‘shildi: {ch}")


# =========================================================
# ADMIN: SURVEY LIST
# =========================================================
@router.message(F.text == "📋 So‘rovnomalarni ko‘rish")
async def list_s(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    s = await get_surveys()
    if not s:
        return await message.answer("Aktiv so‘rovnoma yo‘q.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(f"{r['id']}: {r['short_title']}", callback_data=f"adminopen_{r['id']}")]
        for r in s
    ])
    await message.answer("So‘rovnomani tanlang:", reply_markup=kb)


@router.callback_query(F.data.startswith("adminopen_"))
async def admin_open(q: CallbackQuery):
    sid = int(q.data.replace("adminopen_", ""))
    s, cand, ch = await get_survey(sid)

    text = f"🗳 So‘rovnoma: {s['short_title']}\n\nNomzodlar:\n"
    for c in cand:
        text += f"- {c['name']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("⛔ Stop", callback_data=f"stop_{sid}")]
    ])

    await q.message.answer(text, reply_markup=kb)
    await q.answer()


# =========================================================
# ADMIN: STOP SURVEY
# =========================================================
@router.callback_query(F.data.startswith("stop_"))
async def stop_s(q: CallbackQuery):
    sid = int(q.data.replace("stop_", ""))

    async with pool.acquire() as conn:
        await conn.execute("UPDATE surveys SET active=false WHERE id=$1", sid)
        s = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", sid)
        cand = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1", sid)
        voters = await conn.fetch("SELECT user_id FROM voted_users WHERE survey_id=$1", sid)

    result = f"🔴 So‘rovnoma yopildi: {s['short_title']}\n\nNatijalar:\n"
    for c in cand:
        result += f"- {c['name']}: {c['votes']} ovoz\n"

    sent = 0
    for v in voters:
        try:
            await bot.send_message(v["user_id"], result)
            await asyncio.sleep(0.05)
            sent += 1
        except:
            pass

    await q.message.answer(f"Yopildi. Jo‘natildi: {sent}")
    await q.answer("Yopildi")


# =========================================================
# ADMIN: SUBSCRIBERS
# =========================================================
@router.message(F.text == "📋 Obunachilar")
async def subs(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        r = await conn.fetch("SELECT * FROM users ORDER BY joined_at DESC")

    await message.answer(f"Foydalanuvchilar soni: {len(r)}")


# =========================================================
# ADMIN: BROADCAST
# =========================================================
@router.message(F.text == "✉️ Xabar yuborish")
async def bc1(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Xabarni yuboring:")
    await state.set_state(Broadcast.waiting)


@router.message(Broadcast.waiting)
async def bc2(message: Message, state: FSMContext):
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id FROM users")

    await message.answer(f"{len(users)} ta foydalanuvchiga jo‘natilmoqda...")

    for u in users:
        try:
            if message.photo:
                await bot.send_photo(u["id"], message.photo[-1].file_id, caption=message.caption or "")
            else:
                await bot.send_message(u["id"], message.text)
            await asyncio.sleep(0.05)
        except:
            continue

    await state.clear()
    await message.answer("Jo‘natildi!")


# =========================================================
# ADMIN: LIVE MONITORING
# =========================================================
@router.message(F.text == "📡 Live monitoring")
async def monitor_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    surveys = await get_surveys()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(s['short_title'], callback_data=f"mon_{s['id']}")]
        for s in surveys
    ])
    await message.answer("Monitoring uchun tanlang:", reply_markup=kb)


@router.callback_query(F.data.startswith("mon_"))
async def monitor_link(q: CallbackQuery):
    sid = int(q.data.replace("mon_", ""))

    link = f"{MONITOR_BASE_URL}/monitor?survey_id={sid}&key={MONITOR_KEY}"

    await q.message.answer(f"🔗 Monitoring:\n{link}")
    await q.answer()


# =========================================================
# ADMIN: START SCREEN
# =========================================================
@router.message(F.text == "🖼 Foydalanuvchi oynasi")
async def ss1(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Rasm yuboring:")
    await state.set_state(StartScreen.photo)


@router.message(StartScreen.photo)
async def ss2(message: Message, state: FSMContext):
    if not message.photo:
        return await message.answer("Rasm yuboring!")

    await state.update_data(photo=message.photo[-1].file_id)
    await message.answer("Matn yuboring:")
    await state.set_state(StartScreen.caption)


@router.message(StartScreen.caption)
async def ss3(message: Message, state: FSMContext):
    data = await state.get_data()

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE start_screen SET photo=$1, caption=$2 WHERE id=1
        """, data["photo"], message.text)

    await state.clear()
    await message.answer("Saqlandi!", reply_markup=admin_kb())


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================
@router.errors()
async def err_handler(update, exception):
    logging.error(exception)
    return True


# =========================================================
# RUN
# =========================================================
async def main():
    await setup_db()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
