"""Accounting checks applied after OCR and before persistence."""

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from src.invoices.domain import FiscalStatus
from src.ocr.models import Invoice, IvaLine

CENT = Decimal("0.01")
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class AccountingResult:
    invoice: Invoice
    status: FiscalStatus


def reconcile_invoice(invoice: Invoice) -> AccountingResult:
    """Fill derivable VAT values and check the invoice's fiscal total."""
    reconciled = replace(
        invoice,
        lineas_iva=tuple(_fill_vat_line(line) for line in invoice.lineas_iva),
    )
    reconciled = _fill_from_total(reconciled)

    if not _has_required_values(reconciled):
        return AccountingResult(reconciled, FiscalStatus.MISSING_DATA)
    if _is_consistent(reconciled):
        status = (
            FiscalStatus.RECONCILED
            if reconciled == invoice
            else FiscalStatus.CORRECTED
        )
        return AccountingResult(reconciled, status)

    corrections = _valid_single_field_corrections(reconciled)
    if len(corrections) == 1:
        return AccountingResult(corrections[0], FiscalStatus.CORRECTED)
    return AccountingResult(reconciled, FiscalStatus.MATH_ERROR)


def _fill_vat_line(line: IvaLine) -> IvaLine:
    base = _decimal(line.base_imponible)
    rate = _decimal(line.tipo_iva)
    quota = _decimal(line.cuota_iva)

    if base is not None and rate is not None and quota is None:
        return replace(line, cuota_iva=_money(base * rate / HUNDRED))
    if base is not None and quota is not None and rate is None and base != 0:
        return replace(line, tipo_iva=_number(quota * HUNDRED / base))
    if rate is not None and quota is not None and base is None and rate != 0:
        return replace(line, base_imponible=_money(quota * HUNDRED / rate))
    return line


def _fill_from_total(invoice: Invoice) -> Invoice:
    total = _decimal(invoice.total)
    if total is None:
        if not invoice.lineas_iva:
            return invoice
        fiscal_total = _known_fiscal_total(invoice)
        if fiscal_total is not None and all(
            _vat_line_is_consistent(line) for line in invoice.lineas_iva
        ):
            return replace(invoice, total=_money(fiscal_total))
        return invoice

    incomplete = [
        index
        for index, line in enumerate(invoice.lineas_iva)
        if line.base_imponible is None or line.cuota_iva is None
    ]
    if len(incomplete) != 1:
        return invoice

    index = incomplete[0]
    line = invoice.lineas_iva[index]
    rate = _decimal(line.tipo_iva)
    known_total = _known_fiscal_total(invoice, skip_vat_index=index)
    if rate is None or known_total is None:
        return invoice

    gross = total - known_total
    base = (gross / (Decimal(1) + rate / HUNDRED)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    quota = gross - base
    if quota.quantize(CENT, rounding=ROUND_HALF_UP) != (
        base * rate / HUNDRED
    ).quantize(CENT, rounding=ROUND_HALF_UP):
        return invoice

    filled = replace(
        line,
        base_imponible=line.base_imponible or _money(base),
        cuota_iva=line.cuota_iva or _money(quota),
    )
    lines = list(invoice.lineas_iva)
    lines[index] = filled
    return replace(invoice, lineas_iva=tuple(lines))


def _valid_single_field_corrections(invoice: Invoice) -> list[Invoice]:
    candidates: list[Invoice] = []
    lines = list(invoice.lineas_iva)

    for index, line in enumerate(lines):
        base = _decimal(line.base_imponible)
        rate = _decimal(line.tipo_iva)
        quota = _decimal(line.cuota_iva)
        replacements: list[IvaLine] = []
        if base is not None and rate is not None:
            replacements.append(replace(line, cuota_iva=_money(base * rate / HUNDRED)))
        if base not in (None, Decimal(0)) and quota is not None:
            replacements.append(replace(line, tipo_iva=_number(quota * HUNDRED / base)))
        if rate not in (None, Decimal(0)) and quota is not None:
            replacements.append(replace(line, base_imponible=_money(quota * HUNDRED / rate)))

        for replacement in replacements:
            if replacement == line:
                continue
            changed = lines.copy()
            changed[index] = replacement
            candidate = replace(invoice, lineas_iva=tuple(changed))
            if _is_consistent(candidate) and candidate not in candidates:
                candidates.append(candidate)

    fiscal_total = _known_fiscal_total(invoice)
    if fiscal_total is not None:
        candidate = replace(invoice, total=_money(fiscal_total))
        if candidate != invoice and _is_consistent(candidate) and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _has_required_values(invoice: Invoice) -> bool:
    if not invoice.lineas_iva or _decimal(invoice.total) is None:
        return False
    if any(
        _decimal(value) is None
        for line in invoice.lineas_iva
        for value in (line.base_imponible, line.tipo_iva, line.cuota_iva)
    ):
        return False
    return _known_fiscal_total(invoice) is not None


def _is_consistent(invoice: Invoice) -> bool:
    total = _decimal(invoice.total)
    fiscal_total = _known_fiscal_total(invoice)
    if total is None or fiscal_total is None:
        return False
    return all(_vat_line_is_consistent(line) for line in invoice.lineas_iva) and (
        fiscal_total == total
    )


def _vat_line_is_consistent(line: IvaLine) -> bool:
    base = _decimal(line.base_imponible)
    rate = _decimal(line.tipo_iva)
    quota = _decimal(line.cuota_iva)
    if None in (base, rate, quota):
        return False
    return (base * rate / HUNDRED).quantize(
        CENT, rounding=ROUND_HALF_UP
    ) == quota


def _known_fiscal_total(
    invoice: Invoice, skip_vat_index: int | None = None
) -> Decimal | None:
    total = Decimal(0)
    for index, line in enumerate(invoice.lineas_iva):
        if index == skip_vat_index:
            continue
        base = _decimal(line.base_imponible)
        quota = _decimal(line.cuota_iva)
        if base is None or quota is None:
            return None
        total += base + quota
    for surcharge in invoice.recargos_equivalencia:
        quota = _decimal(surcharge.cuota_re)
        if quota is None:
            return None
        total += quota
    if invoice.retencion_irpf:
        withholding = _decimal(invoice.retencion_irpf.cuota_irpf)
        if withholding is None:
            return None
        total += withholding
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
        if not number.is_finite():
            return None
        return number.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")
