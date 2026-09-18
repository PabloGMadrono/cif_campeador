"""Database-independent domain contracts for invoice ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from src.ocr.models import Invoice

PHONE_DIGITS = re.compile(r"^[1-9][0-9]{7,14}$")


class DocumentStatus(StrEnum):
    RECEIVED = "received"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    OCR_PROCESSING = "ocr_processing"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageType(StrEnum):
    IMAGE = "image"
    DOCUMENT = "document"


class FailureStage(StrEnum):
    DOWNLOAD = "download"
    OCR = "ocr"


@dataclass(frozen=True, slots=True)
class Customer:
    id: UUID
    display_name: str
    legal_name: str | None
    tax_id: str | None
    address: str | None
    email: str | None
    is_provisional: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerPhoneNumber:
    phone_number: str
    customer_id: UUID
    meta_user_id: str | None
    profile_name: str | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class InboundDocument:
    id: UUID
    whatsapp_message_id: str
    whatsapp_media_id: str
    sender_phone_number: str
    message_type: MessageType
    mime_type: str
    sha256: str | None
    original_filename: str | None
    storage_path: str | None
    file_size: int | None
    received_at: datetime
    downloaded_at: datetime | None
    ocr_started_at: datetime | None
    completed_at: datetime | None
    status: DocumentStatus
    extractor_name: str | None
    download_attempts: int
    ocr_attempts: int
    failure_stage: FailureStage | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StoredInvoice:
    document_id: UUID
    invoice: Invoice
    extracted_at: datetime


@dataclass(frozen=True, slots=True)
class DownloadedAttachment:
    absolute_path: Path
    storage_path: str
    file_size: int


def normalize_phone_number(value: str) -> str:
    """Return the canonical E.164-like form used as the phone primary key."""
    digits = value.strip().removeprefix("+").replace(" ", "")
    if not PHONE_DIGITS.fullmatch(digits):
        raise ValueError(f"Invalid international phone number: {value!r}")
    return f"+{digits}"


def utc_now() -> datetime:
    return datetime.now(UTC)
