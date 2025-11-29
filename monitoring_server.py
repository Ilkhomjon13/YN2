import asyncpg
import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS (front-end ochilishi uchun)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = "postgres://...."   # << sizning DB manzilingiz

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

async def get_votes(survey_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, votes FROM candidates WHERE survey_id=$1 ORDER BY id",
            survey_id
        )
    return [{"name": r["name"], "votes": r["votes"]} for r in rows]

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()

    survey_id = int(websocket.query_params.get("survey_id"))
    
    while True:
        data = await get_votes(survey_id)
        await websocket.send_json(data)
        await asyncio.sleep(1)
