"""id Tech 7/8 static models (``model`` resources, named ``.lwo``): props, pickups, map pieces.

A header (reference point, levels of detail, surfaces, whether the geometry is streamed), per surface its material
decl and up to five levels of detail, lightmap settings and decal projections; then either the one level kept in
the container, or per level the memory layouts and disk layout of its StreamDB piece.

DOOM: The Dark Ages (``darkages``) writes a longer level of detail record (three more numbers after the counts,
the mask after them), decal projections of three quaternions, and disk layouts without memory layouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ....errors import FormatError
from .geometry import DiskLayout, LodInfo, MemoryLayout, Surface, read_interleaved
from .reader import Reader

LODS = 5
LOD_MAGIC = 0x724C4D42


@dataclass
class StaticSurface:
    material: str
    lods: list[LodInfo | None]


@dataclass
class StaticModel:
    surfaces: list[StaticSurface]
    streamable: bool
    disk: list[DiskLayout] = field(default_factory=list)  # per level, when streamable
    embedded: list[Surface] = field(default_factory=list)  # the level kept in the container, when not


def read_memory_layout(r: Reader) -> MemoryLayout:
    mask, size, n = r.unpack("III")
    masks = list(r.unpack(f"{n}I")) if n else []
    offsets = list(r.unpack(f"{n}I")) if n else []
    return MemoryLayout(mask, size, masks, offsets, r.u32())


def read_disk_layout(r: Reader, layouts: list[MemoryLayout]) -> DiskLayout:
    comp, size, stored, offset = r.unpack("IIII")
    return DiskLayout(comp, size, stored, offset, layouts)


def _darkages_lod(r: Reader) -> LodInfo:
    if r.i32() != -5:
        raise FormatError("not a DOOM: The Dark Ages static model (level of detail record)")
    nv, ne, _u1, _u2, _u3, mask = r.unpack("6I")
    r.vec(6)  # bounds
    offset = r.vec(3)
    scale = r.f32()
    uv_offset = r.vec(2)
    uv_scale = r.f32()
    r.u32()
    r.vec(2)
    if r.u32() != LOD_MAGIC:
        raise FormatError("not a static model (level of detail record)")
    return LodInfo(nv, ne // 3, mask, offset, scale, uv_offset, uv_scale)


def _static_lod(r: Reader) -> LodInfo:
    if r.i32() != -2:
        raise FormatError("not a static model (level of detail record)")
    nv, ne, mask = r.unpack("III")
    r.vec(6)  # bounds
    offset = r.vec(3)
    scale = r.f32()
    uv_offset = r.vec(2)
    uv_scale = r.f32()
    r.u32()
    r.vec(2)
    if r.u32() != LOD_MAGIC:
        raise FormatError("not a static model (level of detail record)")
    return LodInfo(nv, ne // 3, mask, offset, scale, uv_offset, uv_scale)


def read_static_model(data: bytes, darkages: bool = False) -> StaticModel:
    r = Reader(data)
    r.vec(3)
    n_lods, n_surf = r.unpack("II")
    if n_surf > 100000 or n_lods > 64:
        raise FormatError("not a static model")
    r.vec(n_lods) if n_lods else None
    streamable = bool(r.u32())
    surfaces = []
    for _ in range(n_surf):
        mat = r.string()
        r.unpack("III")
        lods: list[LodInfo | None] = []
        for _ in range(LODS):
            lods.append(None if r.u32() else (_darkages_lod(r) if darkages else _static_lod(r)))
        surfaces.append(StaticSurface(mat, lods))
    r.f32()
    r.unpack("II")
    n_axes = r.u32()
    r.bytes(n_axes * 56)
    n_proj = r.u32()
    r.bytes(n_proj * (48 if darkages else 32))
    r.string()
    r.u32()
    r.bytes(n_surf * LODS)
    model = StaticModel(surfaces, streamable)
    if streamable:
        for _ in range(LODS):
            layouts = [] if darkages else [read_memory_layout(r) for _ in range(r.u32())]
            model.disk.append(read_disk_layout(r, layouts))
    else:
        pos = r.pos
        for s in surfaces:
            lod = next((x for x in s.lods if x is not None), None)
            if lod is None:
                raise FormatError("a surface of the model has no geometry")
            surf, pos = read_interleaved(data, pos, lod)
            model.embedded.append(surf)
    return model
