"""Tests for webhook verification and Redis job publication."""

import asyncio

import httpx

import main
from src.invoices.domain import MessageType


class FakeJobQueue:
    def __init__(self) -> None:
        self.download_jobs = []

    async def enqueue_download(self, job):
        self.download_jobs.append(job)
        return f"message-{len(self.download_jobs)}"

    async def enqueue_ocr(self, job):
        raise AssertionError("Webhook must not enqueue OCR directly")


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


def test_webhook_queues_images_and_pdfs_but_ignores_text():
    queue = FakeJobQueue()
    main.app.state.job_queue = queue
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [
                                {
                                    "wa_id": "34638894450",
                                    "user_id": "ES.2240891796752471",
                                    "profile": {"name": "Pablo"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "34638894450",
                                    "id": "text-message",
                                    "timestamp": "1789645881",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                },
                                {
                                    "from": "34638894450",
                                    "id": "image-message",
                                    "timestamp": "1789645881",
                                    "type": "image",
                                    "image": {
                                        "id": "image-media",
                                        "mime_type": "image/jpeg",
                                    },
                                },
                                {
                                    "from": "34638894450",
                                    "id": "pdf-message",
                                    "timestamp": "1789645882",
                                    "type": "document",
                                    "document": {
                                        "id": "pdf-media",
                                        "mime_type": "application/pdf",
                                        "filename": "invoice.pdf",
                                    },
                                },
                            ],
                        },
                    }
                ]
            }
        ],
    }

    response = asyncio.run(request("POST", "/webhook", json=payload))

    assert response.status_code == 200
    assert [job.message_type for job in queue.download_jobs] == [
        MessageType.IMAGE,
        MessageType.DOCUMENT,
    ]
    assert queue.download_jobs[0].phone_number == "+34638894450"
    assert queue.download_jobs[0].meta_user_id == "ES.2240891796752471"
    assert queue.download_jobs[1].original_filename == "invoice.pdf"


def test_webhook_rejects_non_object_json():
    main.app.state.job_queue = FakeJobQueue()
    response = asyncio.run(request("POST", "/webhook", json=[]))
    assert response.status_code == 400
