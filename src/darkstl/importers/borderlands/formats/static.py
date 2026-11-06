"""Unreal Engine 3 ``StaticMesh``: props, map pieces and other rigid models.

After the properties come bounds, the collision tree (skipped) and the levels
of detail; the first one is read: its sections (material, index range), the
position stream, the UV stream (packed tangent frame plus half or full float
UVs) and the index buffer. Borderlands 1 and 2 drop a few of the generic
fields, as UE Viewer documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .props import ObjectRef, Props, object_props
from .skeletal import is_bl1, skip_bulk, unpack_normals
from .upk import Export, Package, Reader


@dataclass
class StaticSection:
    material: ObjectRef
    first_index: int
    triangles: int


@dataclass
class StaticMesh:
    name: str
    path: str
    props: Props
    sections: list[StaticSection] = field(default_factory=list)
    positions: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.float32))
    normals: np.ndarray | None = None
    uvs: list[np.ndarray] = field(default_factory=list)
    indices: np.ndarray = field(default_factory=lambda: np.zeros(0, np.uint32))
    lod_count: int = 0
    notes: list[str] = field(default_factory=list)


def read_static_mesh(pkg: Package, export: Export) -> StaticMesh:
    r = pkg.export_data(export)
    v = pkg.summary.version
    props = object_props(pkg, r)
    r.skip(28 + 4)  # bounds, body setup
    if v < 315:
        r.skip(4)
    if v >= 770:
        r.skip(24)
    r.bulk_array()  # kDOP nodes
    r.bulk_array()  # kDOP triangles
    internal = r.i32()
    if internal >= 17 and v < 593:
        r.skip(8 * r.count())
    if v >= 823:
        if r.i32():
            _read_lod(pkg, r, StaticMesh("", "", props))
        if v < 829:
            r.skip(4 * r.count())
        else:
            r.skip(7 * r.count())
        r.i32()
    if v >= 859:
        r.i32()
    mesh = StaticMesh(export.name, export.path, props)
    mesh.lod_count = r.count(64)
    if mesh.lod_count:
        _read_lod(pkg, r, mesh)
    return mesh


def _read_lod(pkg: Package, r: Reader, mesh: StaticMesh) -> None:
    v = pkg.summary.version
    if v >= 218:
        skip_bulk(r)
    for _ in range(r.count(1 << 16)):
        mat = ObjectRef(pkg, r.i32())
        r.skip(8)
        if v >= 473:
            r.skip(4)
        first, faces = r.i32(), r.i32()
        r.skip(8)
        if v >= 492:
            r.skip(4)
        if v >= 514:
            r.skip(8 * r.count())
        if v >= 618 and r.u8():
            for elem in (4, 4, 2, 2, 2, 2, 2, 2):
                r.skip(elem * r.count())
        mesh.sections.append(StaticSection(mat, first, faces))
    if v < 466:
        raise FormatError(f"{mesh.name}: static meshes this old (version {v}) aren't supported")
    # positions
    r.i32()
    r.i32()
    elem, n, raw = r.bulk_array()
    if elem != 12:
        raise FormatError(f"{mesh.name}: unexpected position size {elem}")
    mesh.positions = np.frombuffer(raw, "<f4").reshape(n, 3).astype(np.float32)
    # UVs and tangent frame
    tex_coords = r.i32()
    r.i32()
    r.i32()
    full = r.i32() if v >= 474 else 1
    elem, count, raw = r.bulk_array()
    _decode_uv_items(mesh, v, elem, count, raw, tex_coords, bool(full))
    bl1 = is_bl1(pkg)
    if not bl1:
        if v >= 615:
            r.i32()
            if r.i32():
                r.bulk_array()
        if v < 686:
            r.i32()
            r.i32()
            r.bulk_array()
    r.i32()  # vertex count
    elem, n, raw = r.bulk_array()
    if v < 297:
        r.i32()
    mesh.indices = np.frombuffer(raw, np.uint16 if elem == 2 else np.uint32).astype(np.uint32)
    if bl1:
        return
    if pkg.summary.licensee < 46 or v < 832:  # Borderlands 2 leaves out the wireframe indices
        r.bulk_array()
        if v < 297:
            r.i32()
    if v < 686:
        r.bulk_array()
        r.skip(r.count())
    if v >= 841:
        r.bulk_array()


def _decode_uv_items(mesh: StaticMesh, v: int, elem: int, count: int, raw: bytes, tex_coords: int,
                     full: bool) -> None:
    head = 0
    if v < 472:
        head += 16
    normals = 12 if v < 477 else 8
    color = 4 if 434 <= v < 615 else 0
    for uv_size in ((8, 4) if full else (4, 8)):
        if head + normals + color + uv_size * tex_coords == elem:
            break
    else:
        raise FormatError(f"{mesh.name}: unexpected UV item size {elem}")
    a = np.frombuffer(raw, np.uint8).reshape(count, elem)
    o = head
    nrm = a[:, o:o + normals].copy().view("<u4")
    mesh.normals = unpack_normals(nrm[:, -1].copy())
    o += normals + color
    for _ in range(tex_coords):
        dt = "<f4" if uv_size == 8 else "<f2"
        mesh.uvs.append(a[:, o:o + uv_size].copy().view(dt).reshape(count, 2).astype(np.float32))
        o += uv_size


__all__ = ["StaticMesh", "StaticSection", "read_static_mesh"]
