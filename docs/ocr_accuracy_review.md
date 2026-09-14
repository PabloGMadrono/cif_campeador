# OCR accuracy review — 14 September 2026

The strongest opportunities are to repair the evaluation contract, isolate the target document, and preserve readable image detail. The available evidence does not justify starting with another OCR model.

## Scope and evidence

Reviewed all four adapters, the shared parser and Invoice model, benchmark/scoring/report code, the 18 original HEIC photographs, overview sheets, enlarged views of representative failures, and six available run JSON files. Original photographs are 4032×3024 or 3024×4032. Nine have sideways document text despite EXIF orientation 1. Source images, ground truths, and application code were not changed. No new OCR/API benchmark was run.

The current scorer was used to independently verify saved predictions. Diagnostic scripts and image inspection copies are in the ignored `tests/results/` directory: `analyze_saved.py`, `score_sensitivity.py`, `verify_cached.py`, `inspect_originals.py`, and `inspection/`.

The score measures exact agreement on eight extracted fields, including expected absence. It is not character-level OCR accuracy. A failure can originate in recognition, selecting the wrong document or party, structuring text, normalization, annotation policy, or an API error.

| Saved complete execution | Correct fields | Accuracy | Extraction errors |
|---|---:|---:|---:|
| Surya, 11 September 15:26 UTC | 91/144 | 63.19% | 0 |
| OpenAI, 11 September 14:18 UTC | 77/144 | 53.47% | 0 |
| OpenAI, 11 September 14:51 UTC | 91/144 | 63.19% | 0 |
| OpenAI, 11 September 15:16 UTC | 89/144 | 61.81% | 0 |
| Mistral, 14 September 15:11 UTC | 70/144 | 48.61% | 2 |

No saved Qwen run was found. The 67.5% Surya entry in executions.csv is interrupted after five images and its run JSON is absent; it is not comparable with full runs. Model IDs, prompts, reasoning effort, source revision and preprocessing settings are not captured in run metadata, so these historical results cannot be attributed confidently to today's exact code. The current selector uses Surya although the README says Qwen.

For the complete Surya run and latest saved complete OpenAI run:

| Field | Surya | OpenAI |
|---|---:|---:|
| Date | 18/18 | 17/18 |
| Invoice number | 7/18 | 7/18 |
| Supplier tax ID | 10/18 | 8/18 |
| Supplier name | 6/18 | 5/18 |
| Taxable base | 9/18 | 10/18 |
| VAT rate | 13/18 | 14/18 |
| VAT amount | 10/18 | 11/18 |
| Total | 18/18 | 17/18 |

Supplier identity and invoice number account for 31 of Surya's 53 failed fields and 34 of OpenAI's 55. Tax-base and VAT failures include unprinted or inconsistent references, so the table does not establish that recognition is the cause.

## 1. Repair ground truths and define the extraction contract

**Highest priority for trustworthy measurement.** Several correct or defensible extractions are penalized. Some references require information the shared instructions explicitly prohibit inventing or calculating.

| Image | What the image and code establish | Consequence |
|---|---|---|
| IMG_3310 | Heading says `011-0009-R923744`; CSV says `011-0009-R23744`. Supplier footer prints `C.I.F. B-84406289`; CSV supplier NIF is blank. | Surya is penalized for correctly reading both. OpenAI/Mistral's `B88044656` is a separate customer/supplier selection error; do not accept it as supplier CIF. |
| IMG_3311 | Supplier block includes both the Media Markt location and the longer company name. Invoice number is printed with spaces around its hyphen. | Define legal name versus display name consistently, and distinguish formatting from identifier errors. The current legal-name preference conflicts with the short reference name. |
| IMG_3312 | Reference `000377` appears in the card-payment section as `Op 000377`. A separate `Recibo` identifier is printed near the top. | The reference conflicts with the prompt's exclusion of payment operation numbers. Audit the actual receipt identifier at full resolution; keep payment and invoice identifiers separate. |
| IMG_3319 | VAT rows are 10%: base 8.80, VAT 0.88; 21%: base 9.08, VAT 1.91. CSV demands 15.60%. | 15.60% is approximately an effective rate, not a printed rate. Current prompt explicitly requires null when several rates occur. Preserve two tax rows; label any aggregate as derived. |
| IMG_3320 | `#:04047`, total 62.30, and `IVA 10% INCLÒS`; no printed base/VAT split. CSV expects `4047`, 56.07 and 6.23. | Preserve the leading zero. Separate extraction from calculation. Under a single 10% inclusive-tax assumption, division by 1.10 gives 56.64 base and 5.66 VAT, not the reference values. |
| IMG_3321 | Document says `PROFORMA`, shows `Oper. Caja Ticket` with `00003 00001 31962`, total 112.00 and 10% included; no printed base/VAT split. | Classify proforma and define whether number means ticket only or the three-column composite. References 100.80/11.20 apply 10% to the gross total; an optional derived split would be 101.82/10.18. |
| IMG_3322 | Severely faded receipt; the CSV note admits the invoice number cannot be read from the image. Base/VAT split is not printed. | Mark unreadable fields explicitly. A model should not be rewarded for guessing hidden reference values. |
| IMG_3323 | Printed tax base is 14.82, VAT 1.48; a separate 0.70 donation brings the payment total to 17.00. CSV base is 15.52. | The reference folds the donation into the taxable base. Surya and OpenAI both return the printed 14.82 and are marked wrong. |
| IMG_3324 | Receipt says VAT included but does not print a rate or split. `dto. -10%` is a discount. | The reference 10% rate and base/VAT split are not directly extractable. Do not mistake the discount for VAT. |
| IMG_3325 | Prints `SPAI NATURAL`, `ERICA ZAMORA ALARCO`, and VAT included, but no VAT rate/split. | Resolve commercial versus individual supplier name consistently. The current individual/legal-name instructions support the person's name. Three tax references are unsupported by printed values. |
| IMG_3326 | BBVA card-payment slip, total 212.00, `Op 014124`; no printed VAT rate/split or invoice number. | Do not force this into an invoice schema with invented VAT and an operation number relabeled as an invoice number. |

