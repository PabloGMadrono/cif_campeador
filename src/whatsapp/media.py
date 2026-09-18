"""Securely download WhatsApp image and PDF attachments."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.environment import PROJECT_ROOT, load_project_environment
from src.invoices.domain import DownloadedAttachment, MessageType

DEFAULT_MEDIA_DIRECTORY = Path("tests/whatsapp_images")
DEFAULT_ALLOWED_MEDIA_HOSTS = ("lookaside.fbsbx.com",)
MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
SAFE_FILENAME_CHARACTER = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class WhatsAppMediaSettings:
    access_token: str | None
    media_directory: Path
    graph_api_version: str
    allowed_media_hosts: tuple[str, ...]
    request_timeout_seconds: float
    max_media_bytes: int

    @classmethod
    def from_environment(cls) -> WhatsAppMediaSettings:
        load_project_environment()
        configured_directory = Path(
            os.getenv("WHATSAPP_MEDIA_DIR", str(DEFAULT_MEDIA_DIRECTORY))
        ).expanduser()
        if not configured_directory.is_absolute():
            configured_directory = PROJECT_ROOT / configured_directory

        hosts = tuple(
            host.strip().lower()
            for host in os.getenv(
                "WHATSAPP_MEDIA_ALLOWED_HOSTS",
                ",".join(DEFAULT_ALLOWED_MEDIA_HOSTS),
            ).split(",")
            if host.strip()
        )
        if not hosts:
            raise ValueError("WHATSAPP_MEDIA_ALLOWED_HOSTS cannot be empty")

        timeout = float(os.getenv("WHATSAPP_MEDIA_TIMEOUT_SECONDS", "30"))
        max_media_bytes = int(
            os.getenv(
                "WHATSAPP_MEDIA_MAX_BYTES",
                os.getenv("WHATSAPP_MEDIA_MAX_IMAGE_BYTES", str(100 * 1024 * 1024)),
            )
        )
        if timeout <= 0 or max_media_bytes <= 0:
            raise ValueError("WhatsApp media timeout and size limit must be positive")

        return cls(
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
            media_directory=configured_directory.resolve(),
            graph_api_version=os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0"),
            allowed_media_hosts=hosts,
            request_timeout_seconds=timeout,
            max_media_bytes=max_media_bytes,
        )


@dataclass(frozen=True, slots=True)
class WhatsAppAttachment:
    media_id: str
    message_type: MessageType
    mime_type: str
    received_at: datetime
    sha256: str | None = None
    download_url: str | None = None


class WhatsAppMediaDownloader:
    def __init__(
        self,
        settings: WhatsAppMediaSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client

    async def download(self, attachment: WhatsAppAttachment) -> DownloadedAttachment:
        if not self.settings.access_token:
            raise RuntimeError("WHATSAPP_ACCESS_TOKEN is required to download media")

        extension = MEDIA_EXTENSIONS.get(attachment.mime_type)
        if extension is None:
            raise ValueError(f"Unsupported WhatsApp MIME type: {attachment.mime_type}")
        if attachment.message_type is MessageType.DOCUMENT:
            if attachment.mime_type != "application/pdf":
                raise ValueError("Only PDF WhatsApp documents are accepted")
        elif not attachment.mime_type.startswith("image/"):
            raise ValueError("WhatsApp image message has a non-image MIME type")

        download_url = attachment.download_url or await self._retrieve_download_url(
            attachment.media_id
        )
        self._validate_download_url(download_url)

        date_path = attachment.received_at.strftime("%Y/%m/%d")
        relative_path = Path(date_path) / f"{_safe_filename(attachment.media_id)}{extension}"
        output_path = self.settings.media_directory / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_suffix(f"{output_path.suffix}.part")

        digest = hashlib.sha256()
        downloaded_bytes = 0
        headers = {"Authorization": f"Bearer {self.settings.access_token}"}

        try:
            async with self.client.stream("GET", download_url, headers=headers) as response:
                response.raise_for_status()
                response_mime_type = response.headers.get("content-type", "")
                response_mime_type = response_mime_type.partition(";")[0].lower()
                if response_mime_type and response_mime_type != attachment.mime_type:
                    raise ValueError(
                        "Downloaded MIME type does not match the webhook: "
                        f"{response_mime_type!r} != {attachment.mime_type!r}"
                    )

                with temporary_path.open("wb") as output_file:
                    async for chunk in response.aiter_bytes():
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > self.settings.max_media_bytes:
                            raise ValueError(
                                "WhatsApp attachment exceeds WHATSAPP_MEDIA_MAX_BYTES"
                            )
                        digest.update(chunk)
                        output_file.write(chunk)

            _verify_digest(attachment.sha256, digest.digest())
            temporary_path.replace(output_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        return DownloadedAttachment(
            absolute_path=output_path,
            storage_path=relative_path.as_posix(),
            file_size=downloaded_bytes,
        )

    async def _retrieve_download_url(self, media_id: str) -> str:
        url = (
            "https://graph.facebook.com/"
            f"{self.settings.graph_api_version}/{media_id}"
        )
        headers = {"Authorization": f"Bearer {self.settings.access_token}"}
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        download_url = response.json().get("url")
        if not isinstance(download_url, str) or not download_url:
            raise ValueError("Meta media response did not contain a download URL")
        return download_url

    def _validate_download_url(self, url: str) -> None:
        parsed_url = urlparse(url)
        hostname = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or hostname not in self.settings.allowed_media_hosts:
            raise ValueError(f"Refusing untrusted WhatsApp media URL: {url!r}")


def _verify_digest(expected_sha256: str | None, digest: bytes) -> None:
    if not expected_sha256:
        return
    try:
        expected_digest = base64.b64decode(expected_sha256, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Webhook contained an invalid base64 SHA-256 digest") from error
    if not hmac.compare_digest(expected_digest, digest):
        raise ValueError("Downloaded attachment failed SHA-256 verification")


def _safe_filename(value: str) -> str:
    sanitized = SAFE_FILENAME_CHARACTER.sub("_", value).strip("._")
    if not sanitized:
        raise ValueError("WhatsApp media id cannot produce an empty filename")
    return sanitized[:120]
