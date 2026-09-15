"""Shared local document preparation; image coordinates use pixel centers.

The primary coordinate frame is the EXIF-normalized image / rotated PDF render.
matrix and inverse_matrix map that frame to/from prepared pixels. raw_to_prepared
also includes EXIF, allowing callers to map back to the original encoded raster.
"""

import argparse
import base64
import hashlib
import json
import os
import tempfile
import time
from threading import RLock
from importlib.metadata import version
import zlib
from contextlib import ExitStack, contextmanager, closing
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from filelock import FileLock
from PIL import Image, ImageOps, ImageSequence

from src import config as _app_config  # Load .env before reading settings.
from .preprocessing_models import LocalModels, file_hash, model_versions, setup_models


ROOT = Path(__file__).resolve().parents[2] / ".ocr_preprocessing"
PIPELINE_VERSION = "3"
_capture = ContextVar("prepared_document_capture", default=None)
_pdf_lock = RLock()  # PDFium is not thread-safe, including across documents.


@dataclass(frozen=True)
class PreprocessingConfig:
    mode: str = "off"
    pdf_dpi: int = 300
    analysis_max_side: int = 1200
    boundary_allowance: float = .04
    min_area_ratio: float = .025
    orientation_confidence: float = .8
    deskew: bool = True
    max_skew_degrees: float = 5
    min_skew_degrees: float = 1.25
    threads: int = 2
    cache_dir: str = str(ROOT / "cache")
    model_dir: str = str(ROOT / "models")

    def __post_init__(self):
        if self.mode not in {"off", "orientation", "full"}:
            raise ValueError("OCR_PREPROCESSING must be off, orientation, or full")
        if not 72 <= self.pdf_dpi <= 600 or not 256 <= self.analysis_max_side <= 4096:
            raise ValueError("PDF DPI must be 72..600 and analysis size 256..4096")
        if not 0 <= self.boundary_allowance <= .1 or not 0 < self.min_area_ratio < 1:
            raise ValueError("Invalid boundary allowance or minimum area")
        if not 0 <= self.orientation_confidence <= 1 or not 0 <= self.min_skew_degrees <= self.max_skew_degrees <= 10 or self.threads < 1:
            raise ValueError("Invalid orientation/deskew/thread configuration")

    @classmethod
    def from_env(cls):
        return cls(mode=os.getenv("OCR_PREPROCESSING", "off").strip().lower(),
                   pdf_dpi=int(os.getenv("OCR_PDF_DPI", "300")),
                   boundary_allowance=float(os.getenv("OCR_BOUNDARY_ALLOWANCE", ".04")),
                   cache_dir=os.getenv("OCR_PREPROCESSING_CACHE", str(ROOT / "cache")),
                   model_dir=os.getenv("OCR_PREPROCESSING_MODELS", str(ROOT / "models")))


@dataclass(frozen=True)
class PreparedPage:
    original_path: Path
    image_path: Path
    metadata: dict

    def open_image(self):
        return Image.open(self.image_path)


@dataclass(frozen=True)
class PreparedDocument:
    source_hash: str
    cache_key: str
    pages: tuple[PreparedPage, ...]
    config: dict
    cache_hit: bool
    duration_seconds: float
    processing_seconds: float

    @contextmanager
    def images(self):
        with ExitStack() as stack:
            yield [stack.enter_context(page.open_image()) for page in self.pages]

    def metadata(self):
        return {"source_hash": self.source_hash, "cache_key": self.cache_key,
                "config": self.config, "cache_hit": self.cache_hit,
                "duration_seconds": self.duration_seconds, "processing_seconds": self.processing_seconds,
                "pages": [p.metadata for p in self.pages]}


@contextmanager
def capture_preparation():
    """Observe the backend's one preparation call, including before OCR failures.

    Context-local, so concurrent extractions cannot overwrite each other's report.
    """
    captured = []
    token = _capture.set(captured)
    try:
        yield captured
    finally:
        _capture.reset(token)


def transform_points(points, matrix):
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    homogeneous = np.c_[points, np.ones(len(points))] @ np.asarray(matrix).T
    return homogeneous[:, :2] / homogeneous[:, 2:]


