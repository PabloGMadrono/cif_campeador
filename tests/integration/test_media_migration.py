"""Legacy media migration validates before committing and never removes source files."""

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.invoices.domain import DocumentStatus, MessageType, StorageBackend
from src.jobs.contracts import DownloadJob
from src.object_storage.migrate_media import MigrationSummary, migrate_media
from src.object_storage.minio import original_object_key
from src.sql_database import Database, DatabaseSettings
from src.sql_database.models import Base, DocumentRecord
from src.sql_database.operations import InvoiceSubmissionLifecycle
from src.workers.ocr import OcrWorker
from src.workers.settings import WorkerSettings
from tests.invoice_fixtures import make_invoice
from tests.object_storage_fixtures import make_object_storage


@pytest.fixture
def legacy_submission(tmp_path):
    database = Database(DatabaseSettings(f"sqlite:///{tmp_path / 'migration.db'}"))
    Base.metadata.create_all(database.engine)
    job = DownloadJob.create(
        whatsapp_message_id="legacy-message", whatsapp_media_id="media-1",
        message_type=MessageType.IMAGE, mime_type="image/jpeg", phone_number="34600000000",
        received_at=datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
        sha256=base64.b64encode(hashlib.sha256(b"original").digest()).decode(),
    )
    InvoiceSubmissionLifecycle(database).prepare_download(job)
    media_directory = tmp_path / "media"
    media_directory.mkdir()
    path = media_directory / "invoice.jpg"
    path.write_bytes(b"original")
    with database.session_factory.begin() as session:
        document = session.get(DocumentRecord, job.submission_id)
        document.storage_backend = StorageBackend.LOCAL
        document.storage_path = path.name
        document.file_size = 8
        document.status = DocumentStatus.DOWNLOADED
    try:
        yield database, job, media_directory, path
    finally:
        database.dispose()


def test_dry_run_checks_sources_without_writing(legacy_submission):
    database, job, directory, path = legacy_submission
    summary = migrate_media(database, directory)
    assert summary == MigrationSummary(planned=1)
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.storage_backend is StorageBackend.LOCAL
        assert document.storage_bucket is None
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("status", [DocumentStatus.DOWNLOADED, DocumentStatus.COMPLETED, DocumentStatus.FAILED])
def test_apply_preserves_status_and_path_and_is_resumable(legacy_submission, status):
    database, job, directory, path = legacy_submission
    with database.session_factory.begin() as session:
        session.get(DocumentRecord, job.submission_id).status = status
    object_storage = make_object_storage()
    assert migrate_media(database, directory, object_storage) == MigrationSummary(migrated=1)
    assert migrate_media(database, directory, object_storage) == MigrationSummary(skipped=1)
    assert object_storage.client.uploads == 1
    assert path.read_bytes() == b"original"
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.storage_backend is StorageBackend.MINIO
        assert document.status == status
        assert document.storage_path == path.name
        assert document.download_attempts == 1
        assert document.ocr_attempts == 0
        assert document.content_sha256 == hashlib.sha256(b"original").hexdigest()


@pytest.mark.parametrize("problem", ["missing", "escaped", "size", "digest"])
def test_invalid_sources_keep_legacy_reference(legacy_submission, problem):
    database, job, directory, path = legacy_submission
    with database.session_factory.begin() as session:
        document = session.get(DocumentRecord, job.submission_id)
        if problem == "missing":
            path.unlink()
        elif problem == "escaped":
            document.storage_path = "../outside.jpg"
        elif problem == "size":
            document.file_size = 1
        else:
            document.sha256 = base64.b64encode(b"x" * 32).decode()
    object_storage = make_object_storage()
    assert migrate_media(database, directory, object_storage) == MigrationSummary(failed=1)
    assert object_storage.client.uploads == 0
    with database.session_factory() as session:
        assert session.get(DocumentRecord, job.submission_id).storage_backend is StorageBackend.LOCAL


def test_failed_verification_can_resume_without_reupload(legacy_submission, monkeypatch):
    database, job, directory, path = legacy_submission
    object_storage = make_object_storage()
    with monkeypatch.context() as patch:
        patch.setattr(object_storage, "download", Mock(side_effect=TimeoutError("retrieval timed out")))
        assert migrate_media(database, directory, object_storage) == MigrationSummary(failed=1)
    with database.session_factory() as session:
        assert session.get(DocumentRecord, job.submission_id).storage_backend is StorageBackend.LOCAL
    assert migrate_media(database, directory, object_storage) == MigrationSummary(migrated=1)
    assert object_storage.client.uploads == 1
    assert path.exists()


def test_conflict_is_reported_without_switching_backend(legacy_submission):
    database, job, directory, _ = legacy_submission
    object_storage = make_object_storage()
    key = original_object_key(job.submission_id, job.received_at, ".jpg")
    object_storage.client.objects[object_storage.settings.bucket, key] = (b"conflict", "image/jpeg", {})
    assert migrate_media(database, directory, object_storage) == MigrationSummary(failed=1)
    with database.session_factory() as session:
        assert session.get(DocumentRecord, job.submission_id).storage_backend is StorageBackend.LOCAL


def test_migrated_pending_ocr_reads_minio_without_local_original(legacy_submission):
    database, job, directory, path = legacy_submission
    object_storage = make_object_storage()
    assert migrate_media(database, directory, object_storage) == MigrationSummary(migrated=1)
    path.unlink()
    received_paths = []

    def extract(filename):
        temporary_path = Path(filename)
        received_paths.append(temporary_path)
        assert temporary_path.read_bytes() == b"original"
        return make_invoice()

    worker = OcrWorker(
        consumer=Mock(), database=database, extractor=Mock(extract_invoice=extract),
        media_directory=directory, object_storage=object_storage, settings=WorkerSettings(20, 3, 1, 3, (0,)),
    )
    assert worker.process_ocr(job.submission_id) is DocumentStatus.COMPLETED
    assert not received_paths[0].exists()
