"""Durable JSON and CSV exports for the level-two OCR benchmark."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.benchmark_io import atomic_write, csv_text
from tests.invoice_accuracy_v2 import (
    DocumentScore,
    GroundTruthDocument,
    document_score_dict,
    summarize_scores,
)

TEMPLATE = Path(__file__).resolve().parent / "templates" / "invoice_dashboard_v2.html"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class LevelTwoReport:
    """Save after every image so a long or interrupted OCR run remains useful."""

    def __init__(
        self,
        directory: Path,
        scope: str,
        extractor: str,
        documents: list[GroundTruthDocument],
    ):
        self.root = Path(directory)
        self.run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid4().hex[:10]
        )
        self.directory = self.root / "runs" / self.run_id
        self.data = {
            "schema_version": 2,
            "run_id": self.run_id,
            "scope": scope,
            "extractor": extractor,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "documents_total": len(documents),
            "records": [],
            "summary": None,
        }
        self._save()

    def record(
        self,
        document: GroundTruthDocument,
        score: DocumentScore,
        *,
        source_path: Path,
        duration_seconds: float,
        error: str | None,
    ) -> None:
        self.data["records"].append(
            {
                "filename": document.filename,
                "source_path": str(source_path),
                "expected": _json_value(document),
                "score": document_score_dict(score),
                "duration_seconds": duration_seconds,
                "error": error,
            }
        )
        self.data["summary"] = summarize_scores(
            _score_from_record(record) for record in self.data["records"]
        )
        self._save()

    def finish(self, status: str = "completed") -> None:
        if status not in {"completed", "interrupted"}:
            raise ValueError(f"Invalid report status: {status}")
        self.data["status"] = status
        self.data["finished_at"] = _now()
        self._save()

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.directory / "run.json",
            json.dumps(self.data, ensure_ascii=False, indent=2),
        )
        encoded = json.dumps(self.data, ensure_ascii=True).replace("<", "\\u003c")
        atomic_write(
            self.directory / "dashboard.html",
            TEMPLATE.read_text(encoding="utf-8").replace("__REPORT_DATA__", encoded),
        )
        if not self.data["records"]:
            return
        result_rows = []
        field_rows = []
        for record in self.data["records"]:
            score = record["score"]
            classification = score["classification"]
            extraction = score["extraction"]
            result_rows.append(
                {
                    "run_id": self.run_id,
                    "scope": self.data["scope"],
                    "filename": record["filename"],
                    "expected_status": classification["expected"],
                    "obtained_status": classification["obtained"],
                    "classification_scored": classification["scored"],
                    "classification_matches": classification["matched"],
                    "expected_diagnostic_type": score["expected_diagnostic_type"],
                    "obtained_diagnostic_type": score["obtained_diagnostic_type"],
                    "matched_fields": extraction["matched"],
                    "scored_fields": extraction["total"],
                    "extraction_accuracy": extraction["accuracy"],
                    "duration_seconds": record["duration_seconds"],
                    "error": record["error"],
                }
            )
            for field in extraction["fields"]:
                field_rows.append(
                    {
                        "run_id": self.run_id,
                        "filename": record["filename"],
                        "key": field["key"],
                        "field": field["field"],
                        "expected": field["expected"],
                        "obtained": field["obtained"],
                        "matches": field["matched"],
                    }
                )
        atomic_write(
            self.directory / "results.csv",
            csv_text(list(result_rows[0]), result_rows),
        )
        if field_rows:
            atomic_write(
                self.directory / "fields.csv",
                csv_text(list(field_rows[0]), field_rows),
            )


def _score_from_record(record: dict[str, Any]) -> DocumentScore:
    """Rehydrate only the immutable scoring data needed for aggregation."""
    from src.ocr.models import InvoiceValidity
    from tests.invoice_accuracy_v2 import (
        ClassificationScore,
        ExtractionScore,
        FieldResult,
    )

    raw = record["score"]
    classification = raw["classification"]
    return DocumentScore(
        filename=raw["filename"],
        classification=ClassificationScore(
            InvoiceValidity(classification["expected"])
            if classification["expected"]
            else None,
            InvoiceValidity(classification["obtained"])
            if classification["obtained"]
            else None,
        ),
        extraction=ExtractionScore(
            tuple(FieldResult(**field) for field in raw["extraction"]["fields"]),
            raw["extraction"]["possible_fields"],
        ),
        expected_diagnostic_type=raw["expected_diagnostic_type"],
        obtained_diagnostic_type=raw["obtained_diagnostic_type"],
    )
