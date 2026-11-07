"""Unreal Engine 4 (and 5) ``SkeletalMesh`` and ``StaticMesh``: the first level of detail of the cooked render data.

After the tagged properties both classes write their own binary data: the
materials and skeleton (skeletal meshes), then the render data, one entry per
level of detail. Only the first LOD that has geometry is decoded: its sections
(material and index range), the index buffer, the positions, and the
tangent/UV stream (from Unreal 4.19 tangents and texture coordinates are two
separate streams). Since 4.23 (static) and 4.24 (skeletal) a LOD's buffers may
sit in a bulk block, usually in the package's ``.ubulk``.

Borderlands 3 (Unreal 4.20 with Gearbox's changes) writes several colour
streams where other games write one. Layouts follow CUE4Parse (Apache 2.0),
which tracks these differences, and UE Viewer (MIT).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ..formats.props import ObjectRef, Props
from ..formats.upk import Reader
from . import asset
from .asset import Export, Package
from .props import Array4, read_props

RF_CLASS_DEFAULT_OBJECT = 0x10


def object_props(pkg: Package, r: Reader, e: Export) -> Props:
    """An object's properties and the object GUID UE4 writes after them."""
    reader = getattr(pkg, "unversioned_props", None)
    props = reader(r, e) if reader is not None else read_props(r)
    if not e.flags & RF_CLASS_DEFAULT_OBJECT and r.remaining() >= 4:
        if r.i32():
            r.skip(16)
    return props


@dataclass
class Section4:
    material: int
    first_index: int
    triangles: int


@dataclass
class Mesh4:
    name: str
    kind: str  # "SkeletalMesh" or "StaticMesh"
    positions: np.ndarray
    normals: np.ndarray | None
    uvs: list[np.ndarray]
    indices: np.ndarray
    sections: list[Section4]
    materials: list[ObjectRef]
    slot_names: list[str] = field(default_factory=list)
    bones: list[tuple[str, int]] = field(default_factory=list)
    lod_count: int = 0
    lod: int = 0
    sockets: list[ObjectRef] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def triangles(self) -> np.ndarray:
        return self.indices.reshape(-1, 3)


# -- shared buffers -------------------------------------------------------------------

def _strip(r: Reader) -> tuple[int, int]:
    return r.u8(), r.u8()


def _index_container(r: Reader) -> np.ndarray:
    """``FMultisizeIndexContainer`` (skeletal meshes): element size, then a bulk array."""
    size = r.u8()
    esize, n, raw = r.bulk_array()
    dtype = np.uint16 if (esize or size) == 2 else np.uint32
    return np.frombuffer(raw, dtype).astype(np.int64)


def _raw_static_index_buffer(r: Reader, v: asset.Versions) -> np.ndarray:
    """``FRawStaticIndexBuffer``: 32-bit flag, the indices as bytes, (4.25+) whether to widen them."""
    is32 = r.i32()
    _esize, n, raw = r.bulk_array()
    if v.engine >= 425:
        r.i32()
    if not raw:
        return np.zeros(0, np.int64)
    return np.frombuffer(raw, np.uint32 if is32 else np.uint16).astype(np.int64)


def _positions(r: Reader) -> np.ndarray:
    r.i32()  # stride
    count = r.i32()
    esize, n, raw = r.bulk_array()
    if n != count and count:
        raise FormatError(f"position buffer holds {n} of {count} vertices")
    if esize == 12:
        return np.frombuffer(raw, "<f4").reshape(-1, 3).astype(np.float64)
    if esize == 24:
        return np.frombuffer(raw, "<f8").reshape(-1, 3)
    raise FormatError(f"positions of {esize} bytes aren't supported")


def _unpack_normals8(raw: np.ndarray, increased: bool) -> np.ndarray:
    b = raw.astype(np.int32)
    if increased:
        b ^= 0x80
    return (b[:, :3].astype(np.float32) / 127.5) - 1.0


