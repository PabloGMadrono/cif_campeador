"""Durable, local reports for the implementation-independent invoice benchmark."""

import csv
import io
import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from uuid import uuid4

from filelock import FileLock

from tests.invoice_accuracy import (
    INVOICE_FIELDS, PASS_THRESHOLD, category_order, image_category, image_index,
    load_ground_truths, resolve_image,
)


DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "results"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "invoice_dashboard.html"
FIELD_LABELS = {item.name: item.metadata["csv"] for item in INVOICE_FIELDS}
FINISHED = {"passed", "failed", "error"}
LOGGER = logging.getLogger(__name__)
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, content):
    """Readers see either the previous complete file or the new complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                     prefix=".report-", suffix=".tmp", delete=False) as target:
        temporary = Path(target.name)
        target.write(content)
    try:
        # On Windows, browser reads, antivirus scans and Excel can temporarily
        # prevent replacement even though the directory is writable.
        for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == len(REPLACE_RETRY_DELAYS):
                    raise
                sleep(REPLACE_RETRY_DELAYS[attempt])
    finally:
        temporary.unlink(missing_ok=True)


def write_export(path, content):
    """A locked derived report must not abort OCR or discard saved run data."""
    try:
        atomic_write(path, content)
        return True
    except OSError as error:
        LOGGER.warning(
            "Report refresh deferred for %s: %s. Run data is saved in run.json; "
            "continuing the benchmark. Refresh will be retried on the next save.",
            path, error,
        )
        return False


def csv_text(headers, rows):
    # Prevent document text from becoming executable formulas in a spreadsheet.
    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    buffer = io.StringIO(newline="")
    buffer.write("\ufeff")  # Excel recognises UTF-8 Spanish text.
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows({key: safe(row.get(key)) for key in headers} for row in rows)
    return buffer.getvalue()


def snapshot_rows(rows, image_directory, report_directory, relative_directory):
    """Keep reference values and portable JPEG previews with this execution."""
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
    index_by_name = image_index(image_directory)
    records = []
    for index, (filename, expected) in enumerate(rows, start=1):
        source_path = resolve_image(image_directory, filename, index_by_name)
        preview = relative_directory / "images" / f"{index:03d}.jpg"
        preview_error = None
        try:
            destination = report_directory / preview
            destination.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source_path) as original:
                with ImageOps.exif_transpose(original) as oriented:
                    with oriented.convert("RGB") as image:
                        image.thumbnail((1800, 1800))
                        image.save(destination, "JPEG", quality=88)
            image_url = preview.as_posix()
        except Exception as error:
            image_url = None
            preview_error = f"Preview unavailable: {type(error).__name__}: {error}"
        records.append({
            "filename": filename, "image": image_url, "preview_error": preview_error,
            "source_path": source_path.relative_to(image_directory).as_posix(),
            "category": image_category(image_directory, source_path), "category_source": "execution",
            "expected": asdict(expected), "obtained": None, "status": "pending",
            "matched": None, "total": len(INVOICE_FIELDS), "accuracy_pct": None,
            "mismatches": [], "error": None, "started_at": None,
            "finished_at": None, "duration_seconds": None,
        })
    return records


def summarize(run):
    scored = [record for record in run["invoices"] if record["status"] in FINISHED]
    correct = sum(record["matched"] for record in scored)
    total = sum(record["total"] for record in scored)
    accuracy = 100 * correct / total if total else None
    complete = run["status"] == "completed" and len(scored) == len(run["invoices"])
    threshold = run["threshold_pct"] / 100
    passed = complete and bool(total) and correct / total > threshold and all(
        record["matched"] / record["total"] > threshold for record in scored
    )
    return {
        "run_id": run["run_id"], "started_at": run["started_at"],
        "finished_at": run["finished_at"], "updated_at": run["updated_at"],
        "extractor": run["extractor"], "status": run["status"],
        "outcome": ("passed" if passed else "failed") if complete else "incomplete",
        "images_total": len(run["invoices"]), "images_scored": len(scored),
        "images_passed": sum(record["status"] == "passed" for record in scored),
        "images_failed": sum(record["status"] == "failed" for record in scored),
        "images_errored": sum(record["status"] == "error" for record in scored),
        "correct_fields": correct, "scored_fields": total, "accuracy_pct": accuracy,
        "threshold_pct": run["threshold_pct"],
    }


def summarize_categories(run):
    categories = sorted({r.get("category", "uncategorized") for r in run["invoices"]}, key=category_order)
    results = []
    for category in categories:
        subset = {**run, "invoices": [r for r in run["invoices"] if r.get("category", "uncategorized") == category]}
        results.append({"category": category, **summarize(subset)})
    return results


def rebuild_dashboard(directory):
    """Called under the report lock; JSON snapshots are the source of truth."""
    runs = [json.loads(path.read_text(encoding="utf-8"))
            for path in (directory / "runs").glob("*/run.json")]
    runs.sort(key=lambda run: run["started_at"], reverse=True)
    preview_path = directory / "preview" / "run.json"
    preview = json.loads(preview_path.read_text(encoding="utf-8")) if preview_path.exists() else None
    summaries = [summarize(run) for run in runs]
    history_saved = True
    if summaries:
        history_saved = write_export(directory / "executions.csv", csv_text(list(summaries[0]), summaries))
        categories = [row for run in runs for row in summarize_categories(run)]
        history_saved = write_export(directory / "categories.csv", csv_text(list(categories[0]), categories)) and history_saved
    payload = {"fields": FIELD_LABELS, "runs": runs, "preview": preview,
               "summaries": summaries, "generated_at": now(), "threshold_pct": PASS_THRESHOLD * 100}
    # JSON is data even when an invoice contains HTML or a closing script tag.
    encoded = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
    template = TEMPLATE.read_text(encoding="utf-8")
    dashboard_saved = write_export(directory / "dashboard.html", template.replace("__REPORT_DATA__", encoded))
    return history_saved and dashboard_saved


def write_run_exports(run_directory, data):
    """Regenerate optional CSVs from an authoritative execution snapshot."""
    per_image = []
    per_field = []
    for record in data["invoices"]:
        per_image.append({
            "run_id": data["run_id"], "category": record.get("category", "uncategorized"), **{key: record[key] for key in (
                "filename", "status", "matched", "total", "accuracy_pct",
                "duration_seconds", "started_at", "finished_at", "error",
            )},
        })
        for name, label in FIELD_LABELS.items():
            per_field.append({
                "run_id": data["run_id"], "filename": record["filename"],
                "category": record.get("category", "uncategorized"),
                "status": record["status"], "field": name, "label": label,
                "expected": record["expected"][name],
                "obtained": record["obtained"][name] if record["obtained"] is not None else None,
                "matches": (name not in record["mismatches"]) if record["status"] in FINISHED else None,
            })
    results_saved = write_export(run_directory / "results.csv", csv_text(list(per_image[0]), per_image))
    fields_saved = write_export(run_directory / "fields.csv", csv_text(list(per_field[0]), per_field))
    categories = summarize_categories(data)
    categories_saved = write_export(run_directory / "categories.csv", csv_text(list(categories[0]), categories))
    return results_saved and fields_saved and categories_saved


def refresh_reports(directory, image_directory=None):
    """Rebuild exports; assign legacy runs categories from today's image layout."""
    directory = Path(directory)
    image_directory = Path(image_directory or os.environ.get(
        "OCR_IMAGE_DIR", Path(__file__).resolve().parent / "images" / "trial_invoices"
    ))
    index = image_index(image_directory)
    directory.mkdir(parents=True, exist_ok=True)
    saved = True
    with FileLock(str(directory / ".reports.lock"), timeout=30):
        for path in (directory / "runs").glob("*/run.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for record in data["invoices"]:
                if "category" not in record and record["filename"].casefold() in index:
                    source = resolve_image(image_directory, record["filename"], index)
                    record.update(category=image_category(image_directory, source),
                                  category_source="current_layout")
                    changed = True
            if changed:
                atomic_write(path, json.dumps(data, ensure_ascii=True, indent=2))
            saved = write_run_exports(path.parent, data) and saved
        saved = rebuild_dashboard(directory) and saved
    return saved


class ExecutionReport:
    def __init__(self, rows, image_directory, directory, extractor):
        self.directory = Path(directory)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
        self.relative = Path("runs") / self.run_id
        self.run_directory = self.directory / self.relative
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.directory / ".reports.lock"), timeout=30)
        self.data = {
            "schema_version": 1, "run_id": self.run_id, "extractor": extractor,
            "status": "running", "started_at": now(), "finished_at": None,
            "updated_at": now(), "threshold_pct": PASS_THRESHOLD * 100,
            "invoices": snapshot_rows(rows, Path(image_directory), self.directory, self.relative),
        }
        self.records = {record["filename"]: record for record in self.data["invoices"]}
        self.save()

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        complete = error_type is None and all(
            record["status"] in FINISHED for record in self.data["invoices"]
        )
        self.data["status"] = "completed" if complete else "interrupted"
        self.data["finished_at"] = now()
        for record in self.data["invoices"]:
            if record["status"] == "running":
                record["status"] = "interrupted"
                record["finished_at"] = now()
                record["duration_seconds"] = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(record["started_at"])
                ).total_seconds()
        self.save()

    def start(self, filename):
        record = self.records[filename]
        record["status"] = "running"
        record["started_at"] = now()
        self.save()

    def record(self, filename, actual, score, error, duration, *, evidence=None, prepared=None):
        record = self.records[filename]
        if prepared is not None:
            record["preprocessing"] = self.save_preparation(filename, prepared)
        record.update({
            "obtained": asdict(actual) if score is not None else None,
            "matched": score.matched if score is not None else 0,
            "mismatches": list(score.mismatches) if score is not None else list(FIELD_LABELS),
            "error": error, "finished_at": now(), "duration_seconds": duration,
            "extraction_evidence": evidence,
        })
        record["accuracy_pct"] = 100 * record["matched"] / record["total"]
        record["status"] = "error" if error else (
            "passed" if record["matched"] / record["total"] > PASS_THRESHOLD else "failed"
        )
        self.save()

    def save_preparation(self, filename, document):
        """Portable previews of every page, plus full geometry and diagnostics."""
        from PIL import Image

        metadata = document.metadata()
        metadata["pages"] = [dict(page) for page in metadata["pages"]]
        index = list(self.records).index(filename) + 1
        for page, page_data in zip(document.pages, metadata["pages"]):
            for kind, source in (("original", page.original_path), ("prepared", page.image_path)):
                relative = self.relative / "images" / f"{index:03d}-p{page_data['page_number']:03d}-{kind}.jpg"
                destination = self.directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(source) as image:
                    image.thumbnail((1800, 1800))
                    image.save(destination, "JPEG", quality=92)
                page_data[kind + "_preview"] = relative.as_posix()
        return metadata

    def save(self):
        self.data["updated_at"] = now()
        with self.lock:
            atomic_write(self.run_directory / "run.json", json.dumps(self.data, ensure_ascii=True, indent=2))
            write_run_exports(self.run_directory, self.data)
            rebuild_dashboard(self.directory)


