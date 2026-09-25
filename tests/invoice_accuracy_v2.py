"""Ground-truth loading and scoring for the level-two OCR benchmark."""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from src.ocr.models import Invoice, InvoiceValidity


class OcrScope(StrEnum):
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
            return document.validity is not None
        return document.validity is InvoiceValidity(self.value)


def image_index(directory: Path) -> dict[str, list[Path]]:
    """Index files recursively by case-insensitive basename."""
    index: dict[str, list[Path]] = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            index.setdefault(path.name.casefold(), []).append(path)
    return index


def resolve_image(directory: Path, filename: str, index: dict[str, list[Path]]) -> Path:
    """Resolve one unique basename, retaining a useful path when it is absent."""
    matches = index.get(filename.casefold(), [])
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous invoice image {filename}: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0] if matches else Path(directory) / filename


DOCUMENT_FIELDS = (
    ("fecha", "Fecha"),
    ("numero_factura", "Nº de factura"),
    ("nif_proveedor", "NIF proveedor"),
    ("nombre_proveedor", "Nombre Proveedor"),
    ("total", "Total"),
)
VAT_FIELDS = (
    ("base_imponible", "Base Imponible"),
    ("tipo_iva", "Tipo IVA %"),
    ("cuota_iva", "Cuota IVA"),
)
RE_FIELDS = (
    ("base_imponible", "Base Imponible RE"),
    ("tipo_re", "RE"),
    ("cuota_re", "Cuota RE"),
)
IRPF_FIELDS = (
    ("base_retencion", "Base retención IRPF"),
    ("tipo_irpf", "Tipo IRPF"),
    ("cuota_irpf", "Cuota IRPF"),
)
SUMMARY_FIELDS = (
    DOCUMENT_FIELDS
    + tuple((f"lineas_iva.{name}", label) for name, label in VAT_FIELDS)
    + tuple(
        (f"recargos_equivalencia.{name}", label) for name, label in RE_FIELDS
    )
    + tuple((f"retencion_irpf.{name}", label) for name, label in IRPF_FIELDS)
)
FIELD_LABELS = {}
for field_name, field_label in DOCUMENT_FIELDS + VAT_FIELDS + RE_FIELDS + IRPF_FIELDS:
    FIELD_LABELS.setdefault(field_name, field_label)
NUMERIC_FIELDS = {
    "total",
    "base_imponible",
    "tipo_iva",
    "cuota_iva",
    "tipo_re",
    "cuota_re",
    "base_retencion",
    "tipo_irpf",
    "cuota_irpf",
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
class GroundTruthIvaLine:
    base_imponible: str | None
    tipo_iva: str | None
    cuota_iva: str | None


@dataclass(frozen=True)
class GroundTruthSurcharge:
    base_imponible: str | None
    tipo_re: str | None
    cuota_re: str | None


@dataclass(frozen=True)
class GroundTruthWithholding:
    base_retencion: str | None
    tipo_irpf: str | None
    cuota_irpf: str | None


@dataclass(frozen=True)
class GroundTruthDocument:
    filename: str
    validity: InvoiceValidity | None
    review_required: bool
    diagnostic_type: str | None
    notes: str | None
    fecha: str | None
    numero_factura: str | None
    nif_proveedor: str | None
    nombre_proveedor: str | None
    lineas_iva: tuple[GroundTruthIvaLine, ...]
    recargos_equivalencia: tuple[GroundTruthSurcharge, ...]
    retencion_irpf: GroundTruthWithholding | None
    total: str | None
    raw_row_totals: tuple[str | None, ...]
    total_derivation: str


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
    expected: InvoiceValidity | None
    obtained: InvoiceValidity | None

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
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    value = "".join(unicodedata.normalize("NFC", value).casefold().split())
    if value in MISSING_MARKERS:
        return None
    if name == "nif_proveedor":
        value = value.replace("-", "")
    if name in {"numero_factura", "nif_proveedor"} and value.isdecimal():
        return str(int(value))
    if name == "fecha":
        try:
            if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
                day, month, year = map(int, value.split("/"))
                return date(year, month, day)
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"Invalid invoice date: {value!r}") from error
    if name in NUMERIC_FIELDS:
        value = value.removesuffix("%").removesuffix("€").strip()
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+),\d+", value):
            value = value.replace(".", "").replace(",", ".")
        elif not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
            raise ValueError(f"Invalid numeric field {name}: {value!r}")
        return Decimal(value)
    if name == "nombre_proveedor":
        return "".join(re.sub(r"[.,]", "", value).split())
    return value


