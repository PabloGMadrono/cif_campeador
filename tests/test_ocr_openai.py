"""Verify direct vision extraction using the real SDK and an offline transport."""

import base64
import json
import tempfile
import unittest
from dataclasses import asdict, fields
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
import pypdfium2 as pdfium
from openai import AuthenticationError, OpenAI
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from src.ocr.models import Invoice
from src.ocr.ocr_openai import Ocr_openai


class OpenAIOcrTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "invoice.png"
        with Image.new("RGB", (20, 30), "white") as image:
            image.save(self.path)
        self.expected = Invoice(
            fecha="2022-08-09", numero_factura="000123", nif_proveedor=None,
            nombre_proveedor="Compañía S.L.", base_imponible="10.00",
            tipo_iva="21", cuota_iva="2.10", total="12.10",
        )
        self.response = {
            "id": "resp_test", "object": "response", "created_at": 0,
            "model": "gpt-5-mini", "status": "completed",
            "output": [{
                "id": "msg_test", "type": "message", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": json.dumps(asdict(self.expected)),
                    "annotations": [],
                }],
            }],
        }
        self.status_code = 200
        self.requests = []

        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status_code, json=self.response)

        self.client = OpenAI(
            api_key="offline-test-key", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        self.addCleanup(self.client.close)
        self.ocr = Ocr_openai()
        self.ocr._client = self.client

    def set_output(self, text):
        self.response["output"][0]["content"][0]["text"] = text

    def sent_images(self):
        body = json.loads(self.requests[-1].content)
        images = []
        for content in body["input"][0]["content"]:
            self.assertEqual(content["type"], "input_image")
            self.assertEqual(content["detail"], "high")
            url = content["image_url"]
            self.assertTrue(url.startswith("data:image/png;base64,"))
            image = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1], validate=True)))
            self.addCleanup(image.close)
            images.append(image)
        return images

    def test_invoice_is_extracted_in_one_structured_vision_call(self):
        with (
            patch.object(self.ocr, "extract_text", side_effect=AssertionError("Separate OCR")),
            patch.object(self.ocr, "parse_invoice", side_effect=AssertionError("Separate parse")),
        ):
            result = self.ocr.extract_invoice(str(self.path))
        self.assertIs(type(result), Invoice)
        self.assertEqual(result, self.expected)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url), "https://api.openai.com/v1/responses")
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertFalse(body["store"])
        self.assertEqual(self.sent_images()[0].size, (20, 30))
        format_ = body["text"]["format"]
        self.assertEqual(format_["type"], "json_schema")
        self.assertTrue(format_["strict"])
        schema = format_["schema"]
        names = {field.name for field in fields(Invoice)}
        self.assertEqual(set(schema["required"]), names)
        self.assertEqual(set(schema["properties"]), names)
        self.assertFalse(schema["additionalProperties"])
        for property_ in schema["properties"].values():
            self.assertEqual({branch["type"] for branch in property_["anyOf"]}, {"string", "null"})

    def test_heic_is_converted_to_base64_png(self):
        register_heif_opener(thumbnails=False)
        path = self.path.with_suffix(".HEIC")
        with Image.new("RGB", (32, 48), "white") as image:
            image.save(path)
        self.assertEqual(self.ocr.extract_invoice(str(path)), self.expected)
        self.assertEqual(self.sent_images()[0].size, (32, 48))

    def test_orientation_and_transparency_are_preserved(self):
        with Image.new("RGBA", (20, 30), (0, 0, 0, 0)) as image:
            image.putpixel((0, 0), (0, 0, 0, 255))
            exif = Image.Exif()
            exif[274] = 6
            image.save(self.path, exif=exif)
        self.ocr.extract_invoice(str(self.path))
        sent = self.sent_images()[0]
        self.assertEqual(sent.size, (30, 20))
        self.assertEqual(sent.getpixel((29, 0)), (0, 0, 0))
        self.assertEqual(sent.getpixel((0, 0)), (255, 255, 255))

    def test_all_pdf_pages_are_sent_in_order_in_one_call(self):
        path = self.path.with_suffix(".PDF")
        with pdfium.PdfDocument.new() as document:
            for size in ((72, 144), (144, 72)):
                page = document.new_page(*size)
                page.close()
            document.save(str(path))
        self.assertEqual(self.ocr.extract_invoice(str(path)), self.expected)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([image.size for image in self.sent_images()], [(300, 600), (600, 300)])

    def test_all_tiff_frames_are_sent_in_one_call(self):
        path = self.path.with_suffix(".tiff")
        with Image.new("RGB", (10, 15), "white") as first:
            with Image.new("RGB", (30, 25), "white") as second:
                first.save(path, save_all=True, append_images=[second])
        self.ocr.extract_invoice(str(path))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([image.size for image in self.sent_images()], [(10, 15), (30, 25)])

    def test_explicit_text_extraction_is_a_single_vision_call(self):
        for text in ("Factura 000123\nEspaña: acción y niñez\nTotal 12,10 €", ""):
            with self.subTest(text=text):
                self.requests.clear()
                self.set_output(json.dumps({"text": text}))
                self.assertEqual(self.ocr.extract_text(str(self.path)), text)
                self.assertEqual(len(self.requests), 1)
                body = json.loads(self.requests[0].content)
                self.assertEqual(body["model"], "gpt-5.6-luna")
                self.assertEqual(body["reasoning"], {"effort": "low"})
                self.assertEqual(len(self.sent_images()), 1)

    def test_parse_invoice_still_supports_existing_raw_text_flow(self):
        self.assertEqual(self.ocr.parse_invoice("Factura 000123"), self.expected)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["input"], [{"role": "user", "content": "Factura 000123"}])

    def test_blank_invoice_uses_one_call_and_returns_all_null_fields(self):
        self.set_output(json.dumps(asdict(Invoice.empty())))
        self.assertEqual(self.ocr.extract_invoice(str(self.path)), Invoice.empty())
        self.assertEqual(len(self.requests), 1)

    def test_bad_inputs_fail_before_creating_a_client(self):
        with patch("src.ocr.ocr_abc.OpenAI") as factory:
            ocr = Ocr_openai()
            with self.assertRaises(FileNotFoundError):
                ocr.extract_invoice(str(self.path.with_name("missing.png")))
            with self.assertRaises(IsADirectoryError):
                ocr.extract_invoice(str(self.path.parent))
            self.path.write_bytes(b"corrupt image")
            with self.assertRaises(UnidentifiedImageError):
                ocr.extract_invoice(str(self.path))
            with patch("src.ocr.ocr_openai._document_image_urls", return_value=[]):
                with self.assertRaisesRegex(ValueError, "no pages"):
                    ocr.extract_invoice(str(self.path))
            factory.assert_not_called()

    def test_client_is_lazy_and_shared_with_text_parsing(self):
        with (
            patch("src.ocr.ocr_abc.OpenAI", return_value=self.client) as factory,
            patch("src.ocr.ocr_abc.OPENAI_API_KEY", "configured-key"),
        ):
            ocr = Ocr_openai()
            factory.assert_not_called()
            ocr.extract_invoice(str(self.path))
            ocr.parse_invoice("Factura")
            factory.assert_called_once_with(api_key="configured-key", timeout=120.0)
        self.assertEqual(len(self.requests), 2)

    def test_invalid_structured_output_is_rejected(self):
        for payload in ("", "not JSON", "null", "[]", "{}",
                        json.dumps({**asdict(self.expected), "total": 12.10}),
                        json.dumps({**asdict(self.expected), "extra": "value"})):
            with self.subTest(payload=payload):
                self.set_output(payload)
                with self.assertRaisesRegex(RuntimeError, "schema"):
                    self.ocr.extract_invoice(str(self.path))

    def test_incomplete_responses_are_rejected(self):
        for status in ("incomplete", "failed"):
            self.response["status"] = status
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                self.ocr.extract_invoice(str(self.path))

    def test_refusal_and_missing_parsed_output_are_rejected(self):
        self.response["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot comply"}]
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.ocr.extract_invoice(str(self.path))
        self.response["output"] = []
        with self.assertRaisesRegex(RuntimeError, "parsed result"):
            self.ocr.extract_invoice(str(self.path))

    def test_authentication_errors_propagate(self):
        self.status_code = 401
        self.response = {"error": {"message": "Invalid test key", "code": "invalid_api_key"}}
        with self.assertRaises(AuthenticationError):
            self.ocr.extract_invoice(str(self.path))


if __name__ == "__main__":
    unittest.main()
