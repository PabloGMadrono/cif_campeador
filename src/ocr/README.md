# OCR backends

All backends now use [shared local preprocessing](../../docs/ocr_preprocessing.md).
Install the new requirements and run `python -m src.ocr.preprocessing --setup-models`
before using `OCR_PREPROCESSING=orientation` or `full`. The default is `off`
until crop validation passes. Prepared/original previews are identical in off mode.

All invoice extraction paths share Spanish invoice/receipt terminology for
invoice numbers, supplier tax IDs (CIF/NIF/DNI/NIE/VAT) and legal names, including
company suffixes and individual suppliers. Supplier tax IDs take priority over
commercial names; ambiguous operation/reference numbers need document context.
The glossary also guides Qwen text transcription without changing
printed labels or omitting other text. Surya uses it in the invoice block parser;
its local recognition predictor does not accept this prompt.

The shared result keeps each printed VAT row in `lineas_iva`. Equivalence
surcharges live separately in `recargos_equivalencia`, and an explicitly
printed withholding lives in `retencion_irpf`. Backends do not calculate an
effective VAT rate, aggregate fiscal rows, or infer missing tax values.

## Which prompt to edit

The shared invoice rules are defined in `src/ocr/prompts.py`:

- `INVOICE_CLASSIFICATION_RULES`: valid/invalid decisions and the Proforma rule.
- `INVOICE_EXTRACTION_RULES`, `INVOICE_FISCAL_AND_IDENTITY_RULES`, and
  `INVOICE_LABEL_HINTS`: field extraction rules and terminology.
- `INVOICE_RULES`: assembles those rules for each invoice parser.

Provider-specific instructions live beside their integrations:

- `OPENAI_IMAGE_INVOICE_PROMPT` in `src/ocr/ocr_openai.py` adds image-reading
  instructions to `INVOICE_RULES`. `Ocr_openai.extract_invoice` sends it in one
  model call; this is the active application and benchmark path, selected in
  `src/ocr/__init__.py`.
- `SURYA_BLOCK_INVOICE_PROMPT` in `src/ocr/ocr_surya.py` adds block and source
  citation instructions to `INVOICE_RULES`. Surya's local text recognition has
  no LLM prompt.
- `TEXT_INVOICE_PROMPT` in `src/ocr/ocr_abc.py` adds instructions for backends
  that parse already-transcribed OCR text, such as Qwen.

The image and block prompts both include the same classification rules. The
`Invoice` and `InvoiceEvidence` models in `src/ocr/models.py` and
`src/ocr/evidence.py` define the output schemas, not additional instructions.

## Mistral Document AI OCR

