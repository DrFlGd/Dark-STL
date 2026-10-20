"""Adaptive subdivision to a maximum edge length.

Every edge longer than the limit is split at its midpoint, repeatedly, until
no edge is longer than the limit. Large flat areas therefore get as many
vertices as small detailed ones, which is the point of the "equal spacing"
step: displacement detail then has the same resolution everywhere.

Because new vertices are edge midpoints, positions stay exactly on the
original surface and per-corner UVs are interpolated exactly, so textures map
without any re-projection error. Sharp edges are preserved.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

from .mesh import TriMesh

ProgressFn = Callable[[float, str], None]


def _rotate(arr: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Cyclically rotate the corner axis (axis 1) of ``arr`` by ``r`` per row."""
    idx = (r[:, None] + np.arange(3)[None, :]) % 3
    idx = idx.reshape(idx.shape + (1,) * (arr.ndim - 2))
    return np.take_along_axis(arr, np.broadcast_to(idx, (arr.shape[0], 3) + arr.shape[2:]), axis=1)


def split_long_edges(mesh: TriMesh, max_edge: float) -> tuple[TriMesh, int]:
    """One refinement pass. Returns the new mesh and the number of edges split."""
    edges, face_edge = mesh.edges_unique()
    v = mesh.vertices
    lengths = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
    split = lengths > max_edge
    n_split = int(split.sum())
    if n_split == 0:
        return mesh, 0

    new_index = np.full(len(edges), -1, np.int64)
    new_index[split] = mesh.n_vertices + np.arange(n_split)
    midpoints = 0.5 * (v[edges[split, 0]] + v[edges[split, 1]])
    vertices = np.concatenate([v, midpoints])

    faces = mesh.faces
    has_uv = mesh.corner_uvs is not None
    uvs = mesh.corner_uvs if has_uv else np.zeros((len(faces), 3, 2), np.float32)
    mid = new_index[face_edge]  # (F, 3): vertex on edge (k, k+1) or -1
    mid_uv = 0.5 * (uvs + np.roll(uvs, -1, axis=1))  # (F, 3, 2)
    mask = mid >= 0
    count = mask.sum(axis=1)
    mat = mesh.face_material
    grp = mesh.face_group

    out_f, out_uv, out_m, out_g = [], [], [], []

    def emit(f, uv, sel):
        out_f.append(f)
        out_uv.append(uv)
        out_m.append(mat[sel])
        out_g.append(grp[sel])

    # 0 split edges: unchanged
    s0 = count == 0
    emit(faces[s0], uvs[s0], s0)

    # 1 split edge: rotate so it is edge 0 (c0-c1), then two triangles
    s1 = np.nonzero(count == 1)[0]
    if len(s1):
        r = np.argmax(mask[s1], axis=1)
        c = _rotate(faces[s1], r)
        cu = _rotate(uvs[s1], r)
        m = _rotate(mid[s1], r)
        mu = _rotate(mid_uv[s1], r)
        f = np.concatenate([np.stack([c[:, 0], m[:, 0], c[:, 2]], 1), np.stack([m[:, 0], c[:, 1], c[:, 2]], 1)])
        uv = np.concatenate([np.stack([cu[:, 0], mu[:, 0], cu[:, 2]], 1), np.stack([mu[:, 0], cu[:, 1], cu[:, 2]], 1)])
        emit(f, uv, np.concatenate([s1, s1]))

    # 2 split edges: rotate so the unsplit edge is edge 2 (c2-c0)
    s2 = np.nonzero(count == 2)[0]
    if len(s2):
        r = (np.argmin(mask[s2], axis=1) + 1) % 3
        c = _rotate(faces[s2], r)
        cu = _rotate(uvs[s2], r)
        m = _rotate(mid[s2], r)
        mu = _rotate(mid_uv[s2], r)
        # corner triangle at c1, then split the quad (c0, m0, m1, c2) along its shorter diagonal
        d_a = np.linalg.norm(vertices[c[:, 0]] - vertices[m[:, 1]], axis=1)  # c0-m1
        d_b = np.linalg.norm(vertices[m[:, 0]] - vertices[c[:, 2]], axis=1)  # m0-c2
        use_a = d_a <= d_b
        t0 = np.stack([m[:, 0], c[:, 1], m[:, 1]], 1)
        u0 = np.stack([mu[:, 0], cu[:, 1], mu[:, 1]], 1)
        t1 = np.where(use_a[:, None], np.stack([c[:, 0], m[:, 0], m[:, 1]], 1), np.stack([c[:, 0], m[:, 0], c[:, 2]], 1))
        t2 = np.where(use_a[:, None], np.stack([c[:, 0], m[:, 1], c[:, 2]], 1), np.stack([m[:, 0], m[:, 1], c[:, 2]], 1))
        ua = use_a[:, None, None]
        u1 = np.where(ua, np.stack([cu[:, 0], mu[:, 0], mu[:, 1]], 1), np.stack([cu[:, 0], mu[:, 0], cu[:, 2]], 1))
        u2 = np.where(ua, np.stack([cu[:, 0], mu[:, 1], cu[:, 2]], 1), np.stack([mu[:, 0], mu[:, 1], cu[:, 2]], 1))
        emit(np.concatenate([t0, t1, t2]), np.concatenate([u0, u1, u2]), np.concatenate([s2, s2, s2]))

    # 3 split edges: four similar triangles
    s3 = np.nonzero(count == 3)[0]
    if len(s3):
        c = faces[s3]
        cu = uvs[s3]
        m = mid[s3]
        mu = mid_uv[s3]
        f = np.concatenate([
            np.stack([c[:, 0], m[:, 0], m[:, 2]], 1),
            np.stack([m[:, 0], c[:, 1], m[:, 1]], 1),
            np.stack([m[:, 2], m[:, 1], c[:, 2]], 1),
            np.stack([m[:, 0], m[:, 1], m[:, 2]], 1),
        ])
        uv = np.concatenate([
            np.stack([cu[:, 0], mu[:, 0], mu[:, 2]], 1),
            np.stack([mu[:, 0], cu[:, 1], mu[:, 1]], 1),
            np.stack([mu[:, 2], mu[:, 1], cu[:, 2]], 1),
            np.stack([mu[:, 0], mu[:, 1], mu[:, 2]], 1),
        ])
        emit(f, uv, np.concatenate([s3, s3, s3, s3]))

    out = TriMesh(
        vertices,
        np.concatenate(out_f),
        np.concatenate(out_uv).astype(np.float32) if has_uv else None,
        np.concatenate(out_m),
        np.concatenate(out_g),
        dict(mesh.metadata),
    )
    return out, n_split