def prepare_dashboard(rows, image_directory, directory=DEFAULT_REPORTS_DIR):
    """Show the dataset before its first execution; never invent OCR results."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    preview = {
        "schema_version": 1, "run_id": "preview", "extractor": "No execution yet",
        "status": "ready", "started_at": now(), "finished_at": None,
        "updated_at": now(), "threshold_pct": PASS_THRESHOLD * 100,
        "invoices": snapshot_rows(rows, Path(image_directory), directory, Path("preview")),
    }
    with FileLock(str(directory / ".reports.lock"), timeout=30):
        atomic_write(directory / "preview" / "run.json", json.dumps(preview, ensure_ascii=True))
        rebuild_dashboard(directory)
    return directory / "dashboard.html"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prepare or refresh the local OCR dashboard.")
    parser.add_argument("--refresh", action="store_true", help="Rebuild exports from saved runs without OCR")
    args = parser.parse_args()
    directory = Path(os.environ.get("OCR_REPORT_DIR", DEFAULT_REPORTS_DIR))
    if args.refresh:
        saved = refresh_reports(directory)
        print(f"Reports {'refreshed' if saved else 'partially refreshed (some files remain locked)'}: {directory.resolve()}")
        raise SystemExit(0 if saved else 1)
    tests = Path(__file__).resolve().parent
    rows = load_ground_truths(tests / "ground_truths" / "ground_truth_trial_invoices.csv")
    path = prepare_dashboard(rows, Path(os.environ.get("OCR_IMAGE_DIR", tests / "images" / "trial_invoices")),
                             directory)
    print(f"Dashboard ready: {path.resolve()}")
