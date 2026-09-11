"""Exercise actual image/PDF codecs and the OpenAI SDK with offline HTTP."""

import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
import pypdfium2 as pdfium
from openai import AuthenticationError, OpenAI
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

from src.ocr.models import Invoice
from src.ocr.ocr_qwen import Ocr_qwen


class QwenOcrTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "invoice.png"
        with Image.new("RGB", (20, 30), "white") as image:
            image.save(self.path)
        self.response = {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "qwen/qwen2.5-vl-72b-instruct",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({
                    "text": "Factura 000123\nEspaña: acción y niñez\nTotal 12,10 €",
                })},
            }],
        }
        self.requests = []
        self.status_code = 200

        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status_code, json=self.response)

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1", api_key="offline-test-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        self.addCleanup(self.client.close)
        self.ocr = Ocr_qwen()
        self.ocr._ocr_client = self.client

    def sent_image(self, index=0):
        body = json.loads(self.requests[index].content)
        url = body["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        image = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1], validate=True)))
        self.addCleanup(image.close)
        return image

    def test_local_image_is_sent_to_openrouter_and_returns_unicode_text(self):
        text = self.ocr.extract_text(str(self.path))
        self.assertEqual(text, "Factura 000123\nEspaña: acción y niñez\nTotal 12,10 €")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer offline-test-key")
        body = json.loads(request.content)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(self.sent_image().size, (20, 30))
        self.assertNotIn("_client", self.ocr.__dict__)

    def test_pdf_pages_are_rendered_and_transcribed_in_order(self):
        path = self.path.with_suffix(".PDF")
        with pdfium.PdfDocument.new() as document:
            for size in ((72, 144), (144, 72)):
                page = document.new_page(*size)
                page.close()
            document.save(str(path))
        text = self.ocr.extract_text(str(path))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.sent_image(0).size, (144, 288))
        self.assertEqual(self.sent_image(1).size, (288, 144))
        self.assertEqual(len(text.split("\n\n")), 2)

    def test_multiframe_tiff_preserves_all_pages(self):
        path = self.path.with_suffix(".tiff")
        with Image.new("RGB", (10, 15), "white") as first:
            with Image.new("RGB", (30, 25), "white") as second:
                first.save(path, save_all=True, append_images=[second])
        self.ocr.extract_text(str(path))
        self.assertEqual(self.sent_image(0).size, (10, 15))
        self.assertEqual(self.sent_image(1).size, (30, 25))

    def test_heic_is_decoded_locally_to_png(self):
        register_heif_opener(thumbnails=False)
        path = self.path.with_suffix(".HEIC")
        with Image.new("RGB", (32, 48), "white") as image:
            image.save(path)
        self.ocr.extract_text(str(path))
        self.assertEqual(self.sent_image().size, (32, 48))

    def test_exif_orientation_is_applied(self):
        path = self.path.with_suffix(".jpg")
        with Image.new("RGB", (20, 30), "white") as image:
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
        self.ocr.extract_text(str(path))
        self.assertEqual(self.sent_image().size, (30, 20))

    def test_transparent_background_is_white(self):
        with Image.new("RGBA", (20, 30), (0, 0, 0, 0)) as image:
            image.putpixel((1, 1), (0, 0, 0, 255))
            image.save(self.path)
        self.ocr.extract_text(str(self.path))
        sent = self.sent_image()
        self.assertEqual(sent.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(sent.getpixel((1, 1)), (0, 0, 0))

    def test_inherited_invoice_flow_receives_plain_text(self):
        with patch.object(self.ocr, "parse_invoice", return_value=Invoice.empty()) as parse:
            self.assertEqual(self.ocr.extract_invoice(str(self.path)), Invoice.empty())
        parse.assert_called_once_with("Factura 000123\nEspaña: acción y niñez\nTotal 12,10 €")

    def test_blank_ocr_returns_empty_invoice_without_openai(self):
        self.response["choices"][0]["message"]["content"] = '{"text": ""}'
        self.assertEqual(self.ocr.extract_invoice(str(self.path)), Invoice.empty())
        self.assertNotIn("_client", self.ocr.__dict__)

    def test_bad_paths_and_corrupt_images_do_not_send_requests(self):
        with self.assertRaises(FileNotFoundError):
            self.ocr.extract_text(str(self.path.with_name("missing.png")))
        with self.assertRaises(IsADirectoryError):
            self.ocr.extract_text(str(self.path.parent))
        self.path.write_bytes(b"not an image")
        with self.assertRaises(UnidentifiedImageError):
            self.ocr.extract_text(str(self.path))
        self.assertFalse(self.requests)

    def test_missing_credentials_fail_clearly_and_client_is_lazy(self):
        with patch("src.ocr.ocr_qwen.OPENROUTER_API_KEY", None):
            ocr = Ocr_qwen()
            self.assertNotIn("_ocr_client", ocr.__dict__)
            with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY"):
                ocr.extract_text(str(self.path))

    def test_configured_client_is_reused_and_model_is_sent(self):
        with (
            patch("src.ocr.ocr_qwen.OPENROUTER_API_KEY", "configured-key"),
            patch("src.ocr.ocr_qwen.OPENROUTER_OCR_MODEL", "custom/vision-model"),
            patch("src.ocr.ocr_qwen.OpenAI", return_value=self.client) as factory,
        ):
            ocr = Ocr_qwen()
            factory.assert_not_called()
            ocr.extract_text(str(self.path))
            ocr.extract_text(str(self.path))
            factory.assert_called_once_with(
                base_url="https://openrouter.ai/api/v1",
                api_key="configured-key", timeout=120.0,
            )
        self.assertEqual(json.loads(self.requests[0].content)["model"], "custom/vision-model")

    def test_malformed_responses_are_rejected(self):
        for content in (None, "", "not JSON", "[]", "null", "{}",
                        '{"text": null}', '{"text": 1}', '{"text": "ok", "extra": 1}'):
            with self.subTest(content=content):
                self.response["choices"][0]["message"]["content"] = content
                with self.assertRaisesRegex(RuntimeError, "page 1"):
                    self.ocr.extract_text(str(self.path))

    def test_refusals_missing_choices_and_truncated_responses_are_rejected(self):
        choice = self.response["choices"][0]
        choice["message"]["refusal"] = "Unable to comply"
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.ocr.extract_text(str(self.path))
        choice["message"]["refusal"] = None
        for reason in ("length", "content_filter", "tool_calls", None):
            with self.subTest(reason=reason):
                choice["finish_reason"] = reason
                with self.assertRaisesRegex(RuntimeError, "did not complete"):
                    self.ocr.extract_text(str(self.path))
        self.response["choices"] = []
        with self.assertRaisesRegex(RuntimeError, "no choices"):
            self.ocr.extract_text(str(self.path))

    def test_api_authentication_errors_propagate(self):
        self.status_code = 401
        self.response = {"error": {"message": "Invalid test credential", "code": 401}}
        with self.assertRaises(AuthenticationError):
            self.ocr.extract_text(str(self.path))


if __name__ == "__main__":
    unittest.main()
