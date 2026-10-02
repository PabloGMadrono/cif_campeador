"""Store validated originals and materialize them for path-based OCR."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import urllib3
from minio import Minio
from minio.error import S3Error

from src.invoices.domain import DownloadedAttachment, StoredAttachment

from .files import file_sha256
from .settings import MinioSettings


def original_object_key(document_id: UUID, received_at: datetime, extension: str) -> str:
    """Use one stable key per submission, including during retry and migration."""
    # SQLite returns UTC timestamps without tzinfo.
    received_at = received_at.replace(tzinfo=UTC) if received_at.tzinfo is None else received_at.astimezone(UTC)
    date_path = received_at.strftime("%Y/%m/%d")
    return f"originals/{date_path}/{document_id}{extension}"


class MinioObjectStorage:
    def __init__(self, settings: MinioSettings) -> None:
        self.settings = settings
        self.client = Minio(
            settings.endpoint,
            access_key=settings.access_key,
            secret_key=settings.secret_key,
            secure=settings.secure,
            region=settings.region,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(
                    connect=settings.connect_timeout_seconds,
                    read=settings.read_timeout_seconds,
                ),
                retries=False,
            ),
        )

    def ensure_bucket(self) -> None:
        """Require provisioning before workers start; never create buckets here."""
        if not self.client.bucket_exists(self.settings.bucket):
            raise RuntimeError(f"MinIO bucket {self.settings.bucket!r} is not provisioned")

    def upload(self, downloaded: DownloadedAttachment, object_key: str, mime_type: str) -> StoredAttachment:
        """Upload once, or reuse a matching object left by an interrupted attempt."""
        bucket = self.settings.bucket
        try:
            existing = self.client.stat_object(bucket, object_key)
        except S3Error as error:
            if error.code != "NoSuchKey":
                raise
            self.client.fput_object(
                bucket,
                object_key,
                str(downloaded.absolute_path),
                content_type=mime_type,
                metadata={"sha256": downloaded.content_sha256},
            )
        else:
            if (
                existing.size != downloaded.file_size
                or existing.content_type != mime_type
                or existing.metadata.get("x-amz-meta-sha256") != downloaded.content_sha256
            ):
                raise ValueError(f"MinIO original conflicts with submission object {object_key}")

        return StoredAttachment(bucket, object_key, downloaded.file_size, downloaded.content_sha256)

    def download(self, stored: StoredAttachment, destination: Path) -> None:
        """Retrieve an original and verify its bytes before giving it to OCR."""
        self.client.fget_object(stored.bucket, stored.object_key, str(destination))
        if destination.stat().st_size != stored.file_size or file_sha256(destination) != stored.content_sha256:
            raise ValueError(f"MinIO original failed integrity verification: {stored.object_key}")

    def presigned_get_url(self, stored: StoredAttachment, expires: timedelta = timedelta(minutes=5)) -> str:
        """Sign with this client's endpoint; use a browser-reachable endpoint for UI access."""
        return self.client.presigned_get_object(stored.bucket, stored.object_key, expires=expires)
