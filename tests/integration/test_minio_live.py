"""Opt-in transfer and private URL checks against the development MinIO service."""

import hashlib
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from minio.error import S3Error

from src.invoices.domain import DownloadedAttachment
from src.object_storage import MinioObjectStorage, MinioSettings
from src.object_storage.minio import original_object_key

pytestmark = pytest.mark.skipif(os.getenv("RUN_MINIO_INTEGRATION") != "1", reason="Set RUN_MINIO_INTEGRATION=1 for development MinIO")


@pytest.mark.parametrize("mime_type,extension", [("image/jpeg", ".jpg"), ("application/pdf", ".pdf")])
def test_live_original_transfer_private_access_and_expiry(tmp_path, mime_type, extension):
    object_storage = MinioObjectStorage(MinioSettings.from_environment())
    object_storage.ensure_bucket()
    source = tmp_path / f"original{extension}"
    content = b"development MinIO integration fixture"
    source.write_bytes(content)
    downloaded = DownloadedAttachment(source, len(content), hashlib.sha256(content).hexdigest())
    key = original_object_key(uuid4(), datetime.now(UTC), extension)
    stored = object_storage.upload(downloaded, key, mime_type)
    assert object_storage.upload(downloaded, key, mime_type) == stored
    metadata = object_storage.client.stat_object(stored.bucket, stored.object_key)
    assert metadata.content_type == mime_type
    assert metadata.metadata["x-amz-meta-sha256"] == downloaded.content_sha256
    destination = tmp_path / f"retrieved{extension}"
    object_storage.download(stored, destination)
    assert destination.read_bytes() == content

    with httpx.Client(timeout=10) as client:
        url = object_storage.presigned_get_url(stored)
        response = client.get(url)
        response.raise_for_status()
        assert response.content == content
        assert response.headers["content-type"] == mime_type
        assert client.get(url.partition("?")[0]).status_code == 403
        expired_url = object_storage.presigned_get_url(stored, expires=timedelta(seconds=1))
        time.sleep(2)
        assert client.get(expired_url).status_code == 403

    # The provisioned application account deliberately cannot delete originals.
    with pytest.raises(S3Error) as caught:
        object_storage.client.remove_object(stored.bucket, stored.object_key)
    assert caught.value.code == "AccessDenied"
