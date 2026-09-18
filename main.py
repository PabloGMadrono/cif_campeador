"""FastAPI entry point for inbound WhatsApp webhook events."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from src.environment import load_project_environment
from src.jobs.contracts import InvoiceJobQueue
from src.jobs.redis_streams import (
    RedisInvoiceJobQueue,
    RedisQueueSettings,
    create_redis,
)
from src.whatsapp.events import extract_download_jobs

load_project_environment()
logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = RedisQueueSettings.from_environment()
    redis = create_redis(settings)
    app.state.job_queue = RedisInvoiceJobQueue(redis, settings)
    try:
        yield
    finally:
        await redis.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Complete the one-time webhook verification challenge from Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if VERIFY_TOKEN and mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        logger.info("WhatsApp webhook verified")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Publish supported WhatsApp attachments to the durable download stream."""
    body: Any = await request.json()
    if not isinstance(body, dict):
        return Response(content="Invalid webhook payload", status_code=400)

    jobs = extract_download_jobs(body)
    queue: InvoiceJobQueue = request.app.state.job_queue
    if jobs:
        await asyncio.gather(*(queue.enqueue_download(job) for job in jobs))
        logger.info("Queued %s WhatsApp invoice attachment(s)", len(jobs))

    return Response(status_code=200)
