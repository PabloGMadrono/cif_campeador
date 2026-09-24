"""Level-two OCR ground truths and scoring, independent of an OCR backend.

The production ``Invoice`` model deliberately remains unchanged.  This module
adapts its current eight fields to the richer benchmark and can also consume a
future result wrapper exposing ``document_status``, ``diagnostic_type`` and
``tax_lines``.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

from src.ocr.models import Invoice


class DocumentStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"


class OcrScope(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    VALID_INVALID = "valid-invalid"
    REVIEW = "review"
    ALL = "all"

    def includes(self, document: GroundTruthDocument) -> bool:
        if self is OcrScope.ALL:
            return True
        if self is OcrScope.REVIEW:
            return document.review_required
        if self is OcrScope.VALID_INVALID:
            return document.status is not None
        expected = DocumentStatus(self.value)
        return document.status is expected


DOCUMENT_FIELDS = (
    ("fecha", "Fecha"),
    ("numero_factura", "Nº de factura"),
    ("nif_proveedor", "NIF proveedor"),
    ("nombre_proveedor", "Nombre Proveedor"),
)
TAX_FIELDS = (
    ("tipo_re", "RE"),
    ("cuota_re", "Cuota RE"),
    ("tipo_irpf", "Tipo IRPF"),
    ("cuota_irpf", "Cuota IRPF"),
    ("base_imponible", "Base Imponible"),
    ("tipo_iva", "Tipo IVA %"),
    ("cuota_iva", "Cuota IVA"),
    ("total", "Total"),
)
FIELD_LABELS = dict(DOCUMENT_FIELDS + TAX_FIELDS)
NUMERIC_FIELDS = {
    "tipo_re",
    "cuota_re",
    "tipo_irpf",
    "cuota_irpf",
    "base_imponible",
    "tipo_iva",
    "cuota_iva",
    "total",
}
MISSING_MARKERS = {"", "-"}
CSV_COLUMNS = {
    "Nombre foto",
    "Fecha",
    "Nº de factura",
    "NIF proveedor",
    "Nombre Proveedor",
    "RE",
    "Cuota RE",
    "Tipo IRPF",
    "Cuota IRPF",
    "Base Imponible",
    "Tipo IVA %",
    "Cuota IVA",
    "Total",
    "Tipo",
    "Deducible",
    "Notas",
}


@dataclass(frozen=True)
class TaxLine:
    tipo_re: str | None
    cuota_re: str | None
    tipo_irpf: str | None
    cuota_irpf: str | None
    base_imponible: str | None
    tipo_iva: str | None
    cuota_iva: str | None
    total: str | None


@dataclass(frozen=True)
class GroundTruthDocument:
    filename: str
    status: DocumentStatus | None
    review_required: bool
    diagnostic_type: str | None
    notes: str | None
    fecha: str | None
    numero_factura: str | None
    nif_proveedor: str | None
    nombre_proveedor: str | None
    tax_lines: tuple[TaxLine, ...]


@dataclass(frozen=True)
class FieldResult:
    key: str
    field: str
    expected: str
    obtained: str | None
    matched: bool


@dataclass(frozen=True)
class ExtractionScore:
    fields: tuple[FieldResult, ...]
    possible_fields: int

    @property
    def matched(self) -> int:
        return sum(result.matched for result in self.fields)

    @property
    def total(self) -> int:
        return len(self.fields)

    @property
    def accuracy(self) -> float | None:
        return self.matched / self.total if self.total else None

    @property
    def coverage(self) -> float:
        return self.total / self.possible_fields if self.possible_fields else 0.0

    @property
    def mismatches(self) -> tuple[str, ...]:
        return tuple(result.key for result in self.fields if not result.matched)


@dataclass(frozen=True)
class ClassificationScore:
    expected: DocumentStatus | None
    obtained: DocumentStatus | None

    @property
    def scored(self) -> bool:
        return self.expected is not None

    @property
    def matched(self) -> bool | None:
        return self.obtained is self.expected if self.scored else None


@dataclass(frozen=True)
class DocumentScore:
    filename: str
    classification: ClassificationScore
    extraction: ExtractionScore
    expected_diagnostic_type: str | None
    obtained_diagnostic_type: str | None


def is_annotated(value: str | None) -> bool:
    return isinstance(value, str) and value.strip() not in MISSING_MARKERS


def normalize_field(name: str, value: str | None):
    """Normalize an annotated value; missing markers share one representation."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    value = " ".join(unicodedata.normalize("NFC", value).casefold().split())
    if value in MISSING_MARKERS:
        return None
    if name == "fecha":
        try:
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
                day, month, year = map(int, value.split("/"))
                return date(year, month, day)
            return date.fromisoformat(value)
        except ValueError:
            pass
        raise ValueError(f"Invalid invoice date: {value!r}")
    if name in NUMERIC_FIELDS:
        value = value.removesuffix("%").removesuffix("€").strip()
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+),\d+", value):
            value = value.replace(".", "").replace(",", ".")
        elif not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
            raise ValueError(f"Invalid numeric field {name}: {value!r}")
        return Decimal(value)
    if name == "nombre_proveedor":
        # Supplier punctuation is non-critical. Removing rather than replacing
        # punctuation makes both "S.L." and "SL" equivalent.
        return re.sub(r"[.,]", "", value)
    return value


