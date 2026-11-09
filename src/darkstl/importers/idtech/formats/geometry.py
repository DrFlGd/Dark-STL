"""Vertex streams of id Tech 7/8 models (static ``.lwo`` models and skinned ``.md6mesh`` models).

A level of detail names its attributes with a vertex mask; each attribute is a stream of fixed-size records:
positions as three 16-bit unsigned normals times a scale plus an offset (or floats), normal and tangent as bytes
(the tangent's sign in its last byte; skinned models keep weights in the spare bits), UVs as 16-bit unsigned
normals or floats with their own scale and offset, a colour (joint indices on skinned models). Indices are 16-bit.

DOOM Eternal's StreamDB pieces say where each attribute's stream starts (memory layouts); DOOM: The Dark Ages's
don't: each surface's streams follow one another, each aligned to its record size, then its indices, and the next
surface starts on 8 bytes. The Dark Ages's skinned models name their joints in a skinning stream (4, 6 or 8 per
vertex) with the first weights after them and the last three packed into the normal and tangent's spare bits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError

POSITION_SHORT, POSITION, NORMAL_TANGENT = 0x20, 0x01, 0x14
LIGHTMAP_UV_SHORT, LIGHTMAP_UV = 0x40, 0x02
MATERIAL_UV_SHORT, MATERIAL_UV, MATERIAL_UV1, MATERIAL_UV2 = 0x020000, 0x8000, 0x08000000, 0x10000000
MATERIAL_UV1_SHORT, MATERIAL_UV2_SHORT = 0x20000000, 0x40000000
COLOR, MATERIALS = 0x08, 0x010000
SKINNING_8, SKINNING_1, SKINNING_4, SKINNING_6 = 0x80, 0x01000000, 0x02000000, 0x04000000

SIZES = {POSITION_SHORT: 8, POSITION: 12, NORMAL_TANGENT: 8, LIGHTMAP_UV_SHORT: 4, LIGHTMAP_UV: 8,
         MATERIAL_UV_SHORT: 4, MATERIAL_UV: 8, MATERIAL_UV1: 8, MATERIAL_UV2: 8, MATERIAL_UV1_SHORT: 4,
         MATERIAL_UV2_SHORT: 4, COLOR: 4, MATERIALS: 8, SKINNING_8: 12, SKINNING_1: 0, SKINNING_4: 4,
         SKINNING_6: 8}
ORDER = list(SIZES)  # how a combined mask splits into attributes
# the order attributes are interleaved in, in the small models kept inside the container
INTERLEAVED = [POSITION_SHORT, POSITION, LIGHTMAP_UV_SHORT, LIGHTMAP_UV, NORMAL_TANGENT, COLOR, MATERIAL_UV_SHORT,
               MATERIAL_UV, MATERIALS]


def split_mask(mask: int) -> list[int]:
    out = []
    for m in ORDER:
        if mask & m == m:
            out.append(m)
            mask &= ~m
    if mask:
        raise FormatError(f"vertex mask has attributes Dark STL doesn't know (0x{mask:x})")
    return out


@dataclass
class LodInfo:
    vertices: int
    triangles: int
    mask: int
    offset: tuple[float, float, float]
    scale: float
    uv_offset: tuple[float, float]
    uv_scale: float
    influence: int = 0  # joints per vertex (The Dark Ages's skinned models)


@dataclass
class MemoryLayout:
    mask: int
    size: int
    masks: list[int]
    offsets: list[int]
    index_offset: int


@dataclass
class DiskLayout:
    compression: int
    size: int
    stored: int
    offset: int
    layouts: list[MemoryLayout] = field(default_factory=list)


@dataclass
class Surface:
    """One mesh's vertices and triangles as the game keeps them, in its own space and units."""

    positions: np.ndarray  # (n, 3) float32
    normals: np.ndarray | None  # (n, 3)
    uvs: np.ndarray | None  # (n, 2)
    triangles: np.ndarray  # (m, 3) int64
    joints: np.ndarray | None = None  # (n, 4) raw joint indices (before the model's remap)
    weights: np.ndarray | None = None  # (n, 4)


def _unorm8(b: np.ndarray) -> np.ndarray:
    return b.astype(np.float32) * (2.0 / 255.0) - 1.0


def _records(buf: bytes, offset: int, stride: int, count: int, size: int) -> np.ndarray:
    """``count`` records of ``size`` bytes, ``stride`` apart from ``offset``, as a (count, size) byte array."""
    need = offset + stride * (count - 1) + size if count else offset
    if need > len(buf):
        raise FormatError("the model's vertex data is cut short")
    if not count:
        return np.zeros((0, size), np.uint8)
    arr = np.frombuffer(buf, np.uint8, stride * (count - 1) + size, offset)
    idx = np.arange(count)[:, None] * stride + np.arange(size)[None, :]
    return arr[idx]


