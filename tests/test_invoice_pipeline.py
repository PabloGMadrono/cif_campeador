"""Offline integration tests for SQL persistence and pipeline idempotency."""

import asyncio
from datetime import UTC, datetime, timedelta

from src.invoices.domain import (
    DocumentStatus,
    DownloadedAttachment,
    MessageType,
)
from src.invoices.services import DownloadProcessingService, OcrProcessingService
from src.jobs.contracts import DownloadJob
from src.ocr.models import (
    EquivalenceSurcharge,
    InvoiceValidity,
    IrpfWithholding,
    IvaLine,
)
from src.persistence import Database, DatabaseSettings
from src.persistence.models import Base
from src.persistence.unit_of_work import create_unit_of_work_factory
from tests.invoice_fixtures import make_invoice

RECEIVED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


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
    factory = create_unit_of_work_factory(database)
    try:
        with factory() as uow:
            first = uow.customers.resolve_sender(
                phone_number="+34638894450",
                meta_user_id="shared-user",
                profile_name="Pablo",
                seen_at=RECEIVED_AT,
            )
            uow.commit()

        with factory() as uow:
            second = uow.customers.resolve_sender(
                phone_number="+34600000000",
                meta_user_id="shared-user",
                profile_name="Accounts",
                seen_at=RECEIVED_AT + timedelta(days=1),
            )
            uow.commit()

        assert first.customer_id == second.customer_id
        assert first.phone_number != second.phone_number
    finally:
        database.dispose()


def test_download_and_ocr_pipeline_is_idempotent(tmp_path):
    database = _database(tmp_path)
    factory = create_unit_of_work_factory(database)
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
                recargos_equivalencia=(
                    EquivalenceSurcharge("100.00", "5.2", "5.20"),
                ),
                retencion_irpf=IrpfWithholding("150.00", "15", "-22.50"),
                total="158.70",
            )

    downloader = FakeDownloader()
    extractor = FakeExtractor()
    download_service = DownloadProcessingService(factory, downloader)
    ocr_service = OcrProcessingService(factory, extractor, media_directory)
    job = _job()

    try:
        first_download = asyncio.run(download_service.process(job))
        second_download = asyncio.run(download_service.process(job))
        first_ocr = ocr_service.process(job.submission_id)
        second_ocr = ocr_service.process(job.submission_id)

        assert first_download.status == DocumentStatus.DOWNLOADED
        assert second_download.status == DocumentStatus.DOWNLOADED
        assert first_ocr.status == DocumentStatus.COMPLETED
        assert second_ocr.status == DocumentStatus.COMPLETED
        assert downloader.calls == 1
        assert extractor.calls == 1

        with factory() as uow:
            document = uow.documents.get(job.submission_id)
            invoice = uow.invoices.get(job.submission_id)
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
