"""Level-two black-box OCR benchmark: real documents in, scored results out."""

from __future__ import annotations

import os
from pathlib import Path
from time import monotonic

from src.ocr import invoice_extractor
from tests.invoice_accuracy import PASS_THRESHOLD, image_index, resolve_image
from tests.invoice_accuracy_v2 import (
    OcrScope,
    filter_scope,
    load_ground_truths_v2,
    score_document,
    summarize_scores,
)
from tests.invoice_report_v2 import LevelTwoReport

TESTS = Path(__file__).resolve().parent
GROUND_TRUTH = TESTS / "ground_truths" / "ocr_ground_truth_v2.csv"
DEFAULT_IMAGE_DIRECTORY = TESTS / "images"
DEFAULT_REPORT_DIRECTORY = TESTS / "results" / "v2"


def _resolve_v2_image(directory: Path, filename: str) -> Path:
    """Resolve exact basenames first; extensionless annotations may use a stem."""
    index = image_index(directory)
    exact = index.get(filename.casefold(), [])
    if exact:
        return resolve_image(directory, filename, index)
    stem_matches = [
        path
        for paths in index.values()
        for path in paths
        if path.stem.casefold() == filename.casefold()
    ]
    if len(stem_matches) > 1:
        raise ValueError(
            f"Ambiguous invoice image {filename}: "
            + ", ".join(str(path) for path in stem_matches)
        )
    return stem_matches[0] if stem_matches else directory / filename


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def test_invoice_accuracy_v2(ocr_scope: OcrScope):
    documents = filter_scope(load_ground_truths_v2(GROUND_TRUTH), ocr_scope)
    assert documents, f"No documents selected by --ocr-scope {ocr_scope.value}"

    image_directory = Path(os.environ.get("OCR_IMAGE_DIR", DEFAULT_IMAGE_DIRECTORY))
    report_directory = Path(
        os.environ.get("OCR_V2_REPORT_DIR", DEFAULT_REPORT_DIRECTORY)
    )
    extractor_name = (
        f"{type(invoice_extractor).__module__}.{type(invoice_extractor).__qualname__}"
    )
    report = LevelTwoReport(
        report_directory, ocr_scope.value, extractor_name, documents
    )
    scores = []
    errors = []
    completed = False

    print(
        f"\nOCR level 2 · scope={ocr_scope.value} · {len(documents)} documents\n"
        f"Report: {report.directory.resolve()}",
        flush=True,
    )
    try:
        for position, expected in enumerate(documents, start=1):
            started = monotonic()
            source_path = _resolve_v2_image(image_directory, expected.filename)
            actual = None
            error_text = None
            print(f"[{position}/{len(documents)}] {expected.filename}", flush=True)
            try:
                if not source_path.is_file():
                    raise FileNotFoundError(f"Invoice image not found: {source_path}")
                extract_with_evidence = getattr(
                    invoice_extractor, "extract_invoice_with_evidence", None
                )
                actual = (
                    extract_with_evidence(str(source_path))
                    if callable(extract_with_evidence)
                    else invoice_extractor.extract_invoice(str(source_path))
                )
            except Exception as error:  # noqa: BLE001 - every backend failure belongs in the report
                error_text = f"{type(error).__name__}: {error}"
                errors.append(f"{expected.filename}: {error_text}")
            score = score_document(actual, expected)
            scores.append(score)
            report.record(
                expected,
                score,
                source_path=source_path,
                duration_seconds=monotonic() - started,
                error=error_text,
            )
            classification = score.classification
            classification_text = (
                "not scored"
                if not classification.scored
                else "correct"
                if classification.matched
                else (
                    f"wrong ({classification.expected.value} != "
                    f"{classification.obtained.value if classification.obtained else 'missing'})"
                )
            )
            print(
                f"  classification: {classification_text}; "
                f"extraction: {score.extraction.matched}/{score.extraction.total} "
                f"({_percent(score.extraction.accuracy)})"
                + (f"; {error_text}" if error_text else ""),
                flush=True,
            )
        completed = True
    finally:
        report.finish("completed" if completed else "interrupted")

    summary = summarize_scores(scores)
    classification = summary["classification"]
    extraction = summary["extraction"]
    print(
        "\nClassification: "
        f"{classification['matched']}/{classification['total']} "
        f"({_percent(classification['accuracy'])}); "
        f"review excluded: {summary['review_documents']}\n"
        "Extraction: "
        f"{extraction['matched']}/{extraction['total']} "
        f"({_percent(extraction['accuracy'])})",
        flush=True,
    )
    for name, field in extraction["by_field"].items():
        print(
            f"  {name}: {field['matched']}/{field['total']} "
            f"({_percent(field['accuracy'])})",
            flush=True,
        )

    assert not errors, "OCR execution errors:\n" + "\n".join(errors)
    if classification["total"]:
        assert classification["accuracy"] > PASS_THRESHOLD, (
            f"Classification accuracy {_percent(classification['accuracy'])} must be "
            f"greater than {PASS_THRESHOLD:.0%}"
        )
    assert extraction["total"], "The selected scope has no annotated fields"
    assert extraction["accuracy"] > PASS_THRESHOLD, (
        f"Extraction accuracy {_percent(extraction['accuracy'])} must be "
        f"greater than {PASS_THRESHOLD:.0%}"
    )
