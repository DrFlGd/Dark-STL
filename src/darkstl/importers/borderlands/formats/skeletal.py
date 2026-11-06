"""Unreal Engine 3 ``SkeletalMesh``: characters, weapons (Borderlands 2's gestalt meshes), anything skinned.

After the tagged properties come the bounds, the material list, the reference
skeleton and the levels of detail. Only the first (full-detail) level is
read: its sections (material ranges of the index buffer), its chunks (which
bones the vertices of each range use) and its GPU vertex buffer (positions,
packed normals, half or full float UVs, four bone influences per vertex).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .props import ObjectRef, Props, object_props
from .upk import Export, Package, Reader


@dataclass
class Bone:
    name: str
    parent: int
    rotation: tuple[float, float, float, float]  # x, y, z, w
    position: tuple[float, float, float]


@dataclass
class Section:
    material: int
    chunk: int
    first_index: int
    triangles: int


@dataclass
class Chunk:
    first_vertex: int
    vertices: int
    bones: list[int]


@dataclass
class SkeletalMesh:
    name: str
    path: str
    props: Props
    materials: list[ObjectRef]
    bones: list[Bone]
    sections: list[Section] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    indices: np.ndarray = field(default_factory=lambda: np.zeros(0, np.uint32))
    positions: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.float32))  # Unreal units, Unreal axes
    normals: np.ndarray | None = None
    uvs: list[np.ndarray] = field(default_factory=list)
    bone_index: np.ndarray | None = None  # (N, 4) skeleton bone per influence
    bone_weight: np.ndarray | None = None  # (N, 4) 0..1
    lod_count: int = 0
    notes: list[str] = field(default_factory=list)

    def triangles(self) -> np.ndarray:
        return self.indices.reshape(-1, 3)


def unpack_normals(packed: np.ndarray) -> np.ndarray:
    """UE3 FPackedNormal (bytes x, y, z, w; ``b / 127.5 - 1``)."""
    b = packed.view(np.uint8).reshape(-1, 4)[:, :3].astype(np.float32)
    return b / 127.5 - 1.0


def _skip_array(r: Reader, elem: int) -> int:
    n = r.count()
    r.skip(n * elem)
    return n


def skip_bulk(r: Reader) -> tuple[int, int, int, int]:
    """A UE3 bulk data header, skipping the data when it is stored inline."""
    flags, count, size, offset = r.u32(), r.i32(), r.i32(), r.i32()
    if not flags & 0x01 and not flags & 0x20 and size > 0:
        r.skip(size)
    return flags, count, size, offset


def is_bl1(pkg: Package) -> bool:
    """Borderlands (2009) and its Enhanced edition: licensee 57+ on an older engine than Borderlands 2."""
    s = pkg.summary
    return s.licensee >= 57 and s.version < 700


def read_skeletal_mesh(pkg: Package, export: Export) -> SkeletalMesh:
    r = pkg.export_data(export)
    v = pkg.summary.version
    props = object_props(pkg, r)
    r.skip(28)  # bounds: origin, extent, radius
    materials = [ObjectRef(pkg, r.i32()) for _ in range(r.count())]
    r.skip(24)  # mesh origin, rotation origin
    bones = []
    for _ in range(r.count(1 << 16)):
        name = r.name()
        r.u32()
        rot = r.floats(4)
        pos = r.floats(3)
        if v < 224:
            r.skip(16)
        r.i32()  # children
        parent = r.i32()
        if v >= 515:
            r.skip(4)
        bones.append(Bone(name, parent, rot, pos))
    r.i32()  # skeletal depth
    lods = r.count(64)
    mesh = SkeletalMesh(export.name, export.path, props, materials, bones, lod_count=lods)
    if lods:
        _read_lod(pkg, r, mesh, bool(props.get("bHasVertexColors", False)))
    return mesh


def _read_lod(pkg: Package, r: Reader, mesh: SkeletalMesh, has_colors: bool) -> None:
    v = pkg.summary.version
    for _ in range(r.count(1 << 16)):
        mat, chunk = r.i16(), r.i16()
        first = r.i32()
        tris = r.u16() if v < 806 else r.i32()
        if v >= 599:
            r.skip(1)
        mesh.sections.append(Section(mat, chunk, first, tris))
    item = 2
    if v >= 806:
        r.i32()
        item = r.u8()
    size, n, raw = r.bulk_array()
    if size not in (2, 4):
        raise FormatError(f"{mesh.name}: index size {size}")
    mesh.indices = np.frombuffer(raw, np.uint16 if size == 2 else np.uint32).astype(np.uint32)
    del item
    uv_sets = 4 if v >= 709 else 1
    rigid_size = 12 + 12 + 8 * uv_sets + (4 if v >= 710 else 0) + 1
    soft_size = 12 + 12 + 8 * uv_sets + (4 if v >= 710 else 0) + 8

    def chunks() -> None:
        for _ in range(r.count(1 << 16)):
            first = r.i32()
            nr = _skip_array(r, rigid_size)
            ns = _skip_array(r, soft_size)
            bones = [r.i16() for _ in range(r.count(1 << 16))]
            if v >= 333:
                nr, ns = r.i32(), r.i32()
            if v >= 362:
                r.i32()
            mesh.chunks.append(Chunk(first, nr + ns, bones))

    if is_bl1(pkg):
        _skip_array(r, 2)  # used bones
        chunks()
        r.i32()
        num_vertices = r.i32()
    else:
        if v < 686:
            _skip_array(r, 2)
        _skip_array(r, 2)  # used bones
        if v < 686:
            _skip_array(r, 1)
        chunks()
        r.i32()
        num_vertices = r.i32()
        if v < 686:
            _skip_array(r, 16)
    if v >= 207:
        _skip_array(r, 1)
    else:
        _skip_array(r, 2)
    if v >= 221:
        skip_bulk(r)
    if v >= 709:
        r.i32()
    if v < 493:
        raise FormatError(f"{mesh.name}: skeletal meshes this old (version {v}) aren't supported")
    uv_count = r.i32() if v >= 709 else 1
    full_uvs = r.i32()
    if v >= 592:
        r.i32()  # packed positions: only consoles use them
        r.skip(24)
    elem, count, raw = r.bulk_array()
    _decode_gpu_vertices(mesh, v, elem, count, raw, uv_count, bool(full_uvs))
    if count != num_vertices:
        mesh.notes.append(f"{mesh.name}: vertex buffer holds {count} vertices, the model says {num_vertices}")


def _decode_gpu_vertices(mesh: SkeletalMesh, v: int, elem: int, count: int, raw: bytes, uv_count: int,
                         full_uvs: bool) -> None:
    normals = 12 if v < 494 else 8
    uv_size = 8 if full_uvs else 4
    expected = normals + 8 + 12 + uv_size * uv_count
    if elem != expected:
        # the flags may be off; try the other UV precision, then a packed (console) position
        for full, pos in ((not full_uvs, 12), (full_uvs, 4), (not full_uvs, 4)):
            us = 8 if full else 4
            if normals + 8 + pos + us * uv_count == elem:
                full_uvs, uv_size = full, us
                if pos == 4:
                    raise FormatError(f"{mesh.name} stores packed (console) vertex positions")
                break
        else:
            raise FormatError(f"{mesh.name}: unexpected vertex size {elem} (expected {expected})")
    a = np.frombuffer(raw, np.uint8).reshape(count, elem)
    o = 0
    if v < 592:
        pos = a[:, 0:12].copy().view("<f4").reshape(count, 3)
        o = 12
    nrm = a[:, o:o + normals].copy().view("<u4")
    tangent_z = nrm[:, -1]
    o += normals
    bidx = a[:, o:o + 4].astype(np.int32)
    bwt = a[:, o + 4:o + 8].astype(np.float32) / 255.0
    o += 8
    if v >= 592:
        pos = a[:, o:o + 12].copy().view("<f4").reshape(count, 3)
        o += 12
    uvs = []
    for _ in range(uv_count):
        if uv_size == 8:
            uvs.append(a[:, o:o + 8].copy().view("<f4").reshape(count, 2).astype(np.float32))
        else:
            uvs.append(a[:, o:o + 4].copy().view("<f2").reshape(count, 2).astype(np.float32))
        o += uv_size
    mesh.positions = pos.astype(np.float32)
    mesh.normals = unpack_normals(tangent_z.astype("<u4"))
    mesh.uvs = uvs
    # chunk-local bone indices -> skeleton bones
    gidx = np.zeros_like(bidx)
    for c in mesh.chunks:
        lo, hi = c.first_vertex, min(count, c.first_vertex + c.vertices)
        if hi <= lo or not c.bones:
            continue
        table = np.asarray(c.bones, np.int32)
        local = np.clip(bidx[lo:hi], 0, len(table) - 1)
        gidx[lo:hi] = table[local]
    mesh.bone_index = gidx
    mesh.bone_weight = bwt


__all__ = ["Bone", "Chunk", "Section", "SkeletalMesh", "is_bl1", "read_skeletal_mesh", "skip_bulk",
           "unpack_normals"]
