import os

import requests
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Query
import logging
import hmac
from urllib.parse import urlparse

CHATWOOT_URL = os.environ["CHATWOOT_URL"].rstrip("/")
CHATWOOT_TOKEN = os.environ["CHATWOOT_TOKEN"]

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

STT_URL = os.getenv("STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
STT_KEY = os.environ["STT_KEY"]
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
STT_LANG = os.getenv("STT_LANG", "es")  # vacío = autodetectar

app = FastAPI()
logger = logging.getLogger("uvicorn.error")


def transcribe(data_url: str) -> str:
    audio = requests.get(data_url, timeout=60)
    audio.raise_for_status()
    data = {"model": STT_MODEL, "response_format": "text"}
    if STT_LANG:
        data["language"] = STT_LANG
    r = requests.post(
        STT_URL,
        headers={"Authorization": f"Bearer {STT_KEY}"},
        files={"file": ("audio.ogg", audio.content, "audio/ogg")},
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    return r.text.strip()


def post_note(account_id: int, conversation_id: int, text: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    requests.post(
        url,
        headers={"api_access_token": CHATWOOT_TOKEN},
        json={
            "content": f"🎤 Transcripción:\n{text}",
            "message_type": "outgoing",
            "private": True,
        },
        timeout=30,
    ).raise_for_status()


def process(payload: dict):
    account_id = payload["account"]["id"]
    conversation_id = payload["conversation"]["id"]
    for att in payload.get("attachments") or []:
        if att.get("file_type") != "audio":
            continue
        try:
            text = transcribe(att["data_url"]) or "(audio sin voz detectada)"
        except Exception as e:
            text = f"(error al transcribir: {e})"
        post_note(account_id, conversation_id, text)


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks, token: str = Query("")):
    if not hmac.compare_digest(token, WEBHOOK_SECRET):
        raise HTTPException(status_code=401)
    payload = await request.json()
    atts = [a.get("file_type") for a in payload.get("attachments") or []]
    logger.info(
        "event=%s type=%s attachments=%s",
        payload.get("event"),
        payload.get("message_type"),
        atts,
    )
    if (
        payload.get("event") == "message_created"
        and payload.get("message_type") == "incoming"
    ):
        bg.add_task(process, payload)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
