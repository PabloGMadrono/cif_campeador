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

### Invoice accounting accuracy v2 — OCR with corrections

This benchmark runs the same live OCR extraction and then applies
`reconcile_invoice()` before scoring. Use it to measure the final invoice values
that would be persisted by the application. Reconciliation fills missing fiscal
values but preserves every value read by OCR; inconsistent complete data receives
the `math_error` status for manual review.

#### Accounting statuses

| Stored value | Dashboard label | Meaning |
|---|---|---|
| `reconciled` | **Verified** | All required fiscal values were already present and every accounting check passed. Nothing was changed. |
| `corrected` | **Missing values filled** | One or more empty fiscal fields were calculated, and the completed values passed every accounting check. The tooltip lists each calculated value. |
| `math_error` | **Needs review** | The available fiscal values do not satisfy the VAT or total checks. Values read by OCR are preserved. Any empty fields calculated before detecting the mismatch remain listed in the tooltip. |
| `missing_data` | **Incomplete** | There is not enough information to populate and verify all required fiscal fields. |
| No value | **Not checked** | Accounting reconciliation did not run, as in the raw OCR benchmark, or OCR failed before producing an invoice. This is not a stored `FiscalStatus`. |

Only the first four values belong to `FiscalStatus` and can be persisted for a
completed invoice. **Not checked** describes the absence of an accounting result.

```powershell
python -m pytest tests/test_invoice_accounting_accuracy_v2.py -s
```

The default scope is `all`. The test reads
`tests/ground_truths/ocr_ground_truth_accounting_v2.csv` and skips with a clear
message until that file exists. The CSV uses the same columns as
`ocr_ground_truth_v2.csv`; populate fields that accounting can derive so the
corrected values are included in the score. Blank cells and `-` remain unscored.

The accounting benchmark supports the same selection options as the raw OCR
benchmark:

```powershell
# Recommended business scope: definitive valid and invalid documents
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-scope valid-invalid -s

# One scope only
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-scope valid -s
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-scope invalid -s
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-scope review -s
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-scope all -s

# One image, selected by filename or unique stem
python -m pytest tests/test_invoice_accounting_accuracy_v2.py --ocr-image "IMG_3446.HEIC" -s
```

Available options and environment settings:

| Option or variable | Default | Purpose |
|---|---|---|
| `--ocr-scope` / `OCR_TEST_SCOPE` | `all` | Select `valid`, `invalid`, `valid-invalid`, `review`, or `all`. |
| `--ocr-image` | all selected images | Run one filename or unique filename stem. |
| `OCR_IMAGE_DIR` | `tests/images` | Override the directory searched recursively for invoice images. |
| `OCR_ACCOUNTING_V2_REPORT_DIR` | `tests/results/accounting_v2` | Override the corrected benchmark report directory. |
| `-s` | output captured | Show per-document progress and metrics while the test runs. |

Reports are written after every document under
`tests/results/accounting_v2/runs/<run-id>/` and include `dashboard.html`,
`run.json`, `results.csv`, and `fields.csv`. The benchmark scores classification
and invoice fields after correction. The dashboard shows a clear accounting
status with an information tooltip containing calculated fields and relevant
fiscal details. This metadata is also saved in `run.json`; the correction list
is included in `results.csv`. Click any row in **Field accuracy** to show only
documents that missed that field; their expected and obtained values appear in
the collapsed document header. Fiscal metadata does not affect the accuracy score.

Like the raw benchmark, this is a live test that uses the configured OCR backend
and may make billable API calls.

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
python -m pytest tests --ignore=tests/test_invoice_accuracy_v2.py --ignore=tests/test_invoice_accounting_accuracy_v2.py -q
python -m alembic heads
```

For implementation details, dataset semantics, report fields and OCR backend
configuration, see [the OCR documentation](src/ocr/README.md).
