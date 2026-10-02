# Test suites

| Folder | Purpose | Examples |
| --- | --- | --- |
| `benchmarks/` | Measure OCR and accounting accuracy on real annotated invoices. These runs can make billable OCR calls. | Invoice accuracy and accounting accuracy. |
| `validation/` | Validate individual behaviors, provider adapters with test doubles, scoring, reports, accounting, and preprocessing regressions. | MIME validation, storage checks, fiscal calculations, crop regression. |
| `integration/` | Check interactions across API, queues, workers, SQL, and object storage. Most use offline doubles; live checks require explicit opt-in. | Webhook publication, download/OCR pipelines, database and media migrations, real MinIO. |

Run each suite from the repository root:

```powershell
python -m pytest tests/validation -q
python -m pytest tests/integration -q
python -m pytest tests/benchmarks --ocr-scope valid-invalid -s
```

For routine checks without billable OCR, leave `RUN_LIVE_OCR_DB_TEST` unset and run
`python -m pytest tests/validation tests/integration -q`. The live SQL/OCR test
requires `RUN_LIVE_OCR_DB_TEST=1`; real MinIO checks require
`RUN_MINIO_INTEGRATION=1` and a running development MinIO service. Preprocessing
dataset checks use local images/models and skip when those assets are absent.

Shared helpers and fixtures remain at this directory's root. `images/`,
`ground_truths/`, `templates/`, and `results/` retain their existing locations, so
benchmark inputs and report output paths stay the same. The root `conftest.py`
registers the shared benchmark options.

See the [development guide](../docs/development.md) for configuration, targeted
commands, and live-test setup.
