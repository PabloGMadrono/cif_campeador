"""Tests for the initial WhatsApp webhook handshake and event receiver."""

import asyncio

import httpx

import main


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_webhook_verification_returns_challenge(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_TOKEN", "test-token")

    response = asyncio.run(
        request(
            "GET",
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-token",
                "hub.challenge": "challenge-value",
            },
        )
    )

    assert response.status_code == 200
    assert response.text == "challenge-value"
    assert response.headers["content-type"].startswith("text/plain")


def test_webhook_verification_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_TOKEN", "test-token")

    response = asyncio.run(
        request(
            "GET",
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "challenge-value",
            },
        )
    )

    assert response.status_code == 403


def test_webhook_event_is_acknowledged(capsys):
    payload = {"object": "whatsapp_business_account", "entry": []}

    response = asyncio.run(request("POST", "/webhook", json=payload))

    assert response.status_code == 200
    output = capsys.readouterr().out
    assert "WEBHOOK RECEIVED:" in output
    assert "whatsapp_business_account" in output


def test_image_event_is_passed_to_background_downloader(monkeypatch):
    captured_images = []

    async def capture_download(images, settings):
        captured_images.extend(images)

    monkeypatch.setattr(main, "download_images", capture_download)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "type": "image",
                                    "image": {
                                        "id": "media-id",
                                        "mime_type": "image/jpeg",
                                        "url": "https://lookaside.fbsbx.com/media",
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }

    response = asyncio.run(request("POST", "/webhook", json=payload))

    assert response.status_code == 200
    assert len(captured_images) == 1
    assert captured_images[0].media_id == "media-id"
