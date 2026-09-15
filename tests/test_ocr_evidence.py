"""Source validation and structured Surya parsing through an offline SDK transport."""

import copy
import json
import unittest

import httpx
from openai import OpenAI

from src.ocr.evidence import InvoiceEvidence, InvoiceExtraction, OcrBlock, OcrDocument, OcrPage
from src.ocr.models import Invoice
from src.ocr.ocr_surya import Ocr_surya


class SuryaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.document = OcrDocument(pages=[OcrPage(
            page_number=1, image_bbox=[0., 0., 600., 800.], blocks=[
                OcrBlock(block_id="p1_b1", reading_order=0,
                         polygon=[[0., 0.], [100., 0.], [100., 30.], [0., 30.]],
                         label="Text", html="<p>CIF B-07672694</p>",
                         text="CIF B-07672694", skipped=False, error=False),
                OcrBlock(block_id="p1_b2", reading_order=1,
                         polygon=[[0., 100.], [100., 100.], [100., 200.], [0., 200.]],
                         label="Table", html="<table><tr><td>10%</td><td>100.00</td>"
                         "<td>10.00</td></tr><tr><td>21%</td><td>50.00</td><td>10.50</td>"
                         "</tr></table>", text="10%\t100.00\t10.00\n21%\t50.00\t10.50",
                         skipped=False, error=False),
            ],
        )])
        self.payload = InvoiceEvidence.empty().model_dump()
        self.payload["nif_proveedor"] = {
            "value": "B07672694", "status": "printed",
            "sources": [{"block_id": "p1_b1", "printed_text": "CIF B-07672694"}],
        }
        self.status = "completed"
        self.refusal = False
        self.requests = []

        def handle(request):
            self.requests.append(json.loads(request.content))
            content = ({"type": "refusal", "refusal": "No"} if self.refusal else
                       {"type": "output_text", "text": json.dumps(self.payload), "annotations": []})
            return httpx.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 0,
                "model": "gpt-5-mini", "status": self.status,
                "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                            "status": "completed", "content": [content]}],
            })

        self.client = OpenAI(api_key="offline", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(self.client.close)
        self.ocr = Ocr_surya()
        self.ocr._client = self.client

    def test_parser_receives_block_structure_and_returns_source_linked_invoice(self):
        result = self.ocr.parse_document(self.document)
        self.assertIsInstance(result, InvoiceExtraction)
        self.assertIs(type(result.invoice), Invoice)
        self.assertEqual(result.invoice.nif_proveedor, "B07672694")
        self.assertEqual(result.evidence.nif_proveedor.sources[0].block_id, "p1_b1")
        body = self.requests[0]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertFalse(body["store"])
        self.assertEqual(json.loads(body["input"][0]["content"]), self.document.model_dump())
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(set(body["text"]["format"]["schema"]["required"]), set(self.payload))
        restored = InvoiceExtraction.model_validate_json(result.model_dump_json())
        self.assertEqual(restored.invoice, result.invoice)
        self.assertEqual(restored.document, self.document)

    def test_multiple_vat_rows_can_support_a_derived_rate(self):
        self.payload["tipo_iva"] = {
            "value": "13.67", "status": "derived", "sources": [
                {"block_id": "p1_b2", "printed_text": "10% 100.00 10.00"},
                {"block_id": "p1_b2", "printed_text": "21% 50.00 10.50"},
            ],
        }
        result = self.ocr.parse_document(self.document)
        self.assertEqual(result.invoice.tipo_iva, "13.67")
        self.assertEqual(len(result.evidence.tipo_iva.sources), 2)
        self.assertIsNone(result.invoice.base_imponible)

    def test_unknown_empty_or_invented_quotes_are_rejected(self):
        for citation in (
            {"block_id": "p99_b1", "printed_text": "CIF B-07672694"},
            {"block_id": "p1_b1", "printed_text": "CIF B-99999999"},
            {"block_id": "p1_b1", "printed_text": "  "},
        ):
            with self.subTest(citation=citation):
                self.payload["nif_proveedor"]["sources"] = [citation]
                with self.assertRaisesRegex(RuntimeError, "source blocks"):
                    self.ocr.parse_document(self.document)

    def test_field_can_cite_evidence_on_multiple_pages(self):
        second = self.document.pages[0].blocks[0].model_copy(deep=True)
        second.block_id = "p2_b1"
        second.text = "Proveedor PAMADI S.L."
        second.html = "<p>Proveedor PAMADI S.L.</p>"
        self.document.pages.append(OcrPage(page_number=2, image_bbox=[0., 0., 600., 800.],
                                           blocks=[second]))
        self.payload["nif_proveedor"]["sources"].append({
            "block_id": "p2_b1", "printed_text": "Proveedor PAMADI S.L.",
        })
        result = self.ocr.parse_document(self.document)
        self.assertEqual([s.block_id for s in result.evidence.nif_proveedor.sources], ["p1_b1", "p2_b1"])
        self.assertEqual(len(result.document.pages), 2)

    def test_skipped_and_error_blocks_cannot_be_cited(self):
        for flag in ("skipped", "error"):
            with self.subTest(flag=flag):
                document = self.document.model_copy(deep=True)
                setattr(document.pages[0].blocks[0], flag, True)
                with self.assertRaisesRegex(RuntimeError, "source blocks"):
                    self.ocr.parse_document(document)

    def test_duplicate_block_ids_are_rejected(self):
        self.document.pages[0].blocks[1].block_id = "p1_b1"
        with self.assertRaisesRegex(RuntimeError, "source blocks"):
            self.ocr.parse_document(self.document)

    def test_non_null_values_require_sources_and_consistent_status(self):
        original = copy.deepcopy(self.payload["nif_proveedor"])
        for changes in ({"sources": []}, {"value": None}, {"value": " "}, {"status": "missing"}):
            with self.subTest(changes=changes):
                self.payload["nif_proveedor"] = {**original, **changes}
                with self.assertRaises(RuntimeError):
                    self.ocr.parse_document(self.document)

    def test_monetary_fields_cannot_be_marked_derived(self):
        self.payload["cuota_iva"] = {
            "value": "20.50", "status": "derived",
            "sources": [{"block_id": "p1_b2", "printed_text": "10% 100.00 10.00"}],
        }
        with self.assertRaisesRegex(RuntimeError, "source blocks"):
            self.ocr.parse_document(self.document)

    def test_unreadable_value_may_cite_context_but_must_be_null(self):
        self.payload["nif_proveedor"].update(value=None, status="unreadable")
        result = self.ocr.parse_document(self.document)
        self.assertIsNone(result.invoice.nif_proveedor)
        self.assertEqual(result.evidence.nif_proveedor.status, "unreadable")

    def test_malformed_payload_refusal_and_incomplete_output_fail(self):
        original = copy.deepcopy(self.payload)
        for payload in ({}, {**original, "extra": "unexpected"}, {**original, "total": "12.10"}):
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaises(RuntimeError):
                    self.ocr.parse_document(self.document)
        self.payload = original
        self.refusal = True
        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.ocr.parse_document(self.document)
        self.refusal = False
        self.status = "incomplete"
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            self.ocr.parse_document(self.document)

    def test_non_document_input_is_rejected_before_api(self):
        with self.assertRaises(TypeError):
            self.ocr.parse_document("raw text")
        self.assertFalse(self.requests)
