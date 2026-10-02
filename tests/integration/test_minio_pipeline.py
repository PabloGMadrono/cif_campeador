"""Failures between Meta, MinIO, SQL, and Redis remain safely replayable."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.invoices.domain import DocumentStatus, FailureStage, MessageType
from src.jobs.contracts import DownloadJob, OcrJob
from src.jobs.redis_streams import StreamMessage
from src.sql_database import Database, DatabaseSettings
from src.sql_database.models import Base, DocumentRecord
from src.whatsapp.media import WhatsAppMediaDownloader, WhatsAppMediaSettings
from src.workers.download import DownloadWorker
from src.workers.ocr import OcrWorker
from src.workers.settings import WorkerSettings
from tests.invoice_fixtures import make_invoice
from tests.object_storage_fixtures import make_object_storage


@pytest.fixture
def pipeline(tmp_path):
    database = Database(DatabaseSettings(f"sqlite:///{tmp_path / 'pipeline.db'}"))
    Base.metadata.create_all(database.engine)
    object_storage = make_object_storage()
    transport = httpx.MockTransport(lambda request: (
        httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media"})
        if request.url.host == "graph.facebook.com"
        else httpx.Response(200, content=b"invoice", headers={"content-type": "image/jpeg"})
    ))
    client = httpx.AsyncClient(transport=transport)
    media_directory = tmp_path / "legacy"
    downloader = WhatsAppMediaDownloader(
        WhatsAppMediaSettings("meta-token", media_directory, "v23.0", ("lookaside.fbsbx.com",), 5, 1024), client,
    )
    downloaded_paths = []
    original_download = downloader.download

    async def capture_download(attachment, directory):
        result = await original_download(attachment, directory)
        downloaded_paths.append(result.absolute_path)
        return result

    downloader.download = capture_download
    consumer = Mock(stream="whatsapp:downloads", settings=SimpleNamespace(ocr_stream="whatsapp:ocr"))
    consumer.replace = AsyncMock()
    consumer.complete = AsyncMock()
    consumer.dead_letter = AsyncMock()
    settings = WorkerSettings(20, 3, 1, 3, (0,))
    download_worker = DownloadWorker(consumer=consumer, database=database, downloader=downloader, object_storage=object_storage, settings=settings)
    ocr_worker = OcrWorker(
        consumer=consumer, database=database, extractor=Mock(extract_invoice=Mock(return_value=make_invoice())),
        media_directory=media_directory, object_storage=object_storage, settings=settings,
    )
    job = DownloadJob.create(
        whatsapp_message_id="message-1", whatsapp_media_id="media-1", message_type=MessageType.IMAGE,
        mime_type="image/jpeg", phone_number="34600000000", received_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    try:
        yield SimpleNamespace(
            database=database, object_storage=object_storage, download=download_worker, ocr=ocr_worker,
            job=job, downloaded_paths=downloaded_paths, consumer=consumer,
        )
    finally:
        asyncio.run(client.aclose())
        database.dispose()


def test_upload_before_sql_failure_reuses_original_and_cleans_temporary_file(pipeline, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(pipeline.download.submissions, "record_download", Mock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            asyncio.run(pipeline.download.process_download(pipeline.job))
    with pipeline.database.session_factory() as session:
        document = session.get(DocumentRecord, pipeline.job.submission_id)
        assert document.status is DocumentStatus.DOWNLOADING
        assert document.storage_object_key is None
    assert pipeline.object_storage.client.uploads == 1
    assert not pipeline.downloaded_paths[0].exists()
    result = asyncio.run(pipeline.download.process_download(pipeline.job))
    assert result.status is DocumentStatus.DOWNLOADED
    assert result.download_attempts == 2
    assert pipeline.object_storage.client.uploads == 1
    assert all(not path.exists() for path in pipeline.downloaded_paths)


def test_sql_before_redis_failure_retries_handoff_without_reupload(pipeline):
    pipeline.consumer.replace.side_effect = [TimeoutError("Redis timeout"), None, None]
    asyncio.run(pipeline.download._handle(StreamMessage("1-0", pipeline.job.to_dict())))
    asyncio.run(pipeline.download._handle(StreamMessage("2-0", pipeline.job.with_attempt(2).to_dict())))
    assert pipeline.object_storage.client.uploads == 1
    assert len(pipeline.downloaded_paths) == 1
    assert pipeline.consumer.replace.call_args.kwargs["destination_stream"] == "whatsapp:ocr"
    assert pipeline.consumer.replace.call_args.kwargs["payload"] == OcrJob(pipeline.job.submission_id).to_dict()
    assert pipeline.consumer.dead_letter.call_count == 0


@pytest.mark.parametrize("attempt", [1, 3])
def test_upload_failure_uses_download_retry_or_dead_letter(pipeline, attempt):
    pipeline.object_storage.client.fput_object = Mock(side_effect=TimeoutError("MinIO upload timeout"))
    asyncio.run(pipeline.download._handle(StreamMessage("1-0", pipeline.job.with_attempt(attempt).to_dict())))
    with pipeline.database.session_factory() as session:
        document = session.get(DocumentRecord, pipeline.job.submission_id)
        assert document.storage_object_key is None
        assert document.status is (DocumentStatus.DOWNLOADING if attempt == 1 else DocumentStatus.FAILED)
        if attempt == 3:
            assert document.failure_stage is FailureStage.DOWNLOAD
    assert all(not path.exists() for path in pipeline.downloaded_paths)
    if attempt == 1:
        assert pipeline.consumer.replace.call_args.kwargs["destination_stream"] == "whatsapp:downloads"
    else:
        assert pipeline.consumer.dead_letter.call_count == 1


@pytest.mark.parametrize("problem", ["missing", "corrupt", "timeout"])
def test_retrieval_failure_never_calls_ocr_or_falls_back_to_local(pipeline, problem, monkeypatch):
    document = asyncio.run(pipeline.download.process_download(pipeline.job))
    if problem == "missing":
        pipeline.object_storage.client.objects.clear()
    elif problem == "corrupt":
        key = (document.storage_bucket, document.storage_object_key)
        pipeline.object_storage.client.objects[key] = (b"changed", "image/jpeg", {})
    else:
        pipeline.object_storage.client.fget_object = Mock(side_effect=TimeoutError("MinIO read timeout"))
    with pipeline.database.session_factory.begin() as session:
        record = session.get(DocumentRecord, document.id)
        record.storage_path = "invoice.jpg"
    pipeline.ocr.media_directory.mkdir()
    (pipeline.ocr.media_directory / "invoice.jpg").write_bytes(b"invoice")
    with monkeypatch.context() as patch:
        local_read = Mock(side_effect=AssertionError("Unexpected local fallback"))
        patch.setattr("src.workers.ocr.legacy_media_path", local_read)
        asyncio.run(pipeline.ocr._handle(StreamMessage("ocr-1", OcrJob(document.id, attempt=3).to_dict())))
        local_read.assert_not_called()
    pipeline.ocr.extractor.extract_invoice.assert_not_called()
    with pipeline.database.session_factory() as session:
        record = session.get(DocumentRecord, document.id)
        assert record.status is DocumentStatus.FAILED
        assert record.failure_stage is FailureStage.OCR


def test_ocr_exception_removes_temporary_original_and_preserves_minio(pipeline):
    document = asyncio.run(pipeline.download.process_download(pipeline.job))
    paths = []

    def extract(path):
        paths.append(Path(path))
        assert paths[0].read_bytes() == b"invoice"
        raise RuntimeError("OCR failed")

    pipeline.ocr.extractor.extract_invoice.side_effect = extract
    with pytest.raises(RuntimeError, match="OCR failed"):
        pipeline.ocr.process_ocr(document.id)
    assert not paths[0].exists()
    assert len(pipeline.object_storage.client.objects) == 1


def test_late_download_cannot_regress_completed_submission(pipeline):
    document = asyncio.run(pipeline.download.process_download(pipeline.job))
    assert pipeline.ocr.process_ocr(document.id) is DocumentStatus.COMPLETED
    replay = pipeline.download.submissions.record_download(document.id, document.stored_attachment())
    assert replay.status is DocumentStatus.COMPLETED
    assert replay.ocr_attempts == 1
