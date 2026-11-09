"""Posing a mesh from a skeleton and per-vertex bone weights, the same way for every game.

Importers pose their own models from the game's files. A model package (see :mod:`darkstl.package`)
keeps what that needs instead, the skeleton (:class:`darkstl.model.Rig`), the weights and the
animations sampled per frame, and poses with the functions here.

Bone adjustments (the Pose tab's bones) are worked out here for every importer alike: each adjusted bone's
local matrix, as the animation (or the bind pose) gives it, is turned about the bone's own axes
(``local @ turn``), its children follow, and the bind-pose mesh is skinned to that pose
(:func:`pose_adjusted`). Turns are three angles in degrees, X then Y then Z as three.js's "XYZ" order
makes them (``Rx @ Ry @ Rz``), so the page's gizmo and the engine agree.

Mirroring an adjustment to the other side of the body finds the bone's partner by its name (``L_Hand``
and ``R_Hand``, ``hand_l`` and ``hand_r``, ``Bip01 L Hand``, ``LeftHand``, see :func:`mirror_name`), and
reflects the turn through the plane between the sides, as the bones stand in the bind pose.
"""

from __future__ import annotations

import math
import re

import numpy as np

from .errors import FormatError
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


# ---------------------------------------------------------------------------
# bone adjustments


def turn_matrix(deg) -> np.ndarray:
    """A turn of ``(x, y, z)`` degrees about the X, Y and Z axes as three.js's "XYZ" order makes it:
    ``Rx @ Ry @ Rz`` (3x3)."""
    x, y, z = (math.radians(float(a)) for a in (list(deg) + [0.0, 0.0, 0.0])[:3])
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rx @ ry @ rz


def turn_angles(m: np.ndarray) -> tuple[float, float, float]:
    """The ``(x, y, z)`` degrees :func:`turn_matrix` makes ``m`` (a rotation) from, as three.js reads them."""
    m = np.asarray(m, np.float64)[:3, :3]
    y = math.asin(min(max(m[0, 2], -1.0), 1.0))
    if abs(m[0, 2]) < 0.9999999:
        x = math.atan2(-m[1, 2], m[2, 2])
        z = math.atan2(-m[0, 1], m[0, 0])
    else:
        x = math.atan2(m[2, 1], m[1, 1])
        z = 0.0
    return tuple(_tidy(math.degrees(a)) for a in (x, y, z))


def _tidy(a: float) -> float:
    a = round(a, 3)
    return 0.0 if a == 0 else a


def clean_bones(bones) -> dict[str, tuple[float, float, float]]:
    """Bone adjustments as :class:`darkstl.model.MeshOptions` keeps them: ``{name: (x, y, z)}`` degrees,
    each angle in -180..180, rounded to a thousandth, bones with no turn left out."""
    out = {}
    for name, deg in dict(bones or {}).items():
        try:
            values = [float(a) for a in (list(deg) + [0.0, 0.0, 0.0])[:3]]
        except (TypeError, ValueError):
            raise FormatError(f"The adjustment of {name} isn't three angles") from None
        if not all(math.isfinite(a) for a in values):
            raise FormatError(f"The adjustment of {name} isn't three angles")
        values = [_tidy(((a + 180.0) % 360.0) - 180.0) for a in values]
        if any(values):
            out[str(name)] = tuple(values)
    return dict(sorted(out.items()))


def local_pose(rig, pose: tuple[str, int] | None = None, bones: dict | None = None) -> np.ndarray:
    """(B, 4, 4) local matrices of the rig's bones: its bind pose, the animation frame ``pose`` names on
    the bones it moves, then each adjusted bone turned about its own axes."""
    local = np.array(rig.bind_local, np.float64, copy=True).reshape(-1, 4, 4)
    if pose is not None:
        names, anim_local = rig.pose_locals(pose[0], int(pose[1]))
        index = {n: i for i, n in enumerate(names)}
        for i, name in enumerate(rig.names):
            j = index.get(name)
            if j is not None:
                local[i] = anim_local[j]
    if bones:
        index = {n: i for i, n in enumerate(rig.names)}
        for name, deg in bones.items():
            i = index.get(name)
            if i is None:
                continue
            t = np.eye(4)
            t[:3, :3] = turn_matrix(deg)
            local[i] = local[i] @ t
    return local


