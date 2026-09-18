"""Translate Meta webhook payloads into queue-safe download jobs."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.invoices.domain import MessageType
from src.jobs.contracts import DownloadJob

logger = logging.getLogger(__name__)


def extract_download_jobs(payload: Mapping[str, Any]) -> list[DownloadJob]:
    jobs: list[DownloadJob] = []
    for entry in _mapping_items(payload.get("entry")):
        for change in _mapping_items(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue
            contacts = _contacts_by_phone(value.get("contacts"))
            for message in _mapping_items(value.get("messages")):
                job = _job_from_message(message, contacts)
                if job is not None:
                    jobs.append(job)
    return jobs


def _job_from_message(
    message: Mapping[str, Any],
    contacts: Mapping[str, Mapping[str, Any]],
) -> DownloadJob | None:
    raw_type = message.get("type")
    if raw_type not in {MessageType.IMAGE.value, MessageType.DOCUMENT.value}:
        return None
    message_type = MessageType(raw_type)
    attachment = message.get(raw_type)
    if not isinstance(attachment, Mapping):
        return None

    try:
        mime_type = _required_string(attachment.get("mime_type"))
        if message_type is MessageType.DOCUMENT and mime_type != "application/pdf":
            return None
        if message_type is MessageType.IMAGE and not mime_type.startswith("image/"):
            return None

        phone_number = _required_string(message.get("from"))
        contact = contacts.get(phone_number, {})
        profile = contact.get("profile")
        profile_name = (
            _optional_string(profile.get("name"))
            if isinstance(profile, Mapping)
            else None
        )
        meta_user_id = _optional_string(
            message.get("from_user_id")
        ) or _optional_string(contact.get("user_id"))

        return DownloadJob.create(
            whatsapp_message_id=_required_string(message.get("id")),
            whatsapp_media_id=_required_string(attachment.get("id")),
            message_type=message_type,
            mime_type=mime_type,
            phone_number=phone_number,
            received_at=_parse_timestamp(message.get("timestamp")),
            meta_user_id=meta_user_id,
            profile_name=profile_name,
            sha256=_optional_string(attachment.get("sha256")),
            original_filename=_optional_string(attachment.get("filename")),
        )
    except (TypeError, ValueError) as error:
        logger.warning("Ignoring malformed WhatsApp attachment event: %s", error)
        return None


def _contacts_by_phone(value: Any) -> dict[str, Mapping[str, Any]]:
    contacts: dict[str, Mapping[str, Any]] = {}
    for contact in _mapping_items(value):
        wa_id = contact.get("wa_id")
        if isinstance(wa_id, str):
            contacts[wa_id] = contact
    return contacts


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, (str, int)):
        raise TypeError("WhatsApp message timestamp must be a string or integer")
    return datetime.fromtimestamp(int(value), tz=UTC)


def _required_string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Required WhatsApp message field is missing")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping_items(value: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, Mapping))
