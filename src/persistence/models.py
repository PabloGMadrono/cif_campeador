"""Private SQLAlchemy mappings for the invoice ingestion database."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.invoices.domain import DocumentStatus, FailureStage, MessageType
from src.ocr.models import InvoiceValidity

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CustomerRecord(Base):
    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    legal_name: Mapped[str | None] = mapped_column(String(255))
    tax_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    address: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CustomerPhoneRecord(Base):
    __tablename__ = "customer_phone_numbers"

    phone_number: Mapped[str] = mapped_column(String(16), primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        index=True,
    )
    meta_user_id: Mapped[str | None] = mapped_column(String(255), index=True)
    profile_name: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentRecord(Base):
    __tablename__ = "invoice_submissions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    whatsapp_message_id: Mapped[str] = mapped_column(String(255), unique=True)
    whatsapp_media_id: Mapped[str] = mapped_column(String(255), index=True)
    sender_phone_number: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("customer_phone_numbers.phone_number", ondelete="RESTRICT"),
        index=True,
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(
            MessageType,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    mime_type: Mapped[str] = mapped_column(String(127))
    sha256: Mapped[str | None] = mapped_column(String(128))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    storage_path: Mapped[str | None] = mapped_column(String(1024), unique=True)
    file_size: Mapped[int | None] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ocr_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            native_enum=False,
            length=32,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        index=True,
    )
    extractor_name: Mapped[str | None] = mapped_column(String(255))
    download_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ocr_attempts: Mapped[int] = mapped_column(Integer, default=0)
    failure_stage: Mapped[FailureStage | None] = mapped_column(
        Enum(
            FailureStage,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    last_error: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_invoice_submissions_status_received", "status", "received_at"),
    )


class InvoiceRecord(Base):
    __tablename__ = "invoices"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoice_submissions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    validity: Mapped[InvoiceValidity | None] = mapped_column(
        Enum(
            InvoiceValidity,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        index=True,
    )
    diagnostic_type: Mapped[str | None] = mapped_column(String(255))
    fecha: Mapped[str | None] = mapped_column(String(64))
    numero_factura: Mapped[str | None] = mapped_column(String(255))
    nif_proveedor: Mapped[str | None] = mapped_column(String(64))
    nombre_proveedor: Mapped[str | None] = mapped_column(String(255))
    base_retencion: Mapped[str | None] = mapped_column(String(64))
    tipo_irpf: Mapped[str | None] = mapped_column(String(64))
    cuota_irpf: Mapped[str | None] = mapped_column(String(64))
    total: Mapped[str | None] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    iva_lines: Mapped[list[InvoiceIvaLineRecord]] = relationship(
        cascade="all, delete-orphan",
        order_by="InvoiceIvaLineRecord.position",
    )
    equivalence_surcharges: Mapped[list[InvoiceEquivalenceSurchargeRecord]] = relationship(
        cascade="all, delete-orphan",
        order_by="InvoiceEquivalenceSurchargeRecord.position",
    )


class InvoiceIvaLineRecord(Base):
    __tablename__ = "invoice_iva_lines"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.document_id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_imponible: Mapped[str | None] = mapped_column(String(64))
    tipo_iva: Mapped[str | None] = mapped_column(String(64))
    cuota_iva: Mapped[str | None] = mapped_column(String(64))


class InvoiceEquivalenceSurchargeRecord(Base):
    __tablename__ = "invoice_equivalence_surcharges"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.document_id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_imponible: Mapped[str | None] = mapped_column(String(64))
    tipo_re: Mapped[str | None] = mapped_column(String(64))
    cuota_re: Mapped[str | None] = mapped_column(String(64))