def bone_world(rig, pose: tuple[str, int] | None = None, bones: dict | None = None) -> np.ndarray:
    """(B, 4, 4) world matrices of the rig's bones in ``pose`` with ``bones`` adjusted (export frame)."""
    return compose_hierarchy(local_pose(rig, pose, bones), rig.parents)


def pose_adjusted(model, options) -> TriMesh:
    """``model``'s mesh in ``options.pose`` with ``options.bones`` adjusted: its bind-pose mesh skinned to
    the rig's bones posed so. Its skin weights stay with it (``metadata["skin"]``, as the bind pose
    carries them), with the bone matrices it was posed by (``metadata["pose"]``), for a glTF export that
    keeps the skeleton in the adjusted pose."""
    from dataclasses import replace

    rig = model.rig
    bind = model.build(replace(options, pose=None, bones=None))
    skin = bind.metadata.get("skin")
    if skin is None:
        raise FormatError(f"{model.name}'s bones can't be adjusted: its mesh isn't weighted to its bones")
    world = bone_world(rig, options.pose, options.bones)
    out = pose_mesh(bind, skin[0], skin[1], world @ np.linalg.pinv(rig.bind_world))
    out.metadata["skin"] = skin
    out.metadata["pose"] = world
    return out


def orthonormal(m: np.ndarray) -> np.ndarray:
    """The rotation (or reflection) part of a 3x3 matrix: its polar factor, without scale or shear."""
    u, _s, vt = np.linalg.svd(np.asarray(m, np.float64)[:3, :3])
    return u @ vt


