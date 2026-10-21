"""Integrate normal-map slopes into vertex heights on the mesh itself.

The texture-space route (textures/heightmap.py) integrates each normal map on
its own image and then has to patch things up: a high-pass removes the drift
of the integration, and seams.py makes the UV islands agree where they meet.
Both cost fidelity: the high-pass also flattens real shapes larger than its
radius, and the seam blend smooths detail near every seam.

Here the slopes are integrated over the welded mesh instead. Every face edge
gets a target height difference, read from the normal map at the edge's
middle and its UV extent in texels; one sparse least-squares solve then finds
the vertex heights that best match all of them. A UV seam is just another
edge, so the surface is continuous by construction. A light screening term
(heights pulled weakly towards zero) stands in for the high-pass, with a much
longer reach.

With ``edge_keeping`` > 0 the solve is repeated with iteratively re-weighted
least squares: edges whose target disagrees with the surface around them
(hand-painted detail that no real surface could produce, compression noise)
lose weight, so their error stays local instead of smearing outwards, and
consistent steep slopes (plate edges, engraving walls) keep their full step.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import cg

from ..textures.heightmap import sample_bicubic
from .mesh import TriMesh

ProgressFn = Callable[[float, str], None]


@dataclass
class SlopeLayer:
    """One material's normal-map slopes, ready to be read along mesh edges."""

    p: np.ndarray  # (H, W) dh/dx, heights in texels per texel, x right
    q: np.ndarray  # (H, W) dh/dy, y down the image
    texel: float  # model units per texel of these images (UV scale included)
    uv_scale: tuple[float, float] = (1.0, 1.0)
    hint: np.ndarray | None = None  # (H, W) 0..1: likely material edges, where a jump may be real


def edge_targets(mesh: TriMesh, layers: dict[int, SlopeLayer]):
    """Directed face edges (i -> j), their target height differences, base weights, and which are known."""
    i = mesh.faces.reshape(-1)
    j = np.roll(mesh.faces, -1, axis=1).reshape(-1)
    d = np.zeros(len(i))
    w = np.ones(len(i))
    known = np.zeros(len(i), bool)
    if mesh.corner_uvs is None:
        return i, j, d, w, known
    uv_a = mesh.corner_uvs.reshape(-1, 2).astype(np.float64)
    uv_b = np.roll(mesh.corner_uvs, -1, axis=1).reshape(-1, 2).astype(np.float64)
    corner_mat = np.repeat(mesh.face_material, 3)
    for mat, layer in layers.items():
        sel = np.nonzero(corner_mat == mat)[0]
        if not len(sel):
            continue
        s = np.asarray(layer.uv_scale, np.float64)
        a, b = uv_a[sel] * s, uv_b[sel] * s
        mid = 0.5 * (a + b)
        h, wd = layer.p.shape
        delta = (b - a) * np.array([wd, h])
        pq = sample_bicubic(np.stack([layer.p, layer.q], axis=-1), mid)
        d[sel] = (pq[:, 0] * delta[:, 0] + pq[:, 1] * delta[:, 1]) * layer.texel
        if layer.hint is not None:
            w[sel] *= 1.0 - 0.8 * np.clip(sample_bicubic(layer.hint, mid), 0.0, 1.0)
        known[sel] = True
    return i, j, d, w, known


def _laplacian(i: np.ndarray, j: np.ndarray, w: np.ndarray, n: int) -> sparse.csr_matrix:
    rows = np.concatenate([i, j, i, j])
    cols = np.concatenate([i, j, j, i])
    vals = np.concatenate([w, w, -w, -w])
    return sparse.csr_matrix((vals, (rows, cols)), shape=(n, n))


def _solve(a: sparse.csr_matrix, b: np.ndarray, x0: np.ndarray | None, rtol: float) -> np.ndarray:
    try:
        import pyamg  # optional: a multigrid solver converges in a handful of cycles

        ml = pyamg.smoothed_aggregation_solver(a, symmetry="symmetric")
        return ml.solve(b, x0=x0, tol=rtol, maxiter=60, accel="cg")
    except ImportError:
        pass
    diag = a.diagonal()
    precond = sparse.diags(1.0 / np.where(diag > 0, diag, 1.0))
    x, _ = cg(a, b, x0=x0, M=precond, rtol=rtol, maxiter=4000)
    return x


def integrate_on_mesh(mesh: TriMesh, layers: dict[int, SlopeLayer], screen_length: float,
                      edge_keeping: float = 0.0, iterations: int = 3,
                      progress: ProgressFn | None = None) -> np.ndarray:
    """Vertex heights (model units) whose differences along the mesh edges best match the slopes.

    ``screen_length`` (model units) is how far a height can wander from zero
    before the screening pulls it back; shapes much larger than it are left to
    the mesh. Vertices no layer covers are held near zero.
    """
    n = mesh.n_vertices
    if n == 0 or not layers:
        return np.zeros(n)
    i, j, d, w0, known = edge_targets(mesh, layers)
    lengths = np.linalg.norm(mesh.vertices[i] - mesh.vertices[j], axis=1)
    keep = lengths > 0
    i, j, d, w0, known = i[keep], j[keep], d[keep], w0[keep], known[keep]
    spacing = float(np.median(lengths[keep])) if keep.any() else 1.0
    rings = max(screen_length / spacing, 2.0) if screen_length > 0 else 1e6

    covered = np.zeros(n, bool)
    covered[i[known]] = True
    covered[j[known]] = True
    deg = np.bincount(i, weights=w0, minlength=n) + np.bincount(j, weights=w0, minlength=n)
    screen = np.where(covered, deg / (4.0 * rings**2), deg)  # uncovered vertices: held at zero

    h = None
    w = w0.copy()
    rounds = 1 + (max(int(iterations), 1) if edge_keeping > 0 else 0)
    for k in range(rounds):
        if progress:
            progress(k / rounds, "Integrating slopes on the mesh" if k == 0 else "Keeping edges sharp")
        a = _laplacian(i, j, w, n) + sparse.diags(screen)
        b = np.bincount(j, weights=w * d, minlength=n) - np.bincount(i, weights=w * d, minlength=n)
        h = _solve(a.tocsr(), b, h, 1e-7)
        if k + 1 < rounds:
            r = h[j] - h[i] - d
            scale = 1.4826 * float(np.median(np.abs(r[known]))) if known.any() else 0.0
            scale = max(scale, 1e-12)
            # Cauchy-style weights: edges far from the surface's consensus fade
            robust = 1.0 / (1.0 + (r / (2.0 * scale)) ** 2)
            w = w0 * ((1.0 - edge_keeping) + edge_keeping * robust)
    return h
