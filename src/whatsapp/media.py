"""Extract and download image attachments from WhatsApp webhook events."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEDIA_DIRECTORY = Path("tests/whatsapp_images")
DEFAULT_ALLOWED_MEDIA_HOSTS = ("lookaside.fbsbx.com",)
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
SAFE_FILENAME_CHARACTER = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class WhatsAppMediaSettings:
    """Runtime settings for authenticated WhatsApp media downloads."""

    access_token: str | None
    media_directory: Path
    graph_api_version: str
    allowed_media_hosts: tuple[str, ...]
    request_timeout_seconds: float
    max_image_bytes: int

    @classmethod
    def from_environment(cls) -> WhatsAppMediaSettings:
        """Build settings from environment variables, resolving relative paths."""
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
        max_image_bytes = int(
            os.getenv("WHATSAPP_MEDIA_MAX_IMAGE_BYTES", str(10 * 1024 * 1024))
        )
        if timeout <= 0:
            raise ValueError("WHATSAPP_MEDIA_TIMEOUT_SECONDS must be positive")
        if max_image_bytes <= 0:
            raise ValueError("WHATSAPP_MEDIA_MAX_IMAGE_BYTES must be positive")

        return cls(
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
            media_directory=configured_directory.resolve(),
            graph_api_version=os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0"),
            allowed_media_hosts=hosts,
            request_timeout_seconds=timeout,
            max_image_bytes=max_image_bytes,
        )


@dataclass(frozen=True, slots=True)
class WhatsAppImage:
    """The fields required to retrieve one inbound WhatsApp image."""

    media_id: str
    mime_type: str
    sha256: str | None = None
    download_url: str | None = None


def extract_images(payload: Mapping[str, Any]) -> list[WhatsAppImage]:
    """Return image messages from a WhatsApp webhook payload.

    Unknown event shapes and non-image messages are intentionally ignored so
    delivery receipts, text messages, and future event types remain harmless.
    """
    images: list[WhatsAppImage] = []

    for entry in _mapping_items(payload.get("entry")):
        for change in _mapping_items(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue

            for message in _mapping_items(value.get("messages")):
                if message.get("type") != "image":
                    continue

                image = message.get("image")
                if not isinstance(image, Mapping):
                    continue

                media_id = image.get("id")
                mime_type = image.get("mime_type")
                if not isinstance(media_id, str) or not isinstance(mime_type, str):
                    logger.warning("Ignoring image event without an id or MIME type")
                    continue

                images.append(
                    WhatsAppImage(
                        media_id=media_id,
                        mime_type=mime_type.lower(),
                        sha256=_optional_string(image.get("sha256")),
                        download_url=_optional_string(image.get("url")),
                    )
                )

    return images


class WhatsAppMediaDownloader:
    """Download authenticated image attachments from Meta."""

    def __init__(
        self,
        settings: WhatsAppMediaSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client

    async def download(self, image: WhatsAppImage) -> Path:
        """Download one image, verify it, and atomically place it on disk."""
        if not self.settings.access_token:
            raise RuntimeError("WHATSAPP_ACCESS_TOKEN is required to download images")

        extension = IMAGE_EXTENSIONS.get(image.mime_type)
        if extension is None:
            raise ValueError(f"Unsupported WhatsApp image MIME type: {image.mime_type}")

        download_url = image.download_url or await self._retrieve_download_url(
            image.media_id
        )
        self._validate_download_url(download_url)

        output_directory = self.settings.media_directory
        output_directory.mkdir(parents=True, exist_ok=True)
        output_path = output_directory / f"{_safe_filename(image.media_id)}{extension}"
        temporary_path = output_path.with_suffix(f"{output_path.suffix}.part")

        digest = hashlib.sha256()
        downloaded_bytes = 0
        headers = {"Authorization": f"Bearer {self.settings.access_token}"}

        try:
            async with self.client.stream(
                "GET", download_url, headers=headers
            ) as response:
                response.raise_for_status()
                response_mime_type = response.headers.get("content-type", "")
                response_mime_type = response_mime_type.partition(";")[0].lower()
                if response_mime_type and response_mime_type != image.mime_type:
                    raise ValueError(
                        "Downloaded MIME type does not match the webhook: "
                        f"{response_mime_type!r} != {image.mime_type!r}"
                    )

                with temporary_path.open("wb") as output_file:
                    async for chunk in response.aiter_bytes():
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > self.settings.max_image_bytes:
                            raise ValueError(
                                "WhatsApp image exceeds WHATSAPP_MEDIA_MAX_IMAGE_BYTES"
                            )
                        digest.update(chunk)
                        output_file.write(chunk)

            self._verify_digest(image.sha256, digest.digest())
            temporary_path.replace(output_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        return output_path

    async def _retrieve_download_url(self, media_id: str) -> str:
        """Resolve a fresh temporary media URL through the Graph API."""
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
        """Prevent sending the bearer token to an untrusted download host."""
        parsed_url = urlparse(url)
        hostname = (parsed_url.hostname or "").lower()
        if parsed_url.scheme != "https" or hostname not in self.settings.allowed_media_hosts:
            raise ValueError(f"Refusing untrusted WhatsApp media URL: {url!r}")

    @staticmethod
    def _verify_digest(expected_sha256: str | None, digest: bytes) -> None:
        if not expected_sha256:
            return

        try:
            expected_digest = base64.b64decode(expected_sha256, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Webhook contained an invalid base64 SHA-256 digest") from error

        if not hmac.compare_digest(expected_digest, digest):
            raise ValueError("Downloaded image failed SHA-256 verification")


async def download_images(
    images: Iterable[WhatsAppImage],
    settings: WhatsAppMediaSettings,
) -> None:
    """Download images independently so one failure does not block the others."""
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=False,
    ) as client:
        downloader = WhatsAppMediaDownloader(settings, client)
        for image in images:
            try:
                output_path = await downloader.download(image)
                logger.info("Saved WhatsApp image to %s", output_path)
            except Exception:
                logger.exception("Could not download WhatsApp image %s", image.media_id)


def _mapping_items(value: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, Mapping))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_filename(value: str) -> str:
    sanitized = SAFE_FILENAME_CHARACTER.sub("_", value).strip("._")
    if not sanitized:
        raise ValueError("WhatsApp media id cannot produce an empty filename")
    return sanitized[:120]
