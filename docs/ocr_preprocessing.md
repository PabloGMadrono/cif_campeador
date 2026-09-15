# Shared local OCR preprocessing

All four backends call `prepare_document(path)` once per extraction. Prepared
pages are lossless PNGs shared through a content/configuration/model cache.
Original uploads are never modified. The selected OCR backend remains an
independent choice in `src/ocr/__init__.py`.

## Setup and modes

From the repository root, using the same Python environment as the application:

```shell
python -m pip install -r requirements.txt
python -m src.ocr.preprocessing --setup-models
```

Model downloads occur only during this explicit setup step. Runtime inference
uses ONNX Runtime's CPU provider and checks the pinned weight hashes. Missing or
invalid weights cause a setup error; they do not silently switch the detector.
The manifest in `src/ocr/preprocessing_models.py` pins DocAligner's 83 MB heatmap
`fastvit_sa24` and Paddle's 6.8 MB `PP-LCNet_x1_0_doc_ori` ONNX export.

Set `OCR_PREPROCESSING` in `.env` or the process environment:

| Mode | Behavior |
| --- | --- |
| `off` (default) | Shared decoding/EXIF/white transparency normalization; no crop, text rotation or deskew. |
| `orientation` | Decode and classify the full page; apply the highest-scoring right-angle correction. |
| `full` | Detect/crop/rectify, classify orientation, and apply consistent small text-line deskew. |

For an OpenAI comparison using the currently selected backend in PowerShell:

```powershell
$env:OCR_PREPROCESSING = "full"
python -m unittest tests.test_invoice_accuracy -v
```

Use `orientation` for the intermediate comparison and `off` for the baseline.
An existing process environment value overrides `.env`. Restart the benchmark
process after editing `.env`. Changing mode invalidates the preparation cache.
The dashboard's Original/Prepared toggle only changes the preview; it cannot
enable processing or change an already saved run.

Optional settings: `OCR_PDF_DPI` (default 300), `OCR_BOUNDARY_ALLOWANCE` (0.02 of
the smaller rectified dimension), `OCR_PREPROCESSING_MODELS`, and
`OCR_PREPROCESSING_CACHE`. By default models/cache live in the ignored
`.ocr_preprocessing` directory. `PreprocessingConfig` also exposes analysis
resolution, area validation, confidence flags, deskew and thread settings.

## Processing and coordinates

Images support JPEG/PNG, HEIC/HEIF and multipage TIFF. PDFium renders each PDF
page at the configured DPI, respecting existing PDF rotation and page order.
PDFium access is serialized because its library is not thread-safe. EXIF is
applied once and removed from the prepared PNG. Near-uniform blank pages remain
unchanged; the blank test is deliberately strict so faint text is processed.

DocAligner receives a full-frame BGR analysis image, resized without preliminary
center cropping. Finite, in-bounds, convex quadrilaterals must pass area and
non-degeneracy checks. OpenCV fallback candidates combine centrality, edge
support, enclosure and paper/background contrast; printed tables, logos and QR
boundaries are rejected where the paper-boundary checks identify them. If no
candidate passes, the full page continues to orientation correction.

The best valid candidate and highest-scoring orientation are applied even when
uncertain. Heatmap strength and orientation scores are diagnostics, not a
probability that the intended foreground receipt was selected. The rectangle
uses measured edge lengths; it has no fixed paper aspect ratio. All geometric
transforms are applied to the full-resolution pixels. Small deskew requires at
least four consistent character-center line fits; borders alone cannot trigger
it. No sharpening, output thresholding or reconstructed content is introduced.

Each page saves original/output dimensions, selected polygon, source hash,
model revisions/hashes, rotation, skew, selection method and uncertainty flags.
`matrix` maps EXIF-normalized original pixels (or the rotated PDF render) to
prepared pixels; `inverse_matrix` maps back. `raw_to_prepared` additionally
includes the saved EXIF transform for encoded raster coordinates. Coordinates
refer to pixel centers. Surya's block polygons stay in prepared coordinates;
its `OcrPage.preprocessing` stores this mapping alongside the evidence.

Cache identity includes source content, suffix/decoder, configuration, pipeline
version, model hashes and codec versions. Page hashes detect missing/corrupt
cache files. A per-key lock and metadata-last publication keep incomplete
entries out of subsequent reads. Cached originals are normalized pixels, not
copies of the uploaded file. Full-page cache entries and models are local;
normal OCR API calls still go to the selected backend where applicable.

