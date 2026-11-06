"""Finishing a model for printing: post-processing on the whole model, after it is generated.

:func:`make_watertight` wraps the model into one watertight solid with nothing left inside it. Game models
are built from parts that overlap (an arm pushed into a sleeve, armour plates over a body, a head into a
hood) and from open surfaces (capes, single-sided cloth); a slicer given them as they are prints walls
where parts cross, or fails on the open edges. Here:

1. Every part is made solid and they are all united (:func:`darkstl.cut.make_solid`): where parts cross,
   the triangles inside the other part are removed, so only the outside surface stays, exactly as it was.
2. Hollows sealed inside the model (the space between a body and a closed coat over it) are filled, so the
   model is solid all through and no inside surfaces are left.
3. Optionally, gaps narrower than a width are closed (the slit between a sleeve and the arm in it): the
   model is wrapped in a voxel grid, the gaps bridged by closing it, and the bridges sunk a little below the
   real surface where there's no gap, so the detail outside stays as it was.
4. Optionally, loose bits (a floating buckle, specks) smaller than a share of the model are removed.

Sizes are in millimetres; meshes in and out are in metres (export frame), as everywhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .cut import _components, _from_manifold, _m, _n, _signed_volume, _to_manifold, available, make_solid
from .geometry.mesh import TriMesh

ProgressFn = Callable[[float, str], None]

__all__ = ["WatertightSettings", "FinishResult", "make_watertight", "settings_from_json", "available"]

MAX_CELLS = 32_000_000  # voxels for closing gaps: about 320 a side, some seconds


@dataclass
class WatertightSettings:
    sheet_mm: float = 1.2  # open surfaces are thickened to this
    fill_hollows: bool = True  # fill space sealed inside the model
    close_gaps_mm: float = 0.0  # close gaps up to this wide (0: off)
    loose_percent: float = 0.0  # remove loose bits smaller than this % of the model's volume (0: keep all)

    def key(self) -> tuple:
        return (round(self.sheet_mm, 4), self.fill_hollows, round(self.close_gaps_mm, 4), round(self.loose_percent, 4))


@dataclass
class FinishResult:
    mesh: TriMesh  # metres
    notes: list[str] = field(default_factory=list)
    volume_mm3: float = 0.0
    shells: int = 1  # separate solids in it
    source_triangles: int = 0
    hollows: int = 0
    gap_volume_mm3: float = 0.0
    loose_removed: int = 0
    source: TriMesh | None = None  # the textured model it was made from, to paint it from (set by the app)

    def stats(self) -> dict:
        lo, hi = self.mesh.bounds()
        return {"triangles": self.mesh.n_faces, "vertices": self.mesh.n_vertices, "source_triangles": self.source_triangles,
                "volume_cm3": round(self.volume_mm3 / 1000, 3), "shells": self.shells, "hollows": self.hollows,
                "loose_removed": self.loose_removed, "gap_cm3": round(self.gap_volume_mm3 / 1000, 3),
                "size_mm": ((hi - lo) * 1000).round(2).tolist(), "watertight": True}


def _shells(solid) -> list[tuple[np.ndarray, np.ndarray, float]]:
    """The closed surfaces a solid is bounded by: (vertices, faces, signed volume) each; a hollow inside
    the solid is bounded by a surface facing inwards, so its volume is negative."""
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties, np.float64)[:, :3]
    f = np.asarray(mesh.tri_verts, np.int64)
    if not len(f):
        return []
    labels = _components(len(v), f)
    order = np.argsort(labels, kind="stable")
    cuts = np.flatnonzero(np.diff(labels[order])) + 1
    out = []
    for sel in np.split(order, cuts):
        used, inv = np.unique(f[sel], return_inverse=True)
        pv, pf = v[used], inv.reshape(-1, 3)
        out.append((pv, pf, _signed_volume(pv, pf)))
    return out


def fill_hollows(solid) -> tuple[object, int, float]:
    """The solid with every hollow sealed inside it filled: (solid, hollows filled, their volume)."""
    m3 = _m()
    shells = _shells(solid)
    inner = [s for s in shells if s[2] < 0]
    if not inner:
        return solid, 0, 0.0
    outer = [_to_manifold(pv, pf) for pv, pf, vol in shells if vol > 0]
    outer = [s for s in outer if s is not None]
    if not outer:  # pragma: no cover - a solid always has an outside
        return solid, 0, 0.0
    filled = m3.Manifold.batch_boolean(outer, m3.OpType.Add) if len(outer) > 1 else outer[0]
    return filled, len(inner), max(float(filled.volume() - solid.volume()), 0.0)


def _occupancy(solid, h: float, pad: int):
    """The solid as voxels of size ``h``: (grid, origin), a voxel set where its centre is inside the solid.
    Each layer of voxels is the solid's cross-section through their centres, filled in (even-odd over its
    outlines, so holes stay holes)."""
    from PIL import Image, ImageDraw

    bb = solid.bounding_box()
    lo, hi = np.asarray(bb[:3], np.float64), np.asarray(bb[3:], np.float64)
    origin = lo - pad * h
    shape = tuple(int(x) for x in np.ceil((hi - lo) / h).astype(int) + 2 * pad + 1)
    nx, ny, nz = shape
    grid = np.zeros(shape, bool)
    for k in range(nz):
        z = origin[2] + (k + 0.5) * h
        if z <= lo[2] or z >= hi[2]:
            continue
        layer = np.zeros((ny, nx), bool)
        for poly in solid.slice(float(z)).to_polygons():
            p = np.asarray(poly, np.float64)
            if len(p) < 3:
                continue
            img = Image.new("1", (nx, ny), 0)
            # pixel (i, j) covers [i, i + 1): its centre is the voxel's
            ImageDraw.Draw(img).polygon([((x - origin[0]) / h - 0.5, (y - origin[1]) / h - 0.5) for x, y in p], fill=1)
            layer ^= np.asarray(img, bool)
        grid[:, :, k] = layer.T
    return grid, origin


def _ball(r: int) -> np.ndarray:
    k = np.arange(-r, r + 1)
    x, y, z = np.meshgrid(k, k, k, indexing="ij")
    return x * x + y * y + z * z <= r * r + r  # a little rounder than the strict sphere at small radii


def _field_sdf(field_: np.ndarray, origin: np.ndarray, h: float):
    """A function of a point giving ``field_`` (voxel values, 1 inside) interpolated there, less one half."""
    nx, ny, nz = field_.shape
    flat = field_.ravel().tolist()
    inv = 1.0 / h
    ox, oy, oz = (float(x) + h / 2 for x in origin)  # voxel centres sit half a voxel in from their corner

    def sdf(x, y, z):
        gx, gy, gz = (x - ox) * inv, (y - oy) * inv, (z - oz) * inv
        i, j, k = int(gx), int(gy), int(gz)
        if i < 0 or j < 0 or k < 0 or i >= nx - 1 or j >= ny - 1 or k >= nz - 1 or gx < 0 or gy < 0 or gz < 0:
            return -0.5
        fx, fy, fz = gx - i, gy - j, gz - k
        b = (i * ny + j) * nz + k
        c000, c001 = flat[b], flat[b + 1]
        c010, c011 = flat[b + nz], flat[b + nz + 1]
        b2 = b + ny * nz
        c100, c101 = flat[b2], flat[b2 + 1]
        c110, c111 = flat[b2 + nz], flat[b2 + nz + 1]
        c00 = c000 + (c001 - c000) * fz
        c01 = c010 + (c011 - c010) * fz
        c10 = c100 + (c101 - c100) * fz
        c11 = c110 + (c111 - c110) * fz
        c0 = c00 + (c01 - c00) * fy
        c1 = c10 + (c11 - c10) * fy
        return c0 + (c1 - c0) * fx - 0.5

    return sdf


def gap_grid(span_mm, gap_mm: float) -> float | None:
    """The voxel size gaps up to ``gap_mm`` are closed on for a model ``span_mm`` big: a quarter of the gap,
    coarser for a big model, but no coarser than half of it (None: the model is too big for gaps that narrow)."""
    span = np.maximum(np.asarray(span_mm, np.float64), 1e-6)
    h = gap_mm / 4
    if np.prod(span / h + 1) > MAX_CELLS:
        h = float((np.prod(span) / MAX_CELLS) ** (1 / 3))
    return h if h <= gap_mm / 2 else None


def narrowest_gap(span_mm) -> float:
    """The narrowest gaps that can be closed on a model ``span_mm`` big, in mm."""
    span = np.maximum(np.asarray(span_mm, np.float64), 1e-6)
    return float(math.ceil(2 * (np.prod(span) / MAX_CELLS) ** (1 / 3) * 10) / 10)


def close_gaps(solid, gap_mm: float, progress: ProgressFn | None = None) -> tuple[object, float, str | None]:
    """The solid with gaps up to ``gap_mm`` wide bridged: (solid, volume added, a note when it couldn't be)."""
    from scipy import ndimage

    m3 = _m()
    bb = solid.bounding_box()
    span = np.asarray(bb[3:]) - np.asarray(bb[:3])
    h = gap_grid(span, gap_mm)
    if h is None:
        return solid, 0.0, (f"Gaps weren't closed: the model is too big ({span.max():.0f} mm) to find gaps "
                            f"{gap_mm:g} mm wide. Gaps from {narrowest_gap(span):g} mm can be closed on it.")
    r = max(1, int(math.ceil(gap_mm / 2 / h)))
    if progress:
        progress(0.1, "Wrapping the model in voxels")
    grid, origin = _occupancy(solid, h, r + 4)
    ball = _ball(r)
    if progress:
        progress(0.3, "Finding the gaps")
    closed = ndimage.binary_erosion(ndimage.binary_dilation(grid, ball), ball)
    closed = ndimage.binary_fill_holes(closed)
    gaps = closed & ~grid  # the voxels the closing filled: the gaps (and grooves) themselves
    if not gaps.any():
        return solid, 0.0, None
    # the bridges: the gaps grown into the model on either side, so they're thick enough to keep their shape
    # and hold on to it, but kept two voxels under the outside the closing makes, so they only show in a gap
    bridge = ndimage.binary_dilation(gaps, iterations=3) & ndimage.binary_erosion(closed, iterations=2)
    labels, count = ndimage.label(bridge)
    if not count:
        return solid, 0.0, None
    blobs = []
    for n, box in enumerate(ndimage.find_objects(labels)):
        if progress and n % 8 == 0:
            progress(0.45 + 0.4 * n / count, f"Bridging the gaps: {n + 1} of {count}")
        lo = np.array([max(b.start - 2, 0) for b in box])
        hi = np.array([min(b.stop + 2, s) for b, s in zip(box, labels.shape)])
        sub = tuple(slice(a, b) for a, b in zip(lo, hi))
        local = ndimage.gaussian_filter((labels[sub] == n + 1).astype(np.float32), 0.7)
        at = origin + lo * h
        blob = m3.Manifold.level_set(_field_sdf(local, at, h), [*(at - h).tolist(), *(at + (hi - lo + 1) * h).tolist()], h)
        # only bridges that hold on to the model (a stray speck of the grid isn't one)
        if blob.status() == m3.Error.NoError and not blob.is_empty() and (blob ^ solid).volume() > 0:
            blobs.append(blob)
    if not blobs:
        return solid, 0.0, None
    if progress:
        progress(0.88, "Joining the bridges to the model")
    out = m3.Manifold.batch_boolean([solid, *blobs], m3.OpType.Add)
    return out, max(float(out.volume() - solid.volume()), 0.0), None


def remove_loose(solid, percent: float) -> tuple[object, int]:
    """The solid without separate bits smaller than ``percent`` of the biggest: (solid, bits removed)."""
    m3 = _m()
    parts = solid.decompose()
    if len(parts) < 2 or percent <= 0:
        return solid, 0
    vols = [p.volume() for p in parts]
    keep_min = max(vols) * percent / 100
    kept = [p for p, vol in zip(parts, vols) if vol >= keep_min]
    if len(kept) == len(parts):
        return solid, 0
    return (m3.Manifold.compose(kept) if len(kept) > 1 else kept[0]), len(parts) - len(kept)


def make_watertight(mesh: TriMesh, settings: WatertightSettings | None = None,
                    progress: ProgressFn | None = None) -> FinishResult:
    """The model as one watertight solid with nothing inside it. See the module's notes."""
    s = settings or WatertightSettings()
    sub = (lambda lo, hi: (lambda f, m: progress(lo + (hi - lo) * f, m))) if progress else (lambda lo, hi: None)
    gaps = s.close_gaps_mm > 0
    solid = make_solid(mesh, s.sheet_mm, sub(0, 0.55 if gaps else 0.85))
    notes = [n for n in solid.notes if not n.endswith("joined into one solid.")]
    m = solid.manifold
    if solid.parts > 1:
        notes.append(f"{solid.parts} parts joined into one; the surfaces inside one another removed.")
    hollows, hollow_vol = 0, 0.0
    if s.fill_hollows:
        if progress:
            progress(0.86 if not gaps else 0.56, "Filling hollows inside the model")
        m, hollows, hollow_vol = fill_hollows(m)
        if hollows:
            notes.append(f"{_n(hollows, 'hollow')} sealed inside the model filled ({_cm3(hollow_vol)} cm³).")
    gap_vol = 0.0
    if gaps:
        m, gap_vol, note = close_gaps(m, s.close_gaps_mm, sub(0.6, 0.95))
        if note:
            notes.append(note)
        elif gap_vol > 0:
            notes.append(f"Gaps up to {s.close_gaps_mm:g} mm wide closed ({_cm3(gap_vol)} cm³ added).")
        else:
            notes.append(f"No gaps up to {s.close_gaps_mm:g} mm wide were found.")
        if s.fill_hollows:  # bridging seals off pockets (and specks of space where a bridge meets the surface)
            m, more, _ = fill_hollows(m)
            hollows += more
    loose = 0
    if s.loose_percent > 0:
        m, loose = remove_loose(m, s.loose_percent)
        if loose:
            notes.append(f"{_n(loose, 'loose bit')} smaller than {s.loose_percent:g}% of the model removed.")
    shells = len(m.decompose())
    if shells > 1:
        notes.append(f"{shells} separate pieces don't touch the rest; each is watertight.")
    if progress:
        progress(0.98, "Done")
    return FinishResult(_from_manifold(m), notes, float(m.volume()), shells, mesh.n_faces, hollows, gap_vol, loose)


def _cm3(mm3: float) -> str:
    cm3 = mm3 / 1000
    return f"{cm3:,.0f}" if cm3 >= 100 else f"{cm3:.3g}"


def settings_from_json(data: dict) -> WatertightSettings:
    data = data or {}
    return WatertightSettings(
        sheet_mm=_clamp(data.get("sheet_mm", 1.2), 0.2, 20),
        fill_hollows=bool(data.get("fill_hollows", True)),
        close_gaps_mm=_clamp(data.get("close_gaps_mm", 0.0), 0, 50),
        loose_percent=_clamp(data.get("loose_percent", 0.0), 0, 100),
    )


def _clamp(value, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = lo
    if not math.isfinite(v):
        v = lo
    return min(max(v, lo), hi)


def is_watertight(mesh: TriMesh) -> bool:
    """Whether the mesh (metres) is one closed, consistently facing surface, with no edge shared by more or
    fewer than two triangles."""
    return _to_manifold(mesh.vertices * 1000.0, mesh.faces) is not None
