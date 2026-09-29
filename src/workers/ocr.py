"""Consume downloaded invoice jobs and persist structured OCR results."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from src.invoices.domain import DocumentStatus, FailureStage, utc_now
from src.jobs.contracts import OcrJob
from src.jobs.redis_streams import (
    OCR_DEAD_STREAM,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
    create_redis,
)
from src.ocr import invoice_extractor
from src.ocr.ocr_abc import Ocr_operator
from src.persistence import Database, DatabaseSettings
from src.persistence.operations import mark_failed, require_document, save_invoice
from src.whatsapp.media import WhatsAppMediaSettings

from .settings import WorkerSettings

logger = logging.getLogger(__name__)
OCR_GROUP = "invoice-ocr-workers"


class OcrWorker:
    def __init__(
        self,
        *,
        consumer: RedisStreamConsumer,
        database: Database,
        extractor: Ocr_operator,
        media_directory: Path,
        settings: WorkerSettings,
    ) -> None:
        self.consumer = consumer
        self.database = database
        self.extractor = extractor
        self.media_directory = media_directory
        self.settings = settings

    def process_ocr(self, document_id: UUID) -> DocumentStatus:
        """Commit the OCR attempt, extract outside SQL, then save the result atomically."""
        with self.database.session_factory.begin() as session:
            document = require_document(session, document_id)
            if document.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
                return document.status
            if document.status not in {
                DocumentStatus.DOWNLOADED,
                DocumentStatus.OCR_PROCESSING,
            }:
                raise RuntimeError(
                    f"Invoice submission {document_id} is not ready for OCR"
                )
            now = utc_now()
            document.status = DocumentStatus.OCR_PROCESSING
            document.extractor_name = type(self.extractor).__name__
            document.ocr_attempts += 1
            document.ocr_started_at = document.ocr_started_at or now
            document.failure_stage = None
            document.last_error = None
            document.updated_at = now

        if not document.storage_path:
            raise RuntimeError(f"Invoice submission {document_id} has no stored file")
        absolute_path = (self.media_directory / document.storage_path).resolve()
        if not absolute_path.is_relative_to(self.media_directory.resolve()):
            raise ValueError("Stored invoice path escapes the media directory")
        invoice = self.extractor.extract_invoice(str(absolute_path))

        completed_at = utc_now()
        with self.database.session_factory.begin() as session:
            save_invoice(session, document_id, invoice, completed_at)
            document = require_document(session, document_id)
            document.status = DocumentStatus.COMPLETED
            document.completed_at = completed_at
            document.failure_stage = None
            document.last_error = None
            document.updated_at = completed_at
        return document.status

    def mark_failed(self, document_id: UUID, error: Exception) -> None:
        with self.database.session_factory.begin() as session:
            mark_failed(session, document_id, error, FailureStage.OCR)

    async def run_forever(self) -> None:
        await self.consumer.ensure_group()
        logger.info("OCR worker ready and listening")
        while True:
            messages = await self.consumer.claim_stale(
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
            status = await asyncio.to_thread(
                self.process_ocr,
                job.submission_id,
            )
            await self.consumer.complete(message.id)
            if status == DocumentStatus.COMPLETED:
                logger.info(
                    "Completed OCR for invoice submission %s", job.submission_id
                )
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
            await asyncio.to_thread(self.mark_failed, job.submission_id, error)
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
                queue_settings,
                stream=queue_settings.ocr_stream,
                group=OCR_GROUP,
            ),
            database=database,
            extractor=invoice_extractor,
            media_directory=media_settings.media_directory,
            settings=worker_settings,
        )
        await worker.run_forever()
    finally:
        await redis.aclose()
        database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
