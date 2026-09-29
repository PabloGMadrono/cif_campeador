"""Deterministic checks for invoice accounting reconciliation."""

import pytest

from src.invoices.accounting import reconcile_invoice
from src.invoices.domain import FiscalStatus
from src.ocr.models import EquivalenceSurcharge, IrpfWithholding, IvaLine
from tests.invoice_fixtures import make_invoice


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (IvaLine(None, "21", "21.00"), IvaLine("100.00", "21", "21.00")),
        (IvaLine("100.00", None, "21.00"), IvaLine("100.00", "21", "21.00")),
        (IvaLine("100.00", "21", None), IvaLine("100.00", "21", "21.00")),
    ],
)
def test_fills_each_missing_vat_value(line, expected):
    result = reconcile_invoice(make_invoice(lineas_iva=(line,), total="121.00"))

    assert result.invoice.lineas_iva == (expected,)
    assert result.status is FiscalStatus.CORRECTED


def test_splits_a_vat_included_total_using_the_printed_rate():
    invoice = make_invoice(
        lineas_iva=(IvaLine(None, "21", None),),
        total="121.00",
    )

    result = reconcile_invoice(invoice)

    assert result.invoice.lineas_iva == (IvaLine("100.00", "21", "21.00"),)
    assert result.status is FiscalStatus.CORRECTED


def test_fills_a_missing_total_from_consistent_fiscal_values():
    invoice = make_invoice(
        lineas_iva=(IvaLine("100.00", "21", "21.00"),),
        total=None,
    )

    result = reconcile_invoice(invoice)

    assert result.invoice.total == "121.00"
    assert result.status is FiscalStatus.CORRECTED


def test_reconciles_surcharge_and_signed_withholding():
    invoice = make_invoice(
        lineas_iva=(IvaLine("100.00", "21", "21.00"),),
        recargos_equivalencia=(EquivalenceSurcharge("100.00", "5.2", "5.20"),),
        retencion_irpf=IrpfWithholding("100.00", "15", "-15.00"),
        total="111.20",
    )

    result = reconcile_invoice(invoice)

    assert result.invoice == invoice
    assert result.status is FiscalStatus.RECONCILED


def test_corrects_the_only_value_that_satisfies_row_and_total():
    invoice = make_invoice(
        lineas_iva=(IvaLine("100.00", "21", "22.00"),),
        total="121.00",
    )

    result = reconcile_invoice(invoice)

    assert result.invoice.lineas_iva == (IvaLine("100.00", "21", "21.00"),)
    assert result.status is FiscalStatus.CORRECTED


def test_preserves_an_unresolvable_conflict():
    invoice = make_invoice(
        lineas_iva=(IvaLine("100.00", "21", "22.00"),),
        total="130.00",
    )

    result = reconcile_invoice(invoice)

    assert result.invoice == invoice
    assert result.status is FiscalStatus.MATH_ERROR


def test_reports_missing_data_without_inventing_a_base():
    invoice = make_invoice(
        lineas_iva=(IvaLine(None, None, None),),
        total="121.00",
    )

    result = reconcile_invoice(invoice)

    assert result.invoice == invoice
    assert result.status is FiscalStatus.MISSING_DATA