# The Dark Ages: the scales of the three weights packed into the normal and tangent, by joints per vertex
_PACKED = {4: (45.0, 60.0), 6: (75.0, 89.0), 8: (104.0, 120.0)}


def _extra_weights(influence: int) -> int:
    """How many weights follow the joints in a Dark Ages skinning stream."""
    trailing = 0 if influence <= 2 else (3 if influence % 2 == 0 else 1)
    return max(0, influence - 1 - trailing)


def _darkages_skin(attrs: dict[int, np.ndarray], lod: LodInfo) -> tuple[np.ndarray, np.ndarray] | None:
    """(joints, weights), the four heaviest per vertex, for a Dark Ages skinning stream; None without one."""
    mode = next((m for m in (SKINNING_4, SKINNING_6, SKINNING_8) if m in attrs), None)
    if mode is None:
        return None
    n = lod.vertices
    influence = lod.influence or {SKINNING_4: 4, SKINNING_6: 6, SKINNING_8: 8}[mode]
    raw = attrs[mode]
    if influence > raw.shape[1]:
        raise FormatError(f"the model says {influence} joints per vertex, more than its skinning stream holds")
    joints = raw[:, :influence].astype(np.int32)
    weights = np.zeros((n, influence), np.float32)
    extra = min(_extra_weights(influence), raw.shape[1] - influence)
    if extra > 0:
        weights[:, 1:1 + extra] = raw[:, influence:influence + extra].astype(np.float32) / 255.0
    if influence in _PACKED and NORMAL_TANGENT in attrs:
        s1, s2 = _PACKED[influence]
        nt = attrs[NORMAL_TANGENT]
        wn, wt = nt[:, 3].astype(np.int32), nt[:, 7].astype(np.int32)
        packed = np.stack([(wt & 0x7F) / 254.0, ((wn & 0xF0) >> 4) / s1, (wn & 0x0F) / s2], axis=1)
        weights[:, 1 + extra:4 + extra] = packed[:, :max(0, min(3, influence - 1 - extra))]
    weights[:, 0] = 1.0 - weights[:, 1:].sum(axis=1)
    if influence < 4:
        joints = np.pad(joints, ((0, 0), (0, 4 - influence)))
        weights = np.pad(weights, ((0, 0), (0, 4 - influence)))
    elif influence > 4:  # keep the four heaviest, as the rest of Dark STL poses with four
        top = np.argsort(-weights, axis=1, kind="stable")[:, :4]
        joints = np.take_along_axis(joints, top, axis=1)
        weights = np.take_along_axis(weights, top, axis=1)
        total = weights.sum(axis=1, keepdims=True)
        weights = weights / np.where(total > 0, total, 1)
    return joints, weights.astype(np.float32)


def _decode(attrs: dict[int, np.ndarray], lod: LodInfo, skinned: bool, darkages: bool = False) -> Surface:
    n = lod.vertices
    off = np.asarray(lod.offset, np.float32)
    if POSITION_SHORT in attrs:
        q = attrs[POSITION_SHORT].view("<u2").reshape(n, 4)[:, :3].astype(np.float32) / 65535.0
        pos = q * np.float32(lod.scale) + off
    elif POSITION in attrs:
        pos = attrs[POSITION].view("<f4").reshape(n, 3) * np.float32(lod.scale) + off
    else:
        raise FormatError("the model has no positions")
    normals = weights = joints = uvs = None
    if NORMAL_TANGENT in attrs:
        nt = attrs[NORMAL_TANGENT]
        normals = _unorm8(nt[:, 0:3])
        length = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = normals / np.where(length > 0, length, 1)
        if skinned:
            wn, wt = nt[:, 3].astype(np.int32), nt[:, 7].astype(np.int32)
            y = (wt & 0x7F) / 254.0
            z = ((wn & 0xF0) >> 4) / 45.0
            w = (wn & 0x0F) / 60.0
            weights = np.stack([1.0 - y - z - w, y, z, w], axis=1).astype(np.float32)
    for m in (MATERIAL_UV_SHORT, MATERIAL_UV):
        if m in attrs:
            if m == MATERIAL_UV_SHORT:
                uv = attrs[m].view("<u2").reshape(n, 2).astype(np.float32) / 65535.0
            else:
                uv = attrs[m].view("<f4").reshape(n, 2)
            uvs = uv * np.float32(lod.uv_scale) + np.asarray(lod.uv_offset, np.float32)
            break
    if skinned and darkages:
        got = _darkages_skin(attrs, lod)
        if got is not None:
            joints, weights = got
        elif SKINNING_1 in attrs and NORMAL_TANGENT in attrs:
            joints = np.zeros((n, 4), np.int32)
            joints[:, 0] = attrs[NORMAL_TANGENT][:, 3]
            weights = np.zeros((n, 4), np.float32)
            weights[:, 0] = 1.0
        elif COLOR in attrs:  # four joints in the colour, weights in the normal and tangent, as in DOOM Eternal
            joints = attrs[COLOR].astype(np.int32)
    elif skinned:
        if COLOR in attrs:
            joints = attrs[COLOR].astype(np.int32)
        if SKINNING_1 in attrs and joints is None and NORMAL_TANGENT in attrs:
            joints = np.zeros((n, 4), np.int32)
            joints[:, 0] = attrs[NORMAL_TANGENT][:, 3]
            weights = np.zeros((n, 4), np.float32)
            weights[:, 0] = 1.0
    return Surface(pos.astype(np.float32), normals, uvs, np.zeros((0, 3), np.int64), joints, weights)


