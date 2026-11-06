"""Cut a model into pieces for printing, with joints between them.

The model is made solid first (:func:`make_solid`): game meshes are open shells with overlapping
parts, and a cut through an open shell has no inside to close. Each connected part is taken as
it is when it is already closed; small openings (a neck, a sleeve end) are closed with a fan;
open surfaces (capes, single-sided cloth) are thickened into a shell; whatever is still broken
is rebuilt from voxels. The parts are then united into one solid (manifold3d), which every cut
splits exactly, so each piece comes out watertight.

Each cut is a plane with a joint for the faces it makes (:class:`Joint`):

- ``flat``: the faces as they are, to glue.
- ``pegs``: round pegs on one side, holes on the other, the holes wider by the clearance.
- ``dowels``: holes on both sides and separate dowel pieces (or bought pins / filament).
- ``magnets``: round pockets on both sides for disc magnets, with the clearance around them.
- ``key``: a tongue in the shape of the cut face itself (inset from the edge), and a groove
  on the other side, so the pieces only go together one way.

Features are placed per island of the cut face (a leg and a sleeve cut by one plane each get
their own), as far from the edge as they can be; one that would break through the far side of
a piece is made shorter, or left out with a note. All sizes are in millimetres; the model's
own units are metres (export frame, Z up), as everywhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable

import numpy as np

from .geometry.mesh import TriMesh, weld

ProgressFn = Callable[[float, str], None]

JOINTS = ("flat", "pegs", "dowels", "magnets", "key")
EMBED = 0.3  # mm a feature reaches past the cut face into the piece it joins, so unions don't meet on a plane
MAX_FEATURES = 8
NUDGE = 1.3e-3  # mm every plane is moved along its normal (see cut_solid)


def available() -> bool:
    try:
        import manifold3d  # noqa: F401
    except ImportError:
        return False
    return True


def _m():
    try:
        import manifold3d
    except ImportError as e:  # pragma: no cover - a dependency, but say so clearly if missing
        raise RuntimeError("Cutting needs the 'manifold3d' package (pip install manifold3d).") from e
    return manifold3d


# ------------------------------------------------------------------ settings


@dataclass
class Joint:
    kind: str = "pegs"
    clearance_mm: float = 0.2  # gap on each side between parts that slide together
    count: int = 0  # features per island of the cut face; 0 = as many as suit it (up to 2)
    diameter_mm: float = 5.0  # pegs and dowels
    depth_mm: float = 6.0  # how far a peg, dowel or key goes into a piece
    magnet_diameter_mm: float = 6.0
    magnet_height_mm: float = 3.0
    flip: bool = False  # pegs and key on the other side of the cut
    print_dowels: bool = True  # dowels: also make the dowels as pieces


@dataclass
class CutPlane:
    normal: np.ndarray  # unit vector; the side it points to is the "first" side
    point: np.ndarray  # a point on the plane, in millimetres (export frame)
    joint: Joint = field(default_factory=Joint)

    def __post_init__(self):
        n = np.asarray(self.normal, np.float64).reshape(3)
        length = float(np.linalg.norm(n))
        if not np.isfinite(length) or length < 1e-9:
            raise ValueError("A cut needs a direction.")
        self.normal = n / length
        self.point = np.asarray(self.point, np.float64).reshape(3)

    @property
    def offset(self) -> float:
        return float(self.normal @ self.point)


@dataclass
class CutSettings:
    cuts: list[CutPlane] = field(default_factory=list)
    sheet_mm: float = 1.2  # open surfaces are thickened to this
    wall_mm: float = 1.6  # least material kept between a feature and the outside


# ------------------------------------------------------------------ results


@dataclass
class Piece:
    name: str
    mesh: TriMesh  # metres, as the rest of the app
    kind: str = "piece"  # piece | dowel
    faces: list[tuple[np.ndarray, float, bool]] = field(default_factory=list)  # cut faces: (outward normal, offset mm, has pegs/tongue)
    volume_mm3: float = 0.0

    def stats(self) -> dict:
        lo, hi = self.mesh.bounds()
        return {"name": self.name, "kind": self.kind, "triangles": self.mesh.n_faces,
                "volume_cm3": round(self.volume_mm3 / 1000, 3),
                "size_mm": ((hi - lo) * 1000).round(2).tolist(), "center_mm": ((hi + lo) * 500).round(3).tolist()}


@dataclass
class CutResult:
    pieces: list[Piece]
    notes: list[str] = field(default_factory=list)  # what making it solid and placing the joints did
    joints: list[dict] = field(default_factory=list)  # per cut: what was placed

    def combined(self) -> TriMesh:
        """All pieces in one mesh, each piece's triangles carrying its index as their material."""
        from .geometry.mesh import concatenate

        parts = []
        for i, p in enumerate(self.pieces):
            m = TriMesh(p.mesh.vertices, p.mesh.faces, face_material=np.full(p.mesh.n_faces, i, np.int32))
            parts.append(m)
        return concatenate(parts)


