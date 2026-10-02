"""Offline storage transfer, integrity, retry, and configuration checks."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from minio.error import S3Error

from src.invoices.domain import DownloadedAttachment
from src.object_storage import MinioObjectStorage, MinioSettings
from src.object_storage.minio import original_object_key
from tests.object_storage_fixtures import make_object_storage


@pytest.fixture
def downloaded(tmp_path):
    path = tmp_path / "invoice.jpg"
    path.write_bytes(b"invoice")
    return DownloadedAttachment(path, 7, hashlib.sha256(b"invoice").hexdigest())


def test_upload_reuses_valid_original_and_download_checks_bytes(downloaded, tmp_path):
    object_storage = make_object_storage()
    first = object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    second = object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    assert first == second
    assert object_storage.client.uploads == 1
    assert object_storage.client.stat_object(first.bucket, first.object_key).content_type == "image/jpeg"
    destination = tmp_path / "worker-original.jpg"
    object_storage.download(first, destination)
    assert destination.read_bytes() == b"invoice"


def test_overlapping_identical_uploads_keep_one_object(downloaded):
    object_storage = make_object_storage()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg"), range(2)))
    assert results[0] == results[1]
    assert len(object_storage.client.objects) == 1


@pytest.mark.parametrize("conflict", ["size", "checksum", "mime"])
def test_upload_rejects_conflicting_object_without_overwriting(downloaded, conflict):
    object_storage = make_object_storage()
    content = b"short" if conflict == "size" else b"invoice"
    mime_type = "image/png" if conflict == "mime" else "image/jpeg"
    digest = "0" * 64 if conflict == "checksum" else downloaded.content_sha256
    original = (content, mime_type, {"x-amz-meta-sha256": digest})
    object_storage.client.objects["invoice-originals", "originals/one.jpg"] = original
    with pytest.raises(ValueError, match="conflicts"):
        object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    assert object_storage.client.uploads == 0
    assert object_storage.client.objects["invoice-originals", "originals/one.jpg"] == original


@pytest.mark.parametrize("error_code", ["AccessDenied", "NoSuchBucket"])
def test_stat_errors_are_visible_and_do_not_trigger_upload(downloaded, error_code):
    object_storage = make_object_storage()
    object_storage.client = Mock()
    object_storage.client.stat_object.side_effect = S3Error(None, error_code, "failed", "key", "request", "host")
    with pytest.raises(S3Error) as caught:
        object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    assert caught.value.code == error_code
    object_storage.client.fput_object.assert_not_called()


@pytest.mark.parametrize("content", [b"bad", b"changed"])
def test_download_rejects_corrupted_original(downloaded, tmp_path, content):
    object_storage = make_object_storage()
    stored = object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    object_storage.client.objects[stored.bucket, stored.object_key] = (content, "image/jpeg", {})
    with pytest.raises(ValueError, match="integrity verification"):
        object_storage.download(stored, tmp_path / "retrieved.jpg")


def test_missing_original_and_unprovisioned_bucket_remain_visible(downloaded, tmp_path):
    object_storage = make_object_storage()
    stored = object_storage.upload(downloaded, "originals/one.jpg", "image/jpeg")
    object_storage.client.objects.clear()
    with pytest.raises(S3Error) as caught:
        object_storage.download(stored, tmp_path / "retrieved.jpg")
    assert caught.value.code == "NoSuchKey"
    object_storage.client.bucket_exists = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="not provisioned"):
        object_storage.ensure_bucket()


def test_presigned_url_uses_configured_browser_endpoint_and_expiry(downloaded):
    stored = make_object_storage().upload(downloaded, "originals/one.jpg", "image/jpeg")
    object_storage = MinioObjectStorage(MinioSettings("media.example.test", "test-user", "test-secret"))
    url = urlsplit(object_storage.presigned_get_url(stored, timedelta(minutes=5)))
    assert url.scheme == "https"
    assert url.netloc == "media.example.test"
    assert url.path == "/invoice-originals/originals/one.jpg"
    assert parse_qs(url.query)["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Signature" in parse_qs(url.query)


@pytest.mark.parametrize("received_at", [
    datetime(2026, 10, 2, 0, 0, tzinfo=UTC).replace(tzinfo=None),
    datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
    datetime(2026, 10, 2, 2, 0, tzinfo=timezone(timedelta(hours=2))),
])
def test_keys_preserve_utc_date_for_sqlite_and_aware_timestamps(received_at):
    document_id = uuid4()
    assert original_object_key(document_id, received_at, ".pdf") == f"originals/2026/10/02/{document_id}.pdf"


@pytest.fixture
def minio_environment(monkeypatch):
    monkeypatch.setattr("src.object_storage.settings.load_project_environment", lambda: None)
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "test-user")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test-secret")
    for name in ("MINIO_SECURE", "MINIO_BUCKET", "MINIO_REGION", "MINIO_CONNECT_TIMEOUT_SECONDS", "MINIO_READ_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_settings_default_to_tls_and_hide_credentials(minio_environment, monkeypatch):
    settings = MinioSettings.from_environment()
    assert settings.secure is True
    assert "test-secret" not in repr(settings)
    assert "test-user" not in repr(settings)
    monkeypatch.setenv("MINIO_SECURE", "false")
    assert MinioSettings.from_environment().secure is False


@pytest.mark.parametrize("name,value", [
    ("MINIO_ENDPOINT", ""),
    ("MINIO_ENDPOINT", "http://localhost:9000"),
    ("MINIO_ENDPOINT", "localhost:9000/path"),
    ("MINIO_ENDPOINT", "localhost:abc"),
    ("MINIO_ACCESS_KEY", ""),
    ("MINIO_SECRET_KEY", ""),
    ("MINIO_SECURE", "maybe"),
    ("MINIO_BUCKET", "invalid_bucket"),
    ("MINIO_REGION", ""),
    ("MINIO_CONNECT_TIMEOUT_SECONDS", "0"),
    ("MINIO_READ_TIMEOUT_SECONDS", "-1"),
    ("MINIO_READ_TIMEOUT_SECONDS", "nan"),
])
def test_settings_reject_invalid_environment(minio_environment, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        MinioSettings.from_environment()
