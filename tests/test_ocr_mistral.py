"""Exercise the Mistral SDK with an offline HTTP transport."""

import base64
import json
import os
import tempfile
import unittest
from dataclasses import asdict, fields
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
import pypdfium2 as pdfium
from mistralai.client import Mistral
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from src.ocr.preprocessing import lossless_pdf
from src.ocr.models import Invoice
from src.ocr.ocr_mistral import Ocr_mistral
from src.ocr.ocr_openai import IMAGE_INVOICE_INSTRUCTIONS


class MistralOcrTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, OCR_PREPROCESSING="off")
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "invoice.png"
        with Image.new("RGB", (20, 30), "white") as image:
            image.save(self.path)
        self.response = {
            "pages": [{"index": 0, "markdown": "Factura Nº 001\nTotal: 12,10 €", "images": [], "dimensions": None}],
            "model": "mistral-ocr-latest",
            "usage_info": {"pages_processed": 1},
            "document_annotation": json.dumps(asdict(Invoice.unreadable())),
        }
        self.requests = []
        self.status = 200

        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.response)

        transport = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(transport.close)
        self.ocr = Ocr_mistral()
        self.ocr._ocr_client = Mistral(api_key="offline-key", client=transport)

    def test_image_request_and_markdown(self):
        self.assertEqual(self.ocr.extract_text(str(self.path)), self.response["pages"][0]["markdown"])
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.mistral.ai/v1/ocr")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "mistral-ocr-latest")
        self.assertTrue(body["include_image_base64"])
        self.assertEqual(body["document"]["type"], "image_url")
        url = body["document"]["image_url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
            self.assertEqual(image.size, (20, 30))

    def test_pdf_sent_intact_and_all_response_pages_joined(self):
        path = self.path.with_suffix(".PDF")
        content = lossless_pdf([Image.new("RGB", (100, 200), "white")])
        path.write_bytes(content)
        self.response["pages"].append({"index": 1, "markdown": "Second page", "images": [], "dimensions": None})
        result = self.ocr.extract_text(str(path))
        self.assertTrue(result.endswith("\n\nSecond page"))
        self.assertEqual(len(self.requests), 1)
        document = json.loads(self.requests[0].content)["document"]
        self.assertEqual(document["type"], "document_url")
        self.assertEqual(document["document_url"], "data:application/pdf;base64," + base64.b64encode(content).decode())

    def test_heic_and_multipage_tiff(self):
        register_heif_opener(thumbnails=False)
        for suffix in (".HEIC", ".tiff"):
            with self.subTest(suffix=suffix):
                path = self.path.with_suffix(suffix)
                with Image.new("RGB", (32, 48), "white") as first:
                    with Image.new("RGB", (48, 32), "white") as second:
                        first.save(path, save_all=True, append_images=[second] if suffix == ".tiff" else [])
                self.requests.clear()
                self.ocr.extract_text(str(path))
                self.assertEqual(len(self.requests), 1)

    def test_invoice_is_annotated_in_one_call_without_shared_parser(self):
        values = asdict(Invoice.unreadable())
        values.update(numero_factura="000123", nombre_proveedor="Compañía S.L.", total="12.10")
        self.response["document_annotation"] = json.dumps(values)
        with (
            patch.object(self.ocr, "parse_invoice", side_effect=AssertionError("Shared parser called")),
            patch.object(self.ocr, "extract_text", side_effect=AssertionError("Separate OCR called")),
        ):
            result = self.ocr.extract_invoice(str(self.path))
        self.assertIs(type(result), Invoice)
        self.assertEqual(asdict(result), values)
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["document_annotation_prompt"], IMAGE_INVOICE_INSTRUCTIONS)
        format_ = body["document_annotation_format"]
        self.assertEqual(format_["type"], "json_schema")
        self.assertTrue(format_["json_schema"]["strict"])
        schema = format_["json_schema"]["schema"]
        names = {field.name for field in fields(Invoice)}
        self.assertEqual(set(schema["required"]), names)
        self.assertEqual(set(schema["properties"]), names)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["lineas_iva"]["type"], "array")
        self.assertIn("$defs", schema)

    def test_invoice_multipage_images_are_one_pdf_in_page_order(self):
        path = self.path.with_suffix(".tiff")
        with Image.new("RGB", (32, 48), "white") as first:
            with Image.new("RGB", (48, 32), "black") as second:
                first.save(path, save_all=True, append_images=[second])
        self.ocr.extract_invoice(str(path))
        self.assertEqual(len(self.requests), 1)
        document = json.loads(self.requests[0].content)["document"]
        self.assertEqual(document["type"], "document_url")
        data = base64.b64decode(document["document_url"].split(",", 1)[1])
        with pdfium.PdfDocument(data) as pdf:
            self.assertEqual(len(pdf), 2)
            for actual, expected in zip([pdf.get_page_size(i) for i in range(2)], [(7.68, 11.52), (11.52, 7.68)]):
                for value, target in zip(actual, expected):
                    self.assertAlmostEqual(value, target, places=4)

    def test_invoice_pdf_is_sent_intact(self):
        path = self.path.with_suffix(".pdf")
        content = lossless_pdf([Image.new("RGB", (100, 200), "white")])
        path.write_bytes(content)
        self.ocr.extract_invoice(str(path))
        self.assertEqual(len(self.requests), 1)
        document = json.loads(self.requests[0].content)["document"]
        self.assertEqual(base64.b64decode(document["document_url"].split(",", 1)[1]), content)

    def test_missing_or_invalid_annotations_fail_without_fallback(self):
        values = asdict(Invoice.unreadable())
        for annotation in (None, "", "not JSON", "null", "{}",
                           json.dumps({**values, "total": 12}),
                           json.dumps({**values, "extra": "unexpected"})):
            with self.subTest(annotation=annotation):
                self.response["document_annotation"] = annotation
                with patch.object(self.ocr, "parse_invoice", side_effect=AssertionError("Fallback")):
                    with self.assertRaises(RuntimeError):
                        self.ocr.extract_invoice(str(self.path))

    def test_explicit_unreadable_result_is_returned(self):
        self.response["pages"][0]["markdown"] = ""
        self.assertEqual(self.ocr.extract_invoice(str(self.path)), Invoice.unreadable())

    def test_missing_pages_are_an_error(self):
        self.response["pages"] = []
        with self.assertRaisesRegex(RuntimeError, "no pages"):
            self.ocr.extract_text(str(self.path))

    def test_malformed_response_is_rejected_by_sdk(self):
        from mistralai.client.errors import ResponseValidationError

        del self.response["pages"][0]["markdown"]
        with self.assertRaises(ResponseValidationError):
            self.ocr.extract_text(str(self.path))

    def test_invalid_local_input_never_calls_api(self):
        for path, error in ((self.path.parent / "missing.png", FileNotFoundError),
                            (self.path.parent, IsADirectoryError)):
            with self.assertRaises(error):
                self.ocr.extract_text(str(path))
        self.path.write_bytes(b"not an image")
        with self.assertRaises(UnidentifiedImageError):
            self.ocr.extract_text(str(self.path))
        self.assertEqual(self.requests, [])

    def test_credentials_are_lazy_and_client_is_reused(self):
        with patch("src.ocr.ocr_mistral.MISTRAL_API_KEY", " "):
            ocr = Ocr_mistral()
            with self.assertRaisesRegex(RuntimeError, "MISTRAL_API_KEY"):
                ocr.extract_text(str(self.path))
        with patch("src.ocr.ocr_mistral.MISTRAL_API_KEY", "offline-key"):
            with patch("mistralai.client.Mistral") as factory:
                ocr = Ocr_mistral()
                self.assertIs(ocr._ocr_client, ocr._ocr_client)
                factory.assert_called_once_with(api_key="offline-key", timeout_ms=120_000)

    def test_api_errors_propagate(self):
        from mistralai.client.errors import SDKError

        self.status = 401
        self.response = {"message": "Unauthorized"}
        with self.assertRaises(SDKError):
            self.ocr.extract_text(str(self.path))


if __name__ == "__main__":
    unittest.main()
