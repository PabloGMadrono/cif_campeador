# MinIO media storage

Status: Storage, workers, schema migration, development setup, and media migration
implemented. Real Docker/MinIO validation is pending; frontend delivery and
production hosting remain future work. Created: 2026-10-02.

## Goal

Store original WhatsApp invoice attachments in a private MinIO bucket instead of
keeping them in the application's local media directory. Download and OCR workers
must be able to run on different machines without a shared filesystem. Preserve
stable document references so a future frontend can display the original image or PDF.

## Scope and assumptions

- Include JPEG, PNG, WebP, and PDF: these are the attachment types already supported.
- MinIO becomes the durable store for all new attachments. Local files are allowed
  only as temporary processing inputs and existing OCR caches.
- Keep the current webhook, Redis job payloads, OCR providers, processing statuses,
  retry limits, and invoice extraction behavior.
- Include configuration, development infrastructure, tests, and a migration tool
  for existing attachments.
- Default scope: implement storage and OCR now; document frontend delivery for a
  later feature. There is currently no frontend authentication or review API.
- Exclude frontend UI, browser uploads, thumbnails, migration of preprocessing
  caches, automatic deletion of originals, and changes to invoice accounting.

## Current behavior

`WhatsAppMediaDownloader` streams attachments into `WHATSAPP_MEDIA_DIR` using
`YYYY/MM/DD/<media-id>.<extension>`. It validates the download host, MIME type,
size, and the supplied SHA-256 digest before publishing the file.

`InvoiceSubmissionLifecycle.record_download()` saves `storage_path` and
`file_size` in `invoice_submissions`. `OcrWorker` joins that path to the local
media directory and passes it to `extract_invoice(path)`. Redis carries only
submission identifiers for OCR. Compose currently runs Redis only.

## Proposed flow

1. The webhook publishes the existing download job.
2. The download worker creates or reuses the submission and records its attempt.
3. Download the attachment from Meta into a unique temporary directory. Preserve
   all current validation and compute its SHA-256 while streaming. Keep the real
   extension so existing image/PDF processing works.
4. Upload the validated file to MinIO with the correct `Content-Type` and SHA-256
   metadata. Only successfully completed uploads count as stored attachments.
5. Commit the bucket, object key, size, checksum, and `downloaded` status in one
   SQL transaction. Enqueue OCR through the existing Redis handoff.
6. The OCR worker retrieves the object into its own temporary directory, verifies
   size and checksum, and calls the existing extractor with that local path.
7. Save the invoice using the current lifecycle. Remove temporary files after
   success or failure. Preserve the MinIO original even when OCR fails.

Neither worker needs access to the other's filesystem. Uploads, reads, and OCR
remain outside SQL transactions. Use bounded streaming/file transfers rather
than loading an entire attachment into memory.

The preprocessing cache currently stores rendered original and prepared pages.
It stays local and disposable; this feature moves durable source attachments,
not every image generated during OCR. Normal cleanup uses temporary-directory
contexts. Abrupt process termination may leave temporary files; deployment
cleanup must remove stale attempt directories only when workers are stopped.

## Object identity and metadata

Default bucket: `invoice-originals`.

Object key:

```text
originals/YYYY/MM/DD/<submission-uuid>.<extension>
```

Use the submission's received date in UTC and the existing MIME-to-extension map.
The key is deterministic for retries and unique per submission. Do not put phone
numbers, customer names, or original filenames in object keys.

Store the object's MIME type as `Content-Type` and the computed lowercase hex
SHA-256 as user metadata `sha256`. Retain the original filename in SQL. Never use
the S3 ETag as the application's SHA-256 digest.

## SQL and domain changes

Add an Alembic migration; do not reinterpret existing local paths as object keys.

