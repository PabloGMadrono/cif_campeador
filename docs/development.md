# Development

[Start here](../README.md) · [Architecture](architecture.md) · [Processing](invoice-processing.md) · [Data model](data-model.md)

## Local setup

Use 64-bit Python 3.12 and Docker Desktop. Commands below run from the repository
root in PowerShell. ngrok is needed only to receive Meta events on a local machine.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Configure WhatsApp credentials, the MinIO settings from `.env.example`, and the
selected OCR backend in `.env`. Existing
process environment values override `.env`; restart processes after changes.
SQL code lives in `src/sql_database`; the local SQLite file lives in
`local_database/cif_campeador.db`. MinIO code lives in `src/object_storage`, and
original attachments live in the MinIO Docker volume.

For an existing checkout using `data/cif_campeador.db`, stop FastAPI and both
workers before renaming `data` to `local_database`. Move the entire directory,
including any SQLite journal files, and update `DATABASE_URL` in `.env` to
`sqlite:///./local_database/cif_campeador.db` before restarting.

The example sets `OCR_PREPROCESSING=full`. Either change it to `off` for a baseline
or download the pinned local preprocessing models before extraction:

```powershell
python -m src.ocr.preprocessing --setup-models
```

Start all services with the script:

```powershell
.\scripts\start-development.ps1
```

It starts Redis and MinIO, creates the private bucket and application account,
applies Alembic migrations, and opens separate terminals for
FastAPI, both workers, and ngrok. `-SkipNgrok` omits the tunnel;
`-InstallDependencies` installs application requirements before starting.
Configure Meta's callback as the tunnel's HTTPS URL plus `/webhook`, using the
same verification token as `.env`.

For manual startup, start Redis and MinIO, provision storage and migrate SQL,
then run each Python process in its
own activated terminal:

```powershell
docker compose up -d --wait redis minio
docker compose run --rm minio-init
python -m alembic upgrade head
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
python -m src.workers.download
python -m src.workers.ocr
```

MinIO runs in Docker on your machine: S3 API `http://localhost:9000`, console
`http://localhost:9001`. Host Python processes use `MINIO_ENDPOINT=localhost:9000`
and `MINIO_SECURE=false`. Containerized workers on the same Compose network would
use `minio:9000`. Images are stored in the `minio-data` Docker volume mounted at
`/data`, rather than in the application's media directory. Restarting containers
preserves objects; `docker compose down -v` destroys the volume and its objects.

The first build compiles pinned official MinIO server and `mc` sources using the
[Dockerfile](../docker/minio.Dockerfile). The provisioning container exits after
setup; its root credentials are separate from the workers' application account.
Workers require an existing bucket and fail clearly if it is not provisioned.
The application account can read/write `originals/*` in its bucket and cannot
delete originals or administer MinIO. Use new credentials for deployment.

