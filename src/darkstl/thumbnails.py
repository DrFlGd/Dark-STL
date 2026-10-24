"""Preview pictures of models for the library's cards.

A small software renderer, so previews need no graphics card or window: the
model is drawn from the 3D view's three-quarter angle, back to front, each
triangle lit from the same lights as the viewer and coloured from its albedo
texture where the model has one. Pictures are kept in a cache folder, keyed by
the file's path, size and date, so each model is drawn once: again only when its
file changes, or when its folder is rescanned (:meth:`ThumbnailCache.forget`).
A model that can't be drawn is remembered too, so it isn't tried on every visit.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path

import numpy as np

from .geometry.mesh import TriMesh

VERSION = 2  # bump to redraw every cached preview (2: textures found elsewhere in the game folder)
VIEW = np.array([0.62, -0.95, 0.75])  # from the model towards the camera, as the viewer's 3/4 view
KEY = np.array([1.0, -1.5, 2.5])
RIM = np.array([-2.0, 2.0, 1.0])
GRAY = (163, 169, 174)  # models with no texture, as the viewer draws them
MAX_TRIANGLES = 400_000


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def face_colours(mesh: TriMesh, textures: dict[int, np.ndarray]) -> np.ndarray:
    """Each triangle's colour: its albedo texture sampled at the triangle's centre, else gray."""
    colours = np.tile(np.array(GRAY, np.float32), (mesh.n_faces, 1))
    if mesh.corner_uvs is None:
        return colours
    centre = mesh.corner_uvs.mean(axis=1)
    for mat, img in textures.items():
        sel = mesh.face_material == mat
        if not sel.any():
            continue
        rgb = img[..., :3]
        if rgb.dtype != np.uint8:
            rgb = np.clip(rgb * 255, 0, 255)
        h, w = rgb.shape[:2]
        uv = centre[sel] % 1.0
        x = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
        y = np.clip((uv[:, 1] * h).astype(int), 0, h - 1)  # UVs here run down the image
        colours[sel] = rgb[y, x].astype(np.float32)
    return colours


def render(mesh: TriMesh, textures: dict[int, np.ndarray] | None = None, size: int = 256,
           supersample: int = 2) -> np.ndarray:
    """An RGBA picture (``size`` square, transparent background) of the mesh."""
    from PIL import Image, ImageDraw

    if mesh.n_faces > MAX_TRIANGLES:  # a map piece the size of a level: every n-th triangle is plenty
        mesh = mesh.subset(np.arange(mesh.n_faces) % int(np.ceil(mesh.n_faces / MAX_TRIANGLES)) == 0)
    s = size * supersample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if mesh.n_faces == 0:
        return np.asarray(img.resize((size, size)))
    back = _unit(VIEW)
    right = _unit(np.cross(-back, [0.0, 0.0, 1.0]))
    up = np.cross(right, -back)
    v = mesh.vertices - 0.5 * (mesh.vertices.min(0) + mesh.vertices.max(0))
    x, y, depth = v @ right, v @ up, v @ back
    x, y = _lay_diagonally(x, y)
    span = max(np.ptp(x), np.ptp(y), 1e-9)
    scale = 0.9 * s / span
    px = (x - 0.5 * (x.min() + x.max())) * scale + s / 2
    py = s / 2 - (y - 0.5 * (y.min() + y.max())) * scale

    normals = mesh.face_normals()
    if mesh.corner_normals is not None:  # the model's own smoothing, kept on the side the face shows
        smooth = mesh.corner_normals.mean(axis=1)
        length = np.linalg.norm(smooth, axis=1, keepdims=True)
        ok = length[:, 0] > 1e-6
        smooth[ok] /= length[ok]
        flip = np.einsum("ij,ij->i", smooth, normals) < 0
        smooth[flip] *= -1
        normals = np.where(ok[:, None], smooth, normals)
    facing = normals @ back
    normals[facing < 0] *= -1  # drawn from both sides, like the viewer
    light = (0.42 + 0.18 * (0.5 + 0.5 * normals[:, 2]) + 0.55 * np.clip(normals @ _unit(KEY), 0, None)
             + 0.2 * np.clip(normals @ _unit(RIM), 0, None))
    colours = 255 * (face_colours(mesh, textures or {}) / 255) ** 0.8  # game albedo is dark; lift it a little
    rgb = np.clip(colours * light[:, None], 0, 255).astype(np.uint8)

    order = np.argsort(depth[mesh.faces].mean(axis=1))  # far first
    draw = ImageDraw.Draw(img)
    tri_x, tri_y = px[mesh.faces], py[mesh.faces]
    for f in order:
        c = tuple(rgb[f]) + (255,)
        pts = [(tri_x[f, 0], tri_y[f, 0]), (tri_x[f, 1], tri_y[f, 1]), (tri_x[f, 2], tri_y[f, 2])]
        draw.polygon(pts, fill=c, outline=c)
    return np.asarray(img.resize((size, size), Image.LANCZOS))


def _lay_diagonally(x: np.ndarray, y: np.ndarray, ratio: float = 2.5) -> tuple[np.ndarray, np.ndarray]:
    """Turn a long thin picture (a sword, a spear) to run corner to corner, so it fills more of the card."""
    pts = np.column_stack([x - x.mean(), y - y.mean()])
    evals, evecs = np.linalg.eigh(np.cov(pts.T))
    if evals[0] <= 0 or np.sqrt(evals[1] / evals[0]) < ratio:
        return x, y
    major = evecs[:, 1]
    if major[1] < 0:
        major = -major  # keep the end that was up, up
    turn = np.arctan2(1.0, 1.0) - np.arctan2(major[1], major[0])
    c, s = np.cos(turn), np.sin(turn)
    return c * x - s * y, s * x + c * y


def albedo_textures(model, mesh: TriMesh, max_size: int = 256) -> dict[int, np.ndarray]:
    """The albedo of each material the mesh uses, shrunk (colours only need to be roughly right)."""
    from PIL import Image

    out = {}
    for m in np.unique(mesh.face_material):
        m = int(m)
        if m >= len(model.materials) or not model.materials[m].albedo:
            continue
        tex = model.textures.get(model.materials[m].albedo)
        if tex is None:
            continue
        try:
            img = tex.decode()
        except Exception:  # noqa: BLE001 - a texture that can't be read just leaves the part gray
            continue
        if img.dtype != np.uint8:
            img = np.clip(img * 255, 0, 255).astype(np.uint8)
        if max(img.shape[:2]) > max_size:
            pil = Image.fromarray(np.ascontiguousarray(img[..., :4] if img.shape[2] >= 4 else img[..., :3]))
            pil.thumbnail((max_size, max_size))
            img = np.asarray(pil)
        out[m] = img
    return out


def png_bytes(img: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


class ThumbnailCache:
    """Previews on disk, drawn on first request; at most ``workers`` drawn at once."""

    def __init__(self, folder: str | os.PathLike, size: int = 256, workers: int = 2):
        self.folder = Path(folder)
        self.size = size
        self._gate = threading.Semaphore(workers)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def key(self, path: Path, variant: str | None = None) -> str:
        try:
            st = path.stat()
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stamp = "missing"
        text = f"{VERSION}|{self.size}|{os.path.normcase(os.path.abspath(path))}|{variant or ''}|{stamp}"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def file(self, path: Path, variant: str | None = None) -> Path:
        k = self.key(path, variant)
        return self.folder / k[:2] / f"{k}.png"

    def get(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = (),
            draw: bool = True) -> bytes | None:
        """The preview's PNG, drawn now if it isn't cached; None for a model with nothing to draw (or one
        that can't be read). With ``draw`` False, a preview not drawn yet is b"" instead of drawn."""
        target = self.file(path, variant)
        cached = self._cached(target)
        if cached is not None or not draw:
            return cached if cached is not None else (None if self._known_empty(target) else b"")
        if self._known_empty(target):
            return None
        with self._lock_for(target.name), self._gate:
            cached = self._cached(target)
            if cached is not None or self._known_empty(target):
                return cached
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = self._draw(path, variant, texture_dirs)
            except Exception:  # noqa: BLE001 - remembered, so the model isn't read again on every visit
                target.with_suffix(".fail").touch()
                raise
            if data is None:
                target.with_suffix(".none").touch()
                return None
            tmp = target.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return data

    @staticmethod
    def _cached(target: Path) -> bytes | None:
        try:
            return target.read_bytes()
        except OSError:
            return None

    @staticmethod
    def _known_empty(target: Path) -> bool:
        return target.with_suffix(".none").exists() or target.with_suffix(".fail").exists()

    def forget(self, paths) -> int:
        """Drop the previews of these model files, so they are drawn again."""
        n = 0
        for path in paths:
            target = self.file(Path(path))
            for f in (target, target.with_suffix(".none"), target.with_suffix(".fail")):
                if f.exists():
                    f.unlink(missing_ok=True)
                    n += 1
        return n

    def clear(self, only_failed: bool = False) -> int:
        """Remove every preview (or only the marks of models that couldn't be drawn)."""
        n = 0
        suffixes = (".fail",) if only_failed else (".png", ".none", ".fail", ".tmp")
        if self.folder.is_dir():
            for f in self.folder.rglob("*"):
                if f.is_file() and f.suffix in suffixes:
                    f.unlink(missing_ok=True)
                    n += 1
        return n

    def _lock_for(self, name: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def _draw(self, path: Path, variant: str | None, texture_dirs: tuple[str, ...]) -> bytes | None:
        from .importers import open_model

        model = open_model(path, variant, texture_dirs)
        mesh = model.build_mesh()
        if mesh.n_faces == 0:
            return None
        return png_bytes(render(mesh, albedo_textures(model, mesh), self.size))