@dataclass
class Solid:
    manifold: object  # manifold3d.Manifold, millimetres
    notes: list[str] = field(default_factory=list)
    parts: int = 1  # the solids joined into it


# ------------------------------------------------------------------ making it solid


def _to_manifold(vertices: np.ndarray, faces: np.ndarray):
    m3 = _m()
    mesh = m3.Mesh(np.ascontiguousarray(vertices, np.float32), np.ascontiguousarray(faces, np.uint32))
    solid = m3.Manifold(mesh)
    if solid.status() != m3.Error.NoError or solid.is_empty():
        return None
    if solid.volume() < 0:  # pragma: no cover - manifold keeps orientation; inside-out parts are flipped first
        return None
    return solid


def _from_manifold(solid) -> TriMesh:
    mesh = solid.to_mesh()
    v = np.asarray(mesh.vert_properties, np.float64)[:, :3] / 1000.0
    return TriMesh(v, np.asarray(mesh.tri_verts, np.int64))


def _components(n_vertices: int, faces: np.ndarray) -> np.ndarray:
    """The connected part (by shared vertices) each face belongs to."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    a = np.concatenate([faces[:, 0], faces[:, 1]])
    b = np.concatenate([faces[:, 1], faces[:, 2]])
    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n_vertices, n_vertices))
    _, labels = connected_components(g, directed=False)
    return labels[faces[:, 0]]


def _edge_components(faces: np.ndarray) -> np.ndarray:
    """The connected part each face belongs to, faces joined only through a shared edge: two surfaces that
    merely touch at a vertex (an armour plate's corner on a body) stay apart."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(faces)
    if not n:
        return np.zeros(0, np.int64)
    e = np.sort(np.stack([faces, np.roll(faces, -1, axis=1)], axis=2).reshape(-1, 2), axis=1)
    _, edge = np.unique(e, axis=0, return_inverse=True)
    edge = edge.reshape(-1)
    face = np.repeat(np.arange(n), 3)
    m = int(edge.max()) + 1
    g = coo_matrix((np.ones(len(face), np.int8), (face, n + edge)), shape=(n + m, n + m))
    _, labels = connected_components(g, directed=False)
    return labels[:n]


def _signed_volume(v: np.ndarray, f: np.ndarray) -> float:
    t = v[f]
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def _edge_counts(f: np.ndarray):
    """Directed half-edges (a, b) of the faces, and for each how often it and its reverse occur."""
    he = np.stack([f, np.roll(f, -1, axis=1)], axis=2).reshape(-1, 2)
    n = int(f.max()) + 1 if len(f) else 1
    key = he[:, 0] * n + he[:, 1]
    rkey = he[:, 1] * n + he[:, 0]
    uniq, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    same = counts[inv]
    pos = np.searchsorted(uniq, rkey)
    pos = np.minimum(pos, len(uniq) - 1)
    reverse = np.where(uniq[pos] == rkey, counts[pos], 0)
    return he, same, reverse


def _boundary_loops(he: np.ndarray, boundary: np.ndarray) -> list[list[int]] | None:
    """Walk the boundary half-edges into loops; None when a vertex starts two of them (not simple)."""
    nxt: dict[int, int] = {}
    for a, b in he[boundary]:
        a, b = int(a), int(b)
        if a in nxt:
            return None
        nxt[a] = b
    loops, seen = [], set()
    for start in nxt:
        if start in seen:
            continue
        loop, v = [], start
        while v not in seen:
            seen.add(v)
            loop.append(v)
            v = nxt.get(v)
            if v is None:
                return None
        if v != start:
            return None
        loops.append(loop)
    return loops


def _fill_loops(v: np.ndarray, f: np.ndarray, loops: list[list[int]]):
    """Close each loop with a fan of triangles to a new vertex at its centre."""
    verts, faces = [v], [f]
    n = len(v)
    area = 0.0
    for loop in loops:
        idx = np.asarray(loop, np.int64)
        c = v[idx].mean(0)
        verts.append(c[None])
        a, b = idx, np.roll(idx, -1)
        # the boundary half-edge a->b belongs to an existing face, so the new one runs b->a
        fan = np.stack([b, a, np.full(len(idx), n)], axis=1)
        faces.append(fan)
        t = np.concatenate([v, c[None]])[fan]
        area += float(0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).sum())
        n += 1
    return np.concatenate(verts), np.concatenate(faces), area


