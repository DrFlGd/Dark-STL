"""Make heights sampled per UV island agree along UV seams.

A height field integrated from a normal map is only known up to a constant
on each UV island, and drifts a little along island borders. Where two islands
meet on the model (a UV seam), the two sides therefore disagree, sometimes by
as much as the relief itself. Averaging the two at the seam vertex alone
leaves a step one vertex wide, which prints as a ridge or groove along every
seam. Instead:

1. Each island is shifted by a constant chosen so the islands agree along
   their shared seams as well as possible (least squares).
2. What still differs is spread smoothly into each island from the seam, so
   the surface meets without a step and the texture detail is untouched.

Everything works on "nodes": one per (vertex, material, UV) combination, so a
seam vertex has one node per island that touches it.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import cg, lsqr
from scipy.spatial import cKDTree

from .mesh import TriMesh

UV_EPS = 1e-5


def _corner_nodes(mesh: TriMesh) -> tuple[np.ndarray, np.ndarray, int]:
    """Node id per corner, whether each vertex lies on a seam, and the node count."""
    v = mesh.faces.reshape(-1)
    mat = np.repeat(mesh.face_material, 3)
    uv = mesh.corner_uvs.reshape(-1, 2).astype(np.float64)
    first = np.full(mesh.n_vertices, -1, np.int64)
    first[v[::-1]] = np.arange(len(v))[::-1]  # first corner of each vertex
    ref = first[v]
    differs = (mat != mat[ref]) | (np.abs(uv - uv[ref]).max(axis=1) > UV_EPS)
    seam_vertex = np.bincount(v, weights=differs, minlength=mesh.n_vertices) > 0

    node = v.copy()
    n_nodes = mesh.n_vertices
    idx = np.nonzero(seam_vertex[v])[0]
    if len(idx):
        q = np.round(uv[idx] / UV_EPS).astype(np.int64)
        order = np.lexsort((q[:, 1], q[:, 0], mat[idx], v[idx]))
        s = idx[order]
        qs = q[order]
        new = np.ones(len(s), bool)
        new[1:] = (v[s[1:]] != v[s[:-1]]) | (mat[s[1:]] != mat[s[:-1]]) | (qs[1:] != qs[:-1]).any(axis=1)
        group = np.cumsum(new) - 1
        node[s] = n_nodes + group
        n_nodes += int(group[-1]) + 1
    return node, seam_vertex, n_nodes


def _adjacency(face_nodes: np.ndarray, n_nodes: int) -> sparse.csr_matrix:
    a = face_nodes.reshape(-1, 3)
    rows = np.concatenate([a[:, 0], a[:, 1], a[:, 2], a[:, 1], a[:, 2], a[:, 0]])
    cols = np.concatenate([a[:, 1], a[:, 2], a[:, 0], a[:, 0], a[:, 1], a[:, 2]])
    adj = sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n_nodes, n_nodes))
    adj.data[:] = 1.0  # duplicate edges collapse to one
    return adj


def seam_consistent_heights(mesh: TriMesh, corner_heights: np.ndarray, fixed_materials: set[int] | None = None,
                            blend: float | None = None) -> np.ndarray:
    """Vertex heights from per-corner samples, continuous across UV seams.

    ``corner_heights`` is (F, 3). ``fixed_materials`` are materials whose heights
    must not be shifted (for example ones without a detail map, height 0).
    ``blend`` is the distance (model units) over which the remaining seam
    mismatch fades out; by default 12 vertex spacings.
    """
    h_corner = np.asarray(corner_heights, np.float64).reshape(-1)
    v = mesh.faces.reshape(-1)
    nv = mesh.n_vertices
    if mesh.corner_uvs is None or mesh.n_faces == 0:
        cnt = np.bincount(v, minlength=nv)
        return np.bincount(v, weights=h_corner, minlength=nv) / np.maximum(cnt, 1)

    node, seam_vertex, n_nodes = _corner_nodes(mesh)
    cnt = np.bincount(node, minlength=n_nodes)
    h = np.bincount(node, weights=h_corner, minlength=n_nodes) / np.maximum(cnt, 1)
    if not seam_vertex.any():
        return h[:nv].copy()

    node_vertex = np.empty(n_nodes, np.int64)
    node_vertex[:nv] = np.arange(nv)
    node_vertex[node] = v
    used = cnt > 0
    adj = _adjacency(node, n_nodes)
    n_islands, island = connected_components(adj, directed=False)

    # Pairs of nodes that share a vertex: consecutive nodes of each seam vertex.
    seam_nodes = np.nonzero(used & seam_vertex[node_vertex])[0]
    seam_nodes = seam_nodes[np.argsort(node_vertex[seam_nodes], kind="stable")]
    same = node_vertex[seam_nodes[1:]] == node_vertex[seam_nodes[:-1]]
    a, b = seam_nodes[:-1][same], seam_nodes[1:][same]

    # 1. One constant per island so the islands agree along their seams.
    cross = island[a] != island[b]
    offset = np.zeros(n_islands)
    if cross.any():
        ia, ib = island[a[cross]], island[b[cross]]
        m = len(ia)
        rows = np.concatenate([np.arange(m), np.arange(m)])
        cols = np.concatenate([ia, ib])
        vals = np.concatenate([np.ones(m), -np.ones(m)])
        rhs = h[b[cross]] - h[a[cross]]
        fixed_islands = np.zeros(n_islands, bool)
        if fixed_materials:
            mat_node = np.zeros(n_nodes, np.int64)
            mat_node[node] = np.repeat(mesh.face_material, 3)
            fixed_islands[np.unique(island[np.isin(mat_node, list(fixed_materials)) & used])] = True
        fi = np.nonzero(fixed_islands)[0]
        if len(fi):  # pin islands that must not move
            rows = np.concatenate([rows, m + np.arange(len(fi))])
            cols = np.concatenate([cols, fi])
            vals = np.concatenate([vals, np.full(len(fi), 100.0)])
            rhs = np.concatenate([rhs, np.zeros(len(fi))])
        system = sparse.csr_matrix((vals, (rows, cols)), shape=(len(rhs), n_islands))
        offset = lsqr(system, rhs, damp=1e-3)[0]
    h = h + offset[island]

    # 2. Remaining mismatch: seam nodes take the vertex mean, the difference fades into each island.
    target = np.bincount(node_vertex[seam_nodes], weights=h[seam_nodes], minlength=nv)
    count = np.bincount(node_vertex[seam_nodes], minlength=nv)
    residual = np.zeros(n_nodes)
    residual[seam_nodes] = target[node_vertex[seam_nodes]] / np.maximum(count[node_vertex[seam_nodes]], 1) - h[seam_nodes]
    if np.abs(residual).max() > 0:
        h = h + _fade(mesh, adj, node_vertex, used, seam_nodes, residual, blend)

    total = np.bincount(node_vertex[used], weights=h[used] * cnt[used], minlength=nv)
    return total / np.maximum(np.bincount(node_vertex[used], weights=cnt[used], minlength=nv), 1)


def _fade(mesh: TriMesh, adj: sparse.csr_matrix, node_vertex: np.ndarray, used: np.ndarray,
          seam_nodes: np.ndarray, residual: np.ndarray, blend: float | None) -> np.ndarray:
    """Screened-Laplace extension of the seam residual: exact at seams, decaying over ``blend``."""
    spacing = float(np.median(mesh.edge_lengths())) or 1.0
    blend = blend if blend and blend > 0 else 12.0 * spacing
    rings = max(blend / spacing, 2.0)
    # Only nodes near a seam can receive a noticeable correction.
    tree = cKDTree(mesh.vertices[np.unique(node_vertex[seam_nodes])])
    dist, _ = tree.query(mesh.vertices[node_vertex], distance_upper_bound=4.0 * blend)
    band = used & np.isfinite(dist)
    boundary = np.zeros(len(residual), bool)
    boundary[seam_nodes] = True
    free = np.nonzero(band & ~boundary)[0]
    out = residual.copy()
    if len(free) == 0:
        return out
    a_ff = adj[free][:, free]
    deg = np.asarray(adj[free].sum(axis=1)).ravel()
    rhs = adj[free][:, np.nonzero(boundary)[0]] @ residual[boundary]
    screen = deg / (4.0 * rings**2)  # makes the correction decay over about `rings` edges
    system = sparse.diags(deg + screen) - a_ff
    precond = sparse.diags(1.0 / (deg + screen))
    sol, _ = cg(system, rhs, M=precond, rtol=1e-6, maxiter=2000)
    out[free] = sol
    return out
