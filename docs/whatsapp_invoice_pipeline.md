# WhatsApp invoice pipeline

Inbound WhatsApp images and PDFs are processed through two Redis Streams:

```text
Meta webhook -> whatsapp:downloads -> download worker
             -> whatsapp:ocr       -> OCR worker -> SQL database
```

Redis is the job queue. SQL stores customers, phone mappings, submission status,
and extracted invoice data. Jobs and database writes are idempotent by WhatsApp
message ID and the deterministic submission UUID.

## Local setup

Install dependencies and copy the environment template values into `.env`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up -d redis
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Run the API and both independent workers in separate terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
.\.venv\Scripts\python.exe -m src.workers.download
.\.venv\Scripts\python.exe -m src.workers.ocr
```

The selected OCR backend remains `invoice_extractor` in `src/ocr/__init__.py`.
The default worker limits are 20 concurrent async downloads and one OCR task.

## Reading the code

- `src/workers/download.py` contains `DownloadWorker`, whose `process_download`
  method prepares a download and records its file metadata.
- `src/workers/ocr.py` contains `OcrWorker`, whose `process_ocr` method extracts
  an invoice and saves the result. Both worker modules receive Redis jobs, retry
  failed attempts, and acknowledge successful work. Their processing methods
  use short database transactions around the file or OCR call.
- `src/persistence/operations.py` contains queries, invoice storage functions,
  and the shared `mark_failed` function used by both workers.
  These use the caller's SQLAlchemy session; the caller owns the transaction.
- `src/persistence/models.py` defines the database records used by these functions.
- `src/ocr/__init__.py` selects the OCR backend. `OcrWorker` receives an
  `Ocr_operator` and calls its `extract_invoice` method.

Workers receive their database, backend, and settings once during startup.
Processing and failure handling are instance methods: `process_ocr(document_id)`,
`process_download(job)`, and `mark_failed(document_id, error)`. OCR processing
returns a `DocumentStatus`; the worker already has the document ID for logging.
Download processing returns the document needed for the OCR handoff.

The Redis consumer receives `RedisQueueSettings` and owns the claim timeout for
its stream and the dead-letter retention limits. Workers request stale jobs with
`claim_stale(count=...)` without forwarding those settings on every call.

`session_factory.begin()` commits on success and rolls back
on failure. Sessions keep scalar fields available after commit; invoice reads
convert tax relationships into the shared `Invoice` before the session closes.

## Processing guarantees

- The webhook returns success only after Redis accepts every supported attachment.
- Images and PDF documents are queued; text and other document formats are ignored.
- Download messages are acknowledged only after the file metadata is committed and
  an OCR message is published. The handoff acknowledges and deletes the download
  entry atomically.
- OCR messages are acknowledged only after the invoice transaction commits.
- Successfully processed and superseded retry entries are deleted from their
  active streams; SQL remains the permanent audit history.
- Consumer groups retain unacknowledged work; idle jobs are reclaimed after the
  configured claim timeout.
- A stage receives three attempts by default, then moves to its dead-letter stream.
- Dead-letter streams retain approximately 10,000 entries and trim entries older
  than 30 days whenever a new failure is added. Both limits are configurable.
- Duplicate jobs reuse downloaded files and completed invoices.

The dead-letter streams are `whatsapp:downloads:dead` and `whatsapp:ocr:dead`.
Webhook signature verification is not implemented yet and remains required before
deploying this publicly beyond the current development setup.
