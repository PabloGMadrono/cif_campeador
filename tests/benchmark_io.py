"""Small, shared file writers for OCR benchmark artifacts."""

import csv
import io
import tempfile
from pathlib import Path
from time import sleep

REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def atomic_write(path: Path, content: str) -> None:
    """Replace a text file atomically, retrying transient Windows locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=".report-",
        suffix=".tmp",
        delete=False,
    ) as target:
        temporary = Path(target.name)
        target.write(content)
    try:
        for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == len(REPLACE_RETRY_DELAYS):
                    raise
                sleep(REPLACE_RETRY_DELAYS[attempt])
    finally:
        temporary.unlink(missing_ok=True)


def csv_text(headers, rows) -> str:
    """Create an Excel-friendly UTF-8 CSV without executable formula cells."""

    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    buffer = io.StringIO(newline="")
    buffer.write("\ufeff")
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows({key: safe(row.get(key)) for key in headers} for row in rows)
    return buffer.getvalue()