def quaternion(m: np.ndarray) -> tuple[list[float], bool]:
    """A bone's world rotation as a quaternion (x, y, z, w), and whether its axes are mirrored (a frame
    with one axis reversed: the quaternion is then of the frame with its Z axis turned back)."""
    r = orthonormal(m)
    flip = bool(np.linalg.det(r) < 0)
    if flip:
        r = r @ np.diag([1.0, 1.0, -1.0])
    t = np.trace(r)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = [(r[2, 1] - r[1, 2]) / s, (r[0, 2] - r[2, 0]) / s, (r[1, 0] - r[0, 1]) / s, 0.25 * s]
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2
        q = [0.25 * s, (r[0, 1] + r[1, 0]) / s, (r[0, 2] + r[2, 0]) / s, (r[2, 1] - r[1, 2]) / s]
    elif r[1, 1] > r[2, 2]:
        s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2
        q = [(r[0, 1] + r[1, 0]) / s, 0.25 * s, (r[1, 2] + r[2, 1]) / s, (r[0, 2] - r[2, 0]) / s]
    else:
        s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2
        q = [(r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s, 0.25 * s, (r[1, 0] - r[0, 1]) / s]
    return [round(float(x), 6) for x in q], flip


# ---------------------------------------------------------------------------
# left and right


_WORDS = {"left": "right", "right": "left", "Left": "Right", "Right": "Left", "LEFT": "RIGHT", "RIGHT": "LEFT"}
_SWAP = {"L": "R", "R": "L", "l": "r", "r": "l"}
_SEP = r"[_ .:\-]"
# (pattern, which group is the side): the forms games and tools name the sides of a skeleton with
_SIDE_FORMS = [
    re.compile(r"(Left|Right|LEFT|RIGHT|left|right)"),  # LeftArm, mixamorig:RightHand, arm_left
    re.compile(rf"^([LRlr])(?={_SEP})"),  # L_Clavicle, L Thigh (FromSoftware), l_arm_humerus (RE Engine)
    re.compile(rf"(?<={_SEP})([LRlr])$"),  # upperarm_l (Unreal), Hand.R (Blender)
    re.compile(rf"(?<={_SEP})([LRlr])(?={_SEP}|\d)"),  # Bip01 L Hand (3ds Max), c_L_arm, Finger_L1
    re.compile(r"^([LR])(?=[A-Z][a-z])"),  # LUpperArm, RThigh
]


def _swaps(name: str):
    for form in _SIDE_FORMS:
        for m in form.finditer(name):
            side = m.group(1)
            other = _WORDS.get(side) or _SWAP.get(side)
            if other:
                yield name[:m.start(1)] + other + name[m.end(1):], side


def mirror_name(name: str, names) -> str | None:
    """The bone on the other side of the body from ``name`` among ``names`` (by the side its name says),
    or None for a bone in the middle, or one whose partner isn't there."""
    known = names if isinstance(names, (set, frozenset, dict)) else set(names)
    for other, _side in _swaps(name):
        if other != name and other in known:
            return other
    return None


def side_of(name: str, names) -> str | None:
    """"left" or "right" for a bone with a partner on the other side, else None."""
    known = names if isinstance(names, (set, frozenset, dict)) else set(names)
    for other, side in _swaps(name):
        if other != name and other in known:
            return "left" if side.lower().startswith("l") else "right"
    return None


def mirror_pairs(names) -> dict[str, str]:
    """Every bone with a partner on the other side: {name: partner}."""
    known = set(names)
    return {n: p for n in names if (p := mirror_name(n, known)) is not None}


def mirror_axis(rig) -> int | None:
    """The axis (0, 1, 2 in the export frame) the two sides of the skeleton lie apart along in the bind
    pose, from the bones that pair up; None without pairs."""
    pairs = mirror_pairs(rig.names)
    if not pairs:
        return None
    index = {n: i for i, n in enumerate(rig.names)}
    pos = np.asarray(rig.bind_world, np.float64)[:, :3, 3]
    apart = np.zeros(3)
    for a, b in pairs.items():
        apart += np.abs(pos[index[a]] - pos[index[b]])
    if not apart.any():
        return 0
    return int(np.argmax(apart))


def mirror_turn(rig, bone: str, partner: str, deg, axis: int | None = None) -> tuple[float, float, float]:
    """``bone``'s turn ``deg`` reflected onto ``partner``: the same motion on the other side of the body.

    The turn is taken into the world as the bone stands in the bind pose (``B R Bᵀ``), reflected through
    the plane between the sides (``S D S``), and taken into the partner's own axes (``B'ᵀ D' B'``)."""
    index = {n: i for i, n in enumerate(rig.names)}
    axis = mirror_axis(rig) if axis is None else axis
    s = np.eye(3)
    s[axis if axis is not None else 0, axis if axis is not None else 0] = -1.0
    b = orthonormal(rig.bind_world[index[bone]])
    b2 = orthonormal(rig.bind_world[index[partner]])
    d = b @ turn_matrix(deg) @ b.T
    return turn_angles(b2.T @ (s @ d @ s) @ b2)


def mirror_bones(rig, bones: dict | None, names) -> dict[str, tuple[float, float, float]]:
    """``bones`` with the adjustment of each bone in ``names`` copied, mirrored, onto its partner (a bone
    without one takes its partner's adjustment away). Bones without a partner are left as they are."""
    out = dict(clean_bones(bones))
    known = set(rig.names)
    axis = mirror_axis(rig)
    for name in names:
        partner = mirror_name(name, known)
        if partner is None:
            continue
        deg = out.get(name)
        if deg is None:
            out.pop(partner, None)
        else:
            out[partner] = mirror_turn(rig, name, partner, deg, axis)
    return clean_bones(out)


def skeleton_signature(names) -> str:
    """What a skeleton is known by for sharing saved poses: its bone names (any order)."""
    import hashlib

    return hashlib.sha1("\n".join(sorted(set(map(str, names)))).encode("utf-8")).hexdigest()[:16]


__all__ = ["blend", "bone_world", "clean_bones", "compose_hierarchy", "local_pose", "mirror_axis", "mirror_bones",
           "mirror_name", "mirror_pairs", "mirror_turn", "orthonormal", "pose_adjusted", "pose_mesh", "pose_world",
           "quaternion", "side_of", "skeleton_signature", "turn_angles", "turn_matrix"]
