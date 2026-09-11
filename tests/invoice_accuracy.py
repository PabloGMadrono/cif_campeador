"""Ground-truth loading and field scoring, independent of any OCR engine."""

import csv
import re
import unicodedata
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.ocr.models import Invoice


INVOICE_FIELDS = tuple(fields(Invoice))
PASS_THRESHOLD = 0.70
NUMERIC_FIELDS = {"base_imponible", "tipo_iva", "cuota_iva", "total"}


def load_ground_truths(path: Path) -> list[tuple[str, Invoice]]:
    """Read all rows, rejecting incomplete schemas and duplicate image names."""
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = [header.strip() for header in (reader.fieldnames or [])]
        expected = {"Nombre foto", "Notas"} | {
            item.metadata["csv"] for item in INVOICE_FIELDS
        }
        if len(headers) != len(expected) or set(headers) != expected:
            raise ValueError(f"Unexpected ground-truth columns: {headers}")
        reader.fieldnames = headers
        rows = []
        seen = set()
        for line, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed ground-truth row {line}")
            filename = row["Nombre foto"].strip()
            if not filename or filename in seen:
                raise ValueError(f"Missing or duplicate image on row {line}: {filename}")
            seen.add(filename)
            invoice = Invoice(**{
                item.name: row[item.metadata["csv"]].strip()
                for item in INVOICE_FIELDS
            })
            # Invalid reference values must never silently lower the benchmark.
            for item in INVOICE_FIELDS:
                normalize(item.name, getattr(invoice, item.name))
            rows.append((filename, invoice))
    if not rows:
        raise ValueError("Ground-truth CSV contains no invoices")
    return rows


def normalize(name: str, value: str | None):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if not value:
        return ""
    if name == "fecha":
        for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                pass
        raise ValueError(f"Invalid invoice date: {value!r}")
    if name in NUMERIC_FIELDS:
        symbol = "%" if name == "tipo_iva" else "€"
        value = value.removesuffix(symbol).strip()
        # Spanish decimal commas, optional thousands dots; or decimal dots.
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+),\d+", value):
            value = value.replace(".", "").replace(",", ".")
        elif not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
            raise ValueError(f"Invalid numeric field {name}: {value!r}")
        return Decimal(value)
    # Keep punctuation, accents and leading zeroes meaningful.
    return value.casefold()


@dataclass(frozen=True)
class Score:
    matched: int
    total: int
    mismatches: tuple[str, ...]

    @property
    def accuracy(self) -> float:
        return self.matched / self.total


def score_invoice(actual: Invoice, expected: Invoice) -> Score:
    if not isinstance(actual, Invoice):
        raise TypeError(f"extract_invoice must return Invoice, got {type(actual).__name__}")
    mismatches = []
    for item in INVOICE_FIELDS:
        reference = normalize(item.name, getattr(expected, item.name))
        try:
            equal = normalize(item.name, getattr(actual, item.name)) == reference
        except (ValueError, TypeError):
            equal = False
        if not equal:
            mismatches.append(item.name)
    return Score(len(INVOICE_FIELDS) - len(mismatches), len(INVOICE_FIELDS), tuple(mismatches))