| Field | Meaning |
| --- | --- |
| `storage_backend` | Required `local` or `minio`; backfill existing rows as `local`. New submissions use `minio`. |
| `storage_bucket` | Nullable string; required when a MinIO attachment is successfully stored. |
| `storage_object_key` | Nullable string; required when a MinIO attachment is successfully stored. |
| `content_sha256` | Nullable 64-character lowercase hex digest; required for newly stored or migrated MinIO attachments. |
| `storage_path` | Existing local path; retain for legacy reads and migration rollback. New MinIO submissions leave it null. |
| `file_size` | Existing field; actual validated attachment size in bytes. |
| `sha256` | Existing optional base64 digest supplied by WhatsApp; keep its meaning unchanged. |

Add a unique constraint on `(storage_bucket, storage_object_key)`. Storage location
fields may remain null until download completes. Persist MinIO location, size,
and checksum together; a downloaded MinIO row must contain all of them.

Replace the durable meaning of `DownloadedAttachment` with a small stored
attachment value containing bucket, object key, size, and checksum. Temporary
paths belong to download/OCR operations, not persisted storage references.

Read legacy `local` rows using the existing path containment check until migration
is complete. New writes always go to MinIO; a MinIO outage must never trigger a
silent fallback to local storage. Never persist credentials, endpoint URLs, or
presigned URLs in document records or Redis jobs.

## Storage integration

Add a focused `src/object_storage` module with settings and one MinIO client wrapper for
uploading files, retrieving files, inspecting objects, and generating expiring
read URLs. Avoid a general plugin framework or multiple selectable storage providers.
Keep the legacy local-read branch at the OCR boundary during migration.

Use the official Python `minio` SDK, adding a tested version constraint to
`requirements.txt`. Its file upload/download and presigned GET APIs cover this
feature. Create a client per worker process; synchronous storage operations in
the async download worker must run through `asyncio.to_thread`. OCR already runs
outside the event loop. Configure bounded connection/read timeouts and avoid
stacking long SDK retry loops on the existing worker retries.

