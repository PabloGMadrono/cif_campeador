"""Preprocessing diagnostics first; opt-in fixed Surya extraction ablation.

python -m tests.preprocessing_benchmark
python -m tests.preprocessing_benchmark --extract
"""

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps, ImageFont

from src.ocr.preprocessing import (PreprocessingConfig, capture_preparation, prepare_document,
                                   transform_points)
from src.ocr.preprocessing_models import file_hash
from tests.invoice_accuracy import load_ground_truths, image_index, resolve_image, score_invoice, INVOICE_FIELDS
from tests.invoice_report import ExecutionReport, atomic_write


TESTS = Path(__file__).resolve().parent
ANNOTATIONS = TESTS / "ground_truths" / "preprocessing.json"


def assess(page, annotation):
    """Geometry proxy plus separately recorded visual verdicts; neither is OCR."""
    width, height = page["original_dimensions"]
    boundary = np.float32(annotation["polygon_normalized"]) * [width - 1, height - 1]
    selected = page["selected_polygon"] or [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    a, b = cv2.convexHull(np.float32(boundary)), cv2.convexHull(np.float32(selected))
    intersection, _ = cv2.intersectConvexConvex(a, b)
    union = cv2.contourArea(a) + cv2.contourArea(b) - intersection
    # Transform the annotated top-of-text direction, including perspective/skew.
    center = boundary.mean(axis=0)
    vectors = {0: [0, -1], 90: [1, 0], 180: [0, 1], 270: [-1, 0]}
    end = center + np.array(vectors[annotation["upright_rotation_ccw"]]) * 50
    mapped = transform_points([center, end], page["matrix"])
    delta = mapped[1] - mapped[0]
    upright_error = float(abs(np.degrees(np.arctan2(delta[0], -delta[1]))))
    points = np.array(annotation.get("text_extent_normalized", annotation["polygon_normalized"])) * [width - 1, height - 1]
    mapped = transform_points(points, page["matrix"])
    ow, oh = page["output_dimensions"]
    contained = bool(((mapped >= [-2, -2]) & (mapped <= [ow + 1, oh + 1])).all())
    return {"boundary_iou": float(intersection / union), "text_extent_inside": contained,
            "upright_error_degrees": upright_error, "wrong_rotation": upright_error > 45,
            "crop_failure": intersection / union < .8 or not contained,
            "round_trip_error_pixels": float(np.max(abs(transform_points(mapped, page["inverse_matrix"]) - points)))}


def contact_sheets(records, directory):
    """Portable side-by-side original/crop previews, three photos per sheet."""
    for start in range(0, len(records), 3):
        sheet = Image.new("RGB", (1800, 900), "#dce0e5")
        draw = ImageDraw.Draw(sheet)
        for column, record in enumerate(records[start:start + 3]):
            x = column * 600
            draw.text((x + 10, 5), record["filename"] + "  original / prepared", fill="black")
            for row, key in enumerate(("original_path", "prepared_path")):
                with Image.open(record[key]) as image:
                    thumb = ImageOps.contain(image, (580, 410))
                    sheet.paste(thumb, (x + (600 - thumb.width) // 2, 30 + row * 440))
                    thumb.close()
            draw.text((x + 10, 875), f"{record['page']['selection_method']} | {record['page']['rotation_ccw']} CCW", fill="black")
        sheet.save(directory / f"contact-{start // 3 + 1}.jpg", quality=93)
        sheet.close()


def diagnose(image_directory, directory, config):
    annotations = json.loads(ANNOTATIONS.read_text()) if ANNOTATIONS.exists() else {"images": {}}
    rows = load_ground_truths(TESTS / "ground_truths/ground_truth_trial_invoices.csv")
    index = image_index(image_directory)
    records = []
    directory.mkdir(parents=True, exist_ok=True)
    report = ExecutionReport(rows, image_directory, directory, f"Preprocessing / {config.mode}")
    report.data["status"] = "ready"
    report.data["experiment"] = {"kind": "preprocessing_diagnostic", "mode": config.mode}
    for filename, _ in rows:
        source = resolve_image(image_directory, filename, index)
        before = file_hash(source)
        prepared = prepare_document(source, config)
        page = prepared.pages[0]
        annotation = annotations["images"].get(filename)
        if annotation and annotation.get("source_sha256") not in (None, before):
            raise ValueError(f"Annotations do not match the current source: {filename}")
        record = {"filename": filename, "original_path": str(page.original_path),
                  "prepared_path": str(page.image_path), "page": page.metadata,
                  "duration_seconds": prepared.duration_seconds,
                  "processing_seconds": prepared.processing_seconds, "cache_hit": prepared.cache_hit,
                  "source_preserved": file_hash(source) == before,
                  "assessment": assess(page.metadata, annotation) if annotation else None}
        records.append(record)
        report.records[filename]["preprocessing"] = report.save_preparation(filename, prepared)
        print(f"{filename}: {page.metadata['selection_method']}, {page.metadata['rotation_ccw']} CCW, "
              f"{prepared.duration_seconds:.2f}s {page.metadata['uncertainty']}", flush=True)
    assessed = [r["assessment"] for r in records if r["assessment"]]
    summary = {"configuration": config.mode, "images": len(records), "annotated": len(assessed),
               "isolation_or_crop_failures": sum(r["crop_failure"] for r in assessed) if assessed else None,
               "wrong_rotations": sum(r["wrong_rotation"] for r in assessed) if assessed else None,
               "fallbacks": sum(r["page"]["selection_method"] in {"opencv", "full_page"} for r in records) if config.mode == "full" else 0,
               "low_confidence": sum(bool(r["page"]["uncertainty"]) for r in records),
               "mean_processing_seconds": float(np.mean([r["processing_seconds"] for r in records])),
               "mean_elapsed_seconds": float(np.mean([r["duration_seconds"] for r in records])),
               "cache_hits": sum(r["cache_hit"] for r in records),
               "visual_review_required": True}
    atomic_write(directory / "diagnostics.json", json.dumps({"summary": summary, "records": records}, indent=2))
    contact_sheets(records, directory)
    report.data["preprocessing_summary"] = summary
    report.save()
    print(json.dumps(summary, indent=2), flush=True)
    return records


def synthetic_diagnostics(directory, config):
    """Exercise the real pinned models on known rotations and projective warps."""
    directory.mkdir(parents=True, exist_ok=True)
    from src.ocr.preprocessing import lossless_pdf, rotate_upright

    image = Image.new("RGB", (480, 900), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    lines = ["FACTURA / INVOICE 12345", "Proveedor Ejemplo S.L.", "NIF B12345678", "Fecha 15/09/2026"]
    lines += [f"Articulo {i:02d}       1       10,00" for i in range(1, 13)]
    lines += ["Base imponible       120,00", "IVA 21%                25,20", "TOTAL EUR             145,20", "Gracias por su visita"]
    for i, text in enumerate(lines):
        draw.text((25, 30 + i * 40), text, font=font, fill="black")
    source = np.float32([[0, 0], [479, 0], [479, 899], [0, 899]])
    results = []
    for perspective in (False, True):
        polygon = np.float32([[190, 40], [650, 100], [700, 1050], [100, 1000]] if perspective else [[180, 80], [659, 80], [659, 979], [180, 979]])
        matrix = cv2.getPerspectiveTransform(source, polygon)
        photo = cv2.warpPerspective(np.array(image), matrix, (800, 1100), borderValue=(95, 70, 45))
        text_extent = transform_points([[20, 25], [460, 25], [460, 860], [20, 860]], matrix)
        for rotation in (0, 90, 180, 270):
            pixels, rotate_matrix = rotate_upright(photo, rotation)
            h, w = pixels.shape[:2]
            path = directory / f"perspective-{int(perspective)}-rotation-{rotation}.png"
            with Image.fromarray(pixels) as output:
                output.save(path)
            annotation = {"polygon_normalized": (transform_points(polygon, rotate_matrix) / [w - 1, h - 1]).tolist(),
                          "text_extent_normalized": (transform_points(text_extent, rotate_matrix) / [w - 1, h - 1]).tolist(),
                          "upright_rotation_ccw": (360 - rotation) % 360}
            document = prepare_document(path, config)
            results.append({"filename": path.name, "assessment": assess(document.pages[0].metadata, annotation),
                            "preprocessing": document.metadata()})
    with Image.new("RGB", (480, 900), "white") as blank:
        (directory / "image-and-blank.pdf").write_bytes(lossless_pdf([image, blank], dpi=300))
    image.close()
    atomic_write(directory / "synthetic.json", json.dumps(results, indent=2))
    print(f"Synthetic cases: {len(results)}; wrong rotations: {sum(r['assessment']['wrong_rotation'] for r in results)}; "
          f"isolation/crop failures: {sum(r['assessment']['crop_failure'] for r in results)}", flush=True)


def extract_ablation(image_directory, directory, modes):
    from src.ocr.ocr_surya import Ocr_surya
    extractor = Ocr_surya()
    rows = load_ground_truths(TESTS / "ground_truths/ground_truth_trial_invoices.csv")
    comparison = []
    for mode in modes:
        with patch.dict(os.environ, OCR_PREPROCESSING=mode):
            with ExecutionReport(rows, image_directory, directory, f"Surya / {mode}") as report:
                report.data["experiment"] = {"preprocessing": mode, "reference_sha256": file_hash(TESTS / "ground_truths/ground_truth_trial_invoices.csv"),
                                             "parser_settings": "repository default; unchanged across modes"}
                for filename, expected in rows:
                    report.start(filename)
                    started = perf_counter()
                    result = score = error = evidence = None
                    with capture_preparation() as captured:
                        try:
                            path = image_directory / report.records[filename]["source_path"]
                            extraction = extractor.extract_invoice_with_evidence(str(path))
                            result, evidence = extraction.invoice, extraction.model_dump(mode="json")
                            score = score_invoice(result, expected)
                        except Exception as exception:
                            error = f"{type(exception).__name__}: {exception}"
                    report.record(filename, result, score, error, perf_counter() - started, evidence=evidence,
                                  prepared=captured[0] if captured else None)
                    print(f"{mode} {filename}: {score.matched if score else 0}/8 {error or ''}", flush=True)
                invoices = report.data["invoices"]
                comparison.append({"mode": mode, "run_id": report.run_id,
                                   "accuracy_pct": 100 * sum(r["matched"] for r in invoices) / (8 * len(rows)),
                                   "per_field_accuracy_pct": {f.name: 100 * sum(f.name not in r["mismatches"] for r in invoices) / len(rows) for f in INVOICE_FIELDS}})
                atomic_write(directory / "ablation.json", json.dumps(comparison, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, default=TESTS / "images/trial_invoices")
    parser.add_argument("--output", type=Path, default=TESTS / "results/preprocessing")
    parser.add_argument("--modes", nargs="+", choices=("off", "orientation", "full"), default=["off", "orientation", "full"])
    parser.add_argument("--extract", action="store_true", help="Run real Surya and the configured parser for all modes")
    parser.add_argument("--synthetic", action="store_true", help="Also benchmark known rotations/perspective using the real local models")
    args = parser.parse_args()
    for mode in args.modes:
        diagnose(args.images, args.output / mode, replace(PreprocessingConfig.from_env(), mode=mode))
    if args.synthetic:
        synthetic_diagnostics(args.output / "synthetic", replace(PreprocessingConfig.from_env(), mode="full"))
    if args.extract:
        extract_ablation(args.images, args.output, args.modes)


if __name__ == "__main__":
    main()
