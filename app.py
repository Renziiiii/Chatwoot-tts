import os

import requests
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Query
import logging
import hmac
from urllib.parse import urljoin, urlparse

CHATWOOT_URL = os.environ["CHATWOOT_URL"].rstrip("/")
CHATWOOT_TOKEN = os.environ["CHATWOOT_TOKEN"]
CHATWOOT_HOST = urlparse(CHATWOOT_URL).netloc

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB

STT_URL = os.getenv("STT_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
STT_KEY = os.environ["STT_KEY"]
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
STT_LANG = os.getenv("STT_LANG", "es")  # vacío = autodetectar

app = FastAPI()
logger = logging.getLogger("uvicorn.error")


def download_audio(data_url: str) -> bytes:
    """Baja el audio. Único punto que toca la red con una URL de afuera: se valida acá.

    Chatwoot entrega los audios con una redirección (ActiveStorage redirect),
    así que la seguimos, pero validando el host en cada salto: si en algún
    momento redirige a otro host, se corta igual que antes (anti SSRF).
    """
    url = data_url
    for _ in range(3):  # máximo de redirecciones a seguir
        if urlparse(url).netloc != CHATWOOT_HOST:
            raise ValueError("el audio no viene de Chatwoot")
        with requests.get(url, timeout=60, allow_redirects=False, stream=True) as audio:
            if audio.is_redirect:
                # urljoin resuelve también Locations relativas
                url = urljoin(url, audio.headers.get("Location", ""))
                continue
            audio.raise_for_status()
            # Chequeo barato: si el servidor ya declaró el tamaño, cortamos sin bajar nada.
            declared = audio.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
                raise ValueError("audio demasiado grande")
            # Chequeo real: bajamos de a pedacitos y cortamos apenas nos pasamos.
            chunks = []
            received = 0
            for chunk in audio.iter_content(chunk_size=64 * 1024):
                received += len(chunk)
                if received > MAX_AUDIO_BYTES:
                    raise ValueError("audio demasiado grande")
                chunks.append(chunk)
            return b"".join(chunks)
    raise ValueError("demasiadas redirecciones")


def transcribe(audio: bytes) -> str:
    """Convierte bytes de audio en texto usando Groq. No maneja URLs ni descargas."""
    data = {"model": STT_MODEL, "response_format": "text"}
    if STT_LANG:
        data["language"] = STT_LANG
    r = requests.post(
        STT_URL,
        headers={"Authorization": f"Bearer {STT_KEY}"},
        files={"file": ("audio.ogg", audio, "audio/ogg")},
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    return r.text.strip()


def post_note(account_id: int, conversation_id: int, text: str):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    requests.post(
        url,
        headers={"api-access-token": CHATWOOT_TOKEN},
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
            audio = download_audio(att["data_url"])
            text = transcribe(audio) or "(audio sin voz detectada)"
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
