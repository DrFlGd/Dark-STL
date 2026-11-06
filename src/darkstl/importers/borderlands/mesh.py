"""Turn Unreal meshes into one welded TriMesh in the export frame.

Unreal is left-handed with Z up and measures in centimetres; Dark STL's frame is
right-handed, Z up, in metres. Mirroring Y converts one into the other, which
also reverses the triangles' winding, so faces are re-wound to agree with the
(mirrored) stored normals, as the other importers do.
"""

from __future__ import annotations

import numpy as np

from ...geometry.mesh import TriMesh, weld

UNIT = 0.01  # Unreal units (cm) -> metres
MIRROR = np.array([1.0, -1.0, 1.0])


def to_export_frame(positions: np.ndarray) -> np.ndarray:
    return np.asarray(positions, np.float64) * MIRROR * UNIT


def build_trimesh(positions: np.ndarray, normals: np.ndarray | None, uvs: np.ndarray | None, ranges: list,
                  do_weld: bool = True) -> TriMesh:
    """``ranges``: (triangles (T, 3) vertex indices, material index, group index) per piece kept."""
    p = to_export_frame(positions)
    n = None if normals is None else np.asarray(normals, np.float64) * MIRROR
    faces, mats, groups = [], [], []
    for tris, material, group in ranges:
        tris = np.asarray(tris, np.int64).reshape(-1, 3)
        tris = tris[(tris < len(p)).all(axis=1)]
        if len(tris) == 0:
            continue
        faces.append(tris[:, [0, 2, 1]])  # mirrored: wind the other way
        mats.append(np.full(len(tris), material, np.int32))
        groups.append(np.full(len(tris), group, np.int32))
    if not faces:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
    f = np.concatenate(faces)
    uv = np.zeros((len(p), 2), np.float32) if uvs is None else np.asarray(uvs, np.float32)
    mesh = TriMesh(p, f, uv[f], np.concatenate(mats), np.concatenate(groups),
                   corner_normals=None)
    if n is not None:
        length = np.linalg.norm(n, axis=1, keepdims=True)
        n = n / np.where(length > 0, length, 1)
        mesh.corner_normals = n[f].astype(np.float32)
        if (length > 0).any():
            flip = np.einsum("ij,ij->i", mesh.face_cross(), mesh.corner_normals.sum(axis=1)) < 0
            if flip.any():
                mesh.faces[flip] = mesh.faces[flip][:, [0, 2, 1]]
                mesh.corner_uvs[flip] = mesh.corner_uvs[flip][:, [0, 2, 1]]
                mesh.corner_normals[flip] = mesh.corner_normals[flip][:, [0, 2, 1]]
    if do_weld:
        mesh = weld(mesh)
    return mesh


__all__ = ["MIRROR", "UNIT", "build_trimesh", "to_export_frame"]
