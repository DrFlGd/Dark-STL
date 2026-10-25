"""Turn a parsed CMesh into a single welded TriMesh in the export frame.

Cyberpunk's game space is already right-handed and Z up in metres, so the
positions are used as they are. Triangle winding is fixed per face to agree
with the stored normals, as for the other importers.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...geometry.mesh import TriMesh, weld
from .formats.mesh import RedMesh


@dataclass
class RedMeshOptions:
    include_chunks: set[int] | None = None  # chunk indices to keep (None = all)
    all_lods: bool = False  # keep every level of detail, not only the full one
    uv_channel: int = 0
    weld: bool = True
    fix_orientation: bool = True


def chunks_to_trimesh(m: RedMesh, options: RedMeshOptions | None = None) -> TriMesh:
    options = options or RedMeshOptions()
    positions, normals, faces, uvs, mats, groups = [], [], [], [], [], []
    offset = 0
    for c in m.chunks:
        if options.include_chunks is not None and c.index not in options.include_chunks:
            continue
        if not options.all_lods and options.include_chunks is None and c.lod_level != 0:
            continue
        tris = c.triangles()
        if len(tris) == 0 or c.vertex_count == 0:
            continue
        tris = tris[(tris < c.vertex_count).all(axis=1)]
        if len(tris) == 0:
            continue
        p = c.positions.astype(np.float64)
        n = c.normals.astype(np.float64) if c.normals is not None else np.zeros_like(p)
        if options.uv_channel < len(c.uvs):
            uv = c.uvs[options.uv_channel]
        elif c.uvs:
            uv = c.uvs[0]
        else:
            uv = np.zeros((c.vertex_count, 2), np.float32)
        positions.append(p)
        normals.append(n)
        faces.append(tris + offset)
        uvs.append(uv[tris])
        mats.append(np.full(len(tris), c.material_index, np.int32))
        groups.append(np.full(len(tris), c.index, np.int32))
        offset += len(p)

    if not faces:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))

    p = np.concatenate(positions)
    n = np.concatenate(normals)
    f = np.concatenate(faces)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = n / np.where(length > 0, length, 1)
    mesh = TriMesh(p, f, np.concatenate(uvs).astype(np.float32), np.concatenate(mats), np.concatenate(groups),
                   corner_normals=n[f].astype(np.float32))
    if options.fix_orientation and (length > 0).any():
        flip = np.einsum("ij,ij->i", mesh.face_cross(), mesh.corner_normals.sum(axis=1)) < 0
        if flip.any():
            mesh.faces[flip] = mesh.faces[flip][:, [0, 2, 1]]
            mesh.corner_uvs[flip] = mesh.corner_uvs[flip][:, [0, 2, 1]]
            mesh.corner_normals[flip] = mesh.corner_normals[flip][:, [0, 2, 1]]
    if options.weld:
        mesh = weld(mesh)
    return mesh


__all__ = ["RedMeshOptions", "chunks_to_trimesh"]
