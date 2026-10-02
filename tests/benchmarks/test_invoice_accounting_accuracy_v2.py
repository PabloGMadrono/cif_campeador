"""Level-two invoice benchmark after accounting reconciliation."""

import os
from pathlib import Path

import pytest

from src.invoices.accounting import reconcile_invoice
from src.ocr.models import Invoice
from tests.benchmarks import test_invoice_accuracy_v2 as benchmark
from tests.invoice_accuracy_v2 import OcrScope

TESTS = Path(__file__).resolve().parents[1]
GROUND_TRUTH = TESTS / "ground_truths" / "ocr_ground_truth_accounting_v2.csv"
DEFAULT_REPORT_DIRECTORY = TESTS / "results" / "accounting_v2"


def _reconcile(invoice: Invoice) -> tuple[Invoice, str, tuple[object, ...]]:
    result = reconcile_invoice(invoice)
    return result.invoice, result.status.value, result.corrections


def test_invoice_accounting_accuracy_v2(
    ocr_scope: OcrScope,
    ocr_image: str | None,
):
    if not GROUND_TRUTH.is_file():
        pytest.skip(f"Add accounting ground truth: {GROUND_TRUTH}")

    benchmark._run_accuracy_benchmark(
        ocr_scope,
        ocr_image,
        ground_truth=GROUND_TRUTH,
        report_directory=Path(
            os.environ.get(
                "OCR_ACCOUNTING_V2_REPORT_DIR",
                DEFAULT_REPORT_DIRECTORY,
            )
        ),
        result_transform=_reconcile,
        title="OCR + accounting level 2",
    )