def _vertex_buffer(r: Reader, v: asset.Versions) -> tuple[np.ndarray | None, list[np.ndarray], int]:
    """``FStaticMeshVertexBuffer``: (normals, UV sets, vertex count)."""
    g, _c = _strip(r)
    sets = r.i32()
    if v.engine < 419:
        r.i32()  # stride
    count = r.i32()
    full_uv = bool(r.i32())
    high_tangents = bool(r.i32()) if v.engine >= 412 else False
    if g & 2 or count == 0:
        return None, [], count
    increased = v.cv("Rendering") >= asset.RENDERING_INCREASE_NORMAL_PRECISION
    tbytes = 16 if high_tangents else 8
    ubytes = (8 if full_uv else 4) * sets
    if v.engine < 419:
        esize, n, raw = r.bulk_array()
        if esize != tbytes + ubytes:
            raise FormatError(f"vertex stream of {esize} bytes per vertex, expected {tbytes + ubytes}")
        a = np.frombuffer(raw, np.uint8).reshape(n, esize)
        tangents, texcoords = a[:, :tbytes], a[:, tbytes:]
    else:
        esize, n, raw = r.bulk_array()
        if n != count or esize * n != len(raw):
            raise FormatError("tangent stream doesn't match the vertex count")
        tangents = np.frombuffer(raw, np.uint8).reshape(n, -1)
        esize, n2, raw2 = r.bulk_array()
        texcoords = np.frombuffer(raw2, np.uint8).reshape(count, -1) if count else np.zeros((0, 0), np.uint8)
    if high_tangents:
        tz = tangents[:, 8:16].copy().view("<u2").astype(np.int32)
        if v.engine >= 420:
            tz ^= 0x8000
        normals = (tz[:, :3].astype(np.float32) - 32767.5) / 32767.5
    else:
        normals = _unpack_normals8(tangents[:, 4:8], increased)
    uvs = []
    per = 8 if full_uv else 4
    if texcoords.shape[1] >= per * sets:
        for i in range(sets):
            chunk = np.ascontiguousarray(texcoords[:, per * i:per * (i + 1)])
            uv = chunk.view("<f4" if full_uv else "<f2").reshape(-1, 2).astype(np.float32)
            uvs.append(uv)
    return normals, uvs, count


def _color_buffer(r: Reader) -> None:
    g, _c = _strip(r)
    r.i32()  # stride
    n = r.i32()
    if not g & 2 and n > 0:
        r.bulk_array()


def _bounds(r: Reader, v: asset.Versions) -> None:
    r.skip(56 if v.engine >= 500 else 28)


# -- skeletal meshes ------------------------------------------------------------------

def _skeletal_material(r: Reader, pkg: Package) -> tuple[ObjectRef, str]:
    v = pkg.versions
    ref = ObjectRef(pkg, r.i32())
    slot = ""
    if v.cv("Editor") >= asset.EDITOR_REFACTOR_MESH_EDITOR_MATERIALS:
        slot = r.name()
        imported = not v.filter_editor_only
        if v.cv("Core") >= asset.CORE_SKELETAL_MATERIAL_EDITOR_DATA_STRIPPING:
            imported = bool(r.i32())
        if imported:
            r.name()
    else:
        if v.ue4 >= 302:
            r.i32()
        if v.cv("RecomputeTangent") >= asset.RECOMPUTE_TANGENT_RUNTIME:
            r.i32()
    if v.cv("Rendering") >= asset.RENDERING_TEXTURE_STREAMING_UV:
        r.skip(24)  # UV channel data
    if v.cv("FortniteMain") >= 196:  # MeshMaterialSlotOverlayMaterialAdded
        r.i32()
    return ref, slot


def _skel_section(r: Reader, v: asset.Versions) -> Section4:
    g, c = _strip(r)
    mat = r.i16()
    first = r.i32()
    tris = r.i32()
    r.i32()  # recompute tangent
    if v.cv("RecomputeTangent") >= asset.RECOMPUTE_TANGENT_VERTEX_COLOR_MASK:
        r.u8()
    if v.cv("Editor") >= asset.EDITOR_REFACTOR_MESH_EDITOR_MATERIALS:
        r.i32()  # cast shadow
    if v.cv("UE5Main") >= 54:  # SkelMeshSectionVisibleInRayTracingFlagAdded
        r.i32()
    r.u32()  # base vertex index
    if v.cv("UE5Release") >= 15:  # AddClothMappingLODBias: an array per LOD
        for _ in range(r.count(64)):
            r.skip(64 * r.count())
    else:
        r.skip(64 * r.count())  # cloth mapping data
    r.skip(2 * r.count())  # bone map
    r.i32()  # vertices
    r.i32()  # max bone influences
    r.i16()  # cloth asset index
    r.skip(20)  # clothing data
    if v.engine < 423 or not c & 1:
        r.skip(4 * r.count())  # duplicated vertices
        r.skip(8 * r.count())
    if v.cv("Release") >= asset.RELEASE_ADD_SKELETAL_MESH_SECTION_DISABLE:
        r.i32()
    return Section4(mat, first, tris)


