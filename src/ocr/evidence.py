"""OCR blocks and source-linked invoice fields, separate from the public Invoice."""

import unicodedata
from dataclasses import fields
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .models import Invoice


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OcrBlock(EvidenceModel):
    block_id: str
    reading_order: int
    polygon: list[list[float]]
    label: str
    html: str
    text: str
    skipped: bool
    error: bool


class OcrPage(EvidenceModel):
    page_number: int
    image_bbox: list[float]
    blocks: list[OcrBlock]


class OcrDocument(EvidenceModel):
    pages: list[OcrPage]

    @property
    def text(self) -> str:
        return "\n\n".join(
            "\n".join(block.text for block in page.blocks
                      if block.text and not block.skipped and not block.error)
            for page in self.pages
        )


class SourceQuote(EvidenceModel):
    block_id: str
    printed_text: str


class FieldEvidence(EvidenceModel):
    value: str | None
    status: Literal["printed", "derived", "missing", "unreadable"]
    sources: list[SourceQuote]

    @model_validator(mode="after")
    def check_value_and_sources(self):
        if self.status in {"printed", "derived"}:
            if self.value is None or not self.value.strip() or not self.sources:
                raise ValueError("Printed/derived values require a value and source quotes")
        elif self.value is not None:
            raise ValueError("Missing/unreadable fields must have a null value")
        if self.status == "missing" and self.sources:
            raise ValueError("Missing fields must have no source quotes")
        return self


class InvoiceEvidence(EvidenceModel):
    fecha: FieldEvidence
    numero_factura: FieldEvidence
    nif_proveedor: FieldEvidence
    nombre_proveedor: FieldEvidence
    base_imponible: FieldEvidence
    tipo_iva: FieldEvidence
    cuota_iva: FieldEvidence
    total: FieldEvidence

    @property
    def invoice(self) -> Invoice:
        return Invoice(**{item.name: getattr(self, item.name).value for item in fields(Invoice)})

    @classmethod
    def empty(cls) -> "InvoiceEvidence":
        return cls(**{item.name: FieldEvidence(value=None, status="missing", sources=[])
                      for item in fields(Invoice)})

    def validate_sources(self, document: OcrDocument) -> None:
        """Check references/quotes, not whether the model selected the correct party."""
        blocks = {block.block_id: block for page in document.pages for block in page.blocks}
        if len(blocks) != sum(len(page.blocks) for page in document.pages):
            raise ValueError("OCR block IDs must be unique within a document")

        def normalized(text: str) -> str:
            return " ".join(unicodedata.normalize("NFC", text).split())

        for item in fields(Invoice):
            evidence = getattr(self, item.name)
            if evidence.status == "derived" and item.name != "tipo_iva":
                raise ValueError(f"{item.name}: only the effective VAT rate may be derived")
            for source in evidence.sources:
                block = blocks.get(source.block_id)
                if block is None or block.skipped or block.error:
                    raise ValueError(f"{item.name}: invalid source block {source.block_id!r}")
                quote = normalized(source.printed_text)
                if not quote or quote not in normalized(block.text):
                    raise ValueError(f"{item.name}: source quote is not present in {source.block_id}")


class InvoiceExtraction(EvidenceModel):
    document: OcrDocument
    evidence: InvoiceEvidence

    @model_validator(mode="after")
    def check_sources(self):
        self.evidence.validate_sources(self.document)
        return self

    @property
    def invoice(self) -> Invoice:
        return self.evidence.invoice