def _reference_value(name: str, raw: str, line: int) -> str | None:
    value = raw.strip()
    if not is_annotated(value):
        return None
    try:
        normalize_field(name, value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid {FIELD_LABELS[name]} on ground-truth row {line}: {error}"
        ) from error
    return value


def _ground_truth_status(raw: str, line: int) -> tuple[DocumentStatus | None, bool]:
    value = unicodedata.normalize("NFC", raw).strip().casefold()
    if value == "válida":
        return DocumentStatus.VALID, False
    if value == "no válida":
        return DocumentStatus.INVALID, False
    if value == "revisar":
        return None, True
    raise ValueError(f"Unknown Deducible value on ground-truth row {line}: {raw!r}")


def load_ground_truths_v2(path: Path) -> list[GroundTruthDocument]:
    """Load and group v2 rows, preserving multiple fiscal lines per image."""
    grouped: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        original_headers = reader.fieldnames or []
        headers = [header.strip() for header in original_headers]
        if len(headers) != len(CSV_COLUMNS) or set(headers) != CSV_COLUMNS:
            raise ValueError(f"Unexpected v2 ground-truth columns: {headers}")
        reader.fieldnames = headers
        for line, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed v2 ground-truth row {line}")
            filename = row["Nombre foto"].strip()
            if not filename:
                raise ValueError(f"Missing image on ground-truth row {line}")
            status, review_required = _ground_truth_status(row["Deducible"], line)
            diagnostic_type = row["Tipo"].strip() or None
            notes = row["Notas"].strip() or None
            document_values = {
                name: _reference_value(name, row[label], line)
                for name, label in DOCUMENT_FIELDS
            }
            tax_line = TaxLine(
                **{
                    name: _reference_value(name, row[label], line)
                    for name, label in TAX_FIELDS
                }
            )
            identity = filename.casefold()
            if identity not in grouped:
                grouped[identity] = {
                    "filename": filename,
                    "status": status,
                    "review_required": review_required,
                    "diagnostic_type": diagnostic_type,
                    "notes": notes,
                    **document_values,
                    "tax_lines": [tax_line],
                }
                continue
            current = grouped[identity]
            stable = {
                "status": status,
                "review_required": review_required,
                "diagnostic_type": diagnostic_type,
                **document_values,
            }
            conflicts = [
                name for name, value in stable.items() if current[name] != value
            ]
            if conflicts:
                raise ValueError(
                    f"Conflicting document values for {filename!r} on row {line}: "
                    + ", ".join(conflicts)
                )
            if notes and current["notes"] and notes != current["notes"]:
                raise ValueError(f"Conflicting notes for {filename!r} on row {line}")
            current["notes"] = current["notes"] or notes
            current["tax_lines"].append(tax_line)
    if not grouped:
        raise ValueError("Ground-truth v2 CSV contains no documents")
    return [
        GroundTruthDocument(**{**values, "tax_lines": tuple(values["tax_lines"])})
        for values in grouped.values()
    ]


def filter_scope(
    documents: Iterable[GroundTruthDocument],
    scope: OcrScope | str,
) -> list[GroundTruthDocument]:
    selected = OcrScope(scope)
    return [document for document in documents if selected.includes(document)]