At least 16 tax-field reference cells need printed-versus-derived policy review: base and VAT amount on 3320/3321/3322; base, rate and VAT amount on 3324/3325/3326; and the aggregate rate on 3319. This is 11.1% of the entire benchmark, before identifier and supplier-name issues. This count is an annotation-review scope, not a measured accuracy gain.

Keep two clearly defined outputs if business users need both: printed extraction with evidence, and optional derived/accounting values with formulas and assumptions. For absent versus unreadable fields, use separate annotation states even if the public Invoice still returns null. Evaluate abstention correctly instead of forcing all fields to contain values.

Also introduce a separate semantic score alongside exact transcription accuracy. In an offline sensitivity check, ignoring periods/commas/spacing in supplier names, separators in tax IDs, and spaces around invoice-number slashes/hyphens recovers seven fields in each of the two runs above:

* Surya: 91/144 → 98/144, **63.19% → 68.06%**.
* Latest saved OpenAI: 89/144 → 96/144, **61.81% → 66.67%**.

This changes interpretation of existing predictions; it does not improve recognition. Production normalization should be narrower and documented, particularly for name collisions and country prefixes. Preserve original values, actual digits, leading zeroes, and meaningful identifier punctuation. Do not broadly fuzzy-match tax IDs or invoice numbers.

## 2. Isolate the intended document before extraction

**Highest expected impact on actual extraction.** IMG_3312–3316 contain receipts over other documents. IMG_3323 also includes a neighboring receipt. A detector that merely finds the largest page would select the wrong document in several cases.

Mistral on IMG_3312 returns `CINEPLAN S.L.`, `B88044656`, base 1122.04, VAT 235.63, and total 1357.67. Those values are visible in the background Media Markt invoice/customer block. The foreground parking receipt total is 4.95. IMG_3313 also gets background-derived amounts. This is concrete document contamination, not simply poor character recognition.

Add a shared preprocessing stage that locates separate document regions, chooses the intended foreground receipt, and masks or crops away other documents. Preserve enough border and the entire receipt footer. Where the intended target is ambiguous, request a selected region or process each document separately. Do not combine fields from different regions.

For future capture, one flattened receipt on a plain opaque background with diffuse light is likely more valuable than another model swap. Use a capture quality check for shadows, clipped corners and blur. IMG_3322's fading may require a better source or manual review; image processing cannot guarantee recovery of missing ink.

Start with manually defined target crops as an experiment to measure the opportunity before building an automatic detector. Keep these evaluation crops separate from production inference and do not pass reference field values to a model.

## 3. Correct orientation and preserve small text

All 18 originals have about 12.2 million pixels. IMG_3309–3317 remain sideways after HEIC decode and EXIF transpose. OpenAI, Qwen and Mistral normalization handles EXIF and alpha composition but has no content-based orientation, target segmentation or deskew step. The installed Surya image loader simply opens and converts to RGB; later recognizer behavior should be measured independently.

Use content-based 0/90/180/270 orientation first, modest deskew/perspective correction second. Rotation should not be inferred from landscape dimensions alone in production. Keep the original image as a comparison view when contrast/shadow correction is tried. Aggressive thresholding can erase faint strokes or amplify text showing through the paper.

