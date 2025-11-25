# db.py
import asyncpg
import ssl
from config import DATABASE_URL

async def create_pool():
    """
    PostgreSQL bilan ulanishni yaratadi va pool qaytaradi.
    SSL bilan ishlashni qo'llab-quvvatlaydi.
    """
    # SSL kontekstini yaratish
    ssl_context = ssl.create_default_context()
    
    # Pool yaratish
    pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        ssl=ssl_context,
        min_size=1,  # minimal ulanishlar soni
        max_size=5   # maksimal ulanishlar soni
    )
    return pool

async def init_db(pool):
    """
    Bazani boshlang'ich sozlash (jadval yaratish va hokazo)
    """
    async with pool.acquire() as conn:
        # Jadval yaratish misoli
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            image TEXT,
            active BOOLEAN DEFAULT TRUE
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id SERIAL PRIMARY KEY,
            survey_id INT REFERENCES surveys(id),
            name TEXT NOT NULL,
            votes INT DEFAULT 0
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS voted_users (
            survey_id INT REFERENCES surveys(id),
            user_id BIGINT,
            PRIMARY KEY (survey_id, user_id)
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            survey_id INT REFERENCES surveys(id),
            channel TEXT
        );
        """)
