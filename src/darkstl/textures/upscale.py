"""Optional AI texture upscaling and deblocking through ONNX Runtime.

Community models (ESRGAN and similar, see openmodeldb.info) are image to image
networks: RGB in 0..1, RGB out at 1x (deblocking) or 2x/4x (upscaling). Dark
STL runs them with ONNX Runtime, which uses the GPU on Windows through
DirectML when ``onnxruntime-directml`` is installed and the CPU otherwise.

The Windows app ships normal-map upscalers in ``engine/models`` (see
desktop/models: RunDevelopment's 4x-Normal-RG0 models, CC BY 4.0), next to
the engine rather than inside it. When no model is picked, the one trained for
the texture's block compression is used. Users can add their own ``.onnx``
files to the models folder (Settings shows where).

Normal maps go through a model trained for them in "RG0" form (red and green
kept, blue zeroed). The output is not unit length, so blue is rebuilt and the
vector renormalised afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np


class UpscaleError(RuntimeError):
    pass


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def list_models(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() == ".onnx")


def bundled_dir() -> Path | None:
    """The folder of the models that come with the app: ``models`` beside the frozen engine, or
    DARKSTL_BUNDLED_MODELS (for source installs, wherever desktop/models/bundle.py put them)."""
    if os.environ.get("DARKSTL_BUNDLED_MODELS"):
        folder = Path(os.environ["DARKSTL_BUNDLED_MODELS"])
    elif getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent / "models"
    else:
        return None
    return folder if (folder / "models.json").is_file() else None


@lru_cache(maxsize=4)
def _bundled_spec(folder: str) -> list[dict]:
    try:
        spec = json.loads((Path(folder) / "models.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [m for m in spec.get("models", []) if (Path(folder) / f"{m['name']}.onnx").is_file()]


def bundled_models() -> list[dict]:
    """The bundled models present: [{"name", "file", "codecs", "description", ...}]."""
    folder = bundled_dir()
    if folder is None:
        return []
    return [{**m, "file": str(folder / f"{m['name']}.onnx")} for m in _bundled_spec(str(folder))]


def automatic_model(codec: str | None) -> Path | None:
    """The bundled upscaler trained for textures stored with ``codec`` ("BC1", "BC7"...): the one
    listing it, else the one marked as the fallback (``codecs`` containing "*")."""
    models = bundled_models()
    codec = (codec or "").upper()
    for m in models:
        if codec and codec in m.get("codecs", []):
            return Path(m["file"])
    for m in models:
        if "*" in m.get("codecs", []):
            return Path(m["file"])
    return Path(models[0]["file"]) if models else None


@dataclass
class Model:
    path: Path
    session: object
    input_name: str
    scale: int


@lru_cache(maxsize=4)
def load(path: str) -> Model:
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise UpscaleError("AI upscaling needs the 'onnxruntime' package (onnxruntime-directml on Windows)") from e
    if not Path(path).is_file():
        raise UpscaleError(f"Model file not found: {path}")
    preferred = ["DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    providers = [p for p in preferred if p in ort.get_available_providers()] or ["CPUExecutionProvider"]
    sess = ort.InferenceSession(path, providers=providers)
    inp = sess.get_inputs()[0]
    probe = np.zeros((1, 3, 16, 16), np.float32)
    if inp.type == "tensor(float16)":
        probe = probe.astype(np.float16)
    out = sess.run(None, {inp.name: probe})[0]
    scale = max(int(round(out.shape[-1] / 16)), 1)
    return Model(Path(path), sess, inp.name, scale)


def run(model: Model, rgb: np.ndarray, tile: int = 256, overlap: int = 16) -> np.ndarray:
    """Run the model over an (H, W, 3) float image in 0..1, tile by tile, and return the result."""
    h, w = rgb.shape[:2]
    s = model.scale
    out = np.zeros((h * s, w * s, 3), np.float32)
    weight = np.zeros((h * s, w * s, 1), np.float32)
    half = model.session.get_inputs()[0].type == "tensor(float16)"
    step = max(tile - 2 * overlap, 16)
    for y in range(0, h, step):
        for x in range(0, w, step):
            y0, x0 = max(y - overlap, 0), max(x - overlap, 0)
            y1, x1 = min(y + step + overlap, h), min(x + step + overlap, w)
            patch = np.ascontiguousarray(rgb[y0:y1, x0:x1].transpose(2, 0, 1)[None])
            res = model.session.run(None, {model.input_name: patch.astype(np.float16 if half else np.float32)})[0]
            res = np.asarray(res[0], np.float32).transpose(1, 2, 0)
            # feathered blend so tile borders do not show
            ph, pw = res.shape[:2]
            ry = np.minimum(np.arange(ph) + 1, np.arange(ph)[::-1] + 1).astype(np.float32)
            rx = np.minimum(np.arange(pw) + 1, np.arange(pw)[::-1] + 1).astype(np.float32)
            feather = np.minimum(np.minimum.outer(ry, rx), overlap * s + 1)[..., None]
            out[y0 * s:y0 * s + ph, x0 * s:x0 * s + pw] += res[..., :3] * feather
            weight[y0 * s:y0 * s + ph, x0 * s:x0 * s + pw] += feather
    return np.clip(out / np.maximum(weight, 1e-6), 0.0, 1.0)


def normal_map(model: Model, rgba: np.ndarray, rg_only: bool = True) -> np.ndarray:
    """Run a normal map through a model; returns uint8 RGBA with a rebuilt, unit-length blue.

    ``rg_only`` zeroes blue on the way in, as "RG0" normal-map models expect;
    general models (deblockers) get the full vector.
    """
    f = rgba[..., :3].astype(np.float32) / (255.0 if rgba.dtype == np.uint8 else 1.0)
    f = f.copy()
    if rg_only:
        f[..., 2] = 0.0
    else:
        nx, ny = f[..., 0] * 2 - 1, f[..., 1] * 2 - 1
        f[..., 2] = np.sqrt(np.clip(1 - nx * nx - ny * ny, 0, 1)) * 0.5 + 0.5
    res = run(model, f)
    nx, ny = res[..., 0] * 2 - 1, res[..., 1] * 2 - 1
    length = np.sqrt(nx * nx + ny * ny)
    over = length > 0.999
    nx = np.where(over, nx / np.maximum(length, 1e-6) * 0.999, nx)
    ny = np.where(over, ny / np.maximum(length, 1e-6) * 0.999, ny)
    nz = np.sqrt(np.clip(1 - nx * nx - ny * ny, 0, 1))
    out = np.empty(res.shape[:2] + (4,), np.uint8)
    out[..., 0] = np.rint((nx * 0.5 + 0.5) * 255)
    out[..., 1] = np.rint((ny * 0.5 + 0.5) * 255)
    out[..., 2] = np.rint((nz * 0.5 + 0.5) * 255)
    out[..., 3] = 255
    return out


def color(model: Model, rgba: np.ndarray) -> np.ndarray:
    """Run a colour texture (albedo, masks) through a model; returns uint8 RGBA."""
    f = rgba[..., :3].astype(np.float32) / (255.0 if rgba.dtype == np.uint8 else 1.0)
    res = run(model, f)
    out = np.empty(res.shape[:2] + (4,), np.uint8)
    out[..., :3] = np.rint(res * 255)
    out[..., 3] = 255
    return out
