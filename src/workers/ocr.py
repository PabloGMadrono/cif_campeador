"""Consume downloaded invoice jobs and persist structured OCR results."""

from __future__ import annotations

import asyncio
import logging

from src.invoices.domain import DocumentStatus
from src.invoices.services import OcrProcessingService
from src.jobs.contracts import OcrJob
from src.jobs.redis_streams import (
    OCR_DEAD_STREAM,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
    create_redis,
)
from src.ocr import invoice_extractor
from src.persistence import Database, DatabaseSettings
from src.persistence.unit_of_work import create_unit_of_work_factory
from src.whatsapp.media import WhatsAppMediaSettings

from .settings import WorkerSettings

logger = logging.getLogger(__name__)
OCR_GROUP = "invoice-ocr-workers"


class OcrWorker:
    def __init__(
        self,
        *,
        consumer: RedisStreamConsumer,
        service: OcrProcessingService,
        settings: WorkerSettings,
        claim_idle_ms: int,
    ) -> None:
        self.consumer = consumer
        self.service = service
        self.settings = settings
        self.claim_idle_ms = claim_idle_ms

    async def run_forever(self) -> None:
        await self.consumer.ensure_group()
        logger.info("OCR worker ready and listening")
        while True:
            messages = await self.consumer.claim_stale(
                min_idle_ms=self.claim_idle_ms,
                count=self.settings.ocr_concurrency,
            )
            if not messages:
                messages = await self.consumer.read(count=self.settings.ocr_concurrency)
            if messages:
                await asyncio.gather(*(self._handle(message) for message in messages))

    async def _handle(self, message: StreamMessage) -> None:
        try:
            job = OcrJob.from_dict(message.payload)
        except (KeyError, TypeError, ValueError) as error:
            await self.consumer.dead_letter(
                message,
                dead_stream=OCR_DEAD_STREAM,
                error=f"Invalid OCR job: {error}",
            )
            return
        try:
            document = await asyncio.to_thread(
                self.service.process,
                job.submission_id,
            )
            await self.consumer.complete(message.id)
            if document.status == DocumentStatus.COMPLETED:
                logger.info("Completed OCR for invoice submission %s", document.id)
        except Exception as error:
            logger.exception("OCR attempt %s failed", job.attempt)
            if job.attempt < self.settings.ocr_max_attempts:
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
                dead_stream=OCR_DEAD_STREAM,
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
        worker = OcrWorker(
                consumer=RedisStreamConsumer(
                    redis,
                    stream=queue_settings.ocr_stream,
                    group=OCR_GROUP,
                    dead_letter_max_entries=(
                        queue_settings.dead_letter_max_entries
                    ),
                    dead_letter_retention_days=(
                        queue_settings.dead_letter_retention_days
                    ),
                ),
            service=OcrProcessingService(
                create_unit_of_work_factory(database),
                invoice_extractor,
                media_settings.media_directory,
            ),
            settings=worker_settings,
            claim_idle_ms=queue_settings.ocr_claim_idle_ms,
        )
        await worker.run_forever()
    finally:
        await redis.aclose()
        database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
