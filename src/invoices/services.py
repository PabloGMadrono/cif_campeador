"""Application services for the download and OCR pipeline stages."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol
from uuid import UUID

from src.invoices.domain import (
    DocumentStatus,
    FailureStage,
    InboundDocument,
    StoredInvoice,
    utc_now,
)
from src.jobs.contracts import DownloadJob
from src.ocr.models import Invoice
from src.persistence.unit_of_work import UnitOfWorkFactory
from src.whatsapp.media import WhatsAppAttachment, WhatsAppMediaDownloader


class InvoiceExtractor(Protocol):
    def extract_invoice(self, path: str) -> Invoice: ...


class DownloadProcessingService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        downloader: WhatsAppMediaDownloader,
    ) -> None:
        self.unit_of_work_factory = unit_of_work_factory
        self.downloader = downloader

    async def process(self, job: DownloadJob) -> InboundDocument:
        document = await asyncio.to_thread(self._prepare, job)
        if document.status in {
            DocumentStatus.DOWNLOADED,
            DocumentStatus.OCR_PROCESSING,
            DocumentStatus.COMPLETED,
            DocumentStatus.FAILED,
        }:
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

    def mark_failed(self, document_id: UUID, error: Exception) -> None:
        with self.unit_of_work_factory() as uow:
            uow.documents.mark_failed(document_id, FailureStage.DOWNLOAD, str(error))
            uow.commit()

    def _prepare(self, job: DownloadJob) -> InboundDocument:
        with self.unit_of_work_factory() as uow:
            uow.customers.resolve_sender(
                phone_number=job.phone_number,
                meta_user_id=job.meta_user_id,
                profile_name=job.profile_name,
                seen_at=job.received_at,
            )
            document = uow.documents.add_if_absent(job)
            if document.status not in {
                DocumentStatus.DOWNLOADED,
                DocumentStatus.OCR_PROCESSING,
                DocumentStatus.COMPLETED,
                DocumentStatus.FAILED,
            }:
                document = uow.documents.mark_downloading(job.submission_id)
            uow.commit()
            return document

    def _record_download(self, document_id, attachment) -> InboundDocument:
        with self.unit_of_work_factory() as uow:
            document = uow.documents.mark_downloaded(document_id, attachment)
            uow.commit()
            return document


class OcrProcessingService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        extractor: InvoiceExtractor,
        media_directory: Path,
    ) -> None:
        self.unit_of_work_factory = unit_of_work_factory
        self.extractor = extractor
        self.media_directory = media_directory

    def process(self, document_id: UUID) -> InboundDocument:
        document = self._prepare(document_id)
        if document.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
            return document
        if not document.storage_path:
            raise RuntimeError(f"Invoice submission {document_id} has no stored file")

        absolute_path = (self.media_directory / document.storage_path).resolve()
        if not absolute_path.is_relative_to(self.media_directory.resolve()):
            raise ValueError("Stored invoice path escapes the media directory")
        invoice = self.extractor.extract_invoice(str(absolute_path))

        completed_at = utc_now()
        with self.unit_of_work_factory() as uow:
            uow.invoices.add(
                StoredInvoice(
                    document_id=document_id,
                    invoice=invoice,
                    extracted_at=completed_at,
                )
            )
            uow.documents.mark_completed(document_id, completed_at)
            uow.commit()
            result = uow.documents.get(document_id)
            if result is None:
                raise RuntimeError(f"Completed invoice submission {document_id} disappeared")
            return result

    def mark_failed(self, document_id: UUID, error: Exception) -> None:
        with self.unit_of_work_factory() as uow:
            uow.documents.mark_failed(document_id, FailureStage.OCR, str(error))
            uow.commit()

    def _prepare(self, document_id: UUID) -> InboundDocument:
        extractor_name = type(self.extractor).__name__
        with self.unit_of_work_factory() as uow:
            document = uow.documents.get(document_id)
            if document is None:
                raise LookupError(f"Invoice submission {document_id} does not exist")
            if document.status not in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
                if document.status not in {
                    DocumentStatus.DOWNLOADED,
                    DocumentStatus.OCR_PROCESSING,
                }:
                    raise RuntimeError(
                        f"Invoice submission {document_id} is not ready for OCR"
                    )
                document = uow.documents.mark_ocr_processing(
                    document_id,
                    extractor_name,
                )
            uow.commit()
            return document