def supplier_names_match(expected: str, obtained: str | None) -> bool:
    if obtained is None:
        return False
    if normalize_field("nombre_proveedor", expected) == normalize_field(
        "nombre_proveedor", obtained
    ):
        return True

    expected_words = _supplier_name_words(expected)
    obtained_words = _supplier_name_words(obtained)
    if not expected_words or not obtained_words:
        return False

    shorter, longer = sorted((expected_words, obtained_words), key=len)
    width = len(shorter)
    return any(longer[index : index + width] == shorter for index in range(len(longer) - width + 1))


def _supplier_name_words(value: str) -> tuple[str, ...]:
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value).casefold())
    legal_forms = {"sa", "sal", "sl", "slu", "slne"}
    return tuple(
        word
        for word in words
        if len(word) > 1 and not any(character.isdigit() for character in word) and word not in legal_forms
    )


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


def _ground_truth_status(raw: str, line: int) -> tuple[InvoiceValidity | None, bool]:
    value = unicodedata.normalize("NFC", raw).strip().casefold()
    if value == "válida":
        return InvoiceValidity.VALID, False
    if value == "no válida":
        return InvoiceValidity.INVALID, False
    if value == "revisar":
        return None, True
    raise ValueError(f"Unknown Deducible value on ground-truth row {line}: {raw!r}")


def _decimal(value: str | None) -> Decimal | None:
    normalized = normalize_field("total", value)
    return normalized if isinstance(normalized, Decimal) else None


def _document_total(rows: list[dict[str, Any]]) -> tuple[str | None, str]:
    totals = [row["total"] for row in rows]
    if len(rows) == 1:
        return totals[0], "printed"
    if all(total == totals[0] for total in totals):
        return totals[0], "repeated"
    if any(total is None for total in totals):
        return None, "ambiguous"
    for row in rows:
        components = (row["base_imponible"], row["cuota_iva"], row["cuota_re"])
        values = [_decimal(value) for value in components if value is not None]
        if not values or sum(values) != _decimal(row["total"]):
            return None, "ambiguous"
    total = sum(_decimal(value) for value in totals if value is not None)
    return format(total, "f"), "summed_tax_subtotals"


def load_ground_truths_v2(path: Path) -> list[GroundTruthDocument]:
    """Load the untouched CSV and group its repeated fiscal rows by document."""
    grouped: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = [header.strip() for header in (reader.fieldnames or [])]
        if len(headers) != len(CSV_COLUMNS) or set(headers) != CSV_COLUMNS:
            raise ValueError(f"Unexpected v2 ground-truth columns: {headers}")
        reader.fieldnames = headers
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed v2 ground-truth row {line_number}")
            filename = row["Nombre foto"].strip()
            if not filename:
                raise ValueError(f"Missing image on ground-truth row {line_number}")
            validity, review_required = _ground_truth_status(
                row["Deducible"], line_number
            )
            document_values = {
                name: _reference_value(name, row[label], line_number)
                for name, label in DOCUMENT_FIELDS[:-1]
            }
            fiscal = {
                "base_imponible": _reference_value(
                    "base_imponible", row["Base Imponible"], line_number
                ),
                "tipo_iva": _reference_value(
                    "tipo_iva", row["Tipo IVA %"], line_number
                ),
                "cuota_iva": _reference_value(
                    "cuota_iva", row["Cuota IVA"], line_number
                ),
                "tipo_re": _reference_value("tipo_re", row["RE"], line_number),
                "cuota_re": _reference_value(
                    "cuota_re", row["Cuota RE"], line_number
                ),
                "tipo_irpf": _reference_value(
                    "tipo_irpf", row["Tipo IRPF"], line_number
                ),
                "cuota_irpf": _reference_value(
                    "cuota_irpf", row["Cuota IRPF"], line_number
                ),
                "total": _reference_value("total", row["Total"], line_number),
            }
            identity = filename.casefold()
            stable = {
                "validity": validity,
                "review_required": review_required,
                "diagnostic_type": row["Tipo"].strip() or None,
                **document_values,
            }
            if identity not in grouped:
                grouped[identity] = {
                    "filename": filename,
                    **stable,
                    "notes": row["Notas"].strip() or None,
                    "rows": [fiscal],
                }
                continue
            current = grouped[identity]
            conflicts = [name for name, value in stable.items() if current[name] != value]
            if conflicts:
                raise ValueError(
                    f"Conflicting document values for {filename!r} on row {line_number}: "
                    + ", ".join(conflicts)
                )
            notes = row["Notas"].strip() or None
            if notes and current["notes"] and notes != current["notes"]:
                raise ValueError(f"Conflicting notes for {filename!r} on row {line_number}")
            current["notes"] = current["notes"] or notes
            current["rows"].append(fiscal)

    documents = []
    for values in grouped.values():
        rows = values.pop("rows")
        total, total_derivation = _document_total(rows)
        iva_lines = tuple(
            GroundTruthIvaLine(
                row["base_imponible"], row["tipo_iva"], row["cuota_iva"]
            )
            for row in rows
            if any(row[name] is not None for name, _ in VAT_FIELDS)
        )
        surcharges = tuple(
            GroundTruthSurcharge(
                row["base_imponible"], row["tipo_re"], row["cuota_re"]
            )
            for row in rows
            if row["tipo_re"] is not None or row["cuota_re"] is not None
        )
        withholding_rows = [
            row
            for row in rows
            if row["tipo_irpf"] is not None or row["cuota_irpf"] is not None
        ]
        if len(withholding_rows) > 1:
            raise ValueError(f"Multiple IRPF rows for {values['filename']!r}")
        withholding = None
        if withholding_rows:
            row = withholding_rows[0]
            withholding = GroundTruthWithholding(
                row["base_imponible"], row["tipo_irpf"], row["cuota_irpf"]
            )
        documents.append(
            GroundTruthDocument(
                **values,
                lineas_iva=iva_lines,
                recargos_equivalencia=surcharges,
                retencion_irpf=withholding,
                total=total,
                raw_row_totals=tuple(row["total"] for row in rows),
                total_derivation=total_derivation,
            )
        )
    if not documents:
        raise ValueError("Ground-truth v2 CSV contains no documents")
    return documents