def exif_matrix(orientation, width, height):
    return np.array({
        2: [[-1, 0, width - 1], [0, 1, 0], [0, 0, 1]],
        3: [[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]],
        4: [[1, 0, 0], [0, -1, height - 1], [0, 0, 1]],
        5: [[0, 1, 0], [1, 0, 0], [0, 0, 1]],
        6: [[0, -1, height - 1], [1, 0, 0], [0, 0, 1]],
        7: [[0, -1, height - 1], [-1, 0, width - 1], [0, 0, 1]],
        8: [[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]],
    }.get(orientation, np.eye(3)), dtype=float)


def decode_pages(path, pdf_dpi=300):
    """Yield owned RGB arrays and decoding metadata; all codec handles are closed."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Expected a document file: {path}")
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with _pdf_lock, pdfium.PdfDocument(str(path)) as document:
            for number in range(len(document)):
                with closing(document[number]) as page:
                    rotation = page.get_rotation()
                    with closing(page.render(scale=pdf_dpi / 72)) as bitmap, bitmap.to_pil() as image:
                        with image.convert("RGB") as rgb:
                            array = np.array(rgb)
                        yield array, {"raw_dimensions": list(image.size), "exif_orientation": 1,
                                      "exif_matrix": np.eye(3).tolist(), "pdf_rotation": rotation,
                                      "pdf_dpi": pdf_dpi}
        return
    if path.suffix.lower() in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener
        register_heif_opener(thumbnails=False)
    with Image.open(path) as document:
        for frame in ImageSequence.Iterator(document):
            raw_size, orientation = frame.size, frame.getexif().get(274, 1)
            with ImageOps.exif_transpose(frame) as oriented, oriented.convert("RGBA") as rgba:
                with Image.new("RGB", rgba.size, "white") as rgb, rgba.getchannel("A") as alpha:
                    rgb.paste(rgba, mask=alpha)
                    yield np.array(rgb), {"raw_dimensions": list(raw_size), "exif_orientation": orientation,
                                          "exif_matrix": exif_matrix(orientation, *raw_size).tolist()}


def analysis_image(rgb, max_side):
    h, w = rgb.shape[:2]
    scale = min(1, max_side / max(h, w))
    return cv2.resize(rgb, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)


def valid_quad(points, width, height, min_area=.025):
    """Return ordered TL/TR/BR/BL corners or None; never repair a degenerate quad."""
    p = np.asarray(points, dtype=np.float32)
    if p.shape != (4, 2) or not np.isfinite(p).all():
        return None
    if (p < -2).any() or (p[:, 0] > width + 1).any() or (p[:, 1] > height + 1).any():
        return None
    hull = cv2.convexHull(p).reshape(-1, 2)
    if len(hull) != 4 or cv2.contourArea(hull) < width * height * min_area:
        return None
    # Clockwise in image coordinates, starting near the top-left.
    angles = np.arctan2(hull[:, 1] - hull[:, 1].mean(), hull[:, 0] - hull[:, 0].mean())
    p = hull[np.argsort(angles)]
    p = np.roll(p, -int(np.argmin(p.sum(axis=1))), axis=0)
    if np.min(np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1)) < 5:
        return None
    unit = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(p, unit)
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        return None
    return p


def boundary_candidates(rgb, config):
    """Fallback score combines centrality, boundary evidence, and enclosure."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    smooth = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(smooth, 30, 100)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    _, bright = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates = []
    for mask in (closed, bright):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            if cv2.contourArea(contour) < h * w * config.min_area_ratio:
                continue
            for epsilon in (.015, .03, .05):
                approx = cv2.approxPolyDP(contour, epsilon * cv2.arcLength(contour, True), True).reshape(-1, 2)
                quad = valid_quad(approx, w, h, config.min_area_ratio)
                if quad is not None:
                    if not any(np.mean(np.linalg.norm(quad - q["polygon"], axis=1)) < 10 for q in candidates):
                        candidates.append({"polygon": quad})
                    break
    edge_support = cv2.dilate(edges, np.ones((7, 7), np.uint8))
    usable = []
    for candidate in candidates:
        p = candidate["polygon"]
        interior = np.zeros_like(gray)
        cv2.fillConvexPoly(interior, p.astype(np.int32), 255)
        values = gray[interior > 0]
        # Logos/QRs and ruled tables are not paper boundaries. Sample both
        # sides of each edge: foreground paper should be lighter inside.
        contrasts = []
        center_pixels = p.mean(axis=0)
        for start, end in zip(p, np.roll(p, -1, axis=0)):
            samples = start + np.linspace(.15, .85, 30)[:, None] * (end - start)
            direction = center_pixels - samples
            direction /= np.maximum(1, np.linalg.norm(direction, axis=1))[:, None]
            step = max(4, min(w, h) * .012)
            inside = np.rint(samples + direction * step).astype(int)
            outside = np.rint(samples - direction * step).astype(int)
            valid = ((outside >= [0, 0]) & (outside < [w, h])).all(axis=1)
            inside = np.clip(inside, [0, 0], [w - 1, h - 1])
            if valid.sum() >= 10:
                i, o = inside[valid], outside[valid]
                contrasts.append(float(np.median(gray[i[:, 1], i[:, 0]].astype(float) - gray[o[:, 1], o[:, 0]])))
        paper_contrast = float(np.median(contrasts)) if contrasts else 0.
        if np.median(values) < np.percentile(gray, 65) - 25 or np.mean(values < 100) > .3 or paper_contrast < 3:
            continue
        center = p.mean(axis=0) / [w, h]
        centrality = 1 - min(1., np.linalg.norm(center - .5) / .707)
        border = np.zeros_like(gray)
        cv2.polylines(border, [p.astype(np.int32)], True, 255, 2)
        support = float(np.mean(edge_support[border > 0] > 0))
        touching = float(np.mean((p[:, 0] < 3) | (p[:, 0] > w - 4) | (p[:, 1] < 3) | (p[:, 1] > h - 4)))
        area = cv2.contourArea(p) / (w * h)
        enclosed = sum(all(cv2.pointPolygonTest(p, tuple(map(float, xy)), False) > 0 for xy in other["polygon"])
                       for other in candidates if other is not candidate)
        candidate.update(score=float(.45 * centrality + .4 * support + .15 * min(area / .2, 1)
                                     - .35 * touching - .15 * min(enclosed, 2)),
                         area_ratio=area, edge_support=support, border_touching=touching,
                         paper_contrast=paper_contrast)
        usable.append(candidate)
    return sorted(usable, key=lambda c: c["score"], reverse=True)


