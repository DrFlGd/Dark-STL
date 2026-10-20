"""Closest-point queries against a triangle mesh.

Candidates come from a KD-tree over face centroids, so the source mesh should
have small, even triangles (subdivide it first); the exact point-triangle
distance is then evaluated for the k nearest candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .mesh import TriMesh


def closest_point_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Barycentric coordinates ``(n, 3)`` of the closest point on each triangle (Ericson, RTCD 5.1.5)."""
    ab = b - a
    ac = c - a
    ap = p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2

    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v_in = np.where(denom != 0, vb / denom, 1 / 3)
        w_in = np.where(denom != 0, vc / denom, 1 / 3)
        bary = np.stack([1 - v_in - w_in, v_in, w_in], axis=1)

        e_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        bary[e_bc] = np.stack([np.zeros(e_bc.sum()), 1 - w[e_bc], w[e_bc]], 1)

        e_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        w = d2 / (d2 - d6)
        bary[e_ac] = np.stack([1 - w[e_ac], np.zeros(e_ac.sum()), w[e_ac]], 1)

        r_c = (d6 >= 0) & (d5 <= d6)
        bary[r_c] = (0.0, 0.0, 1.0)

        e_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        v = d1 / (d1 - d3)
        bary[e_ab] = np.stack([1 - v[e_ab], v[e_ab], np.zeros(e_ab.sum())], 1)

        r_b = (d3 >= 0) & (d4 <= d3)
        bary[r_b] = (0.0, 1.0, 0.0)

        r_a = (d1 <= 0) & (d2 <= 0)
        bary[r_a] = (1.0, 0.0, 0.0)
    return np.nan_to_num(bary, nan=1 / 3)


@dataclass
class SurfaceHits:
    face: np.ndarray  # (n,) source face index
    bary: np.ndarray  # (n, 3) barycentric coordinates in that face
    points: np.ndarray  # (n, 3) closest points
    distance: np.ndarray  # (n,)


class SurfaceLocator:
    def __init__(self, mesh: TriMesh):
        self.mesh = mesh
        self.tri = mesh.triangles()
        self.centroids = self.tri.mean(axis=1)
        self.normals = mesh.face_normals()
        self.tree = cKDTree(self.centroids)

    def query(self, points: np.ndarray, k: int = 8, normals: np.ndarray | None = None,
              chunk: int = 200_000, normal_penalty: float | None = None) -> SurfaceHits:
        """Closest point on the mesh for every query point.

        With ``normals``, candidates whose face normal points away from the
        query normal are penalised by ``normal_penalty`` (a distance), which
        keeps points on thin parts (blades, cloth) on the correct side.
        """
        n = len(points)
        k = max(1, min(k, len(self.centroids)))
        faces = np.empty(n, np.int64)
        bary = np.empty((n, 3))
        hits = np.empty((n, 3))
        dist = np.empty(n)
        for s in range(0, n, chunk):
            p = points[s:s + chunk]
            _, cand = self.tree.query(p, k=k)
            cand = cand.reshape(len(p), k)
            pr = np.repeat(p, k, axis=0)
            cf = cand.reshape(-1)
            t = self.tri[cf]
            bc = closest_point_on_triangles(pr, t[:, 0], t[:, 1], t[:, 2])
            cp = np.einsum("ij,ijk->ik", bc, t)
            d2 = np.einsum("ij,ij->i", cp - pr, cp - pr)
            if normals is not None and normal_penalty:
                qn = np.repeat(normals[s:s + chunk], k, axis=0)
                away = np.einsum("ij,ij->i", qn, self.normals[cf]) < -0.2
                d2 = d2 + away * normal_penalty ** 2
            best = np.argmin(d2.reshape(len(p), k), axis=1)
            sel = np.arange(len(p)) * k + best
            faces[s:s + chunk] = cf[sel]
            bary[s:s + chunk] = bc[sel]
            hits[s:s + chunk] = cp[sel]
            dist[s:s + chunk] = np.linalg.norm(cp[sel] - p, axis=1)
        return SurfaceHits(faces, bary, hits, dist)
