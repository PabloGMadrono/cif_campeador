r"""Opt-in OCR-to-database check for one valid and one proforma image.

Run with:
    $env:RUN_LIVE_OCR_DB_TEST = "1"
    & .\.venv\Scripts\python.exe -m pytest tests/integration/test_invoice_database_live.py -v -s

The live test makes billable OCR calls. Both tests use in-memory SQLite.
"""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.invoices.accounting import reconcile_invoice
from src.invoices.domain import MessageType
from src.jobs.contracts import DownloadJob
from src.ocr import invoice_extractor
from src.ocr.models import InvoiceValidity
from src.sql_database import Database, DatabaseSettings
from src.sql_database.models import Base
from src.sql_database.operations import InvoiceSubmissionLifecycle
from tests.invoice_fixtures import make_invoice

IMAGES = Path(__file__).resolve().parents[1] / "images" / "trial_invoices"
CASES = (
    ("medium/IMG_3320.HEIC", InvoiceValidity.VALID, None),
    ("easy/IMG_3321.HEIC", InvoiceValidity.INVALID, "Proforma"),
)


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_OCR_DB_TEST") != "1",
    reason="Set RUN_LIVE_OCR_DB_TEST=1 to allow billable OCR calls",
)
def test_live_ocr_persists_validity_and_diagnosis():
    _assert_ocr_database_roundtrip(invoice_extractor, print_rows=True)


def test_database_roundtrip_with_offline_ocr(capsys):
    def extract_invoice(path: str):
        if Path(path).name == "IMG_3321.HEIC":
            return make_invoice(
                validity=InvoiceValidity.INVALID,
                diagnostic_type="Proforma",
            )
        return make_invoice(validity=InvoiceValidity.VALID)

    _assert_ocr_database_roundtrip(
        SimpleNamespace(extract_invoice=extract_invoice), print_rows=True
    )
    rows = json.loads(capsys.readouterr().out)
    assert [row["filename"] for row in rows] == ["IMG_3320.HEIC", "IMG_3321.HEIC"]
    assert [(row["validity"], row["diagnostic_type"]) for row in rows] == [
        ("valid", None),
        ("invalid", "Proforma"),
    ]
    assert all("lineas_iva" in row and "total" in row for row in rows)


def _assert_ocr_database_roundtrip(extractor, *, print_rows=False):
    database = Database(DatabaseSettings("sqlite:///:memory:"))
    Base.metadata.create_all(database.engine)
    observed = {}
    persisted_rows = {}
    submissions = InvoiceSubmissionLifecycle(database)
    try:
        for relative_path, _, _ in CASES:
            source = IMAGES / relative_path
            assert source.is_file(), f"Missing test image: {source}"
            invoice = extractor.extract_invoice(str(source))

            job = DownloadJob.create(
                whatsapp_message_id=f"live-ocr-db-{source.stem}",
                whatsapp_media_id=f"live-ocr-db-media-{source.stem}",
                message_type=MessageType.IMAGE,
                mime_type="image/heic",
                phone_number="34638894450",
                received_at=datetime.now(UTC),
                original_filename=source.name,
                meta_user_id="live-ocr-db-test",
                profile_name="Live OCR database test",
            )
            submissions.prepare_download(job)
            accounting_result = reconcile_invoice(invoice)
            submissions.save_invoice(
                job.submission_id,
                accounting_result.invoice,
                accounting_result.status,
                datetime.now(UTC),
            )

            stored = submissions.get_invoice(job.submission_id)
            assert stored is not None
            assert stored.invoice == accounting_result.invoice
            observed[source.name] = (
                stored.invoice.validity,
                stored.invoice.diagnostic_type,
            )
            persisted_rows[source.name] = {
                "filename": source.name,
                "document_id": str(stored.document_id),
                "extracted_at": stored.extracted_at.isoformat(),
                **asdict(stored.invoice),
            }
        with database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT s.original_filename, i.validity, i.diagnostic_type "
                    "FROM invoices AS i "
                    "JOIN invoice_submissions AS s ON s.id = i.document_id"
                )
            ).all()
        if print_rows:
            print(
                json.dumps(
                    [persisted_rows[filename] for filename in sorted(persisted_rows)],
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )

        expected = {
            Path(relative_path).name: (validity, diagnosis)
            for relative_path, validity, diagnosis in CASES
        }
        assert observed == expected
        assert {
            filename: (validity, diagnosis) for filename, validity, diagnosis in rows
        } == {
            filename: (validity.value, diagnosis)
            for filename, (validity, diagnosis) in expected.items()
        }
    finally:
        database.dispose()
