"""Adaptive subdivision to a maximum edge length.

Every edge longer than the limit is split at its midpoint, repeatedly, until
no edge is longer than the limit. Large flat areas therefore get as many
vertices as small detailed ones, which is the point of the "equal spacing"
step: displacement detail then has the same resolution everywhere.

Because new vertices are edge midpoints, per-corner UVs are interpolated
exactly, so textures map without any re-projection error, and sharp edges are
preserved. Optionally (``smooth``) each midpoint is bent towards the curved
surface the model's shading normals describe, so a 16-sided grip becomes round
instead of keeping its 16 flat facets; flat-shaded parts stay flat.
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


def _curve_offsets(mesh: TriMesh, face_edge: np.ndarray, split: np.ndarray, weight: float) -> np.ndarray:
    """Offsets that move the midpoints of the ``split`` edges onto the surface the shading normals describe.

    This is Phong tessellation evaluated at an edge midpoint: the average of the
    midpoint projected onto the tangent planes at both ends. With ``weight``
    0.5 a circle sampled by its radial normals is reproduced to second order,
    so repeated splitting converges on the round shape the game shades rather
    than the flat facets it stores. Where the two faces of an edge disagree
    (a hard edge), the larger offset wins so the crease line still curves.
    Returns one row per split edge, in edge order.
    """
    fi, k = np.nonzero(split[face_edge])
    k1 = (k + 1) % 3
    f = mesh.faces
    pa = mesh.vertices[f[fi, k]]
    d = mesh.vertices[f[fi, k1]] - pa
    na = mesh.corner_normals[fi, k].astype(np.float64)
    nb = mesh.corner_normals[fi, k1].astype(np.float64)
    off = (0.25 * weight) * (np.einsum("ij,ij->i", d, nb)[:, None] * nb - np.einsum("ij,ij->i", d, na)[:, None] * na)
    edge = face_edge[fi, k]
    order = np.lexsort((-np.einsum("ij,ij->i", off, off), edge))
    first = np.ones(len(order), bool)
    first[1:] = edge[order[1:]] != edge[order[:-1]]
    pick = order[first]  # one corner per split edge, sorted by edge id
    return off[pick]


def split_long_edges(mesh: TriMesh, max_edge: float, smooth: float = 0.0, max_splits: int | None = None,
                     limit: int | None = None) -> tuple[TriMesh | None, int]:
    """One refinement pass. Returns the new mesh and the number of edges split.

    ``smooth`` > 0 bends new midpoints towards the surface implied by the
    per-corner shading normals (see :func:`_curve_offsets`); 0 keeps them on
    the flat triangles. ``max_splits`` splits only that many of the longest
    qualifying edges (each split adds exactly one vertex). If more than
    ``limit`` edges qualify, nothing is built and ``(None, count)`` is returned.
    """
    edges, face_edge = mesh.edges_unique()
    v = mesh.vertices
    lengths = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
    split = lengths > max_edge
    n_split = int(split.sum())
    if max_splits is not None and n_split > max_splits:
        idx = np.nonzero(split)[0]
        split[:] = False
        if max_splits > 0:
            split[idx[np.argpartition(-lengths[idx], max_splits - 1)[:max_splits]]] = True
        n_split = max(max_splits, 0)
    if n_split == 0:
        return mesh, 0
    if limit is not None and n_split > limit:
        return None, n_split

    new_index = np.full(len(edges), -1, np.int64)
    new_index[split] = mesh.n_vertices + np.arange(n_split)
    midpoints = 0.5 * (v[edges[split, 0]] + v[edges[split, 1]])
    if smooth > 0 and mesh.corner_normals is not None:
        midpoints += _curve_offsets(mesh, face_edge, split, smooth)
    vertices = np.concatenate([v, midpoints])

    faces = mesh.faces
    # Per-corner attributes (UV, then shading normal) travel together and are interpolated at midpoints.
    has_uv = mesh.corner_uvs is not None
    has_n = mesh.corner_normals is not None
    parts = [mesh.corner_uvs if has_uv else np.zeros((len(faces), 3, 2), np.float32)]
    if has_n:
        parts.append(mesh.corner_normals)
    attr = np.concatenate(parts, axis=2).astype(np.float32)  # (F, 3, 2 or 5)
    mid = new_index[face_edge]  # (F, 3): vertex on edge (k, k+1) or -1
    mid_attr = 0.5 * (attr + np.roll(attr, -1, axis=1))
    mask = mid >= 0
    count = mask.sum(axis=1)
    mat = mesh.face_material
    grp = mesh.face_group

    out_f, out_a, out_m, out_g = [], [], [], []

    def emit(f, a, sel):
        out_f.append(f)
        out_a.append(a)
        out_m.append(mat[sel])
        out_g.append(grp[sel])

    # 0 split edges: unchanged
    s0 = count == 0
    emit(faces[s0], attr[s0], s0)

    # 1 split edge: rotate so it is edge 0 (c0-c1), then two triangles
    s1 = np.nonzero(count == 1)[0]
    if len(s1):
        r = np.argmax(mask[s1], axis=1)
        c = _rotate(faces[s1], r)
        ca = _rotate(attr[s1], r)
        m = _rotate(mid[s1], r)
        ma = _rotate(mid_attr[s1], r)
        f = np.concatenate([np.stack([c[:, 0], m[:, 0], c[:, 2]], 1), np.stack([m[:, 0], c[:, 1], c[:, 2]], 1)])
        a = np.concatenate([np.stack([ca[:, 0], ma[:, 0], ca[:, 2]], 1), np.stack([ma[:, 0], ca[:, 1], ca[:, 2]], 1)])
        emit(f, a, np.concatenate([s1, s1]))

    # 2 split edges: rotate so the unsplit edge is edge 2 (c2-c0)
    s2 = np.nonzero(count == 2)[0]
    if len(s2):
        r = (np.argmin(mask[s2], axis=1) + 1) % 3
        c = _rotate(faces[s2], r)
        ca = _rotate(attr[s2], r)
        m = _rotate(mid[s2], r)
        ma = _rotate(mid_attr[s2], r)
        # corner triangle at c1, then split the quad (c0, m0, m1, c2) along its shorter diagonal
        d_a = np.linalg.norm(vertices[c[:, 0]] - vertices[m[:, 1]], axis=1)  # c0-m1
        d_b = np.linalg.norm(vertices[m[:, 0]] - vertices[c[:, 2]], axis=1)  # m0-c2
        use_a = d_a <= d_b
        t0 = np.stack([m[:, 0], c[:, 1], m[:, 1]], 1)
        a0 = np.stack([ma[:, 0], ca[:, 1], ma[:, 1]], 1)
        t1 = np.where(use_a[:, None], np.stack([c[:, 0], m[:, 0], m[:, 1]], 1), np.stack([c[:, 0], m[:, 0], c[:, 2]], 1))
        t2 = np.where(use_a[:, None], np.stack([c[:, 0], m[:, 1], c[:, 2]], 1), np.stack([m[:, 0], m[:, 1], c[:, 2]], 1))
        ua = use_a[:, None, None]
        a1 = np.where(ua, np.stack([ca[:, 0], ma[:, 0], ma[:, 1]], 1), np.stack([ca[:, 0], ma[:, 0], ca[:, 2]], 1))
        a2 = np.where(ua, np.stack([ca[:, 0], ma[:, 1], ca[:, 2]], 1), np.stack([ma[:, 0], ma[:, 1], ca[:, 2]], 1))
        emit(np.concatenate([t0, t1, t2]), np.concatenate([a0, a1, a2]), np.concatenate([s2, s2, s2]))

    # 3 split edges: four similar triangles
    s3 = np.nonzero(count == 3)[0]
    if len(s3):
        c = faces[s3]
        ca = attr[s3]
        m = mid[s3]
        ma = mid_attr[s3]
        f = np.concatenate([
            np.stack([c[:, 0], m[:, 0], m[:, 2]], 1),
            np.stack([m[:, 0], c[:, 1], m[:, 1]], 1),
            np.stack([m[:, 2], m[:, 1], c[:, 2]], 1),
            np.stack([m[:, 0], m[:, 1], m[:, 2]], 1),
        ])
        a = np.concatenate([
            np.stack([ca[:, 0], ma[:, 0], ma[:, 2]], 1),
            np.stack([ma[:, 0], ca[:, 1], ma[:, 1]], 1),
            np.stack([ma[:, 2], ma[:, 1], ca[:, 2]], 1),
            np.stack([ma[:, 0], ma[:, 1], ma[:, 2]], 1),
        ])
        emit(f, a, np.concatenate([s3, s3, s3, s3]))

    a = np.concatenate(out_a)
    normals = None
    if has_n:
        normals = a[..., 2:5]
        length = np.linalg.norm(normals, axis=2, keepdims=True)
        normals = normals / np.where(length > 0, length, 1)
    out = TriMesh(
        vertices,
        np.concatenate(out_f),
        np.ascontiguousarray(a[..., :2]) if has_uv else None,
        np.concatenate(out_m),
        np.concatenate(out_g),
        dict(mesh.metadata),
        normals,
    )
    return out, n_split


def _faces_where(mesh: TriMesh, mask: np.ndarray) -> TriMesh:
    """The selected faces over the same vertex array (indices unchanged)."""
    return TriMesh(
        mesh.vertices,
        mesh.faces[mask],
        None if mesh.corner_uvs is None else mesh.corner_uvs[mask],
        mesh.face_material[mask],
        mesh.face_group[mask],
        dict(mesh.metadata),
        None if mesh.corner_normals is None else mesh.corner_normals[mask],
    )


def _join_faces(parts: list[TriMesh], vertices: np.ndarray) -> TriMesh:
    """Recombine face sets that all index into (a prefix of) ``vertices``."""
    has_uv = all(p.corner_uvs is not None for p in parts)
    has_n = all(p.corner_normals is not None for p in parts)
    return TriMesh(
        vertices,
        np.concatenate([p.faces for p in parts]),
        np.concatenate([p.corner_uvs for p in parts]) if has_uv else None,
        np.concatenate([p.face_material for p in parts]),
        np.concatenate([p.face_group for p in parts]),
        dict(parts[-1].metadata),
        np.concatenate([p.corner_normals for p in parts]) if has_n else None,
    )


def _refine(mesh: TriMesh, max_edge: float, smooth: float, budget: int | None = None,
            progress: ProgressFn | None = None) -> tuple[TriMesh, bool]:
    """Split edges until none is longer than ``max_edge``; ``(mesh, False)`` if ``budget`` vertices ran out first.

    A face whose edges are all short enough can never be touched again (only
    long edges are split, and those are never shared with it), so it is set
    aside and each pass only works on the faces that still need splitting.
    """
    finished: list[TriMesh] = []
    current = mesh
    complete = True
    for it in range(64):
        tri = current.vertices[current.faces]
        active = (np.linalg.norm(tri - np.roll(tri, -1, axis=1), axis=2) > max_edge).any(axis=1)
        if not active.all():
            finished.append(_faces_where(current, ~active))
            current = _faces_where(current, active)
        if not active.any():
            break
        nxt, _ = split_long_edges(current, max_edge, smooth,
                                  limit=None if budget is None else budget - current.n_vertices)
        if nxt is None:
            complete = False
            break
        current = nxt
        if progress:
            progress(min(0.95, 0.15 * (it + 1)), f"Subdividing: {current.n_vertices:,} vertices")
    if current.n_faces:
        finished.append(current)
    if len(finished) == 1 and finished[0] is mesh:
        return mesh, complete
    return _join_faces(finished, current.vertices), complete


def subdivide_max_edge(mesh: TriMesh, max_edge: float, max_vertices: int | None = None,
                       progress: ProgressFn | None = None, smooth: float = 0.0) -> TriMesh:
    """Split edges until none is longer than ``max_edge``."""
    if max_edge <= 0:
        raise ValueError("max_edge must be positive")
    result, complete = _refine(mesh, max_edge, smooth, max_vertices, progress)
    if not complete:
        raise MemoryError(f"Subdivision would exceed the vertex limit ({max_vertices:,})")
    return result


def edge_length_for_vertex_count(area: float, vertices: int) -> float:
    """Edge length of an equilateral mesh with ``vertices`` vertices covering ``area``."""
    return math.sqrt(2.0 * area / (math.sqrt(3.0) * max(vertices, 1)))


def subdivide_to_vertex_count(mesh: TriMesh, target: int, progress: ProgressFn | None = None,
                              smooth: float = 0.0) -> TriMesh:
    """Adaptive subdivision to exactly ``target`` vertices.

    The edge limit is lowered by a factor of sqrt(2) at a time, each level
    continuing from the last, for as long as a whole level fits in the vertex
    budget. Then the longest remaining edges are split, longest first, until
    the count is reached. Every split adds exactly one vertex, so the count is
    exact, and the longest-first order keeps the spacing as even as possible.
    """
    if target <= mesh.n_vertices or mesh.n_faces == 0:
        return mesh
    level = float(mesh.edge_lengths().max())
    current = mesh
    while current.n_vertices < target:
        level /= math.sqrt(2.0)
        if progress:
            frac = math.log(max(current.n_vertices, 2)) / math.log(max(target, 3))
            progress(min(0.9, frac), f"Subdividing: {current.n_vertices:,} of {target:,} vertices")
        current, complete = _refine(current, level, smooth, budget=target)
        if not complete:
            break
    while current.n_vertices < target:
        current, n = split_long_edges(current, 0.0, smooth, max_splits=target - current.n_vertices)
        if n == 0:
            break
    current.metadata["max_edge"] = float(current.edge_lengths().max())
    return current
