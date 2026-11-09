"""id Tech 7 skinned models (``baseModel`` resources, named ``.md6mesh``): monsters, characters, weapons.

A header naming the skeleton, the joint remap table, per mesh its name, material decl and five levels of detail,
material infos, decal data, and the memory and disk layouts of each level's StreamDB piece.

DOOM: The Dark Ages's (:func:`read_md6_model_darkages`) names the joints it skins with in two tables (skinned joints,
and extra joints that repeat earlier ones) instead of a remap table, has a radius per skeleton joint (so its
skeleton's joint count is needed to read it), and per level of detail joints per vertex and a joint offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .geometry import DiskLayout, LodInfo
from .reader import Reader
from .staticmodel import LODS, read_disk_layout, read_memory_layout


@dataclass
class Md6Mesh:
    name: str
    material: str
    joint_offset: int
    lods: list[LodInfo | None]


@dataclass
class Md6Model:
    skeleton: str
    joint_remap: np.ndarray  # uint8
    meshes: list[Md6Mesh]
    disk: list[DiskLayout] = field(default_factory=list)
    resolved: np.ndarray | None = None  # The Dark Ages: raw joint index (plus offset) -> skeleton joint

    def joint_lookup(self) -> np.ndarray:
        """Raw joint index (plus a mesh's offset) -> skeleton joint: the inverse of the remap table."""
        if self.resolved is not None:
            lookup = np.zeros(max(256, len(self.resolved) + 256), np.int32)
            lookup[:len(self.resolved)] = self.resolved
            return lookup
        lookup = np.zeros(256, np.int32)
        for i, j in enumerate(self.joint_remap):
            lookup[int(j)] = i
        return lookup


def _md6_lod(r: Reader) -> LodInfo:
    nv, nf = r.unpack("II")
    r.vec(6)
    offset = r.vec(3)
    scale = r.f32()
    uv_offset = r.vec(2)
    uv_scale = r.f32()
    mask = r.u32()
    r.vec(2)
    return LodInfo(nv, nf, mask, offset, scale, uv_offset, uv_scale)


def read_md6_model(data: bytes) -> Md6Model:
    r = Reader(data)
    skeleton = r.string()
    r.vec(6)
    r.u8()
    r.u32()
    remap = np.frombuffer(r.bytes(r.u16()), np.uint8).copy()
    r.vec(6)
    if r.u32() != LODS:
        raise FormatError("not a DOOM Eternal skinned model (level of detail count)")
    r.vec(LODS)
    r.u32()
    r.bytes(36)
    n_mesh = r.u32()
    if n_mesh > 10000:
        raise FormatError("not a skinned model")
    meshes = []
    for _ in range(n_mesh):
        name, material = r.string(), r.string()
        r.u8()
        r.u32()
        _u1, joint_offset, _u3, _hash = r.unpack("IIII")
        lods: list[LodInfo | None] = []
        for _ in range(LODS):
            lods.append(None if r.u32() else _md6_lod(r))
        r.u8()
        r.u32()
        meshes.append(Md6Mesh(name, material, joint_offset, lods))
    for _ in range(r.u32()):  # material infos
        r.string()
        r.unpack("III")
    r.string()  # decal material
    counts = [r.u32() for _ in range(r.u32())]
    for c in counts:
        r.bytes(c * 4)
    layouts = [read_memory_layout(r) for _ in range(r.u32())]
    disk = [read_disk_layout(r, layouts[i:i + 1]) for i in range(LODS)]
    return Md6Model(skeleton, remap, meshes, disk)


def _darkages_lod(r: Reader) -> tuple[LodInfo, int]:
    nv, nf, influence = r.unpack("IIB")
    r.vec(6)  # bounds
    offset = r.vec(3)
    scale = r.f32()
    uv_offset = r.vec(2)
    uv_scale = r.f32()
    mask = r.u32()
    r.vec(2)
    joint_offset = r.i16()
    return LodInfo(nv, nf, mask, offset, scale, uv_offset, uv_scale, influence), joint_offset


def read_md6_model_darkages(data: bytes, skeleton_joints: int) -> Md6Model:
    """A DOOM: The Dark Ages skinned model; ``skeleton_joints`` is its skeleton's joint count (see
    :func:`model_skeleton`)."""
    r = Reader(data)
    skeleton = r.string()
    n8 = (skeleton_joints + 7) & ~7
    r.vec(6)  # bounds expansion
    r.u8()
    r.string()
    skinned = np.frombuffer(r.bytes(2 * r.u16()), "<i2").astype(np.int32)
    extra = np.frombuffer(r.bytes(2 * r.u16()), "<u2").astype(np.int32)
    r.vec(6)  # bounds
    n_lods = r.i32()
    if not 0 < n_lods <= 16:
        raise FormatError("not a DOOM: The Dark Ages skinned model (level of detail count)")
    r.vec(5)
    for _ in range(r.u32()):  # blend shape names
        r.string()
    r.vec(9)
    r.vec(n8)  # joint bound radii
    n_mesh = r.u32()
    if n_mesh > 10000:
        raise FormatError("not a skinned model")
    meshes = []
    for _ in range(n_mesh):
        name, material = r.string(), r.string()
        r.unpack("iih")
        lods: list[LodInfo | None] = []
        joint_offset = 0
        for _ in range(n_lods):
            if r.u32():
                lods.append(None)
                continue
            lod, off = _darkages_lod(r)
            if not any(lo is not None for lo in lods):
                joint_offset = off
            lods.append(lod)
        r.u8()
        r.u32()
        meshes.append(Md6Mesh(name, material, joint_offset, lods))
    for _ in range(r.u32()):  # surface infos
        r.string()
        r.unpack("iii")
    n_model_wounds, _n_mesh_wounds, _unknown = r.unpack("iii")
    for _ in range(n_model_wounds):
        r.string()
        r.vec(4)
        for _ in range(r.u32()):
            r.string()
            r.unpack("i5i")
    r.string()  # decal material
    sizes = [r.u32() for _ in range(r.u32())]
    r.bytes(4 * sum(sizes))
    n_streams = r.u32()
    if n_streams > 64:
        raise FormatError("not a skinned model (stream count)")
    r.bytes(12 * n_streams)  # wound offsets
    disk = [read_disk_layout(r, []) for _ in range(n_streams)]
    len8 = (len(skinned) + 7) & ~7
    resolved = np.zeros(len8 + len(extra), np.int32)
    resolved[:len(skinned)] = skinned
    for i, e in enumerate(extra):
        resolved[len8 + i] = resolved[e] if e < len(resolved) else 0
    return Md6Model(skeleton, np.zeros(0, np.uint8), meshes, disk, resolved)


def model_skeleton(data: bytes) -> str:
    """Only the name of the skeleton a skinned model is for."""
    return Reader(data).string()
