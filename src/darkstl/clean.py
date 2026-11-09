"""Cleaning a model before it is evened out, displaced and exported (the Clean tab).

Game models carry what a print doesn't want: the body under the armour, linings inside a coat, cloth with no
thickness (capes, loincloths, two-sided sheets), the same triangles twice, small holes, faces turned inside
out, and the game's own size and placement. Cleaning fixes the source model, so every step after it (Mesh,
Displace, Finish, Cut, Export, Model Painter) gets a better input.

A model's clean-up is a list of recorded operations (:class:`CleanList`), replayed in order on the model as
it is built and posed (:func:`apply`), before Mesh. Operations that remove faces record them by stable ids:
the part (``face_group``) and the face's index in that part as the model builds (:func:`face_ids`), so the
list replays the same after a rescan, with other parts switched off, or in another pose. The finders work out
what to remove (:func:`find_hidden_parts`, :func:`find_hidden_faces`, :func:`find_sheets`, :class:`FaceGraph`'s
flood); an operation can also name a finder instead of faces (the command line's ``--clean``), and then finds
them as it is replayed.

Operations (``op``):

- ``remove``: faces taken out (``faces``: ids by part, see :func:`encode_ids`; or ``find``: a finder with its
  settings). ``kind`` says what they are: ``hidden_parts``, ``hidden_faces``, ``sheets`` or ``selection``.
- ``fill_holes``: openings up to ``size_mm`` across closed with a fan of triangles.
- ``dedupe``: the same triangle twice (a two-sided sheet made of one side and its mirror) kept once.
- ``weld``: vertices within ``distance_mm`` of each other merged.
- ``fix_normals``: faces turned the same way in each connected surface, outwards on a closed one.
- ``transform``: rotation, mirror, scale (or a print height in mm), move and snap to the floor. It is always
  applied last, wherever it is in the list, so the other operations work on the model as the game has it.

Sizes are millimetres on the print: when the transform scales the model (a print height), the finders'
millimetres are divided by its scale, so "0.5 mm thick" means 0.5 mm on the printed model. Meshes are in
metres (export frame, Z up), as everywhere else.

The topology helpers at the top (half-edges, boundary loops, filling them, repeated faces, connected parts)
are shared with :mod:`darkstl.cut` and :mod:`darkstl.finish`.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Callable

import numpy as np

from .geometry.mesh import VERTEX_METADATA, TriMesh

ProgressFn = Callable[[float, str], None]

OPS = ("remove", "fill_holes", "dedupe", "weld", "fix_normals", "transform")
REMOVE_KINDS = ("hidden_parts", "hidden_faces", "sheets", "selection")
PART_SHIFT = 32  # a face's stable id: part << 32 | its index in the part
ADDED_PART = 1 << 20  # faces an operation adds get the part ADDED_PART + the operation's place in the list
OPEN_FILL = 0.3  # a surface whose openings would take more than this share of its area to close is open (a cape)
TWO_SIDED_MM = 0.02  # layers this close (or closer) are two sides of one sheet rather than a thin solid
MAX_REGIONS = 40  # sheets listed at most (the biggest)


# ------------------------------------------------------------------ topology (shared with cut.py and finish.py)


def components(n_vertices: int, faces: np.ndarray) -> np.ndarray:
    """The connected part (by shared vertices) each face belongs to."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(faces):
        return np.zeros(0, np.int64)
    a = np.concatenate([faces[:, 0], faces[:, 1]])
    b = np.concatenate([faces[:, 1], faces[:, 2]])
    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n_vertices, n_vertices))
    _, labels = connected_components(g, directed=False)
    return labels[faces[:, 0]]


