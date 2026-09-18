"""Persistence interfaces consumed by invoice application services."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self
from uuid import UUID

from src.invoices.domain import (
    CustomerPhoneNumber,
    DownloadedAttachment,
    FailureStage,
    InboundDocument,
    StoredInvoice,
)
from src.jobs.contracts import DownloadJob


class CustomerRepository(Protocol):
    def resolve_sender(
        self,
        *,
        phone_number: str,
        meta_user_id: str | None,
        profile_name: str | None,
        seen_at: datetime,
    ) -> CustomerPhoneNumber: ...


class DocumentRepository(Protocol):
    def add_if_absent(self, job: DownloadJob) -> InboundDocument: ...

    def get(self, document_id: UUID) -> InboundDocument | None: ...

    def mark_downloading(self, document_id: UUID) -> InboundDocument: ...

    def mark_downloaded(
        self,
        document_id: UUID,
        attachment: DownloadedAttachment,
    ) -> InboundDocument: ...

    def mark_ocr_processing(
        self,
        document_id: UUID,
        extractor_name: str,
    ) -> InboundDocument: ...

    def mark_completed(self, document_id: UUID, completed_at: datetime) -> None: ...

    def mark_failed(
        self,
        document_id: UUID,
        stage: FailureStage,
        error: str,
    ) -> None: ...


class InvoiceRepository(Protocol):
    def get(self, document_id: UUID) -> StoredInvoice | None: ...

    def add(self, invoice: StoredInvoice) -> None: ...


class UnitOfWork(Protocol):
    customers: CustomerRepository
    documents: DocumentRepository
    invoices: InvoiceRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type, exc_value, traceback) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
