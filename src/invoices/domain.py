"""Shared invoice statuses, transfer values, and phone normalization."""

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
