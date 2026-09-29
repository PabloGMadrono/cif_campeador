"""Tests for deterministic Redis jobs and download-worker acknowledgement rules."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fakeredis.aioredis import FakeRedis

from src.invoices.domain import DocumentStatus, MessageType
from src.jobs.contracts import DownloadJob, OcrJob
from src.jobs.redis_streams import (
    RedisInvoiceJobQueue,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
)
from src.workers.download import DownloadWorker
from src.workers.ocr import OcrWorker
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
            settings,
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
            settings,
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
            settings,
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


def _worker(worker_type, consumer):
    dependencies = (
        {"downloader": Mock()}
        if worker_type is DownloadWorker
        else {"extractor": Mock(), "media_directory": Path(".")}
    )
    return worker_type(
        consumer=consumer,
        database=Mock(),
        settings=WorkerSettings(20, 3, 1, 3, (0,)),
        **dependencies,
    )


def test_download_worker_requeues_failed_attempt_before_acknowledging():
    consumer = SimpleNamespace(stream="whatsapp:downloads", replace=AsyncMock())
    worker = _worker(DownloadWorker, consumer)
    worker.process_download = AsyncMock(
        side_effect=TimeoutError("temporary Meta timeout")
    )
    worker.mark_failed = Mock()
    message = StreamMessage(id="1-0", payload=_job().to_dict())

    asyncio.run(worker._handle(message))

    consumer.replace.assert_awaited_once_with(
        message.id,
        destination_stream=consumer.stream,
        payload=_job(2).to_dict(),
    )
    worker.mark_failed.assert_not_called()


def test_completed_submission_is_acknowledged_without_enqueuing_ocr():
    job = _job()
    consumer = SimpleNamespace(complete=AsyncMock(), replace=AsyncMock())
    worker = _worker(DownloadWorker, consumer)
    worker.process_download = AsyncMock(
        return_value=SimpleNamespace(
            id=job.submission_id,
            status=DocumentStatus.COMPLETED,
        )
    )

    asyncio.run(worker._handle(StreamMessage(id="2-0", payload=job.to_dict())))

    consumer.complete.assert_awaited_once_with("2-0")
    consumer.replace.assert_not_awaited()


def test_download_worker_hands_off_to_configured_ocr_stream():
    job = _job()
    consumer = SimpleNamespace(
        settings=RedisQueueSettings(url="redis://test", ocr_stream="custom:ocr"),
        complete=AsyncMock(),
        replace=AsyncMock(),
    )
    worker = _worker(DownloadWorker, consumer)
    worker.process_download = AsyncMock(
        return_value=SimpleNamespace(
            id=job.submission_id,
            status=DocumentStatus.DOWNLOADED,
        )
    )

    asyncio.run(worker._handle(StreamMessage(id="download-1", payload=job.to_dict())))

    consumer.replace.assert_awaited_once_with(
        "download-1",
        destination_stream="custom:ocr",
        payload=OcrJob(job.submission_id).to_dict(),
    )
    consumer.complete.assert_not_awaited()


def test_ocr_worker_acknowledges_only_after_processing_completes():
    events = []
    job = OcrJob(_job().submission_id)

    def process(document_id):
        events.append("processed")
        return DocumentStatus.COMPLETED

    async def complete(message_id):
        events.append("acknowledged")

    consumer = SimpleNamespace(complete=AsyncMock(side_effect=complete))
    worker = _worker(OcrWorker, consumer)
    worker.process_ocr = Mock(side_effect=process)
    worker.mark_failed = Mock()
    asyncio.run(worker._handle(StreamMessage("ocr-1", job.to_dict())))

    assert events == ["processed", "acknowledged"]
    worker.process_ocr.assert_called_once_with(job.submission_id)
    consumer.complete.assert_awaited_once_with("ocr-1")
    worker.mark_failed.assert_not_called()


@pytest.mark.parametrize(
    "worker_type, job_type, stream",
    [
        (DownloadWorker, DownloadJob, "whatsapp:downloads"),
        (OcrWorker, OcrJob, "whatsapp:ocr"),
    ],
)
@pytest.mark.parametrize("attempt", [1, 3])
def test_workers_retry_or_record_final_failure(worker_type, job_type, stream, attempt):
    error = TimeoutError("temporary provider timeout")
    download_job = _job(attempt)
    job = (
        download_job
        if job_type is DownloadJob
        else OcrJob(download_job.submission_id, attempt)
    )
    consumer = SimpleNamespace(
        stream=stream,
        complete=AsyncMock(),
        replace=AsyncMock(),
        dead_letter=AsyncMock(),
    )
    worker = _worker(worker_type, consumer)
    if worker_type is DownloadWorker:
        worker.process_download = AsyncMock(side_effect=error)
    else:
        worker.process_ocr = Mock(side_effect=error)
    worker.mark_failed = Mock()
    message = StreamMessage("failed-1", job.to_dict())

    asyncio.run(worker._handle(message))

    consumer.complete.assert_not_awaited()
    if attempt == 1:
        consumer.replace.assert_awaited_once_with(
            message.id,
            destination_stream=stream,
            payload=job.with_attempt(2).to_dict(),
        )
        worker.mark_failed.assert_not_called()
        consumer.dead_letter.assert_not_awaited()
    else:
        worker.mark_failed.assert_called_once()
        document_id, recorded_error = worker.mark_failed.call_args.args
        assert document_id == job.submission_id
        assert type(recorded_error) is TimeoutError
        assert str(recorded_error) == str(error)
        consumer.dead_letter.assert_awaited_once_with(
            message,
            dead_stream=f"{stream}:dead",
            error=str(error),
        )
        consumer.replace.assert_not_awaited()


@pytest.mark.parametrize("stage, timeout", [("download", 123), ("ocr", 456)])
def test_consumer_uses_its_streams_configured_claim_timeout(stage, timeout):
    settings = RedisQueueSettings(
        url="redis://test",
        download_stream="custom:downloads",
        ocr_stream="custom:ocr",
        download_claim_idle_ms=123,
        ocr_claim_idle_ms=456,
    )
    stream = settings.download_stream if stage == "download" else settings.ocr_stream
    redis = SimpleNamespace(xautoclaim=AsyncMock(return_value=[b"0-0", [], []]))
    consumer = RedisStreamConsumer(
        redis, settings, stream=stream, group="workers", consumer="worker-1"
    )

    assert asyncio.run(consumer.claim_stale(count=2)) == []

    redis.xautoclaim.assert_awaited_once_with(
        stream, "workers", "worker-1", timeout, "0-0", count=2
    )


def test_consumer_uses_configured_dead_letter_limits():
    settings = RedisQueueSettings(
        url="redis://test", dead_letter_max_entries=42, dead_letter_retention_days=7
    )
    pipeline = Mock()
    pipeline.execute = AsyncMock()
    redis = Mock()
    redis.pipeline.return_value = pipeline
    consumer = RedisStreamConsumer(
        redis, settings, stream=settings.download_stream, group="workers"
    )
    message = StreamMessage("failed-1", _job().to_dict())
    earliest_cutoff = datetime.now(UTC) - timedelta(days=7)

    asyncio.run(
        consumer.dead_letter(message, dead_stream="downloads:dead", error="failure")
    )

    latest_cutoff = datetime.now(UTC) - timedelta(days=7)
    assert pipeline.xadd.call_args.kwargs == {"maxlen": 42, "approximate": True}
    cutoff_ms = int(pipeline.xtrim.call_args.kwargs["minid"].split("-")[0])
    assert (
        int(earliest_cutoff.timestamp() * 1000)
        <= cutoff_ms
        <= int(latest_cutoff.timestamp() * 1000)
    )
    pipeline.xack.assert_called_once_with(
        settings.download_stream, "workers", message.id
    )
    pipeline.xdel.assert_called_once_with(settings.download_stream, message.id)
    pipeline.execute.assert_awaited_once()
