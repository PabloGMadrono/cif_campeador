# OCR backends

All invoice extraction paths share Spanish invoice/receipt terminology for
invoice numbers, supplier tax IDs (CIF/NIF/DNI/NIE/VAT) and legal names, including
company suffixes and individual suppliers. Supplier tax IDs take priority over
commercial names; ambiguous operation/reference numbers need document context.
The glossary also guides Qwen and OpenAI text transcription without changing
printed labels or omitting other text. Surya uses it in the shared invoice parser;
its local recognition predictor does not accept this prompt.

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

PDFs are submitted as base64 PDF documents in one call. Images are converted
to PNG with camera orientation applied and transparency composited onto white;
HEIC/HEIF and multipage TIFFs are supported. Each image frame gets a separate
request. `extract_text` returns page Markdown joined with blank lines, retaining
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

For invoice annotations, PDFs are sent intact and a single image is sent as PNG.
Multipage images are packaged into one PDF in frame order (144 DPI, JPEG quality
95), so annotation sees the whole invoice in one request. This conversion uses
lossy image compression. Text extraction retains its separate per-frame calls.

An annotation with all eight fields null returns an empty Invoice. Missing,
malformed or schema-invalid annotations raise `RuntimeError`; there is no parser
fallback. Empty OCR Markdown alone is not treated as an empty invoice. Plain
text extraction rejects missing pages. File, API and SDK response-validation
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
HEIC/HEIF and multipage TIFFs are supported. PDFs are rendered locally at 144 DPI.
All pages are sent in order in the same request as `input_image` items, using
`image_url="data:image/png;base64,..."` and `detail="high"`. No file upload or
public image URL is required. Encoded pages are held in memory for the request;
the complete document must fit the model's image, request and context limits.
The implementation follows the official guides for
[base64 image inputs](https://developers.openai.com/api/docs/guides/images-vision)
and [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

`extract_text(path)` remains available for callers that explicitly want a
transcription; it makes its own Responses call using the same model and low
reasoning, and returns plain text.
`parse_invoice(raw_text)` retains the inherited text-only parser. These methods
are not invoked by direct invoice extraction. Both image and text paths reuse
the base class's lazy OpenAI client and its 120-second timeout/default retries.

Unreadable fields are null. Refusals, incomplete responses, missing parsed output
and schema violations raise `RuntimeError`; file and API errors propagate.
Document images are sent to OpenAI and calls are billable. The offline tests
validate request construction and parsing, not live OCR accuracy.

To use this backend for the application and accuracy benchmark, set
`invoice_extractor = Ocr_openai()` in `src/ocr/__init__.py`, where it is already
imported. The existing Qwen selection is preserved.

```shell
python -m unittest tests.test_ocr_openai -v
```

## Qwen2.5-VL via OpenRouter

`Ocr_qwen` implements `Ocr_operator.extract_text(path)` using the OpenAI Python
client pointed at OpenRouter. It follows the vision API approach in the
[Qwen2.5-VL walkthrough](https://medium.com/@tententgc/extracting-invoice-data-with-qwen2-5-vl-and-openrouter-an-ocr-walkthrough-in-python-7b5490578cad).
To fit the shared interface, it requests JSON containing a `text` transcription
and returns that field as plain text. Invoice structuring remains in the base
class, using the same schema and parser as Surya.

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
PDFs are rendered locally at 144 DPI with PDFium. Each page is sent as a PNG
data URL in a separate OpenRouter request, and page texts are joined with blank
lines. This backend needs no local model weights or llama.cpp server.

Text extraction sends document images to OpenRouter and its model provider.
`extract_invoice` additionally sends the resulting text to the shared OpenAI
parser. These are billable API calls. Each OCR request has a 120-second timeout
and the SDK's default retries; there is no whole-document timeout. OCR responses
are limited to 8192 output tokens per page. Truncation, refusals and malformed
JSON raise `RuntimeError`; decoding and API errors propagate. An explicit empty
transcription is valid and uses the base class's empty-invoice behavior.

To select Qwen for the application and accuracy benchmark, change
`src/ocr/__init__.py` to import `Ocr_qwen` from `.ocr_qwen` and assign
`invoice_extractor = Ocr_qwen()`. Qwen is the current selection.

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

To OCR the included trial invoice and print it directly in PowerShell, run
this from the repository root:

```powershell
python -m src.ocr.ocr_surya "tests/images/trial_invoices/IMG_3309.HEIC"
```

## Invoice accuracy tests

Run the black-box invoice suite from the repository root:

```shell
python -m unittest tests.test_invoice_accuracy -v
```

It reads every row of `tests/ground_truths/ground_truth_trial_invoices.csv`
and passes the corresponding image from `tests/images/trial_invoices` to
`extract_invoice(path)`. Implementations must return the shared `Invoice`
Pydantic dataclass in `src/ocr/models.py`, with the eight CSV fields excluding
`Nombre foto` and `Notas`. Python field names carry the original CSV headers
as dataclass metadata. Supply every field using strings for values, or `None`
for absent fields. `Invoice.empty()` creates an invoice with all fields null.
Pydantic rejects incorrect types, missing fields and extra fields.

Each OCR implementation implements `extract_text(path)`. The base class supplies
two concrete methods; direct vision backends can override `extract_invoice`:

- `extract_invoice(path) -> Invoice`: runs OCR and passes its raw text to
  `parse_invoice`.
- `parse_invoice(raw_text) -> Invoice`: calls the OpenAI Responses API using
  `gpt-5-mini` and `responses.parse(text_format=Invoice)`. The SDK generates
  the strict schema, and Pydantic validates and parses the response directly
  into the shared Invoice dataclass.

Set `OPENAI_API_KEY` in your environment or project `.env` before running the
invoice benchmark. `src/config.py` loads the project `.env` once and exposes
API keys and the llama.cpp runtime selection for the OCR modules to import. Existing
environment variables take precedence; restart the process after changing
configuration. The OpenAI client is a cached property, created on first use
and reused without a constructor or a manual initialization check. Plain
text OCR with Surya does not require an API key. Only raw OCR text is sent to OpenAI, with
response storage disabled; the CSV reference data stays in the tests.

Empty OCR text returns an empty Invoice without an API call. Unknown fields are
null. API errors propagate; refusals, incomplete responses and invalid payloads
raise errors instead of returning partial invoices. The benchmark scores failed
executions as zero and continues to the next image. The invoice benchmark runs
real OCR and makes billable API requests; it does not mock predictions.

The suite prints correct fields / 8 and accuracy for every image, followed
by global accuracy (correct fields / all fields). While extraction is running,
it prints the image name at the start and elapsed time every 30 seconds. These
updates indicate a pending call, not guaranteed inference progress. There is no
hard timeout for an entire image; backend request timeouts and retries can make
an image take longer than any single request timeout. Both each image and the
global score must be **strictly greater than 70%**; an image needs at least
6/8 correct fields. Failed executions and invalid return models score 0/8
and subsequent images still run. Mismatches show expected and actual values.
Run without unittest's `-f` (fail fast) or `-b` (buffer output) options to see
the complete report.

Comparison ignores surrounding/repeated whitespace and letter case, accepts
DD/MM/YYYY and ISO dates, and compares numeric values exactly with Decimal.
Spanish amounts such as `1.117,04 €` equal `1117.04`; `21,00%` equals `21`.
Identifiers retain leading zeroes, punctuation and accents. Empty reference
cells are scored as expected absence, not omitted. Reference amounts are
used as recorded, without recalculating tax or totals.

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

Images are discovered recursively below that directory. Keep the CSV's image
basenames unchanged when moving files into `easy/`, `medium/`, `hard/` or
`special_cases/`. The first folder below the image root is the category; additional
category names also work. Files directly in the root are `uncategorized`. Duplicate
basenames are rejected so the benchmark cannot silently score the wrong image.
The final console summary prints accuracy for each category followed by GLOBAL.

## Results dashboard and execution history

The same invoice test command now saves a local dashboard and CSV exports:

```shell
python -m unittest tests.test_invoice_accuracy -v
```

Open `tests/results/dashboard.html` in your browser, including while tests are
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

Select an execution and click **Rename run** to give it a memorable name. Save
with the button or Enter; Cancel/Escape discards the edit. A blank name restores
the default date label. Refresh pauses while the editor is open. Names persist
in browser local storage across reloads, browser restarts and report regeneration
at the same location. They appear in the selector and execution history. They
are browser-specific, are not included in the JSON/CSV files, and are lost if
browser storage is cleared. If browser storage is unavailable, the editor shows
an error and retains the unsaved text.

In **Field comparison**, use the **Verdict** dropdown to mark a scored field
**Correct** or **Wrong** when automated comparison misclassifies it. Choose
**Auto** to restore the original verdict. The invoice accuracy, global accuracy,
field breakdown, filters and execution history immediately use your corrections.
Manual verdicts and the original automated accuracy are labeled in the dashboard.
Pending invoices and extraction errors without an obtained result cannot be
manually scored.

Like run names, corrections persist in browser local storage for this dashboard
location, including after report regeneration. They do not edit expected/obtained
values, ground truths, saved JSON/CSV files or unittest outcomes. CSV downloads
continue to contain automated scores. Clearing browser storage removes manual
verdicts. A storage failure leaves the previous score intact and shows an error.

To verify the dashboard's manual scoring and persistence without OCR (requires
Node.js):

```shell
node --test tests/test_invoice_dashboard.cjs
```

Results are saved after each image, before its pass/fail assertion. An extraction
error counts as zero; pending images have no score. Partial-run accuracy is clearly
labelled provisional and uses only scored images. `Ctrl+C` saves an interrupted
run. A force-killed process retains its last saved snapshot, which may still say
running; refresh activity alone does not indicate OCR progress.

Each execution gets a unique directory; old results are preserved:

```text
tests/results/
  dashboard.html             All executions, with embedded report data
  executions.csv             One row per execution, including accuracy and status
  categories.csv             Per-category accuracy for every execution
  runs/<execution-id>/
    run.json                 Exact expected/obtained values and execution metadata
    results.csv              One row per image: status, accuracy, timing and errors
    fields.csv               One row per field: expected, obtained and match flag
    categories.csv           Category accuracy and scored/total invoice counts
    images/                  JPEG snapshots for that execution
```

CSV accuracy values are percentages (e.g. `75.0`). CSV exports use UTF-8 with a
BOM for Spanish text, and formula-like text is prefixed with an apostrophe for
spreadsheet safety. `run.json` preserves exact values. When importing CSVs into
Excel, select text columns for identifiers to preserve their leading zeroes.

Use `OCR_REPORT_DIR` in your environment or `.env` to change the report directory.
Reports are ignored by Git in the default directory. Keep the entire results
folder together when copying it, so the images and download links keep working.
On Windows, a browser, spreadsheet application or antivirus may briefly lock an
export file. The writer retries file replacement. If a dashboard or CSV remains
locked, it logs a warning and continues the test; `run.json` is saved first and
the export is retried on the next update. An inability to save the authoritative
JSON still raises an error rather than silently losing results.

After closing a program that holds an export open, regenerate the dashboard and
all CSVs from the saved JSON without rerunning OCR or making API requests:

```shell
python -m tests.invoice_report --refresh
```

Refresh also assigns categories to older saved runs that predate category
tracking, using the current image folders (or `OCR_IMAGE_DIR`). The dashboard
labels this assignment; predictions, scores and execution timestamps are retained.
New runs snapshot their category at execution time, so moving images later does
not change their history. Missing legacy categories display as `uncategorized`.

Results from executions before this feature cannot be recovered from partial
console output; new executions populate the history automatically.

To generate an initial dashboard containing images and ground truths without
calling OCR or any external API:

```shell
python -m tests.invoice_report
```

The initial preview has no obtained values or accuracy and is excluded from
execution history. To test the report writer itself without running OCR:

```shell
python -m unittest tests.test_invoice_report -v
```

Run fast unit tests of the scorer and the existing Surya adapter separately:

```shell
python -m unittest tests.test_invoice_scoring tests.test_ocr_surya tests.test_ocr_qwen tests.test_ocr_openai tests.test_ocr_abc -v
```

These use test doubles and do not measure OCR accuracy. Full discovery
(`python -m unittest discover -s tests -v`) also includes the real-image
benchmark and requires working OCR, an OpenAI API key and accuracy above the
threshold. Actual inference may download models and take time.

The shared Responses API integration follows the
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
[GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini) supports
both the Responses API and structured outputs.
