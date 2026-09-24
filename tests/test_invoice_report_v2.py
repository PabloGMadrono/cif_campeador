"""Offline checks for level-two paths and durable report exports."""

import csv
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ocr.models import InvoiceValidity, IvaLine
from tests import test_invoice_accuracy_v2 as benchmark
from tests.invoice_accuracy_v2 import (
    GroundTruthDocument,
    GroundTruthIvaLine,
    OcrScope,
    score_document,
)
from tests.invoice_fixtures import make_invoice
from tests.invoice_report_v2 import LevelTwoReport
from tests.test_invoice_accuracy_v2 import _resolve_v2_image


def expected_document() -> GroundTruthDocument:
    return GroundTruthDocument(
        filename="one.png",
        validity=InvoiceValidity.INVALID,
        review_required=False,
        diagnostic_type="Proforma",
        notes="Informative only",
        fecha="01/02/2026",
        numero_factura="001",
        nif_proveedor=None,
        nombre_proveedor="Example, S.L.",
        lineas_iva=(GroundTruthIvaLine("10,00 €", "21%", "2,10 €"),),
        recargos_equivalencia=(),
        retencion_irpf=None,
        total="12,10 €",
        raw_row_totals=("12,10 €",),
        total_derivation="printed",
    )


def actual_result():
    return make_invoice(
        validity=InvoiceValidity.INVALID,
        diagnostic_type="Preticket",
        fecha="2026-02-01",
        numero_factura="001",
        nif_proveedor="unexpected-but-unscored",
        nombre_proveedor="Example SL",
        lineas_iva=(IvaLine("10", "21", "2.1"),),
        total="12.1",
    )


def test_report_saves_json_results_fields_and_informative_diagnostic_types(tmp_path):
    expected = expected_document()
    score = score_document(actual_result(), expected)
    report = LevelTwoReport(tmp_path, "invalid", "FakeExtractor", [expected])

    report.record(
        expected,
        score,
        source_path=tmp_path / expected.filename,
        duration_seconds=1.25,
        error=None,
    )
    report.finish()

    saved = json.loads((report.directory / "run.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert saved["status"] == "completed"
    assert saved["summary"]["classification"]["accuracy"] == 1
    assert saved["summary"]["extraction"]["accuracy"] == 1
    assert saved["summary"]["extraction"]["coverage"] == 7 / 8
    record = saved["records"][0]
    assert record["score"]["expected_diagnostic_type"] == "Proforma"
    assert record["score"]["obtained_diagnostic_type"] == "Preticket"
    dashboard = (report.directory / "dashboard.html").read_text(encoding="utf-8")
    assert "__REPORT_DATA__" not in dashboard
    assert "Proforma" in dashboard
    assert "Preticket" in dashboard

    with (report.directory / "results.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        result_row = next(csv.DictReader(source))
    assert result_row["classification_matches"] == "True"
    assert result_row["expected_diagnostic_type"] == "Proforma"
    assert result_row["obtained_diagnostic_type"] == "Preticket"

    with (report.directory / "fields.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        field_rows = list(csv.DictReader(source))
    assert len(field_rows) == 7
    assert "nif_proveedor" not in {row["field"] for row in field_rows}


def test_v2_image_resolution_supports_exact_names_and_unique_extensionless_stems(
    tmp_path,
):
    exact = tmp_path / "nested" / "IMG_0001.HEIC"
    extensionless = tmp_path / "special" / "Factura IRPF 1.pdf"
    exact.parent.mkdir()
    extensionless.parent.mkdir()
    exact.touch()
    extensionless.touch()

    assert _resolve_v2_image(tmp_path, "IMG_0001.HEIC") == exact
    assert _resolve_v2_image(tmp_path, "Factura IRPF 1") == extensionless
    assert _resolve_v2_image(tmp_path, "missing") == tmp_path / "missing"


def test_v2_image_resolution_rejects_ambiguous_extensionless_stems(tmp_path):
    first = tmp_path / "one" / "invoice.png"
    second = tmp_path / "two" / "invoice.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.touch()
    second.touch()

    with pytest.raises(ValueError, match="Ambiguous invoice image"):
        _resolve_v2_image(tmp_path, "invoice")


def test_level_two_runner_executes_once_and_writes_a_complete_report(
    tmp_path, monkeypatch
):
    expected = expected_document()
    image = tmp_path / "images" / expected.filename
    image.parent.mkdir()
    image.touch()
    extractor = SimpleNamespace(extract_invoice=Mock(return_value=actual_result()))
    reports = tmp_path / "reports"
    monkeypatch.setenv("OCR_IMAGE_DIR", str(image.parent))
    monkeypatch.setenv("OCR_V2_REPORT_DIR", str(reports))
    monkeypatch.setattr(benchmark, "invoice_extractor", extractor)
    monkeypatch.setattr(benchmark, "load_ground_truths_v2", lambda _path: [expected])

    benchmark.test_invoice_accuracy_v2(OcrScope.INVALID)

    extractor.extract_invoice.assert_called_once_with(str(image))
    run = next((reports / "runs").iterdir())
    saved = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["scope"] == "invalid"
    assert saved["summary"]["classification"]["accuracy"] == 1
    assert (run / "dashboard.html").is_file()