def filter_scope(
    documents: Iterable[GroundTruthDocument], scope: OcrScope | str
) -> list[GroundTruthDocument]:
    selected = OcrScope(scope)
    return [document for document in documents if selected.includes(document)]


def _compare(name: str, expected: str, obtained: str | None) -> bool:
    try:
        if name == "nombre_proveedor":
            return supplier_names_match(expected, obtained)
        return normalize_field(name, obtained) == normalize_field(name, expected)
    except (TypeError, ValueError):
        return False


def _line_match_count(
    expected: Any, obtained: Any, field_definitions: Sequence[tuple[str, str]]
) -> int:
    return sum(
        _compare(name, value, getattr(obtained, name, None))
        for name, _ in field_definitions
        if (value := getattr(expected, name)) is not None
    )


def _best_line_assignment(
    expected: Sequence[Any],
    obtained: Sequence[Any],
    field_definitions: Sequence[tuple[str, str]],
) -> tuple[int | None, ...]:
    @cache
    def visit(index: int, used: tuple[int, ...]) -> tuple[int, tuple[int | None, ...]]:
        if index == len(expected):
            return 0, ()
        choices = []
        tail_score, tail = visit(index + 1, used)
        choices.append((tail_score, (None,) + tail))
        for actual_index, actual_line in enumerate(obtained):
            if actual_index in used:
                continue
            tail_score, tail = visit(index + 1, tuple(sorted((*used, actual_index))))
            choices.append(
                (
                    _line_match_count(expected[index], actual_line, field_definitions)
                    + tail_score,
                    (actual_index,) + tail,
                )
            )
        return max(
            choices,
            key=lambda choice: (
                choice[0],
                sum(item is not None for item in choice[1]),
            ),
        )

    return visit(0, ())[1]


def _score_value(
    results: list[FieldResult],
    key: str,
    name: str,
    expected: str | None,
    obtained: str | None,
    *,
    metric_name: str | None = None,
) -> None:
    if expected is None:
        return
    results.append(
        FieldResult(
            key,
            metric_name or name,
            expected,
            obtained,
            _compare(name, expected, obtained),
        )
    )


def _score_lines(
    results: list[FieldResult],
    key: str,
    expected: Sequence[Any],
    obtained: Sequence[Any],
    field_definitions: Sequence[tuple[str, str]],
) -> None:
    assignment = _best_line_assignment(expected, obtained, field_definitions)
    for line_index, (expected_line, actual_index) in enumerate(zip(expected, assignment)):
        actual_line = obtained[actual_index] if actual_index is not None else None
        for name, _ in field_definitions:
            _score_value(
                results,
                f"{key}[{line_index}].{name}",
                name,
                getattr(expected_line, name),
                getattr(actual_line, name, None),
                metric_name=f"{key}.{name}",
            )


