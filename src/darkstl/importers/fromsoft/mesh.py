"""Turn a parsed FLVER into a single welded TriMesh in the export frame.

Export frame: right-handed, Z up, metres. The games use a left-handed, Y-up
frame, so (x, y, z) in game space becomes (x, z, y) here: swapping two axes is
a reflection, which is exactly the handedness change needed. Triangle winding
is then fixed per face so that it agrees with the stored vertex normals,
which makes outward faces counter-clockwise regardless of how each game
stored them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...geometry.mesh import TriMesh, weld
from .formats.flver import Flver

GAME_TO_EXPORT = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]])


@dataclass
class FlverMeshOptions:
    include_meshes: set[int] | None = None  # FLVER mesh indices to keep (None = all)
    apply_bone_transforms: bool = True  # move rigid (non-skinned) meshes into model space
    uv_channel: int = 0
    weld: bool = True
    fix_orientation: bool = True


def _static_vertex_bones(flver: Flver, mesh) -> np.ndarray | None:
    """Global bone index for each vertex of a non-skinned mesh, or None."""
    n_bones = len(flver.bones)
    if n_bones == 0:
        return None
    vd = mesh.vertices
    if vd.bone_indices is not None:
        local = vd.bone_indices[:, 0].astype(np.int64)
        if mesh.bone_indices:
            table = np.asarray(mesh.bone_indices, np.int64)
            local = np.where((local >= 0) & (local < len(table)), table[np.clip(local, 0, len(table) - 1)], -1)
        idx = local
    else:
        idx = np.full(vd.count, mesh.default_bone_index, np.int64)
    idx = np.where((idx >= 0) & (idx < n_bones), idx, -1)
    return idx


def flver_to_trimesh(flver: Flver, options: FlverMeshOptions | None = None) -> TriMesh:
    options = options or FlverMeshOptions()
    world = flver.bone_world_matrices() if (options.apply_bone_transforms and flver.bones) else None

    positions, normals, faces, uvs, mats, groups = [], [], [], [], [], []
    offset = 0
    for mi, mesh in enumerate(flver.meshes):
        if options.include_meshes is not None and mi not in options.include_meshes:
            continue
        tris = mesh.triangles()
        if len(tris) == 0:
            continue
        vd = mesh.vertices
        p = vd.positions.astype(np.float64)
        n = vd.normals.astype(np.float64) if vd.normals is not None else None
        if world is not None and not mesh.dynamic:
            bones = _static_vertex_bones(flver, mesh)
            if bones is not None and (bones >= 0).any():
                mats4 = np.where((bones >= 0)[:, None, None], world[np.clip(bones, 0, None)], np.eye(4))
                p = np.einsum("nij,nj->ni", mats4[:, :3, :3], p) + mats4[:, :3, 3]
                if n is not None:
                    n = np.einsum("nij,nj->ni", mats4[:, :3, :3], n)
        if options.uv_channel < len(vd.uvs):
            uv = vd.uvs[options.uv_channel]
        elif vd.uvs:
            uv = vd.uvs[0]
        else:
            uv = np.zeros((vd.count, 2), np.float32)
        positions.append(p)
        normals.append(n if n is not None else np.zeros_like(p))
        faces.append(tris + offset)
        uvs.append(uv[tris])
        mats.append(np.full(len(tris), mesh.material_index, np.int32))
        groups.append(np.full(len(tris), mi, np.int32))
        offset += len(p)

    if not faces:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))

    p = np.concatenate(positions) @ GAME_TO_EXPORT.T
    n = np.concatenate(normals) @ GAME_TO_EXPORT.T
    f = np.concatenate(faces)
    corner_uvs = np.concatenate(uvs).astype(np.float32)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    n = n / np.where(length > 0, length, 1)  # missing normals stay zero
    mesh = TriMesh(p, f, corner_uvs, np.concatenate(mats), np.concatenate(groups),
                   corner_normals=n[f].astype(np.float32))

    if options.fix_orientation:
        flip = np.einsum("ij,ij->i", mesh.face_cross(), mesh.corner_normals.sum(axis=1)) < 0
        if flip.any():
            mesh.faces[flip] = mesh.faces[flip][:, [0, 2, 1]]
            mesh.corner_uvs[flip] = mesh.corner_uvs[flip][:, [0, 2, 1]]
            mesh.corner_normals[flip] = mesh.corner_normals[flip][:, [0, 2, 1]]
    if options.weld:
        mesh = weld(mesh)
    return mesh
