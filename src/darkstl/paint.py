"""Painting a model from its colour textures for multi-colour printing.

A slicer with several filaments (Bambu Studio, OrcaSlicer, PrusaSlicer) prints a model in colours from
paint on its triangles. The paint is stored in the 3MF on each triangle as a small tree: the triangle is
split into four at its edge midpoints, each quarter again, and so on, and every leaf names a filament
slot. A colour border can therefore run anywhere across a triangle without adding any geometry to the
mesh, which is what makes a model painted from a texture look like the texture rather than like its
triangles. :func:`paint_mesh` builds that tree for every triangle from the model's albedo textures:

1. The textures are reduced to a palette of a few colours (a slicer holds 4 to 16 filaments), chosen by
   k-means in Oklab (a perceptual colour space) over the texels the model's surface really covers,
   weighted by the surface area each covers (:func:`suggest_palette`), or given by the user. Only flat
   texels (ones whose neighbours share their colour) are learned from when there are enough of them, so
   the blend along the edge between two colours never takes a filament of its own.
2. Each texture is quantised to the palette (:func:`quantize`) and cleaned of specks that would be
   single dots of filament (:func:`cleanup`).
3. Every triangle is sampled over the quantised texture; one that isn't all one colour is split, and so
   on, until each piece is one colour, is smaller than the feature size asked for, or the depth limit is
   reached (the same error-driven subdivision Bambu Studio uses when it paints a textured model). Only the
   sides longer than the feature size are split, as the slicers' brushes and GusGusGooose's paint tools
   do: a piece with three long sides makes four, with two makes three and with one makes two, so a thin
   sliver halves along its length instead of turning into ever thinner slivers. Pieces too small for the
   texture to tell apart are not split.
4. The trees are serialised into the slicers' per-triangle strings (:func:`encode_tree`), the format
   PrusaSlicer's TriangleSelector writes (``slic3rpe:mmu_segmentation``) and Bambu Studio and
   OrcaSlicer read as ``paint_color``: read from the last hex digit backwards, each digit holds the number
   of split sides in its low two bits (0 for a leaf) and in the high two either the leaf's filament slot
   (or escape to the next digit, + 3) or the split's special side; a split's children follow in reverse
   order.

Meshes are in metres (export frame); the feature size is given at the print size the user names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from .geometry.mesh import TriMesh

ProgressFn = Callable[[float, str], None]

__all__ = ["PaintSettings", "Painting", "suggest_palette", "quantize", "cleanup", "paint_mesh", "encode_tree",
           "decode_string", "leaf_colours", "parse_colour", "hex_colour", "MAX_COLOURS", "SurfaceColours",
           "surface_colours", "transfer_paint", "colour_textures"]

MAX_COLOURS = 16  # filament slots the slicers accept in paint (OrcaSlicer stops at 16)
LATTICE = (1, 2, 3, 4, 6, 8, 11, 16)  # sample lattices per piece: n * n points
MAX_PREVIEW = 2048  # textures are read at most this big: a 0.5 mm feature on a 150 mm print is 300 texels
MANY_LEAVES = 3_000_000  # pieces of paint above which the slicer is warned to be slow
FLAT_DISTANCE = 0.04  # Oklab distance under which neighbouring texels count as one flat colour
FLAT_SHARE = 0.25  # the palette is learned from flat texels when they cover at least this share of the surface


@dataclass
class PaintSettings:
    colours: int = 4  # palette size when the palette is chosen from the textures
    palette: list[str] | None = None  # "#rrggbb" per filament slot, chosen by the user; None = from the textures
    print_height_mm: float = 150.0  # the model's height when printed, for the feature size
    feature_mm: float = 0.5  # smallest colour feature kept, at that print size
    cleanup: str = "light"  # none | light | strong: specks removed from the quantised textures
    max_depth: int = 12  # levels of splits of a triangle (each halves at least one side)
    tolerance: float = 0.0  # suggested colours closer than this (Oklab distance x 100) are merged into one
    min_share: float = 0.0  # % of the surface: suggested colours covering less are dropped (taken by the nearest)
    brightness: float = 0.0  # -1..1, added to the texture colours before they are reduced
    contrast: float = 1.0  # 0..3, around mid grey
    saturation: float = 1.0  # 0 (grey) .. 3
    sources: dict[str, str] | None = None  # material index -> texture key it is painted from ("none": not painted)

    def key(self) -> tuple:
        return (int(self.colours), tuple(self.palette) if self.palette else None, round(self.print_height_mm, 3),
                round(self.feature_mm, 4), self.cleanup, int(self.max_depth)) + self.colour_key()

    def colour_key(self) -> tuple:
        """What changes the colours read off the textures (and so the suggested palette)."""
        return (round(self.tolerance, 3), round(self.min_share, 3), *self.adjust(),
                tuple(sorted((self.sources or {}).items())))

    def adjust(self) -> tuple[float, float, float]:
        return (round(float(self.brightness), 3), round(float(self.contrast), 3), round(float(self.saturation), 3))


@dataclass
class Painting:
    """A painted mesh: one string per triangle (empty: unpainted, the object's own filament)."""

    strings: list[str]
    palette: np.ndarray  # (K, 3) uint8, filament slot k + 1
    coverage: np.ndarray  # (K,) fraction of the surface in each colour
    split_faces: int = 0
    leaves: int = 0
    depth: int = 0
    unpainted_faces: int = 0  # triangles whose material has no colour texture
    notes: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        return {"colours": [hex_colour(c) for c in self.palette], "coverage": [round(float(c), 4) for c in self.coverage],
                "split_faces": self.split_faces, "leaves": self.leaves, "depth": self.depth,
                "unpainted_faces": self.unpainted_faces, "notes": list(self.notes)}


# ---------------------------------------------------------------------------
# colours


def parse_colour(text: str) -> np.ndarray:
    s = str(text).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"Not a colour: '{text}' (use #rrggbb)")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)


