"""Database operations using the caller's session and transaction.

Functions return SQLAlchemy records directly. Invoice reads materialize the
shared OCR result while the session is open, including its related tax lines.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.invoices.domain import DocumentStatus, FailureStage, StoredInvoice, utc_now
from src.jobs.contracts import DownloadJob
from src.ocr.models import EquivalenceSurcharge, Invoice, IrpfWithholding, IvaLine

from .models import (
    CustomerPhoneRecord,
    CustomerRecord,
    DocumentRecord,
    InvoiceEquivalenceSurchargeRecord,
    InvoiceIvaLineRecord,
    InvoiceRecord,
)


def resolve_sender(session: Session, job: DownloadJob) -> CustomerPhoneRecord:
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


def add_document(session: Session, job: DownloadJob) -> DocumentRecord:
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


def require_document(session: Session, document_id: UUID) -> DocumentRecord:
    document = session.get(DocumentRecord, document_id)
    if document is None:
        raise LookupError(f"Invoice submission {document_id} does not exist")
    return document


def save_invoice(
    session: Session,
    document_id: UUID,
    invoice: Invoice,
    extracted_at: datetime,
) -> None:
    """Store the invoice and its tax lines once; the caller commits them."""
    if session.get(InvoiceRecord, document_id) is not None:
        return
    withholding = invoice.retencion_irpf
    record = InvoiceRecord(
        document_id=document_id,
        validity=invoice.validity,
        diagnostic_type=invoice.diagnostic_type,
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


def get_invoice(session: Session, document_id: UUID) -> StoredInvoice | None:
    """Read the structured invoice before closing its database session."""
    record = session.get(InvoiceRecord, document_id)
    if record is None:
        return None
    if record.validity is None:
        raise RuntimeError(
            f"Invoice {record.document_id} predates validity classification; reprocess it"
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
        extracted_at=record.extracted_at,
    )


def mark_failed(
    session: Session,
    document_id: UUID,
    error: Exception,
    stage: FailureStage,
) -> None:
    """Record a permanent stage failure in the caller's transaction."""
    document = require_document(session, document_id)
    document.status = DocumentStatus.FAILED
    document.failure_stage = stage
    document.last_error = str(error)[:2000]
    document.updated_at = utc_now()
