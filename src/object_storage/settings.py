"""MinIO connection settings shared by workers and maintenance commands."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from minio.helpers import check_bucket_name

from src.environment import load_project_environment


@dataclass(frozen=True, slots=True)
class MinioSettings:
    endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    secure: bool = True
    bucket: str = "invoice-originals"
    region: str = "us-east-1"
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 30

    @classmethod
    def from_environment(cls) -> MinioSettings:
        load_project_environment()
        required = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        for name in required:
            if not os.getenv(name, "").strip():
                raise ValueError(f"{name} is required")

        endpoint = os.environ["MINIO_ENDPOINT"].strip()
        parsed = urlsplit(f"//{endpoint}")
        if not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("MINIO_ENDPOINT must be a host or host:port without a URL scheme")
        # Accessing port validates invalid/non-numeric port settings.
        if parsed.port == 0:
            raise ValueError("MINIO_ENDPOINT port must be positive")

        secure = os.getenv("MINIO_SECURE", "true").strip().lower()
        if secure not in {"true", "false"}:
            raise ValueError("MINIO_SECURE must be true or false")
        bucket = os.getenv("MINIO_BUCKET", "invoice-originals").strip()
        check_bucket_name(bucket, strict=True)
        region = os.getenv("MINIO_REGION", "us-east-1").strip()
        if not region:
            raise ValueError("MINIO_REGION cannot be empty")
        connect_timeout = float(os.getenv("MINIO_CONNECT_TIMEOUT_SECONDS", "5"))
        read_timeout = float(os.getenv("MINIO_READ_TIMEOUT_SECONDS", "30"))
        if any(not math.isfinite(value) or value <= 0 for value in (connect_timeout, read_timeout)):
            raise ValueError("MinIO timeouts must be finite and positive")

        return cls(
            endpoint=endpoint,
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=secure == "true",
            bucket=bucket,
            region=region,
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=read_timeout,
        )
