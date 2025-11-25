# db.py
import asyncio
import asyncpg
from config import DATABASE_URL

pool = None

async def create_pool_with_retry(retries=5, delay=5):
    global pool
    for i in range(retries):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
            print("✅ DB connected!")
            return pool
        except Exception as e:
            print(f"❌ DB connection failed: {e}, retrying in {delay}s...")
            await asyncio.sleep(delay)
    raise Exception("Cannot connect to DB after retries")

async def setup_db():
    await create_pool_with_retry()

# Misol: table yaratish
async def init_tables():
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            voted BOOLEAN DEFAULT FALSE
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        """)
