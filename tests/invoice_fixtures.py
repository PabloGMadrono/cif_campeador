"""Small, explicit builders shared by OCR tests."""

from src.ocr.models import (
    EquivalenceSurcharge,
    Invoice,
    InvoiceValidity,
    IrpfWithholding,
    IvaLine,
)


def make_invoice(
    *,
    validity: InvoiceValidity = InvoiceValidity.VALID,
    diagnostic_type: str | None = None,
    fecha: str | None = None,
    numero_factura: str | None = None,
    nif_proveedor: str | None = None,
    nombre_proveedor: str | None = None,
    lineas_iva: tuple[IvaLine, ...] = (),
    recargos_equivalencia: tuple[EquivalenceSurcharge, ...] = (),
    retencion_irpf: IrpfWithholding | None = None,
    total: str | None = None,
) -> Invoice:
    """Build a complete invoice with readable defaults for tests."""
    return Invoice(
        validity=validity,
        diagnostic_type=diagnostic_type,
        fecha=fecha,
        numero_factura=numero_factura,
        nif_proveedor=nif_proveedor,
        nombre_proveedor=nombre_proveedor,
        lineas_iva=lineas_iva,
        recargos_equivalencia=recargos_equivalencia,
        retencion_irpf=retencion_irpf,
        total=total,
    )
