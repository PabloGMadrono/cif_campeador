"""Consume WhatsApp download jobs and enqueue successfully stored files for OCR."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import httpx

from src.invoices.domain import (
    DocumentStatus,
    DownloadedAttachment,
    FailureStage,
    utc_now,
)
from src.jobs.contracts import DownloadJob, OcrJob
from src.jobs.redis_streams import (
    DOWNLOAD_DEAD_STREAM,
    RedisQueueSettings,
    RedisStreamConsumer,
    StreamMessage,
    create_redis,
)
from src.persistence import Database, DatabaseSettings
from src.persistence.models import DocumentRecord
from src.persistence.operations import (
    add_document,
    mark_failed,
    require_document,
    resolve_sender,
)
from src.whatsapp.media import (
    WhatsAppAttachment,
    WhatsAppMediaDownloader,
    WhatsAppMediaSettings,
)

from .settings import WorkerSettings

logger = logging.getLogger(__name__)
DOWNLOAD_GROUP = "invoice-downloaders"


class DownloadWorker:
    def __init__(
        self,
        *,
        consumer: RedisStreamConsumer,
        database: Database,
        downloader: WhatsAppMediaDownloader,
        settings: WorkerSettings,
    ) -> None:
        self.consumer = consumer
        self.database = database
        self.downloader = downloader
        self.settings = settings

    async def process_download(self, job: DownloadJob) -> DocumentRecord:
        """Commit the download attempt, fetch the file, then commit its metadata."""
        document = await asyncio.to_thread(self._prepare_download, job)
        if document.status != DocumentStatus.DOWNLOADING:
            return document

        attachment = WhatsAppAttachment(
            media_id=job.whatsapp_media_id,
            message_type=job.message_type,
            mime_type=job.mime_type,
            received_at=job.received_at,
            sha256=job.sha256,
        )
        downloaded = await self.downloader.download(attachment)
        return await asyncio.to_thread(
            self._record_download,
            job.submission_id,
            downloaded,
        )

    def _prepare_download(self, job: DownloadJob) -> DocumentRecord:
        with self.database.session_factory.begin() as session:
            resolve_sender(session, job)
            document = add_document(session, job)
            if document.status in {DocumentStatus.RECEIVED, DocumentStatus.DOWNLOADING}:
                document.status = DocumentStatus.DOWNLOADING
                document.download_attempts += 1
                document.failure_stage = None
                document.last_error = None
                document.updated_at = utc_now()
        return document

    def _record_download(
        self,
        document_id: UUID,
        attachment: DownloadedAttachment,
    ) -> DocumentRecord:
        with self.database.session_factory.begin() as session:
            document = require_document(session, document_id)
            now = utc_now()
            document.storage_path = attachment.storage_path
            document.file_size = attachment.file_size
            document.downloaded_at = now
            document.status = DocumentStatus.DOWNLOADED
            document.failure_stage = None
            document.last_error = None
            document.updated_at = now
        return document

    def mark_failed(self, document_id: UUID, error: Exception) -> None:
        with self.database.session_factory.begin() as session:
            mark_failed(session, document_id, error, FailureStage.DOWNLOAD)

    async def run_forever(self) -> None:
        await self.consumer.ensure_group()
        logger.info("Download worker ready and listening")
        while True:
            messages = await self.consumer.claim_stale(
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
            document = await self.process_download(job)
            if document.status not in {
                DocumentStatus.COMPLETED,
                DocumentStatus.FAILED,
            }:
                await self.consumer.replace(
                    message.id,
                    destination_stream=self.consumer.settings.ocr_stream,
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
            await asyncio.to_thread(self.mark_failed, job.submission_id, error)
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
                    queue_settings,
                    stream=queue_settings.download_stream,
                    group=DOWNLOAD_GROUP,
                ),
                database=database,
                downloader=WhatsAppMediaDownloader(media_settings, client),
                settings=worker_settings,
            )
            await worker.run_forever()
    finally:
        await redis.aclose()
        database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