def subdivide_max_edge(mesh: TriMesh, max_edge: float, max_vertices: int | None = None,
                       progress: ProgressFn | None = None) -> TriMesh:
    """Split edges until none is longer than ``max_edge``."""
    if max_edge <= 0:
        raise ValueError("max_edge must be positive")
    current = mesh
    for it in range(64):
        current, n = split_long_edges(current, max_edge)
        if progress:
            progress(min(0.95, 0.15 * (it + 1)), f"Subdividing: {current.n_vertices:,} vertices")
        if n == 0:
            break
        if max_vertices is not None and current.n_vertices > max_vertices:
            raise MemoryError(f"Subdivision exceeded the vertex limit ({max_vertices:,})")
    return current


def edge_length_for_vertex_count(area: float, vertices: int) -> float:
    """Edge length of an equilateral mesh with ``vertices`` vertices covering ``area``."""
    return math.sqrt(2.0 * area / (math.sqrt(3.0) * max(vertices, 1)))


def subdivide_to_vertex_count(mesh: TriMesh, target: int, tolerance: float = 0.1,
                              progress: ProgressFn | None = None) -> TriMesh:
    """Adaptive subdivision whose result has roughly ``target`` vertices.

    The vertex count behaves like ``V(L) ~ V0 + c / L**2`` for max edge length
    ``L``, so one cheap calibration run at a coarser length is enough to pick L.
    """
    v0 = mesh.n_vertices
    if target <= v0:
        return mesh
    area = mesh.surface_area()
    if area <= 0:
        return mesh
    guess = edge_length_for_vertex_count(area, target) / 0.7
    calib_len = guess * 2.0
    calib = subdivide_max_edge(mesh, calib_len)
    c = max((calib.n_vertices - v0) * calib_len ** 2, 1e-30)
    length = math.sqrt(c / max(target - v0, 1))
    result = None
    for attempt in range(3):
        if progress:
            progress(0.1 + 0.3 * attempt, f"Subdividing to max edge {length * 1000:.3f} mm")
        result = subdivide_max_edge(mesh, length, max_vertices=int(target * 4 + v0))
        err = (result.n_vertices - target) / target
        if abs(err) <= tolerance:
            break
        c = max((result.n_vertices - v0) * length ** 2, 1e-30)
        length = math.sqrt(c / max(target - v0, 1))
    result.metadata["max_edge"] = length
    return result
