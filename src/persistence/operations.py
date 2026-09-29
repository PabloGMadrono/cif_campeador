"""Durable state transitions for the invoice ingestion pipeline."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.invoices.domain import (
    DocumentStatus,
    DownloadedAttachment,
    FailureStage,
    FiscalStatus,
    StoredInvoice,
    utc_now,
)
from src.jobs.contracts import DownloadJob
from src.ocr.models import EquivalenceSurcharge, Invoice, IrpfWithholding, IvaLine

from .database import Database
from .models import (
    CustomerPhoneRecord,
    CustomerRecord,
    DocumentRecord,
    InvoiceEquivalenceSurchargeRecord,
    InvoiceIvaLineRecord,
    InvoiceRecord,
)


class InvoiceSubmissionLifecycle:
    """Persist invoice submissions and their processing transitions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def prepare_download(self, job: DownloadJob) -> DocumentRecord:
        with self.database.session_factory.begin() as session:
            _resolve_sender(session, job)
            document = _add_document(session, job)
            if document.status in {
                DocumentStatus.RECEIVED,
                DocumentStatus.DOWNLOADING,
            }:
                document.status = DocumentStatus.DOWNLOADING
                document.download_attempts += 1
                _clear_failure(document)
                document.updated_at = utc_now()
            return document

    def record_download(
        self,
        document_id: UUID,
        attachment: DownloadedAttachment,
    ) -> DocumentRecord:
        with self.database.session_factory.begin() as session:
            document = _require_document(session, document_id)
            now = utc_now()
            document.storage_path = attachment.storage_path
            document.file_size = attachment.file_size
            document.downloaded_at = now
            document.status = DocumentStatus.DOWNLOADED
            _clear_failure(document)
            document.updated_at = now
            return document

    def begin_ocr_attempt(
        self,
        document_id: UUID,
        extractor_name: str,
    ) -> DocumentRecord:
        with self.database.session_factory.begin() as session:
            return _begin_ocr_attempt(session, document_id, extractor_name)

    def save_invoice(
        self,
        document_id: UUID,
        invoice: Invoice,
        fiscal_status: FiscalStatus,
        extracted_at: datetime,
    ) -> DocumentStatus:
        with self.database.session_factory.begin() as session:
            return _save_invoice(
                session,
                document_id,
                invoice,
                fiscal_status,
                extracted_at,
            )

    def get_invoice(self, document_id: UUID) -> StoredInvoice | None:
        with self.database.session_factory() as session:
            return _get_invoice(session, document_id)

    def mark_failed(
        self,
        document_id: UUID,
        error: Exception,
        stage: FailureStage,
    ) -> None:
        with self.database.session_factory.begin() as session:
            _mark_failed(session, document_id, error, stage)


