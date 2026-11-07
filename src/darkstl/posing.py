"""Posing a mesh from a skeleton and per-vertex bone weights, the same way for every game.

Importers pose their own models from the game's files. A model package (see :mod:`darkstl.package`)
keeps what that needs instead, the skeleton (:class:`darkstl.model.Rig`), the weights and the
animations sampled per frame, and poses with the functions here.
"""

from __future__ import annotations

import numpy as np

from .geometry.mesh import TriMesh


def compose_hierarchy(local: np.ndarray, parents: np.ndarray) -> np.ndarray:
    """World matrices from local ones: each bone's local matrix on top of its parent's world matrix."""
    local = np.asarray(local, np.float64)
    world = np.array(local, copy=True)
    parents = np.asarray(parents, np.int64)
    done = np.zeros(len(local), bool)
    for start in range(len(local)):
        chain = []
        i = start
        while 0 <= i < len(local) and not done[i] and i not in chain and len(chain) < 4096:
            chain.append(i)
            i = int(parents[i])
        for j in reversed(chain):
            p = int(parents[j])
            if 0 <= p < len(local) and p != j and done[p]:
                world[j] = world[p] @ local[j]
            done[j] = True
    return world


def pose_world(names: list[str], parents: np.ndarray, bind_local: np.ndarray,
               anim_names: list[str], anim_local: np.ndarray) -> np.ndarray:
    """World matrices of a skeleton's bones in a pose: bones the animation names take its local matrix,
    the rest keep their bind-pose one and follow their parents."""
    local = np.array(bind_local, np.float64, copy=True).reshape(-1, 4, 4)
    index = {n: i for i, n in enumerate(anim_names)}
    for i, name in enumerate(names):
        j = index.get(name)
        if j is not None:
            local[i] = anim_local[j]
    return compose_hierarchy(local, parents)


def blend(joints: np.ndarray, weights: np.ndarray, matrices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each vertex's weighted sum of its bones' matrices (V, 4, 4), and whether it has any weight at all."""
    joints = np.asarray(joints, np.int64)
    weights = np.asarray(weights, np.float64)
    valid = (joints >= 0) & (joints < len(matrices)) & (weights > 0)
    w = np.where(valid, weights, 0.0)
    total = w.sum(axis=1)
    j = np.clip(joints, 0, max(len(matrices) - 1, 0))
    blended = np.einsum("nk,nkij->nij", w, matrices[j]) if len(matrices) else np.zeros((len(joints), 4, 4))
    still = total <= 0
    blended[still] = np.eye(4)
    return blended, ~still


def pose_mesh(mesh: TriMesh, joints: np.ndarray, weights: np.ndarray, skin: np.ndarray) -> TriMesh:
    """``mesh`` (in its bind pose) moved by linear blend skinning: ``skin`` holds per bone the matrix that
    carries a vertex from the bind pose into the pose. Vertices without weights stay where they are."""
    blended, _ = blend(joints, weights, skin)
    out = mesh.copy()
    out.vertices = np.einsum("nij,nj->ni", blended[:, :3, :3], mesh.vertices) + blended[:, :3, 3]
    if mesh.corner_normals is not None and len(mesh.faces):
        rot = blended[:, :3, :3][mesh.faces]  # (F, 3, 3, 3): each corner's vertex matrix
        n = np.einsum("fkij,fkj->fki", rot, mesh.corner_normals.astype(np.float64))
        length = np.linalg.norm(n, axis=2, keepdims=True)
        out.corner_normals = (n / np.where(length > 0, length, 1)).astype(np.float32)
    out.metadata.pop("skin", None)
    return out


__all__ = ["blend", "compose_hierarchy", "pose_mesh", "pose_world"]
