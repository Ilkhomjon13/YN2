from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import asyncio

app = FastAPI()
clients = {}  # survey_id → set of websockets

# HTML frontendni ishlatish
@app.get("/monitor")
async def monitor_page(survey_id: int):
    with open("monitor.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(html_content.replace("SURVEY_ID_PLACEHOLDER", str(survey_id)))

# Frontend WebSocket
@app.websocket("/ws/{survey_id}")
async def websocket_endpoint(websocket: WebSocket, survey_id: int):
    await websocket.accept()
    if survey_id not in clients:
        clients[survey_id] = set()
    clients[survey_id].add(websocket)
    try:
        while True:
            await asyncio.sleep(10)  # keep connection alive
    except Exception:
        pass
    finally:
        clients[survey_id].remove(websocket)

# Botdan kelgan update endpoint
@app.post("/update")
async def update_votes(data: dict):
    survey_id = str(data.get("survey_id"))
    # Shu yerda DB yoki cache’dan natijalarni oling
    from bot import pool  # bot.py da pool mavjud deb faraz qilamiz
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT name, votes FROM candidates WHERE survey_id=$1 ORDER BY id", int(survey_id))
    result = [{"name": r["name"], "votes": r["votes"]} for r in rows]

    # Websocket orqali barcha frontendlarga yuborish
    for ws in clients.get(survey_id, []):
        try:
            await ws.send_json(result)
        except Exception:
            pass
    return {"status": "ok"}