def _skel_streamed(r: Reader, v: asset.Versions):
    _strip(r)
    indices = _index_container(r)
    positions = _positions(r)
    normals, uvs, _n = _vertex_buffer(r, v)
    return indices, positions, normals, uvs


def _skel_lod(r: Reader, pkg: Package, new_format: bool):
    """One LOD's (sections, indices, positions, normals, uvs), or None when it was cooked out."""
    v = pkg.versions
    g, c = _strip(r)
    if not new_format:
        sections = [_skel_section(r, v) for _ in range(r.count(4096))]
        indices = _index_container(r)
        r.skip(2 * r.count())  # active bones
        r.skip(2 * r.count())  # required bones
        if g & 2 or c & 2:
            return None
        positions = _positions(r)
        normals, uvs, _n = _vertex_buffer(r, v)
        return sections, indices, positions, normals, uvs
    cooked_out = bool(r.i32())
    inlined = bool(r.i32())
    r.skip(2 * r.count())  # required bones
    if g & 2 or cooked_out:
        return None
    sections = [_skel_section(r, v) for _ in range(r.count(4096))]
    r.skip(2 * r.count())  # active bones
    r.u32()  # buffers size
    if inlined:
        indices, positions, normals, uvs = _skel_streamed(r, v)
    else:
        data = asset.read_bulk(r)
        if not data:
            return None
        indices, positions, normals, uvs = _skel_streamed(Reader(data, pkg), v)
    return sections, indices, positions, normals, uvs


def read_skeletal_mesh(pkg: Package, e: Export) -> Mesh4:
    v = pkg.versions
    r = pkg.export_data(e)
    props = object_props(pkg, r, e)
    g, _c = _strip(r)
    _bounds(r, v)
    mats, slots = [], []
    for _ in range(r.count(4096)):
        ref, slot = _skeletal_material(r, pkg)
        mats.append(ref)
        slots.append(slot)
    bones = []
    for _ in range(r.count(1 << 16)):
        bones.append((r.name(), r.i32()))
    r.skip((80 if v.engine >= 500 else 40) * r.count(1 << 16))  # reference pose
    for _ in range(r.count(1 << 16)):  # name -> index
        r.name()
        r.i32()
    if v.cv("SkeletalMesh") < asset.SKELETAL_SPLIT_MODEL_AND_RENDER_DATA:
        raise FormatError(f"{e.name}: skeletal meshes from before Unreal 4.19 aren't supported")
    if not g & 1:
        raise FormatError(f"{e.name} holds editor data (an uncooked mesh); only cooked games are supported")
    cooked = bool(r.i32())
    if not cooked:
        raise FormatError(f"{e.name} has no cooked render data")
    sockets = []
    arr = props.get("Sockets")
    if isinstance(arr, Array4):
        sockets = arr.objects()
    n = r.count(64)
    new_format = v.engine >= 424
    notes = []
    for lod in range(n):
        got = _skel_lod(r, pkg, new_format)
        if got is None:
            continue
        sections, indices, positions, normals, uvs = got
        if len(positions) == 0:
            continue
        if lod:
            notes.append(f"Level of detail {lod} is the most detailed one this mesh ships with.")
        return Mesh4(e.name, "SkeletalMesh", positions, normals, uvs, indices, sections, mats, slots, bones, n, lod,
                     sockets, notes)
    return Mesh4(e.name, "SkeletalMesh", np.zeros((0, 3)), None, [], np.zeros(0, np.int64), [], mats, slots, bones,
                 n, 0, sockets, ["No level of detail has geometry."])


# -- static meshes --------------------------------------------------------------------

def _static_section(r: Reader, v: asset.Versions) -> Section4:
    mat = r.i32()
    first = r.i32()
    tris = r.i32()
    r.i32(), r.i32()  # min, max vertex index
    r.i32(), r.i32()  # collision, cast shadow
    if v.cv("Rendering") >= 37:  # StaticMeshSectionForceOpaqueField
        r.i32()
    if v.engine >= 426:  # visible in ray tracing
        r.i32()
    if v.engine >= 501:  # affect distance field lighting
        r.i32()
    return Section4(mat, first, tris)


