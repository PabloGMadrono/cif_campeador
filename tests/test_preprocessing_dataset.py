"""Fast regression gate for the two large invoices that previously lost text.

Uses the local annotated diagnostic photos and pinned CPU models. The first run
warms the shared cache; later runs normally complete in well under a second.
The module skips cleanly when the private diagnostic images or setup models are
not available in another checkout.
"""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from src.ocr.preprocessing import PreprocessingConfig, prepare_document
from src.ocr.preprocessing_models import MODEL_MANIFEST, file_hash
from tests.invoice_accuracy import image_index, resolve_image
from tests.preprocessing_benchmark import assess


TESTS = Path(__file__).resolve().parent
IMAGES = TESTS / "images/trial_invoices"
ANNOTATIONS = TESTS / "ground_truths/preprocessing.json"
MODELS = Path(__file__).resolve().parents[1] / ".ocr_preprocessing/models"


@unittest.skipUnless(IMAGES.exists() and ANNOTATIONS.exists() and all(
    (MODELS / spec["file"]).exists() for spec in MODEL_MANIFEST.values()
), "local diagnostic images and setup models are required")
class LargeInvoicePreprocessingRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.annotations = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))["images"]
        cls.index = image_index(IMAGES)
        cls.config = replace(
            PreprocessingConfig.from_env(),
            mode="full",
            deskew=True,
            cache_dir=str(TESTS / "results/preprocessing/_cache"),
        )

    def assertLargeInvoicePreserved(self, filename):
        path = resolve_image(IMAGES, filename, self.index)
        annotation = self.annotations[filename]
        if annotation.get("source_sha256") is not None:
            self.assertEqual(file_hash(path), annotation["source_sha256"])
        document = prepare_document(path, self.config)
        page = document.pages[0].metadata
        result = assess(page, annotation)
        self.assertEqual(page["selection_method"], "docaligner")
        self.assertTrue(page["quality"]["recovered_weak_corners"])
        self.assertFalse(result["wrong_rotation"])
        self.assertTrue(result["text_extent_inside"])
        self.assertGreaterEqual(result["boundary_iou"], .80)
        self.assertEqual(page["skew_ccw"], 0)

    def test_3309_keeps_totals_and_footer(self):
        self.assertLargeInvoicePreserved("IMG_3309.HEIC")

    def test_3311_keeps_header_and_bottom_tax_rows(self):
        self.assertLargeInvoicePreserved("IMG_3311.HEIC")


if __name__ == "__main__":
    unittest.main()
