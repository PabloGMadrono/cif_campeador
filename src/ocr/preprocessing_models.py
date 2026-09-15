"""Small CPU ONNX adapters. Network access exists only in setup_models().

DocAligner heatmap preprocessing/postprocessing adapted from DocsaidLab/DocAligner
at 3275b0f07f8e99d8c01cb0774dea2549be1416b6 (Apache-2.0).
Orientation preprocessing follows PaddlePaddle's pinned inference.yml.
See docs/ocr_preprocessing.md and docs/licenses/Apache-2.0.txt.
"""

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


MODEL_MANIFEST = {
    "docaligner": {
        "name": "DocAligner/heatmap/fastvit_sa24",
        "revision": "3275b0f07f8e99d8c01cb0774dea2549be1416b6",
        "file": "docaligner.onnx",
        "google_drive_id": "14vUH77v6yGg7zFctUgcT6BzV5Iisg4Dl",
        "sha256": "7f9f5a8935b2eb22b3ee0245d34996063f54562df390d34714af2d76928695bc",
    },
    "orientation": {
        "name": "PP-LCNet_x1_0_doc_ori",
        "revision": "7330ab7039123e46af2dc03154b9969aa412c61d",
        "file": "orientation.onnx",
        "url": "https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx/resolve/7330ab7039123e46af2dc03154b9969aa412c61d/inference.onnx",
        "sha256": "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92",
    },
}


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def model_versions(mode):
    keys = () if mode == "off" else (("orientation",) if mode == "orientation" else MODEL_MANIFEST)
    return {key: {k: MODEL_MANIFEST[key][k] for k in ("name", "revision", "sha256")} for key in keys}


@lru_cache(maxsize=4)
def session(name, directory, threads=2):
    import onnxruntime as ort

    spec = MODEL_MANIFEST[name]
    path = Path(directory) / spec["file"]
    if not path.is_file() or file_hash(path) != spec["sha256"]:
        raise RuntimeError(f"Missing or invalid {name} weights in {directory}. "
                           "Run python -m src.ocr.preprocessing --setup-models first.")
    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class LocalModels:
    def __init__(self, directory, threads=2):
        self.directory, self.threads = str(directory), threads

    def detect(self, rgb):
        model = session("docaligner", self.directory, self.threads)
        # DocAligner expects BGR / 255, with a full-frame resize to 256 square.
        tensor = cv2.resize(rgb[:, :, ::-1], (256, 256)).transpose(2, 0, 1)[None].astype(np.float32) / 255
        maps = model.run(["heatmap"], {model.get_inputs()[0].name: tensor})[0][0]
        points, peaks, recovered = [], [], []
        for index, heatmap in enumerate(maps[:4]):
            heatmap = cv2.resize(heatmap, (rgb.shape[1], rgb.shape[0]))
            peak = float(heatmap.max())
            peaks.append(peak)
            # DocAligner's published threshold is 0.3. A page can still have
            # three excellent corners and one weak corner at the image edge.
            # Recover only that corner's dominant component; quad validation
            # remains responsible for rejecting incoherent geometry.
            if peak < .1:
                continue
            threshold = .3 if peak >= .3 else peak * .7
            if threshold < .3:
                recovered.append(index)
            heatmap[heatmap < threshold] = 0
            mask = (heatmap * 255).astype(np.uint8)
            _, mask = cv2.threshold(mask, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            moments = cv2.moments(max(contours, key=cv2.contourArea))
            if moments["m00"]:
                points.append([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]])
        return points, {"corner_peak_min": min(peaks), "corner_peaks": peaks,
                        "recovered_weak_corners": recovered}

    def orient(self, rgb):
        model = session("orientation", self.directory, self.threads)
        height, width = rgb.shape[:2]
        scale = 256 / min(height, width)
        resized = cv2.resize(rgb, (round(width * scale), round(height * scale)))
        y, x = (resized.shape[0] - 224) // 2, (resized.shape[1] - 224) // 2
        tensor = resized[y:y + 224, x:x + 224].astype(np.float32) / 255
        tensor = (tensor - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        scores = model.run(None, {model.get_inputs()[0].name: tensor.transpose(2, 0, 1)[None]})[0].reshape(-1)
        if scores.size != 4 or not np.isfinite(scores).all():
            raise RuntimeError("Orientation model returned invalid scores")
        # The exported graph includes softmax. Do not apply it a second time.
        index = int(np.argmax(scores))
        return index * 90, [float(s) for s in scores]


def setup_models(directory):
    """Explicit, hash-verified downloads; never called by document processing."""
    import urllib.request
    from filelock import FileLock

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / ".setup.lock"), timeout=600):
        for name, spec in MODEL_MANIFEST.items():
            destination = directory / spec["file"]
            if destination.exists() and file_hash(destination) == spec["sha256"]:
                continue
            temporary = destination.with_suffix(".download")
            try:
                if "google_drive_id" in spec:
                    import gdown
                    gdown.download(id=spec["google_drive_id"], output=str(temporary), use_cookies=False)
                else:
                    with urllib.request.urlopen(spec["url"], timeout=120) as source, temporary.open("wb") as target:
                        import shutil
                        shutil.copyfileobj(source, target)
                if file_hash(temporary) != spec["sha256"]:
                    raise RuntimeError(f"Downloaded {name} checksum does not match the pinned manifest")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
