"""FastAPI entry point for inbound WhatsApp webhook events."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from src.whatsapp.media import (
    WhatsAppMediaSettings,
    download_images,
    extract_images,
)


# Load the repository's .env even when Uvicorn is started from another directory.
load_dotenv(Path(__file__).resolve().parent / ".env")


app = FastAPI()

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
MEDIA_SETTINGS = WhatsAppMediaSettings.from_environment()


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Complete the one-time webhook verification challenge from Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if VERIFY_TOKEN and mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        print("WEBHOOK VERIFIED")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Acknowledge an event and download any image attachments in the background."""
    body: dict[str, Any] = await request.json()

    print("WEBHOOK RECEIVED:")
    print(body)

    images = extract_images(body)
    if images:
        background_tasks.add_task(download_images, images, MEDIA_SETTINGS)

    return Response(status_code=200)
