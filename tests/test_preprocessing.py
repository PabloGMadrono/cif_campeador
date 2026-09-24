"""Offline codecs, geometry, cache, fallbacks, and backend input contracts."""

import base64
import json
import os
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageOps

from src.ocr import preprocessing as prep
from src.ocr.preprocessing_models import file_hash, LocalModels, MODEL_MANIFEST


class PreprocessingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = prep.PreprocessingConfig(mode="full", cache_dir=str(self.directory / "cache"), deskew=False)
        self.image = Image.new("RGB", (400, 600), "white")
        draw = ImageDraw.Draw(self.image)
        for y in range(70, 520, 25):
            draw.text((30, y), "Invoice 12345  VAT 21%   Total 12.10", fill="black")
        self.path = self.directory / "receipt.png"
        self.image.save(self.path)
        self.addCleanup(self.image.close)
        self.models = Mock()
        self.models.detect.return_value = ([[0, 0], [399, 0], [399, 599], [0, 599]], {"corner_peak_min": .9})
        self.models.orient.return_value = (0, [.97, .01, .01, .01])

    def prepare(self, config=None, path=None):
        with patch.object(prep, "LocalModels", return_value=self.models):
            return prep.prepare_document(path or self.path, config or self.config)

    def test_off_is_pixel_identical_and_does_not_run_models(self):
        result = self.prepare(replace(self.config, mode="off"))
        with result.pages[0].open_image() as image:
            np.testing.assert_array_equal(np.array(image), np.array(self.image))
        self.models.detect.assert_not_called()
        self.models.orient.assert_not_called()

    def test_right_angle_rotations_and_matrix_match_pixels_even_low_confidence(self):
        original = np.array(self.image)
        original[30:40, 20:40] = [255, 0, 0]
        points = [[25, 35], [150, 200], [399, 599]]
        for degrees in (0, 90, 180, 270):
            with self.subTest(degrees=degrees):
                scores = [.24] * 4
                scores[degrees // 90] = .28
                self.models.orient.return_value = degrees, scores
                result, metadata = prep.prepare_page(original, replace(self.config, mode="orientation"), self.models)
                np.testing.assert_array_equal(result, np.rot90(original, degrees // 90))
                mapped = prep.transform_points(points, metadata["matrix"])
                np.testing.assert_allclose(prep.transform_points(mapped, metadata["inverse_matrix"]), points, atol=1e-8)
                self.assertIn("low_orientation_confidence", metadata["uncertainty"])
                self.models.detect.assert_not_called()

    def test_perspective_preserves_header_footer_and_natural_aspect(self):
        # Known quadrilateral: project a complete receipt onto a larger photo.
        source = np.float32([[0, 0], [399, 0], [399, 599], [0, 599]])
        polygon = np.float32([[160, 30], [510, 80], [550, 740], [100, 690]])
        forward = cv2.getPerspectiveTransform(source, polygon)
        photo = cv2.warpPerspective(np.array(self.image), forward, (700, 800), borderValue=(80, 70, 50))
        self.models.detect.return_value = polygon.tolist(), {"corner_peak_min": .9}
        output, metadata = prep.prepare_page(photo, self.config, self.models)
        mapped = prep.transform_points(prep.transform_points([[30, 70], [300, 519]], forward), metadata["matrix"])
        self.assertTrue(((mapped >= 0) & (mapped < [output.shape[1], output.shape[0]])).all())
        self.assertLess(output.shape[1] / output.shape[0], .8)
        recovered = prep.transform_points(mapped, metadata["inverse_matrix"])
        np.testing.assert_allclose(recovered, prep.transform_points([[30, 70], [300, 519]], forward), atol=1e-7)

    def test_full_resolution_transform_uses_analysis_coordinate_scale(self):
        photo = np.tile(np.array(self.image), (4, 4, 1))
        self.models.detect.return_value = [[100, 100], [650, 100], [650, 1100], [100, 1100]], {}
        _, metadata = prep.prepare_page(photo, self.config, self.models)
        self.assertEqual(self.models.detect.call_args.args[0].shape[:2], (1200, 800))
        np.testing.assert_allclose(metadata["selected_polygon"], [[200, 200], [1300, 200], [1300, 2200], [200, 2200]])

    def test_invalid_quadrilaterals_are_rejected(self):
        for points in ([], [[1, 1]] * 4, [[0, 0], [100, 0], [100, 1], [0, 1]],
                       [[0, 0], [4000, 0], [100, 100], [0, 100]],
                       [[0, 0], [100, 0], [50, 50], [0, 100]],
                       [[float("nan"), 0], [100, 0], [100, 100], [0, 100]]):
            with self.subTest(points=points):
                self.assertIsNone(prep.valid_quad(points, 400, 600))

    def test_fallback_keeps_full_page_when_no_boundary_is_usable(self):
        self.models.detect.return_value = [], {}
        with patch.object(prep, "boundary_candidates", return_value=[]):
            output, metadata = prep.prepare_page(np.array(self.image), self.config, self.models)
        self.assertEqual(metadata["selection_method"], "full_page")
        self.assertIn("no_document_boundary", metadata["uncertainty"])
        np.testing.assert_array_equal(output, np.array(self.image))
        self.models.orient.assert_called_once()

    def test_paper_boundary_fallback_rejects_internal_table_and_logo(self):
        photo = np.full((700, 600, 3), 60, np.uint8)
        cv2.rectangle(photo, (100, 40), (490, 650), (235, 235, 235), -1)
        cv2.rectangle(photo, (230, 150), (360, 280), (0, 0, 0), -1)
        cv2.rectangle(photo, (180, 380), (410, 550), (0, 0, 0), 2)
        candidates = prep.boundary_candidates(photo, self.config)
        self.assertTrue(candidates)
        self.assertGreater(candidates[0]["area_ratio"], .5)

    def test_valid_fallback_selected_automatically(self):
        self.models.detect.return_value = [], {}
        best = {"polygon": np.float32([[20, 20], [380, 20], [380, 580], [20, 580]]), "score": .25}
        with patch.object(prep, "boundary_candidates", return_value=[best]):
            _, metadata = prep.prepare_page(np.array(self.image), self.config, self.models)
        self.assertEqual(metadata["selection_method"], "opencv")
        self.assertIn("fallback_selection", metadata["uncertainty"])

    def test_blank_unchanged_but_faint_text_is_processed(self):
        blank = np.full((200, 100, 3), 255, np.uint8)
        output, metadata = prep.prepare_page(blank, self.config, self.models)
        self.assertTrue(metadata["blank"])
        np.testing.assert_array_equal(blank, output)
        self.models.orient.assert_not_called()
        blank[20:30, 20:40] = 235
        self.models.detect.return_value = [], {}
        _, metadata = prep.prepare_page(blank, self.config, self.models)
        self.assertFalse(metadata["blank"])
        self.models.orient.assert_called_once()

    def test_consistent_text_skew_only_and_expanded_canvas(self):
        self.assertEqual(prep.estimate_skew(np.full((200, 300, 3), 255, np.uint8), 5)[0], 0)
        lines = np.full((800, 1000, 3), 255, np.uint8)
        for y in range(100, 701, 55):
            for x in range(80, 881, 20):
                cv2.rectangle(lines, (x, y), (x + 7, y + 15), (0, 0, 0), -1)
        skewed, matrix = prep.deskew_image(lines, -2)
        angle, count = prep.estimate_skew(skewed, 5)
        self.assertGreaterEqual(count, 4)
        self.assertAlmostEqual(angle, 2, delta=.4)
        corners = prep.transform_points([[0, 0], [999, 0], [999, 799], [0, 799]], matrix)
        self.assertTrue((corners >= 0).all())
        self.assertTrue((corners < [skewed.shape[1], skewed.shape[0]]).all())

    def test_sub_degree_skew_is_left_unchanged(self):
        with patch.object(prep, "estimate_skew", wraps=prep.estimate_skew) as estimate:
            output, metadata = prep.prepare_page(np.array(self.image), replace(self.config, deskew=True), self.models)
        estimate.assert_called_once()
        self.assertEqual(metadata["skew_ccw"], 0)
        self.assertEqual(metadata["output_dimensions"], [400, 600])

    def test_cache_content_config_version_and_corruption(self):
        before = self.path.read_bytes()
        first = self.prepare()
        second = self.prepare()
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.models.detect.assert_called_once()
        self.assertEqual(first.cache_key, second.cache_key)
        renamed = self.path.with_name("same.png")
        renamed.write_bytes(before)
        self.assertEqual(first.cache_key, self.prepare(path=renamed).cache_key)
        changed_config = self.prepare(replace(self.config, boundary_allowance=.06))
        self.assertNotEqual(first.cache_key, changed_config.cache_key)
        with patch.object(prep, "PIPELINE_VERSION", "test-version"):
            self.assertNotEqual(first.cache_key, self.prepare().cache_key)
        with patch.object(prep, "model_versions", return_value={"orientation": {"sha256": "changed"}}):
            self.assertNotEqual(first.cache_key, self.prepare().cache_key)
        first.pages[0].image_path.write_bytes(b"corrupt cache")
        self.assertFalse(self.prepare().cache_hit)
        self.assertEqual(before, self.path.read_bytes())
        with Image.new("RGB", (10, 20), "white") as other:
            other.save(self.path)
        self.assertNotEqual(first.cache_key, self.prepare().cache_key)

    def test_partial_cache_and_capture_are_safe(self):
        with prep.capture_preparation() as outer:
            first = self.prepare()
            with prep.capture_preparation() as inner:
                self.prepare()
            self.prepare()
        self.assertEqual(len(outer), 2)
        self.assertEqual(len(inner), 1)
        (first.pages[0].image_path.parent / "metadata.json").write_text("{")
        self.assertFalse(self.prepare().cache_hit)

    def test_exif_all_eight_orientations_applied_once_with_raw_matrix(self):
        for orientation in range(1, 9):
            path = self.directory / f"exif-{orientation}.png"
            image = Image.new("RGB", (80, 120), "white")
            image.putpixel((20, 30), (255, 0, 0))
            exif = Image.Exif()
            exif[274] = orientation
            image.save(path, exif=exif)
            image.close()
            result = self.prepare(replace(self.config, mode="off"), path)
            point = np.rint(prep.transform_points([[20, 30]], result.pages[0].metadata["raw_to_prepared"])[0]).astype(int)
            with result.pages[0].open_image() as output:
                self.assertEqual(output.getpixel(tuple(point)), (255, 0, 0))
                self.assertNotIn(274, output.getexif())

    def test_transparency_and_multipage_tiff(self):
        path = self.path.with_suffix(".tiff")
        with Image.new("RGBA", (40, 60), (0, 0, 0, 0)) as a, Image.new("RGBA", (70, 30), (255, 0, 0, 255)) as b:
            a.save(path, save_all=True, append_images=[b])
        result = self.prepare(replace(self.config, mode="off"), path)
        with result.images() as images:
            self.assertEqual([im.size for im in images], [(40, 60), (70, 30)])
            self.assertEqual(images[0].getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(images[1].getpixel((0, 0)), (255, 0, 0))

    def test_pdf_image_blank_rotation_and_order(self):
        path = self.path.with_suffix(".pdf")
        path.write_bytes(prep.lossless_pdf([self.image], dpi=300))
        with pdfium.PdfDocument(str(path)) as pdf:
            page = pdf[0]
            page.set_rotation(90)
            page.close()
            pdf.new_page(72, 144).close()
            pdf.save(str(self.directory / "rotated.pdf"))
        result = self.prepare(replace(self.config, mode="off"), self.directory / "rotated.pdf")
        self.assertEqual([p.metadata["output_dimensions"] for p in result.pages], [[600, 400], [300, 600]])
        self.assertEqual(result.pages[0].metadata["pdf_rotation"], 90)
        self.assertTrue(result.pages[1].metadata["blank"])

    def test_text_pdf_rasterized_at_configured_dpi(self):
        # Minimal text PDF fixture with an embedded content stream and standard font.
        from reportlab.pdfgen.canvas import Canvas
        path = self.path.with_suffix(".pdf")
        canvas = Canvas(str(path), pagesize=(144, 216))
        canvas.drawString(10, 170, "Invoice 12345")
        canvas.save()
        result = self.prepare(replace(self.config, mode="off", pdf_dpi=150), path)
        # PDFium rounds floating point page dimensions up to whole pixels.
        width, height = result.pages[0].metadata["output_dimensions"]
        self.assertEqual(width, 300)
        self.assertIn(height, (450, 451))
        self.assertFalse(result.pages[0].metadata["blank"])

    def test_lossless_pdf_has_identical_rgb_image_streams(self):
        data = prep.lossless_pdf([self.image])
        self.assertNotIn(b"/DCTDecode", data)
        with pdfium.PdfDocument(data) as pdf, closing(pdf[0]) as page:
            obj = next(page.get_objects())
            with closing(obj.get_bitmap()) as bitmap, bitmap.to_pil() as image:
                np.testing.assert_array_equal(np.array(image.convert("RGB")), np.array(self.image))

    def test_bad_paths_and_inputs_fail_before_models(self):
        with self.assertRaises(FileNotFoundError):
            self.prepare(path=self.directory / "absent.png")
        with self.assertRaises(IsADirectoryError):
            self.prepare(path=self.directory)
        self.path.write_bytes(b"not an image")
        with self.assertRaises(Exception):
            self.prepare()
        self.models.orient.assert_not_called()

    def test_missing_weights_never_download_at_runtime(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
            with self.assertRaisesRegex(RuntimeError, "setup-models"):
                LocalModels(self.directory).orient(np.array(self.image))

    def test_all_backend_inputs_share_pixels_and_one_preparation_per_invocation(self):
        from src.ocr.ocr_openai import _document_image_urls
        from src.ocr.ocr_mistral import Ocr_mistral
        from src.ocr.ocr_qwen import Ocr_qwen
        from src.ocr.ocr_surya import Ocr_surya

        self.models.orient.return_value = 90, [.01, .97, .01, .01]
        env = {"OCR_PREPROCESSING": "orientation", "OCR_PREPROCESSING_CACHE": self.config.cache_dir}
        with patch.dict(os.environ, env), patch.object(prep, "LocalModels", return_value=self.models):
            with prep.capture_preparation() as captured:
                urls = _document_image_urls(str(self.path))
            self.assertEqual(len(captured), 1)
            with captured[0].pages[0].open_image() as image:
                expected = np.array(image)
            self.assertEqual(expected.shape[:2], (400, 600))
            with Image.open(BytesIO(base64.b64decode(urls[0].split(",")[1]))) as sent:
                np.testing.assert_array_equal(np.array(sent), expected)
            with prep.capture_preparation() as captured:
                documents = Ocr_mistral._documents(str(self.path))
            self.assertEqual(len(captured), 1)
            self.assertEqual(documents[0]["image_url"], urls[0])
            qwen = Ocr_qwen()
            def inspect(image, number):
                np.testing.assert_array_equal(np.array(image), expected)
                return "text"
            qwen._extract_image = Mock(side_effect=inspect)
            with prep.capture_preparation() as captured:
                qwen.extract_text(str(self.path))
            self.assertEqual(len(captured), 1)
            surya = Ocr_surya()
            def recognize(images):
                np.testing.assert_array_equal(np.array(images[0]), expected)
                return [SimpleNamespace(blocks=[], image_bbox=[0., 0., 600., 400.])]
            surya._recognition_predictor = Mock(side_effect=recognize)
            with prep.capture_preparation() as captured:
                document = surya.extract_document(str(self.path))
            self.assertEqual(len(captured), 1)
            self.assertEqual(document.pages[0].preprocessing["rotation_ccw"], 90)
            self.models.orient.assert_called_once()  # Across all four backends.

    def test_mistral_enabled_pdf_is_raster_and_off_retains_native_bytes(self):
        from src.ocr.ocr_mistral import Ocr_mistral
        path = self.path.with_suffix(".pdf")
        path.write_bytes(prep.lossless_pdf([self.image, self.image]))
        env = {"OCR_PREPROCESSING": "orientation", "OCR_PREPROCESSING_CACHE": self.config.cache_dir}
        self.models.orient.return_value = 90, [.01, .97, .01, .01]
        with patch.dict(os.environ, env), patch.object(prep, "LocalModels", return_value=self.models):
            document = Ocr_mistral._documents(str(path))[0]
            payload = base64.b64decode(document["document_url"].split(",")[1])
            self.assertNotEqual(payload, path.read_bytes())
            with pdfium.PdfDocument(payload) as pdf:
                self.assertEqual(len(pdf), 2)
            with patch.dict(os.environ, OCR_PREPROCESSING="off"):
                original = Ocr_mistral._documents(str(path))[0]
            self.assertEqual(base64.b64decode(original["document_url"].split(",")[1]), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