def _value(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _parse_status(value: Any) -> DocumentStatus | None:
    if value is None:
        return None
    if isinstance(value, DocumentStatus):
        return value
    try:
        return DocumentStatus(str(value).strip().casefold())
    except ValueError:
        return None


def _actual_parts(
    actual: Any,
) -> tuple[Any, list[Any], DocumentStatus | None, str | None]:
    if isinstance(actual, Invoice):
        invoice = actual
        tax_lines = [actual]
    else:
        invoice = _value(actual, "invoice")
        explicit_lines = _value(actual, "tax_lines")
        tax_lines = (
            list(explicit_lines)
            if explicit_lines is not None
            else ([invoice] if invoice else [])
        )
    status = _parse_status(_value(actual, "document_status"))
    diagnostic = _value(actual, "diagnostic_type")
    return invoice, tax_lines, status, str(diagnostic).strip() if diagnostic else None


def _compare(name: str, expected: str, obtained: Any) -> bool:
    try:
        return normalize_field(name, obtained) == normalize_field(name, expected)
    except (TypeError, ValueError):
        return False


def _line_match_count(expected: TaxLine, obtained: Any) -> int:
    return sum(
        _compare(name, value, _value(obtained, name))
        for name, _ in TAX_FIELDS
        if (value := getattr(expected, name)) is not None
    )


def _best_line_assignment(
    expected: tuple[TaxLine, ...], obtained: list[Any]
) -> tuple[int | None, ...]:
    """Pair tax lines one-to-one to maximize matched annotated fields."""

    @cache
    def visit(
        expected_index: int, used: tuple[int, ...]
    ) -> tuple[int, tuple[int | None, ...]]:
        if expected_index == len(expected):
            return 0, ()
        used_set = set(used)
        choices: list[tuple[int, tuple[int | None, ...]]] = []
        remaining_score, remaining_assignment = visit(expected_index + 1, used)
        choices.append((remaining_score, (None,) + remaining_assignment))
        for obtained_index, actual_line in enumerate(obtained):
            if obtained_index in used_set:
                continue
            tail_score, tail_assignment = visit(
                expected_index + 1, tuple(sorted((*used, obtained_index)))
            )
            score = (
                _line_match_count(expected[expected_index], actual_line) + tail_score
            )
            choices.append((score, (obtained_index,) + tail_assignment))
        return max(
            choices,
            key=lambda choice: (
                choice[0],
                sum(index is not None for index in choice[1]),
            ),
        )

    return visit(0, ())[1]


def score_document(actual: Any, expected: GroundTruthDocument) -> DocumentScore:
    invoice, actual_lines, actual_status, diagnostic_type = _actual_parts(actual)
    results: list[FieldResult] = []
    for name, _ in DOCUMENT_FIELDS:
        reference = getattr(expected, name)
        if reference is None:
            continue
        obtained = _value(invoice, name)
        results.append(
            FieldResult(
                key=name,
                field=name,
                expected=reference,
                obtained=obtained,
                matched=_compare(name, reference, obtained),
            )
        )
    assignment = _best_line_assignment(expected.tax_lines, actual_lines)
    for line_index, (expected_line, actual_index) in enumerate(
        zip(expected.tax_lines, assignment)
    ):
        actual_line = actual_lines[actual_index] if actual_index is not None else None
        for name, _ in TAX_FIELDS:
            reference = getattr(expected_line, name)
            if reference is None:
                continue
            obtained = _value(actual_line, name)
            results.append(
                FieldResult(
                    key=f"tax_lines[{line_index}].{name}",
                    field=name,
                    expected=reference,
                    obtained=obtained,
                    matched=_compare(name, reference, obtained),
                )
            )
    return DocumentScore(
        filename=expected.filename,
        classification=ClassificationScore(expected.status, actual_status),
        extraction=ExtractionScore(
            tuple(results),
            len(DOCUMENT_FIELDS) + len(TAX_FIELDS) * len(expected.tax_lines),
        ),
        expected_diagnostic_type=expected.diagnostic_type,
        obtained_diagnostic_type=diagnostic_type,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize_scores(scores: Iterable[DocumentScore]) -> dict[str, Any]:
    scores = list(scores)
    classified = [
        score.classification for score in scores if score.classification.scored
    ]
    correct = sum(classification.matched is True for classification in classified)
    confusion = {
        expected.value: {obtained: 0 for obtained in ("valid", "invalid", "missing")}
        for expected in DocumentStatus
    }
    for classification in classified:
        obtained = (
            classification.obtained.value if classification.obtained else "missing"
        )
        confusion[classification.expected.value][obtained] += 1

    classification_by_status = {}
    for status in DocumentStatus:
        true_positive = confusion[status.value][status.value]
        expected_total = sum(confusion[status.value].values())
        predicted_total = sum(row[status.value] for row in confusion.values())
        precision = (
            _ratio(true_positive, predicted_total)
            if predicted_total
            else (0.0 if expected_total else None)
        )
        recall = _ratio(true_positive, expected_total)
        f1 = None
        if precision is not None and recall is not None:
            f1 = (
                0.0
                if precision + recall == 0
                else 2 * precision * recall / (precision + recall)
            )
        classification_by_status[status.value] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "expected": expected_total,
            "predicted": predicted_total,
        }

    field_summary = {}
    all_fields = [field for score in scores for field in score.extraction.fields]
    for name, label in DOCUMENT_FIELDS + TAX_FIELDS:
        values = [field for field in all_fields if field.field == name]
        matched = sum(field.matched for field in values)
        field_summary[name] = {
            "label": label,
            "matched": matched,
            "total": len(values),
            "accuracy": _ratio(matched, len(values)),
        }
    extracted_correct = sum(field.matched for field in all_fields)
    possible_fields = sum(score.extraction.possible_fields for score in scores)
    recalls = [metrics["recall"] for metrics in classification_by_status.values()]
    f1_scores = [metrics["f1"] for metrics in classification_by_status.values()]
    by_diagnostic_type = {}
    diagnostic_types = {score.expected_diagnostic_type or "untyped" for score in scores}
    for diagnostic_type in sorted(diagnostic_types):
        group = [
            score
            for score in scores
            if (score.expected_diagnostic_type or "untyped") == diagnostic_type
        ]
        group_classifications = [
            score.classification for score in group if score.classification.scored
        ]
        group_fields = [field for score in group for field in score.extraction.fields]
        by_diagnostic_type[diagnostic_type] = {
            "documents": len(group),
            "classification_matched": sum(
                item.matched is True for item in group_classifications
            ),
            "classification_total": len(group_classifications),
            "extraction_matched": sum(field.matched for field in group_fields),
            "extraction_total": len(group_fields),
        }
    return {
        "documents": len(scores),
        "review_documents": sum(not score.classification.scored for score in scores),
        "classification": {
            "matched": correct,
            "total": len(classified),
            "accuracy": _ratio(correct, len(classified)),
            "confusion_matrix": confusion,
            "by_status": classification_by_status,
            "balanced_accuracy": (
                sum(recall for recall in recalls if recall is not None)
                / sum(recall is not None for recall in recalls)
                if any(recall is not None for recall in recalls)
                else None
            ),
            "macro_f1": (
                sum(f1 for f1 in f1_scores if f1 is not None)
                / sum(f1 is not None for f1 in f1_scores)
                if any(f1 is not None for f1 in f1_scores)
                else None
            ),
            "false_valid": confusion[DocumentStatus.INVALID.value][
                DocumentStatus.VALID.value
            ],
            "false_invalid": confusion[DocumentStatus.VALID.value][
                DocumentStatus.INVALID.value
            ],
        },
        "extraction": {
            "matched": extracted_correct,
            "total": len(all_fields),
            "accuracy": _ratio(extracted_correct, len(all_fields)),
            "possible_fields": possible_fields,
            "ignored_unannotated": possible_fields - len(all_fields),
            "coverage": _ratio(len(all_fields), possible_fields),
            "by_field": field_summary,
        },
        "by_diagnostic_type": by_diagnostic_type,
    }


def document_score_dict(score: DocumentScore) -> dict[str, Any]:
    """JSON-safe representation used by the level-two report writer."""
    result = asdict(score)
    for side in ("expected", "obtained"):
        status = result["classification"][side]
        result["classification"][side] = (
            status.value if isinstance(status, DocumentStatus) else status
        )
    result["extraction"].update(
        {
            "matched": score.extraction.matched,
            "total": score.extraction.total,
            "accuracy": score.extraction.accuracy,
            "coverage": score.extraction.coverage,
            "mismatches": score.extraction.mismatches,
        }
    )
    result["classification"].update(
        {
            "scored": score.classification.scored,
            "matched": score.classification.matched,
        }
    )
    return result


def invoice_field_names() -> set[str]:
    """Expose the unchanged production boundary for regression tests."""
    return {item.name for item in fields(Invoice)}
