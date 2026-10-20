"""Triangle mesh container shared by the whole pipeline.

Per-corner ("wedge") attributes are used for UVs so that a welded vertex can
sit on a UV seam without splitting the surface: every face keeps its own UVs
for each of its three corners.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np


@dataclass
class TriMesh:
    vertices: np.ndarray  # (V, 3) float64
    faces: np.ndarray  # (F, 3) int64
    corner_uvs: np.ndarray | None = None  # (F, 3, 2) float32
    face_material: np.ndarray | None = None  # (F,) int32, index into a material list
    face_group: np.ndarray | None = None  # (F,) int32, source sub-mesh index
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.vertices = np.asarray(self.vertices, np.float64).reshape(-1, 3)
        self.faces = np.asarray(self.faces, np.int64).reshape(-1, 3)
        n = len(self.faces)
        if self.face_material is None:
            self.face_material = np.zeros(n, np.int32)
        if self.face_group is None:
            self.face_group = np.zeros(n, np.int32)

    # -- basic properties --------------------------------------------------

    @property
    def n_vertices(self) -> int:
        return len(self.vertices)

    @property
    def n_faces(self) -> int:
        return len(self.faces)

    def copy(self) -> "TriMesh":
        return replace(
            self,
            vertices=self.vertices.copy(),
            faces=self.faces.copy(),
            corner_uvs=None if self.corner_uvs is None else self.corner_uvs.copy(),
            face_material=self.face_material.copy(),
            face_group=self.face_group.copy(),
            metadata=dict(self.metadata),
        )

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if self.n_vertices == 0:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(0), self.vertices.max(0)

    def diagonal(self) -> float:
        lo, hi = self.bounds()
        return float(np.linalg.norm(hi - lo))

    def triangles(self) -> np.ndarray:
        return self.vertices[self.faces]

    def face_cross(self) -> np.ndarray:
        tri = self.triangles()
        return np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])

    def face_areas(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self.face_cross(), axis=1)

    def surface_area(self) -> float:
        return float(self.face_areas().sum())

    def face_normals(self) -> np.ndarray:
        c = self.face_cross()
        n = np.linalg.norm(c, axis=1, keepdims=True)
        return c / np.where(n > 0, n, 1)

    def vertex_normals(self) -> np.ndarray:
        """Area-weighted smooth vertex normals."""
        c = self.face_cross()
        vn = np.zeros_like(self.vertices)
        for k in range(3):
            np.add.at(vn, self.faces[:, k], c)
        n = np.linalg.norm(vn, axis=1, keepdims=True)
        return vn / np.where(n > 0, n, 1)

    def edges_unique(self) -> tuple[np.ndarray, np.ndarray]:
        """Unique undirected edges and, for every face corner k, the id of edge (k, k+1)."""
        a = self.faces
        b = np.roll(self.faces, -1, axis=1)
        lo = np.minimum(a, b).reshape(-1)
        hi = np.maximum(a, b).reshape(-1)
        keys = lo * np.int64(max(self.n_vertices, 1)) + hi
        uniq, inverse = np.unique(keys, return_inverse=True)
        n = np.int64(max(self.n_vertices, 1))
        edges = np.stack([uniq // n, uniq % n], axis=1)
        return edges, inverse.reshape(-1, 3)

    def edge_lengths(self) -> np.ndarray:
        edges, _ = self.edges_unique()
        return np.linalg.norm(self.vertices[edges[:, 0]] - self.vertices[edges[:, 1]], axis=1)

    def subset(self, face_mask: np.ndarray) -> "TriMesh":
        """Mesh made of the selected faces, with unused vertices dropped."""
        faces = self.faces[face_mask]
        used, inverse = np.unique(faces, return_inverse=True)
        return TriMesh(
            self.vertices[used],
            inverse.reshape(-1, 3),
            None if self.corner_uvs is None else self.corner_uvs[face_mask],
            self.face_material[face_mask],
            self.face_group[face_mask],
            dict(self.metadata),
        )

    def remove_degenerate(self, area_eps: float = 0.0) -> "TriMesh":
        f = self.faces
        ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        if area_eps > 0:
            ok &= self.face_areas() > area_eps
        if ok.all():
            return self
        return self.subset(ok)

    def vertex_uvs(self) -> np.ndarray | None:
        """One UV per vertex (averaged over its corners); seams get blended values."""
        if self.corner_uvs is None:
            return None
        acc = np.zeros((self.n_vertices, 2))
        cnt = np.zeros(self.n_vertices)
        for k in range(3):
            np.add.at(acc, self.faces[:, k], self.corner_uvs[:, k])
            np.add.at(cnt, self.faces[:, k], 1)
        return acc / np.maximum(cnt, 1)[:, None]


def weld(mesh: TriMesh, tolerance: float | None = None) -> TriMesh:
    """Merge vertices that share a position (within ``tolerance``).

    Game meshes duplicate vertices along UV seams and hard edges; welding makes
    the surface continuous so that subdivision and displacement do not tear it.
    """
    if mesh.n_vertices == 0:
        return mesh
    if tolerance is None:
        tolerance = max(mesh.diagonal() * 1e-7, 1e-9)
    keys = np.round(mesh.vertices / tolerance).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    vertices = mesh.vertices[first]
    faces = inverse[mesh.faces]
    out = TriMesh(vertices, faces, mesh.corner_uvs, mesh.face_material, mesh.face_group, dict(mesh.metadata))
    return out.remove_degenerate()


def concatenate(meshes: list[TriMesh]) -> TriMesh:
    meshes = [m for m in meshes if m.n_faces]
    if not meshes:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64))
    offsets = np.cumsum([0] + [m.n_vertices for m in meshes[:-1]])
    has_uv = all(m.corner_uvs is not None for m in meshes)
    return TriMesh(
        np.concatenate([m.vertices for m in meshes]),
        np.concatenate([m.faces + o for m, o in zip(meshes, offsets)]),
        np.concatenate([m.corner_uvs for m in meshes]) if has_uv else None,
        np.concatenate([m.face_material for m in meshes]),
        np.concatenate([m.face_group for m in meshes]),
        dict(meshes[0].metadata),
    )