OpenAI and Qwen encode prepared PNGs. Surya receives prepared Pillow images.
Mistral receives one image or a document-wide raster PDF with lossless Flate RGB
streams for multiple pages. Its native PDF passthrough is retained only in
`off` mode. All backends share the 300 DPI decoding baseline; this differs from
older 144 DPI OpenAI/Qwen runs and Surya's previous loader defaults.

## Diagnostics and rollout

```shell
python -m pip install -r requirements-dev.txt
python -m unittest tests.test_preprocessing tests.test_ocr_surya tests.test_ocr_openai tests.test_ocr_qwen tests.test_ocr_mistral tests.test_invoice_report -v
node --test tests/test_invoice_dashboard.cjs
python -m tests.preprocessing_benchmark --synthetic
```

The diagnostic command evaluates off/orientation/full without OCR APIs. It
saves per-mode `diagnostics.json`, contact sheets and a dashboard with page and
Original/Prepared controls under `tests/results/preprocessing`. Synthetic tests
exercise the real models on known 0/90/180/270 rotations and perspective warps.
Offline tests independently cover text/image/blank/rotated/multipage PDFs,
HEIC, TIFF, EXIF, transparency, transform round-trips, cache invalidation and
backend input equivalence. Install development requirements for the text-PDF
fixture generator.

`tests/ground_truths/preprocessing.json` contains independently estimated visual
boundaries and upright directions for the 18 source photos. Boundaries are
approximate; a conservative text envelope and boundary IoU are diagnostic
proxies. An isolation/crop failure means IoU below 0.8 or an envelope extending
outside the output. Full-page baselines can fail isolation while preserving all
text. These metrics do not replace visual checks of reference-bearing text.
Invoice reference labels in the existing CSV are unchanged.

Initial full-mode review corrected the upright direction on all 18 photos,
but found seven isolation/crop failures. Seven used a fallback (three OpenCV,
four full-page), and nine carried uncertainty flags. Mean cold preparation was
about 4.1 seconds per image in the validation environment, including decoding
and saving full-resolution previews; cached preparation was about 0.03 seconds.
Orientation-only got 17/18 directions right (3323 was turned upside down).
These are preprocessing measurements, not extraction accuracy measurements.

The full-mode crops on 3309, 3311 and 3313 visibly cut text at an edge/footer.
3312, 3314 and 3315 retain the complete photo after fallback, including background
documents. 3316 includes neighboring paper. The other individual receipt crops
retain their visible text in the contact-sheet review, including 3319's long
footer and faint 3322. This fails the no-clipped-reference-text rollout gate:
**keep the default off**. Do not infer improved field accuracy from rotations
alone. Detailed measurements and previews remain in the diagnostic output.

The requested Surya extraction comparison is available separately:

```shell
python -m tests.preprocessing_benchmark --extract
```

It uses the same Surya instance/parser settings across all three modes and
unchanged reference labels, saving per-field scores, timing, evidence and
preprocessing previews after every image. This invokes the real parser and can
be slow on CPU. No completed three-mode extraction accuracy result is claimed
by the initial implementation; the independent crop validation has not passed.

The OpenAI run `20260915T093806-5d73cb684a` used `off` on all 18 photos:
all original/prepared hashes match and every rotation is zero. Its score is
90/144 fields (62.5%), so it is a baseline. Re-running with `full` is necessary
to measure the effect of document isolation for that backend.

## Model sources and attribution

- [DocAligner heatmap inference source, pinned revision](https://github.com/DocsaidLab/DocAligner/blob/3275b0f07f8e99d8c01cb0774dea2549be1416b6/docaligner/heatmap_reg/infer.py)
- [DocAligner advanced usage](https://docsaid.org/en/docs/docaligner/advance/)
- [Paddle orientation model and pinned preprocessing configuration](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx/tree/7330ab7039123e46af2dc03154b9969aa412c61d)
- [Paddle orientation module documentation](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/doc_img_orientation_classification.en.md)

The lightweight heatmap adapter adapts DocsaidLab's Apache-2.0 inference
algorithm using NumPy/OpenCV in place of Capybara; it is not the packaged
DocAligner Python wrapper. Orientation follows PaddlePaddle's exported model
configuration and counterclockwise correction convention. The Apache license
is included in `docs/licenses/Apache-2.0.txt`.
