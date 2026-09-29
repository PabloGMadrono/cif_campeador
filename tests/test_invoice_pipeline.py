"""Offline integration tests for SQL persistence and pipeline idempotency."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.invoices.domain import (
    DocumentStatus,
    DownloadedAttachment,
    FailureStage,
    MessageType,
)
from src.jobs.contracts import DownloadJob
from src.ocr.models import (
    EquivalenceSurcharge,
    InvoiceValidity,
    IrpfWithholding,
    IvaLine,
)
from src.persistence import Database, DatabaseSettings
from src.persistence.models import Base, DocumentRecord
from src.persistence.operations import (
    add_document,
    get_invoice,
    resolve_sender,
)
from src.workers import ocr as ocr_module
from src.workers.download import DownloadWorker
from src.workers.ocr import OcrWorker
from src.workers.settings import WorkerSettings
from tests.invoice_fixtures import make_invoice

RECEIVED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SETTINGS = WorkerSettings(20, 3, 1, 3, (0,))


def _database(tmp_path):
    database = Database(DatabaseSettings(f"sqlite:///{tmp_path / 'pipeline.db'}"))
    Base.metadata.create_all(database.engine)
    return database


def _job(message_id="message-1", phone="34638894450", meta_user_id="user-1"):
    return DownloadJob.create(
        whatsapp_message_id=message_id,
        whatsapp_media_id=f"media-{message_id}",
        message_type=MessageType.IMAGE,
        mime_type="image/jpeg",
        phone_number=phone,
        received_at=RECEIVED_AT,
        meta_user_id=meta_user_id,
        profile_name="Pablo",
    )


def test_phone_numbers_share_customer_when_meta_user_matches(tmp_path):
    database = _database(tmp_path)
    factory = database.session_factory
    try:
        first_job = _job(phone="34638894450", meta_user_id="shared-user")
        second_job = DownloadJob.create(
            whatsapp_message_id="message-2",
            whatsapp_media_id="media-2",
            message_type=MessageType.IMAGE,
            mime_type="image/jpeg",
            phone_number="34600000000",
            meta_user_id="shared-user",
            profile_name="Accounts",
            received_at=RECEIVED_AT + timedelta(days=1),
        )
        with factory.begin() as session:
            first = resolve_sender(session, first_job)
        with factory.begin() as session:
            second = resolve_sender(session, second_job)

        assert first.customer_id == second.customer_id
        assert first.phone_number != second.phone_number
    finally:
        database.dispose()


@pytest.fixture
def downloaded_submission(tmp_path):
    """A real committed submission for transaction and retry regression checks."""
    database = _database(tmp_path)
    job = _job()
    media_directory = tmp_path / "media"
    media_directory.mkdir()
    (media_directory / "invoice.jpg").write_bytes(b"invoice")
    with database.session_factory.begin() as session:
        resolve_sender(session, job)
        document = add_document(session, job)
        document.status = DocumentStatus.DOWNLOADED
        document.storage_path = "invoice.jpg"
        document.file_size = 7
        document.download_attempts = 1
    try:
        yield database, job, media_directory
    finally:
        database.dispose()


@pytest.fixture
def ocr_worker(downloaded_submission):
    database, _, media_directory = downloaded_submission
    return OcrWorker(
        consumer=Mock(),
        database=database,
        extractor=Mock(),
        media_directory=media_directory,
        settings=SETTINGS,
    )


@pytest.fixture
def download_worker(downloaded_submission):
    database, _, _ = downloaded_submission
    return DownloadWorker(
        consumer=Mock(),
        database=database,
        downloader=Mock(),
        settings=SETTINGS,
    )


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.DOWNLOADED,
        DocumentStatus.OCR_PROCESSING,
        DocumentStatus.COMPLETED,
        DocumentStatus.FAILED,
    ],
)
def test_download_skips_submissions_already_past_download(
    downloaded_submission, download_worker, status
):
    database, job, _ = downloaded_submission
    with database.session_factory.begin() as session:
        session.get(DocumentRecord, job.submission_id).status = status
    downloader = download_worker.downloader

    result = asyncio.run(download_worker.process_download(job))

    assert result.status == status
    assert result.download_attempts == 1
    downloader.download.assert_not_called()


@pytest.mark.parametrize("status", [DocumentStatus.COMPLETED, DocumentStatus.FAILED])
def test_ocr_skips_terminal_submissions(downloaded_submission, ocr_worker, status):
    database, job, _ = downloaded_submission
    with database.session_factory.begin() as session:
        session.get(DocumentRecord, job.submission_id).status = status
    extractor = ocr_worker.extractor

    result = ocr_worker.process_ocr(job.submission_id)

    assert result == status
    with database.session_factory() as session:
        assert session.get(DocumentRecord, job.submission_id).ocr_attempts == 0
    extractor.extract_invoice.assert_not_called()


@pytest.mark.parametrize(
    "status", [DocumentStatus.RECEIVED, DocumentStatus.DOWNLOADING]
)
def test_ocr_rejects_submissions_before_download(
    downloaded_submission, ocr_worker, status
):
    database, job, _ = downloaded_submission
    with database.session_factory.begin() as session:
        session.get(DocumentRecord, job.submission_id).status = status
    extractor = ocr_worker.extractor

    with pytest.raises(RuntimeError, match="not ready for OCR"):
        ocr_worker.process_ocr(job.submission_id)

    extractor.extract_invoice.assert_not_called()
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.status == status
        assert document.ocr_attempts == 0


@pytest.mark.parametrize(
    "path, error, message",
    [
        (None, RuntimeError, "no stored file"),
        ("../outside.jpg", ValueError, "escapes the media directory"),
    ],
)
def test_ocr_rejects_missing_or_unsafe_paths(
    downloaded_submission, ocr_worker, path, error, message
):
    database, job, _ = downloaded_submission
    with database.session_factory.begin() as session:
        session.get(DocumentRecord, job.submission_id).storage_path = path
    extractor = ocr_worker.extractor

    with pytest.raises(error, match=message):
        ocr_worker.process_ocr(job.submission_id)

    extractor.extract_invoice.assert_not_called()
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.status == DocumentStatus.OCR_PROCESSING
        assert document.ocr_attempts == 1


def test_ocr_retry_keeps_committed_attempt_and_original_start_time(
    downloaded_submission,
    ocr_worker,
):
    database, job, _ = downloaded_submission
    expected = make_invoice()
    extractor = ocr_worker.extractor
    extractor.extract_invoice.side_effect = [
        TimeoutError("temporary timeout"),
        expected,
    ]
    process = ocr_worker.process_ocr

    with pytest.raises(TimeoutError):
        process(job.submission_id)
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.status == DocumentStatus.OCR_PROCESSING
        assert document.ocr_attempts == 1
        started_at = document.ocr_started_at
        assert get_invoice(session, job.submission_id) is None

    completed = process(job.submission_id)
    assert completed == DocumentStatus.COMPLETED
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.ocr_attempts == 2
        assert document.ocr_started_at == started_at
        assert get_invoice(session, job.submission_id).invoice == expected


def test_invoice_write_and_completion_roll_back_together(
    downloaded_submission, ocr_worker, monkeypatch
):
    database, job, _ = downloaded_submission
    extractor = ocr_worker.extractor
    extractor.extract_invoice.return_value = make_invoice()
    process = ocr_worker.process_ocr
    save_invoice = ocr_module.save_invoice

    def fail_after_write(session, *args):
        save_invoice(session, *args)
        session.flush()
        raise RuntimeError("database write failed")

    with monkeypatch.context() as patch:
        patch.setattr(ocr_module, "save_invoice", fail_after_write)
        with pytest.raises(RuntimeError, match="database write failed"):
            process(job.submission_id)

    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.status == DocumentStatus.OCR_PROCESSING
        assert document.completed_at is None
        assert get_invoice(session, job.submission_id) is None

    assert process(job.submission_id) == DocumentStatus.COMPLETED


@pytest.mark.parametrize("stage", list(FailureStage))
def test_final_failure_is_persisted_with_bounded_error(
    downloaded_submission,
    ocr_worker,
    download_worker,
    stage,
):
    database, job, _ = downloaded_submission
    worker = ocr_worker if stage is FailureStage.OCR else download_worker
    worker.mark_failed(job.submission_id, RuntimeError("x" * 2500))
    with database.session_factory() as session:
        document = session.get(DocumentRecord, job.submission_id)
        assert document.status == DocumentStatus.FAILED
        assert document.failure_stage == stage
        assert document.last_error == "x" * 2000


def test_ocr_rejects_unknown_submission(ocr_worker):
    extractor = ocr_worker.extractor
    with pytest.raises(LookupError, match="does not exist"):
        ocr_worker.process_ocr(uuid4())
    extractor.extract_invoice.assert_not_called()


def test_download_and_ocr_pipeline_is_idempotent(tmp_path):
    database = _database(tmp_path)
    factory = database.session_factory
    media_directory = tmp_path / "media"
    stored_file = media_directory / "2026/09/17/media-message-1.jpg"
    stored_file.parent.mkdir(parents=True)
    stored_file.write_bytes(b"invoice")

    class FakeDownloader:
        calls = 0

        async def download(self, attachment):
            self.calls += 1
            return DownloadedAttachment(
                absolute_path=stored_file,
                storage_path="2026/09/17/media-message-1.jpg",
                file_size=7,
            )

    class FakeExtractor:
        calls = 0

        def extract_invoice(self, path):
            self.calls += 1
            assert path == str(stored_file.resolve())
            return make_invoice(
                validity=InvoiceValidity.INVALID,
                diagnostic_type="Proforma",
                fecha="2026-09-17",
                numero_factura="F-42",
                nif_proveedor="B12345678",
                nombre_proveedor="Supplier SL",
                lineas_iva=(
                    IvaLine("100.00", "21", "21.00"),
                    IvaLine("50.00", "10", "5.00"),
                ),
                recargos_equivalencia=(EquivalenceSurcharge("100.00", "5.2", "5.20"),),
                retencion_irpf=IrpfWithholding("150.00", "15", "-22.50"),
                total="158.70",
            )

    downloader = FakeDownloader()
    extractor = FakeExtractor()
    download_worker = DownloadWorker(
        consumer=Mock(),
        database=database,
        downloader=downloader,
        settings=SETTINGS,
    )
    ocr_worker = OcrWorker(
        consumer=Mock(),
        database=database,
        extractor=extractor,
        media_directory=media_directory,
        settings=SETTINGS,
    )
    download = download_worker.process_download
    ocr = ocr_worker.process_ocr
    job = _job()

    try:
        first_download = asyncio.run(download(job))
        second_download = asyncio.run(download(job))
        first_ocr = ocr(job.submission_id)
        second_ocr = ocr(job.submission_id)

        assert first_download.status == DocumentStatus.DOWNLOADED
        assert second_download.status == DocumentStatus.DOWNLOADED
        assert first_ocr == DocumentStatus.COMPLETED
        assert second_ocr == DocumentStatus.COMPLETED
        assert downloader.calls == 1
        assert extractor.calls == 1

        with factory() as session:
            document = session.get(DocumentRecord, job.submission_id)
            invoice = get_invoice(session, job.submission_id)
        assert document is not None
        assert document.download_attempts == 1
        assert document.ocr_attempts == 1
        assert invoice is not None
        assert invoice.invoice.numero_factura == "F-42"
        assert invoice.invoice.validity is InvoiceValidity.INVALID
        assert invoice.invoice.diagnostic_type == "Proforma"
        assert invoice.invoice.lineas_iva == (
            IvaLine("100.00", "21", "21.00"),
            IvaLine("50.00", "10", "5.00"),
        )
        assert invoice.invoice.recargos_equivalencia == (
            EquivalenceSurcharge("100.00", "5.2", "5.20"),
        )
        assert invoice.invoice.retencion_irpf == IrpfWithholding(
            "150.00", "15", "-22.50"
        )
    finally:
        database.dispose()
