"""Tests for deterministic Redis jobs and download-worker acknowledgement rules."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from fakeredis.aioredis import FakeRedis

from src.invoices.domain import DocumentStatus, MessageType
from src.jobs.contracts import DownloadJob
from src.jobs.redis_streams import (
    RedisInvoiceJobQueue,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
)
from src.workers.download import DownloadWorker
from src.workers.settings import WorkerSettings


def _job(attempt=1):
    job = DownloadJob.create(
        whatsapp_message_id="wamid-1",
        whatsapp_media_id="media-1",
        message_type=MessageType.IMAGE,
        mime_type="image/jpeg",
        phone_number="34638894450",
        received_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    return job.with_attempt(attempt)


def test_download_job_id_is_deterministic_and_round_trips():
    first = _job()
    second = _job()

    assert first.submission_id == second.submission_id
    assert DownloadJob.from_dict(first.to_dict()) == first


def test_redis_queue_publishes_compact_json():
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def xadd(self, stream, fields):
            self.calls.append((stream, fields))
            return b"123-0"

    redis = FakeRedis()
    settings = RedisQueueSettings(url="redis://test")
    queue = RedisInvoiceJobQueue(redis, settings)

    message_id = asyncio.run(queue.enqueue_download(_job()))

    assert message_id == "123-0"
    stream, fields = redis.calls[0]
    assert stream == "whatsapp:downloads"
    assert json.loads(fields["payload"])["whatsapp_message_id"] == "wamid-1"


def test_redis_stream_consumer_completes_and_deletes_jobs():
    async def exercise_stream():
        redis = FakeRedis(decode_responses=False)
        settings = RedisQueueSettings(url="redis://test")
        queue = RedisInvoiceJobQueue(redis, settings)
        consumer = RedisStreamConsumer(
            redis,
            stream=settings.download_stream,
            group="test-downloaders",
            consumer="worker-1",
        )
        await consumer.ensure_group()
        await queue.enqueue_download(_job())

        messages = await consumer.read(count=1, block_ms=1)
        await consumer.complete(messages[0].id)
        pending = await redis.xpending(settings.download_stream, "test-downloaders")
        stream_length = await redis.xlen(settings.download_stream)
        await redis.aclose()
        return messages, pending, stream_length

    messages, pending, stream_length = asyncio.run(exercise_stream())

    assert DownloadJob.from_dict(messages[0].payload) == _job()
    assert pending["pending"] == 0
    assert stream_length == 0


def test_redis_stream_consumer_atomically_replaces_a_job():
    async def exercise_stream():
        redis = FakeRedis(decode_responses=False)
        settings = RedisQueueSettings(url="redis://test")
        queue = RedisInvoiceJobQueue(redis, settings)
        consumer = RedisStreamConsumer(
            redis,
            stream=settings.download_stream,
            group="test-downloaders",
            consumer="worker-1",
        )
        await consumer.ensure_group()
        await queue.enqueue_download(_job())
        message = (await consumer.read(count=1, block_ms=1))[0]

        await consumer.replace(
            message.id,
            destination_stream=settings.download_stream,
            payload=_job(attempt=2).to_dict(),
        )

        entries = await redis.xrange(settings.download_stream)
        pending = await redis.xpending(settings.download_stream, "test-downloaders")
        await redis.aclose()
        return entries, pending

    entries, pending = asyncio.run(exercise_stream())

    assert len(entries) == 1
    payload = json.loads(entries[0][1][b"payload"])
    assert payload["attempt"] == 2
    assert pending["pending"] == 0


def test_dead_letter_moves_job_and_deletes_source_entry():
    async def exercise_stream():
        redis = FakeRedis(decode_responses=False)
        settings = RedisQueueSettings(url="redis://test")
        queue = RedisInvoiceJobQueue(redis, settings)
        consumer = RedisStreamConsumer(
            redis,
            stream=settings.download_stream,
            group="test-downloaders",
            consumer="worker-1",
        )
        await consumer.ensure_group()
        await queue.enqueue_download(_job(attempt=3))
        message = (await consumer.read(count=1, block_ms=1))[0]

        await consumer.dead_letter(
            message,
            dead_stream="whatsapp:downloads:dead",
            error="permanent failure",
        )

        source_length = await redis.xlen(settings.download_stream)
        dead_entries = await redis.xrange("whatsapp:downloads:dead")
        await redis.aclose()
        return source_length, dead_entries

    source_length, dead_entries = asyncio.run(exercise_stream())

    assert source_length == 0
    assert len(dead_entries) == 1
    assert dead_entries[0][1][b"error"] == b"permanent failure"


def test_download_worker_requeues_failed_attempt_before_acknowledging():
    class FailingService:
        async def process(self, job):
            raise TimeoutError("temporary Meta timeout")

        def mark_failed(self, document_id, error):
            raise AssertionError("A retryable attempt must not be marked failed")

    class FakeConsumer:
        def __init__(self):
            self.stream = "whatsapp:downloads"
            self.replacements = []

        async def replace(self, message_id, *, destination_stream, payload):
            self.replacements.append((message_id, destination_stream, payload))

    consumer = FakeConsumer()
    worker = DownloadWorker(
        consumer=consumer,
        ocr_stream="whatsapp:ocr",
        service=FailingService(),
        settings=WorkerSettings(
            download_concurrency=20,
            download_max_attempts=3,
            ocr_concurrency=1,
            ocr_max_attempts=3,
            retry_delays_seconds=(0,),
        ),
        claim_idle_ms=1000,
    )
    message = StreamMessage(id="1-0", payload=_job().to_dict())

    asyncio.run(worker._handle(message))

    assert consumer.replacements[0][0:2] == (
        "1-0",
        "whatsapp:downloads",
    )
    assert consumer.replacements[0][2]["attempt"] == 2


def test_completed_submission_is_acknowledged_without_enqueuing_ocr():
    class CompletedService:
        async def process(self, job):
            return SimpleNamespace(id=job.submission_id, status=DocumentStatus.COMPLETED)

    class FakeConsumer:
        def __init__(self):
            self.completed = []

        async def complete(self, message_id):
            self.completed.append(message_id)

    consumer = FakeConsumer()
    worker = DownloadWorker(
        consumer=consumer,
        ocr_stream="whatsapp:ocr",
        service=CompletedService(),
        settings=WorkerSettings(20, 3, 1, 3, (0,)),
        claim_idle_ms=1000,
    )

    asyncio.run(worker._handle(StreamMessage(id="2-0", payload=_job().to_dict())))

    assert consumer.completed == ["2-0"]
