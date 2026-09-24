"""Strict, provider-independent result returned by every OCR backend."""

from dataclasses import field
from enum import StrEnum

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

STRICT_CONFIG = ConfigDict(extra="forbid", strict=True)


class InvoiceValidity(StrEnum):
    """Binary business decision produced for every processed document."""

    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, config=STRICT_CONFIG)
class IvaLine:
    """One printed VAT breakdown line."""

    base_imponible: str | None = field(
        metadata={"csv": "Base Imponible", "description": "Base Imponible"}
    )
    tipo_iva: str | None = field(
        metadata={"csv": "Tipo IVA %", "description": "Tipo IVA %"}
    )
    cuota_iva: str | None = field(
        metadata={"csv": "Cuota IVA", "description": "Cuota IVA"}
    )


@dataclass(frozen=True, config=STRICT_CONFIG)
class EquivalenceSurcharge:
    """One printed recargo de equivalencia breakdown line."""

    base_imponible: str | None = field(
        metadata={"csv": "Base Imponible", "description": "Base Imponible RE"}
    )
    tipo_re: str | None = field(metadata={"csv": "RE", "description": "Tipo RE"})
    cuota_re: str | None = field(
        metadata={"csv": "Cuota RE", "description": "Cuota RE"}
    )


@dataclass(frozen=True, config=STRICT_CONFIG)
class IrpfWithholding:
    """Printed IRPF withholding, preserving the amount's sign."""

    base_retencion: str | None = field(
        metadata={"csv": "Base Imponible", "description": "Base retención IRPF"}
    )
    tipo_irpf: str | None = field(
        metadata={"csv": "Tipo IRPF", "description": "Tipo IRPF"}
    )
    cuota_irpf: str | None = field(
        metadata={"csv": "Cuota IRPF", "description": "Cuota IRPF"}
    )


@dataclass(frozen=True, config=STRICT_CONFIG)
class Invoice:
    """Complete OCR decision and all readable invoice data."""

    validity: InvoiceValidity
    diagnostic_type: str | None
    fecha: str | None = field(metadata={"csv": "Fecha", "description": "Fecha"})
    numero_factura: str | None = field(
        metadata={"csv": "Nº de factura", "description": "Nº de factura"}
    )
    nif_proveedor: str | None = field(
        metadata={"csv": "NIF proveedor", "description": "NIF proveedor"}
    )
    nombre_proveedor: str | None = field(
        metadata={"csv": "Nombre Proveedor", "description": "Nombre Proveedor"}
    )
    lineas_iva: tuple[IvaLine, ...]
    recargos_equivalencia: tuple[EquivalenceSurcharge, ...]
    retencion_irpf: IrpfWithholding | None
    total: str | None = field(metadata={"csv": "Total", "description": "Total"})

    @classmethod
    def unreadable(cls) -> "Invoice":
        """Return the deterministic result for a document with no readable text."""
        return cls(
            validity=InvoiceValidity.INVALID,
            diagnostic_type="Unreadable",
            fecha=None,
            numero_factura=None,
            nif_proveedor=None,
            nombre_proveedor=None,
            lineas_iva=(),
            recargos_equivalencia=(),
            retencion_irpf=None,
            total=None,
        )


INVOICE_IDENTITY_FIELDS = (
    "fecha",
    "numero_factura",
    "nif_proveedor",
    "nombre_proveedor",
)
