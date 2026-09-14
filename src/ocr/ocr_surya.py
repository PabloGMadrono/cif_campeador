import argparse
from functools import cached_property
from pathlib import Path

from bs4 import BeautifulSoup

from src.config import (
    LLAMA_CPP_CPU_BINARY,
    LLAMA_CPP_CUDA_BINARY,
    SURYA_LLAMA_DEVICE,
)

from .ocr_abc import Ocr_operator


class Ocr_surya(Ocr_operator):
    """Extract text from images or PDFs with Surya's multilingual OCR.

    Spanish is recognized automatically; the current API takes no language
    argument. The predictor is initialized on first extraction and reused.
    Uses a CPU or NVIDIA CUDA llama.cpp build according to
    ``SURYA_LLAMA_DEVICE``. Install both servers as described in README.md.
    Surya's device settings are process-wide.
    """

    @cached_property
    def _recognition_predictor(self):
        if SURYA_LLAMA_DEVICE not in {"cpu", "cuda"}:
            raise ValueError(
                "SURYA_LLAMA_DEVICE must be either 'cpu' or 'cuda', "
                f"got {SURYA_LLAMA_DEVICE!r}"
            )

        llama_binary = (
            LLAMA_CPP_CUDA_BINARY
            if SURYA_LLAMA_DEVICE == "cuda"
            else LLAMA_CPP_CPU_BINARY
        )
        if not llama_binary:
            variable = (
                "LLAMA_CPP_CUDA_BINARY"
                if SURYA_LLAMA_DEVICE == "cuda"
                else "LLAMA_CPP_CPU_BINARY (or legacy LLAMA_CPP_BINARY)"
            )
            raise RuntimeError(
                f"{variable} must point to the selected llama-server executable"
            )

        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor
        from surya.settings import settings

        # Set the settings object itself: Surya may already have been imported,
        # in which case changing environment variables would be too late.
        # Python-side preprocessing remains on CPU. The selected external
        # llama-server owns model and vision-projector GPU execution.
        settings.TORCH_DEVICE = "cpu"
        settings.LLAMA_CPP_BINARY = llama_binary
        if SURYA_LLAMA_DEVICE == "cuda":
            settings.LLAMA_CPP_NGL = 99
            settings.LLAMA_CPP_NO_MMPROJ_OFFLOAD = False
        else:
            settings.LLAMA_CPP_NGL = 0
            settings.LLAMA_CPP_NO_MMPROJ_OFFLOAD = True
        if settings.SURYA_INFERENCE_PARALLEL is None:
            settings.SURYA_INFERENCE_PARALLEL = 1

        manager = SuryaInferenceManager(method="llamacpp")
        return RecognitionPredictor(manager)

    def extract_text(self, path: str) -> str:
        """Return plain text in reading order, with blank lines between pages.

        Raise FileNotFoundError for missing files, IsADirectoryError for
        directories, and RuntimeError if Surya reports a failed OCR block.
        Loading and inference errors propagate to the caller.
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Expected a document file: {file_path}")

        if file_path.suffix.lower() in {".heic", ".heif"}:
            from pillow_heif import register_heif_opener

            register_heif_opener(thumbnails=False)

        from surya.input.load import load_from_file

        images, _ = load_from_file(str(file_path))
        try:
            predictions = self._recognition_predictor(images)
            pages = []
            for page_number, prediction in enumerate(predictions, start=1):
                blocks = []
                for block in sorted(prediction.blocks, key=lambda b: b.reading_order):
                    if block.error:
                        raise RuntimeError(
                            f"Surya OCR failed on page {page_number} of {file_path}"
                        )
                    if block.skipped:
                        continue
                    text = _html_to_text(block.html)
                    if text:
                        blocks.append(text)
                pages.append("\n".join(blocks))
            return "\n\n".join(pages)
        finally:
            for image in images:
                image.close()


def _html_to_text(html: str) -> str:
    """Remove markup while retaining inline words, line breaks and table cells."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(["td", "th"]):
        tag.insert_after("\t")
    for tag in soup.find_all(
        ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"]
    ):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return "\n".join(
        line.strip() for line in soup.get_text().splitlines() if line.strip()
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract and print text from an image or PDF using Surya OCR."
    )
    parser.add_argument("path", help="Path to an image or PDF")
    args = parser.parse_args()
    print(Ocr_surya().extract_text(args.path))


if __name__ == "__main__":
    main()
