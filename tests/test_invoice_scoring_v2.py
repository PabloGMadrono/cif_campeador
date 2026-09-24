"""Fast, offline checks for the level-two OCR benchmark."""

from dataclasses import replace
from pathlib import Path

import pytest

from src.ocr.models import (
    EquivalenceSurcharge,
    InvoiceValidity,
    IrpfWithholding,
    IvaLine,
)
from tests.invoice_accuracy_v2 import (
    GroundTruthDocument,
    GroundTruthIvaLine,
    GroundTruthSurcharge,
    GroundTruthWithholding,
    OcrScope,
    document_score_dict,
    filter_scope,
    load_ground_truths_v2,
    normalize_field,
    score_document,
    summarize_scores,
    supplier_names_match,
)
from tests.invoice_fixtures import make_invoice

GROUND_TRUTH = Path(__file__).parent / "ground_truths" / "ocr_ground_truth_v2.csv"


def document(**changes):
    values = {
        "filename": "invoice.png",
        "validity": InvoiceValidity.VALID,
        "review_required": False,
        "diagnostic_type": None,
        "notes": None,
        "fecha": "17/08/2022",
        "numero_factura": "2018/2022",
        "nif_proveedor": "B57991598",
        "nombre_proveedor": "Copies Salom S.L.",
        "lineas_iva": (GroundTruthIvaLine("5,12 €", "21,00%", "1,08 €"),),
        "recargos_equivalencia": (),
        "retencion_irpf": None,
        "total": "6,20 €",
        "raw_row_totals": ("6,20 €",),
        "total_derivation": "printed",
    }
    values.update(changes)
    return GroundTruthDocument(**values)


def invoice(**changes):
    values = {
        "fecha": "17/08/2022",
        "numero_factura": "2018/2022",
        "nif_proveedor": "B57991598",
        "nombre_proveedor": "Copies Salom SL",
        "lineas_iva": (IvaLine("5.12", "21", "1.08"),),
        "total": "6.20",
    }
    values.update(changes)
    return make_invoice(**values)


def test_real_csv_is_grouped_without_modifying_source_semantics():
    documents = load_ground_truths_v2(GROUND_TRUTH)
    by_name = {item.filename: item for item in documents}

    assert len(documents) == 45
    assert len(filter_scope(documents, OcrScope.VALID)) == 29
    assert len(filter_scope(documents, OcrScope.INVALID)) == 9
    assert len(filter_scope(documents, OcrScope.VALID_INVALID)) == 38
    assert len(filter_scope(documents, OcrScope.REVIEW)) == 7
    assert len(filter_scope(documents, OcrScope.ALL)) == 45
    assert len(by_name["IMG_3451.HEIC"].lineas_iva) == 2
    assert len(by_name["IMG_3459.HEIC"].lineas_iva) == 2
    assert by_name["IMG_3321.HEIC"].validity is InvoiceValidity.INVALID
    assert by_name["Factura IRPF 1"].validity is None


def test_multiple_row_totals_are_derived_in_memory_and_scored_once():
    documents = {item.filename: item for item in load_ground_truths_v2(GROUND_TRUTH)}

    first = documents["IMG_3451.HEIC"]
    second = documents["IMG_3459.HEIC"]

    assert first.raw_row_totals == ("13,22 €", "6,38 €")
    assert first.total == "19.60"
    assert first.total_derivation == "summed_tax_subtotals"
    assert second.total == "50.05"
    assert second.total_derivation == "summed_tax_subtotals"


def test_supplier_name_ignores_points_and_commas_only_for_that_field():
    expected = document(nombre_proveedor="Copies Salom, S.L.")
    assert score_document(invoice(nombre_proveedor=" COPIES salom SL "), expected).extraction.accuracy == 1
    assert normalize_field("numero_factura", "A.1") != normalize_field("numero_factura", "A1")


@pytest.mark.parametrize(
    ("expected", "obtained"),
    [
        ("Pamadi S.L.", "PAMADI S.L. - PANY MAX"),
        (
            "E232 Media Markt Majadahonda",
            "MEDIA MARKT MAJADAHONDA VIDEO-TV-HIFI-ELEKTRO-COMPUTER-FOTO, S.A.",
        ),
    ],
)
def test_supplier_name_accepts_a_longer_commercial_name(expected, obtained):
    assert supplier_names_match(expected, obtained)


def test_supplier_name_does_not_match_partial_words():
    assert not supplier_names_match("Pamadi S.L.", "Pamadiño Servicios S.L.")


def test_invoice_number_ignores_whitespace_but_preserves_punctuation():
    assert normalize_field("numero_factura", "E232-61145209") == normalize_field(
        "numero_factura", "E232 - 61145209"
    )
    assert normalize_field("numero_factura", "E232-61145209") != normalize_field(
        "numero_factura", "E23261145209"
    )


@pytest.mark.parametrize("field", ["numero_factura", "nif_proveedor"])
def test_numeric_identifiers_ignore_leading_zeroes(field):
    assert normalize_field(field, "000123") == normalize_field(field, "123")


@pytest.mark.parametrize(
    ("field", "with_zeroes", "without_zeroes"),
    [
        ("numero_factura", "FAC-000123", "FAC-123"),
        ("nif_proveedor", "B01234567", "B1234567"),
    ],
)
def test_alphanumeric_identifiers_preserve_leading_zeroes(
    field, with_zeroes, without_zeroes
):
    assert normalize_field(field, with_zeroes) != normalize_field(
        field, without_zeroes
    )


