"""An in-memory MinIO client for offline storage and pipeline scenarios."""

from pathlib import Path
from types import SimpleNamespace

from minio.error import S3Error
from urllib3 import HTTPHeaderDict

from src.object_storage import MinioObjectStorage, MinioSettings


class MemoryMinioClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str, dict[str, str]]] = {}
        self.uploads = 0

    def bucket_exists(self, bucket: str) -> bool:
        return bucket == "invoice-originals"

    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        try:
            content, mime_type, metadata = self.objects[bucket, key]
        except KeyError:
            raise S3Error(None, "NoSuchKey", "Object does not exist", key, "request", "host") from None
        return SimpleNamespace(size=len(content), content_type=mime_type, metadata=HTTPHeaderDict(metadata))

    def fput_object(self, bucket: str, key: str, path: str, *, content_type: str, metadata: dict[str, str]) -> None:
        self.uploads += 1
        self.objects[bucket, key] = (
            Path(path).read_bytes(), content_type, {f"x-amz-meta-{name}": value for name, value in metadata.items()},
        )

    def fget_object(self, bucket: str, key: str, path: str) -> None:
        self.stat_object(bucket, key)
        Path(path).write_bytes(self.objects[bucket, key][0])


def make_object_storage() -> MinioObjectStorage:
    object_storage = MinioObjectStorage(MinioSettings("localhost:9000", "test-user", "test-secret", secure=False))
    object_storage.client = MemoryMinioClient()
    return object_storage
