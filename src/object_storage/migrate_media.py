"""Explicit, resumable migration of legacy originals; source files are never removed."""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from src.invoices.domain import DownloadedAttachment, StorageBackend
from src.sql_database import Database, DatabaseSettings
from src.sql_database.models import DocumentRecord
from src.whatsapp.media import (
    MEDIA_EXTENSIONS,
    WhatsAppMediaSettings,
    verify_media_digest,
)

from .files import file_sha256, legacy_media_path
from .minio import MinioObjectStorage, original_object_key
from .settings import MinioSettings

logger = logging.getLogger(__name__)


@dataclass
class MigrationSummary:
    planned: int = 0
    migrated: int = 0
    skipped: int = 0
    failed: int = 0


def migrate_media(database: Database, media_directory: Path, object_storage: MinioObjectStorage | None = None) -> MigrationSummary:
    """Validate a dry run, or upload and verify when a storage client is supplied.

    Stop ingestion and OCR before applying. Each verified row commits separately;
    retries reuse matching objects left by an interrupted run.
    """
    summary = MigrationSummary()
    with database.session_factory() as session:
        documents = session.scalars(select(DocumentRecord).order_by(DocumentRecord.id)).all()

    for document in documents:
        if document.storage_backend is StorageBackend.MINIO or not document.storage_path:
            summary.skipped += 1
            continue
        try:
            path = legacy_media_path(media_directory, document.storage_path)
            size = path.stat().st_size
            digest = file_sha256(path)
            if document.file_size is not None and size != document.file_size:
                raise ValueError("Legacy original size differs from the database")
            verify_media_digest(document.sha256, bytes.fromhex(digest))
            key = original_object_key(document.id, document.received_at, MEDIA_EXTENSIONS[document.mime_type])
            if object_storage is None:
                logger.info("Would migrate submission %s to %s", document.id, key)
                summary.planned += 1
                continue

            stored = object_storage.upload(DownloadedAttachment(path, size, digest), key, document.mime_type)
            with TemporaryDirectory(prefix="invoice-migrate-") as directory:
                object_storage.download(stored, Path(directory) / path.name)
            with database.session_factory.begin() as session:
                record = session.get(DocumentRecord, document.id)
                record.storage_backend = StorageBackend.MINIO
                record.storage_bucket = stored.bucket
                record.storage_object_key = stored.object_key
                record.content_sha256 = stored.content_sha256
                record.file_size = stored.file_size
            summary.migrated += 1
            logger.info("Migrated submission %s", document.id)
        except Exception:
            # Continue independent rows, but report failure and return a nonzero CLI status.
            logger.exception("Could not migrate submission %s", document.id)
            summary.failed += 1
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate source files without writing (default)")
    mode.add_argument("--apply", action="store_true", help="Upload verified originals; stop workers first")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    object_storage = MinioObjectStorage(MinioSettings.from_environment()) if args.apply else None
    if object_storage is not None:
        object_storage.ensure_bucket()
    database = Database(DatabaseSettings.from_environment())
    try:
        summary = migrate_media(database, WhatsAppMediaSettings.from_environment().media_directory, object_storage)
        logger.info(
            "Media migration: planned=%s migrated=%s skipped=%s failed=%s",
            summary.planned, summary.migrated, summary.skipped, summary.failed,
        )
        return 1 if summary.failed else 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
