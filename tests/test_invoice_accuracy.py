"""Black-box invoice acceptance tests: real images in, Invoice models out."""

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from src.ocr import invoice_extractor
from tests.invoice_accuracy import INVOICE_FIELDS, PASS_THRESHOLD, load_ground_truths, score_invoice
from tests.invoice_report import DEFAULT_REPORTS_DIR, ExecutionReport, summarize_categories


TESTS = Path(__file__).resolve().parent
REPORTS_DIR = Path(os.environ.get("OCR_REPORT_DIR", DEFAULT_REPORTS_DIR))


@contextmanager
def report_progress(filename: str, index: int, total: int):
    """Report elapsed time while the public extraction call is running."""
    started = monotonic()
    finished = Event()
    print(f"[{index}/{total}] Starting {filename}", flush=True)

    def heartbeat():
        while not finished.wait(30):
            print(f"[{index}/{total}] {filename}: still processing "
                  f"({monotonic() - started:.0f}s elapsed)", flush=True)

    reporter = Thread(target=heartbeat, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        finished.set()
        reporter.join()
        print(f"[{index}/{total}] {filename}: execution ended after "
              f"{monotonic() - started:.1f}s", flush=True)


class InvoiceAccuracyTests(unittest.TestCase):
    def test_trial_invoices(self):
        rows = load_ground_truths(TESTS / "ground_truths" / "ground_truth_trial_invoices.csv")
        image_directory = Path(os.environ.get(
            "OCR_IMAGE_DIR", str(TESTS / "images" / "trial_invoices")
        ))
        extractor_name = f"{type(invoice_extractor).__module__}.{type(invoice_extractor).__qualname__}"
        print(f"\nDashboard: {(REPORTS_DIR / 'dashboard.html').resolve()}", flush=True)
        with ExecutionReport(rows, image_directory, REPORTS_DIR, extractor_name) as report:
            matched_total = 0
            fields_per_invoice = len(INVOICE_FIELDS)
            print(f"\nInvoice accuracy (required > {PASS_THRESHOLD:.0%}):", flush=True)
            for index, (filename, expected) in enumerate(rows, start=1):
                matched = 0
                diagnostic = ""
                actual = score = extraction_error = None
                report.start(filename)
                started = monotonic()
                try:
                    image_path = image_directory / report.records[filename]["source_path"]
                    if not image_path.is_file():
                        raise FileNotFoundError(f"Invoice image not found: {image_path}")
                    # Only the image path crosses the public OCR boundary.
                    with report_progress(filename, index, len(rows)):
                        actual = invoice_extractor.extract_invoice(str(image_path))
                    score = score_invoice(actual, expected)
                    matched = score.matched
                    diagnostic = "; ".join(
                        f"{name}: expected {getattr(expected, name)!r}, got {getattr(actual, name)!r}"
                        for name in score.mismatches
                    )
                except Exception as error:
                    # An execution error scores zero but must not hide later rows.
                    extraction_error = diagnostic = f"{type(error).__name__}: {error}"
                report.record(filename, actual, score, extraction_error, monotonic() - started)
                matched_total += matched
                accuracy = matched / fields_per_invoice
                print(f"{filename}: {matched}/{fields_per_invoice} = {accuracy:.2%}"
                      + (f" | {diagnostic}" if diagnostic else ""), flush=True)
                with self.subTest(image=filename):
                    self.assertGreater(accuracy, PASS_THRESHOLD, diagnostic)

            field_total = len(rows) * fields_per_invoice
            global_accuracy = matched_total / field_total
            print("\nAccuracy by category:", flush=True)
            for category in summarize_categories(report.data):
                print(f"{category['category']}: {category['correct_fields']}/{category['scored_fields']} "
                      f"= {category['accuracy_pct']:.2f}% "
                      f"({category['images_scored']}/{category['images_total']} invoices)", flush=True)
            print(f"GLOBAL: {matched_total}/{field_total} = {global_accuracy:.2%}", flush=True)
            with self.subTest(scope="global"):
                self.assertGreater(global_accuracy, PASS_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