The direct OpenAI adapter hardcodes `detail="high"`. Current official documentation lists a 2048-pixel dimension limit and 2,500-patch budget for this setting on GPT-5.6 Luna. A 4032-pixel photo therefore loses linear detail even though the client sends a full-size PNG. Test `detail="original"` within its model limits and cost budget, preferably on the isolated document. Also test a receipt overview plus labeled, overlapping native-resolution header/footer/tax-region crops. Explicitly describe them as views of the same document, not extra pages, to avoid duplicate content. [Official image-input guidance](https://developers.openai.com/api/docs/guides/images-vision).

The documentation explicitly lists small text and rotation as limitations. Classical OCR documentation also recommends evaluating rescaling, deskewing and border handling, but the best preprocessing must be measured on each backend rather than assumed universal. [Tesseract image-quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html).

PDF rendering at 144 DPI is worth testing at higher resolution for future PDFs; it does not explain the current HEIC scores. PNG conversion is not itself a lossy-compression bottleneck. Increasing output-token limits is also not supported as a primary fix by these saved errors.

## 4. Extract evidence and preserve document structure

Surya and Qwen feed plain text into the same GPT-5-mini parser. They are not independent end-to-end approaches. Surya sorts blocks and flattens HTML, losing region geometry; skipped blocks are silently omitted. Qwen transcribes all visible text, including text from other documents, without coordinates. OpenAI extracts directly; Mistral directly annotates and discards its returned Markdown from the invoice result.

Introduce an internal evidence model, while retaining a compatibility mapping to the public eight-field Invoice:

* Document type and selected document region.
* Separate supplier and customer entities, with printed legal and commercial names.
* Separate invoice, ticket, order, transaction and payment-operation identifiers.
* Tax rows with rate/base/amount and separate donation, discount or non-tax amounts.
* For each value: verbatim text, page/region, normalized value and printed/derived/unreadable status.

Then use small targeted rereads for missing or conflicting identifiers and tax rows. For example, the 3313 footer contains the actual supplier name and tax ID; 3319's long document reference is at the bottom, away from the payment identifiers. Require selected values to be supported by the selected document's region.

Use amount consistency and identifier-format/checksum checks to trigger a reread or review, not to silently overwrite evidence. A simple base + VAT = total invariant is insufficient: 3323 includes a donation; 3311 has a separate third-party amount. Validate only after capturing these components.

## 5. Add selective fallback and reliability handling

Mistral's two errors are 429 capacity failures on 3314 and 3323, each scored 0/8. Its success-only field accuracy is 70/128 = 54.69%, versus 48.61% end-to-end. Use bounded exponential backoff with jitter and resumable failed-image execution; preserve end-to-end and success-only metrics separately. Recovery of all 16 lost fields would be an 11.11-point theoretical maximum, not an expected gain.

Route ambiguous or unsupported fields to another recognizer only after preprocessing and evidence checks. Prefer a targeted crop reread over rerunning every model on every image.

To assess whether a simple ensemble can solve today's ceiling, I computed an oracle union: a field counts as correct if any selected saved backend got it correct, using the reference to choose the winner. Latest OpenAI + complete Surya reaches only **101/144 = 70.14%**. Adding complete Mistral reaches **103/144 = 71.53%**. A deployable selector cannot assume this oracle knowledge. These bounds apply only to those cached outputs and the current labels; they demonstrate heavily shared errors, not a universal limit on future systems.

## 6. Compare models and fine-tune only after controlled experiments

A model change, higher reasoning effort, or few-shot examples may help semantic selection, but cannot repair an unprinted reference or guarantee isolation of background text. Prompt terminology is already extensive; adding more synonyms should rank below fixing the target region and field definitions.

There are only 18 images and repeated suppliers (Areas, Decuatro, Can Rigo), so this is a diagnostic set, not enough evidence for a reliable model ranking or fine-tuning program. Acquire more independently captured receipts and hold out suppliers/templates, not just random photos. Persist exact model/provider versions, prompt hashes, code revision, preprocessing configuration, source/label hashes, usage/cost, raw OCR, regions and intermediate extraction. Browser-only manual verdicts are not durable ground-truth corrections and do not change CSV or unittest results.

## Recommended experiment sequence

1. Version corrected labels and field semantics; retain original exact scores and add semantic/abstention metrics. Persist reproducible run metadata.
2. Freeze one backend and compare original images, content-upright images, then upright isolated target crops. Manually define crops for the diagnostic experiment to establish whether an automatic cropper is worthwhile.
3. With the same regions and prompt, compare existing detail with original-resolution input and selected header/footer crops. Report per-field improvements/regressions and latency/cost.
4. Add document typing, tax-row evidence and targeted rereads. Compare recognition text with final fields to distinguish OCR and parser failures. A small manually transcribed text subset can test parser behavior without visual errors.
5. Add bounded retry/fallback and evaluate correctness versus automatic-acceptance coverage. Review unresolved fields instead of filling them by guesswork.
6. Only then compare model/reasoning configurations on held-out suppliers and real capture conditions.

The recommended first implementation touches the annotation/scoring contract and a shared image-preparation module used by all adapters. No accuracy increase from these proposed extraction changes has yet been measured.
