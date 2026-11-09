"""id Tech 7/8 skeletons (``skeleton`` resources, named ``.md6skl``).

After a 4-byte prefix, a header of 16-bit offsets (relative to the end of the prefix), then per joint (padded to a
multiple of 8) a local rotation (x, y, z, w), scale and translation, the parent table, the inverse bind matrices
(3x4, rows) and, after the header's size, the joint names. DOOM: The Dark Ages's header (``darkages``) starts with
32-bit sizes and offsets (size, loaded size, base pose, inverse base pose, two hashes) before the 16-bit ones.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from ....errors import FormatError
from .reader import Reader


@dataclass
class Skeleton:
    names: list[str]
    parents: np.ndarray  # (n,) int
    rotations: np.ndarray  # (n, 4) x, y, z, w
    scales: np.ndarray  # (n, 3)
    translations: np.ndarray  # (n, 3)

    def __len__(self) -> int:
        return len(self.names)

    def bind_local(self) -> np.ndarray:
        return trs_matrices(self.rotations, self.scales, self.translations)


def quat_matrices(q: np.ndarray) -> np.ndarray:
    """(n, 4) quaternions x, y, z, w -> (n, 3, 3) rotation matrices (column vectors)."""
    q = np.asarray(q, np.float64)
    q = q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    m = np.empty(q.shape[:-1] + (3, 3))
    m[..., 0, 0] = 1 - 2 * (y * y + z * z)
    m[..., 0, 1] = 2 * (x * y - z * w)
    m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w)
    m[..., 1, 1] = 1 - 2 * (x * x + z * z)
    m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w)
    m[..., 2, 1] = 2 * (y * z + x * w)
    m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def trs_matrices(rot: np.ndarray, scale: np.ndarray, trans: np.ndarray) -> np.ndarray:
    rot = np.asarray(rot, np.float64)
    out = np.zeros(rot.shape[:-1] + (4, 4))
    out[..., :3, :3] = quat_matrices(rot) * np.asarray(scale, np.float64)[..., None, :]
    out[..., :3, 3] = trans
    out[..., 3, 3] = 1.0
    return out


def read_skeleton(data: bytes, darkages: bool = False) -> Skeleton:
    if len(data) < 64:
        raise FormatError("not a skeleton (too short)")
    base = 4
    if darkages:
        size, _loaded, pose_off, inv_off, _name_hash, _parent_hash, n, _handles, parent_off = struct.unpack_from(
            "<6i3h", data, base)
    else:
        (size, n, _users, _crc, pose_off, inv_off, parent_off) = struct.unpack_from("<7H", data, base)
    if n <= 0 or n > 4096:
        raise FormatError("not a skeleton (joint count)")
    n8 = (n + 7) & ~7
    p = base + pose_off
    rot = np.frombuffer(data, "<f4", n8 * 4, p).reshape(n8, 4)
    scl = np.frombuffer(data, "<f4", n8 * 3, p + n8 * 16).reshape(n8, 3)
    trn = np.frombuffer(data, "<f4", n8 * 3, p + n8 * 28).reshape(n8, 3)
    parents = np.frombuffer(data, "<i2", n8, base + parent_off).astype(np.int64)
    del inv_off  # the bind pose is rebuilt from the local transforms
    if not 0 < base + size < len(data):
        raise FormatError("not a skeleton (its names are missing)")
    r = Reader(data, base + size)
    names = [r.string() for _ in range(n)]
    return Skeleton(names, parents[:n].copy(), rot[:n].astype(np.float64), scl[:n].astype(np.float64),
                    trn[:n].astype(np.float64))
