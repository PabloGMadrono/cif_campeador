"""Redis Streams adapter for durable invoice pipeline jobs."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from src.environment import load_project_environment

from .contracts import DownloadJob, OcrJob

DOWNLOAD_STREAM = "whatsapp:downloads"
OCR_STREAM = "whatsapp:ocr"
DOWNLOAD_DEAD_STREAM = "whatsapp:downloads:dead"
OCR_DEAD_STREAM = "whatsapp:ocr:dead"


@dataclass(frozen=True, slots=True)
class RedisQueueSettings:
    url: str
    download_stream: str = DOWNLOAD_STREAM
    ocr_stream: str = OCR_STREAM
    download_claim_idle_ms: int = 300_000
    ocr_claim_idle_ms: int = 1_800_000
    dead_letter_max_entries: int = 10_000
    dead_letter_retention_days: int = 30

    @classmethod
    def from_environment(cls) -> RedisQueueSettings:
        load_project_environment()
        settings = cls(
            url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            download_claim_idle_ms=int(
                os.getenv("DOWNLOAD_CLAIM_IDLE_MS", "300000")
            ),
            ocr_claim_idle_ms=int(os.getenv("OCR_CLAIM_IDLE_MS", "1800000")),
            dead_letter_max_entries=int(
                os.getenv("REDIS_DEAD_LETTER_MAX_ENTRIES", "10000")
            ),
            dead_letter_retention_days=int(
                os.getenv("REDIS_DEAD_LETTER_RETENTION_DAYS", "30")
            ),
        )
        if min(
            settings.download_claim_idle_ms,
            settings.ocr_claim_idle_ms,
            settings.dead_letter_max_entries,
            settings.dead_letter_retention_days,
        ) <= 0:
            raise ValueError("Redis queue limits and retention must be positive")
        return settings


@dataclass(frozen=True, slots=True)
class StreamMessage:
    id: str
    payload: dict[str, Any]


class RedisInvoiceJobQueue:
    """Publish download and OCR jobs as compact JSON stream entries."""

    def __init__(self, redis: Redis, settings: RedisQueueSettings) -> None:
        self.redis = redis
        self.settings = settings

    async def enqueue_download(self, job: DownloadJob) -> str:
        return await self._publish(self.settings.download_stream, job.to_dict())

    async def enqueue_ocr(self, job: OcrJob) -> str:
        return await self._publish(self.settings.ocr_stream, job.to_dict())

    async def _publish(self, stream: str, payload: dict[str, Any]) -> str:
        message_id = await self.redis.xadd(
            stream,
            {"payload": json.dumps(payload, separators=(",", ":"))},
        )
        return _decode(message_id)


class RedisStreamConsumer:
    """Consumer-group operations shared by the two worker processes."""

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str,
        group: str,
        consumer: str | None = None,
        dead_letter_max_entries: int = 10_000,
        dead_letter_retention_days: int = 30,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.dead_letter_max_entries = dead_letter_max_entries
        self.dead_letter_retention_days = dead_letter_retention_days

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                self.stream,
                self.group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def read(self, *, count: int, block_ms: int = 5_000) -> list[StreamMessage]:
        response = await self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        return _parse_stream_response(response)

    async def claim_stale(
        self,
        *,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamMessage]:
        response = await self.redis.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_ms,
            "0-0",
            count=count,
        )
        entries = response[1] if response and len(response) > 1 else []
        return _parse_entries(entries)

    async def complete(self, message_id: str) -> None:
        """Atomically acknowledge and remove a successfully processed entry."""
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.xack(self.stream, self.group, message_id)
        pipeline.xdel(self.stream, message_id)
        await pipeline.execute()

    async def replace(
        self,
        message_id: str,
        *,
        destination_stream: str,
        payload: dict[str, Any],
    ) -> str:
        """Atomically publish the next job and remove the completed source job."""
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.xadd(destination_stream, {"payload": _serialize(payload)})
        pipeline.xack(self.stream, self.group, message_id)
        pipeline.xdel(self.stream, message_id)
        results = await pipeline.execute()
        return _decode(results[0])

    async def dead_letter(
        self,
        message: StreamMessage,
        *,
        dead_stream: str,
        error: str,
    ) -> None:
        cutoff = datetime.now(UTC) - timedelta(
            days=self.dead_letter_retention_days
        )
        cutoff_id = f"{int(cutoff.timestamp() * 1000)}-0"
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.xadd(
            dead_stream,
            {
                "payload": _serialize(message.payload),
                "source_stream": self.stream,
                "source_message_id": message.id,
                "error": error[:2000],
            },
            maxlen=self.dead_letter_max_entries,
            approximate=True,
        )
        pipeline.xtrim(dead_stream, minid=cutoff_id, approximate=False)
        pipeline.xack(self.stream, self.group, message.id)
        pipeline.xdel(self.stream, message.id)
        await pipeline.execute()


def create_redis(settings: RedisQueueSettings) -> Redis:
    return Redis.from_url(settings.url, decode_responses=False)


def _parse_stream_response(response: Any) -> list[StreamMessage]:
    messages: list[StreamMessage] = []
    for _stream_name, entries in response or []:
        messages.extend(_parse_entries(entries))
    return messages


def _parse_entries(entries: Any) -> list[StreamMessage]:
    messages: list[StreamMessage] = []
    for message_id, fields in entries or []:
        payload_value = fields.get(b"payload", fields.get("payload"))
        if payload_value is None:
            raise ValueError(f"Redis job {message_id!r} has no payload")
        messages.append(
            StreamMessage(
                id=_decode(message_id),
                payload=json.loads(_decode(payload_value)),
            )
        )
    return messages


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))
