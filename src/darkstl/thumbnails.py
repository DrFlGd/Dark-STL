"""Preview pictures of models for the library's cards.

A small software renderer, so previews need no graphics card or window: the
model is drawn from the 3D view's three-quarter angle, back to front, each
triangle lit from the same lights as the viewer and coloured from its albedo
texture where the model has one. Pictures are kept in a cache folder, keyed by
the file's path, size and date, so each model is drawn once: again only when its
file changes, or when its folder is rescanned (:meth:`ThumbnailCache.forget`).
A model that can't be drawn is remembered too, so it isn't tried on every visit.

Drawing previews is background work: while the user opens a model (:meth:`ThumbnailCache.foreground`),
no new preview starts and the ones being drawn pause between steps, so the model isn't kept waiting
behind a page of previews.

Each preview also gets a **turntable** (:func:`turntable`): eight frames round the model in one strip, which
the card plays while the pointer is over it. It's drawn from the same opened model right after the preview,
in the background once no preview is waiting, so cards show their pictures first.

**Posed previews**: a character is drawn in a resting frame of an idle-like animation (one named idle, wait or
stand, or FromSoftware's standby ``a000_000000``) when it has one, instead of its bind pose (arms out). Such
a preview is kept under its own name, so turning the Settings switch off and on shows each kind at once.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
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
           supersample: int = 2, pause: Callable[[], None] | None = None) -> np.ndarray:
    """An RGBA picture (``size`` square, transparent background) of the mesh. ``pause`` is called every
    few thousand triangles, to let more urgent work go first."""
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
    for i, f in enumerate(order):
        if pause is not None and i % 4096 == 0:
            pause()
        c = tuple(rgb[f]) + (255,)
        pts = [(tri_x[f, 0], tri_y[f, 0]), (tri_x[f, 1], tri_y[f, 1]), (tri_x[f, 2], tri_y[f, 2])]
        draw.polygon(pts, fill=c, outline=c)
    return np.asarray(img.resize((size, size), Image.LANCZOS))


TURN_FRAMES = 8  # the turntable played on hover: a frame every 45 degrees round the model
TURN_SIZE = 192  # each frame's size, in pixels (the cards are 132 to 236 wide)
TURN_SAMPLES = 3_000_000  # most points drawn for one frame (a level-sized map piece is thinned out)


def _shading(mesh: TriMesh, textures: dict[int, np.ndarray] | None) -> tuple[np.ndarray, np.ndarray]:
    """Each triangle's normal (the model's own smoothing where it has some) and its colour, before lighting."""
    normals = mesh.face_normals()
    if mesh.corner_normals is not None:
        smooth = mesh.corner_normals.mean(axis=1)
        length = np.linalg.norm(smooth, axis=1, keepdims=True)
        ok = length[:, 0] > 1e-6
        smooth[ok] /= length[ok]
        flip = np.einsum("ij,ij->i", smooth, normals) < 0
        smooth[flip] *= -1
        normals = np.where(ok[:, None], smooth, normals)
    colours = 255 * (face_colours(mesh, textures or {}) / 255) ** 0.8
    return normals, colours


def turntable(mesh: TriMesh, textures: dict[int, np.ndarray] | None = None, frames: int = TURN_FRAMES,
              size: int = TURN_SIZE, pause: Callable[[], None] | None = None) -> np.ndarray:
    """The model turned round its upright axis, ``frames`` pictures side by side in one RGBA strip
    (``size`` high, ``frames * size`` wide), the first from the preview's own angle. The scale is the same in
    every frame, so the model turns without growing or shrinking.

    Drawn by scattering points over each triangle (as many as it covers pixels, twice over) and keeping the
    nearest at each pixel: no per-triangle loop, so eight frames take about as long as the preview itself."""
    from PIL import Image

    s = size * 2  # drawn at twice the size, then shrunk (smooth edges)
    strip = np.zeros((size, size * frames, 4), np.uint8)
    if mesh.n_faces == 0:
        return strip
    normals, colours = _shading(mesh, textures)
    v = mesh.vertices - 0.5 * (mesh.vertices.min(0) + mesh.vertices.max(0))
    radius = max(float(np.linalg.norm(v, axis=1).max()), 1e-9)
    scale = 0.92 * s / (2 * radius)
    faces = mesh.faces
    for k in range(frames):
        if pause is not None:
            pause()
        a = 2 * np.pi * k / frames
        c, si = np.cos(a), np.sin(a)
        turn = np.array([[c, -si, 0.0], [si, c, 0.0], [0.0, 0.0, 1.0]])
        back = turn.T @ _unit(VIEW)  # the camera goes round the model: the model turns the other way on screen
        right = _unit(np.cross(-back, [0.0, 0.0, 1.0]))
        up = np.cross(right, -back)
        px = v @ right * scale + s / 2
        py = s / 2 - v @ up * scale
        depth = v @ back
        n = normals.copy()
        n[n @ back < 0] *= -1
        light = (0.42 + 0.18 * (0.5 + 0.5 * n[:, 2]) + 0.55 * np.clip(n @ (turn.T @ _unit(KEY)), 0, None)
                 + 0.2 * np.clip(n @ (turn.T @ _unit(RIM)), 0, None))
        rgb = np.clip(colours * light[:, None], 0, 255).astype(np.uint8)
        img = _scatter(px[faces], py[faces], depth[faces], rgb, s)
        frame = Image.fromarray(img).resize((size, size), Image.LANCZOS)
        strip[:, k * size:(k + 1) * size] = np.asarray(frame)
    return strip


_R2 = (0.7548776662466927, 0.5698402909980532)  # the plastic number's sequence: even points over a triangle


def _scatter(tx: np.ndarray, ty: np.ndarray, tz: np.ndarray, rgb: np.ndarray, s: int) -> np.ndarray:
    """Triangles (corner x, y and depth, (n, 3) each, x and y in pixels) filled with their colours on an ``s``
    square transparent picture, the nearest over the farther."""
    area = 0.5 * np.abs((tx[:, 1] - tx[:, 0]) * (ty[:, 2] - ty[:, 0]) - (tx[:, 2] - tx[:, 0]) * (ty[:, 1] - ty[:, 0]))
    count = np.clip(np.ceil(area * 2.0).astype(np.int64) + 1, 1, 2 * s * s)
    total = int(count.sum())
    if total > TURN_SAMPLES:
        count = np.maximum(1, (count * (TURN_SAMPLES / total)).astype(np.int64))
        total = int(count.sum())
    face = np.repeat(np.arange(len(count)), count)
    k = np.arange(total) - np.repeat(np.cumsum(count) - count, count)
    u = (0.5 + k * _R2[0]) % 1.0
    w = (0.5 + k * _R2[1]) % 1.0
    out = u + w > 1.0
    u[out], w[out] = 1.0 - u[out], 1.0 - w[out]
    ax, ay, az = tx[face, 0], ty[face, 0], tz[face, 0]
    x = ax + u * (tx[face, 1] - ax) + w * (tx[face, 2] - ax)
    y = ay + u * (ty[face, 1] - ay) + w * (ty[face, 2] - ay)
    z = az + u * (tz[face, 1] - az) + w * (tz[face, 2] - az)
    ix, iy = x.astype(np.int64), y.astype(np.int64)
    keep = (ix >= 0) & (ix < s) & (iy >= 0) & (iy < s)
    pix = iy[keep] * s + ix[keep]
    z, face = z[keep], face[keep]
    order = np.lexsort((-z, pix))  # by pixel, nearest (largest depth towards the camera) first
    pix, face = pix[order], face[order]
    first = np.ones(len(pix), bool)
    first[1:] = pix[1:] != pix[:-1]
    img = np.zeros((s * s, 4), np.uint8)
    img[pix[first], :3] = rgb[face[first]]
    img[pix[first], 3] = 255
    return img.reshape(s, s, 4)


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


IDLE_WORDS = ("idle", "wait", "stand")
_FROMSOFT_STANDBY = re.compile(r"^a0+_0+$", re.I)  # a000_000000: the standby animation of FromSoftware's characters


def animation_names(animations) -> list[str]:
    """The animations' names, without reading every animation where the set can say (``names``)."""
    names = getattr(animations, "names", None)
    if callable(names):
        names = names()
    if isinstance(names, (list, tuple)):
        return [str(n) for n in names]
    return [i.name for i in animations.list()]