def _resolve_sender(session: Session, job: DownloadJob) -> CustomerPhoneRecord:
    """Reuse the phone's customer, or a unique customer for its Meta user ID."""
    phone_number = job.phone_number
    meta_user_id = job.meta_user_id
    profile_name = job.profile_name
    seen_at = job.received_at
    phone = session.get(CustomerPhoneRecord, phone_number)
    if phone is not None:
        last_seen = phone.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        phone.last_seen_at = max(last_seen, seen_at)
        if meta_user_id:
            phone.meta_user_id = meta_user_id
        if profile_name:
            phone.profile_name = profile_name
        return phone

    customer_id = None
    if meta_user_id:
        customer_ids = session.scalars(
            select(CustomerPhoneRecord.customer_id)
            .where(CustomerPhoneRecord.meta_user_id == meta_user_id)
            .distinct()
        ).all()
        if len(customer_ids) == 1:
            customer_id = customer_ids[0]
    if customer_id is None:
        now = utc_now()
        customer_id = uuid4()
        session.add(
            CustomerRecord(
                id=customer_id,
                display_name=profile_name or phone_number,
                is_provisional=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

    phone = CustomerPhoneRecord(
        phone_number=phone_number,
        customer_id=customer_id,
        meta_user_id=meta_user_id,
        profile_name=profile_name,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    session.add(phone)
    session.flush()
    return phone


def _add_document(session: Session, job: DownloadJob) -> DocumentRecord:
    """Create a submission once, keyed by its WhatsApp message ID."""
    document = session.scalar(
        select(DocumentRecord).where(
            DocumentRecord.whatsapp_message_id == job.whatsapp_message_id
        )
    )
    if document is None:
        now = utc_now()
        document = DocumentRecord(
            id=job.submission_id,
            whatsapp_message_id=job.whatsapp_message_id,
            whatsapp_media_id=job.whatsapp_media_id,
            sender_phone_number=job.phone_number,
            message_type=job.message_type,
            mime_type=job.mime_type,
            sha256=job.sha256,
            original_filename=job.original_filename,
            received_at=job.received_at,
            status=DocumentStatus.RECEIVED,
            created_at=now,
            updated_at=now,
        )
        session.add(document)
        session.flush()
    return document


def _require_document(session: Session, document_id: UUID) -> DocumentRecord:
    document = session.get(DocumentRecord, document_id)
    if document is None:
        raise LookupError(f"Invoice submission {document_id} does not exist")
    return document


def _begin_ocr_attempt(
    session: Session,
    document_id: UUID,
    extractor_name: str,
) -> DocumentRecord:
    """Mark an eligible submission as being processed by OCR."""
    document = _require_document(session, document_id)
    if document.status in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}:
        return document
    if document.status not in {
        DocumentStatus.DOWNLOADED,
        DocumentStatus.OCR_PROCESSING,
    }:
        raise RuntimeError(f"Invoice submission {document_id} is not ready for OCR")

    now = utc_now()
    document.status = DocumentStatus.OCR_PROCESSING
    document.extractor_name = extractor_name
    document.ocr_attempts += 1
    document.ocr_started_at = document.ocr_started_at or now
    _clear_failure(document)
    document.updated_at = now
    return document


def _save_invoice(
    session: Session,
    document_id: UUID,
    invoice: Invoice,
    fiscal_status: FiscalStatus,
    extracted_at: datetime,
) -> DocumentStatus:
    """Store an invoice and mark its submission completed in one transaction."""
    if session.get(InvoiceRecord, document_id) is None:
        withholding = invoice.retencion_irpf
        record = InvoiceRecord(
            document_id=document_id,
            validity=invoice.validity,
            diagnostic_type=invoice.diagnostic_type,
            fiscal_status=fiscal_status,
            fecha=invoice.fecha,
            numero_factura=invoice.numero_factura,
            nif_proveedor=invoice.nif_proveedor,
            nombre_proveedor=invoice.nombre_proveedor,
            base_retencion=withholding.base_retencion if withholding else None,
            tipo_irpf=withholding.tipo_irpf if withholding else None,
            cuota_irpf=withholding.cuota_irpf if withholding else None,
            total=invoice.total,
            extracted_at=extracted_at,
        )
        record.iva_lines = [
            InvoiceIvaLineRecord(
                position=position,
                base_imponible=line.base_imponible,
                tipo_iva=line.tipo_iva,
                cuota_iva=line.cuota_iva,
            )
            for position, line in enumerate(invoice.lineas_iva)
        ]
        record.equivalence_surcharges = [
            InvoiceEquivalenceSurchargeRecord(
                position=position,
                base_imponible=line.base_imponible,
                tipo_re=line.tipo_re,
                cuota_re=line.cuota_re,
            )
            for position, line in enumerate(invoice.recargos_equivalencia)
        ]
        session.add(record)

    document = _require_document(session, document_id)
    document.status = DocumentStatus.COMPLETED
    document.completed_at = extracted_at
    _clear_failure(document)
    document.updated_at = extracted_at

    return DocumentStatus.COMPLETED


def _get_invoice(session: Session, document_id: UUID) -> StoredInvoice | None:
    """Read the structured invoice before closing its database session."""
    record = session.get(InvoiceRecord, document_id)
    if record is None:
        return None
    if record.validity is None:
        raise RuntimeError(
            f"Invoice {record.document_id} predates validity classification; reprocess it"
        )
    if record.fiscal_status is None:
        raise RuntimeError(
            f"Invoice {record.document_id} predates fiscal reconciliation; reprocess it"
        )

    withholding = None
    if any((record.base_retencion, record.tipo_irpf, record.cuota_irpf)):
        withholding = IrpfWithholding(
            base_retencion=record.base_retencion,
            tipo_irpf=record.tipo_irpf,
            cuota_irpf=record.cuota_irpf,
        )
    return StoredInvoice(
        document_id=record.document_id,
        invoice=Invoice(
            validity=record.validity,
            diagnostic_type=record.diagnostic_type,
            fecha=record.fecha,
            numero_factura=record.numero_factura,
            nif_proveedor=record.nif_proveedor,
            nombre_proveedor=record.nombre_proveedor,
            lineas_iva=tuple(
                IvaLine(line.base_imponible, line.tipo_iva, line.cuota_iva)
                for line in record.iva_lines
            ),
            recargos_equivalencia=tuple(
                EquivalenceSurcharge(line.base_imponible, line.tipo_re, line.cuota_re)
                for line in record.equivalence_surcharges
            ),
            retencion_irpf=withholding,
            total=record.total,
        ),
        fiscal_status=record.fiscal_status,
        extracted_at=record.extracted_at,
    )


def _mark_failed(
    session: Session,
    document_id: UUID,
    error: Exception,
    stage: FailureStage,
) -> None:
    """Record a permanent stage failure in the caller's transaction."""
    document = _require_document(session, document_id)
    document.status = DocumentStatus.FAILED
    document.failure_stage = stage
    document.last_error = str(error)[:2000]
    document.updated_at = utc_now()


def _clear_failure(document: DocumentRecord) -> None:
    document.failure_stage = None
    document.last_error = None
