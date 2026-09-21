"""Select the OCR implementation used by the application and black-box tests."""

from .ocr_surya import Ocr_surya
from .ocr_qwen import Ocr_qwen
from .ocr_openai import Ocr_openai
from .ocr_mistral import Ocr_mistral


# Import additional implementations here and change this assignment to select one.
# Surya initializes its inference backend lazily, on first extraction.
invoice_extractor = Ocr_openai()

__all__ = ["invoice_extractor"]
