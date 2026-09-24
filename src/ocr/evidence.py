"""OCR blocks and source-linked invoice fields."""

import unicodedata
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .models import (
    EquivalenceSurcharge,
    Invoice,
    InvoiceValidity,
    IrpfWithholding,
    IvaLine,
)


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
    preprocessing: dict | None = None


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
    status: Literal["printed", "missing", "unreadable"]
    sources: list[SourceQuote]

    @model_validator(mode="after")
    def check_value_and_sources(self):
        if self.status == "printed":
            if self.value is None or not self.value.strip() or not self.sources:
                raise ValueError("Printed values require a value and source quotes")
        elif self.value is not None:
            raise ValueError("Missing/unreadable fields must have a null value")
        if self.status == "missing" and self.sources:
            raise ValueError("Missing fields must have no source quotes")
        return self


class InvoiceEvidence(EvidenceModel):
    validity: InvoiceValidity
    diagnostic_type: str | None
    classification_sources: list[SourceQuote]
    fecha: FieldEvidence
    numero_factura: FieldEvidence
    nif_proveedor: FieldEvidence
    nombre_proveedor: FieldEvidence
    lineas_iva: list["IvaLineEvidence"]
    recargos_equivalencia: list["EquivalenceSurchargeEvidence"]
    retencion_irpf: "IrpfWithholdingEvidence | None"
    total: FieldEvidence

    @property
    def invoice(self) -> Invoice:
        return Invoice(
            validity=self.validity,
            diagnostic_type=self.diagnostic_type,
            fecha=self.fecha.value,
            numero_factura=self.numero_factura.value,
            nif_proveedor=self.nif_proveedor.value,
            nombre_proveedor=self.nombre_proveedor.value,
            lineas_iva=tuple(line.invoice_value for line in self.lineas_iva),
            recargos_equivalencia=tuple(
                line.invoice_value for line in self.recargos_equivalencia
            ),
            retencion_irpf=(
                self.retencion_irpf.invoice_value if self.retencion_irpf else None
            ),
            total=self.total.value,
        )

    @classmethod
    def unreadable(cls) -> "InvoiceEvidence":
        def unreadable_field() -> FieldEvidence:
            return FieldEvidence(value=None, status="unreadable", sources=[])

        return cls(
            validity=InvoiceValidity.INVALID,
            diagnostic_type="Unreadable",
            classification_sources=[],
            fecha=unreadable_field(),
            numero_factura=unreadable_field(),
            nif_proveedor=unreadable_field(),
            nombre_proveedor=unreadable_field(),
            lineas_iva=[],
            recargos_equivalencia=[],
            retencion_irpf=None,
            total=unreadable_field(),
        )

    def field_evidence(self) -> Iterable[tuple[str, FieldEvidence]]:
        yield "fecha", self.fecha
        yield "numero_factura", self.numero_factura
        yield "nif_proveedor", self.nif_proveedor
        yield "nombre_proveedor", self.nombre_proveedor
        for index, line in enumerate(self.lineas_iva):
            yield from line.field_evidence(f"lineas_iva[{index}]")
        for index, line in enumerate(self.recargos_equivalencia):
            yield from line.field_evidence(f"recargos_equivalencia[{index}]")
        if self.retencion_irpf:
            yield from self.retencion_irpf.field_evidence("retencion_irpf")
        yield "total", self.total

    def validate_sources(self, document: OcrDocument) -> None:
        """Check references/quotes, not whether the model selected the correct party."""
        blocks = {block.block_id: block for page in document.pages for block in page.blocks}
        if len(blocks) != sum(len(page.blocks) for page in document.pages):
            raise ValueError("OCR block IDs must be unique within a document")

        def normalized(text: str) -> str:
            return " ".join(unicodedata.normalize("NFC", text).split())

        if document.text.strip() and not self.classification_sources:
            raise ValueError("Classification requires at least one source quote")

        evidence_items = list(self.field_evidence())
        source_items = [("classification", source) for source in self.classification_sources]
        source_items.extend(
            (name, source)
            for name, evidence in evidence_items
            for source in evidence.sources
        )
        for name, source in source_items:
            block = blocks.get(source.block_id)
            if block is None or block.skipped or block.error:
                raise ValueError(f"{name}: invalid source block {source.block_id!r}")
            quote = normalized(source.printed_text)
            if not quote or quote not in normalized(block.text):
                raise ValueError(f"{name}: source quote is not present in {source.block_id}")


class IvaLineEvidence(EvidenceModel):
    base_imponible: FieldEvidence
    tipo_iva: FieldEvidence
    cuota_iva: FieldEvidence

    @property
    def invoice_value(self) -> IvaLine:
        return IvaLine(
            base_imponible=self.base_imponible.value,
            tipo_iva=self.tipo_iva.value,
            cuota_iva=self.cuota_iva.value,
        )

    def field_evidence(self, prefix: str) -> Iterable[tuple[str, FieldEvidence]]:
        yield f"{prefix}.base_imponible", self.base_imponible
        yield f"{prefix}.tipo_iva", self.tipo_iva
        yield f"{prefix}.cuota_iva", self.cuota_iva


class EquivalenceSurchargeEvidence(EvidenceModel):
    base_imponible: FieldEvidence
    tipo_re: FieldEvidence
    cuota_re: FieldEvidence

    @property
    def invoice_value(self) -> EquivalenceSurcharge:
        return EquivalenceSurcharge(
            base_imponible=self.base_imponible.value,
            tipo_re=self.tipo_re.value,
            cuota_re=self.cuota_re.value,
        )

    def field_evidence(self, prefix: str) -> Iterable[tuple[str, FieldEvidence]]:
        yield f"{prefix}.base_imponible", self.base_imponible
        yield f"{prefix}.tipo_re", self.tipo_re
        yield f"{prefix}.cuota_re", self.cuota_re


class IrpfWithholdingEvidence(EvidenceModel):
    base_retencion: FieldEvidence
    tipo_irpf: FieldEvidence
    cuota_irpf: FieldEvidence

    @property
    def invoice_value(self) -> IrpfWithholding:
        return IrpfWithholding(
            base_retencion=self.base_retencion.value,
            tipo_irpf=self.tipo_irpf.value,
            cuota_irpf=self.cuota_irpf.value,
        )

    def field_evidence(self, prefix: str) -> Iterable[tuple[str, FieldEvidence]]:
        yield f"{prefix}.base_retencion", self.base_retencion
        yield f"{prefix}.tipo_irpf", self.tipo_irpf
        yield f"{prefix}.cuota_irpf", self.cuota_irpf


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
