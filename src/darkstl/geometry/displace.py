"""Vertex displacement along surface normals from per-material height fields."""

from __future__ import annotations

import numpy as np

from ..textures.heightmap import sample_bilinear
from .mesh import TriMesh


def corner_heights(mesh: TriMesh, fields: dict[int, np.ndarray],
                   uv_scales: dict[int, tuple[float, float]] | None = None) -> np.ndarray:
    """Height sampled at every face corner's UV from its material's field; 0 where none applies."""
    out = np.zeros((mesh.n_faces, 3))
    if mesh.corner_uvs is None:
        return out
    for mat, field in fields.items():
        sel = mesh.face_material == mat
        if not sel.any():
            continue
        uv = mesh.corner_uvs[sel].reshape(-1, 2).astype(np.float64)
        if uv_scales and mat in uv_scales:
            uv = uv * np.asarray(uv_scales[mat])
        out[sel] = sample_bilinear(field, uv).reshape(-1, 3)
    return out


def displace(mesh: TriMesh, heights: np.ndarray, max_displacement: float | None = None,
             normals: np.ndarray | None = None) -> TriMesh:
    """Move each vertex along its smooth normal by ``heights`` (model units)."""
    h = np.asarray(heights, np.float64)
    if max_displacement is not None and max_displacement > 0:
        h = np.clip(h, -max_displacement, max_displacement)
    n = mesh.vertex_normals() if normals is None else normals
    out = mesh.copy()
    out.vertices = mesh.vertices + n * h[:, None]
    out.metadata["displacement"] = h
    return out
