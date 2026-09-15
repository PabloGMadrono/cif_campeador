"""Persistence, interruption and escaping checks for local benchmark reports."""

import csv
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.ocr.models import Invoice
from tests.invoice_accuracy import score_invoice
from tests.invoice_report import ExecutionReport, atomic_write, prepare_dashboard, refresh_reports, summarize_categories


class InvoiceReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        with Image.new("RGB", (100, 160), "white") as image:
            image.save(self.images / "one.png")
            image.save(self.images / "two.png")
        self.output = self.root / "reports"
        self.expected = Invoice(
            fecha="2022-09-09", numero_factura="000123", nif_proveedor="B1234",
            nombre_proveedor="Compañía S.L.", base_imponible="10.00",
            tipo_iva="21", cuota_iva="2.10", total="12.10",
        )
        self.rows = [(name, self.expected) for name in ("one.png", "two.png")]

    def read_csv(self, path):
        with path.open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))

    def test_preparation_previews_survive_cache_removal_and_extraction_errors(self):
        from src.ocr.preprocessing import PreprocessingConfig, prepare_document
        document = prepare_document(self.images / "one.png", PreprocessingConfig(cache_dir=str(self.root / "cache")))
        with ExecutionReport(self.rows, self.images, self.output, "example.Extractor") as report:
            report.record("one.png", None, None, "OCR failed", 1, prepared=document)
            saved = json.loads((report.run_directory / "run.json").read_text())
            page = saved["invoices"][0]["preprocessing"]["pages"][0]
            for kind in ("original", "prepared"):
                preview = self.output / page[kind + "_preview"]
                self.assertTrue(preview.is_file())
                with Image.open(preview) as image:
                    self.assertEqual(image.size, (100, 160))
            self.assertEqual(page["matrix"], [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])
            self.assertIn("OCR failed", saved["invoices"][0]["error"])

    def test_snapshots_and_csv_preserve_matches_values_and_errors(self):
        with ExecutionReport(self.rows, self.images, self.output, "example.Extractor") as report:
            report.start("one.png")
            actual = replace(self.expected, numero_factura="999")
            report.record("one.png", actual, score_invoice(actual, self.expected), None, 2.5)
            partial = json.loads((report.run_directory / "run.json").read_text())
            self.assertEqual(partial["status"], "running")
            self.assertEqual(partial["invoices"][0]["accuracy_pct"], 87.5)
            report.start("two.png")
            report.record("two.png", None, None, "Request timed out", 600)
        summary = self.read_csv(self.output / "executions.csv")[0]
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["outcome"], "failed")
        self.assertEqual(float(summary["accuracy_pct"]), 43.75)
        records = self.read_csv(report.run_directory / "results.csv")
        self.assertEqual(records[1]["status"], "error")
        self.assertEqual(records[1]["error"], "Request timed out")
        details = self.read_csv(report.run_directory / "fields.csv")
        number = next(row for row in details if row["filename"] == "one.png" and row["field"] == "numero_factura")
        self.assertEqual(number["expected"], "000123")
        self.assertEqual(number["obtained"], "999")
        self.assertEqual(number["matches"], "False")
        self.assertEqual(len(details), 16)
        self.assertTrue(all(row["matches"] == "False" for row in details if row["filename"] == "two.png"))
        self.assertTrue((report.run_directory / "images" / "001.jpg").is_file())

    def test_interruption_preserves_partial_run_and_leaves_unfinished_unscored(self):
        with self.assertRaises(KeyboardInterrupt):
            with ExecutionReport(self.rows, self.images, self.output, "example.Extractor") as report:
                report.start("one.png")
                report.record("one.png", self.expected, score_invoice(self.expected, self.expected), None, 1)
                report.start("two.png")
                raise KeyboardInterrupt
        saved = json.loads((report.run_directory / "run.json").read_text())
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["invoices"][1]["status"], "interrupted")
        self.assertIsNone(saved["invoices"][1]["accuracy_pct"])
        summary = self.read_csv(self.output / "executions.csv")[0]
        self.assertEqual(summary["images_scored"], "1")
        self.assertEqual(summary["outcome"], "incomplete")
        self.assertEqual(float(summary["accuracy_pct"]), 100)

    def test_concurrent_executions_keep_separate_history(self):
        def execute(_):
            with ExecutionReport(self.rows[:1], self.images, self.output, "example.Extractor") as report:
                report.start("one.png")
                report.record("one.png", self.expected, score_invoice(self.expected, self.expected), None, 1)
            return report.run_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(execute, range(2)))
        self.assertEqual(len(set(ids)), 2)
        history = self.read_csv(self.output / "executions.csv")
        self.assertEqual({row["run_id"] for row in history}, set(ids))
        self.assertTrue(all(row["outcome"] == "passed" for row in history))

    def test_document_text_cannot_escape_html_or_become_a_csv_formula(self):
        dangerous = '</script><script>alert("invoice")</script>'
        expected = replace(self.expected, nombre_proveedor=dangerous)
        actual = replace(expected, numero_factura='=HYPERLINK("https://example.invalid")')
        with ExecutionReport([("one.png", expected)], self.images, self.output, "example.Extractor") as report:
            report.start("one.png")
            report.record("one.png", actual, score_invoice(actual, expected), None, 1)
        self.assertNotIn(dangerous, (self.output / "dashboard.html").read_text(encoding="utf-8"))
        details = self.read_csv(report.run_directory / "fields.csv")
        number = next(row for row in details if row["field"] == "numero_factura")
        self.assertTrue(number["obtained"].startswith("'="))
        saved = json.loads((report.run_directory / "run.json").read_text())
        self.assertEqual(saved["invoices"][0]["obtained"]["numero_factura"], actual.numero_factura)
        self.assertEqual(saved["invoices"][0]["expected"]["nombre_proveedor"], dangerous)

    def test_dataset_preview_never_creates_a_fake_execution(self):
        dashboard = prepare_dashboard(self.rows, self.images, self.output)
        self.assertTrue(dashboard.is_file())
        self.assertFalse((self.output / "executions.csv").exists())
        preview = json.loads((self.output / "preview" / "run.json").read_text())
        self.assertEqual(preview["status"], "ready")
        self.assertTrue(all(row["obtained"] is None for row in preview["invoices"]))
        self.assertTrue(all(row["accuracy_pct"] is None for row in preview["invoices"]))

    def test_evidence_survives_refresh_and_block_html_cannot_escape_dashboard(self):
        dangerous = '</script><script>alert("block")</script>'
        evidence = {
            "document": {"pages": [{"page_number": 1, "blocks": [{
                "block_id": "p1_b1", "html": dangerous, "text": "CIF B1234",
            }]}]},
            "evidence": {"nif_proveedor": {"value": "B1234", "sources": [{
                "block_id": "p1_b1", "printed_text": "CIF B1234",
            }]}},
        }
        with ExecutionReport(self.rows[:1], self.images, self.output, "surya") as report:
            report.start("one.png")
            report.record("one.png", self.expected, score_invoice(self.expected, self.expected),
                          None, 1, evidence=evidence)
        self.assertTrue(refresh_reports(self.output))
        saved = json.loads((report.run_directory / "run.json").read_text())
        self.assertEqual(saved["invoices"][0]["extraction_evidence"], evidence)
        self.assertEqual(saved["invoices"][0]["matched"], 8)
        self.assertNotIn(dangerous, (self.output / "dashboard.html").read_text(encoding="utf-8"))
        self.assertEqual(len(self.read_csv(report.run_directory / "fields.csv")), 8)

    def test_missing_image_keeps_a_reportable_record(self):
        with ExecutionReport([("missing.png", self.expected)], self.images, self.output, "example.Extractor") as report:
            self.assertIsNone(report.data["invoices"][0]["image"])
            self.assertIn("Preview unavailable", report.data["invoices"][0]["preview_error"])
            report.start("missing.png")
            report.record("missing.png", None, None, "Missing image", 0)
        self.assertEqual(self.read_csv(self.output / "executions.csv")[0]["accuracy_pct"], "0.0")

    def test_atomic_write_retries_temporary_windows_file_locks(self):
        target = self.root / "dashboard.html"
        target.write_text("previous report", encoding="utf-8")
        original_replace = Path.replace
        attempts = 0

        def briefly_locked(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                self.assertEqual(target.read_text(), "previous report")
                raise PermissionError("Windows reader holds the destination")
            return original_replace(source, destination)

        with patch.object(Path, "replace", briefly_locked), patch("tests.invoice_report.sleep") as pause:
            atomic_write(target, "new complete report")
        self.assertEqual(attempts, 3)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(target.read_text(), "new complete report")
        self.assertFalse(list(self.root.glob(".report-*.tmp")))

    def test_locked_dashboard_and_csv_do_not_abort_execution_and_can_be_rebuilt(self):
        prepare_dashboard(self.rows, self.images, self.output)
        previous_html = (self.output / "dashboard.html").read_text(encoding="utf-8")
        original_replace = Path.replace

        def locked_exports(source, destination):
            if Path(destination).name in {"dashboard.html", "results.csv", "executions.csv"}:
                raise PermissionError("Another process holds the destination")
            return original_replace(source, destination)

        with (
            patch.object(Path, "replace", locked_exports),
            patch("tests.invoice_report.sleep"),
            self.assertLogs("tests.invoice_report", level="WARNING") as warnings,
        ):
            with ExecutionReport(self.rows, self.images, self.output, "example.Extractor") as report:
                for filename, expected in self.rows:
                    report.start(filename)
                    report.record(filename, expected, score_invoice(expected, expected), None, 1)
        snapshot_path = report.run_directory / "run.json"
        before = snapshot_path.read_bytes()
        snapshot = json.loads(before)
        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(all(row["matched"] == 8 for row in snapshot["invoices"]))
        self.assertIn("Report refresh deferred", warnings.output[0])
        self.assertEqual((self.output / "dashboard.html").read_text(encoding="utf-8"), previous_html)
        self.assertFalse(list(self.output.rglob(".report-*.tmp")))

        self.assertTrue(refresh_reports(self.output))
        self.assertEqual(snapshot_path.read_bytes(), before)
        self.assertEqual(self.read_csv(self.output / "executions.csv")[0]["outcome"], "passed")
        self.assertEqual(len(self.read_csv(report.run_directory / "results.csv")), 2)
        self.assertIn(report.run_id, (self.output / "dashboard.html").read_text(encoding="utf-8"))

    def test_categories_persist_and_errors_count_zero_while_pending_are_unscored(self):
        for category, name in (("easy", "one.png"), ("hard", "two.png")):
            (self.images / category).mkdir()
            (self.images / name).rename(self.images / category / name)
        report = ExecutionReport(self.rows, self.images, self.output, "example.Extractor")
        report.start("one.png")
        report.record("one.png", self.expected, score_invoice(self.expected, self.expected), None, 1)
        categories = {r["category"]: r for r in summarize_categories(report.data)}
        self.assertEqual(categories["easy"]["accuracy_pct"], 100)
        self.assertIsNone(categories["hard"]["accuracy_pct"])
        self.assertEqual(categories["hard"]["images_scored"], 0)
        report.record("two.png", None, None, "timeout", 1)
        report.__exit__(None, None, None)
        self.assertEqual(report.records["one.png"]["source_path"], "easy/one.png")
        self.assertTrue((self.output / report.records["one.png"]["image"]).is_file())
        categories = self.read_csv(report.run_directory / "categories.csv")
        self.assertEqual([r["category"] for r in categories], ["easy", "hard"])
        self.assertEqual([float(r["accuracy_pct"]) for r in categories], [100, 0])
        self.assertEqual(self.read_csv(self.output / "executions.csv")[0]["accuracy_pct"], "50.0")
        self.assertEqual(self.read_csv(self.output / "categories.csv"), categories)
        self.assertEqual(self.read_csv(report.run_directory / "results.csv")[1]["category"], "hard")

    def test_duplicate_basenames_fail_instead_of_selecting_an_arbitrary_image(self):
        (self.images / "easy").mkdir()
        (self.images / "easy" / "one.png").write_bytes((self.images / "one.png").read_bytes())
        with self.assertRaisesRegex(ValueError, "Ambiguous invoice image one.png"):
            ExecutionReport(self.rows, self.images, self.output, "example.Extractor")

    def test_refresh_adds_legacy_categories_without_changing_predictions_or_scores(self):
        with ExecutionReport(self.rows, self.images, self.output, "example.Extractor") as report:
            for filename, expected in self.rows:
                report.record(filename, expected, score_invoice(expected, expected), None, 1)
        path = report.run_directory / "run.json"
        original = json.loads(path.read_text())
        for row in original["invoices"]:
            row.pop("category")
            row.pop("category_source")
        path.write_text(json.dumps(original), encoding="utf-8")
        (self.images / "medium").mkdir()
        (self.images / "one.png").rename(self.images / "medium" / "one.png")
        refresh_reports(self.output, self.images)
        updated = json.loads(path.read_text())
        self.assertEqual(updated["invoices"][0]["category"], "medium")
        for row in updated["invoices"]:
            self.assertEqual(row.pop("category_source"), "current_layout")
            row.pop("category")
        self.assertEqual(updated, original)
        # Once assigned, categories are snapshots, even if files move later.
        (self.images / "medium" / "one.png").rename(self.images / "one.png")
        refresh_reports(self.output, self.images)
        self.assertEqual(json.loads(path.read_text())["invoices"][0]["category"], "medium")