def hex_colour(rgb) -> str:
    r, g, b = (int(x) for x in np.asarray(rgb).reshape(3)[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def srgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """sRGB 0..1 (..., 3) -> Oklab (..., 3). Distances in Oklab match how different colours look."""
    c = np.asarray(rgb, np.float32)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                   [0.2119034982, 0.6806995451, 0.1073969566],
                   [0.0883024619, 0.2817188376, 0.6299787005]], np.float32)
    m2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]], np.float32)
    lms = np.cbrt(lin @ m1.T)
    return lms @ m2.T


def _rgb(img: np.ndarray) -> np.ndarray:
    """A texture as (H, W, 3) uint8."""
    arr = np.asarray(img)
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.dstack([arr, arr, arr])
    return np.ascontiguousarray(arr[..., :3])


def _shrink(img: np.ndarray, max_size: int = MAX_PREVIEW) -> np.ndarray:
    """Average the texture down by a whole factor so its longer side is at most ``max_size``."""
    h, w = img.shape[:2]
    f = int(np.ceil(max(h, w) / max_size))
    if f <= 1:
        return img
    h2, w2 = h // f * f, w // f * f
    small = img[:h2, :w2].reshape(h2 // f, f, w2 // f, f, -1).mean(axis=(1, 3))
    return small.astype(img.dtype)


@lru_cache(maxsize=8)
def _cube_rgb_cached(adjust: tuple[float, float, float], bits: int = 6) -> np.ndarray:
    rgb = (_cube_colours(np.arange(1 << (3 * bits)), bits) / 255.0).astype(np.float32)
    brightness, contrast, saturation = adjust
    if adjust != (0.0, 1.0, 1.0):
        luma = rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        rgb = luma[:, None] + (rgb - luma[:, None]) * saturation
        rgb = (rgb - 0.5) * contrast + 0.5 + brightness
        rgb = np.clip(rgb, 0.0, 1.0)
    rgb.setflags(write=False)
    return rgb


@lru_cache(maxsize=8)
def _cube_lab_cached(adjust: tuple[float, float, float], bits: int = 6) -> np.ndarray:
    lab = srgb_to_oklab(_cube_rgb_cached(adjust, bits)).astype(np.float32)
    lab.setflags(write=False)
    return lab


def _cube_rgb(settings: PaintSettings | None, bits: int = 6) -> np.ndarray:
    """The colour (sRGB 0..1) every cell of the colour cube stands for, after the settings' brightness,
    contrast and saturation: the colours the palette is chosen from and the textures are reduced by."""
    return _cube_rgb_cached(settings.adjust() if settings else (0.0, 1.0, 1.0), bits)


def _cube_lab(settings: PaintSettings | None, bits: int = 6) -> np.ndarray:
    return _cube_lab_cached(settings.adjust() if settings else (0.0, 1.0, 1.0), bits)


def colour_textures(model, mesh: TriMesh, settings: PaintSettings | None = None) -> dict[int, np.ndarray]:
    """Material -> decoded colour texture to paint it from: the material's own colour texture, or the one the
    settings' ``sources`` name for it ("none": that material isn't painted and takes filament 1)."""
    sources = (settings.sources if settings else None) or {}
    out = {}
    for m in np.unique(mesh.face_material).tolist():
        m = int(m)
        key = sources.get(str(m))
        if key is None:
            key = model.materials[m].albedo if m < len(model.materials) else None
        elif key == "none":
            continue
        elif key not in model.textures:
            raise ValueError(f"No texture '{key}' in this model to paint from.")
        if not key or key not in model.textures:
            continue
        try:
            out[m] = model.textures[key].decode()
        except Exception:  # noqa: BLE001 - that material isn't painted
            pass
    return out


def _cube_keys(rgb: np.ndarray, bits: int = 6) -> np.ndarray:
    shift = 8 - bits
    r, g, b = (rgb[..., i].astype(np.int64) >> shift for i in range(3))
    return (r << (2 * bits)) | (g << bits) | b


def _cube_colours(keys: np.ndarray, bits: int = 6) -> np.ndarray:
    mask = (1 << bits) - 1
    shift = 8 - bits
    half = (1 << shift) / 2 - 0.5
    r, g, b = ((keys >> (2 * bits)) & mask, (keys >> bits) & mask, keys & mask)
    return (np.stack([r, g, b], axis=-1) << shift) + half


def _kmeans(points: np.ndarray, weights: np.ndarray, k: int, iterations: int = 40, seed: int = 7) -> np.ndarray:
    """Weighted k-means with k-means++ seeding; returns (k, 3) centres sorted by the weight they hold."""
    rng = np.random.default_rng(seed)
    n = len(points)
    k = min(k, n)
    w = weights / weights.sum()
    centres = np.empty((k, points.shape[1]), np.float32)
    centres[0] = points[rng.choice(n, p=w)]
    d2 = ((points - centres[0]) ** 2).sum(1)
    for i in range(1, k):
        p = d2 * w
        if p.sum() <= 0:
            centres[i:] = centres[i - 1]
            break
        centres[i] = points[rng.choice(n, p=p / p.sum())]
        d2 = np.minimum(d2, ((points - centres[i]) ** 2).sum(1))
    for _ in range(iterations):
        d = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(2)
        label = d.argmin(1)
        new = centres.copy()
        held = np.zeros(k)
        for i in range(k):
            sel = label == i
            if sel.any():
                ws = w[sel]
                held[i] = ws.sum()
                new[i] = (points[sel] * ws[:, None]).sum(0) / ws.sum()
        if np.allclose(new, centres, atol=1e-6):
            centres = new
            break
        centres = new
    d = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(2)
    label = d.argmin(1)
    held = np.array([w[label == i].sum() for i in range(k)])
    order = np.argsort(-held, kind="stable")
    return centres[order]


# ---------------------------------------------------------------------------
# sampling the model over its textures


def _lattice(n: int) -> np.ndarray:
    """n * n barycentric points spread evenly over a triangle (the centroids of its n-level regular split)."""
    if n == 1:
        return np.array([[1 / 3, 1 / 3, 1 / 3]], np.float32)
    i, j = np.mgrid[0:n, 0:n]
    up = (i + j <= n - 1)
    a = (i[up] + 1 / 3) / n
    b = (j[up] + 1 / 3) / n
    down = (i + j <= n - 2)
    a2 = (i[down] + 2 / 3) / n
    b2 = (j[down] + 2 / 3) / n
    pts = np.column_stack([np.concatenate([a, a2]), np.concatenate([b, b2])])
    return np.column_stack([pts, 1 - pts.sum(1)]).astype(np.float32)


_LATTICES = {n: _lattice(n) for n in LATTICE}


def _lattice_for(texel_area: np.ndarray) -> np.ndarray:
    """The lattice size for pieces covering that many texels: about two samples per texel, 256 at most."""
    want = np.ceil(np.sqrt(np.maximum(texel_area, 0.0) * 2.0))
    out = np.full(len(texel_area), LATTICE[-1], np.int16)
    for n in reversed(LATTICE):
        out[want <= n] = n
    return out


PALETTE_LATTICE = (1, 2, 3, 4, 6, 8, 11, 16, 23, 32, 45, 64, 90, 128)  # for the palette: about a sample a texel
PALETTE_SAMPLES = 8_000_000  # samples per texture for the palette at most


def _palette_lattice(texel_area: np.ndarray) -> np.ndarray:
    """Lattice sizes for reading the palette: about one sample per texel the triangle covers, so a large
    triangle over a finely patterned texture is read texel by texel rather than at a few hundred points
    (which can all fall on the blend between two colours), within PALETTE_SAMPLES for the whole texture."""
    want = np.sqrt(np.maximum(texel_area, 0.0))
    total = float((np.minimum(np.ceil(want), PALETTE_LATTICE[-1]) ** 2).sum())
    if total > PALETTE_SAMPLES:
        want = want * np.sqrt(PALETTE_SAMPLES / total)
    out = np.full(len(texel_area), PALETTE_LATTICE[-1], np.int16)
    for n in reversed(PALETTE_LATTICE):
        out[np.ceil(want) <= n] = n
    return out


def _lattice_cached(n: int) -> np.ndarray:
    if n not in _LATTICES:
        _LATTICES[n] = _lattice(n)
    return _LATTICES[n]


def _texel_area(uv: np.ndarray, w: int, h: int) -> np.ndarray:
    """Area of UV triangles (N, 3, 2) in texels."""
    e1 = (uv[:, 1] - uv[:, 0]) * (w, h)
    e2 = (uv[:, 2] - uv[:, 0]) * (w, h)
    return 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])


