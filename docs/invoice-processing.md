# Invoice processing

[Start here](../README.md) · [Architecture](architecture.md) · [Data model](data-model.md) · [Development](development.md)

## Intake and identity

The webhook queues image events and PDF documents, ignoring text, other document
formats, and malformed attachment events. The downloader supports JPEG, PNG,
WebP, and PDF. Other image MIME types may be queued but fail during download.
Direct OCR also supports HEIC/HEIF and multipage TIFF.

The submission ID is derived from the WhatsApp message ID. Customer resolution
first reuses the sender's phone mapping. For a new phone, it reuses a customer
only when the supplied Meta user ID maps to exactly one existing customer;
otherwise it creates a provisional customer from the profile name or phone.
Supplier details extracted from the invoice do not identify the submitting
customer.

## Preparation and extraction

[src/ocr/__init__.py](../src/ocr/__init__.py) selects one shared
`invoice_extractor` for both workers and benchmarks. Every backend returns the
strict [Invoice model](../src/ocr/models.py).

| Backend | Extraction path | Requirements |
| --- | --- | --- |
| OpenAI (selected) | Prepared pages → one structured invoice request | `OPENAI_API_KEY` |
| Mistral | Document OCR with invoice annotation in one request | `MISTRAL_API_KEY` |
| Qwen via OpenRouter | Page images → transcribed text → OpenAI invoice parser | `OPENROUTER_API_KEY`, `OPENAI_API_KEY` |
| Surya | Local OCR blocks → OpenAI parser with source citations | Local llama.cpp runtime/model weights, `OPENAI_API_KEY` |

Surya validates citations against readable OCR blocks. Evidence field statuses
are `printed`, `missing`, and `unreadable`; citation checks do not guarantee that
the recognized text or extracted value is correct. Its explicit
`extract_invoice_with_evidence()` API exposes evidence; the worker stores only
the resulting invoice.

Shared [preprocessing](../src/ocr/preprocessing.py) decodes pages, applies EXIF
orientation, and normalizes transparency onto white. PDFs render at 300 DPI by
default. Prepared pages are cached by source content, configuration, and model
versions; uploads are never modified.

| `OCR_PREPROCESSING` | Additional behavior |
| --- | --- |
| `off` | No document crop, text-orientation correction, or deskew. Code default. |
| `orientation` | Correct right-angle text orientation. |
| `full` | Detect/crop/rectify the document, correct orientation, and apply supported small deskew. |

`orientation` and `full` require explicit local model setup. `.env.example` sets
`full`, overriding the code default. Crop/orientation selection can be uncertain;
compare prepared images and extraction results before adopting it for a dataset.
Mistral sends PDFs intact only in `off` mode; otherwise it uses prepared pages.

## Classification and field rules

[prompts.py](../src/ocr/prompts.py) owns the shared business instructions:

- Accept invoices, simplified invoices, fiscal receipts, and corrective invoices.
- Reject provisional documents, standalone card-payment slips, non-invoices, and
  documents too unreadable to recognize. Additional rules reject a clearly absent
  invoice number and certain simplified receipts with insufficient fiscal data.
- Identify the supplier, not the buyer. Preserve printed identifiers; use null
  instead of guessing missing or unreadable fields.
- Keep printed IVA rows, RE rows, and signed IRPF withholding separate. OCR does
  not calculate tax values. Invalid classification still returns readable fields.

These rules guide model classification; they are not a separate deterministic
validator applied after extraction. Refusals, malformed output, and provider/file
errors raise failures rather than silently returning a successful partial result.
An empty usable text result returns an invalid `Unreadable` invoice.

## Accounting before persistence

[reconcile_invoice()](../src/invoices/accounting.py) operates independently of
validity. It preserves populated OCR values and can fill missing VAT fields from
the other two fields, derive a missing total, or complete one incomplete VAT row
from a known total and rate when the remaining fiscal values are known.

Checks require at least one complete VAT row and a usable total. Each VAT amount
must equal base × rate / 100 rounded to cents. The invoice total must equal the
sum of VAT bases and VAT amounts, plus RE amounts and the signed IRPF amount.
RE and IRPF amounts participate in the total; their rates are not independently
validated or filled. Results use the four [fiscal statuses](data-model.md#invoice-decisions).
Derived fields can remain present even when the final result needs review.

## Progress, retries, and failures

```text
received → downloading → downloaded → ocr_processing → completed
                ↓                          ↓
              failed                     failed
```

Each stage gets three attempts by default, with retry delays of 1 then 5 seconds.
During retries, SQL remains at the stage's in-progress status. Exhaustion sets
`failed`, records `failure_stage` and `last_error`, and dead-letters the job.
Malformed queue messages go directly to dead-letter storage and may have no
associated SQL submission.

Workers reclaim idle unacknowledged jobs after configurable timeouts. Redelivery
reuses committed downloads and completed invoices. `completed` and `failed`
submissions are terminal in normal worker processing: enqueueing a failed ID
alone does not reset or reprocess it. There is no replay command or review UI.
