"""Consume WhatsApp download jobs and enqueue successfully stored files for OCR."""

from __future__ import annotations

import asyncio
import logging

import httpx

from src.invoices.domain import DocumentStatus
from src.invoices.services import DownloadProcessingService
from src.jobs.contracts import DownloadJob, OcrJob
from src.jobs.redis_streams import (
    DOWNLOAD_DEAD_STREAM,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
    create_redis,
)
from src.persistence import Database, DatabaseSettings
from src.persistence.unit_of_work import create_unit_of_work_factory
from src.whatsapp.media import WhatsAppMediaDownloader, WhatsAppMediaSettings

from .settings import WorkerSettings

logger = logging.getLogger(__name__)
DOWNLOAD_GROUP = "invoice-downloaders"


class DownloadWorker:
    def __init__(
        self,
        *,
        consumer: RedisStreamConsumer,
        ocr_stream: str,
        service: DownloadProcessingService,
        settings: WorkerSettings,
        claim_idle_ms: int,
    ) -> None:
        self.consumer = consumer
        self.ocr_stream = ocr_stream
        self.service = service
        self.settings = settings
        self.claim_idle_ms = claim_idle_ms

    async def run_forever(self) -> None:
        await self.consumer.ensure_group()
        logger.info("Download worker ready and listening")
        while True:
            messages = await self.consumer.claim_stale(
                min_idle_ms=self.claim_idle_ms,
                count=self.settings.download_concurrency,
            )
            if not messages:
                messages = await self.consumer.read(
                    count=self.settings.download_concurrency
                )
            if messages:
                await asyncio.gather(*(self._handle(message) for message in messages))

    async def _handle(self, message: StreamMessage) -> None:
        try:
            job = DownloadJob.from_dict(message.payload)
        except (KeyError, TypeError, ValueError) as error:
            await self.consumer.dead_letter(
                message,
                dead_stream=DOWNLOAD_DEAD_STREAM,
                error=f"Invalid download job: {error}",
            )
            return
        try:
            document = await self.service.process(job)
            if document.status not in {
                DocumentStatus.COMPLETED,
                DocumentStatus.FAILED,
            }:
                await self.consumer.replace(
                    message.id,
                    destination_stream=self.ocr_stream,
                    payload=OcrJob(submission_id=document.id).to_dict(),
                )
            else:
                await self.consumer.complete(message.id)
            logger.info("Downloaded invoice submission %s", document.id)
        except Exception as error:
            logger.exception("Download attempt %s failed", job.attempt)
            if job.attempt < self.settings.download_max_attempts:
                await asyncio.sleep(self.settings.retry_delay(job.attempt))
                await self.consumer.replace(
                    message.id,
                    destination_stream=self.consumer.stream,
                    payload=job.with_attempt(job.attempt + 1).to_dict(),
                )
                return
            await asyncio.to_thread(self.service.mark_failed, job.submission_id, error)
            await self.consumer.dead_letter(
                message,
                dead_stream=DOWNLOAD_DEAD_STREAM,
                error=str(error),
            )


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    queue_settings = RedisQueueSettings.from_environment()
    worker_settings = WorkerSettings.from_environment()
    media_settings = WhatsAppMediaSettings.from_environment()
    redis = create_redis(queue_settings)
    database = Database(DatabaseSettings.from_environment())
    try:
        async with httpx.AsyncClient(
            timeout=media_settings.request_timeout_seconds,
            follow_redirects=False,
        ) as client:
            worker = DownloadWorker(
                consumer=RedisStreamConsumer(
                    redis,
                    stream=queue_settings.download_stream,
                    group=DOWNLOAD_GROUP,
                    dead_letter_max_entries=(
                        queue_settings.dead_letter_max_entries
                    ),
                    dead_letter_retention_days=(
                        queue_settings.dead_letter_retention_days
                    ),
                ),
                ocr_stream=queue_settings.ocr_stream,
                service=DownloadProcessingService(
                    create_unit_of_work_factory(database),
                    WhatsAppMediaDownloader(media_settings, client),
                ),
                settings=worker_settings,
                claim_idle_ms=queue_settings.download_claim_idle_ms,
            )
            await worker.run_forever()
    finally:
        await redis.aclose()
        database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