def _thicken(v: np.ndarray, f: np.ndarray, he: np.ndarray, boundary: np.ndarray, t: float):
    """An open surface as a shell ``t`` thick: the surface, a copy moved inwards (against its normals)
    facing the other way, and a strip joining them along the open edges."""
    mesh = TriMesh(v, f)
    nrm = mesh.vertex_normals()
    inner = v - nrm * t
    n = len(v)
    back = f[:, ::-1] + n
    a, b = he[boundary, 0], he[boundary, 1]
    side = np.concatenate([np.stack([b, a, a + n], 1), np.stack([b, a + n, b + n], 1)])
    return np.concatenate([v, inner]), np.concatenate([f, back, side])


def _voxel_solid(v: np.ndarray, f: np.ndarray, sheet: float):
    """Rebuild a broken part from voxels: its surface marked in a grid, enclosed space filled, and the
    surface of that taken again. Loses detail finer than the grid, so it is only the last resort."""
    from scipy import ndimage

    m3 = _m()
    lo, hi = v.min(0), v.max(0)
    span = float((hi - lo).max())
    h = max(sheet / 2, span / 160, 1e-3)
    pad = 3
    origin = lo - pad * h
    shape = tuple(int(x) for x in np.ceil((hi - lo) / h).astype(int) + 2 * pad + 1)
    grid = np.zeros(shape, bool)
    tri = v[f]
    areas = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    longest = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2).max(1)
    # enough points to cover each triangle, and its length too when it's a sliver narrower than a voxel
    per = np.clip(np.ceil(np.maximum(areas / (h * h) * 4, longest / h * 2)), 1, 200_000).astype(int)
    rng = np.random.default_rng(0)
    rep = np.repeat(np.arange(len(f)), per)
    r1, r2 = rng.random(len(rep)), rng.random(len(rep))
    flip = r1 + r2 > 1
    r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
    t = tri[rep]
    pts = t[:, 0] + r1[:, None] * (t[:, 1] - t[:, 0]) + r2[:, None] * (t[:, 2] - t[:, 0])
    pts = np.concatenate([pts, v])
    ijk = np.clip(np.round((pts - origin) / h).astype(int), 0, np.array(shape) - 1)
    grid[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True
    grid = ndimage.binary_dilation(grid, iterations=max(1, int(round(sheet / 2 / h))))
    grid = ndimage.binary_fill_holes(grid)
    field_ = ndimage.gaussian_filter(grid.astype(np.float32), 0.8)
    nx, ny, nz = shape
    flat = field_.ravel().tolist()
    ox, oy, oz = (float(x) for x in origin)
    inv = 1.0 / h

    def sdf(x, y, z):
        gx, gy, gz = (x - ox) * inv, (y - oy) * inv, (z - oz) * inv
        i, j, k = int(gx), int(gy), int(gz)
        if i < 0 or j < 0 or k < 0 or i >= nx - 1 or j >= ny - 1 or k >= nz - 1:
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

    end = origin + (np.array(shape) - 1) * h
    solid = m3.Manifold.level_set(sdf, [*origin.tolist(), *end.tolist()], h * 1.2)
    if solid.status() != m3.Error.NoError or solid.is_empty():
        return None
    return solid


def _part_solid(pv: np.ndarray, pf: np.ndarray, sheet_mm: float):
    """One connected part as a solid, and how: ("closed" | "filled" | "thickened", solid), or (None, None)
    when it isn't a clean surface (left to the voxels)."""
    he, same, reverse = _edge_counts(pf)
    if not ((same == 1).all() and (reverse <= 1).all()):
        return None, None
    boundary = reverse == 0
    if not boundary.any():
        if _signed_volume(pv, pf) < 0:
            pf = pf[:, ::-1]
        solid = _to_manifold(pv, pf)
        return ("closed", solid) if solid is not None else (None, None)
    loops = _boundary_loops(he, boundary)
    area = float(TriMesh(pv, pf).surface_area())
    if loops is not None:
        fv, ff, fill_area = _fill_loops(pv, pf, loops)
        vol = _signed_volume(fv, ff)
        # small openings are closed; a big one means an open surface (a cape), thickened instead
        if fill_area < 0.3 * area and abs(vol) > fill_area * sheet_mm * 0.5:
            if vol < 0:
                ff = ff[:, ::-1]
            solid = _to_manifold(fv, ff)
            if solid is not None:
                return "filled", solid
    tv, tf = _thicken(pv, pf, he, boundary, sheet_mm)
    solid = _to_manifold(tv, tf)
    return ("thickened", solid) if solid is not None else (None, None)


def _parts(v: np.ndarray, f: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """The connected parts of a surface (by shared edges), each with its own vertices."""
    labels = _edge_components(f)
    order = np.argsort(labels, kind="stable")
    cuts = np.flatnonzero(np.diff(labels[order])) + 1
    out = []
    for sel in np.split(order, cuts):
        used, inv = np.unique(f[sel], return_inverse=True)
        out.append((v[used], inv.reshape(-1, 3)))
    return out


def make_solid(mesh: TriMesh, sheet_mm: float = 1.2, progress: ProgressFn | None = None) -> Solid:
    """The model as one closed solid, in millimetres, ready to cut. See the module's notes."""
    m3 = _m()
    if mesh.n_faces == 0:
        raise ValueError("There's nothing to cut: no parts are switched on.")
    welded = weld(TriMesh(mesh.vertices * 1000.0, mesh.faces, face_group=mesh.face_group), 1e-4)
    v, f, g = welded.vertices, welded.faces, welded.face_group
    # the same triangle twice (two-sided cloth is often one side and its mirror) is kept once
    _, first = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
    keep = np.sort(first)
    f, g = f[keep], g[keep]
    labels = _edge_components(f)
    order = np.argsort(labels, kind="stable")
    cuts = np.flatnonzero(np.diff(labels[order])) + 1
    groups = np.split(order, cuts)
    solids = []
    count = {"closed": 0, "filled": 0, "thickened": 0, "rebuilt": 0, "dropped": 0}
    total = len(groups)
    for gi, sel in enumerate(groups):
        if progress and (gi % 16 == 0):
            progress(gi / max(total, 1), f"Making the model solid: part {gi + 1} of {total}")
        used, inv = np.unique(f[sel], return_inverse=True)
        pv, pf = v[used], inv.reshape(-1, 3)
        how, solid = _part_solid(pv, pf, sheet_mm)
        made = [(how, solid)] if solid is not None else []
        if solid is None and len(np.unique(g[sel])) > 1:
            # sub-meshes of the game model that only touch at a few welded points (an armour plate resting on a
            # body) make one tangled part; each on its own is often clean, and keeps its detail
            made = []
            for k in np.unique(g[sel]):
                sub = sel[g[sel] == k]
                used_k, inv_k = np.unique(f[sub], return_inverse=True)
                for qv, qf in _parts(v[used_k], inv_k.reshape(-1, 3)):
                    how_k, solid_k = _part_solid(qv, qf, sheet_mm)
                    if solid_k is None:
                        solid_k = _voxel_solid(qv, qf, sheet_mm)
                        how_k = "rebuilt" if solid_k is not None else "dropped"
                    if solid_k is None:
                        count["dropped"] += 1
                        continue
                    made.append((how_k, solid_k))
        if not made:
            solid = _voxel_solid(pv, pf, sheet_mm)
            if solid is None:
                count["dropped"] += 1
                continue
            made = [("rebuilt", solid)]
        for how_k, solid_k in made:
            count[how_k] += 1
            solids.append(solid_k)
    if not solids:
        raise ValueError("None of the model's parts could be made solid.")
    if progress:
        progress(0.95, "Joining the parts into one solid")
    whole = m3.Manifold.batch_boolean(solids, m3.OpType.Add) if len(solids) > 1 else solids[0]
    filled, thickened, rebuilt, dropped = count["filled"], count["thickened"], count["rebuilt"], count["dropped"]
    notes = []
    if filled:
        notes.append(f"{_n(filled, 'part')} had openings, closed flat.")
    if thickened:
        notes.append(f"{_n(thickened, 'open surface')} thickened to {sheet_mm:g} mm.")
    if rebuilt:
        notes.append(f"{_n(rebuilt, 'broken part')} rebuilt from voxels, with less detail.")
    if dropped:
        notes.append(f"{_n(dropped, 'part')} couldn't be made solid and {'was' if dropped == 1 else 'were'} left out.")
    if len(solids) > 1:
        notes.append(f"{len(solids)} parts joined into one solid.")
    return Solid(whole, notes, len(solids))


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ------------------------------------------------------------------ cutting


def _frame(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors across the plane, so (e1, e2, normal) is right-handed."""
    a = np.eye(3)[int(np.argmin(np.abs(normal)))]
    e1 = np.cross(normal, a)
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(normal, e1)


def _to_plane(cut: CutPlane) -> np.ndarray:
    """3x4 matrix taking model millimetres to the plane's frame (u, v across it, w along the normal, 0 on it)."""
    e1, e2 = _frame(cut.normal)
    m = np.zeros((3, 4))
    m[0, :3], m[1, :3], m[2, :3] = e1, e2, cut.normal
    m[2, 3] = -cut.offset
    return m


def _from_plane(cut: CutPlane) -> np.ndarray:
    e1, e2 = _frame(cut.normal)
    m = np.zeros((3, 4))
    m[:, 0], m[:, 1], m[:, 2] = e1, e2, cut.normal
    m[:, 3] = cut.normal * cut.offset
    return m


def _inside(points: np.ndarray, polys: list[np.ndarray]) -> np.ndarray:
    """Even-odd point-in-polygon over all the contours (outer and holes)."""
    out = np.zeros(len(points), bool)
    px, py = points[:, 0][:, None], points[:, 1][:, None]
    for poly in polys:
        p = np.asarray(poly, np.float64)
        if len(p) < 3:
            continue
        x1, y1 = p[:, 0][None], p[:, 1][None]
        x2, y2 = np.roll(p[:, 0], -1)[None], np.roll(p[:, 1], -1)[None]
        for s in range(0, len(points), 2048):
            sx, sy = px[s:s + 2048], py[s:s + 2048]
            cond = (y1 > sy) != (y2 > sy)
            with np.errstate(divide="ignore", invalid="ignore"):
                xi = x1 + (sy - y1) * (x2 - x1) / (y2 - y1)
            out[s:s + 2048] ^= (np.count_nonzero(cond & (sx < xi), axis=1) % 2).astype(bool)
    return out


def _edge_distance(points: np.ndarray, polys: list[np.ndarray]) -> np.ndarray:
    d = np.full(len(points), np.inf)
    for poly in polys:
        a = np.asarray(poly, np.float64)
        b = np.roll(a, -1, axis=0)
        ab = b - a
        ll = np.maximum((ab ** 2).sum(1), 1e-12)
        for s in range(0, len(points), 1024):
            p = points[s:s + 1024, None, :]
            t = np.clip(((p - a[None]) * ab[None]).sum(2) / ll[None], 0, 1)
            q = a[None] + t[..., None] * ab[None]
            d[s:s + 1024] = np.minimum(d[s:s + 1024], np.sqrt(((p - q) ** 2).sum(2)).min(1))
    return d


def place_features(island, radius: float, wall: float, count: int, spacing: float) -> list[np.ndarray]:
    """Centres for ``count`` round features (0: as many as suit it, up to 2) of ``radius`` inside a cross-section
    island, each at least ``wall`` from its edge and ``spacing`` from the others, as spread out as they can be."""
    m3 = _m()
    inner = island.offset(-(radius + wall), m3.JoinType.Round)
    if inner.is_empty() or inner.area() <= 0:
        return []
    polys = [np.asarray(p) for p in inner.to_polygons()]
    lo = np.min([p.min(0) for p in polys], axis=0)
    hi = np.max([p.max(0) for p in polys], axis=0)
    step = max(radius / 3, math.sqrt(max(inner.area(), 1e-9) / 3000), 0.05)
    xs = np.arange(lo[0], hi[0] + step, step)
    ys = np.arange(lo[1], hi[1] + step, step)
    gx, gy = np.meshgrid(xs, ys)
    cand = np.column_stack([gx.ravel(), gy.ravel()])
    cand = cand[_inside(cand, polys)]
    if not len(cand):
        # a sliver: its own vertices are inside it
        cand = np.concatenate(polys)
    outer = [np.asarray(p) for p in island.to_polygons()]
    depth = _edge_distance(cand, outer)
    want = min(count if count > 0 else 2, MAX_FEATURES)
    deepest = cand[int(np.argmax(depth))]
    if want == 1:
        return [deepest]
    # spread out from one end of the room left: the candidate furthest from its middle, then furthest from that...
    chosen = [cand[int(np.argmax(np.linalg.norm(cand - deepest, axis=1)))]]
    while len(chosen) < want:
        d = np.min([np.linalg.norm(cand - c, axis=1) for c in chosen], axis=0)
        # spread out, but not onto the very edge of the room left
        score = np.where(d >= spacing, d + 0.25 * depth, -np.inf)
        best = int(np.argmax(score))
        if not np.isfinite(score[best]):
            break
        chosen.append(cand[best])
    if len(chosen) == 1 or (count == 0 and np.linalg.norm(chosen[0] - chosen[1]) < spacing * 1.2):
        return [deepest]  # one only: in the middle, where there's most room
    return chosen


def _cylinder(radius: float, w0: float, w1: float, at: np.ndarray):
    m3 = _m()
    c = m3.Manifold.cylinder(w1 - w0, radius, radius, max(24, min(96, int(radius * 12))))
    return c.translate((float(at[0]), float(at[1]), w0))


def _contained(piece, feature, need: float = 0.995) -> bool:
    """Whether ``feature`` lies inside ``piece`` (so a hole there doesn't break through)."""
    vol = feature.volume()
    if vol <= 0:
        return False
    return (piece ^ feature).volume() >= need * vol


def _sided(w0: float, w1: float, side: int) -> tuple[float, float]:
    """The span w0..w1 (on the positive side) mirrored to ``side`` (+1 or -1)."""
    return (w0, w1) if side > 0 else (-w1, -w0)


def _joint_features(cut: CutPlane, pos, neg, wall: float):
    """The features of one cut's joint, in the plane's frame: (added to the positive side, cut from it,
    added to the negative side, cut from it, dowel pieces, what was placed)."""
    m3 = _m()
    j = cut.joint
    to_plane = _to_plane(cut)
    p_local, n_local = pos.transform(to_plane), neg.transform(to_plane)
    section = p_local.slice(1e-4) if not p_local.is_empty() else m3.CrossSection()
    if section.is_empty():
        section = n_local.slice(-1e-4)
    add = {1: [], -1: []}
    sub = {1: [], -1: []}
    dowels, placed, skipped = [], 0, 0
    local = {1: p_local, -1: n_local}
    male = -1 if j.flip else 1  # pegs and the tongue stand on this side, into the other
    clr = max(j.clearance_mm, 0.0)
    if j.kind == "flat" or section.is_empty():
        return add, sub, dowels, {"placed": 0, "skipped": 0}
    islands = [s for s in section.decompose() if s.area() > 0.5]
    for island in islands:
        if j.kind == "key":
            depth = j.depth_mm
            tongue = island.offset(-wall, m3.JoinType.Round).simplify(0.02)
            groove = island.offset(-wall + clr, m3.JoinType.Round).simplify(0.02)
            if tongue.is_empty() or tongue.area() < 1.0:
                skipped += 1
                continue
            ok = False
            for d in (depth, depth * 0.6, depth * 0.35):
                g0, g1 = _sided(0, d + clr, -male)
                hole = groove.extrude(g1 - g0).translate((0, 0, g0))
                if _contained(local[-male], hole):
                    ok = True
                    break
            if not ok:
                skipped += 1
                continue
            t0, t1 = _sided(-EMBED, d, -male)
            add[male].append(tongue.extrude(t1 - t0).translate((0, 0, t0)))
            h0, h1 = _sided(-EMBED, d + clr, -male)
            sub[-male].append(groove.extrude(h1 - h0).translate((0, 0, h0)))
            placed += 1
            continue
        if j.kind == "magnets":
            r, depth = j.magnet_diameter_mm / 2, j.magnet_height_mm
        else:
            r, depth = j.diameter_mm / 2, j.depth_mm
        hr = r + clr
        centres = place_features(island, hr, wall, j.count, 2 * hr + wall)
        if not centres:
            skipped += 1
            continue
        made = [_round_feature(j, c, r, hr, depth, clr, wall, male, local) for c in centres]
        if not any(made) and len(centres) > 1:  # spread out they break through: one in the middle may not
            deepest = place_features(island, hr, wall, 1, 0)
            made = [_round_feature(j, deepest[0], r, hr, depth, clr, wall, male, local)] if deepest else []
        made = [f for f in made if f is not None]
        if not made:
            skipped += 1
            continue
        for f in made:
            for side, kind, shape in f:
                if kind == "add":
                    add[side].append(shape)
                elif kind == "sub":
                    sub[side].append(shape)
                elif j.print_dowels:
                    dowels.append(shape)
        placed += len(made)
    return add, sub, dowels, {"placed": placed, "skipped": skipped}


def _round_feature(j: Joint, c: np.ndarray, r: float, hr: float, depth: float, clr: float, wall: float, male: int,
                   local: dict) -> list | None:
    """One peg, dowel or magnet at ``c``: [(side, "add" | "sub" | "dowel", shape)], or None when it would break
    through a piece (tried shorter first, for pegs and dowels)."""
    if j.kind == "pegs":
        for d in (depth, depth * 0.6, depth * 0.35):
            probe = _cylinder(hr + wall / 2, *_sided(0, d + clr + wall / 2, -male), c)
            if _contained(local[-male], probe):
                return [(male, "add", _cylinder(r, *_sided(-EMBED, d, -male), c)),
                        (-male, "sub", _cylinder(hr, *_sided(-EMBED, d + clr, -male), c))]
        return None
    hd = depth + clr
    spans = {}
    for side in (1, -1):
        for d in ((hd, hd * 0.6, hd * 0.35) if j.kind == "dowels" else (hd,)):
            probe = _cylinder(hr + wall / 2, *_sided(0, d + wall / 2, side), c)
            if _contained(local[side], probe):
                spans[side] = d
                break
        else:
            return None
    out = [(side, "sub", _cylinder(hr, *_sided(-EMBED, spans[side], side), c)) for side in (1, -1)]
    if j.kind == "dowels":
        out.append((0, "dowel", _cylinder(r, -(spans[-1] - clr), spans[1] - clr, c)))
    return out


def cut_solid(solid: Solid, settings: CutSettings, progress: ProgressFn | None = None) -> CutResult:
    """Split the solid along every plane in turn, adding each cut's joint, into watertight pieces."""
    m3 = _m()
    notes = list(solid.notes)
    pieces: list[tuple[object, list]] = [(solid.manifold, [])]  # (manifold, cut faces)
    dowels: list[object] = []
    joints = []
    wall = max(settings.wall_mm, 0.4)
    for ci, cut in enumerate(settings.cuts):
        label = f"Cut {ci + 1}"
        # a plane lying exactly on a face of the model (a cut at a round number through a flat base) would split
        # that face into a sheet of no thickness on one side; a shift far below print resolution avoids it
        cut = replace(cut, point=cut.point + cut.normal * NUDGE)
        if progress:
            progress(ci / max(len(settings.cuts), 1), f"{label} of {len(settings.cuts)}")
        if cut.joint.kind not in JOINTS:
            raise ValueError(f"Unknown joint '{cut.joint.kind}'.")
        nxt, crossed, placed, skipped = [], 0, 0, 0
        back = _from_plane(cut)
        for solid_, faces in pieces:
            pos, neg = solid_.split_by_plane(tuple(cut.normal), cut.offset)
            if pos.is_empty() or neg.is_empty() or pos.volume() < 1e-3 or neg.volume() < 1e-3:
                nxt.append((solid_, faces))
                continue
            crossed += 1
            add, sub, made, info = _joint_features(cut, pos, neg, wall)
            placed += info["placed"]
            skipped += info["skipped"]
            male = -1 if cut.joint.flip else 1
            for side, part in ((1, pos), (-1, neg)):
                if sub[side]:
                    part = part - m3.Manifold.batch_boolean(sub[side], m3.OpType.Add).transform(back)
                if add[side]:
                    part = part + m3.Manifold.batch_boolean(add[side], m3.OpType.Add).transform(back)
                face = (cut.normal * -side, cut.offset * -side, bool(add[side]) and side == male)
                nxt.append((part, [*faces, face]))
            dowels.extend(d.transform(back) for d in made)
        if not crossed:
            notes.append(f"{label} doesn't go through the model.")
        if skipped:
            what = {"pegs": "peg", "dowels": "dowel", "magnets": "magnet", "key": "key"}[cut.joint.kind]
            notes.append(f"{label}: {_n(skipped, 'place')} had no room for a {what} and "
                         f"{'was' if skipped == 1 else 'were'} left flat.")
        joints.append({"cut": ci, "crossed": crossed, "placed": placed, "skipped": skipped, "kind": cut.joint.kind})
        pieces = nxt
    # the pieces from the top down, then the dowels
    out = []
    order = sorted(range(len(pieces)), key=lambda i: -float(np.mean(pieces[i][0].bounding_box()[2::3])))
    for k, i in enumerate(order):
        m, faces = pieces[i]
        out.append(Piece(f"Piece {k + 1}", _from_manifold(m), "piece", faces, float(m.volume())))
    for k, d in enumerate(dowels):
        out.append(Piece(f"Dowel {k + 1}", _from_manifold(d), "dowel", [], float(d.volume())))
    return CutResult(out, notes, joints)


def cut_mesh(mesh: TriMesh, settings: CutSettings, progress: ProgressFn | None = None) -> CutResult:
    """:func:`make_solid` then :func:`cut_solid`."""
    solid = make_solid(mesh, settings.sheet_mm, _sub(progress, 0, 0.5))
    return cut_solid(solid, settings, _sub(progress, 0.5, 1.0))


def _sub(progress: ProgressFn | None, lo: float, hi: float) -> ProgressFn | None:
    if progress is None:
        return None
    return lambda f, m: progress(lo + (hi - lo) * f, m)


# ------------------------------------------------------------------ laying pieces out to print


def print_pose(piece: Piece) -> TriMesh:
    """The piece turned to lie on its biggest flat cut face (one without pegs or a tongue on it) and set on
    the bed at Z = 0; dowels stand on end. A piece without a usable cut face is left upright."""
    mesh = piece.mesh
    if piece.kind == "dowel" or not piece.faces:
        return _on_bed(mesh)
    v = mesh.vertices * 1000.0
    fn = mesh.face_normals()
    areas = mesh.face_areas() * 1e6
    centroid = v[mesh.faces].mean(1)
    best, best_area = None, 0.0
    for normal, offset, raised in piece.faces:
        if raised:
            continue
        on = (fn @ normal > 0.999) & (np.abs(centroid @ normal - offset) < 0.05)
        area = float(areas[on].sum())
        if area > best_area:
            best, best_area = normal, area
    if best is None:
        return _on_bed(mesh)
    rot = _rotation(best, np.array([0.0, 0.0, -1.0]))
    turned = replace(mesh, vertices=mesh.vertices @ rot.T, metadata=dict(mesh.metadata))
    return _on_bed(turned)


def _on_bed(mesh: TriMesh) -> TriMesh:
    lo, hi = mesh.bounds()
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return replace(mesh, vertices=mesh.vertices + shift, metadata=dict(mesh.metadata))


def _rotation(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """The rotation taking unit vector ``a`` onto ``b``."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(a @ b)
    if c < -0.999999:
        axis = np.cross(a, np.eye(3)[int(np.argmin(np.abs(a)))])
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


# ------------------------------------------------------------------ settings from the page / command line


def settings_from_json(data: dict) -> CutSettings:
    cuts = []
    for c in data.get("cuts") or []:
        j = c.get("joint") or {}
        kind = str(j.get("kind", "pegs"))
        if kind not in JOINTS:
            raise ValueError(f"Unknown joint '{kind}'.")
        joint = Joint(
            kind=kind,
            clearance_mm=_clamp(j.get("clearance_mm", 0.2), 0, 5),
            count=int(_clamp(j.get("count", 0), 0, MAX_FEATURES)),
            diameter_mm=_clamp(j.get("diameter_mm", 5.0), 0.5, 100),
            depth_mm=_clamp(j.get("depth_mm", 6.0), 0.5, 200),
            magnet_diameter_mm=_clamp(j.get("magnet_diameter_mm", 6.0), 0.5, 100),
            magnet_height_mm=_clamp(j.get("magnet_height_mm", 3.0), 0.2, 100),
            flip=bool(j.get("flip", False)),
            print_dowels=bool(j.get("print_dowels", True)),
        )
        cuts.append(CutPlane(np.asarray(c["normal"], float), np.asarray(c["point"], float), joint))
    return CutSettings(cuts, _clamp(data.get("sheet_mm", 1.2), 0.2, 20), _clamp(data.get("wall_mm", 1.6), 0.4, 20))


def _clamp(value, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = lo
    if not math.isfinite(v):
        v = lo
    return min(max(v, lo), hi)


def parse_cut(text: str, mesh: TriMesh | None = None) -> CutPlane:
    """A cut as the command line writes it: ``AXIS=MM[:JOINT]`` (``z=40:pegs``, ``x=-12.5:magnets``), the plane
    across that axis at that many millimetres; ``AXIS=50%`` puts it that far along the model."""
    spec, _, joint = text.partition(":")
    axis, _, where = spec.partition("=")
    axis = axis.strip().lower()
    if axis not in ("x", "y", "z") or not where:
        raise ValueError(f"Write a cut as AXIS=MM[:JOINT], such as z=40:pegs (not '{text}').")
    k = "xyz".index(axis)
    normal = np.eye(3)[k]
    where = where.strip()
    if where.endswith("%"):
        if mesh is None:
            raise ValueError("A cut in percent needs the model.")
        lo, hi = mesh.bounds()
        mm = (lo[k] + (hi[k] - lo[k]) * float(where[:-1]) / 100) * 1000
    else:
        mm = float(where)
    kind = (joint or "pegs").strip().lower()
    if kind not in JOINTS:
        raise ValueError(f"Choose a joint from {', '.join(JOINTS)} (not '{kind}').")
    return CutPlane(normal, normal * mm, Joint(kind=kind))
