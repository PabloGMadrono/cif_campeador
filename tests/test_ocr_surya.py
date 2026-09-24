import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from src.ocr.ocr_abc import Ocr_operator
from src.ocr.evidence import InvoiceEvidence, InvoiceExtraction, SourceQuote
from src.ocr.models import Invoice
from src.ocr.ocr_surya import Ocr_surya, _html_to_text


def block(html, order=0, **flags):
    return SimpleNamespace(
        html=html, reading_order=order,
        polygon=[[0.0, 0.0], [100.0, 0.0], [100.0, 30.0], [0.0, 30.0]],
        label="Text",
        error=flags.get("error", False), skipped=flags.get("skipped", False),
    )


def page(blocks):
    return SimpleNamespace(blocks=blocks, image_bbox=[0.0, 0.0, 600.0, 800.0])


class SuryaOcrTests(unittest.TestCase):
    def setUp(self):
        config_patcher = patch.multiple(
            "src.ocr.ocr_surya",
            SURYA_LLAMA_DEVICE="cpu",
            LLAMA_CPP_CPU_BINARY="cpu-llama-server",
            LLAMA_CPP_CUDA_BINARY=None,
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "documento.pdf"
        self.path.touch()
        self.images = [Mock(), Mock()]
        self.loader = Mock(return_value=(self.images, ["documento", "documento"]))
        self.prepared = SimpleNamespace(pages=[SimpleNamespace(metadata={}) for _ in self.images])
        @contextmanager
        def images():
            try:
                yield self.images
            finally:
                for image in self.images:
                    image.close()
        self.prepared.images = images
        preparation = patch("src.ocr.ocr_surya.prepare_document", return_value=self.prepared)
        self.prepare = preparation.start()
        self.addCleanup(preparation.stop)
        self.predictor = Mock()
        self.factory = Mock(return_value=self.predictor)
        self.manager_factory = Mock()
        self.settings = SimpleNamespace(
            TORCH_DEVICE="cuda", LLAMA_CPP_NGL=99,
            LLAMA_CPP_NO_MMPROJ_OFFLOAD=False, SURYA_INFERENCE_PARALLEL=None,
            LLAMA_CPP_BINARY="llama-server",
        )
        self.register_heif_opener = Mock()
        modules = {
            name: ModuleType(name)
            for name in (
                "surya", "surya.input", "surya.input.load", "surya.recognition",
                "surya.inference", "surya.settings", "pillow_heif",
            )
        }
        modules["surya.input.load"].load_from_file = self.loader
        modules["surya.recognition"].RecognitionPredictor = self.factory
        modules["surya.inference"].SuryaInferenceManager = self.manager_factory
        modules["surya.settings"].settings = self.settings
        modules["pillow_heif"].register_heif_opener = self.register_heif_opener
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_base_class_is_abstract(self):
        with self.assertRaises(TypeError):
            Ocr_operator()

    def test_spanish_text_reading_order_and_multiple_pages(self):
        self.predictor.return_value = [
            page([
                block("<p>Importe: 20 &euro;</p>", 1),
                block("<p>Espa&ntilde;a: <b>acci&oacute;n</b> y niñez</p>", 0),
                block("Ignore this picture", 2, skipped=True),
            ]),
            page([block("<p>Segunda página</p>")]),
        ]
        self.assertEqual(
            Ocr_surya().extract_text(str(self.path)),
            "España: acción y niñez\nImporte: 20 €\n\nSegunda página",
        )
        self.prepare.assert_called_once_with(str(self.path))
        self.loader.assert_not_called()
        self.predictor.assert_called_once_with(self.images)
        self.register_heif_opener.assert_not_called()
        for image in self.images:
            image.close.assert_called_once()

    def test_html_preserves_inline_words_line_breaks_and_table_cells(self):
        self.assertEqual(
            _html_to_text(
                "<p>nú<b>me</b>ro<br/>Total</p>"
                "<table><tr><td>Artículo</td><td>Precio</td></tr>"
                "<tr><td>Pan</td><td>2 €</td></tr></table>"
            ),
            "número\nTotal\nArtículo\tPrecio\nPan\t2 €",
        )

    def test_document_preserves_table_geometry_and_skipped_blocks_across_pages(self):
        table = block("<table><tr><td>10%</td><td>2,00 €</td></tr></table>", 1)
        table.label = "Table"
        table.polygon = [[50., 100.], [200., 100.], [200., 160.], [50., 160.]]
        self.predictor.return_value = [
            page([table, block("Logo", 0, skipped=True)]),
            page([block("<p>Footer</p>")]),
        ]
        document = Ocr_surya().extract_document(str(self.path))
        first, second = document.pages
        self.assertEqual(first.image_bbox, [0., 0., 600., 800.])
        self.assertEqual(first.blocks[0].block_id, "p1_b1")
        self.assertTrue(first.blocks[0].skipped)
        self.assertEqual(first.blocks[1].html, table.html)
        self.assertEqual(first.blocks[1].polygon, table.polygon)
        self.assertEqual(first.blocks[1].label, "Table")
        self.assertEqual(first.blocks[1].text, "10%\t2,00 €")
        self.assertEqual(second.blocks[0].block_id, "p2_b1")
        self.assertEqual(document.text, "10%\t2,00 €\n\nFooter")

    def test_public_invoice_and_evidence_paths_use_one_ocr_pass_each(self):
        self.images.pop()
        self.predictor.return_value = [page([block("Nothing identified")])]
        ocr = Ocr_surya()
        evidence = InvoiceEvidence.unreadable().model_copy(update={
            "classification_sources": [
                SourceQuote(block_id="p1_b1", printed_text="Nothing identified")
            ]
        })
        ocr._parse_structured = Mock(return_value=evidence)
        with patch.object(ocr, "extract_text", side_effect=AssertionError("Must retain blocks")):
            result = ocr.extract_invoice_with_evidence(str(self.path))
            self.assertIsInstance(result, InvoiceExtraction)
            self.assertEqual(result.invoice, Invoice.unreadable())
            self.predictor.assert_called_once_with(self.images)
            self.assertEqual(ocr._parse_structured.call_count, 1)
            self.assertIs(type(ocr.extract_invoice(str(self.path))), Invoice)
        self.assertEqual(self.predictor.call_count, 2)
        self.assertEqual(ocr._parse_structured.call_count, 2)

    def test_empty_document_keeps_blocks_and_does_not_parse(self):
        self.images.pop()
        self.predictor.return_value = [page([block("Picture", skipped=True)])]
        ocr = Ocr_surya()
        ocr._parse_structured = Mock(side_effect=AssertionError("No API for empty text"))
        result = ocr.extract_invoice_with_evidence(str(self.path))
        self.assertEqual(result.invoice, Invoice.unreadable())
        self.assertTrue(result.document.pages[0].blocks[0].skipped)
        ocr._parse_structured.assert_not_called()

    def test_blank_document_and_predictor_reuse(self):
        self.images.pop()
        self.predictor.return_value = [page([])]
        ocr = Ocr_surya()
        self.factory.assert_not_called()
        self.assertEqual(ocr.extract_text(str(self.path)), "")
        self.assertEqual(ocr.extract_text(str(self.path)), "")
        self.factory.assert_called_once_with(self.manager_factory.return_value)
        self.manager_factory.assert_called_once_with(method="llamacpp")

    def test_cpu_settings_are_applied_before_backend_creation(self):
        def create_manager(*, method):
            self.assertEqual(method, "llamacpp")
            self.assertEqual(self.settings.TORCH_DEVICE, "cpu")
            self.assertEqual(self.settings.LLAMA_CPP_NGL, 0)
            self.assertTrue(self.settings.LLAMA_CPP_NO_MMPROJ_OFFLOAD)
            self.assertEqual(self.settings.SURYA_INFERENCE_PARALLEL, 1)
            return Mock()

        self.manager_factory.side_effect = create_manager
        self.predictor.return_value = [page([]), page([])]
        Ocr_surya().extract_text(str(self.path))
        self.manager_factory.assert_called_once_with(method="llamacpp")
        self.assertEqual(self.settings.LLAMA_CPP_BINARY, "cpu-llama-server")

    def test_cuda_settings_and_binary_are_applied_before_backend_creation(self):
        def create_manager(*, method):
            self.assertEqual(method, "llamacpp")
            self.assertEqual(self.settings.TORCH_DEVICE, "cpu")
            self.assertEqual(self.settings.LLAMA_CPP_BINARY, "cuda-llama-server")
            self.assertEqual(self.settings.LLAMA_CPP_NGL, 99)
            self.assertFalse(self.settings.LLAMA_CPP_NO_MMPROJ_OFFLOAD)
            self.assertEqual(self.settings.SURYA_INFERENCE_PARALLEL, 1)
            return Mock()

        self.manager_factory.side_effect = create_manager
        self.predictor.return_value = [page([]), page([])]
        with patch.multiple(
            "src.ocr.ocr_surya",
            SURYA_LLAMA_DEVICE="cuda",
            LLAMA_CPP_CUDA_BINARY="cuda-llama-server",
        ):
            Ocr_surya().extract_text(str(self.path))

        self.manager_factory.assert_called_once_with(method="llamacpp")

    def test_cuda_requires_its_own_binary(self):
        self.predictor.return_value = [page([]), page([])]
        with patch.multiple(
            "src.ocr.ocr_surya",
            SURYA_LLAMA_DEVICE="cuda",
            LLAMA_CPP_CUDA_BINARY=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "LLAMA_CPP_CUDA_BINARY"):
                Ocr_surya().extract_text(str(self.path))
        self.manager_factory.assert_not_called()

    def test_invalid_llama_device_is_rejected(self):
        self.predictor.return_value = [page([]), page([])]
        with patch("src.ocr.ocr_surya.SURYA_LLAMA_DEVICE", "vulkan"):
            with self.assertRaisesRegex(ValueError, "cpu.*cuda"):
                Ocr_surya().extract_text(str(self.path))
        self.manager_factory.assert_not_called()

    def test_heic_uses_shared_preparation_without_surya_loader(self):
        heic_path = self.path.with_suffix(".HEIC")
        heic_path.touch()
        self.predictor.return_value = [page([]), page([])]
        Ocr_surya().extract_text(str(heic_path))
        self.prepare.assert_called_once_with(str(heic_path))
        self.loader.assert_not_called()

    def test_explicit_parallelism_is_preserved(self):
        self.settings.SURYA_INFERENCE_PARALLEL = 2
        self.predictor.return_value = [page([]), page([])]
        Ocr_surya().extract_text(str(self.path))
        self.assertEqual(self.settings.SURYA_INFERENCE_PARALLEL, 2)

    def test_configured_llama_binary_is_used(self):
        self.predictor.return_value = [page([]), page([])]
        with patch(
            "src.ocr.ocr_surya.LLAMA_CPP_CPU_BINARY",
            "configured-llama-server",
        ):
            Ocr_surya().extract_text(str(self.path))
        self.assertEqual(self.settings.LLAMA_CPP_BINARY, "configured-llama-server")

    def test_invalid_paths_fail_before_loading_or_inference(self):
        ocr = Ocr_surya()
        with self.assertRaises(FileNotFoundError):
            ocr.extract_text(str(self.path.with_name("missing.png")))
        with self.assertRaises(IsADirectoryError):
            ocr.extract_text(str(self.path.parent))
        self.loader.assert_not_called()
        self.factory.assert_not_called()

    def test_inference_failure_closes_images(self):
        self.predictor.side_effect = RuntimeError("Backend unavailable")
        with self.assertRaisesRegex(RuntimeError, "Backend unavailable"):
            Ocr_surya().extract_text(str(self.path))
        for image in self.images:
            image.close.assert_called_once()

    def test_failed_block_does_not_return_partial_text(self):
        self.images.pop()
        self.predictor.return_value = [page([
            block("Texto correcto"), block("", 1, error=True),
        ])]
        with self.assertRaisesRegex(RuntimeError, "page 1"):
            Ocr_surya().extract_text(str(self.path))
        for image in self.images:
            image.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
