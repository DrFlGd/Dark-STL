"""Where a mesh passes through itself: pairs of triangles that cross.

Used by the test bench to count how often displacement pushes a thin part through itself (and to
show that keeping thin parts stops it). Triangles that share a vertex are not tested against each
other: they touch by construction.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from .mesh import TriMesh


def _segments_cross(p0: np.ndarray, p1: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Whether each segment p0-p1 passes through the inside of triangle abc (Moller-Trumbore); touching and
    lying in the triangle's plane don't count."""
    d = p1 - p0
    e1 = b - a
    e2 = c - a
    pv = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, pv)
    scale = np.linalg.norm(d, axis=1) * np.linalg.norm(e1, axis=1) * np.linalg.norm(e2, axis=1)
    ok = np.abs(det) > 1e-9 * np.maximum(scale, 1e-300)
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    tv = p0 - a
    u = np.einsum("ij,ij->i", tv, pv) * inv
    qv = np.cross(tv, e1)
    v = np.einsum("ij,ij->i", d, qv) * inv
    t = np.einsum("ij,ij->i", e2, qv) * inv
    eps = 1e-7
    return ok & (u > eps) & (v > eps) & (u + v < 1 - eps) & (t > eps) & (t < 1 - eps)


def _near_pairs(centre: np.ndarray, radius: np.ndarray) -> np.ndarray:
    """Pairs (i, j), i < j, of triangles close enough to touch: centres closer than twice the usual
    triangle size, and the few big triangles with whatever their size reaches."""
    usual = float(np.percentile(radius, 99))
    tree = cKDTree(centre)
    pairs = tree.query_pairs(2.0 * usual, output_type="ndarray").reshape(-1, 2)
    big = np.flatnonzero(radius > usual)
    if len(big):
        # a big triangle with any other: as far as its size and the other's (the usual, or the biggest's) reach
        bigtree = cKDTree(centre[big])
        groups = [(tree.query_ball_point(centre[big], radius[big] + usual), None),
                  (bigtree.query_ball_point(centre[big], radius[big] + float(radius[big].max())), big)]
        extra = []
        for near, among in groups:
            for b, nb in zip(big, near):
                if len(nb):
                    nb = np.asarray(nb, np.int64)
                    extra.append(np.stack([np.full(len(nb), b), nb if among is None else among[nb]], 1))
        if extra:
            extra = np.sort(np.concatenate(extra), axis=1)
            gap = np.linalg.norm(centre[extra[:, 0]] - centre[extra[:, 1]], axis=1)
            extra = extra[(extra[:, 0] != extra[:, 1]) & (gap > 2.0 * usual)]  # the rest are found above
            key = np.unique(extra[:, 0] * np.int64(len(centre)) + extra[:, 1])
            pairs = np.concatenate([pairs, np.stack([key // len(centre), key % len(centre)], 1)])
    return pairs


def crossing_pairs(mesh: TriMesh, chunk: int = 2_000_000) -> np.ndarray:
    """The pairs of triangles (i, j), i < j, that pass through each other."""
    if mesh.n_faces < 2:
        return np.zeros((0, 2), np.int64)
    tri = mesh.triangles()
    centre = tri.mean(axis=1)
    pairs = _near_pairs(centre, np.linalg.norm(tri - centre[:, None, :], axis=2).max(axis=1))
    f = mesh.faces
    lo, hi = tri.min(axis=1).astype(np.float32), tri.max(axis=1).astype(np.float32)
    out = []
    for s in range(0, len(pairs), chunk):
        pr = pairs[s:s + chunk]
        pr = pr[np.all((lo[pr[:, 0]] <= hi[pr[:, 1]]) & (lo[pr[:, 1]] <= hi[pr[:, 0]]), axis=1)]  # boxes overlap
        pr = pr[~(f[pr[:, 0]][:, :, None] == f[pr[:, 1]][:, None, :]).any(axis=(1, 2))]  # no shared corner
        if not len(pr):
            continue
        ta, tb = tri[pr[:, 0]], tri[pr[:, 1]]
        hit = np.zeros(len(pr), bool)
        for one, other in ((ta, tb), (tb, ta)):
            for k in range(3):
                hit |= _segments_cross(one[:, k], one[:, (k + 1) % 3], other[:, 0], other[:, 1], other[:, 2])
        out.append(pr[hit])
    return np.concatenate(out) if out else np.zeros((0, 2), np.int64)


def count_crossings(mesh: TriMesh) -> int:
    """How many pairs of triangles pass through each other."""
    return int(len(crossing_pairs(mesh)))


def crossing_pairs_among(mesh: TriMesh, faces: np.ndarray) -> np.ndarray:
    """:func:`crossing_pairs` among the chosen ``faces`` only (indices into ``mesh.faces``)."""
    faces = np.asarray(faces, np.int64)
    if len(faces) < 2:
        return np.zeros((0, 2), np.int64)
    sub = TriMesh(mesh.vertices, mesh.faces[faces])
    pairs = crossing_pairs(sub)
    return faces[pairs]