def _lookup(index: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Values of a (H, W) map at UV points (..., 2); the texture repeats outside 0..1."""
    h, w = index.shape
    x = np.minimum((np.mod(uv[..., 0], 1.0) * w).astype(np.int64), w - 1)
    y = np.minimum((np.mod(uv[..., 1], 1.0) * h).astype(np.int64), h - 1)
    return index[y, x]


def _sample(index: np.ndarray, uv: np.ndarray, n: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample the map over UV triangles (N, 3, 2) with n[i]^2 points each. Returns for each triangle the
    colour most of its samples have, how many samples disagree, and how many samples it had."""
    count = len(uv)
    dominant = np.zeros(count, np.int16)
    wrong = np.zeros(count, np.int32)
    total = np.zeros(count, np.int32)
    k = int(index.max()) + 1 if index.size else 1
    for size in LATTICE:
        sel = np.flatnonzero(n == size)
        if not len(sel):
            continue
        bary = _LATTICES[size]
        for start in range(0, len(sel), max(1, 2_000_000 // (size * size))):
            part = sel[start:start + max(1, 2_000_000 // (size * size))]
            pts = np.matmul(bary[None, :, :], uv[part])  # (M, n*n, 2)
            vals = _lookup(index, pts).astype(np.int64)  # (M, n*n)
            m, s = vals.shape
            keys = (np.arange(m, dtype=np.int64)[:, None] * k + vals).reshape(-1)
            counts = np.bincount(keys, minlength=m * k).reshape(m, k)
            best = counts.argmax(1)
            dominant[part] = best
            wrong[part] = s - counts[np.arange(m), best]
            total[part] = s
    return dominant, wrong, total


# ---------------------------------------------------------------------------
# palette


def _textured_faces(mesh: TriMesh, textures: dict[int, np.ndarray]) -> dict[int, np.ndarray]:
    """Material -> indices of its faces, for materials with a colour texture."""
    if mesh.corner_uvs is None:
        return {}
    out = {}
    for mat in np.unique(mesh.face_material):
        if int(mat) in textures:
            out[int(mat)] = np.flatnonzero(mesh.face_material == mat)
    return out


def _prepared(textures: dict[int, np.ndarray]) -> dict[int, np.ndarray]:
    return {m: _shrink(_rgb(img)) for m, img in textures.items()}


def suggest_palette(mesh: TriMesh, textures: dict[int, np.ndarray], colours: int,
                    progress: ProgressFn | None = None,
                    settings: PaintSettings | None = None) -> tuple[np.ndarray, np.ndarray]:
    """The ``colours`` colours that best stand for the textures over the model's surface: k-means in Oklab
    over the texels the surface covers, each weighted by the area it covers. Returns the palette (K, 3)
    uint8, most-used colour first, and the fraction of the surface each colour takes.

    The settings' brightness, contrast and saturation change the texture colours first; then colours closer
    than its tolerance are merged and colours covering less than its smallest share are dropped, so the
    palette can have fewer than ``colours`` colours."""
    colours = int(min(max(colours, 1), MAX_COLOURS))
    faces = _textured_faces(mesh, textures)
    if not faces:
        raise ValueError("This model has no colour textures to paint from.")
    bits = 6
    hist = np.zeros(1 << (3 * bits), np.float64)
    flat_hist = np.zeros_like(hist)
    areas = mesh.face_areas()
    cube_rgb = _cube_rgb(settings, bits)
    cube_lab = _cube_lab(settings, bits)
    for i, (mat, sel) in enumerate(faces.items()):
        if progress:
            progress(0.02 + 0.18 * i / len(faces), "Reading the colours")
        img = _shrink(_rgb(textures[mat]))
        keys = _cube_keys(img, bits)
        # a texel counts as flat when its four neighbours are nearly its colour: a blend along an edge between
        # two colours is then never learned as a colour of its own (as GusGusGooose does for pictures)
        flat_keys = np.where(_flat_texels(cube_lab[keys]), keys, -1)
        uv = mesh.corner_uvs[sel]
        n = _palette_lattice(_texel_area(uv, img.shape[1], img.shape[0]))
        for size in np.unique(n).tolist():
            sel_n = np.flatnonzero(n == size)
            bary = _lattice_cached(size)
            step = max(1, 2_000_000 // (size * size))
            for start in range(0, len(sel_n), step):
                part = sel_n[start:start + step]
                weight = np.repeat(areas[sel][part] / (size * size), size * size)
                pts = np.matmul(bary[None, :, :], uv[part]).reshape(-1, 2)
                hist += np.bincount(_lookup(keys, pts), weights=weight, minlength=len(hist))
                fk = _lookup(flat_keys, pts)
                keep = fk >= 0
                flat_hist += np.bincount(fk[keep], weights=weight[keep], minlength=len(hist))
    # learn from the flat texels when there are enough of them (a noisy texture has few, and then all count)
    learn = flat_hist if flat_hist.sum() >= FLAT_SHARE * hist.sum() and np.count_nonzero(flat_hist) >= colours else hist
    used = np.flatnonzero(learn > 0)
    if progress:
        progress(0.2, "Choosing the colours")
    rgb = cube_rgb[used]
    lab = cube_lab[used]
    centres = _kmeans(lab, learn[used], colours)
    tolerance = float(settings.tolerance) / 100 if settings else 0.0
    if tolerance > 0 and len(centres) > 1:
        centres = _merge_close(lab, learn[used], centres, tolerance)
    # each centre becomes the real texel colour nearest it, so the palette holds colours that are in the model
    d = ((lab[:, None, :] - centres[None, :, :]) ** 2).sum(2)
    nearest = d.argmin(0)
    palette = np.clip(np.round(rgb[nearest] * 255), 0, 255).astype(np.uint8)
    # the share of the surface each colour takes counts every texel, flat or not
    every = np.flatnonzero(hist > 0)

    def shares(pal):
        plab = srgb_to_oklab(pal.astype(np.float32) / 255.0)
        label = _nearest(cube_lab[every], plab)
        cov = np.bincount(label, weights=hist[every], minlength=len(pal))
        return cov / max(cov.sum(), 1e-12)

    coverage = shares(palette)
    least = float(settings.min_share) / 100 if settings else 0.0
    while least > 0 and len(palette) > 1 and coverage.min() < least:
        palette = np.delete(palette, int(coverage.argmin()), axis=0)  # its texels go to the nearest colour left
        coverage = shares(palette)
    order = np.argsort(-coverage, kind="stable") if (tolerance > 0 or least > 0) else np.arange(len(palette))
    return palette[order], coverage[order]


def _nearest(lab: np.ndarray, plab: np.ndarray) -> np.ndarray:
    out = np.empty(len(lab), np.int64)
    for start in range(0, len(lab), 65536):
        out[start:start + 65536] = ((lab[start:start + 65536, None, :] - plab[None, :, :]) ** 2).sum(2).argmin(1)
    return out


def _merge_close(points: np.ndarray, weights: np.ndarray, centres: np.ndarray, tolerance: float) -> np.ndarray:
    """Merge k-means centres closer than ``tolerance`` (Oklab distance): the less used one joins the more used
    one, then the centres settle again over the colours (a few k-means steps), until none are that close."""
    w = weights / max(weights.sum(), 1e-12)
    for _ in range(8):
        label = _nearest(points, centres)
        held = np.bincount(label, weights=w, minlength=len(centres))
        order = np.argsort(-held, kind="stable")
        kept: list[int] = []
        for i in order.tolist():
            if held[i] <= 0:
                continue
            if all(np.linalg.norm(centres[i] - centres[j]) >= tolerance for j in kept):
                kept.append(i)
        if len(kept) == len(centres):
            return centres
        centres = centres[kept]
        for _ in range(10):
            label = _nearest(points, centres)
            new = centres.copy()
            for i in range(len(centres)):
                sel = label == i
                if sel.any():
                    new[i] = (points[sel] * w[sel, None]).sum(0) / w[sel].sum()
            if np.allclose(new, centres, atol=1e-6):
                break
            centres = new
    return centres


def _flat_texels(lab: np.ndarray) -> np.ndarray:
    """(H, W) mask of texels whose four neighbours (wrapping) all lie within FLAT_DISTANCE in Oklab."""
    flat = np.ones(lab.shape[:2], bool)
    limit = FLAT_DISTANCE * FLAT_DISTANCE
    for axis in (0, 1):
        d = ((lab - np.roll(lab, -1, axis=axis)) ** 2).sum(2) <= limit  # this texel and the next one agree
        flat &= d & np.roll(d, 1, axis=axis)
    return flat


def quantize(img: np.ndarray, palette: np.ndarray, settings: PaintSettings | None = None) -> np.ndarray:
    """Each texel's nearest palette colour (Oklab), as (H, W) int16 indices into the palette; the texels'
    colours changed first by the settings' brightness, contrast and saturation."""
    rgb = _rgb(img)
    bits = 6
    keys = _cube_keys(rgb, bits)
    lab = _cube_lab(settings, bits)
    plab = srgb_to_oklab(np.asarray(palette, np.float32) / 255.0)
    lut = _nearest(lab, plab).astype(np.int16)
    return lut[keys]


CLEANUP = {"none": (0, 0), "light": (1, 4), "strong": (2, 24)}  # majority radius, smallest island kept (texels)


def cleanup(index: np.ndarray, k: int, level: str = "light") -> np.ndarray:
    """Remove specks from a quantised map: each texel takes the colour most of its neighbours have, then
    islands of one colour smaller than a few texels take the colour around them."""
    from scipy import ndimage

    radius, min_island = CLEANUP.get(level, CLEANUP["light"])
    if radius <= 0 or index.size == 0:
        return index
    size = 2 * radius + 1
    best = np.full(index.shape, -1.0, np.float32)
    result = index.copy()
    for c in range(k):
        votes = ndimage.uniform_filter((index == c).astype(np.float32), size=size, mode="wrap")
        better = votes > best
        result[better] = c
        best[better] = votes[better]
    out = result
    if min_island > 1:
        small = np.zeros(out.shape, bool)
        for c in range(k):
            labels, count = ndimage.label(out == c)
            if count == 0:
                continue
            sizes = np.bincount(labels.reshape(-1), minlength=count + 1)
            tiny = sizes < min_island
            tiny[0] = False
            small |= tiny[labels]
        if small.any() and not small.all():
            near = ndimage.distance_transform_edt(small, return_distances=False, return_indices=True)
            out = out[near[0], near[1]]
    return out


# ---------------------------------------------------------------------------
# painting


def _maybe(progress: ProgressFn | None, frac: float, msg: str) -> None:
    if progress:
        progress(frac, msg)


def paint_mesh(mesh: TriMesh, textures: dict[int, np.ndarray], settings: PaintSettings | None = None,
               progress: ProgressFn | None = None) -> Painting:
    """Paint every triangle of ``mesh`` from the colour textures of its materials (material -> RGB(A)
    image), reduced to the palette the settings ask for. See the module notes for how."""
    settings = settings or PaintSettings()
    faces = _textured_faces(mesh, textures)
    if not faces:
        raise ValueError("This model has no colour textures to paint from.")
    notes: list[str] = []
    if settings.palette:
        palette = np.array([parse_colour(c) for c in settings.palette][:MAX_COLOURS], np.uint8)
        coverage = None
    else:
        palette, coverage = suggest_palette(mesh, textures, settings.colours, progress, settings)
    k = len(palette)
    feature = _feature(mesh, settings)
    strings = [""] * mesh.n_faces
    held = np.zeros(k)
    split_faces = leaves = 0
    deepest = 0
    verts = mesh.vertices[mesh.faces]
    areas = mesh.face_areas()
    for i, (mat, sel) in enumerate(faces.items()):
        _maybe(progress, 0.25 + 0.7 * i / len(faces), f"Painting material {i + 1} of {len(faces)}")
        img = _shrink(_rgb(textures[mat]))
        index = cleanup(quantize(img, palette, settings), k, settings.cleanup)
        h, w = index.shape
        root_uv = mesh.corner_uvs[sel].astype(np.float32)

        def sample(root, bary, _xyz, index=index, root_uv=root_uv, w=w, h=h):
            uv = np.matmul(bary, root_uv[root])
            n = _lattice_for(_texel_area(uv, w, h))
            dominant, wrong, _total = _sample(index, uv, n)
            return dominant, wrong, n > 1

        got = _paint_faces(verts[sel].astype(np.float32), areas[sel], sample, feature, settings.max_depth, k)
        for j, s in zip(sel.tolist(), got.strings):
            strings[j] = s
        held += got.held
        split_faces += got.split_faces
        leaves += got.leaves
        deepest = max(deepest, got.depth)
    unpainted = mesh.n_faces - sum(len(s) for s in faces.values())
    if unpainted:
        notes.append(f"{unpainted:,} triangles have no colour texture; they take the first filament.")
    if leaves > MANY_LEAVES:
        notes.append(f"The paint has {leaves / 1e6:.1f} million pieces, which can make the slicer slow: a larger "
                     "smallest feature, fewer colours or a stronger clean-up would mean fewer.")
    if coverage is None:
        coverage = held / max(held.sum(), 1e-12)
    _maybe(progress, 0.95, "Painted")
    return Painting(strings, palette, coverage, split_faces, leaves, deepest, unpainted, notes)


@dataclass
class _Painted:
    strings: list[str]
    held: np.ndarray  # area painted in each colour
    split_faces: int
    leaves: int
    depth: int


def _feature(mesh: TriMesh, settings: PaintSettings) -> float:
    """The smallest piece worth splitting, in the model's units: the feature size scaled from the print to
    the model (whose height is printed at the print height)."""
    return float(settings.feature_mm) / max(float(settings.print_height_mm), 1e-6) * _height(mesh)


def _height(mesh: TriMesh) -> float:
    """The model's height (Z), which the print height scales; a model with next to no height (a flat plate)
    is taken by its longest side instead."""
    lo, hi = mesh.bounds()
    size = np.asarray(hi, float) - np.asarray(lo, float)
    return float(max(size[2] if size[2] >= 0.01 * size.max() else size.max(), 1e-9))


def _paint_faces(root_xyz: np.ndarray, areas: np.ndarray, sample, feature: float, max_depth: int,
                 k: int) -> _Painted:
    """The slicer strings for triangles (N, 3, 3), split where ``sample`` finds more than one colour.

    ``sample(root, bary, xyz)`` is given the pieces of a level (each piece's triangle, its corners as
    barycentrics of that triangle, and as points) and returns each piece's main palette index, how many of
    its samples disagree with it, and whether it is big enough to tell colours apart in."""
    leaf_str = _leaf_strings(k)
    held = np.zeros(k)
    # nodes of the level being worked on: root face, barycentric corners in the root face and the share of
    # the root face's area each covers
    count0 = len(root_xyz)
    root = np.arange(count0)
    bary = np.broadcast_to(np.eye(3, dtype=np.float32), (count0, 3, 3)).copy()
    share = np.ones(count0)
    deepest = 0
    levels: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []  # (state, digit, first child, children)
    for depth in range(max_depth + 1):
        xyz = np.matmul(bary, root_xyz[root])
        dominant, wrong, big = sample(root, bary, xyz)
        # side k runs from corner k to corner k + 1; only the sides longer than the feature size are split
        long = np.linalg.norm(np.roll(xyz, -1, axis=1) - xyz, axis=2) >= feature
        split = (wrong > 0) & big & long.any(1) & (depth < max_depth)
        leaf = ~split
        held += np.bincount(dominant[leaf], weights=areas[root[leaf]] * share[leaf], minlength=k)[:k]
        which = np.flatnonzero(split)
        digit, first, count, kids, kid_share = _split_long(bary[which], long[which])
        first_all = np.full(len(root), -1, np.int64)
        count_all = np.zeros(len(root), np.int8)
        digit_all = np.zeros(len(root), np.int8)
        first_all[which], count_all[which], digit_all[which] = first, count, digit
        levels.append((dominant.astype(np.int16), digit_all, first_all, count_all))
        if not len(which):
            break
        deepest = max(deepest, depth + 1)
        parent = np.repeat(which, count)
        root, bary, share = root[parent], kids, share[parent] * kid_share
    # strings, bottom up: a leaf's slot, or its children's strings (in order) then the split's own digit
    below: list[str] = []
    for state, digit, first, count in reversed(levels):
        own = [leaf_str[int(s) + 1] for s in state]
        for j in np.flatnonzero(first >= 0):
            f = int(first[j])
            own[j] = "".join(below[f:f + int(count[j])]) + _DIGITS[int(digit[j])]
        below = own
    return _Painted(below, held, int((levels[0][2] >= 0).sum()) if levels else 0,
                    sum(int((lv[2] < 0).sum()) for lv in levels), deepest)


_DIGITS = "0123456789ABCDEF"


def _mid(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a + b) / 2


def _split_long(bary: np.ndarray, long: np.ndarray):
    """Split pieces (N, 3, 3) (corners as barycentrics of the root face) across their long sides only, as the
    slicers' own brushes do (and GusGusGooose after them): ``long[i, k]`` says side k (corner k to k + 1) is
    longer than the feature size. One long side is cut from the corner opposite it into two; two long sides
    keep the third whole and make three; three make four at the edge midpoints. A thin sliver so halves
    only its long sides rather than splitting into ever thinner slivers.

    Returns per piece the split's digit (sides split, plus the special side times 4), where its children
    start in the returned list, how many it has, then the children (M, 3, 3) and each child's share of its
    parent's area. The children are in the slicers' order (PrusaSlicer's TriangleSelector::perform_split):
    with the corners taken round from the special one as a, b, c, one side gives (a, b, m_bc), (m_bc, c, a);
    two sides give (a, m_ab, m_ca), (m_ab, b, m_ca), (b, c, m_ca); three give (v0, m01, m20),
    (m01, v1, m12), (m12, v2, m20), (m01, m12, m20)."""
    n = len(bary)
    sides = long.sum(1).astype(np.int8)
    # the special side, numbered as the slicers number sides (side i lies opposite corner i): for one side the
    # side that is split, for two the side kept; three always have special side 0
    special = np.zeros(n, np.int8)
    one, two = sides == 1, sides == 2
    special[one] = (np.argmax(long[one], axis=1) + 2) % 3
    special[two] = (np.argmin(long[two], axis=1) + 2) % 3
    count = (sides + 1).astype(np.int8)
    first = np.zeros(n, np.int64)
    if n:
        first[1:] = np.cumsum(count)[:-1]
    total = int(count.sum())
    kids = np.empty((total, 3, 3), np.float32)
    kid_share = np.empty(total)
    rot = (special[:, None] + np.arange(3)[None, :]) % 3
    turned = np.take_along_axis(bary, rot[:, :, None].astype(np.int64), axis=1)  # corners from the special one
    for sides_n, layout in ((1, _ONE), (2, _TWO), (3, _THREE)):
        sel = np.flatnonzero(sides == sides_n)
        if not len(sel):
            continue
        src = bary[sel] if sides_n == 3 else turned[sel]
        a, b, c = src[:, 0], src[:, 1], src[:, 2]
        pts = {"a": a, "b": b, "c": c, "ab": _mid(a, b), "bc": _mid(b, c), "ca": _mid(c, a)}
        for i, (corners, part) in enumerate(layout):
            at = first[sel] + i
            kids[at] = np.stack([pts[x] for x in corners], axis=1)
            kid_share[at] = part
    digit = (sides | (special << 2)).astype(np.int8)
    return digit, first, count, kids, kid_share


# children per split as (corners, share of the parent's area); a, b, c are the corners from the special one
_ONE = ((("a", "b", "bc"), 0.5), (("bc", "c", "a"), 0.5))
_TWO = ((("a", "ab", "ca"), 0.25), (("ab", "b", "ca"), 0.25), (("b", "c", "ca"), 0.5))
_THREE = ((("a", "ab", "ca"), 0.25), (("ab", "b", "bc"), 0.25), (("bc", "c", "ca"), 0.25), (("ab", "bc", "ca"), 0.25))


# ---------------------------------------------------------------------------
# painting a model made from a textured one (the Finished model, cut pieces)

SOURCE_POINTS = 6_000_000  # points read off the textured surface at most
FACING = 0.3  # a surface point only colours a target whose normal is within about 70 degrees of its own (or its reverse)


@dataclass
class SurfaceColours:
    """A textured model's surface reduced to the palette, as coloured points, for painting meshes made from it
    that have no textures of their own (the model made watertight, the pieces it was cut into)."""

    palette: np.ndarray  # (K, 3) uint8
    coverage: np.ndarray | None  # (K,) when the palette was suggested
    points: np.ndarray  # (M, 3) on the textured surface
    normals: np.ndarray  # (M, 3) of the triangle each lies on
    labels: np.ndarray  # (M,) palette index
    spacing: float  # between neighbouring points
    feature: float  # the smallest piece worth splitting, in the model's units
    reach: float  # how far from the textured surface a target still takes its colours
    tree: object = None  # scipy cKDTree over the points

    def label_at(self, pts: np.ndarray, normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The palette index of the textured surface nearest each point (N, 3) that lies along the normal given
        for it (N, 3), facing either way, and whether one was found within reach; others take index 0, the first
        filament (a cut face or a filled gap, inside the model)."""
        out = np.zeros(len(pts), np.int16)
        found = np.zeros(len(pts), bool)
        m = len(self.points)
        for start in range(0, len(pts), 500_000):
            p = pts[start:start + 500_000]
            nrm = normals[start:start + 500_000]
            _d, idx = self.tree.query(p, k=8, distance_upper_bound=self.reach, workers=-1)
            ok = idx < m
            safe = np.where(ok, idx, 0)
            # either way round: a game's thin cloth is one open sheet seen from both sides, and made solid it has a
            # back that faces away from it but should be the same colour; a cut face, square to the surface, isn't
            ok &= np.abs(np.einsum("nkd,nd->nk", self.normals[safe], nrm)) > FACING
            first = ok.argmax(1)
            hit = ok[np.arange(len(p)), first]
            out[start:start + len(p)] = np.where(hit, self.labels[safe[np.arange(len(p)), first]], 0)
            found[start:start + len(p)] = hit
        return out, found


def surface_colours(source: TriMesh, textures: dict[int, np.ndarray], settings: PaintSettings | None = None,
                    progress: ProgressFn | None = None) -> SurfaceColours:
    """Read the colours of a textured model's surface for :func:`transfer_paint`: the palette (given, or
    suggested from the textures), then points spread over the surface about half a feature apart, each with
    the palette colour of the cleaned, quantised texture under it."""
    from scipy.spatial import cKDTree

    settings = settings or PaintSettings()
    faces = _textured_faces(source, textures)
    if not faces:
        raise ValueError("This model has no colour textures to paint from.")
    if settings.palette:
        palette = np.array([parse_colour(c) for c in settings.palette][:MAX_COLOURS], np.uint8)
        coverage = None
    else:
        palette, coverage = suggest_palette(source, textures, settings.colours, progress, settings)
    k = len(palette)
    feature = _feature(source, settings)
    verts = source.vertices[source.faces].astype(np.float64)
    longest = np.linalg.norm(np.roll(verts, -1, axis=1) - verts, axis=2).max(1)
    spacing = feature / 2
    want = np.ceil(longest / spacing)
    total = float((np.clip(want, 1, 64) ** 2).sum())
    if total > SOURCE_POINTS:
        spacing *= np.sqrt(total / SOURCE_POINTS)
    n = np.clip(np.ceil(longest / spacing), 1, 64).astype(np.int64)
    normals = source.face_normals()
    pts_all, nrm_all, lab_all = [], [], []
    for i, (mat, sel) in enumerate(faces.items()):
        _maybe(progress, 0.25 + 0.15 * i / len(faces), "Reading the model's colours")
        img = _shrink(_rgb(textures[mat]))
        index = cleanup(quantize(img, palette, settings), k, settings.cleanup)
        uv = source.corner_uvs[sel].astype(np.float32)
        for size in np.unique(n[sel]).tolist():
            part = sel[n[sel] == size]
            local = np.flatnonzero(n[sel] == size)
            bary = _lattice_cached(size)
            step = max(1, 2_000_000 // (size * size))
            for start in range(0, len(part), step):
                f = part[start:start + step]
                pts_all.append(np.matmul(bary[None], verts[f].astype(np.float32)).reshape(-1, 3))
                nrm_all.append(np.repeat(normals[f], size * size, axis=0).astype(np.float32))
                lab_all.append(_lookup(index, np.matmul(bary[None], uv[local[start:start + step]])).reshape(-1))
    textured = np.zeros(source.n_faces, bool)
    for sel in faces.values():
        textured[sel] = True
    plain = np.flatnonzero(~textured)
    if len(plain):  # triangles without a colour texture are the first filament, as on the textured model
        pts_all.append(verts[plain].mean(1).astype(np.float32))
        nrm_all.append(normals[plain].astype(np.float32))
        lab_all.append(np.zeros(len(plain), np.int16))
    points = np.concatenate(pts_all)
    height = _height(source)
    _maybe(progress, 0.42, "Indexing the model's colours")
    return SurfaceColours(palette, coverage, points, np.concatenate(nrm_all), np.concatenate(lab_all).astype(np.int16),
                          float(spacing), feature, max(3 * feature, 4 * float(spacing), 0.005 * height),
                          cKDTree(points))


def transfer_paint(target: TriMesh, colours: SurfaceColours, settings: PaintSettings | None = None,
                   progress: ProgressFn | None = None) -> Painting:
    """Paint a mesh without textures (the model made watertight, or a piece cut from it) from the textured
    model it was made from: each piece of each triangle takes the colour of the textured surface nearest it
    that faces the same way, split where those colours change, as :func:`paint_mesh` splits over a texture.
    Surface with nothing textured near it (cut faces, filled gaps, joints) takes the first filament."""
    settings = settings or PaintSettings()
    k = len(colours.palette)
    spacing = colours.spacing
    normals = target.face_normals().astype(np.float32)
    far = [0.0, 0.0]  # area with no textured surface near it, of all

    def sample(root, bary, xyz):
        longest = np.linalg.norm(np.roll(xyz, -1, axis=1) - xyz, axis=2).max(1)
        n = np.clip(np.ceil(longest / spacing), 1, LATTICE[-1]).astype(np.int64)
        n = np.asarray(LATTICE)[np.searchsorted(LATTICE, n)]  # the lattice size at or above it
        dominant = np.zeros(len(root), np.int16)
        wrong = np.zeros(len(root), np.int32)
        for size in np.unique(n).tolist():
            sel = np.flatnonzero(n == size)
            lattice = _LATTICES[size]
            step = max(1, 1_000_000 // (size * size))
            for start in range(0, len(sel), step):
                part = sel[start:start + step]
                pts = np.matmul(lattice[None], xyz[part].astype(np.float32)).reshape(-1, 3)
                nrm = np.repeat(normals[root[part]], size * size, axis=0)
                labels, found = colours.label_at(pts, nrm)
                vals = labels.reshape(len(part), size * size).astype(np.int64)
                d, w = _vote(vals, k)
                dominant[part], wrong[part] = d, w
                if sample.first:  # the share of the surface (by area) with no textured surface near it
                    t = xyz[part]
                    area = 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
                    missed = (~found).reshape(len(part), size * size).mean(1)
                    far[0] += float((missed * area).sum())
                    far[1] += float(area.sum())
        sample.first = False
        return dominant, wrong, longest > spacing

    sample.first = True
    verts = target.vertices[target.faces].astype(np.float32)
    _maybe(progress, 0.45, "Painting")
    got = _paint_faces(verts, target.face_areas(), sample, colours.feature, settings.max_depth, k)
    notes: list[str] = []
    if far[1] and far[0] / far[1] > 0.02:
        notes.append(f"{far[0] / far[1]:.0%} of the surface lies away from the textured model (cut faces, joints, "
                     "filled gaps) and takes the first filament.")
    if got.leaves > MANY_LEAVES:
        notes.append(f"The paint has {got.leaves / 1e6:.1f} million pieces, which can make the slicer slow: a larger "
                     "smallest feature, fewer colours or a stronger clean-up would mean fewer.")
    coverage = colours.coverage if colours.coverage is not None else got.held / max(got.held.sum(), 1e-12)
    _maybe(progress, 0.95, "Painted")
    return Painting(got.strings, colours.palette, coverage, got.split_faces, got.leaves, got.depth, 0, notes)


def _vote(vals: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """For rows of palette indices (M, S): the index most of each row has, and how many differ from it."""
    m, s = vals.shape
    keys = (np.arange(m, dtype=np.int64)[:, None] * k + vals).reshape(-1)
    counts = np.bincount(keys, minlength=m * k).reshape(m, k)
    best = counts.argmax(1)
    return best.astype(np.int16), (s - counts[np.arange(m), best]).astype(np.int32)


# ---------------------------------------------------------------------------
# the slicers' strings


def _nibbles_leaf(state: int) -> list[int]:
    """The digits of a leaf in filament slot ``state`` (0 = unpainted), in stream order."""
    if state < 3:
        return [state << 2]
    n = state - 3
    out = [0b1100]
    while n >= 15:
        out.append(0b1111)
        n -= 15
    out.append(n)
    return out


def _leaf_strings(k: int) -> list[str]:
    """Per slot 0..k: the leaf's string (digits reversed, as the slicers write them)."""
    return ["".join(f"{d:X}" for d in reversed(_nibbles_leaf(s))) for s in range(k + 1)]


def encode_tree(tree) -> str:
    """A tree as a slicer string: an int is a leaf's filament slot (1-based, 0 unpainted); a list of four
    trees is a triangle split at its edge midpoints into (v0, m01, m20), (m01, v1, m12), (m12, v2, m20),
    (m01, m12, m20); a tuple (special side, [two or three trees]) is a split of one or two sides (see
    :func:`_split_long`), as :func:`decode_string` returns them."""
    if isinstance(tree, int):
        return "".join(f"{d:X}" for d in reversed(_nibbles_leaf(tree)))
    if isinstance(tree, tuple):
        special, children = tree
        if len(children) not in (2, 3) or not 0 <= special <= 2:
            raise ValueError("A one- or two-sided split has a special side 0..2 and two or three children")
        return "".join(encode_tree(t) for t in children) + _DIGITS[(len(children) - 1) | (special << 2)]
    if len(tree) != 4:
        raise ValueError("A split has four children")
    return "".join(encode_tree(t) for t in tree) + "3"


def decode_string(text: str):
    """The tree a slicer string stands for (the inverse of :func:`encode_tree`, for checks and previews);
    one- and two-sided splits are returned as tuples (special side, [children])."""
    digits = [int(c, 16) for c in reversed(text)]
    pos = 0

    def nibble():
        nonlocal pos
        if pos >= len(digits):
            raise ValueError("Paint string ends early")
        d = digits[pos]
        pos += 1
        return d

    def node():
        code = nibble()
        sides = code & 0b11
        if sides == 0:
            if code & 0b1100 == 0b1100:
                extra = nibble()
                state = 3
                while extra == 0b1111:
                    state += 15
                    extra = nibble()
                return state + extra
            return code >> 2
        special = code >> 2
        children = [node() for _ in range(sides + 1)]
        children.reverse()
        return children if sides == 3 else (special, children)

    out = node()
    if pos != len(digits):
        raise ValueError("Paint string has digits left over")
    return out


def leaf_colours(tree, k: int) -> np.ndarray:
    """Area share of each slot 0..k in a tree (for checks)."""
    out = np.zeros(k + 1)
    if isinstance(tree, int):
        out[tree] = 1.0
        return out
    if isinstance(tree, tuple):
        children = tree[1]
        parts = (0.5, 0.5) if len(children) == 2 else (0.25, 0.25, 0.5)
    else:
        children, parts = tree, (0.25,) * 4
    for child, part in zip(children, parts):
        out += leaf_colours(child, k) * part
    return out
