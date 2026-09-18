"""Queue-independent job messages and publishing interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from src.invoices.domain import MessageType, normalize_phone_number

SUBMISSION_NAMESPACE = UUID("dd2fdd25-c96c-41ef-a923-d3a2fcba1b83")


@dataclass(frozen=True, slots=True)
class DownloadJob:
    submission_id: UUID
    whatsapp_message_id: str
    whatsapp_media_id: str
    message_type: MessageType
    mime_type: str
    phone_number: str
    received_at: datetime
    meta_user_id: str | None = None
    profile_name: str | None = None
    sha256: str | None = None
    original_filename: str | None = None
    attempt: int = 1

    @classmethod
    def create(
        cls,
        *,
        whatsapp_message_id: str,
        whatsapp_media_id: str,
        message_type: MessageType,
        mime_type: str,
        phone_number: str,
        received_at: datetime,
        meta_user_id: str | None = None,
        profile_name: str | None = None,
        sha256: str | None = None,
        original_filename: str | None = None,
    ) -> DownloadJob:
        return cls(
            submission_id=uuid5(SUBMISSION_NAMESPACE, whatsapp_message_id),
            whatsapp_message_id=whatsapp_message_id,
            whatsapp_media_id=whatsapp_media_id,
            message_type=message_type,
            mime_type=mime_type.lower(),
            phone_number=normalize_phone_number(phone_number),
            received_at=received_at,
            meta_user_id=meta_user_id,
            profile_name=profile_name,
            sha256=sha256,
            original_filename=original_filename,
        )

    def with_attempt(self, attempt: int) -> DownloadJob:
        values = self.to_dict()
        values["attempt"] = attempt
        return self.from_dict(values)

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["submission_id"] = str(self.submission_id)
        values["message_type"] = self.message_type.value
        values["received_at"] = self.received_at.isoformat()
        return values

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> DownloadJob:
        return cls(
            submission_id=UUID(values["submission_id"]),
            whatsapp_message_id=values["whatsapp_message_id"],
            whatsapp_media_id=values["whatsapp_media_id"],
            message_type=MessageType(values["message_type"]),
            mime_type=values["mime_type"],
            phone_number=values["phone_number"],
            received_at=datetime.fromisoformat(values["received_at"]),
            meta_user_id=values.get("meta_user_id"),
            profile_name=values.get("profile_name"),
            sha256=values.get("sha256"),
            original_filename=values.get("original_filename"),
            attempt=int(values.get("attempt", 1)),
        )


@dataclass(frozen=True, slots=True)
class OcrJob:
    submission_id: UUID
    attempt: int = 1

    def with_attempt(self, attempt: int) -> OcrJob:
        return OcrJob(submission_id=self.submission_id, attempt=attempt)

    def to_dict(self) -> dict[str, Any]:
        return {"submission_id": str(self.submission_id), "attempt": self.attempt}

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> OcrJob:
        return cls(
            submission_id=UUID(values["submission_id"]),
            attempt=int(values.get("attempt", 1)),
        )


class InvoiceJobQueue(Protocol):
    async def enqueue_download(self, job: DownloadJob) -> str: ...

    async def enqueue_ocr(self, job: OcrJob) -> str: ...
