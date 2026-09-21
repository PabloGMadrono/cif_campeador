"""SQLAlchemy repository implementations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.invoices.domain import (
    CustomerPhoneNumber,
    DocumentStatus,
    DownloadedAttachment,
    FailureStage,
    InboundDocument,
    StoredInvoice,
    utc_now,
)
from src.jobs.contracts import DownloadJob

from .models import CustomerPhoneRecord, CustomerRecord, DocumentRecord, InvoiceRecord


class SqlAlchemyCustomerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_sender(
        self,
        *,
        phone_number: str,
        meta_user_id: str | None,
        profile_name: str | None,
        seen_at: datetime,
    ) -> CustomerPhoneNumber:
        phone = self.session.get(CustomerPhoneRecord, phone_number)
        if phone is None:
            customer_id = self._customer_id_for_meta_user(meta_user_id)
            if customer_id is None:
                now = utc_now()
                customer = CustomerRecord(
                    id=uuid4(),
                    display_name=profile_name or phone_number,
                    legal_name=None,
                    tax_id=None,
                    address=None,
                    email=None,
                    is_provisional=True,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(customer)
                self.session.flush()
                customer_id = customer.id

            phone = CustomerPhoneRecord(
                phone_number=phone_number,
                customer_id=customer_id,
                meta_user_id=meta_user_id,
                profile_name=profile_name,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            self.session.add(phone)
            self.session.flush()
        else:
            stored_last_seen = phone.last_seen_at
            if stored_last_seen.tzinfo is None:
                stored_last_seen = stored_last_seen.replace(tzinfo=UTC)
            phone.last_seen_at = max(stored_last_seen, seen_at)
            if meta_user_id:
                phone.meta_user_id = meta_user_id
            if profile_name:
                phone.profile_name = profile_name

        return _phone_from_record(phone)

    def _customer_id_for_meta_user(self, meta_user_id: str | None):
        if not meta_user_id:
            return None
        customer_ids = set(
            self.session.scalars(
                select(CustomerPhoneRecord.customer_id).where(
                    CustomerPhoneRecord.meta_user_id == meta_user_id
                )
            )
        )
        return next(iter(customer_ids)) if len(customer_ids) == 1 else None


class SqlAlchemyDocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_if_absent(self, job: DownloadJob) -> InboundDocument:
        record = self.session.scalar(
            select(DocumentRecord).where(
                DocumentRecord.whatsapp_message_id == job.whatsapp_message_id
            )
        )
        if record is None:
            now = utc_now()
            record = DocumentRecord(
                id=job.submission_id,
                whatsapp_message_id=job.whatsapp_message_id,
                whatsapp_media_id=job.whatsapp_media_id,
                sender_phone_number=job.phone_number,
                message_type=job.message_type,
                mime_type=job.mime_type,
                sha256=job.sha256,
                original_filename=job.original_filename,
                storage_path=None,
                file_size=None,
                received_at=job.received_at,
                downloaded_at=None,
                ocr_started_at=None,
                completed_at=None,
                status=DocumentStatus.RECEIVED,
                extractor_name=None,
                download_attempts=0,
                ocr_attempts=0,
                failure_stage=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            self.session.add(record)
            self.session.flush()
        return _document_from_record(record)

    def get(self, document_id) -> InboundDocument | None:
        record = self.session.get(DocumentRecord, document_id)
        return _document_from_record(record) if record else None

    def mark_downloading(self, document_id) -> InboundDocument:
        record = self._require(document_id)
        if record.status in {DocumentStatus.COMPLETED, DocumentStatus.DOWNLOADED}:
            return _document_from_record(record)
        record.status = DocumentStatus.DOWNLOADING
        record.download_attempts += 1
        record.failure_stage = None
        record.last_error = None
        record.updated_at = utc_now()
        self.session.flush()
        return _document_from_record(record)

    def mark_downloaded(
        self,
        document_id,
        attachment: DownloadedAttachment,
    ) -> InboundDocument:
        record = self._require(document_id)
        now = utc_now()
        record.storage_path = attachment.storage_path
        record.file_size = attachment.file_size
        record.downloaded_at = now
        record.status = DocumentStatus.DOWNLOADED
        record.failure_stage = None
        record.last_error = None
        record.updated_at = now
        self.session.flush()
        return _document_from_record(record)

    def mark_ocr_processing(
        self,
        document_id,
        extractor_name: str,
    ) -> InboundDocument:
        record = self._require(document_id)
        if record.status == DocumentStatus.COMPLETED:
            return _document_from_record(record)
        now = utc_now()
        record.status = DocumentStatus.OCR_PROCESSING
        record.extractor_name = extractor_name
        record.ocr_attempts += 1
        record.ocr_started_at = record.ocr_started_at or now
        record.failure_stage = None
        record.last_error = None
        record.updated_at = now
        self.session.flush()
        return _document_from_record(record)

    def mark_completed(self, document_id, completed_at: datetime) -> None:
        record = self._require(document_id)
        record.status = DocumentStatus.COMPLETED
        record.completed_at = completed_at
        record.failure_stage = None
        record.last_error = None
        record.updated_at = completed_at

    def mark_failed(
        self,
        document_id,
        stage: FailureStage,
        error: str,
    ) -> None:
        record = self._require(document_id)
        record.status = DocumentStatus.FAILED
        record.failure_stage = stage
        record.last_error = error[:2000]
        record.updated_at = utc_now()

    def _require(self, document_id) -> DocumentRecord:
        record = self.session.get(DocumentRecord, document_id)
        if record is None:
            raise LookupError(f"Invoice submission {document_id} does not exist")
        return record


class SqlAlchemyInvoiceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, document_id) -> StoredInvoice | None:
        record = self.session.get(InvoiceRecord, document_id)
        return _invoice_from_record(record) if record else None

    def add(self, invoice: StoredInvoice) -> None:
        if self.session.get(InvoiceRecord, invoice.document_id) is not None:
            return
        values = invoice.invoice
        self.session.add(
            InvoiceRecord(
                document_id=invoice.document_id,
                fecha=values.fecha,
                numero_factura=values.numero_factura,
                nif_proveedor=values.nif_proveedor,
                nombre_proveedor=values.nombre_proveedor,
                base_imponible=values.base_imponible,
                tipo_iva=values.tipo_iva,
                cuota_iva=values.cuota_iva,
                total=values.total,
                extracted_at=invoice.extracted_at,
            )
        )


def _phone_from_record(record: CustomerPhoneRecord) -> CustomerPhoneNumber:
    return CustomerPhoneNumber(
        phone_number=record.phone_number,
        customer_id=record.customer_id,
        meta_user_id=record.meta_user_id,
        profile_name=record.profile_name,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
    )


def _document_from_record(record: DocumentRecord) -> InboundDocument:
    return InboundDocument(
        id=record.id,
        whatsapp_message_id=record.whatsapp_message_id,
        whatsapp_media_id=record.whatsapp_media_id,
        sender_phone_number=record.sender_phone_number,
        message_type=record.message_type,
        mime_type=record.mime_type,
        sha256=record.sha256,
        original_filename=record.original_filename,
        storage_path=record.storage_path,
        file_size=record.file_size,
        received_at=record.received_at,
        downloaded_at=record.downloaded_at,
        ocr_started_at=record.ocr_started_at,
        completed_at=record.completed_at,
        status=record.status,
        extractor_name=record.extractor_name,
        download_attempts=record.download_attempts,
        ocr_attempts=record.ocr_attempts,
        failure_stage=record.failure_stage,
        last_error=record.last_error,
    )


def _invoice_from_record(record: InvoiceRecord) -> StoredInvoice:
    from src.ocr.models import Invoice

    return StoredInvoice(
        document_id=record.document_id,
        invoice=Invoice(
            fecha=record.fecha,
            numero_factura=record.numero_factura,
            nif_proveedor=record.nif_proveedor,
            nombre_proveedor=record.nombre_proveedor,
            base_imponible=record.base_imponible,
            tipo_iva=record.tipo_iva,
            cuota_iva=record.cuota_iva,
            total=record.total,
        ),
        extracted_at=record.extracted_at,
    )