See the [official Python SDK reference](https://github.com/minio/minio-py/blob/master/docs/API.md).

## Retries and consistency

- Upload failure: do not mark the submission downloaded or publish OCR. Reuse
  the current download retry/dead-letter path; permanent errors use stage `download`.
- Upload succeeded but SQL failed: the next attempt uses the same object key.
  After validating the source again, inspect the existing object. Reuse it only
  when size, MIME type, and SHA-256 metadata match; fail visibly on a conflict.
  A simultaneous identical upload to the deterministic key must be harmless.
- SQL committed but Redis handoff failed: redelivery reuses the downloaded row
  and performs the existing handoff without creating a new object.
- Retrieval failure, missing object, or integrity mismatch during OCR: use the
  existing OCR retry/dead-letter path and stage `ocr`. Do not attempt local fallback
  or delete the original object.
- Duplicate delivery of completed submissions must not rerun OCR or create an
  additional object or invoice.
- SQL and MinIO cannot commit atomically. An unreferenced object may remain after
  an exhausted attempt. Keep it for diagnosis; automated orphan cleanup is deferred.

## Where MinIO runs

### Local development

MinIO runs in a Docker container on the developer's machine, managed by this
repository's `compose.yaml` alongside Redis. The API and workers continue running
as local Python processes through `scripts/start-development.ps1`.

| Consumer | MinIO address |
| --- | --- |
| Local Python download/OCR workers | `localhost:9000`, HTTP for development |
| Browser accessing a development original URL | `http://localhost:9000` on the same developer machine |
| Developer opening the administration console | `http://localhost:9001` |
| Future workers running inside the same Compose network | `minio:9000`, using the Compose service name |

Persist objects in a Docker named volume, `minio-data`, mounted at the MinIO
container's `/data`. Restarting or replacing the container must retain this
volume. MinIO still writes bytes to its host's disk: the change removes durable
attachments from the application's filesystem and puts storage behind an object
storage service. It does not automatically move files to a cloud provider.

The current ngrok tunnel exposes FastAPI only. A frontend on another machine
cannot access a developer's MinIO through `localhost` or through that existing
API tunnel. Remote frontend testing requires a separately reachable S3 endpoint
and URLs signed for that endpoint.

### Production decision

Production hosting is not yet selected. Confirm whether MinIO will run on the
backend server or on a dedicated storage server before finalizing deployment.
This spec does not provision a cloud account, production server, or public domain.

For a first deployment, a proposed simple topology is one MinIO container on the
same server as the backend, with its own persistent storage volume. A dedicated
MinIO server is also compatible with the storage design: workers use its
configured endpoint rather than a shared filesystem. A single-server deployment
has no storage high availability; a server or disk failure interrupts access.

Whichever host is chosen, the production deployment must define:

- The server that runs MinIO and the persistent disk/volume containing `/data`.
- A private S3 endpoint reachable by workers and, when frontend access is enabled,
  a browser-reachable HTTPS S3 endpoint used when signing URLs.
- TLS termination and network access. Keep the administration console private;
  expose only the S3 API needed for authorized object requests.
- Who operates MinIO, monitors disk capacity and availability, and backs up and
  restores the object data alongside SQL. A persistent volume is not a backup.

## Configuration and development

| Variable | Purpose / proposed default |
| --- | --- |
| `MINIO_ENDPOINT` | Internal S3 endpoint as `host:port`; local example `localhost:9000`. Required for workers. |
| `MINIO_ACCESS_KEY` | Application service-account access key. Required. |
| `MINIO_SECRET_KEY` | Application service-account secret. Required. |
| `MINIO_SECURE` | TLS flag; default `true`, explicitly `false` for local HTTP development. |
| `MINIO_BUCKET` | Bucket for new writes; default `invoice-originals`. |
| `MINIO_REGION` | Explicit bucket region; proposed local default `us-east-1`. |
| `MINIO_CONNECT_TIMEOUT_SECONDS` | Positive connection timeout; default `5`. |
| `MINIO_READ_TIMEOUT_SECONDS` | Positive socket read timeout; default `30`. |
| `WHATSAPP_MEDIA_DIR` | Legacy read/migration location only; no new permanent files. |

Validate required credentials, endpoint format, booleans, bucket name, and positive
timeouts when the consuming process starts. Keep secrets out of logs and committed
configuration. A runtime outage is handled by worker retries; invalid configuration
or an unprovisioned bucket must produce a clear startup error.

`compose.yaml` runs MinIO with a persistent data volume, an S3 API port (`9000`),
and a console port (`9001`) bound to localhost for development. The Dockerfile
builds the official pinned server release `RELEASE.2025-10-15T17-29-55Z` from source,
following its [release instructions](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z).
Add an idempotent provisioning step that waits for readiness, creates the private
bucket, and configures an application account scoped to the required bucket actions.
Root credentials are provisioning-only, separate from application credentials.

Update `.env.example`, `scripts/start-development.ps1`, and the development guide
so one startup command starts Redis and MinIO and provisions storage before workers.
Document that Compose volume deletion destroys stored attachments and that SQL
and object storage both need backups. Development database reset should preserve
MinIO originals, matching current media preservation behavior.

## Future frontend access contract

This section defines a later API feature; do not expose an unauthenticated image
endpoint as part of the storage change.

Proposed route: `GET /invoice-submissions/{submission_id}/original-url`.

The backend authenticates the requester, authorizes access to the submission's
customer, checks that the original exists, and returns:

```json
{
  "submission_id": "<uuid>",
  "url": "<presigned GET URL>",
  "expires_at": "<UTC ISO-8601 timestamp>",
  "mime_type": "image/jpeg",
  "filename": "<display filename or generated fallback>"
}
```

Default lifetime: 5 minutes. Return `Cache-Control: no-store` on the URL response.
The frontend requests a fresh URL after expiry. Authentication is mandatory;
return `401` without it, `404` for unknown or inaccessible submissions, `409` when
the original is not yet stored, and `503` for unavailable storage or an unexpectedly
missing stored object. Log internal details without exposing storage credentials.

Use a separately configured browser-reachable S3 endpoint when signing URLs.
Never replace the hostname of an already signed URL. Define explicit region/TLS
settings for the signing client and test any reverse proxy with the actual browser
URL. The browser uses the S3 API endpoint, not the MinIO console. Configure CORS
for approved frontend origins and the GET/HEAD operations needed by browser
fetch/PDF viewing; no browser upload permissions are needed.

The bucket remains private. The URL grants temporary access to its holder; do not
log it, persist it, or send MinIO service credentials to the frontend. Authorization
must occur before signing; object naming does not enforce customer access.

## Existing attachment migration

Provide an explicit command with dry-run and apply modes. Pause ingestion and OCR
while applying the migration; back up SQL and retain the source media directory.

1. Select legacy local rows with a stored path, including completed and failed rows.
2. Resolve each path within `WHATSAPP_MEDIA_DIR`; report missing files or escaped
   paths without modifying those rows.
3. Compute size and SHA-256; verify existing size and supplied WhatsApp digest where
   present. Upload using the new deterministic key and recorded MIME type.
4. Read the uploaded object back and compare size and digest. Only then atomically
   update that row's backend, bucket, key, and checksum. Preserve statuses, attempt
   counters, invoice data, and legacy path.
5. Skip already migrated rows. Reuse matching uploaded objects after an interrupted
   run; report conflicts. Report migrated, skipped, and failed totals and use a
   nonzero exit code if any row failed.

Never delete local originals during migration. Retire legacy reads and remove
retained files only in a separate follow-up after migration and backup verification.
Before resuming workers, prove that migrated pending OCR jobs can read MinIO.
Rollback migrated rows can restore local references while the retained files
exist; new MinIO-only rows require export before reverting to old worker code.

## Acceptance criteria and validation

1. A supported attachment is stored byte-for-byte in the private bucket with the
   expected deterministic key, MIME type, size, and checksum in MinIO and SQL.
2. No new permanent attachment is written under `WHATSAPP_MEDIA_DIR`. Temporary
   download and OCR files are removed on success and handled failure.
3. OCR completes when download and OCR workers use separate local directories.
   Test both an image and a PDF without paid/live OCR providers.
4. Unsupported MIME types, untrusted URLs, oversized files, and digest mismatches
   retain existing rejection behavior and do not produce a stored submission.
5. MinIO upload/read errors, missing objects, and integrity errors use the existing
   stage-specific retry/dead-letter behavior without incorrect completed status.
6. Redelivery, upload-before-SQL failure, SQL-before-Redis failure, and overlapping
   identical attempts preserve one object reference and at most one invoice.
7. Migration is resumable, verifies bytes, preserves statuses and source files, and
   reports missing/conflicting attachments without switching their backend.
8. Development startup provisions a private bucket; restarting MinIO preserves
   objects. Application credentials are restricted to the configured bucket.
9. A short-lived signed GET generated by the storage module retrieves the original
   through a browser-reachable endpoint; unsigned GET fails. No public API endpoint
   is required to exercise this storage capability.

Adapt `tests/validation/test_whatsapp_media.py` and `tests/integration/test_invoice_pipeline.py`; add
focused storage settings/client and migration tests following the existing style.
Use fakes for worker failure scenarios and an opt-in real-MinIO integration test
for SDK transfers, MIME metadata, private access, and URL expiry. Run the existing
webhook, job, pipeline, and migration regression suites. Documentation-only spec
creation does not change runtime behavior.

## Implementation sequence

1. Add MinIO configuration, client wrapper, dependency, Compose provisioning, and tests.
2. Add the SQL migration and stored attachment value; implement temporary download,
   verified MinIO upload, and durable metadata persistence.
3. Switch OCR to temporary MinIO retrieval with integrity checks; retain legacy reads.
4. Add and verify the existing-file migration command and failure/replay scenarios.
5. Update architecture, data-model, and development documentation and run regressions.

Frontend authentication, authorization, the URL endpoint, and browser CORS setup
form a subsequent feature using the access contract above.