def _static_buffers(r: Reader, v: asset.Versions):
    _strip(r)
    positions = _positions(r)
    normals, uvs, _n = _vertex_buffer(r, v)
    _color_buffer(r)
    indices = _raw_static_index_buffer(r, v)
    return indices, positions, normals, uvs


def _static_lod(r: Reader, pkg: Package):
    v = pkg.versions
    g, c = _strip(r)
    sections = [_static_section(r, v) for _ in range(r.count(4096))]
    if v.engine >= 506:
        r.skip(56)  # source mesh bounds
    r.f32()  # max deviation
    if v.engine < 423:
        if g & 2 or c & 2:
            return None
        positions = _positions(r)
        normals, uvs, _n = _vertex_buffer(r, v)
        if v.bl3:
            streams = r.i32()
            for _ in range(streams):
                _color_buffer(r)
        else:
            _color_buffer(r)
        indices = _raw_static_index_buffer(r, v)
        return sections, indices, positions, normals, uvs
    cooked_out = bool(r.i32())
    inlined = bool(r.i32())
    if g & 2 or cooked_out:
        return None
    if v.engine >= 505:
        r.i32()  # has ray tracing geometry
    if inlined:
        indices, positions, normals, uvs = _static_buffers(r, v)
    else:
        data = asset.read_bulk(r)
        # the buffers' sizes, which a reader of this LOD skips
        r.skip(8 + 16 + 8 + 8 + 40 + (8 if v.cv("UE5Release") < 3 else 0) + (24 if v.engine >= 506 else 0))
        r.skip(12)
        if not data:
            return None
        indices, positions, normals, uvs = _static_buffers(Reader(data, pkg), v)
    return sections, indices, positions, normals, uvs


def _static_materials(props: Props, pkg: Package) -> tuple[list[ObjectRef], list[str]]:
    arr = props.get("StaticMaterials")
    mats, slots = [], []
    if isinstance(arr, Array4):
        try:
            for p in arr.structs():
                ref = p.get("MaterialInterface")
                mats.append(ref if isinstance(ref, ObjectRef) else ObjectRef(pkg, 0))
                slots.append(str(p.get("MaterialSlotName", "")))
        except FormatError:
            pass
    if not mats and getattr(props, "material_refs", None):  # read without type mappings (see guess.py)
        mats = list(props.material_refs)
        slots = [m.name for m in mats]
    if not mats:
        arr = props.get("Materials")
        if isinstance(arr, Array4):
            mats = arr.objects()
            slots = [m.name for m in mats]
    return mats, slots


def read_static_mesh(pkg: Package, e: Export) -> Mesh4:
    v = pkg.versions
    r = pkg.export_data(e)
    props = object_props(pkg, r, e)
    mats, slots = _static_materials(props, pkg)
    g, _c = _strip(r)
    cooked = bool(r.i32())
    r.i32()  # body setup
    if v.ue4 >= 216:
        r.i32()  # nav collision
    if not g & 1:
        raise FormatError(f"{e.name} holds editor data (an uncooked mesh); only cooked games are supported")
    r.skip(16)  # lighting guid
    sockets = [ObjectRef(pkg, r.i32()) for _ in range(r.count(4096))]
    if not cooked:
        raise FormatError(f"{e.name} has no cooked render data")
    n = r.count(64)
    notes = []
    for lod in range(n):
        got = _static_lod(r, pkg)
        if got is None:
            continue
        sections, indices, positions, normals, uvs = got
        if len(positions) == 0:
            continue
        if lod:
            notes.append(f"Level of detail {lod} is the most detailed one this mesh ships with.")
        return Mesh4(e.name, "StaticMesh", positions, normals, uvs, indices, sections, mats, slots, [], n, lod,
                     sockets, notes)
    if v.engine >= 500:
        notes.append("This mesh's full detail is kept as Nanite data, which Dark STL can't read yet.")
    return Mesh4(e.name, "StaticMesh", np.zeros((0, 3)), None, [], np.zeros(0, np.int64), [], mats, slots, [], n, 0,
                 sockets, notes or ["No level of detail has geometry."])


__all__ = ["Mesh4", "Section4", "object_props", "read_skeletal_mesh", "read_static_mesh"]