def rectify(rgb, polygon, allowance):
    # Expand in the source frame. Adding a white target margin does not recover
    # ink just outside the detector polygon, which is precisely where totals
    # and footer identifiers often sit.
    h, w = rgb.shape[:2]
    center = polygon.mean(axis=0)
    expanded = center + (polygon - center) * (1 + 2 * allowance)
    expanded[:, 0] = np.clip(expanded[:, 0], 0, w - 1)
    expanded[:, 1] = np.clip(expanded[:, 1], 0, h - 1)
    tl, tr, br, bl = expanded
    width = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))) + 1
    height = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))) + 1
    target = np.array([[0, 0], [width - 1, 0],
                       [width - 1, height - 1], [0, height - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(np.float32(expanded), target)
    result = cv2.warpPerspective(rgb, matrix, (width, height),
                                 flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    return result, matrix, expanded


def rotate_upright(rgb, degrees):
    """Model labels specify the counterclockwise correction, as in PaddleX."""
    height, width = rgb.shape[:2]
    matrices = {0: np.eye(3), 90: [[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]],
                180: [[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]],
                270: [[0, -1, height - 1], [1, 0, 0], [0, 0, 1]]}
    return np.ascontiguousarray(np.rot90(rgb, degrees // 90)), np.array(matrices[degrees], float)


def estimate_skew(rgb, max_degrees, min_degrees=1.25):
    """Fit character centers within lines; borders alone cannot trigger deskew."""
    small = analysis_image(rgb, 1600)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 15)
    count, labels, stats, centers = cv2.connectedComponentsWithStats(binary)
    chars = [i for i in range(1, count) if 3 <= stats[i, 3] <= gray.shape[0] * .06
             and 1 <= stats[i, 2] <= 3 * stats[i, 3] and stats[i, 4] >= 5]
    if len(chars) < 20:
        return 0., 0
    median_height = np.median(stats[chars, 3])
    mask = np.isin(labels, chars).astype(np.uint8)
    joined = cv2.dilate(mask, np.ones((3, max(3, int(median_height * 2))), np.uint8))
    _, lines = cv2.connectedComponents(joined)
    groups = {}
    for i in chars:
        x, y = centers[i].astype(int)
        groups.setdefault(int(lines[y, x]), []).append(centers[i])
    angles = []
    for key, points in groups.items():
        p = np.array(points)
        if key == 0 or len(p) < 6 or np.ptp(p[:, 0]) < median_height * 6:
            continue
        slope, intercept = np.polyfit(p[:, 0], p[:, 1], 1)
        angle = float(np.degrees(np.arctan(slope)))
        residual = np.median(abs(p[:, 1] - slope * p[:, 0] - intercept))
        if abs(angle) <= max_degrees and residual < median_height * .2:
            angles.append(angle)
    if len(angles) < 4:
        return 0., len(angles)
    median = float(np.median(angles))
    consistent = np.mean(np.abs(np.array(angles) - median) < .7) >= .8
    return (median if consistent and abs(median) >= min_degrees else 0.), len(angles)


def deskew_image(rgb, angle):
    h, w = rgb.shape[:2]
    matrix = np.vstack([cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), angle, 1), [0, 0, 1]])
    corners = transform_points([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], matrix)
    low, high = np.floor(corners.min(axis=0)), np.ceil(corners.max(axis=0))
    matrix[:2, 2] -= low
    size = tuple((high - low + 1).astype(int))
    return cv2.warpPerspective(rgb, matrix, size, flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255)), matrix


def prepare_page(rgb, config, models):
    started = time.perf_counter()
    h, w = rgb.shape[:2]
    matrix = np.eye(3)
    output = rgb
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blank = bool(np.ptp(gray) <= 3 and gray.std() < 1)
    polygon, warp_polygon, quality, warnings = None, None, {}, []
    selection = "blank" if blank else "full_page"
    rotation, scores, skew = 0, None, 0.
    if config.mode == "full" and not blank:
        small = analysis_image(rgb, config.analysis_max_side)
        points, quality = models.detect(small)
        sh, sw = small.shape[:2]
        quad = valid_quad(points, sw, sh, config.min_area_ratio)
        selection = "docaligner"
        if quad is None:
            warnings.append("detector_no_valid_quad")
            candidates = boundary_candidates(small, config)
            quality["fallback_candidates"] = len(candidates)
            if candidates:
                best = candidates[0]
                quad = best["polygon"]
                quality.update({k: v for k, v in best.items() if k != "polygon"})
                quality["candidate_gap"] = best["score"] - candidates[1]["score"] if len(candidates) > 1 else None
                selection = "opencv"
                warnings.append("fallback_selection")
            else:
                selection = "full_page"
                warnings.append("no_document_boundary")
        elif quality.get("corner_peak_min", 1) < .5:
            warnings.append("weak_corner_heatmap")
        if quad is not None:
            quad *= [w / sw, h / sh]
            polygon = quad.tolist()
            output, matrix, warp_polygon = rectify(rgb, quad, config.boundary_allowance)
            quality["area_ratio"] = cv2.contourArea(quad) / (w * h)
            # Geometric validity is not semantic foreground certainty.
            quality["foreground_verified"] = False
    if config.mode != "off" and not blank:
        rotation, scores = models.orient(output)
        if max(scores) < config.orientation_confidence:
            warnings.append("low_orientation_confidence")
        output, correction = rotate_upright(output, rotation)
        matrix = correction @ matrix
        # Orientation-only is a clean right-angle ablation, without deskew.
        if config.mode == "full" and config.deskew:
            skew, line_count = estimate_skew(output, config.max_skew_degrees, config.min_skew_degrees)
            quality["skew_supporting_lines"] = line_count
            if skew:
                output, correction = deskew_image(output, skew)
                matrix = correction @ matrix
    return output, {"original_dimensions": [w, h], "selected_polygon": polygon,
                    "warp_polygon": warp_polygon.tolist() if polygon is not None else None,
                    "output_dimensions": [output.shape[1], output.shape[0]], "selection_method": selection,
                    "rotation_ccw": rotation, "orientation_scores": scores, "skew_ccw": skew,
                    "matrix": matrix.tolist(), "inverse_matrix": np.linalg.inv(matrix).tolist(),
                    "coordinate_frame": "exif_normalized_pixels_or_rotated_pdf_render",
                    "model_versions": model_versions(config.mode), "quality": quality,
                    "uncertainty": warnings, "blank": blank,
                    "processing_seconds": time.perf_counter() - started}


def prepare_document(path, config=None):
    config = config or PreprocessingConfig.from_env()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Expected a document file: {path}")
    started = time.perf_counter()
    source_hash = file_hash(path)
    settings = asdict(config)
    identity = {k: v for k, v in settings.items() if k not in {"model_dir", "cache_dir"}}
    identity.update(pipeline_version=PIPELINE_VERSION, models=model_versions(config.mode),
                    pillow=Image.__version__, opencv=cv2.__version__,
                    pdfium=version("pypdfium2"), heif=version("pillow-heif"))
    key = hashlib.sha256(json.dumps([source_hash, path.suffix.lower(), identity], sort_keys=True).encode()).hexdigest()
    cache = Path(config.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / key
    hit = False
    with FileLock(str(cache / f"{key}.lock"), timeout=600):
        metadata_path = destination / "metadata.json"
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            hit = bool(data["pages"]) and all(
                file_hash(destination / page[name]) == page[name + "_sha256"]
                for page in data["pages"] for name in ("original", "prepared"))
        except (OSError, ValueError, KeyError, TypeError):
            hit = False
        if not hit:
            models = LocalModels(config.model_dir, config.threads)
            pages = []
            with tempfile.TemporaryDirectory(prefix="preparing-", dir=cache) as staging_name:
                staging = Path(staging_name)
                for number, (rgb, decoding) in enumerate(decode_pages(path, config.pdf_dpi), 1):
                    prepared, page = prepare_page(rgb, config, models)
                    page.update(decoding, page_number=number, original=f"{number:04d}-original.png",
                                prepared=f"{number:04d}-prepared.png")
                    page["raw_to_prepared"] = (np.array(page["matrix"]) @ np.array(page["exif_matrix"])).tolist()
                    for name, pixels in (("original", rgb), ("prepared", prepared)):
                        with Image.fromarray(pixels) as image:
                            image.save(staging / page[name], "PNG")
                        page[name + "_sha256"] = file_hash(staging / page[name])
                    pages.append(page)
                if not pages:
                    raise ValueError("Document contains no pages")
                if file_hash(path) != source_hash:
                    raise RuntimeError("Source document changed during preparation; retry extraction")
                data = {"pages": pages, "processing_seconds": time.perf_counter() - started}
                destination.mkdir(exist_ok=True)
                for file in staging.iterdir():
                    os.replace(file, destination / file.name)
                # Publish metadata last; incomplete cache entries are never used.
                temporary = staging / "metadata.json"
                temporary.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
                os.replace(temporary, metadata_path)
    document = PreparedDocument(source_hash, key,
                                tuple(PreparedPage(destination / p["original"], destination / p["prepared"], p)
                                      for p in data["pages"]), identity, hit,
                                time.perf_counter() - started, data["processing_seconds"])
    if _capture.get() is not None:
        _capture.get().append(document)
    return document


def image_data_url(image):
    """Encode already prepared pixels; no second EXIF transform."""
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def lossless_pdf(images, dpi=300):
    """RGB Flate streams preserve every pixel; PIL's PDF saver uses JPEG."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for image in images:
        with image.convert("RGB") as rgb:
            w, h = rgb.size
            compressed = zlib.compress(rgb.tobytes())
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        image_id, content_id = page_id + 1, page_id + 2
        pw, ph = w * 72 / dpi, h * 72 / dpi
        commands = f"q {pw:.8f} 0 0 {ph:.8f} 0 0 cm /Im0 Do Q".encode()
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.8f} {ph:.8f}] /Resources << /XObject << /Im0 {image_id} 0 R >> >> /Contents {content_id} 0 R >>".encode(),
            f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode() + compressed + b"\nendstream",
            f"<< /Length {len(commands)} >>\nstream\n".encode() + commands + b"\nendstream",
        ])
    if not kids:
        raise ValueError("Cannot create a PDF with no pages")
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    output = BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--setup-models", action="store_true")
    parser.add_argument("--mode", choices=("off", "orientation", "full"))
    args = parser.parse_args()
    config = PreprocessingConfig.from_env()
    if args.mode:
        from dataclasses import replace
        config = replace(config, mode=args.mode)
    if args.setup_models:
        setup_models(config.model_dir)
        print(f"Verified pinned CPU models in {config.model_dir}")
    if args.path:
        document = prepare_document(args.path, config)
        print(json.dumps(document.metadata(), indent=2))
        for page in document.pages:
            print(page.image_path)
    if not args.path and not args.setup_models:
        parser.error("Provide a document path or --setup-models")


if __name__ == "__main__":
    main()
