"""Fast checks of the benchmark itself; these do not measure OCR accuracy."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.ocr.models import Invoice
from src.ocr.evidence import InvoiceEvidence, InvoiceExtraction, OcrDocument
from tests.invoice_accuracy import (
    PASS_THRESHOLD, image_category, image_index, load_ground_truths, normalize,
    resolve_image, score_invoice,
)
from tests import test_invoice_accuracy as benchmark


class InvoiceScoringTests(unittest.TestCase):
    def setUp(self):
        self.expected = Invoice(
            fecha="9/08/2022", numero_factura="001-02", nif_proveedor="A-123",
            nombre_proveedor="Example S.L.", base_imponible="1.117,04 €",
            tipo_iva="21,00%", cuota_iva="230,13 €", total="1.347,17 €",
        )

    def test_model_has_exactly_the_eight_scored_csv_fields(self):
        self.assertEqual({item.metadata["csv"] for item in fields(Invoice)}, {
            "Fecha", "Nº de factura", "NIF proveedor", "Nombre Proveedor",
            "Base Imponible", "Tipo IVA %", "Cuota IVA", "Total",
        })

    def test_equivalent_formats_receive_full_credit(self):
        actual = replace(
            self.expected, fecha="2022-08-09", nombre_proveedor="  EXAMPLE   S.L. ",
            base_imponible="1117.04", tipo_iva="21", cuota_iva="230.130", total="1347.17",
        )
        self.assertEqual(score_invoice(actual, self.expected).accuracy, 1)

    def test_text_fields_are_trimmed_and_lowercased_before_comparison(self):
        actual = replace(
            self.expected,
            numero_factura="\t001-02  ",
            nif_proveedor="  a-123\n",
            nombre_proveedor="  eXAMPLE   s.L.  ",
        )
        self.assertEqual(score_invoice(actual, self.expected).matched, 8)

    def test_each_field_is_equal_weight_and_six_of_eight_are_needed(self):
        for count in range(9):
            with self.subTest(correct=count):
                actual = replace(self.expected, **{
                    item.name: None for item in fields(Invoice)[count:]
                })
                score = score_invoice(actual, self.expected)
                self.assertEqual(score.matched, count)
                self.assertEqual(score.accuracy > PASS_THRESHOLD, count >= 6)
        self.assertFalse(0.70 > PASS_THRESHOLD)

    def test_missing_reference_field_is_still_scored(self):
        expected = replace(self.expected, nif_proveedor="")
        self.assertEqual(score_invoice(replace(expected, nif_proveedor=None), expected).matched, 8)
        self.assertEqual(score_invoice(self.expected, expected).matched, 7)

    def test_identifier_zeroes_and_numeric_errors_are_not_forgiven(self):
        actual = replace(self.expected, numero_factura="1-02", total="1347.18")
        self.assertEqual(score_invoice(actual, self.expected).mismatches, ("numero_factura", "total"))

    def test_invalid_values_count_as_wrong_fields(self):
        for value in ("NaN", "Infinity", "junk", "1,2,3"):
            with self.subTest(value=value):
                self.assertEqual(score_invoice(replace(self.expected, total=value), self.expected).matched, 7)
        with self.assertRaises(TypeError):
            score_invoice("plain OCR text", self.expected)
        with self.assertRaises(ValueError):
            normalize("fecha", "31/02/2022")

    def test_model_rejects_incorrect_types_missing_fields_and_extra_fields(self):
        with self.assertRaises(ValidationError):
            replace(self.expected, total=1347.17)
        with self.assertRaises(ValidationError):
            Invoice()
        with self.assertRaises(ValidationError):
            replace(self.expected, unexpected="extra")

    def test_real_ground_truths_load_without_recomputing_amounts(self):
        rows = load_ground_truths(Path(__file__).parent / "ground_truths" / "ground_truth_trial_invoices.csv")
        self.assertEqual(len(rows), 18)
        self.assertEqual(dict(rows)["IMG_3311.HEIC"].cuota_iva, "230,13 €")
        self.assertTrue(all(score_invoice(invoice, invoice).matched == 8 for _, invoice in rows))

    def run_benchmark_harness(self, outputs):
        # Test doubles exercise reporting/error handling only, never OCR quality.
        extractor = SimpleNamespace(extract_invoice=Mock(side_effect=outputs))
        rows = [(f"image_{index}.HEIC", self.expected) for index in range(len(outputs))]
        result = unittest.TestResult()
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as report_directory,
            patch.object(benchmark, "REPORTS_DIR", Path(report_directory)),
            patch.object(benchmark, "load_ground_truths", return_value=rows),
            patch.object(benchmark, "invoice_extractor", extractor),
            patch.object(Path, "is_file", return_value=True),
            contextlib.redirect_stdout(output),
        ):
            benchmark.InvoiceAccuracyTests("test_trial_invoices").run(result)
        return result, output.getvalue(), extractor.extract_invoice.call_count

    def test_execution_failure_counts_zero_and_remaining_rows_still_run(self):
        result, output, calls = self.run_benchmark_harness([RuntimeError("unavailable"), self.expected])
        self.assertEqual(calls, 2)
        self.assertEqual(len(result.failures), 2)  # Failed invoice and failed global score.
        self.assertFalse(result.errors)
        self.assertIn("image_0.HEIC: 0/8 = 0.00%", output)
        self.assertIn("image_1.HEIC: 8/8 = 100.00%", output)
        self.assertIn("GLOBAL: 8/16 = 50.00%", output)

    def test_passing_global_score_does_not_hide_a_failing_invoice(self):
        result, output, calls = self.run_benchmark_harness([Invoice.empty()] + [self.expected] * 3)
        self.assertEqual(calls, 4)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("GLOBAL: 24/32 = 75.00%", output)

    def test_passing_benchmark(self):
        result, output, calls = self.run_benchmark_harness([self.expected, self.expected])
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(calls, 2)
        self.assertIn("GLOBAL: 16/16 = 100.00%", output)

    def test_benchmark_saves_evidence_without_rerunning_ocr_or_reusing_previous_result(self):
        extraction = InvoiceExtraction(document=OcrDocument(pages=[]), evidence=InvoiceEvidence.empty())
        extractor = SimpleNamespace(
            extract_invoice=Mock(side_effect=AssertionError("No second extraction")),
            extract_invoice_with_evidence=Mock(side_effect=[extraction, RuntimeError("bad citation")]),
        )
        with (
            tempfile.TemporaryDirectory() as report_directory,
            patch.object(benchmark, "REPORTS_DIR", Path(report_directory)),
            patch.object(benchmark, "load_ground_truths", return_value=[
                ("one.HEIC", Invoice.empty()), ("two.HEIC", Invoice.empty()),
            ]),
            patch.object(benchmark, "invoice_extractor", extractor),
            patch.object(Path, "is_file", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = unittest.TestResult()
            benchmark.InvoiceAccuracyTests("test_trial_invoices").run(result)
            saved = json.loads(next(Path(report_directory).glob("runs/*/run.json")).read_text())
            first, second = saved["invoices"]
            self.assertEqual(first["extraction_evidence"], extraction.model_dump(mode="json"))
            self.assertEqual(first["matched"], 8)
            self.assertIsNone(second["extraction_evidence"])
            self.assertEqual(second["status"], "error")
            self.assertIn("bad citation", second["error"])
        self.assertFalse(result.errors)
        extractor.extract_invoice.assert_not_called()
        self.assertEqual(extractor.extract_invoice_with_evidence.call_count, 2)

    def test_all_real_csv_images_resolve_in_category_folders(self):
        from collections import Counter
        root = Path(__file__).parent / "images" / "trial_invoices"
        index = image_index(root)
        rows = load_ground_truths(root.parents[1] / "ground_truths" / "ground_truth_trial_invoices.csv")
        paths = [resolve_image(root, filename, index) for filename, _ in rows]
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(Counter(image_category(root, path) for path in paths),
                         {"easy": 6, "medium": 8, "hard": 3, "special_cases": 1})

    def test_categorized_harness_uses_real_paths_and_weighted_global_summary(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "images" / category / name for category, name in
                     (("easy", "one.png"), ("easy", "two.png"), ("hard", "three.png"))]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                with Image.new("RGB", (10, 10)) as image:
                    image.save(path)
            extractor = SimpleNamespace(extract_invoice=Mock(side_effect=[
                self.expected, self.expected, RuntimeError("OCR failed"),
            ]))
            output = io.StringIO()
            result = unittest.TestResult()
            with (
                patch.dict(os.environ, {"OCR_IMAGE_DIR": str(root / "images")}),
                patch.object(benchmark, "REPORTS_DIR", root / "reports"),
                patch.object(benchmark, "load_ground_truths", return_value=[(p.name, self.expected) for p in paths]),
                patch.object(benchmark, "invoice_extractor", extractor),
                contextlib.redirect_stdout(output),
            ):
                benchmark.InvoiceAccuracyTests("test_trial_invoices").run(result)
            self.assertFalse(result.errors)
            self.assertEqual([Path(call.args[0]) for call in extractor.extract_invoice.call_args_list], paths)
            self.assertIn("easy: 16/16 = 100.00%", output.getvalue())
            self.assertIn("hard: 0/8 = 0.00%", output.getvalue())
            self.assertIn("GLOBAL: 16/24 = 66.67%", output.getvalue())
