import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from src.ocr.ocr_abc import Ocr_operator
from src.ocr.ocr_surya import Ocr_surya, _html_to_text


def block(html, order=0, **flags):
    return SimpleNamespace(
        html=html, reading_order=order,
        error=flags.get("error", False), skipped=flags.get("skipped", False),
    )


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
            SimpleNamespace(blocks=[
                block("<p>Importe: 20 &euro;</p>", 1),
                block("<p>Espa&ntilde;a: <b>acci&oacute;n</b> y niñez</p>", 0),
                block("Ignore this picture", 2, skipped=True),
            ]),
            SimpleNamespace(blocks=[block("<p>Segunda página</p>")]),
        ]
        self.assertEqual(
            Ocr_surya().extract_text(str(self.path)),
            "España: acción y niñez\nImporte: 20 €\n\nSegunda página",
        )
        self.loader.assert_called_once_with(str(self.path))
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

    def test_blank_document_and_predictor_reuse(self):
        self.predictor.return_value = [SimpleNamespace(blocks=[])]
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
        self.predictor.return_value = []
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
        self.predictor.return_value = []
        with patch.multiple(
            "src.ocr.ocr_surya",
            SURYA_LLAMA_DEVICE="cuda",
            LLAMA_CPP_CUDA_BINARY="cuda-llama-server",
        ):
            Ocr_surya().extract_text(str(self.path))

        self.manager_factory.assert_called_once_with(method="llamacpp")

    def test_cuda_requires_its_own_binary(self):
        self.predictor.return_value = []
        with patch.multiple(
            "src.ocr.ocr_surya",
            SURYA_LLAMA_DEVICE="cuda",
            LLAMA_CPP_CUDA_BINARY=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "LLAMA_CPP_CUDA_BINARY"):
                Ocr_surya().extract_text(str(self.path))
        self.manager_factory.assert_not_called()

    def test_invalid_llama_device_is_rejected(self):
        self.predictor.return_value = []
        with patch("src.ocr.ocr_surya.SURYA_LLAMA_DEVICE", "vulkan"):
            with self.assertRaisesRegex(ValueError, "cpu.*cuda"):
                Ocr_surya().extract_text(str(self.path))
        self.manager_factory.assert_not_called()

    def test_heic_decoder_is_registered_before_loading(self):
        heic_path = self.path.with_suffix(".HEIC")
        heic_path.touch()
        self.predictor.return_value = []

        def check_registration(_):
            self.register_heif_opener.assert_called_once_with(thumbnails=False)
            return self.images, ["documento"]

        self.loader.side_effect = check_registration
        Ocr_surya().extract_text(str(heic_path))

    def test_explicit_parallelism_is_preserved(self):
        self.settings.SURYA_INFERENCE_PARALLEL = 2
        self.predictor.return_value = []
        Ocr_surya().extract_text(str(self.path))
        self.assertEqual(self.settings.SURYA_INFERENCE_PARALLEL, 2)

    def test_configured_llama_binary_is_used(self):
        self.predictor.return_value = []
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
        self.predictor.return_value = [SimpleNamespace(blocks=[
            block("Texto correcto"), block("", 1, error=True),
        ])]
        with self.assertRaisesRegex(RuntimeError, "page 1"):
            Ocr_surya().extract_text(str(self.path))
        for image in self.images:
            image.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
