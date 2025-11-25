import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
import matplotlib.pyplot as plt
import pandas as pd
from db import create_pool, init_db
from config import TOKEN, ADMIN_ID

import asyncio
from datetime import datetime

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ====================== BAZA POOL ======================
pool = None

async def setup_db():
    global pool
    pool = await create_pool()
    await init_db(pool)

# ====================== HELPERS ======================

async def get_surveys():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM surveys WHERE active=true")

async def get_survey(survey_id):
    async with pool.acquire() as conn:
        survey = await conn.fetchrow("SELECT * FROM surveys WHERE id=$1", survey_id)
        candidates = await conn.fetch("SELECT * FROM candidates WHERE survey_id=$1", survey_id)
        channels = await conn.fetch("SELECT channel FROM required_channels WHERE survey_id=$1", survey_id)
        return survey, candidates, [ch['channel'] for ch in channels]

async def user_has_voted(survey_id, user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM voted_users WHERE survey_id=$1 AND user_id=$2", survey_id, user_id)
        return row is not None

async def add_vote(survey_id, candidate_id, user_id):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE candidates SET votes=votes+1 WHERE id=$1", candidate_id)
        await conn.execute("INSERT INTO voted_users (survey_id, user_id) VALUES ($1, $2)", survey_id, user_id)

async def check_channels(user_id, channels):
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

# ====================== KEYBOARD ======================

def candidates_keyboard(candidates):
    buttons = []
    for cand in candidates:
        buttons.append([InlineKeyboardButton(f"{cand['name']} ⭐ {cand['votes']}", callback_data=f"vote_{cand['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ====================== HANDLERS ======================

@dp.message(Command(commands=["start"]))
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

    ok = await check_channels(user_id, channels)
    if not ok:
        buttons = [[InlineKeyboardButton(f"📢 {ch} ga qo‘shilish", url=f"https://t.me/{ch[1:]}")] for ch in channels]
        buttons.append([InlineKeyboardButton("✔ Tekshirish", callback_data=f"check_{survey_id}")])
        await query.message.answer("❗ Ushbu so‘rovnomada qatnashish uchun kanallarga a’zo bo‘ling:", reply_markup=InlineKeyboardMarkup(buttons))
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

    ok = await check_channels(user_id, channels)
    if not ok:
        await query.answer("❗ Hali barcha kanallarga a’zo bo‘lmagansiz!", show_alert=True)
        return

    kb = candidates_keyboard(candidates)
    await query.message.answer("🎉 Endi ovoz berishingiz mumkin!", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("vote_"))
async def vote_callback(query: types.CallbackQuery):
    candidate_id = int(query.data.replace("vote_", ""))
    user_id = query.from_user.id

    # candidate -> survey
    async with pool.acquire() as conn:
        cand = await conn.fetchrow("SELECT * FROM candidates WHERE id=$1", candidate_id)
        survey_id = cand['survey_id']

    if await user_has_voted(survey_id, user_id):
        await query.answer("❗ Siz allaqachon ovoz berdingiz!", show_alert=True)
        return

    await add_vote(survey_id, candidate_id, user_id)
    # Yangilangan keyboard
    _, candidates, _ = await get_survey(survey_id)
    kb = candidates_keyboard(candidates)

    await query.message.edit_reply_markup(kb)
    await query.answer("✔ Ovoz berildi!")

# ====================== RUN ======================

async def main():
    await setup_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