def _indices(buf: bytes, offset: int, lod: LodInfo) -> np.ndarray:
    count = lod.triangles * 3
    if offset + count * 2 > len(buf):
        raise FormatError("the model's index data is cut short")
    idx = np.frombuffer(buf, "<u2", count, offset).astype(np.int64).reshape(-1, 3)
    if len(idx) and idx.max() >= lod.vertices:
        raise FormatError("the model's indices point past its vertices")
    return idx


def read_interleaved(buf: bytes, offset: int, lod: LodInfo) -> tuple[Surface, int]:
    """A level of detail kept inside the container: attributes interleaved, then the indices. Returns where it ends."""
    masks = [m for m in INTERLEAVED if lod.mask & m == m]
    stride = sum(SIZES[m] for m in masks)
    attrs, at = {}, offset
    for m in masks:
        attrs[m] = _records(buf, at, stride, lod.vertices, SIZES[m])
        at += SIZES[m]
    surf = _decode(attrs, lod, False)
    start = offset + stride * lod.vertices
    surf.triangles = _indices(buf, start, lod)
    return surf, start + lod.triangles * 6


def read_streamed(buf: bytes, lods: list[LodInfo], layouts: list[MemoryLayout], skinned: bool) -> list[Surface | None]:
    """The surfaces of one level of detail from its StreamDB piece: per memory layout (one per combination of
    attributes) the surfaces using it follow one another in each attribute's stream and in the index stream."""
    cursors = {lay.mask: ([o for o in lay.offsets], lay.index_offset) for lay in layouts}
    by_mask = {lay.mask: lay for lay in layouts}
    out: list[Surface | None] = []
    for lod in lods:
        lay = by_mask.get(lod.mask)
        if lay is None:
            out.append(None)
            continue
        offsets, index_at = cursors[lod.mask]
        attrs = {}
        for v, m in enumerate(lay.masks):
            size = SIZES.get(m)
            if size is None:
                raise FormatError(f"vertex attribute 0x{m:x} isn't known")
            if size:
                attrs[m] = _records(buf, offsets[v], size, lod.vertices, size)
            else:
                attrs[m] = np.zeros((lod.vertices, 0), np.uint8)
            offsets[v] += lod.vertices * size
        surf = _decode(attrs, lod, skinned)
        surf.triangles = _indices(buf, index_at, lod)
        cursors[lod.mask] = (offsets, index_at + lod.triangles * 6)
        out.append(surf)
    return out


def read_sequential(buf: bytes, lods: list[LodInfo], skinned: bool) -> list[Surface]:
    """The surfaces of one level of detail from a Dark Ages StreamDB piece: per surface each attribute's stream
    (in mask order, each starting on a multiple of its record size), then its indices; surfaces start on 8 bytes."""
    out: list[Surface] = []
    offset = 0
    for lod in lods:
        at = 0
        attrs = {}
        for m in split_mask(lod.mask):
            size = SIZES[m]
            start = at + ((size - at % size) % size if size else 0)
            attrs[m] = (_records(buf, offset + start, size, lod.vertices, size) if size
                        else np.zeros((lod.vertices, 0), np.uint8))
            at = start + lod.vertices * size
        surf = _decode(attrs, lod, skinned, darkages=True)
        surf.triangles = _indices(buf, offset + at, lod)
        at += lod.triangles * 6
        offset = (offset + at + 7) & ~7
        out.append(surf)
    return out