@pytest.mark.parametrize(
    ("field", "expected", "obtained"),
    [
        ("fecha", "17/08/2022", " 17 / 08 / 2022 "),
        ("nif_proveedor", "B57991598", " B 57991598 "),
        ("nombre_proveedor", "Copies Salom S.L.", " Copies  Salom  S.L. "),
        ("total", "1117.04", " 1 117,04 € "),
    ],
)
def test_fields_ignore_whitespace(field, expected, obtained):
    assert normalize_field(field, expected) == normalize_field(field, obtained)


@pytest.mark.parametrize("field", ["tipo_iva", "tipo_re", "tipo_irpf"])
def test_integer_tax_rates_ignore_leading_zeroes(field):
    assert normalize_field(field, "21") == normalize_field(field, "00021")


def test_unannotated_fields_do_not_reward_or_penalize_extraction():
    expected = replace(
        document(),
        fecha=None,
        nif_proveedor=None,
        lineas_iva=(GroundTruthIvaLine(None, None, None),),
    )
    actual = invoice(fecha="wrong", nif_proveedor="invented", total="6.20")

    score = score_document(actual, expected).extraction

    assert score.total == 3
    assert score.matched == 3
    assert {field.field for field in score.fields} == {
        "numero_factura",
        "nombre_proveedor",
        "total",
    }


def test_iva_lines_match_once_each_regardless_of_order():
    expected = replace(
        document(),
        validity=None,
        review_required=True,
        lineas_iva=(
            GroundTruthIvaLine("10", "10", "1"),
            GroundTruthIvaLine("20", "21", "4.2"),
        ),
        total="35.2",
    )
    actual = invoice(
        lineas_iva=(IvaLine("20", "21", "4.2"), IvaLine("10", "10", "1")),
        total="35.2",
    )

    score = score_document(actual, expected)

    assert score.extraction.matched == score.extraction.total == 11
    assert not score.classification.scored


def test_re_and_irpf_are_separate_and_negative_irpf_is_preserved():
    expected = replace(
        document(),
        recargos_equivalencia=(GroundTruthSurcharge("1000", "5.2", "52"),),
        retencion_irpf=GroundTruthWithholding("1000", "15", "-150"),
    )
    actual = invoice(
        recargos_equivalencia=(EquivalenceSurcharge("1000", "5.2", "52"),),
        retencion_irpf=IrpfWithholding("1000", "15", "-150"),
        total="6.20",
    )

    score = score_document(actual, expected)
    summary = summarize_scores([score])

    assert score.extraction.accuracy == 1
    assert summary["extraction"]["by_field"]["lineas_iva.base_imponible"]["total"] == 1
    assert (
        summary["extraction"]["by_field"]
        ["recargos_equivalencia.base_imponible"]["total"]
        == 1
    )
    assert summary["extraction"]["by_field"]["retencion_irpf.base_retencion"]["total"] == 1


def test_classification_is_binary_while_review_is_unscored():
    valid = score_document(invoice(), document())
    invalid = score_document(
        invoice(), replace(document(), validity=InvoiceValidity.INVALID)
    )
    review = score_document(
        invoice(validity=InvoiceValidity.INVALID),
        replace(document(), validity=None, review_required=True),
    )

    assert valid.classification.matched is True
    assert invalid.classification.matched is False
    assert review.classification.scored is False
    assert review.classification.matched is None


def test_diagnostic_type_is_reported_but_never_scored():
    expected = replace(
        document(), validity=InvoiceValidity.INVALID, diagnostic_type="Proforma"
    )
    actual = invoice(
        validity=InvoiceValidity.INVALID, diagnostic_type="Preticket"
    )

    score = score_document(actual, expected)

    assert score.classification.matched is True
    assert score.extraction.accuracy == 1
    assert score.expected_diagnostic_type == "Proforma"
    assert score.obtained_diagnostic_type == "Preticket"


def test_summary_separates_classification_extraction_and_coverage():
    scores = [
        score_document(invoice(), document()),
        score_document(
            invoice(),
            replace(
                document(filename="invalid.png"), validity=InvoiceValidity.INVALID
            ),
        ),
        score_document(
            invoice(validity=InvoiceValidity.INVALID),
            replace(
                document(filename="review.png"),
                validity=None,
                review_required=True,
            ),
        ),
    ]

    summary = summarize_scores(scores)

    assert summary["documents"] == 3
    assert summary["review_documents"] == 1
    assert summary["classification"]["accuracy"] == 0.5
    assert summary["classification"]["false_valid"] == 1
    assert summary["extraction"]["accuracy"] == 1


def test_document_score_export_contains_classification_and_informative_type():
    score = score_document(
        invoice(validity=InvoiceValidity.INVALID, diagnostic_type="Preticket"),
        replace(
            document(), validity=InvoiceValidity.INVALID, diagnostic_type="Proforma"
        ),
    )

    exported = document_score_dict(score)

    assert exported["classification"] == {
        "expected": "invalid",
        "obtained": "invalid",
        "scored": True,
        "matched": True,
    }
    assert exported["expected_diagnostic_type"] == "Proforma"
    assert exported["obtained_diagnostic_type"] == "Preticket"


@pytest.mark.parametrize("value", ["review", "unknown", "Válida"])
def test_invoice_validity_rejects_non_binary_values(value):
    with pytest.raises(ValueError):
        InvoiceValidity(value)
