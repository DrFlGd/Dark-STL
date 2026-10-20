"""Vertex displacement along surface normals from per-material height fields."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..textures.heightmap import sample_bilinear
from .mesh import TriMesh


@dataclass
class VertexSamples:
    """Texture lookups contributing to each vertex's height (several per vertex on seams)."""

    vertex: np.ndarray  # (n,) vertex index
    material: np.ndarray  # (n,) material index
    uv: np.ndarray  # (n, 2)

    @classmethod
    def from_corners(cls, mesh: TriMesh) -> "VertexSamples":
        if mesh.corner_uvs is None:
            raise ValueError("Mesh has no UVs to sample textures with")
        return cls(
            mesh.faces.reshape(-1),
            np.repeat(mesh.face_material, 3),
            mesh.corner_uvs.reshape(-1, 2),
        )

    @classmethod
    def from_vertex_uvs(cls, mesh: TriMesh, vertex_material: np.ndarray) -> "VertexSamples":
        """One sample per vertex, for meshes whose UVs came from projection."""
        uv = np.zeros((mesh.n_vertices, 2))
        if mesh.corner_uvs is not None:
            uv[mesh.faces.reshape(-1)] = mesh.corner_uvs.reshape(-1, 2)
        return cls(np.arange(mesh.n_vertices), vertex_material, uv)


def vertex_heights(n_vertices: int, samples: VertexSamples, fields: dict[int, np.ndarray],
                   uv_scales: dict[int, tuple[float, float]] | None = None) -> np.ndarray:
    """Average sampled height for every vertex (0 where no field applies)."""
    total = np.zeros(n_vertices)
    count = np.zeros(n_vertices)
    for mat, field in fields.items():
        sel = samples.material == mat
        if not sel.any():
            continue
        uv = samples.uv[sel]
        if uv_scales and mat in uv_scales:
            uv = uv * np.asarray(uv_scales[mat])
        vals = sample_bilinear(field, uv)
        np.add.at(total, samples.vertex[sel], vals)
        np.add.at(count, samples.vertex[sel], 1)
    return np.where(count > 0, total / np.maximum(count, 1), 0.0)


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
