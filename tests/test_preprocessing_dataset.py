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

import cv2
import numpy as np

from src.ocr.preprocessing import (
    PreprocessingConfig,
    prepare_document,
    transform_points,
)
from src.ocr.preprocessing_models import MODEL_MANIFEST, file_hash
from tests.invoice_accuracy_v2 import image_index, resolve_image

TESTS = Path(__file__).resolve().parent
IMAGES = TESTS / "images/trial_invoices"
ANNOTATIONS = TESTS / "ground_truths/preprocessing.json"
MODELS = Path(__file__).resolve().parents[1] / ".ocr_preprocessing/models"


def assess(page, annotation):
    """Measure document selection and orientation against annotated geometry."""
    width, height = page["original_dimensions"]
    scale = np.array([width - 1, height - 1])
    boundary = np.float32(annotation["polygon_normalized"]) * scale
    selected = page["selected_polygon"] or [
        [0, 0],
        [width - 1, 0],
        [width - 1, height - 1],
        [0, height - 1],
    ]
    expected_hull = cv2.convexHull(np.float32(boundary))
    selected_hull = cv2.convexHull(np.float32(selected))
    intersection, _ = cv2.intersectConvexConvex(expected_hull, selected_hull)
    union = (
        cv2.contourArea(expected_hull)
        + cv2.contourArea(selected_hull)
        - intersection
    )
    center = boundary.mean(axis=0)
    vectors = {0: [0, -1], 90: [1, 0], 180: [0, 1], 270: [-1, 0]}
    end = center + np.array(vectors[annotation["upright_rotation_ccw"]]) * 50
    mapped_direction = transform_points([center, end], page["matrix"])
    delta = mapped_direction[1] - mapped_direction[0]
    upright_error = float(abs(np.degrees(np.arctan2(delta[0], -delta[1]))))
    text_extent = np.array(
        annotation.get("text_extent_normalized", annotation["polygon_normalized"])
    ) * scale
    mapped_extent = transform_points(text_extent, page["matrix"])
    output_width, output_height = page["output_dimensions"]
    contained = bool(
        (
            (mapped_extent >= [-2, -2])
            & (mapped_extent <= [output_width + 1, output_height + 1])
        ).all()
    )
    return {
        "boundary_iou": float(intersection / union),
        "text_extent_inside": contained,
        "wrong_rotation": upright_error > 45,
    }


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