`Ocr_mistral` uses [Mistral's OCR API](https://docs.mistral.ai/studio/document-processing/basic_ocr)
with `mistral-ocr-latest` and `include_image_base64=True`. Install
`requirements.txt` and set `MISTRAL_API_KEY` in your environment or project
`.env`. The Mistral client is created lazily and reused, with a 120-second
request timeout. Restart the process after changing configuration.

```python
from src.ocr.ocr_mistral import Ocr_mistral

ocr = Ocr_mistral()
text = ocr.extract_text("documento.pdf")
invoice = ocr.extract_invoice("factura.HEIC")
```

PDFs are submitted intact only when preprocessing is off. Otherwise the shared
stage renders and prepares them. A single prepared image is sent as PNG;
multiple pages are combined in a lossless raster PDF for one request.
HEIC/HEIF and multipage TIFFs are supported.
`extract_text` returns page Markdown joined with blank lines, retaining
inline tables. Returned image base64 data is not embedded in the text or saved.
Encoded documents are held in memory and must fit Mistral's request limits.

`extract_invoice` returns the shared `Invoice` directly in **one Mistral OCR
request**, using `document_annotation_format` with a strict JSON schema generated
from the Invoice dataclass. `document_annotation_prompt` reuses the OpenAI image
invoice instructions, including the shared field rules and Spanish terminology.
The returned `document_annotation` JSON is validated locally with Pydantic.
Both extraction methods only require `MISTRAL_API_KEY`; invoice extraction
does not call the shared OpenAI parser. Explicit calls to the inherited
`parse_invoice(raw_text)` still use OpenAI and require `OPENAI_API_KEY`.

Multipage images are packaged into one PDF in frame order at the configured DPI
(default 300), with lossless RGB compression. Both extraction methods use this
document-wide transport.

Every annotation includes binary `validity`, optional `diagnostic_type`, the
identity fields, separate VAT/RE/IRPF structures, and `total`. Missing,
malformed or schema-invalid annotations raise `RuntimeError`; there is no parser
fallback. Empty OCR Markdown alone is not treated as a successful annotation.
Plain-text extraction rejects missing pages. File, API and SDK response-validation
errors propagate. Extraction sends document content to Mistral and is billable.

To select this backend for the application and accuracy benchmark, set
`invoice_extractor = Ocr_mistral()` in `src/ocr/__init__.py`.

```shell
python -m src.ocr.ocr_mistral "documento.pdf"
python -m unittest tests.test_ocr_mistral -v
```

The CLI prints Markdown. Adapter tests run offline and do not measure OCR accuracy.

## Direct invoice extraction with OpenAI Responses

`Ocr_openai.extract_invoice(path)` reads the document and returns the shared
`Invoice` in **one structured-output model call**. It overrides the base class's
two-stage flow: no intermediate transcription or second parsing call is used.
The request uses [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
(`gpt-5.6-luna`) with `reasoning={"effort": "low"}`,
`responses.parse(text_format=Invoice)`, and `store=False`.
Set `OPENAI_API_KEY` in the environment or project `.env`.

```python
from src.ocr.ocr_openai import Ocr_openai

ocr = Ocr_openai()
invoice = ocr.extract_invoice("factura.HEIC")
```

```shell
python -m src.ocr.ocr_openai "tests/images/trial_invoices/IMG_3309.HEIC"
```

The CLI prints the invoice as JSON. Local images are converted to PNG, with
camera orientation applied and transparent backgrounds composited onto white.
HEIC/HEIF and multipage TIFFs are supported. PDFs are rendered locally at 300 DPI
by default (`OCR_PDF_DPI` can override it).
All pages are sent in order in the same request as `input_image` items, using
`image_url="data:image/png;base64,..."` and `detail="high"`. No file upload or
public image URL is required. Encoded pages are held in memory for the request;
the complete document must fit the model's image, request and context limits.
The implementation follows the official guides for
[base64 image inputs](https://developers.openai.com/api/docs/guides/images-vision)
and [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

`Ocr_openai.extract_text(path)` is unsupported: direct invoice extraction does
not make an intermediate transcription call. `parse_invoice(raw_text)` retains
the inherited text-only parser for explicitly supplied OCR text. The image and
text parsers reuse the base class's lazy OpenAI client and its 120-second
timeout/default retries.

Unreadable fields are null. Refusals, incomplete responses, missing parsed output
and schema violations raise `RuntimeError`; file and API errors propagate.
Document images are sent to OpenAI and calls are billable. The offline tests
validate request construction and parsing, not live OCR accuracy.

To use this backend for the application and accuracy benchmark, set
`invoice_extractor = Ocr_openai()` in `src/ocr/__init__.py`, where it is already
imported. OpenAI is the current selection.

```shell
python -m unittest tests.test_ocr_openai -v
```

## Qwen2.5-VL via OpenRouter

`Ocr_qwen` implements `Ocr_operator.extract_text(path)` using the OpenAI Python
client pointed at OpenRouter. It follows the vision API approach in the
[Qwen2.5-VL walkthrough](https://medium.com/@tententgc/extracting-invoice-data-with-qwen2-5-vl-and-openrouter-an-ocr-walkthrough-in-python-7b5490578cad).
To fit the shared interface, it requests JSON containing a `text` transcription
and returns that field as plain text. Invoice structuring remains in the base
class's text-only parser; Surya instead supplies structured blocks and requests
source evidence before mapping the result to the same public Invoice schema.

Install `requirements.txt` and set these values in your environment or project
`.env` (existing environment variables take precedence):

```dotenv
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_OCR_MODEL=qwen/qwen2.5-vl-72b-instruct
# Also needed when calling extract_invoice or parse_invoice:
OPENAI_API_KEY=your_openai_key
```

`OPENROUTER_OCR_MODEL` is optional and defaults to the value above, the
[listed Qwen2.5-VL model](https://openrouter.ai/qwen/qwen2.5-vl-72b-instruct).
The walkthrough's `:free` suffix is not assumed to be available. Restart the
process after changing configuration. Both clients are created lazily and reused.

```python
from src.ocr.ocr_qwen import Ocr_qwen

ocr = Ocr_qwen()
text = ocr.extract_text("documento.pdf")
invoice = ocr.extract_invoice("factura.HEIC")
```

```shell
python -m src.ocr.ocr_qwen "tests/images/trial_invoices/IMG_3309.HEIC"
```

Images, including HEIC/HEIF and multipage TIFFs, are decoded with Pillow;
camera orientation is applied and transparency is composited onto white.
PDFs are rendered locally at 300 DPI by default with PDFium. Each page is sent as a PNG
data URL in a separate OpenRouter request, and page texts are joined with blank
lines. Enabled preprocessing needs its two local models; Qwen recognition needs
no local weights or llama.cpp server.

Text extraction sends document images to OpenRouter and its model provider.
`extract_invoice` additionally sends the resulting text to the shared OpenAI
parser. These are billable API calls. Each OCR request has a 120-second timeout
and the SDK's default retries; there is no whole-document timeout. OCR responses
are limited to 8192 output tokens per page. Truncation, refusals and malformed
JSON raise `RuntimeError`; decoding and API errors propagate. An explicit empty
transcription is valid and uses the base class's empty-invoice behavior.

To select Qwen for the application and accuracy benchmark, change
`src/ocr/__init__.py` to import `Ocr_qwen` from `.ocr_qwen` and assign
`invoice_extractor = Ocr_qwen()`. OpenAI is the current selection.

Run the offline adapter tests with:

```shell
python -m unittest tests.test_ocr_qwen -v
```

## Surya OCR with CPU or NVIDIA CUDA llama.cpp

Use 64-bit Python 3.12 on Windows/Linux. Install from the repository root.
The requirements select CPU-only PyTorch and torchvision wheels:

```shell
python -m pip install -r requirements.txt
```

The requirements also install the HEIC decoder needed for iPhone photos.

On Windows, download and extract the **Windows CPU x64** llama.cpp ZIP. For an
NVIDIA GPU, also download the matching **Windows CUDA x64** ZIP and its CUDA
runtime ZIP from the same [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases),
then extract both CUDA archives into one directory. These executables cannot be
installed by `pip install -r requirements.txt`.

Configure both paths once and select the runtime in the project `.env`:

```dotenv
LLAMA_CPP_CPU_BINARY=C:\Tools\llama.cpp-cpu\llama-server.exe
LLAMA_CPP_CUDA_BINARY=C:\Tools\llama.cpp-cuda\llama-server.exe
SURYA_LLAMA_DEVICE=cpu
```

`SURYA_LLAMA_DEVICE` accepts only `cpu` or `cuda`. CPU is the default. The old
`LLAMA_CPP_BINARY` variable remains a fallback for CPU configurations. CUDA
requires `LLAMA_CPP_CUDA_BINARY` so selecting it cannot silently launch a CPU
binary.

Verify either executable directly in PowerShell:

```powershell
& "C:\Tools\llama.cpp-cpu\llama-server.exe" --version
& "C:\Tools\llama.cpp-cuda\llama-server.exe" --list-devices
```

`Ocr_surya` always uses the `llamacpp` backend and defaults to one inference
request at a time. In CPU mode it disables all GPU offloading. In CUDA mode it
requests all model layers and the vision projector on the NVIDIA GPU. Python-side
preprocessing continues to use CPU-only PyTorch in both modes. If CUDA runs out
of VRAM, lower `settings.LLAMA_CPP_NGL` from 99 in the adapter.

First use downloads model weights and starts the selected local backend
automatically. Restart Python after changing `.env`, because configuration is
loaded once per process. If `SURYA_INFERENCE_KEEP_ALIVE` is enabled, stop the
existing `llama-server` before switching devices so Surya does not reconnect to
the server started with the previous executable:

```powershell
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process
```

For either local llama.cpp backend, leave `SURYA_INFERENCE_URL` unset. If you
previously set it, remove it in PowerShell with
`Remove-Item Env:SURYA_INFERENCE_URL -ErrorAction SilentlyContinue`; otherwise
Surya connects to that server, whose hardware is configured separately.

See the [Surya documentation](https://github.com/datalab-to/surya) for backend
details and the [PyTorch installation matrix](https://pytorch.org/get-started/previous-versions/)
for the selected CPU wheel versions.

```python
from src.ocr.ocr_surya import Ocr_surya

ocr = Ocr_surya()
text = ocr.extract_text("documento.pdf")  # Image paths also work.
print(text)
```

Spanish is recognized automatically. Results are plain Unicode text, with
newlines between blocks, tabs between table cells, and blank lines between PDF
pages. Reuse the same instance for subsequent documents.

### Surya invoice extraction with source evidence

`extract_invoice(path)` preserves Surya's page and block structure for the
structured parsing step and returns the complete shared `Invoice`.
It performs one Surya OCR pass and one structured Responses call. The parser
receives each block's stable document-local ID, pixel polygon, layout label,
reading order, original HTML and plain text, together with page bounds.
Skipped blocks are retained as diagnostics and cannot be cited as evidence.
Block errors still fail extraction; images are closed even when inference fails.

The parser returns a value, status (`printed`, `missing`, or `unreadable`) and
source quotes for each field, plus sources for its validity decision. Every
non-null value must cite existing readable blocks. Local validation checks
quoted text against those blocks, tolerating whitespace and Unicode composition
differences. These checks validate the references, not the correctness of the OCR, the chosen
supplier/document, or the arithmetic. Coordinates describe the decoded image;
they do not automatically orient or isolate overlapping receipts.

Use the explicit evidence API when you want to inspect or save the full result:

```python
from pathlib import Path
from src.ocr.ocr_surya import Ocr_surya

ocr = Ocr_surya()
result = ocr.extract_invoice_with_evidence("factura.HEIC")
invoice = result.invoice
print(result.evidence.nif_proveedor.sources)
Path("invoice-evidence.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
```

`extract_document(path)` exposes the OCR blocks without contacting OpenAI.
`parse_document(document)` parses those blocks without running Surya again.
You can save `document.model_dump_json()` and reload it with
`OcrDocument.model_validate_json(...)` from `src.ocr.evidence` to compare parser
changes against identical OCR input. `extract_text(path)` retains its existing
plain-text interface; explicit `parse_invoice(text)` uses the inherited text
parser. Empty usable OCR text produces `Invoice.unreadable()` without an API call.
Invalid schemas, missing citations, unknown/skipped source blocks and fabricated
quotes raise errors. Automatic crop rereads are not implemented in this change.

The accuracy benchmark uses `extract_invoice_with_evidence` when available and
saves successful extraction blocks and field citations under each invoice's
`extraction_evidence` in `runs/<execution-id>/run.json`. The legacy benchmark
still scores its original eight fields through a compatibility projection. Other backends and older runs have no evidence;
the dashboard and CSV scores remain compatible. Evidence is available in JSON,
not rendered as HTML in the dashboard. Ordinary `extract_invoice` calls do not
write files or keep mutable last-result state on the shared extractor.

Run the offline block/evidence and adapter checks with:

```shell
python -m unittest tests.test_ocr_surya tests.test_ocr_evidence tests.test_ocr_abc -v
```

To OCR the included trial invoice and print it directly in PowerShell, run
this from the repository root:

```powershell
python -m src.ocr.ocr_surya "tests/images/trial_invoices/IMG_3309.HEIC"
```

## Invoice model and accuracy tests

Run the level-two black-box benchmark from the repository root:

```shell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope all -s
```

It reads `tests/ground_truths/ocr_ground_truth_v2.csv`
and passes the corresponding image from `tests/images/trial_invoices` to
`extract_invoice(path)`, or the evidence API described above. The scored result
must be the shared `Invoice` Pydantic dataclass in `src/ocr/models.py`. It carries
binary `validity`, informative `diagnostic_type`, identity fields, tuples of
`IvaLine` and `EquivalenceSurcharge`, optional `IrpfWithholding`, and `total`.
`Invoice.unreadable()` creates the deterministic invalid result for documents
with no usable text. Pydantic rejects incorrect types, missing fields and extras.

Each OCR implementation implements `extract_text(path)`. The base class supplies
two concrete methods; Surya and direct vision backends override `extract_invoice`:

- `extract_invoice(path) -> Invoice`: runs OCR and passes its raw text to
  `parse_invoice`.
- `parse_invoice(raw_text) -> Invoice`: calls the OpenAI Responses API using
  `gpt-5.6-luna` and `responses.parse(text_format=Invoice)`. The SDK generates
  the strict schema, and Pydantic validates and parses the response directly
  into the shared Invoice dataclass.

Set `OPENAI_API_KEY` in your environment or project `.env` before running the
invoice benchmark. `src/config.py` loads the project `.env` once and exposes
API keys and the llama.cpp runtime selection for the OCR modules to import. Existing
environment variables take precedence; restart the process after changing
configuration. The OpenAI client is a cached property, created on first use
and reused without a constructor or a manual initialization check. Plain
text OCR with Surya does not require an API key. Surya sends OCR text/HTML and
block geometry to OpenAI; the base parser sends raw text. Both disable response
storage. The CSV reference data stays in the tests.

Empty OCR text returns an invalid unreadable Invoice without an API call. Unknown
fields are null. API errors propagate; refusals, incomplete responses and invalid payloads
raise errors instead of returning partial invoices. The benchmark scores failed
executions as zero and continues to the next image. The invoice benchmark runs
real OCR and makes billable API requests; it does not mock predictions.

The benchmark reports binary classification and field extraction separately.
Failed executions remain visible and subsequent documents still run. `-s`
keeps per-document progress visible while pytest is running.

Comparison ignores whitespace and letter case, accepts DD/MM/YYYY and ISO
dates, and compares numeric values exactly with Decimal, including integer
values with leading zeroes.
Spanish amounts such as `1.117,04 €` equal `1117.04`; `21,00%` equals `21`.
Purely numeric invoice numbers and tax identifiers ignore leading zeroes.
Alphanumeric identifiers retain leading zeroes, punctuation, and accents. Empty
reference cells are omitted from scoring. Reference amounts are used as
recorded, except for documented in-memory document-total handling.

The application and black-box tests import the shared extractor selected in
`src/ocr/__init__.py`:

```python
from src.ocr import invoice_extractor

invoice = invoice_extractor.extract_invoice("invoice.HEIC")
```

That module imports the available OCR implementations and selects the shared
`invoice_extractor`. To evaluate another implementation, import it there and
change that assignment; the tests require no changes. The shared
instance is reused for all images and receives only document paths. An optional
`OCR_IMAGE_DIR` overrides the directory containing the images.

### Dataset scopes

The richer benchmark reads `tests/ground_truths/ocr_ground_truth_v2.csv`, groups
repeated rows into one OCR execution per document, and keeps multiple fiscal
lines as separate expectations. Run it with pytest and select a dataset scope:

```powershell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope valid -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope invalid -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope valid-invalid -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope review -s
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope all -s
```

`-s` disables pytest's output capture so per-document progress and metrics are
visible while the benchmark runs; it does not change selection or scoring.
`OCR_TEST_SCOPE` provides the same selection for CI. `valid-invalid` excludes
manual-review cases. `all` includes them, but review cases never enter the
binary classification denominator. Classification has only `valid` and
`invalid` outcomes.

Classification and field extraction are reported separately. Field accuracy
only includes populated reference cells; blank cells, `-`, and null values
neither reward nor penalize an extraction. Coverage reports how many possible
cells had an annotated reference. Supplier-name comparison also ignores periods,
commas, legal forms, and longer commercial-name suffixes. Other identifiers
retain punctuation.

`Tipo` is stored as `diagnostic_type` on both sides of the report. It is useful
for filtering and diagnosis but is never scored. Reports are saved after every
document below `tests/results/v2/runs`, with a self-contained `dashboard.html`,
`run.json`, `results.csv`, and `fields.csv`. Set `OCR_V2_REPORT_DIR` to override
that directory.

The production `Invoice` model exposes the same concepts the benchmark scores:
binary validity, informative diagnostic type, all IVA lines, RE lines, optional
IRPF, and the document total. Extraction is attempted for valid and invalid
documents alike. Invalid classification never short-circuits field extraction.
The application persists fiscal rows in normalized child tables; migration
`0003_invoice_tax_lines` preserves an existing flat IVA row
as line zero. Historical rows have no invented validity and must be reprocessed
before they can be read through the strict domain model.

Images are discovered recursively below that directory. Keep the CSV's image
basenames unchanged when moving files into `easy/`, `medium/`, `hard/` or
`special_cases/`. The first folder below the image root is the category; additional
category names also work. Files directly in the root are `uncategorized`. Duplicate
basenames are rejected so the benchmark cannot silently score the wrong image.
The final console summary prints accuracy for each category followed by GLOBAL.

## Level-two results

The same invoice test command now saves a local dashboard and CSV exports:

```shell
python -m pytest tests/test_invoice_accuracy_v2.py --ocr-scope all -s
```

Open `tests/results/v2/runs/<run-id>/dashboard.html` in your browser while tests are
running. It requires no web server or external assets. The report includes:

- Every invoice image, with rotation and zoom controls; HEIC files are saved as
  portable JPEG previews and the originals are unchanged.
- Expected and obtained values, field matches, per-image accuracy, elapsed time
  and extraction errors. Search by filename or filter to issues/pending images.
- The execution summary, accuracy by field, and a selector for earlier runs.
- Accuracy by category and overall accuracy, including scored invoice/field counts.
  Overall accuracy divides all correct fields by all scored fields, rather than
  averaging category percentages. Browser manual verdicts update this breakdown too.
- Automatic refresh every ten seconds, which can be paused while inspecting.

The dashboard shows classification, extraction accuracy, per-field metrics and
both expected and obtained diagnostic types. It is a read-only view of the
saved benchmark result; `diagnostic_type` remains informative and unscored.

Results are saved after each image, before its pass/fail assertion. An extraction
error counts as zero; pending images have no score. Partial-run accuracy is clearly
labelled provisional and uses only scored images. `Ctrl+C` saves an interrupted
run. A force-killed process retains its last saved snapshot, which may still say
running; refresh activity alone does not indicate OCR progress.

Each execution gets a unique directory; old results are preserved:

```text
tests/results/v2/
  runs/<execution-id>/
    run.json                 Exact expected/obtained values and execution metadata
    dashboard.html           Self-contained result dashboard
    results.csv              One row per document
    fields.csv               One row per field: expected, obtained and match flag
```

CSV accuracy values are percentages (e.g. `75.0`). CSV exports use UTF-8 with a
BOM for Spanish text, and formula-like text is prefixed with an apostrophe for
spreadsheet safety. `run.json` preserves exact values. When importing CSVs into
Excel, select text columns for identifiers to preserve their leading zeroes.

Use `OCR_V2_REPORT_DIR` in your environment or `.env` to change the report directory.
Reports are ignored by Git in the default directory. Keep the entire results
folder together when copying it, so the images and download links keep working.
On Windows, a browser, spreadsheet application or antivirus may briefly lock an
export file. The writer retries atomic replacement before reporting the error.
To test the scorer and report writer without running OCR:

```shell
python -m pytest tests/test_invoice_scoring_v2.py tests/test_invoice_report_v2.py -q
```

Run the OCR adapter tests separately:

```shell
python -m pytest tests/test_ocr_surya.py tests/test_ocr_qwen.py tests/test_ocr_openai.py tests/test_ocr_abc.py -q
```

These use test doubles and do not measure OCR accuracy. Full pytest discovery
also includes the real-image
benchmark and requires working OCR, an OpenAI API key and accuracy above the
threshold. Actual inference may download models and take time.

The shared Responses API integration follows the
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
[GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini) supports
both the Responses API and structured outputs.