def edge_components(faces: np.ndarray, mask: np.ndarray | None = None, extra: np.ndarray | None = None) -> np.ndarray:
    """The connected part each face belongs to, faces joined only through a shared edge: two surfaces that
    merely touch at a vertex (an armour plate's corner on a body) stay apart. With ``mask``, only the faces it
    selects are joined (the others are each a part of their own); ``extra``: (k, 2) more pairs of faces to join."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(faces)
    if not n:
        return np.zeros(0, np.int64)
    e = np.sort(np.stack([faces, np.roll(faces, -1, axis=1)], axis=2).reshape(-1, 2), axis=1)
    _, edge = np.unique(e, axis=0, return_inverse=True)
    edge = edge.reshape(-1)
    face = np.repeat(np.arange(n), 3)
    if mask is not None:
        keep = np.repeat(np.asarray(mask, bool), 3)
        face, edge = face[keep], edge[keep]
    m = int(edge.max()) + 1 if len(edge) else 0
    a, b = face, n + edge
    if extra is not None and len(extra):
        a, b = np.concatenate([a, extra[:, 0]]), np.concatenate([b, extra[:, 1]])
    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n + m, n + m))
    _, labels = connected_components(g, directed=False)
    return labels[:n]


def signed_volume(v: np.ndarray, f: np.ndarray) -> float:
    t = v[f]
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def edge_counts(f: np.ndarray):
    """Directed half-edges (a, b) of the faces (face ``i``'s corner ``k`` is half-edge ``3 i + k``), and for
    each how often it and its reverse occur."""
    he = np.stack([f, np.roll(f, -1, axis=1)], axis=2).reshape(-1, 2)
    n = int(f.max()) + 1 if len(f) else 1
    key = he[:, 0] * n + he[:, 1]
    rkey = he[:, 1] * n + he[:, 0]
    uniq, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    same = counts[inv.reshape(-1)]
    pos = np.searchsorted(uniq, rkey)
    pos = np.minimum(pos, len(uniq) - 1)
    reverse = np.where(uniq[pos] == rkey, counts[pos], 0)
    return he, same, reverse


def boundary_loops(he: np.ndarray, boundary: np.ndarray, strict: bool = True) -> list[list[int]] | None:
    """Walk the boundary half-edges into loops of vertices. ``strict``: None when a vertex starts two of them
    (the loops aren't simple); otherwise such a vertex is passed through once per loop, which still gives
    closed loops. None when a walk doesn't come back to where it started."""
    loops = boundary_walks(he, boundary, strict)
    return None if loops is None else [[int(he[h, 0]) for h in loop] for loop in loops]


def boundary_walks(he: np.ndarray, boundary: np.ndarray, strict: bool = True) -> list[list[int]] | None:
    """As :func:`boundary_loops`, each loop as the half-edges (indices into ``he``) it runs along."""
    nxt: dict[int, list[int]] = {}
    for h in np.flatnonzero(boundary):
        a = int(he[h, 0])
        if strict and a in nxt:
            return None
        nxt.setdefault(a, []).append(int(h))
    loops: list[list[int]] = []
    for start in list(nxt):
        while nxt.get(start):
            loop, v = [], start
            while True:
                out = nxt.get(v)
                if not out:
                    return None
                h = out.pop()
                loop.append(h)
                v = int(he[h, 1])
                if v == start:
                    break
                if len(loop) > len(he):  # pragma: no cover - a guard; a walk can't be longer than the edges
                    return None
            loops.append(loop)
    return loops


def fill_loops(v: np.ndarray, f: np.ndarray, loops: list[list[int]]):
    """Close each loop with a fan of triangles to a new vertex at its centre: (vertices, faces, the fans' area)."""
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
        area += float(0.5 * np.linalg.norm(np.cross(v[a] - v[b], c - v[b]), axis=1).sum())
        n += 1
    return np.concatenate(verts), np.concatenate(faces), area


def opening_area(v: np.ndarray, loops: list[list[int]]) -> float:
    """How big the openings the loops go round are: the area each spans (the length of its vector area), so a
    jagged edge left where faces were removed counts as the opening it goes round, not the zigzag of it."""
    total = 0.0
    for loop in loops:
        p = v[np.asarray(loop, np.int64)]
        total += float(np.linalg.norm(0.5 * np.cross(p, np.roll(p, -1, axis=0)).sum(0)))
    return total


def duplicate_faces(f: np.ndarray) -> np.ndarray:
    """True for each face made of the same three vertices as a face before it (either way round): the second
    side of a two-sided sheet that is one side and its mirror."""
    if not len(f):
        return np.zeros(0, bool)
    _, first = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
    dup = np.ones(len(f), bool)
    dup[first] = False
    return dup


def _weld_index(v: np.ndarray, tolerance: float) -> tuple[np.ndarray, np.ndarray]:
    """Vertices merged where they share a position (to ``tolerance``): (welded id of each vertex, one vertex
    of each welded id)."""
    if not len(v):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    keys = np.round(v / max(tolerance, 1e-12)).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return inverse.reshape(-1), first


@dataclass
class Topology:
    """A mesh's faces on its vertices merged by position, so seams in the game's UVs don't split surfaces."""

    faces: np.ndarray  # (F, 3) welded vertex ids
    vertices: np.ndarray  # (W, 3) their positions
    first: np.ndarray  # (W,) a vertex of the mesh at each welded id

    @classmethod
    def of(cls, mesh: TriMesh, tolerance: float | None = None) -> "Topology":
        if tolerance is None:
            tolerance = max(mesh.diagonal() * 1e-7, 1e-9)
        inverse, first = _weld_index(mesh.vertices, tolerance)
        return cls(inverse[mesh.faces] if len(mesh.faces) else np.zeros((0, 3), np.int64), mesh.vertices[first], first)


# ------------------------------------------------------------------ stable face ids


def face_ids(mesh: TriMesh) -> np.ndarray:
    """Each face's stable id: its part (``face_group``) and its index among that part's faces, as one number."""
    g = np.asarray(mesh.face_group, np.int64)
    n = len(g)
    if not n:
        return np.zeros(0, np.int64)
    order = np.argsort(g, kind="stable")
    sg = g[order]
    rank = np.empty(n, np.int64)
    rank[order] = np.arange(n) - np.searchsorted(sg, sg, side="left")
    return (g << PART_SHIFT) | rank


def encode_ids(ids) -> dict[str, list[int]]:
    """Face ids as an operation keeps them: by part, runs of indices as ``[start, count, start, count...]``."""
    ids = np.unique(np.asarray(ids, np.int64))
    out: dict[str, list[int]] = {}
    if not len(ids):
        return out
    parts = ids >> PART_SHIFT
    index = ids & ((1 << PART_SHIFT) - 1)
    for p in np.unique(parts):
        idx = index[parts == p]
        breaks = np.flatnonzero(np.diff(idx) != 1) + 1
        starts = np.concatenate([[0], breaks])
        ends = np.concatenate([breaks, [len(idx)]])
        out[str(int(p))] = np.stack([idx[starts], ends - starts], axis=1).reshape(-1).astype(int).tolist()
    return out


def decode_ids(data) -> np.ndarray:
    """The face ids :func:`encode_ids` wrote."""
    out = []
    for part, runs in (data or {}).items():
        try:
            p = int(part)
            r = np.asarray(runs, np.int64).reshape(-1, 2)
        except (TypeError, ValueError):
            continue
        for start, count in r:
            if count > 0:
                out.append((np.int64(p) << PART_SHIFT) | (start + np.arange(count, dtype=np.int64)))
    return np.unique(np.concatenate(out)) if out else np.zeros(0, np.int64)


# ------------------------------------------------------------------ inside a closed surface


def _turn() -> np.ndarray:
    """A small fixed turn, so rays up the Z axis don't run along the model's own (often axis-aligned) edges."""
    a, b = 0.0123, 0.0217
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    return ry @ rx


TURN = _turn()


def _soup(tri: np.ndarray, longest: float, rounds: int = 6) -> np.ndarray:
    """Triangles (n, 3, 3) with each longer than ``longest`` split in four until none is (or ``rounds`` passes):
    small enough that the nearest centres give the nearest triangles (for distances only: they don't join up)."""
    for _ in range(rounds):
        e = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2).max(1)
        big = e > longest
        if not big.any() or len(tri) > 4_000_000:
            break
        t = tri[big]
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        tri = np.concatenate([tri[~big], np.stack([a, ab, ca], 1), np.stack([ab, b, bc], 1),
                              np.stack([ca, bc, c], 1), np.stack([ab, bc, ca], 1)])
    return tri


def _closest_distance(points: np.ndarray, tri: np.ndarray, limit: float = np.inf, k: int = 12,
                      chunk: int = 100_000) -> np.ndarray:
    """Distance from each point to the nearest of the triangles (n, 3, 3), where it is under ``limit`` (inf
    beyond it: a point deep inside a big surface is far from every triangle alike, and only the limit lets the
    search for the nearest stop early)."""
    from scipy.spatial import cKDTree

    from .geometry.proximity import closest_point_on_triangles

    out = np.full(len(points), np.inf)
    if not len(tri) or not len(points):
        return out
    centre = tri.mean(1)
    reach = float(np.linalg.norm(tri - centre[:, None, :], axis=2).max())  # no triangle reaches further from its centre
    tree = cKDTree(centre)
    k = max(1, min(k, len(tri)))
    bound = limit + reach if np.isfinite(limit) else np.inf
    for s in range(0, len(points), chunk):
        p = points[s:s + chunk]
        dist, cand = tree.query(p, k=k, distance_upper_bound=bound)
        dist, cand = np.asarray(dist).reshape(len(p), k), np.asarray(cand).reshape(len(p), k)
        ok = np.isfinite(dist)
        rows = np.repeat(np.arange(len(p)), k)[ok.reshape(-1)]
        if not len(rows):
            continue
        t = tri[cand[ok]]
        pr = p[rows]
        bc = closest_point_on_triangles(pr, t[:, 0], t[:, 1], t[:, 2])
        cp = np.einsum("ij,ijk->ik", bc, t)
        d = np.sqrt(((cp - pr) ** 2).sum(1))
        best = np.full(len(p), np.inf)
        np.minimum.at(best, rows, d)
        out[s:s + chunk] = np.where(best <= limit, best, np.inf)
    return out


class Solid:
    """A closed surface (vertices, faces; facing either way) to ask whether points are inside it, and how deep.

    Inside is worked out by the winding number along a ray: each crossing of the surface above the point counts
    +1 or -1 by which way the surface faces there, and a point inside a closed surface is wound once (outside,
    the crossings cancel). The triangles are binned in a grid across the ray, so each point meets only the few
    above it."""

    MAX_PAIRS = 6_000_000

    def __init__(self, vertices: np.ndarray, faces: np.ndarray):
        self.vertices = np.asarray(vertices, np.float64)
        self.faces = np.asarray(faces, np.int64)
        self.lo, self.hi = self.vertices.min(0), self.vertices.max(0)
        tri = (self.vertices @ TURN.T)[self.faces]
        self.tri = tri
        lo2, hi2 = tri[:, :, :2].min(1), tri[:, :, :2].max(1)
        self.origin = lo2.min(0)
        span = np.maximum(hi2.max(0) - self.origin, 1e-12)
        h = max(math.sqrt(span[0] * span[1] / max(len(tri), 1)), float(span.max()) / 256, 1e-12)
        while True:
            i0 = np.floor((lo2 - self.origin) / h).astype(np.int64)
            i1 = np.floor((hi2 - self.origin) / h).astype(np.int64)
            counts = (i1[:, 0] - i0[:, 0] + 1) * (i1[:, 1] - i0[:, 1] + 1)
            if counts.sum() <= self.MAX_PAIRS:
                break
            h *= 1.6
        self.h = h
        self.shape = (int(i1[:, 0].max()) + 1, int(i1[:, 1].max()) + 1)
        owner = np.repeat(np.arange(len(tri)), counts)
        k = np.arange(len(owner)) - np.repeat(np.cumsum(counts) - counts, counts)
        nx = (i1[:, 0] - i0[:, 0] + 1)[owner]
        cx = i0[owner, 0] + k % nx
        cy = i0[owner, 1] + k // nx
        cell = cx * self.shape[1] + cy
        order = np.argsort(cell, kind="stable")
        self.cell_tris = owner[order]
        self.cell_start = np.searchsorted(cell[order], np.arange(self.shape[0] * self.shape[1] + 1))
        self._soup_tri = None

    def winding(self, points: np.ndarray, chunk_pairs: int = 4_000_000) -> np.ndarray:
        """The winding number of the surface around each point (0 outside; +-1 inside, by its facing)."""
        p = np.asarray(points, np.float64).reshape(-1, 3)
        out = np.zeros(len(p))
        if not len(p):
            return out
        q = p @ TURN.T
        c = np.floor((q[:, :2] - self.origin) / self.h).astype(np.int64)
        ok = (c >= 0).all(1) & (c[:, 0] < self.shape[0]) & (c[:, 1] < self.shape[1])
        ok &= (p >= self.lo).all(1) & (p <= self.hi).all(1)
        idx = np.flatnonzero(ok)
        if not len(idx):
            return out
        cid = c[idx, 0] * self.shape[1] + c[idx, 1]
        s, e = self.cell_start[cid], self.cell_start[cid + 1]
        cnt = e - s
        tri = self.tri
        at = 0
        while at < len(idx):
            # as many points as keep the pairs under chunk_pairs
            run = np.cumsum(cnt[at:])
            stop = at + max(1, int(np.searchsorted(run, chunk_pairs, side="right")))
            sl = slice(at, stop)
            at = stop
            n = cnt[sl]
            if not n.sum():
                continue
            pi = np.repeat(np.arange(sl.start, sl.stop), n)
            offs = np.arange(len(pi)) - np.repeat(np.cumsum(n) - n, n)
            ti = self.cell_tris[s[pi] + offs]
            qq = q[idx[pi]]
            t = tri[ti]
            a, b, cc = t[:, 0], t[:, 1], t[:, 2]
            d = (b[:, 0] - a[:, 0]) * (cc[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (cc[:, 0] - a[:, 0])
            with np.errstate(divide="ignore", invalid="ignore"):
                w1 = ((qq[:, 0] - a[:, 0]) * (cc[:, 1] - a[:, 1]) - (qq[:, 1] - a[:, 1]) * (cc[:, 0] - a[:, 0])) / d
                w2 = ((b[:, 0] - a[:, 0]) * (qq[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (qq[:, 0] - a[:, 0])) / d
            w0 = 1 - w1 - w2
            hit = (d != 0) & (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            z = w0 * a[:, 2] + w1 * b[:, 2] + w2 * cc[:, 2]
            up = hit & (z > qq[:, 2])
            out += np.bincount(idx[pi[up]], weights=np.sign(d[up]), minlength=len(p))
        return out

    def contains(self, points: np.ndarray) -> np.ndarray:
        return np.abs(np.round(self.winding(points))) >= 1

    def distance(self, points: np.ndarray, limit: float = np.inf) -> np.ndarray:
        """How far each point is from the surface, where it is under ``limit`` (inf beyond it)."""
        if self._soup_tri is None:
            tri = self.vertices[self.faces]
            edges = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2)
            self._soup_tri = _soup(tri, max(float(np.median(edges)) * 2, 1e-9))
        return _closest_distance(np.asarray(points, np.float64).reshape(-1, 3), self._soup_tri, limit)


def orientation_flips(faces: np.ndarray, vertices: np.ndarray, corner_normals: np.ndarray | None = None) -> np.ndarray:
    """Which faces to turn over so every connected surface faces one way: outwards on a closed surface, the way
    the model's shading normals point on an open one (or the way most of it faces already). ``faces`` on welded
    vertices (:class:`Topology`)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(faces)
    flip = np.zeros(n, bool)
    if not n:
        return flip
    he = np.stack([faces, np.roll(faces, -1, axis=1)], axis=2).reshape(-1, 2)
    und = np.sort(he, axis=1)
    w = int(faces.max()) + 1
    key = und[:, 0] * w + und[:, 1]
    order = np.argsort(key, kind="stable")
    ks = key[order]
    _, start, counts = np.unique(ks, return_index=True, return_counts=True)
    two = start[counts == 2]  # edges two faces share (others don't say how the faces turn)
    h1, h2 = order[two], order[two + 1]
    f1, f2 = h1 // 3, h2 // 3
    same = f1 != f2
    h1, h2, f1, f2 = h1[same], h2[same], f1[same], f2[same]
    consistent = he[h1, 0] == he[h2, 1]  # a->b in one face, b->a in the other
    src = np.concatenate([f1, f1 + n])
    dst = np.concatenate([np.where(consistent, f2, f2 + n), np.where(consistent, f2 + n, f2)])
    g = coo_matrix((np.ones(len(src), np.int8), (src, dst)), shape=(2 * n, 2 * n))
    _, lab = connected_components(g, directed=False)
    keep, turned = lab[:n], lab[n:]
    orientable = keep != turned
    # how well each face agrees as it is: its share of the enclosed volume on a closed surface, or how it
    # faces against its shading normals on an open one
    tri = vertices[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    boundary_edge = np.zeros(len(he), bool)
    boundary_edge[order[start[counts == 1]]] = True
    open_face = boundary_edge.reshape(-1, 3).any(1)
    shell = np.minimum(keep, turned)
    open_shell = np.zeros(int(shell.max()) + 1, bool)
    np.logical_or.at(open_shell, shell, open_face)
    centre = vertices.mean(0)
    volume = np.einsum("ij,ij->i", tri[:, 0] - centre, np.cross(tri[:, 1] - centre, tri[:, 2] - centre))
    if corner_normals is not None:
        facing = np.einsum("ij,ij->i", cross, np.asarray(corner_normals, np.float64).sum(1))
    else:
        facing = np.linalg.norm(cross, axis=1)  # no shading normals: the way most of it faces now
    score = np.where(open_shell[shell], facing, volume)
    total = np.zeros(2 * n)
    np.add.at(total, keep, score)
    np.add.at(total, turned, -score)
    # each surface faces the way of whichever of its two orientations scores higher
    as_is = (total[keep] > total[turned]) | ((total[keep] == total[turned]) & (keep < turned))
    return orientable & ~as_is


# ------------------------------------------------------------------ the finders


def _shells(topo: Topology) -> list[np.ndarray]:
    """The model's connected surfaces (faces joined through shared edges), each as its face indices."""
    if not len(topo.faces):
        return []
    labels = edge_components(topo.faces)
    order = np.argsort(labels, kind="stable")
    cuts = np.flatnonzero(np.diff(labels[order])) + 1
    return np.split(order, cuts)


def _closed(v: np.ndarray, f: np.ndarray, open_fill: float = 0.5) -> tuple[np.ndarray, np.ndarray] | None:
    """A surface as a closed one: as it is when it is closed, its openings fanned shut when they're small next to
    it (a neck, sleeve ends, a waist); None when it is open (a cape) or its edges don't close into loops."""
    used, inv = np.unique(f, return_inverse=True)
    pv, pf = v[used], inv.reshape(-1, 3)
    flips = orientation_flips(pf, pv)
    pf = np.where(flips[:, None], pf[:, ::-1], pf)
    he, same, reverse = edge_counts(pf)
    boundary = (reverse == 0) & (same == 1)
    if not boundary.any():
        return (pv, pf) if len(pf) >= 4 else None
    loops = boundary_loops(he, boundary, strict=False)
    if loops is None:
        return None
    area = float(0.5 * np.linalg.norm(np.cross(pv[pf[:, 1]] - pv[pf[:, 0]], pv[pf[:, 2]] - pv[pf[:, 0]]), axis=1).sum())
    if opening_area(pv, loops) > open_fill * max(area, 1e-18):
        return None
    fv, ff, _ = fill_loops(pv, pf, loops)
    return fv, ff


def _solids(mesh: TriMesh, topo: Topology, shells: list[np.ndarray]) -> list[Solid | None]:
    out = []
    for sel in shells:
        closed = _closed(topo.vertices, topo.faces[sel]) if len(sel) >= 4 else None
        out.append(Solid(*closed) if closed is not None else None)
    return out


def _samples(points: np.ndarray, at_most: int = 2000) -> np.ndarray:
    if len(points) <= at_most:
        return points
    return points[np.linspace(0, len(points) - 1, at_most).astype(np.int64)]


def _inside_others(points: np.ndarray, i: int, shells, solids, boxes) -> np.ndarray:
    """Which points are inside one of the closed parts other than part ``i``: (inside, the part)."""
    inside = np.zeros(len(points), bool)
    which = np.full(len(points), -1)
    lo, hi = points.min(0), points.max(0)
    for j, s in enumerate(solids):
        if j == i or s is None:
            continue
        blo, bhi = boxes[j]
        if (hi < blo).any() or (lo > bhi).any():
            continue
        cand = np.flatnonzero(~inside & (points >= blo).all(1) & (points <= bhi).all(1))
        if not len(cand):
            continue
        got = s.contains(points[cand])
        inside[cand[got]] = True
        which[cand[got]] = j
    return inside, which


def find_hidden_parts(mesh: TriMesh, progress: ProgressFn | None = None, share: float = 0.99) -> list[np.ndarray]:
    """The parts (connected surfaces) of the model that lie wholly inside another part's closed solid (a lining
    in a coat, a body under a closed shell): every sampled point of them (``share`` of them) inside. Each as its
    face indices."""
    topo = Topology.of(mesh)
    shells = _shells(topo)
    if len(shells) < 2:
        return []
    if progress:
        progress(0.05, "Making each part solid")
    solids = _solids(mesh, topo, shells)
    boxes = [(s.lo, s.hi) if s is not None else None for s in solids]
    tri = mesh.vertices[mesh.faces]
    found = []
    for i, sel in enumerate(shells):
        if progress and i % 8 == 0:
            progress(0.1 + 0.85 * i / len(shells), f"Looking inside part {i + 1} of {len(shells)}")
        pts = np.concatenate([mesh.vertices[np.unique(mesh.faces[sel])], tri[sel].mean(1)])
        pts = _samples(pts)
        inside, _ = _inside_others(pts, i, shells, solids, boxes)
        if inside.mean() >= share:
            found.append(sel)
    return found


def find_hidden_faces(mesh: TriMesh, depth: float, progress: ProgressFn | None = None,
                      skip: np.ndarray | None = None) -> np.ndarray:
    """Faces whose centre is inside another part's closed solid, deeper than ``depth`` (metres) under its surface:
    the body under armour, an arm in a sleeve. ``skip``: faces not to look at (a mask)."""
    topo = Topology.of(mesh)
    shells = _shells(topo)
    if len(shells) < 2:
        return np.zeros(0, np.int64)
    if progress:
        progress(0.05, "Making each part solid")
    solids = _solids(mesh, topo, shells)
    boxes = [(s.lo, s.hi) if s is not None else None for s in solids]
    centres = mesh.vertices[mesh.faces].mean(1)
    hidden = []
    for i, sel in enumerate(shells):
        if progress and i % 8 == 0:
            progress(0.1 + 0.85 * i / len(shells), f"Looking under part {i + 1} of {len(shells)}")
        if skip is not None:
            sel = sel[~skip[sel]]
        if not len(sel):
            continue
        pts = centres[sel]
        inside, which = _inside_others(pts, i, shells, solids, boxes)
        if not inside.any():
            continue
        cand = np.flatnonzero(inside)
        if depth > 0:
            deep = np.zeros(len(cand), bool)
            for j in np.unique(which[cand]):
                at = which[cand] == j
                deep[at] = np.isinf(solids[j].distance(pts[cand[at]], depth))
            cand = cand[deep]
        hidden.append(sel[cand])
    return np.unique(np.concatenate(hidden)) if hidden else np.zeros(0, np.int64)


def face_thickness(mesh: TriMesh, limit: float, chunk: int = 60_000, partners: bool = False):
    """How thick the model is behind each face, up to ``limit`` (metres; inf beyond it): the distance from the
    face's centre to the nearest surface behind it that faces the other way (the other side of a sheet or a
    thin solid; 0 for a two-sided sheet made of one side and its mirror). ``partners``: also the face that is
    the other side (-1 where none), as (thickness, partner)."""
    from scipy.spatial import cKDTree

    from .geometry.proximity import closest_point_on_triangles

    n = mesh.n_faces
    out = np.full(n, np.inf)
    partner = np.full(n, -1, np.int64)
    if n < 2 or limit <= 0:
        return (out, partner) if partners else out
    tri = mesh.vertices[mesh.faces]
    nrm = mesh.face_normals()
    centre = tri.mean(1)
    area = mesh.face_areas()
    edges = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2)
    h = max(float(np.median(edges)), limit, 1e-9)
    # points over each triangle (more over big ones), each naming its triangle, to find what's near a face
    per = np.clip(np.ceil(area / (h * h) * 2), 1, 32).astype(np.int64)
    owner = np.repeat(np.arange(n), per)
    rng = np.random.default_rng(7)
    r1, r2 = rng.random(len(owner)), rng.random(len(owner))
    fold = r1 + r2 > 1
    r1[fold], r2[fold] = 1 - r1[fold], 1 - r2[fold]
    first = np.cumsum(per) - per
    r1[first], r2[first] = 1 / 3, 1 / 3  # every triangle's own centre among them
    t = tri[owner]
    pts = t[:, 0] + r1[:, None] * (t[:, 1] - t[:, 0]) + r2[:, None] * (t[:, 2] - t[:, 0])
    tree = cKDTree(pts)
    k = int(min(24, len(pts)))
    reach = limit + h * 0.75
    for s in range(0, n, chunk):
        sl = slice(s, min(s + chunk, n))
        q = centre[sl] - nrm[sl] * (limit * 1e-3)
        dist, idx = tree.query(q, k=k, distance_upper_bound=reach)
        dist, idx = np.asarray(dist).reshape(-1, k), np.asarray(idx).reshape(-1, k)
        rows = np.repeat(np.arange(sl.start, sl.stop), k)
        ok = np.isfinite(dist.reshape(-1))
        rows, cand = rows[ok], owner[idx.reshape(-1)[ok]]
        pair = np.unique(rows * n + cand)
        rows, cand = pair // n, pair % n
        keep = rows != cand
        keep &= np.einsum("ij,ij->i", nrm[rows], nrm[cand]) < -0.5
        rows, cand = rows[keep], cand[keep]
        if not len(rows):
            continue
        tc = tri[cand]
        p = centre[rows]
        bc = closest_point_on_triangles(p, tc[:, 0], tc[:, 1], tc[:, 2])
        cp = np.einsum("ij,ijk->ik", bc, tc)
        vec = cp - p
        d = np.sqrt((vec ** 2).sum(1))
        behind = np.einsum("ij,ij->i", vec, nrm[rows]) <= 1e-9 + 1e-3 * d
        good = behind & (d <= limit)
        rows, cand, d = rows[good], cand[good], d[good]
        order = np.lexsort((d, rows))
        first = order[np.unique(rows[order], return_index=True)[1]]  # the nearest for each face
        out[rows[first]] = d[first]
        partner[rows[first]] = cand[first]
    return (out, partner) if partners else out


@dataclass
class Region:
    """A thin sheet found on the model: what kind, its faces (indices of the mesh it was found on), its area."""

    kind: str  # "open" (a single sheet with open edges) | "two-sided" (two layers back to back) | "thin"
    faces: np.ndarray
    area: float  # m²
    thickness: float  # metres, the thickest of it that counted (0 for an open sheet)
    centre: np.ndarray

    def as_dict(self, scale: float = 1.0) -> dict:
        return {"kind": self.kind, "faces": int(len(self.faces)), "area_cm2": round(self.area * 1e4 * scale * scale, 2),
                "thickness_mm": round(self.thickness * 1000 * scale, 3), "centre_mm": (self.centre * 1000 * scale).round(1).tolist()}


def _is_open(v: np.ndarray, f: np.ndarray, thickness: float) -> bool:
    """Whether a connected surface is an open sheet: it has open edges, and closing them wouldn't enclose a solid
    (the opening is most of it, as on a cape, or what it would enclose is thinner than ``thickness``)."""
    used, inv = np.unique(f, return_inverse=True)
    pv, pf = v[used], inv.reshape(-1, 3)
    he, same, reverse = edge_counts(pf)
    boundary = (reverse == 0) & (same == 1)
    if not boundary.any():
        return False
    area = float(0.5 * np.linalg.norm(np.cross(pv[pf[:, 1]] - pv[pf[:, 0]], pv[pf[:, 2]] - pv[pf[:, 0]]), axis=1).sum())
    flips = orientation_flips(pf, pv)
    if flips.any():  # the openings are walked on the faces turned one way
        pf = np.where(flips[:, None], pf[:, ::-1], pf)
        he, same, reverse = edge_counts(pf)
        boundary = (reverse == 0) & (same == 1)
    loops = boundary_loops(he, boundary, strict=False)
    if loops is None:
        return boundary.sum() > 0.05 * len(he)
    fv, ff, _ = fill_loops(pv, pf, loops)
    opening = opening_area(pv, loops)
    vol = abs(signed_volume(fv, ff))
    return opening >= OPEN_FILL * area or vol <= opening * max(thickness, 1e-6) * 0.5


def find_sheets(mesh: TriMesh, thickness: float, progress: ProgressFn | None = None, scale: float = 1.0,
                min_share: float = 0.0005, at_most: int = MAX_REGIONS) -> tuple[list[Region], int]:
    """The thin sheets of the model, biggest first: open surfaces with no inside (a cape, a loincloth, linen),
    and faces with less than ``thickness`` (metres) of model behind them (two-sided cloth, a thin flap), each
    connected stretch of them a region. ``scale``: the print's scale, for telling two-sided cloth from a thin
    solid by print millimetres. Returns (regions, how many smaller ones weren't listed)."""
    topo = Topology.of(mesh)
    shells = _shells(topo)
    areas = mesh.face_areas()
    total = float(areas.sum()) or 1.0
    centres = mesh.vertices[mesh.faces].mean(1)
    taken = np.zeros(mesh.n_faces, bool)
    regions: list[Region] = []
    for i, sel in enumerate(shells):
        if progress and i % 16 == 0:
            progress(0.05 + 0.25 * i / max(len(shells), 1), f"Looking for open sheets: part {i + 1} of {len(shells)}")
        if _is_open(topo.vertices, topo.faces[sel], thickness):
            taken[sel] = True
            w = areas[sel]
            regions.append(Region("open", sel, float(w.sum()), 0.0, (centres[sel] * w[:, None]).sum(0) / max(w.sum(), 1e-18)))
    if progress:
        progress(0.35, "Measuring how thick the model is behind each face")
    welded = TriMesh(topo.vertices, topo.faces)
    thick, partner = face_thickness(welded, thickness, partners=True)
    thin = (thick < thickness) & ~taken
    if thin.any():
        # the slivers along a thin part's rim (narrower than the thickness) go with it
        tri = topo.vertices[topo.faces]
        longest = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2).max(1)
        narrow = (2 * welded.face_areas() / np.maximum(longest, 1e-18) < thickness) & ~thin & ~taken
        if narrow.any():
            lab = edge_components(topo.faces, thin | narrow)
            beside = narrow & np.isin(lab, np.unique(lab[thin]))  # joined to the thin faces through slivers
            thin |= beside
            thick[beside] = np.nan  # not measured: they don't say how thick it is
        both = thin & (partner >= 0)
        both[both] &= thin[partner[both]]
        pairs = np.stack([np.flatnonzero(both), partner[both]], axis=1)  # each side joined to the other
        labels = edge_components(topo.faces, thin, pairs)
        sel_all = np.flatnonzero(thin)
        order = sel_all[np.argsort(labels[sel_all], kind="stable")]
        cuts = np.flatnonzero(np.diff(labels[order])) + 1
        two = TWO_SIDED_MM / 1000 / max(scale, 1e-12)
        for sel in np.split(order, cuts):
            w = areas[sel]
            t = thick[sel][np.isfinite(thick[sel])]
            if not len(t):
                continue
            kind = "two-sided" if np.median(t) <= two else "thin"
            regions.append(Region(kind, sel, float(w.sum()), float(t.max()),
                                  (centres[sel] * w[:, None]).sum(0) / max(w.sum(), 1e-18)))
    regions.sort(key=lambda r: -r.area)
    big = [r for r in regions if r.area >= min_share * total and len(r.faces) >= 2]
    return big[:at_most], len(regions) - len(big[:at_most])


class FaceGraph:
    """Which faces of a mesh meet along an edge, and how sharply, to grow a selection from a face clicked."""

    def __init__(self, mesh: TriMesh):
        topo = Topology.of(mesh)
        f = topo.faces
        n = len(f)
        self.n = n
        self.normals = mesh.face_normals()
        if not n:
            self.a = self.b = np.zeros(0, np.int64)
            return
        und = np.sort(np.stack([f, np.roll(f, -1, axis=1)], axis=2).reshape(-1, 2), axis=1)
        key = und[:, 0] * (int(f.max()) + 1) + und[:, 1]
        order = np.argsort(key, kind="stable")
        ks = key[order]
        nxt = np.flatnonzero(ks[1:] == ks[:-1])  # each face joined to the next one on the same edge
        a, b = order[nxt] // 3, order[nxt + 1] // 3
        keep = a != b
        self.a, self.b = a[keep], b[keep]
        self.cos = np.einsum("ij,ij->i", self.normals[self.a], self.normals[self.b])

    def flood(self, seed: int, angle: float) -> np.ndarray:
        """The faces reached from ``seed`` across edges where the surface bends less than ``angle`` degrees."""
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        if not 0 <= seed < self.n:
            return np.zeros(0, np.int64)
        keep = self.cos >= math.cos(math.radians(min(max(angle, 0.0), 180.0))) - 1e-9
        g = coo_matrix((np.ones(int(keep.sum()), np.int8), (self.a[keep], self.b[keep])), shape=(self.n, self.n))
        _, lab = connected_components(g, directed=False)
        return np.flatnonzero(lab == lab[seed])


def find_holes(mesh: TriMesh, size: float) -> list[tuple[list[int], float]]:
    """The model's openings up to ``size`` (metres) across: (the half-edges of the faces around it, as
    :func:`edge_counts` numbers them on the welded vertices, its size), smallest first."""
    topo = Topology.of(mesh)
    if not len(topo.faces):
        return []
    he, same, reverse = edge_counts(topo.faces)
    boundary = (reverse == 0) & (same == 1)
    if not boundary.any():
        return []
    walks = boundary_walks(he, boundary, strict=False) or []
    out = []
    for loop in walks:
        if len(loop) < 3:
            continue
        p = topo.vertices[he[loop, 0]]
        across = float(np.linalg.norm(p.max(0) - p.min(0)))
        if across <= size:
            out.append((loop, across))
    out.sort(key=lambda x: x[1])
    return out


# ------------------------------------------------------------------ the list of operations


@dataclass(frozen=True)
class CleanList:
    """A model's clean-up: the operations in order (as JSON objects), and a key naming them."""

    ops: tuple
    key: str

    @classmethod
    def of(cls, ops) -> "CleanList":
        checked = tuple(_checked(op) for op in (ops or []))
        text = json.dumps(list(checked), sort_keys=True, separators=(",", ":"))
        return cls(checked, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])

    def __bool__(self) -> bool:
        return bool(self.ops)

    def transform(self) -> dict | None:
        found = [op for op in self.ops if op.get("op") == "transform"]
        return found[-1] if found else None


def _num(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi) if math.isfinite(v) else default


def _checked(op) -> dict:
    """An operation as kept: only what it is known by, numbers in range. A broken one raises ValueError."""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        raise ValueError(f"unknown clean-up operation: {op!r}")
    kind = op["op"]
    out: dict = {"op": kind}
    if kind == "remove":
        k = op.get("kind") if op.get("kind") in REMOVE_KINDS else "selection"
        out["kind"] = k
        if isinstance(op.get("faces"), dict):
            out["faces"] = {str(p): [int(x) for x in runs] for p, runs in op["faces"].items()
                            if isinstance(runs, (list, tuple)) and len(runs) % 2 == 0}
        elif k == "selection":
            raise ValueError("a selection removed names its faces")
        for key in ("depth_mm", "thickness_mm"):
            if op.get(key) is not None:
                out[key] = _num(op[key], 0.0, 0.0, 1000.0)
        for key in ("parts", "regions"):
            if op.get(key) is not None:
                out[key] = int(_num(op[key], 0, 0, 1e9))
        if isinstance(op.get("sheet_kinds"), (list, tuple)):
            out["sheet_kinds"] = [str(s) for s in op["sheet_kinds"] if s in ("open", "two-sided", "thin")]
    elif kind == "fill_holes":
        out["size_mm"] = _num(op.get("size_mm"), 5.0, 0.01, 10000.0)
    elif kind == "dedupe":
        out["distance_mm"] = _num(op.get("distance_mm"), 0.01, 0.0, 10.0)
    elif kind == "weld":
        out["distance_mm"] = _num(op.get("distance_mm"), 0.01, 0.0, 100.0)
    elif kind == "transform":
        rot = op.get("rotate") or [0, 0, 0]
        move = op.get("move") or [0, 0, 0]
        mirror = op.get("mirror") or [False, False, False]
        out["rotate"] = [_num(x, 0.0, -36000.0, 36000.0) for x in list(rot)[:3]] + [0.0] * (3 - len(list(rot)[:3]))
        out["move"] = [_num(x, 0.0, -1e7, 1e7) for x in list(move)[:3]] + [0.0] * (3 - len(list(move)[:3]))
        out["mirror"] = [bool(x) for x in list(mirror)[:3]] + [False] * (3 - len(list(mirror)[:3]))
        out["scale"] = _num(op.get("scale"), 1.0, 1e-4, 1e4)
        out["height_mm"] = _num(op["height_mm"], 0.0, 0.0, 1e6) if op.get("height_mm") else None
        out["floor"] = bool(op.get("floor", False))
    return out


@dataclass
class Cleaned:
    """A model as its clean-up leaves it: the mesh, each face's stable id, a line per operation (in the list's
    order), the scale the transform gave it, and its size before the transform (game millimetres)."""

    mesh: TriMesh
    ids: np.ndarray
    rows: list[dict] = field(default_factory=list)
    scale: float = 1.0
    game_size_mm: list[float] = field(default_factory=list)


def _size_mm(mesh: TriMesh) -> list[float]:
    lo, hi = mesh.bounds()
    return ((hi - lo) * 1000).round(2).tolist()


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _keep(mesh: TriMesh, ids: np.ndarray, mask: np.ndarray) -> tuple[TriMesh, np.ndarray]:
    if mask.all():
        return mesh, ids
    return mesh.subset(mask), ids[mask]


def _rotation(degrees) -> np.ndarray:
    rx, ry, rz = (math.radians(d) for d in degrees)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    x = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return z @ y @ x  # about X first, then Y, then Z


def _linear(op: dict) -> np.ndarray:
    m = _rotation(op.get("rotate") or [0, 0, 0])
    mirror = op.get("mirror") or [False, False, False]
    return m @ np.diag([-1.0 if mirror[i] else 1.0 for i in range(3)])


def transform_scale(mesh: TriMesh, op: dict | None) -> float:
    """The scale ``op`` gives ``mesh``: its own, or the one that makes it ``height_mm`` tall as it stands."""
    if not op:
        return 1.0
    if op.get("height_mm"):
        if not mesh.n_vertices:
            return 1.0
        z = mesh.vertices @ _linear(op)[2]
        tall = float(z.max() - z.min())
        return op["height_mm"] / 1000 / tall if tall > 1e-12 else 1.0
    return float(op.get("scale") or 1.0)


def _transform(mesh: TriMesh, op: dict) -> tuple[TriMesh, dict]:
    m = _linear(op)
    s = transform_scale(mesh, op)
    if not mesh.n_vertices:
        return mesh, {"scale": s}
    lo, hi = mesh.bounds()
    centre = (lo + hi) / 2
    v = (mesh.vertices - centre) @ m.T * s + centre + np.asarray(op.get("move") or [0, 0, 0], np.float64) / 1000
    if op.get("floor"):
        v[:, 2] -= v[:, 2].min()
    out = replace(mesh, vertices=v, metadata=dict(mesh.metadata))
    if mesh.corner_normals is not None:
        n = np.asarray(mesh.corner_normals, np.float64) @ np.linalg.inv(m)  # normals turn by the inverse transpose
        length = np.linalg.norm(n, axis=2, keepdims=True)
        out.corner_normals = (n / np.where(length > 0, length, 1)).astype(np.float32)
    if np.linalg.det(m) < 0:  # mirrored: the faces would turn inside out
        out.faces = mesh.faces[:, [0, 2, 1]]
        if out.corner_uvs is not None:
            out.corner_uvs = out.corner_uvs[:, [0, 2, 1]]
        if out.corner_normals is not None:
            out.corner_normals = out.corner_normals[:, [0, 2, 1]]
    return out, {"scale": s}


def _transform_text(op: dict, s: float, size: list[float]) -> str:
    parts = []
    if op.get("height_mm"):
        parts.append(f"{op['height_mm']:g} mm tall (×{s:.4g})")
    elif abs(s - 1) > 1e-9:
        parts.append(f"scaled ×{s:.4g}")
    rot = op.get("rotate") or [0, 0, 0]
    turned = [f"{r:g}° about {a}" for r, a in zip(rot, "XYZ") if abs(r) > 1e-9]
    if turned:
        parts.append("turned " + ", ".join(turned))
    mirrored = [a for on, a in zip(op.get("mirror") or [], "XYZ") if on]
    if mirrored:
        parts.append("mirrored across " + " and ".join(mirrored))
    move = op.get("move") or [0, 0, 0]
    if any(abs(x) > 1e-9 for x in move):
        parts.append("moved " + ", ".join(f"{x:g} mm {a}" for x, a in zip(move, "XYZ") if abs(x) > 1e-9))
    if op.get("floor"):
        parts.append("on the floor")
    if size:
        parts.append(f"{size[0]:.4g} × {size[1]:.4g} × {size[2]:.4g} mm")
    return " · ".join(parts) or "as the game has it"


def _append(mesh: TriMesh, ids: np.ndarray, positions: np.ndarray, like: np.ndarray, faces: np.ndarray,
            uvs: np.ndarray | None, material: np.ndarray, group: np.ndarray, new_ids: np.ndarray) -> tuple[TriMesh, np.ndarray]:
    """``mesh`` with vertices (each taking ``like``'s per-vertex data) and faces added."""
    meta = dict(mesh.metadata)
    for key in VERTEX_METADATA:
        value = meta.get(key)
        if value is not None:
            meta[key] = tuple(np.concatenate([np.asarray(a), np.asarray(a)[like]]) for a in value)
    v = np.concatenate([mesh.vertices, positions])
    f = np.concatenate([mesh.faces, faces + 0])
    tri = v[faces]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-30)
    out = TriMesh(
        v, f,
        None if mesh.corner_uvs is None else np.concatenate([mesh.corner_uvs, (uvs if uvs is not None else np.zeros((len(faces), 3, 2))).astype(np.float32)]),
        np.concatenate([mesh.face_material, material.astype(np.int32)]),
        np.concatenate([mesh.face_group, group.astype(np.int32)]),
        meta,
        None if mesh.corner_normals is None else np.concatenate([mesh.corner_normals, np.repeat(fn[:, None, :], 3, axis=1).astype(np.float32)]),
    )
    return out, np.concatenate([ids, new_ids])


def added_part(op: dict) -> int:
    """The part the faces ``op`` adds are given ids in: the same whatever else is in the list."""
    import zlib

    return ADDED_PART + zlib.crc32(json.dumps(op, sort_keys=True).encode("utf-8")) % (1 << 19)


def fill_holes(mesh: TriMesh, ids: np.ndarray, size: float, part: int = ADDED_PART) -> tuple[TriMesh, np.ndarray, dict]:
    """The openings up to ``size`` across closed with a fan of triangles to their middle, each taking the
    material, part and texture coordinates of the faces around it; the faces added get ids in ``part``."""
    holes = find_holes(mesh, size)
    if not holes:
        return mesh, ids, {"holes": 0, "faces_added": 0}
    topo = Topology.of(mesh)
    he_face = np.repeat(np.arange(mesh.n_faces), 3)
    he_corner = np.tile(np.arange(3), mesh.n_faces)
    positions, like, faces, uvs, mats, groups = [], [], [], [], [], []
    n = mesh.n_vertices
    for loop, _across in holes:
        loop = np.asarray(loop, np.int64)
        fi, ci = he_face[loop], he_corner[loop]
        a = mesh.faces[fi, ci]  # each opening edge runs a -> b in the face it belongs to
        b = mesh.faces[fi, (ci + 1) % 3]
        centre = topo.vertices[topo.faces[fi, ci]].mean(0)
        positions.append(centre)
        like.append(a[0])
        faces.append(np.stack([b, a, np.full(len(loop), n)], axis=1))
        if mesh.corner_uvs is not None:
            ua, ub = mesh.corner_uvs[fi, ci], mesh.corner_uvs[fi, (ci + 1) % 3]
            uc = np.repeat(ua.mean(0, keepdims=True), len(loop), axis=0)
            uvs.append(np.stack([ub, ua, uc], axis=1))
        mats.append(mesh.face_material[fi])
        groups.append(mesh.face_group[fi])
        n += 1
    faces_ = np.concatenate(faces)
    new_ids = (np.int64(part) << PART_SHIFT) | np.arange(len(faces_), dtype=np.int64)
    out, ids = _append(mesh, ids, np.asarray(positions), np.asarray(like, np.int64), faces_,
                       np.concatenate(uvs) if uvs else None, np.concatenate(mats), np.concatenate(groups), new_ids)
    return out, ids, {"holes": len(holes), "faces_added": int(len(faces_))}


def dedupe(mesh: TriMesh, ids: np.ndarray, distance: float) -> tuple[TriMesh, np.ndarray, dict]:
    """The same triangle twice (to ``distance``: either way round) kept once, the first."""
    topo = Topology.of(mesh, max(distance, mesh.diagonal() * 1e-7, 1e-9))
    dup = duplicate_faces(topo.faces)
    out, ids = _keep(mesh, ids, ~dup)
    return out, ids, {"faces_removed": int(dup.sum())}


def weld_vertices(mesh: TriMesh, ids: np.ndarray, distance: float) -> tuple[TriMesh, np.ndarray, dict]:
    """Vertices within ``distance`` of each other merged into one (the first of them); triangles that close up
    to a line are dropped."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    if mesh.n_vertices < 2 or distance <= 0:
        return mesh, ids, {"merged": 0, "faces_removed": 0}
    used = np.unique(mesh.faces)
    pairs = cKDTree(mesh.vertices[used]).query_pairs(distance, output_type="ndarray")
    if not len(pairs):
        return mesh, ids, {"merged": 0, "faces_removed": 0}
    n = len(used)
    g = coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    count, lab = connected_components(g, directed=False)
    first = np.full(count, n, np.int64)
    np.minimum.at(first, lab, np.arange(n))
    to = np.arange(mesh.n_vertices)
    to[used] = used[first[lab]]
    faces = to[mesh.faces]
    out = replace(mesh, faces=faces, metadata=dict(mesh.metadata))
    ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    out, ids = _keep(out, ids, ok)
    return out, ids, {"merged": int(n - count), "faces_removed": int((~ok).sum())}


def fix_normals(mesh: TriMesh, ids: np.ndarray) -> tuple[TriMesh, np.ndarray, dict]:
    """Faces turned over where they face the other way from the surface around them (outwards on a closed
    surface); a face whose shading normals turned with it has them turned back too."""
    topo = Topology.of(mesh)
    flip = orientation_flips(topo.faces, topo.vertices, mesh.corner_normals)
    if not flip.any():
        return mesh, ids, {"flipped": 0}
    out = mesh.copy()
    out.faces[flip] = out.faces[flip][:, [0, 2, 1]]
    if out.corner_uvs is not None:
        out.corner_uvs[flip] = out.corner_uvs[flip][:, [0, 2, 1]]
    if out.corner_normals is not None:
        cn = out.corner_normals[flip][:, [0, 2, 1]]
        fn = out.face_normals()[flip]
        against = np.einsum("ij,ij->i", fn, cn.astype(np.float64).sum(1)) < 0
        cn[against] = -cn[against]
        out.corner_normals[flip] = cn
    return out, ids, {"flipped": int(flip.sum())}


def _found(mesh: TriMesh, op: dict, scale: float) -> np.ndarray:
    """The faces a ``remove`` operation that names a finder finds now (indices into ``mesh``)."""
    mm = 1 / 1000 / max(scale, 1e-12)
    kind = op["kind"]
    if kind == "hidden_parts":
        found = find_hidden_parts(mesh)
        return np.concatenate(found) if found else np.zeros(0, np.int64)
    if kind == "hidden_faces":
        return find_hidden_faces(mesh, op.get("depth_mm", 1.0) * mm)
    if kind == "sheets":
        regions, _ = find_sheets(mesh, op.get("thickness_mm", 0.5) * mm, scale=scale, at_most=10_000, min_share=0)
        kinds = set(op.get("sheet_kinds") or ("open", "two-sided", "thin"))
        sel = [r.faces for r in regions if r.kind in kinds]
        return np.concatenate(sel) if sel else np.zeros(0, np.int64)
    return np.zeros(0, np.int64)


REMOVED = {"hidden_parts": "Parts hidden inside others", "hidden_faces": "Faces hidden inside other parts",
           "sheets": "Thin sheets", "selection": "Faces selected in the 3D view"}


def _label(op: dict) -> str:
    kind = op["op"]
    if kind == "remove":
        label = REMOVED[op["kind"]]
        if op["kind"] == "hidden_faces" and op.get("depth_mm"):
            label += f", deeper than {op['depth_mm']:g} mm"
        if op["kind"] == "sheets" and op.get("thickness_mm") is not None:
            label += f", under {op['thickness_mm']:g} mm"
        return label
    return {"fill_holes": f"Small holes filled (up to {op.get('size_mm', 5):g} mm across)",
            "dedupe": "Duplicate two-sided sheets dropped (one side kept)",
            "weld": f"Vertices welded (within {op.get('distance_mm', 0.01):g} mm)",
            "fix_normals": "Flipped faces turned the right way",
            "transform": "Transform"}[kind]


def apply(mesh: TriMesh, clean: CleanList | None, progress: ProgressFn | None = None) -> Cleaned:
    """``mesh`` (the model as built and posed) with its clean-up done: every operation in order, the transform last."""
    ids = face_ids(mesh)
    game = _size_mm(mesh)
    if not clean:
        return Cleaned(mesh, ids, [], 1.0, game)
    transform = clean.transform()
    scale = transform_scale(mesh, transform)  # for finders' millimetres; the transform's own is worked out at the end
    rows: list[dict | None] = [None] * len(clean.ops)
    steps = [(i, op) for i, op in enumerate(clean.ops) if op["op"] != "transform"]
    for at, (i, op) in enumerate(steps):
        if progress:
            progress(at / max(len(steps), 1), _label(op))
        kind = op["op"]
        row: dict = {"op": kind, "label": _label(op)}
        if kind == "remove":
            if "faces" in op:
                hit = np.isin(ids, decode_ids(op["faces"]))
                wanted = int(sum(sum(runs[1::2]) for runs in op["faces"].values()))
            else:
                hit = np.zeros(mesh.n_faces, bool)
                hit[_found(mesh, op, scale)] = True
                wanted = int(hit.sum())
            removed = int(hit.sum())
            pieces = len(np.unique(edge_components(Topology.of(mesh).faces, hit)[hit])) if removed and op["kind"] in ("hidden_parts", "sheets") else 0
            mesh, ids = _keep(mesh, ids, ~hit)
            row.update(faces_removed=removed, wanted=wanted)
            what = {"hidden_parts": "part", "sheets": "sheet"}.get(op["kind"])
            if not removed:
                row["text"] = "nothing to remove now (its parts may be switched off)" if wanted else "nothing found to remove"
            elif what:
                row["text"] = f"removed {_n(pieces, what)}, {_n(removed, 'face')}"
            else:
                row["text"] = f"removed {_n(removed, 'face')}"
        elif kind == "fill_holes":
            mesh, ids, got = fill_holes(mesh, ids, op["size_mm"] / 1000 / max(scale, 1e-12), added_part(op))
            row.update(got, text=f"filled {_n(got['holes'], 'hole')}, {_n(got['faces_added'], 'face')} added"
                       if got["holes"] else "no holes that small")
        elif kind == "dedupe":
            mesh, ids, got = dedupe(mesh, ids, op["distance_mm"] / 1000 / max(scale, 1e-12))
            row.update(got, text=f"dropped {_n(got['faces_removed'], 'face')}" if got["faces_removed"] else "no faces twice")
        elif kind == "weld":
            mesh, ids, got = weld_vertices(mesh, ids, op["distance_mm"] / 1000 / max(scale, 1e-12))
            row.update(got, text=(f"merged {_n(got['merged'], 'vertex', 'vertices')}"
                                  + (f", {_n(got['faces_removed'], 'face')} closed up and dropped" if got["faces_removed"] else ""))
                       if got["merged"] else "no vertices that close")
        elif kind == "fix_normals":
            mesh, ids, got = fix_normals(mesh, ids)
            row.update(got, text=f"turned {_n(got['flipped'], 'face')}" if got["flipped"] else "every face already faced the right way")
        rows[i] = row
    if mesh.n_faces and len(np.unique(mesh.faces)) < mesh.n_vertices:
        mesh = mesh.subset(np.ones(mesh.n_faces, bool))  # vertices nothing uses any more dropped (faces and ids stay)
    game = _size_mm(mesh)
    s = 1.0
    if transform is not None:
        mesh, got = _transform(mesh, transform)
        s = got["scale"]
        i = len(clean.ops) - 1 - list(reversed(clean.ops)).index(transform)
        rows[i] = {"op": "transform", "label": _label(transform), "scale": s, "size_mm": _size_mm(mesh),
                   "text": _transform_text(transform, s, _size_mm(mesh))}
        for j, op in enumerate(clean.ops):  # a transform before the last one is replaced by it
            if rows[j] is None:
                rows[j] = {"op": "transform", "label": _label(op), "text": "replaced by the transform after it"}
    if progress:
        progress(1.0, "Cleaned")
    return Cleaned(mesh, ids, [r for r in rows if r is not None], s, game)


# ------------------------------------------------------------------ the command line


def parse_steps(texts: list[str]) -> CleanList:
    """The command line's ``--clean`` steps as a clean-up list: ``hidden-parts``, ``hidden-faces[:depth mm]``,
    ``sheets[:thickness mm]``, ``holes[:size mm]``, ``duplicates[:mm]``, ``weld[:mm]``, ``normals``,
    ``scale:<factor>``, ``height:<mm>``, ``rotate:<x>,<y>,<z>``, ``mirror:<x|y|z>``, ``floor``."""
    ops: list[dict] = []
    transform: dict = {}
    for text in texts:
        name, _, value = str(text).strip().partition(":")
        name = name.strip().lower().replace("_", "-")

        def number(default: float, text=text, value=value) -> float:
            try:
                return float(value) if value.strip() else default
            except ValueError:
                raise ValueError(f"--clean {text}: {value!r} isn't a number") from None

        if name == "hidden-parts":
            ops.append({"op": "remove", "kind": "hidden_parts"})
        elif name == "hidden-faces":
            ops.append({"op": "remove", "kind": "hidden_faces", "depth_mm": number(1.0)})
        elif name == "sheets":
            ops.append({"op": "remove", "kind": "sheets", "thickness_mm": number(0.5)})
        elif name == "holes":
            ops.append({"op": "fill_holes", "size_mm": number(5.0)})
        elif name in ("duplicates", "dedupe"):
            ops.append({"op": "dedupe", "distance_mm": number(0.01)})
        elif name == "weld":
            ops.append({"op": "weld", "distance_mm": number(0.01)})
        elif name in ("normals", "fix-normals"):
            ops.append({"op": "fix_normals"})
        elif name == "scale":
            transform["scale"] = number(1.0)
        elif name == "height":
            transform["height_mm"] = number(0.0)
        elif name == "rotate":
            try:
                transform["rotate"] = [float(x) for x in value.split(",")][:3]
            except ValueError:
                raise ValueError(f"--clean {text}: rotate takes three angles, x,y,z") from None
        elif name == "mirror":
            axes = value.lower()
            transform["mirror"] = ["x" in axes, "y" in axes, "z" in axes]
        elif name == "floor":
            transform["floor"] = True
        else:
            raise ValueError(f"--clean {text}: unknown step (hidden-parts, hidden-faces, sheets, holes, duplicates, "
                             "weld, normals, scale, height, rotate, mirror, floor)")
    if transform:
        ops.append({"op": "transform", **transform})
    return CleanList.of(ops)


__all__ = ["CleanList", "Cleaned", "FaceGraph", "Region", "Solid", "Topology", "apply", "boundary_loops",
           "components", "decode_ids", "duplicate_faces", "edge_components", "edge_counts", "encode_ids", "face_ids",
           "face_thickness", "fill_loops", "opening_area", "find_hidden_faces", "find_hidden_parts", "find_holes", "find_sheets",
           "orientation_flips", "parse_steps", "signed_volume", "transform_scale"]
