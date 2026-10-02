"""File boundary checks for OCR inputs and legacy media migration."""

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def legacy_media_path(directory: Path, storage_path: str | None) -> Path:
    if not storage_path:
        raise RuntimeError("Invoice submission has no stored file")
    path = (directory / storage_path).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Stored invoice path escapes the media directory")
    return path
