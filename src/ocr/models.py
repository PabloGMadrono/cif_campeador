"""Public invoice result shared by OCR implementations and their consumers."""

from dataclasses import field, fields

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass


@dataclass(frozen=True, config=ConfigDict(extra="forbid", strict=True))
class Invoice:
    """The eight invoice fields, excluding image filename and annotation notes.

    Return text values, preserving identifiers (including leading zeroes).
    Use None or an empty string for absent fields. Amounts are in euros and
    VAT rates are percentage points, e.g. "21.00" or "21,00%", not "0.21".
    Dates may be DD/MM/YYYY or YYYY-MM-DD. CSV metadata records the external
    column names; neither image paths nor ground truths belong in this model.
    All fields must be supplied, with None for unknown values. Use empty() for
    an invoice where no fields could be read.
    """

    fecha: str | None = field(metadata={"csv": "Fecha", "description": "Fecha"})
    numero_factura: str | None = field(metadata={"csv": "Nº de factura", "description": "Nº de factura"})
    nif_proveedor: str | None = field(metadata={"csv": "NIF proveedor", "description": "NIF proveedor"})
    nombre_proveedor: str | None = field(metadata={"csv": "Nombre Proveedor", "description": "Nombre Proveedor"})
    base_imponible: str | None = field(metadata={"csv": "Base Imponible", "description": "Base Imponible"})
    tipo_iva: str | None = field(metadata={"csv": "Tipo IVA %", "description": "Tipo IVA %"})
    cuota_iva: str | None = field(metadata={"csv": "Cuota IVA", "description": "Cuota IVA"})
    total: str | None = field(metadata={"csv": "Total", "description": "Total"})

    @classmethod
    def empty(cls) -> "Invoice":
        """Return a valid invoice with every field explicitly unknown."""
        return cls(**{item.name: None for item in fields(cls)})
