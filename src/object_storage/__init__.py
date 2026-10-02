"""Durable original attachment storage."""

from .minio import MinioObjectStorage
from .settings import MinioSettings

__all__ = ["MinioObjectStorage", "MinioSettings"]