Production hosting is still undecided. Configure workers to use the chosen
server's endpoint and TLS; define backups for both SQL and MinIO data. A persistent
volume alone is not a backup. The console stays private. See
[where MinIO runs](minio-media-storage-spec.md#where-minio-runs).

The existing ngrok tunnel reaches FastAPI only. Remote frontend testing needs a
separate browser-reachable S3 endpoint; `localhost:9000` only works for a browser
on this development machine. The storage wrapper can sign expiring GET URLs,
but authenticated frontend delivery and CORS configuration remain future work.

## Configuration

[.env.example](../.env.example) is the editable template. Settings are read in
[config.py](../src/config.py), [connection.py](../src/sql_database/connection.py),
[media.py](../src/whatsapp/media.py), [redis_streams.py](../src/jobs/redis_streams.py),
and [workers/settings.py](../src/workers/settings.py). Object storage settings are
in [object_storage/settings.py](../src/object_storage/settings.py).

| Setting | Default / purpose |
| --- | --- |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN` | Subscription challenge token; Meta media retrieval credential. |
| `OPENAI_API_KEY` | Required by the selected OpenAI backend. Alternative backend requirements are in [processing](invoice-processing.md#preparation-and-extraction). |
| `DATABASE_URL` | `sqlite:///./local_database/cif_campeador.db`; relative SQLite paths resolve from the repository root. |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `WHATSAPP_MEDIA_DIR` | `tests/whatsapp_images`; legacy OCR reads and migration sources only. |
| `MINIO_ENDPOINT`, `MINIO_SECURE` | Required S3 host/port; TLS defaults to `true`. Local example uses `localhost:9000` and `false`. |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | Required application credentials, provisioned separately from root credentials. |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | Required for local Compose server/provisioning only. |
| `MINIO_BUCKET`, `MINIO_REGION` | `invoice-originals`, `us-east-1`. |
| `MINIO_CONNECT_TIMEOUT_SECONDS`, `MINIO_READ_TIMEOUT_SECONDS` | 5 / 30 seconds; worker retries handle failures. |
| `WHATSAPP_GRAPH_API_VERSION` | `v23.0` in current code; configurable Meta API version. |
| `WHATSAPP_MEDIA_ALLOWED_HOSTS` | `lookaside.fbsbx.com`; comma-separated HTTPS download host allowlist. |
| `WHATSAPP_MEDIA_TIMEOUT_SECONDS`, `WHATSAPP_MEDIA_MAX_BYTES` | 30 seconds; 104857600 bytes (100 MiB). |
| `DOWNLOAD_CONCURRENCY`, `OCR_CONCURRENCY` | 20 concurrent downloads; 1 OCR task per worker process. |
| `DOWNLOAD_MAX_ATTEMPTS`, `OCR_MAX_ATTEMPTS` | 3 attempts per stage. |
| `QUEUE_RETRY_DELAYS_SECONDS` | `1,5`; later retries reuse the last delay. |
| `DOWNLOAD_CLAIM_IDLE_MS`, `OCR_CLAIM_IDLE_MS` | 300000 / 1800000 ms before reclaiming idle jobs. |
| `REDIS_DEAD_LETTER_MAX_ENTRIES`, `REDIS_DEAD_LETTER_RETENTION_DAYS` | Approximately 10000 entries / 30 days, trimmed when adding failures. |
| `OCR_PREPROCESSING` | Code default `off`; example `full`. Also accepts `orientation`. |
| `OCR_PDF_DPI`, `OCR_BOUNDARY_ALLOWANCE` | 300 DPI; 0.04 document-boundary expansion. |
| `OCR_PREPROCESSING_MODELS`, `OCR_PREPROCESSING_CACHE` | Optional overrides for local model/cache directories under `.ocr_preprocessing`. |

Select a backend by changing `invoice_extractor` in
[src/ocr/__init__.py](../src/ocr/__init__.py). Qwen also accepts
`OPENROUTER_OCR_MODEL` (default `qwen/qwen2.5-vl-72b-instruct`).

Surya requires a separately installed llama.cpp `llama-server` executable:
`SURYA_LLAMA_DEVICE=cpu` uses `LLAMA_CPP_CPU_BINARY` (or legacy `LLAMA_CPP_BINARY`);
`cuda` requires `LLAMA_CPP_CUDA_BINARY` and matching NVIDIA runtime binaries.
First use downloads Surya weights. Leave `SURYA_INFERENCE_URL` unset for the
managed local server. Stop any kept-alive server before switching devices.
Surya invoice parsing still calls OpenAI; local text recognition alone does not.

## Tests without billable OCR

Tests are grouped by purpose: `tests/benchmarks` measures accuracy using real OCR,
`tests/validation` checks component behavior and regressions, and `tests/integration`
checks interactions across the API, workers, queues, SQL, and storage. Shared
helpers and datasets remain under `tests`. See the [test suite guide](../tests/README.md).

Run routine checks with the live database test opt-in unset:

```powershell
Remove-Item Env:RUN_LIVE_OCR_DB_TEST -ErrorAction SilentlyContinue
python -m ruff check .
python -m pytest tests/validation tests/integration -q
python -m alembic heads
```

The broad test command includes preprocessing dataset checks, which use local
models/images when available and otherwise skip. Targeted checks:

| Check | Command |
| --- | --- |
| Download → OCR → SQL pipeline with test doubles | `python -m pytest tests/integration/test_invoice_pipeline.py -v` |
| Offline invoice database round-trip | `python -m pytest tests/integration/test_invoice_database_live.py::test_database_roundtrip_with_offline_ocr -v` |
| Scoring and report generation | `python -m pytest tests/validation/test_invoice_scoring_v2.py tests/validation/test_invoice_report_v2.py -q` |
| Provider adapters and Surya evidence | `python -m pytest tests/validation/test_ocr_openai.py tests/validation/test_ocr_mistral.py tests/validation/test_ocr_qwen.py tests/validation/test_ocr_surya.py tests/validation/test_ocr_abc.py tests/validation/test_ocr_evidence.py -q` |
| Document preparation and crop regression | `python -m pytest tests/validation/test_preprocessing.py tests/validation/test_preprocessing_dataset.py -v` |

Adapter tests validate integration behavior with doubles; they do not measure
recognition accuracy. Preprocessing quality likewise does not prove extraction
accuracy: inspect crops for lost text and compare live benchmark results.
Earlier crop validation recorded selection errors and clipped text; a passing
large-invoice regression test is not a dataset-wide crop approval.

### Real MinIO integration check

After starting and provisioning local MinIO, run:

```powershell
$env:RUN_MINIO_INTEGRATION = "1"
python -m pytest tests/integration/test_minio_live.py -v
Remove-Item Env:RUN_MINIO_INTEGRATION
```

This tests image/PDF byte transfers, content metadata, retry reuse, signed GET,
private unsigned access, URL expiry, and denied deletion by the application
account. It writes two small originals under unique keys in the development
bucket; those test originals remain there because the application cannot delete
them. Use the development service for this opt-in test.

## Existing attachment migration

New submissions use MinIO after the SQL schema upgrade. Existing rows are marked
`local` and remain readable through `WHATSAPP_MEDIA_DIR`; there is no fallback to
local files for a MinIO row. To migrate originals, first back up SQL and the source
directory, then stop download and OCR workers.

```powershell
python -m src.object_storage.migrate_media --dry-run
python -m src.object_storage.migrate_media --apply
```

Dry run is the default and needs no MinIO connection or credentials. It checks
local paths, sizes, and supplied WhatsApp digests. Apply uploads each original,
reads it back, verifies its bytes, and commits its object reference separately.
The command reports planned, migrated, skipped, and failed counts and exits with
code 1 when any row fails. A failed row retains its local reference; rerunning
reuses matching uploads and skips already migrated rows. Original source files,
processing statuses, attempt counters, and invoice records are preserved.

Keep the source directory until migration and backups are verified. Pending OCR
jobs keep their submission IDs and read MinIO once their row is migrated. Restore
legacy backend references from the SQL backup to roll migrated rows back while
source files are retained. New MinIO-only submissions require export before
reverting to old workers. Automatic orphan deletion and removal of legacy files
are separate maintenance tasks.

## Live OCR benchmarks

These commands use the selected backend and real invoice images. API-backed
extraction is billable. Raw OCR scores extraction before accounting; the accounting
benchmark scores values after `reconcile_invoice()`, matching the worker's flow.

```powershell
# Recommended business scope: definitive valid and invalid documents.
python -m pytest tests/benchmarks/test_invoice_accuracy_v2.py --ocr-scope valid-invalid -s
python -m pytest tests/benchmarks/test_invoice_accounting_accuracy_v2.py --ocr-scope valid-invalid -s

# Diagnose one document.
python -m pytest tests/benchmarks/test_invoice_accuracy_v2.py --ocr-scope all --ocr-image "IMG_3446.HEIC" -s

# Include every annotated document, including review cases.
python -m pytest tests/benchmarks/test_invoice_accounting_accuracy_v2.py --ocr-scope all -s
```

Both benchmarks accept the same selection settings:

| Option / environment variable | Meaning |
| --- | --- |
| `--ocr-scope` / `OCR_TEST_SCOPE` | `valid`, `invalid`, `valid-invalid`, `review`, or `all`; default `all`. |
| `--ocr-image` | One annotated filename or unique stem; scope filtering still applies. |
| `OCR_IMAGE_DIR` | Image search root, default `tests/images`; searched recursively. |
| `OCR_V2_REPORT_DIR` | Raw report root, default `tests/results/v2`. |
| `OCR_ACCOUNTING_V2_REPORT_DIR` | Accounting report root, default `tests/results/accounting_v2`. |
| `-s` | Show per-document progress instead of capturing it. |

Ground truth is in `tests/ground_truths/ocr_ground_truth_v2.csv` and
`ocr_ground_truth_accounting_v2.csv`. Repeated document rows represent separate
fiscal lines but trigger one extraction. Keep image basenames stable and unique;
the first folder under the image root supplies the report category. The accounting
test skips if its CSV is absent.

Classification and field accuracy must each exceed 70% when scored; extraction
errors also fail the run. Review annotations are excluded from binary
classification. Blank or `-` reference cells are unscored, so annotate expected
derived fields in the accounting CSV to measure them. `diagnostic_type` and fiscal
status are informative, not scored.

Field comparisons normalize case/whitespace, date formats, and numeric formatting.
Numeric identifiers ignore leading zeroes during scoring; alphanumeric identifiers
retain zeroes; supplier tax ID comparison removes hyphens. Supplier-name matching also tolerates legal forms
and commercial-name suffixes. See [the scorer](../tests/invoice_accuracy_v2.py)
for exact normalization and document-total derivation rules.

### Reports

Each run saves `runs/<run-id>/dashboard.html`, `run.json`, `results.csv`, and
`fields.csv` under its report root. Open the self-contained dashboard while the
run progresses: it shows document previews, expected/obtained fields, errors,
category and field accuracy, and earlier runs. Clicking a field filters its
mismatches. The accounting tooltip lists calculated fields; raw OCR displays
**Not checked**, which means no fiscal result and is not a stored status.

Results save after every document. `Ctrl+C` preserves an interrupted report;
force-killing can leave the last snapshot labelled running. Errors score as
failures; pending documents do not enter the provisional score. Overall field
accuracy is correct fields divided by scored fields, not an average of categories.
Original/Prepared preview toggles do not change processing or saved results.

`run.json` preserves exact data; CSVs use percentages and UTF-8 with BOM. Import
identifiers as text in Excel to preserve zeroes. Keep the run's files and images
together when sharing. Default report directories are ignored by Git.

### Live OCR and database round-trip

```powershell
$env:RUN_LIVE_OCR_DB_TEST = "1"
python -m pytest tests/integration/test_invoice_database_live.py::test_live_ocr_persists_validity_and_diagnosis -v -s
Remove-Item Env:RUN_LIVE_OCR_DB_TEST
```

This extracts a real invoice and proforma, saves and rereads their classification,
diagnosis, and fiscal rows, and prints stored records as JSON. Both database
round-trip tests use isolated in-memory SQLite, not the application database.

## Common changes and data maintenance

| Change | Start here |
| --- | --- |
| Accept or interpret WhatsApp events | [events.py](../src/whatsapp/events.py), [media.py](../src/whatsapp/media.py) |
| Change classification or field rules | [prompts.py](../src/ocr/prompts.py); provider-specific wrappers live in their adapters. |
| Add/change invoice fields | [OCR model](../src/ocr/models.py), [SQL records](../src/sql_database/models.py), [SQL mapping](../src/sql_database/operations.py), and a migration; update fixtures/scoring as needed. |
| Change fiscal calculations | [accounting.py](../src/invoices/accounting.py) and its tests. |
| Change delivery, retries, or concurrency | [redis_streams.py](../src/jobs/redis_streams.py), [worker settings](../src/workers/settings.py), and workers. |

Prefer clear names, direct control flow, standard library/existing dependencies,
and the smallest complete change. Validate untrusted data at boundaries; preserve
visible errors. Add Python input/output type hints and useful public docstrings.
Follow surrounding conventions and update tests when behavior changes.

Use Alembic for schema changes and `python -m alembic upgrade head` to apply them.
Older invoices lacking classification or fiscal status need reprocessing before
strict domain reads; there is no implemented replay tool.

For a development reset, stop the API and workers, then run:

```powershell
.\scripts\reset-development.ps1
```

It requires typing `RESET`; `-Force` skips that prompt. It deletes the fixed local
`local_database/cif_campeador.db` and its SQLite sidecars, clears all four application Redis
streams, and runs migrations against the configured database. Downloaded media
and MinIO objects are preserved. Use it with the default local database configuration.

## Preprocessing attribution

The local adapters use DocAligner's heatmap algorithm and Paddle's orientation
configuration. Pinned revisions and hashes are in
[preprocessing_models.py](../src/ocr/preprocessing_models.py); the Apache-2.0
license remains in [docs/licenses](licenses/Apache-2.0.txt).
