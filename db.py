import asyncpg
from config import DATABASE_URL

async def create_pool():
    return await asyncpg.create_pool(DATABASE_URL)

async def init_db(pool):
    async with pool.acquire() as conn:
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
            survey_id INTEGER REFERENCES surveys(id),
            name TEXT NOT NULL,
            votes INTEGER DEFAULT 0
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS voted_users (
            id SERIAL PRIMARY KEY,
            survey_id INTEGER REFERENCES surveys(id),
            user_id BIGINT NOT NULL
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id SERIAL PRIMARY KEY,
            survey_id INTEGER REFERENCES surveys(id),
            channel TEXT NOT NULL
        );
        """)