def resting_pose(animations) -> tuple[str, int] | None:
    """(animation, frame) to draw a character's preview in: the first frame of an idle-like animation (named
    idle, wait or stand, in that order of preference; else FromSoftware's standby), or None when it has none."""
    def rank(name: str) -> int | None:
        low = name.lower()
        for i, w in enumerate(IDLE_WORDS):
            if w in low:
                return i
        return len(IDLE_WORDS) if _FROMSOFT_STANDBY.match(low) else None

    found = sorted((rank(n), len(n), n) for n in animation_names(animations) if rank(n) is not None)
    return (found[0][2], 0) if found else None


def png_bytes(img: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


class ThumbnailCache:
    """Previews on disk, drawn on first request; at most ``workers`` drawn at once."""

    TURN_PENDING = 24  # turntables waiting to be drawn, with their models' meshes (the oldest are dropped)

    def __init__(self, folder: str | os.PathLike, size: int = 256, workers: int = 2):
        self.folder = Path(folder)
        self.size = size
        self._gate = threading.Semaphore(workers)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._busy = 0  # foreground work under way (a model being opened)
        self._idle = threading.Condition()
        self._drawing = 0  # previews being drawn (their turntables wait)
        self._turns: dict[Path, tuple] = {}  # turntable file -> (mesh, textures), drawn in the background
        self._turns_lock = threading.Lock()
        self._turn_thread: threading.Thread | None = None
        # called with (file, variant, model, mesh) of each model opened to draw its preview: what was learnt of it
        # (its size and counts) is kept for the library's search and columns
        self.on_model: Callable | None = None

    @contextmanager
    def foreground(self):
        """Work the user is waiting for: previews hold back until it's done."""
        with self._idle:
            self._busy += 1
        try:
            yield
        finally:
            with self._idle:
                self._busy -= 1
                if not self._busy:
                    self._idle.notify_all()

    def wait_idle(self, timeout: float = 120.0) -> None:
        """Wait while foreground work is under way (at most ``timeout`` seconds, so a preview never waits
        for ever behind something stuck)."""
        with self._idle:
            self._idle.wait_for(lambda: not self._busy, timeout)

    def key(self, path: Path, variant: str | None = None, posed: bool = False) -> str:
        try:
            st = path.stat()
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stamp = "missing"
        text = f"{VERSION}|{self.size}|{os.path.normcase(os.path.abspath(path))}|{variant or ''}|{stamp}"
        if posed:  # (unposed previews keep the names they had before posed ones came)
            text += "|posed"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def file(self, path: Path, variant: str | None = None, posed: bool = False) -> Path:
        k = self.key(path, variant, posed)
        return self.folder / k[:2] / f"{k}.png"

    @staticmethod
    def turn_file(target: Path) -> Path:
        """Where the turntable of the preview ``target`` is kept."""
        return target.with_name(target.stem + ".turn.png")

    def get(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = (),
            draw: bool = True, posed: bool = False) -> bytes | None:
        """The preview's PNG, drawn now if it isn't cached; None for a model with nothing to draw (or one
        that can't be read). With ``draw`` False, a preview not drawn yet is b"" instead of drawn. ``posed``:
        a character in a resting pose, when it has an idle-like animation (see :func:`resting_pose`)."""
        target = self.file(path, variant, posed)
        cached = self._cached(target)
        if cached is not None or not draw:
            return cached if cached is not None else (None if self._known_empty(target) else b"")
        if self._known_empty(target):
            return None
        self.wait_idle()
        with self._lock_for(target.name), self._gate:
            cached = self._cached(target)
            if cached is not None or self._known_empty(target):
                return cached
            target.parent.mkdir(parents=True, exist_ok=True)
            self._drawing += 1
            try:
                data, mesh, textures = self._draw(path, variant, texture_dirs, posed)
            except Exception:  # noqa: BLE001 - remembered, so the model isn't read again on every visit
                target.with_suffix(".fail").touch()
                raise
            finally:
                self._drawing -= 1
            if data is None:
                target.with_suffix(".none").touch()
                return None
            _write(target, data)
            self._turn_later(self.turn_file(target), mesh, textures)
            return data

    def get_turn(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = (),
                 draw: bool = True, posed: bool = False) -> bytes | None:
        """The turntable strip of a model's preview (:func:`turntable`), drawn now if it isn't kept yet (and not
        waiting to be: then it's drawn now from what's waiting); None for a model with nothing to draw. With
        ``draw`` False, b"" when it isn't drawn yet."""
        target = self.file(path, variant, posed)
        turn = self.turn_file(target)
        cached = self._cached(turn)
        if cached is not None or not draw:
            return cached if cached is not None else (None if self._known_empty(target) else b"")
        if self._known_empty(target):
            return None
        with self._turns_lock:
            pending = self._turns.pop(turn, None)
        self.wait_idle()
        with self._lock_for(turn.name), self._gate:
            cached = self._cached(turn)
            if cached is not None:
                return cached
            if pending is None:
                try:
                    _data, mesh, textures = self._draw(path, variant, texture_dirs, posed, picture=False)
                except Exception:  # noqa: BLE001 - as for the preview: remembered
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.with_suffix(".fail").touch()
                    raise
                if mesh is None:
                    return None
                pending = (mesh, textures)
            data = png_bytes(turntable(*pending, pause=self.wait_idle))
            turn.parent.mkdir(parents=True, exist_ok=True)
            _write(turn, data)
            return data

    def _turn_later(self, turn: Path, mesh: TriMesh, textures: dict) -> None:
        """Draw a turntable in the background, from the model just drawn (no preview waiting meanwhile)."""
        if mesh is None or mesh.n_faces == 0:
            return
        if mesh.n_faces > MAX_TRIANGLES:  # as the preview: every n-th triangle of a huge map piece
            mesh = mesh.subset(np.arange(mesh.n_faces) % int(np.ceil(mesh.n_faces / MAX_TRIANGLES)) == 0)
        with self._turns_lock:
            self._turns.pop(turn, None)
            self._turns[turn] = (mesh, textures)
            while len(self._turns) > self.TURN_PENDING:
                self._turns.pop(next(iter(self._turns)))  # drawn when hovered instead
            if self._turn_thread is None or not self._turn_thread.is_alive():
                self._turn_thread = threading.Thread(target=self._turn_loop, name="turntables", daemon=True)
                self._turn_thread.start()

    def _turn_loop(self) -> None:
        while True:
            self.wait_idle()
            if self._drawing:  # previews first
                time.sleep(0.1)
                continue
            with self._turns_lock:
                if not self._turns:
                    self._turn_thread = None
                    return
                turn = next(reversed(self._turns))  # the latest: most likely in view
                mesh, textures = self._turns.pop(turn)
            try:
                with self._lock_for(turn.name), self._gate:
                    if not turn.exists():
                        turn.parent.mkdir(parents=True, exist_ok=True)
                        _write(turn, png_bytes(turntable(mesh, textures, pause=self.wait_idle)))
            except Exception:  # noqa: BLE001 - drawn when it's hovered instead
                pass

    def wait_turns(self, timeout: float = 30.0) -> None:
        """Wait until every turntable waiting has been drawn (for tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._turns_lock:
                if not self._turns and (self._turn_thread is None or not self._turn_thread.is_alive()):
                    return
            time.sleep(0.02)

    @staticmethod
    def _cached(target: Path) -> bytes | None:
        """The file's bytes, or None when it isn't there."""
        return _read(target)

    @staticmethod
    def _known_empty(target: Path) -> bool:
        return target.with_suffix(".none").exists() or target.with_suffix(".fail").exists()

    def forget(self, paths) -> int:
        """Drop the previews of these model files (posed or not, and their turntables), so they are drawn again."""
        n = 0
        for path in paths:
            for posed in (False, True):
                target = self.file(Path(path), None, posed)
                turn = self.turn_file(target)
                with self._turns_lock:
                    self._turns.pop(turn, None)
                for f in (target, turn, target.with_suffix(".none"), target.with_suffix(".fail")):
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

    def _draw(self, path: Path, variant: str | None, texture_dirs: tuple[str, ...], posed: bool = False,
              picture: bool = True) -> tuple[bytes | None, TriMesh | None, dict]:
        """(the preview's PNG, the mesh drawn and its colour textures, for the turntable); (None, None, {}) for a
        model with nothing to draw. ``picture`` False: only the mesh and textures."""
        from .importers import open_model
        from .model import MeshOptions

        self.wait_idle()
        model = open_model(path, variant, texture_dirs)
        self.wait_idle()
        mesh = model.build_mesh()
        drawn = mesh
        if posed and model.animations is not None:
            try:
                pose = resting_pose(model.animations)
                if pose is not None:
                    self.wait_idle()
                    drawn = model.build_mesh(MeshOptions(pose=pose))
            except Exception as exc:  # noqa: BLE001 - drawn in its bind pose; the game's Problems list it
                model._anim_error = str(exc) or type(exc).__name__
                drawn = mesh
        if self.on_model is not None and picture:
            try:
                self.on_model(Path(path), variant, model, mesh)
            except Exception:  # noqa: BLE001 - only what was learnt is lost
                pass
        if drawn.n_faces == 0:
            return None, None, {}
        self.wait_idle()
        textures = albedo_textures(model, drawn)
        data = png_bytes(render(drawn, textures, self.size, pause=self.wait_idle)) if picture else None
        return data, drawn, textures


def _read(target: Path) -> bytes | None:
    try:
        return target.read_bytes()
    except OSError:
        return None


def _write(target: Path, data: bytes) -> None:
    tmp = target.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
