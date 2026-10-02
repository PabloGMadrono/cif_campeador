"""Tests for authenticated WhatsApp attachment downloads."""

import asyncio
import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.invoices.domain import MessageType
from src.whatsapp.media import (
    WhatsAppAttachment,
    WhatsAppMediaDownloader,
    WhatsAppMediaSettings,
)

RECEIVED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> WhatsAppMediaSettings:
    return WhatsAppMediaSettings(
        access_token="test-access-token",
        media_directory=tmp_path,
        graph_api_version="v23.0",
        allowed_media_hosts=("lookaside.fbsbx.com",),
        request_timeout_seconds=5,
        max_media_bytes=1024,
    )


def test_download_validates_authenticated_image_in_caller_directory(tmp_path):
    image_bytes = b"a small jpeg fixture"
    encoded_digest = base64.b64encode(hashlib.sha256(image_bytes).digest()).decode()

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://lookaside.fbsbx.com/media"
        assert request.headers["authorization"] == "Bearer test-access-token"
        return httpx.Response(
            200,
            content=image_bytes,
            headers={"content-type": "image/jpeg"},
        )

    async def download():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            return await downloader.download(
                WhatsAppAttachment(
                    media_id="1698584094538284",
                    message_type=MessageType.IMAGE,
                    mime_type="image/jpeg",
                    received_at=RECEIVED_AT,
                    sha256=encoded_digest,
                    download_url="https://lookaside.fbsbx.com/media",
                ),
                tmp_path,
            )

    result = asyncio.run(download())

    assert result.absolute_path == tmp_path / "original.jpg"
    assert result.content_sha256 == hashlib.sha256(image_bytes).hexdigest()
    assert result.file_size == len(image_bytes)
    assert result.absolute_path.read_bytes() == image_bytes


def test_download_resolves_url_and_accepts_pdf(tmp_path):
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={"url": "https://lookaside.fbsbx.com/resolved-media"},
            )
        return httpx.Response(
            200,
            content=b"%PDF fixture",
            headers={"content-type": "application/pdf"},
        )

    async def download():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            return await downloader.download(
                WhatsAppAttachment(
                    media_id="pdf-media-id",
                    message_type=MessageType.DOCUMENT,
                    mime_type="application/pdf",
                    received_at=RECEIVED_AT,
                ),
                tmp_path,
            )

    result = asyncio.run(download())

    assert result.absolute_path.name == "original.pdf"
    assert [str(request.url) for request in requests] == [
        "https://graph.facebook.com/v23.0/pdf-media-id",
        "https://lookaside.fbsbx.com/resolved-media",
    ]


def test_download_rejects_untrusted_url_without_sending_token(tmp_path):
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request to {request.url}")

    async def download() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_if_called)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            await downloader.download(
                WhatsAppAttachment(
                    media_id="media-id",
                    message_type=MessageType.IMAGE,
                    mime_type="image/jpeg",
                    received_at=RECEIVED_AT,
                    download_url="https://example.com/steal-token",
                ),
                tmp_path,
            )

    with pytest.raises(ValueError, match="untrusted WhatsApp media URL"):
        asyncio.run(download())


@pytest.mark.parametrize("mime_type,message_type,content,response_mime,digest,error", [
    ("text/plain", MessageType.IMAGE, b"bad", "text/plain", None, "Unsupported"),
    ("image/jpeg", MessageType.DOCUMENT, b"bad", "image/jpeg", None, "Only PDF"),
    ("image/jpeg", MessageType.IMAGE, b"bad", "image/png", None, "does not match"),
    ("image/jpeg", MessageType.IMAGE, b"x" * 1025, "image/jpeg", None, "exceeds"),
    ("image/jpeg", MessageType.IMAGE, b"bad", "image/jpeg", "not-base64", "invalid base64"),
    ("image/jpeg", MessageType.IMAGE, b"bad", "image/jpeg", base64.b64encode(b"x" * 32).decode(), "verification"),
])
def test_rejected_download_does_not_publish_a_file(tmp_path, mime_type, message_type, content, response_mime, digest, error):
    async def download():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=content, headers={"content-type": response_mime}))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            attachment = WhatsAppAttachment(
                "media-id", message_type, mime_type, RECEIVED_AT,
                sha256=digest, download_url="https://lookaside.fbsbx.com/media",
            )
            await downloader.download(attachment, tmp_path)

    with pytest.raises(ValueError, match=error):
        asyncio.run(download())
    assert list(tmp_path.iterdir()) == []


def test_missing_meta_credentials_fail_before_request(tmp_path):
    async def download():
        transport = httpx.MockTransport(lambda _: pytest.fail("Unexpected request"))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = WhatsAppMediaDownloader(replace(_settings(tmp_path), access_token=None), client)
            await downloader.download(WhatsAppAttachment("id", MessageType.IMAGE, "image/jpeg", RECEIVED_AT), tmp_path)

    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        asyncio.run(download())
