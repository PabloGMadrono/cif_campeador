"""Tests for filtering and downloading inbound WhatsApp images."""

import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from src.whatsapp.media import (
    WhatsAppImage,
    WhatsAppMediaDownloader,
    WhatsAppMediaSettings,
    extract_images,
)


def _settings(tmp_path: Path) -> WhatsAppMediaSettings:
    return WhatsAppMediaSettings(
        access_token="test-access-token",
        media_directory=tmp_path,
        graph_api_version="v23.0",
        allowed_media_hosts=("lookaside.fbsbx.com",),
        request_timeout_seconds=5,
        max_image_bytes=1024,
    )


def _payload(*messages: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messages": list(messages)},
                    }
                ]
            }
        ],
    }


def test_extract_images_ignores_non_image_messages():
    payload = _payload(
        {"id": "text-message", "type": "text", "text": {"body": "Hello"}},
        {
            "id": "image-message",
            "type": "image",
            "image": {
                "id": "1698584094538284",
                "mime_type": "image/jpeg",
                "sha256": "digest",
                "url": "https://lookaside.fbsbx.com/media",
            },
        },
    )

    assert extract_images(payload) == [
        WhatsAppImage(
            media_id="1698584094538284",
            mime_type="image/jpeg",
            sha256="digest",
            download_url="https://lookaside.fbsbx.com/media",
        )
    ]


def test_download_saves_authenticated_image_and_verifies_digest(tmp_path):
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

    async def download() -> Path:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            return await downloader.download(
                WhatsAppImage(
                    media_id="1698584094538284",
                    mime_type="image/jpeg",
                    sha256=encoded_digest,
                    download_url="https://lookaside.fbsbx.com/media",
                )
            )

    output_path = asyncio.run(download())

    assert output_path == tmp_path / "1698584094538284.jpg"
    assert output_path.read_bytes() == image_bytes
    assert not output_path.with_suffix(".jpg.part").exists()


def test_download_resolves_url_when_webhook_does_not_include_one(tmp_path):
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
            content=b"image",
            headers={"content-type": "image/png"},
        )

    async def download() -> Path:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            return await downloader.download(
                WhatsAppImage(media_id="media-id", mime_type="image/png")
            )

    output_path = asyncio.run(download())

    assert output_path == tmp_path / "media-id.png"
    assert [str(request.url) for request in requests] == [
        "https://graph.facebook.com/v23.0/media-id",
        "https://lookaside.fbsbx.com/resolved-media",
    ]
    assert all(
        request.headers["authorization"] == "Bearer test-access-token"
        for request in requests
    )


def test_download_rejects_untrusted_url_without_sending_token(tmp_path):
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request to {request.url}")

    async def download() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_if_called)
        ) as client:
            downloader = WhatsAppMediaDownloader(_settings(tmp_path), client)
            await downloader.download(
                WhatsAppImage(
                    media_id="media-id",
                    mime_type="image/jpeg",
                    download_url="https://example.com/steal-token",
                )
            )

    with pytest.raises(ValueError, match="untrusted WhatsApp media URL"):
        asyncio.run(download())
