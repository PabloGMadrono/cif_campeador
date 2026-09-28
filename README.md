# CIF Campeador

## Executive test summary

Run commands from the repository root with the virtual environment activated:

```powershell
.\.venv\Scripts\Activate.ps1
```

The application and black-box tests use the OCR backend selected as
`invoice_extractor` in `src/ocr/__init__.py`. Configure the credentials required
by that backend in `.env` before running a live OCR test. The current default is
OpenAI and requires `OPENAI_API_KEY`.

### Invoice accuracy v2 — primary OCR benchmark

The recommended business benchmark evaluates all documents with a definitive
`valid` or `invalid` ground truth and excludes manual-review cases:

```powershell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope valid-invalid -s
```

This is a live, black-box test. It processes the real invoice images, makes
billable OCR/API calls, evaluates document classification and field extraction,
and requires both accuracies to be greater than 70%. `-s` shows progress and
per-document results while the test runs.

Other available scopes are:

```powershell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope valid -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope invalid -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope review -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope all -s
```

To diagnose one document without processing the full dataset:

```powershell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope all --ocr-image "IMG_3446.HEIC" -s
```

The benchmark reads `tests/ground_truths/ocr_ground_truth_v2.csv`. Each run
writes its dashboard and machine-readable results to
`tests/results/v2/runs/<run-id>/`.

### OCR and database persistence

First verify the complete persistence round-trip without external OCR calls:

```powershell
python -m pytest tests/test_invoice_database_live.py::test_database_roundtrip_with_offline_ocr -v
```

Then run the opt-in live test:

```powershell
$env:RUN_LIVE_OCR_DB_TEST = "1"
python -m pytest tests/test_invoice_database_live.py::test_live_ocr_persists_validity_and_diagnosis -v -s
Remove-Item Env:RUN_LIVE_OCR_DB_TEST
```

The live test:

1. Runs the selected real OCR backend on one valid invoice and one proforma.
2. Persists the complete `Invoice`, including validity, diagnosis and fiscal
   lines.
3. Reads both invoices back through the repository.
4. Verifies the domain objects and underlying database rows.
5. Prints the persisted records as JSON.

Both database tests use an isolated in-memory SQLite database and never write to
the development or production database. The live variant still makes billable
OCR/API calls.

To test the wider download → OCR → database application pipeline with test
doubles and a temporary SQLite database:

```powershell
python -m pytest tests/test_invoice_pipeline.py -v
```

### Fast checks before pushing

These checks do not run the billable accuracy benchmark:

```powershell
python -m ruff check .
python -m pytest tests --ignore=tests/test_invoice_accuracy_v2.py -q
python -m alembic heads
```

For implementation details, dataset semantics, report fields and OCR backend
configuration, see [the OCR documentation](src/ocr/README.md).
