"""id Tech 7 skinned models (``baseModel`` resources, named ``.md6mesh``): monsters, characters, weapons.

A header naming the skeleton, the joint remap table, per mesh its name, material decl and five levels of detail,
material infos, decal data, and the memory and disk layouts of each level's StreamDB piece.
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

    def joint_lookup(self) -> np.ndarray:
        """Raw joint index (plus a mesh's offset) -> skeleton joint: the inverse of the remap table."""
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
