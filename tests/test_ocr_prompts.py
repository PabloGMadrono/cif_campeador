"""Provider prompts compose the same shared invoice rules."""

from src.ocr.ocr_abc import TEXT_INVOICE_PROMPT
from src.ocr.ocr_openai import OPENAI_IMAGE_INVOICE_PROMPT
from src.ocr.ocr_surya import SURYA_BLOCK_INVOICE_PROMPT
from src.ocr.prompts import (
    INVOICE_CLASSIFICATION_RULES,
    INVOICE_EXTRACTION_RULES,
    INVOICE_FISCAL_AND_IDENTITY_RULES,
    INVOICE_LABEL_HINTS,
)


def test_invoice_parsers_receive_the_same_business_rules():
    for prompt in (
        OPENAI_IMAGE_INVOICE_PROMPT,
        SURYA_BLOCK_INVOICE_PROMPT,
        TEXT_INVOICE_PROMPT,
    ):
        assert INVOICE_EXTRACTION_RULES in prompt
        assert INVOICE_CLASSIFICATION_RULES in prompt
        assert INVOICE_FISCAL_AND_IDENTITY_RULES in prompt
        assert INVOICE_LABEL_HINTS in prompt
        assert prompt.count(INVOICE_CLASSIFICATION_RULES) == 1


def test_surya_prompt_adds_evidence_rules_beyond_shared_classification():
    assert "classification_sources" in SURYA_BLOCK_INVOICE_PROMPT
    assert "cite the matching wording" in SURYA_BLOCK_INVOICE_PROMPT
    assert "classification_sources" not in OPENAI_IMAGE_INVOICE_PROMPT