def score_document(actual: Invoice | None, expected: GroundTruthDocument) -> DocumentScore:
    if actual is not None and not isinstance(actual, Invoice):
        raise TypeError(f"Expected Invoice or None, got {type(actual).__name__}")
    results: list[FieldResult] = []
    for name, _ in DOCUMENT_FIELDS:
        _score_value(
            results,
            name,
            name,
            getattr(expected, name),
            getattr(actual, name, None),
        )
    _score_lines(
        results,
        "lineas_iva",
        expected.lineas_iva,
        actual.lineas_iva if actual else (),
        VAT_FIELDS,
    )
    _score_lines(
        results,
        "recargos_equivalencia",
        expected.recargos_equivalencia,
        actual.recargos_equivalencia if actual else (),
        RE_FIELDS,
    )
    if expected.retencion_irpf:
        for name, _ in IRPF_FIELDS:
            _score_value(
                results,
                f"retencion_irpf.{name}",
                name,
                getattr(expected.retencion_irpf, name),
                getattr(actual.retencion_irpf, name, None)
                if actual and actual.retencion_irpf
                else None,
                metric_name=f"retencion_irpf.{name}",
            )
    possible_fields = (
        len(DOCUMENT_FIELDS)
        + len(VAT_FIELDS) * len(expected.lineas_iva)
        + len(RE_FIELDS) * len(expected.recargos_equivalencia)
        + (len(IRPF_FIELDS) if expected.retencion_irpf else 0)
    )
    return DocumentScore(
        filename=expected.filename,
        classification=ClassificationScore(
            expected.validity, actual.validity if actual else None
        ),
        extraction=ExtractionScore(tuple(results), possible_fields),
        expected_diagnostic_type=expected.diagnostic_type,
        obtained_diagnostic_type=actual.diagnostic_type if actual else None,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize_scores(scores: Iterable[DocumentScore]) -> dict[str, Any]:
    scores = list(scores)
    classified = [score.classification for score in scores if score.classification.scored]
    correct = sum(item.matched is True for item in classified)
    confusion = {
        expected.value: {obtained: 0 for obtained in ("valid", "invalid", "missing")}
        for expected in InvoiceValidity
    }
    for classification in classified:
        obtained = classification.obtained.value if classification.obtained else "missing"
        confusion[classification.expected.value][obtained] += 1

    by_status = {}
    for status in InvoiceValidity:
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
            f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        by_status[status.value] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "expected": expected_total,
            "predicted": predicted_total,
        }

    all_fields = [field for score in scores for field in score.extraction.fields]
    by_field = {}
    for name, label in SUMMARY_FIELDS:
        values = [field for field in all_fields if field.field == name]
        matched = sum(field.matched for field in values)
        by_field[name] = {
            "label": label,
            "matched": matched,
            "total": len(values),
            "accuracy": _ratio(matched, len(values)),
        }
    possible = sum(score.extraction.possible_fields for score in scores)
    recalls = [metrics["recall"] for metrics in by_status.values()]
    f1_values = [metrics["f1"] for metrics in by_status.values()]
    diagnostic_types = {score.expected_diagnostic_type or "untyped" for score in scores}
    by_diagnostic_type = {}
    for diagnostic_type in sorted(diagnostic_types):
        group = [
            score
            for score in scores
            if (score.expected_diagnostic_type or "untyped") == diagnostic_type
        ]
        group_classifications = [item.classification for item in group if item.classification.scored]
        group_fields = [field for item in group for field in item.extraction.fields]
        by_diagnostic_type[diagnostic_type] = {
            "documents": len(group),
            "classification_matched": sum(item.matched is True for item in group_classifications),
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
            "by_status": by_status,
            "balanced_accuracy": _mean_defined(recalls),
            "macro_f1": _mean_defined(f1_values),
            "false_valid": confusion["invalid"]["valid"],
            "false_invalid": confusion["valid"]["invalid"],
        },
        "extraction": {
            "matched": sum(field.matched for field in all_fields),
            "total": len(all_fields),
            "accuracy": _ratio(sum(field.matched for field in all_fields), len(all_fields)),
            "possible_fields": possible,
            "ignored_unannotated": possible - len(all_fields),
            "coverage": _ratio(len(all_fields), possible),
            "by_field": by_field,
        },
        "by_diagnostic_type": by_diagnostic_type,
    }


def _mean_defined(values: Iterable[float | None]) -> float | None:
    defined = [value for value in values if value is not None]
    return sum(defined) / len(defined) if defined else None


def document_score_dict(score: DocumentScore) -> dict[str, Any]:
    result = asdict(score)
    for side in ("expected", "obtained"):
        status = result["classification"][side]
        result["classification"][side] = status.value if status else None
    result["extraction"].update(
        matched=score.extraction.matched,
        total=score.extraction.total,
        accuracy=score.extraction.accuracy,
        coverage=score.extraction.coverage,
        mismatches=score.extraction.mismatches,
    )
    result["classification"].update(
        scored=score.classification.scored,
        matched=score.classification.matched,
    )
    return result
