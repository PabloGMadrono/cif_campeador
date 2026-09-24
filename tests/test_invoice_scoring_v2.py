"""Fast, offline checks for the level-two OCR benchmark."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ocr.models import Invoice
from tests.invoice_accuracy_v2 import (
    DocumentStatus,
    GroundTruthDocument,
    OcrScope,
    TaxLine,
    document_score_dict,
    filter_scope,
    invoice_field_names,
    load_ground_truths_v2,
    normalize_field,
    score_document,
    summarize_scores,
)

GROUND_TRUTH = Path(__file__).parent / "ground_truths" / "ocr_ground_truth_v2.csv"


def invoice(**changes):
    values = {
        "fecha": "17/08/2022",
        "numero_factura": "2018/2022",
        "nif_proveedor": "B57991598",
        "nombre_proveedor": "Copies Salom SL",
        "base_imponible": "5.12",
        "tipo_iva": "21",
        "cuota_iva": "1.08",
        "total": "6.20",
    }
    values.update(changes)
    return Invoice(**values)


def document(**changes):
    values = {
        "filename": "invoice.png",
        "status": DocumentStatus.VALID,
        "review_required": False,
        "diagnostic_type": None,
        "notes": None,
        "fecha": "17/08/2022",
        "numero_factura": "2018/2022",
        "nif_proveedor": "B57991598",
        "nombre_proveedor": "Copies Salom S.L.",
        "tax_lines": (
            TaxLine(
                tipo_re=None,
                cuota_re=None,
                tipo_irpf=None,
                cuota_irpf=None,
                base_imponible="5,12 €",
                tipo_iva="21,00%",
                cuota_iva="1,08 €",
                total="6,20 €",
            ),
        ),
    }
    values.update(changes)
    return GroundTruthDocument(**values)


def result(invoice_value=None, *, status=None, diagnostic_type=None, tax_lines=None):
    values = {
        "invoice": invoice_value or invoice(),
        "document_status": status,
        "diagnostic_type": diagnostic_type,
    }
    if tax_lines is not None:
        values["tax_lines"] = tax_lines
    return SimpleNamespace(**values)


def test_real_v2_ground_truth_is_grouped_and_scopes_are_document_based():
    documents = load_ground_truths_v2(GROUND_TRUTH)

    assert len(documents) == 45
    assert len(filter_scope(documents, OcrScope.VALID)) == 29
    assert len(filter_scope(documents, OcrScope.INVALID)) == 9
    assert len(filter_scope(documents, OcrScope.VALID_INVALID)) == 38
    assert len(filter_scope(documents, OcrScope.REVIEW)) == 7
    assert len(filter_scope(documents, OcrScope.ALL)) == 45

    by_name = {item.filename: item for item in documents}
    assert len(by_name["IMG_3451.HEIC"].tax_lines) == 2
    assert len(by_name["IMG_3459.HEIC"].tax_lines) == 2
    assert by_name["IMG_3321.HEIC"].status is DocumentStatus.INVALID
    assert by_name["IMG_3321.HEIC"].diagnostic_type == "Proforma"
    assert by_name["Factura IRPF 1"].status is None
    assert by_name["Factura IRPF 1"].review_required


def test_supplier_name_ignores_only_requested_punctuation_in_addition_to_base_normalization():
    expected = document(nombre_proveedor="Copies Salom, S.L.")
    actual = invoice(nombre_proveedor="  COPIES salom SL  ")

    assert score_document(actual, expected).extraction.accuracy == 1
    assert normalize_field("nombre_proveedor", "Acme, S.L.") == "acme sl"
    assert normalize_field("numero_factura", "A.1") != normalize_field(
        "numero_factura", "A1"
    )


def test_unannotated_fields_do_not_reward_or_penalize_extraction():
    expected = replace(
        document(),
        fecha=None,
        nif_proveedor=None,
        tax_lines=(
            TaxLine(
                tipo_re=None,
                cuota_re=None,
                tipo_irpf=None,
                cuota_irpf=None,
                base_imponible=None,
                tipo_iva=None,
                cuota_iva=None,
                total="6,20 €",
            ),
        ),
    )
    actual = invoice(
        fecha="wrong",
        nif_proveedor="invented",
        base_imponible="999",
        tipo_iva="99",
        cuota_iva="99",
        total="6.20",
    )

    score = score_document(actual, expected).extraction

    assert score.total == 3  # invoice number, supplier name, and total
    assert score.matched == 3
    assert {field.field for field in score.fields} == {
        "numero_factura",
        "nombre_proveedor",
        "total",
    }


def test_irpf_and_re_are_scored_only_when_annotated_without_changing_invoice_model():
    expected = replace(
        document(),
        tax_lines=(
            TaxLine(
                tipo_re="5,20%",
                cuota_re="75,45 €",
                tipo_irpf="15,00%",
                cuota_irpf="-150,00 €",
                base_imponible="1.000,00 €",
                tipo_iva="21%",
                cuota_iva="210 €",
                total="1.135,45 €",
            ),
        ),
    )
    actual = result(
        invoice(base_imponible="1000", tipo_iva="21", cuota_iva="210", total="1135.45"),
        status=DocumentStatus.VALID,
        tax_lines=[
            {
                "tipo_re": "5.2",
                "cuota_re": "75.45",
                "tipo_irpf": "15",
                "cuota_irpf": "-150",
                "base_imponible": "1000",
                "tipo_iva": "21",
                "cuota_iva": "210",
                "total": "1135.45",
            }
        ],
    )

    assert score_document(actual, expected).extraction.accuracy == 1
    assert invoice_field_names() == {
        "fecha",
        "numero_factura",
        "nif_proveedor",
        "nombre_proveedor",
        "base_imponible",
        "tipo_iva",
        "cuota_iva",
        "total",
    }


def test_multiple_tax_lines_are_matched_once_each_regardless_of_order():
    first = TaxLine(None, None, None, None, "10", "10", "1", "11")
    second = TaxLine(None, None, None, None, "20", "21", "4.2", "24.2")
    expected = replace(
        document(), status=None, review_required=True, tax_lines=(first, second)
    )
    actual = result(
        tax_lines=[
            {
                "base_imponible": "20",
                "tipo_iva": "21",
                "cuota_iva": "4.2",
                "total": "24.2",
            },
            {"base_imponible": "10", "tipo_iva": "10", "cuota_iva": "1", "total": "11"},
        ],
    )

    score = score_document(actual, expected)

    assert score.extraction.total == 12  # four document fields plus two fiscal lines
    assert score.extraction.matched == 12
    assert not score.classification.scored


def test_current_single_tax_line_result_exposes_unextracted_second_line():
    first = TaxLine(None, None, None, None, "5.12", "21", "1.08", "6.20")
    second = TaxLine(None, None, None, None, "10", "10", "1", "11")
    expected = replace(document(), tax_lines=(first, second))

    score = score_document(invoice(), expected).extraction

    assert score.matched == 8
    assert score.total == 12
    assert score.mismatches == (
        "tax_lines[1].base_imponible",
        "tax_lines[1].tipo_iva",
        "tax_lines[1].cuota_iva",
        "tax_lines[1].total",
    )


def test_classification_is_binary_and_review_is_explicitly_unscored():
    valid = score_document(result(status="valid"), document())
    invalid = score_document(
        result(status="valid"), replace(document(), status=DocumentStatus.INVALID)
    )
    review = score_document(
        result(status="invalid"), replace(document(), status=None, review_required=True)
    )

    assert valid.classification.matched is True
    assert invalid.classification.matched is False
    assert review.classification.scored is False
    assert review.classification.matched is None


def test_diagnostic_type_is_reported_but_never_scored():
    expected = replace(
        document(), diagnostic_type="Proforma", status=DocumentStatus.INVALID
    )
    score = score_document(
        result(status="invalid", diagnostic_type="Preticket"), expected
    )

    assert score.expected_diagnostic_type == "Proforma"
    assert score.obtained_diagnostic_type == "Preticket"
    assert score.classification.matched is True
    assert score.extraction.accuracy == 1


def test_summary_separates_classification_extraction_and_field_coverage():
    scores = [
        score_document(result(status="valid"), document()),
        score_document(
            result(status="valid"),
            replace(document(filename="bad.png"), status=DocumentStatus.INVALID),
        ),
        score_document(
            result(status="invalid"),
            replace(document(filename="review.png"), status=None, review_required=True),
        ),
    ]

    summary = summarize_scores(scores)

    assert summary["documents"] == 3
    assert summary["review_documents"] == 1
    assert summary["classification"]["matched"] == 1
    assert summary["classification"]["total"] == 2
    assert summary["classification"]["accuracy"] == 0.5
    assert summary["classification"]["balanced_accuracy"] == 0.5
    assert summary["classification"]["false_valid"] == 1
    assert summary["classification"]["false_invalid"] == 0
    assert summary["classification"]["confusion_matrix"]["invalid"]["valid"] == 1
    assert summary["extraction"]["matched"] == 24
    assert summary["extraction"]["total"] == 24
    assert summary["extraction"]["possible_fields"] == 36
    assert summary["extraction"]["ignored_unannotated"] == 12
    assert summary["extraction"]["coverage"] == 2 / 3
    assert summary["extraction"]["by_field"]["tipo_irpf"]["total"] == 0


def test_document_score_export_contains_informative_types_and_scoring_flags():
    score = score_document(
        result(status="invalid", diagnostic_type="Preticket"),
        replace(document(), status=DocumentStatus.INVALID, diagnostic_type="Proforma"),
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
    assert exported["extraction"]["total"] == 8


@pytest.mark.parametrize("value", ["review", "unknown", "Válida"])
def test_document_status_cannot_represent_review_or_free_form_values(value):
    with pytest.raises(ValueError):
        DocumentStatus(value)
