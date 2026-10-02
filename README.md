# CIF Campeador

CIF Campeador receives invoice attachments through WhatsApp, downloads them,
extracts structured invoice data with OCR, checks the fiscal arithmetic, and
stores originals in private MinIO storage and results in SQL. Download and OCR run asynchronously through Redis
Streams.

![Invoice processing architecture](docs/architecture-diagram.png)

The ingestion pipeline is implemented. The frontend and invoice review/edit API
shown in the diagram are not implemented in this repository.

## Start here

| Guide | What you will learn |
| --- | --- |
| [Architecture](docs/architecture.md) | How the diagram maps to code and where data lives. |
| [Invoice processing](docs/invoice-processing.md) | How one attachment becomes a classified, reconciled invoice. |
| [Data model](docs/data-model.md) | Tables, relationships, field conventions, and every stored status. |
| [Development](docs/development.md) | Setup, configuration, tests, benchmarks, and where to make changes. |
| [Test suites](tests/README.md) | Benchmarking, functionality validation, and integration test folders and commands. |

Feature specification: [MinIO media storage](docs/minio-media-storage-spec.md).

For local development, create `.venv`, install `requirements-dev.txt`, and
configure `.env` from `.env.example` as described in the development guide. Then:

```powershell
.\scripts\start-development.ps1
```

The script starts Redis and MinIO, provisions the private bucket, applies migrations, and opens the API, both workers,
and ngrok. Docker Desktop must be running; use `-SkipNgrok` for local-only work.
