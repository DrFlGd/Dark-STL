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
    base_vertex: int = 0  # skeletal meshes: the section's vertices, and the bones its weights index
    vertices: int = 0
    bone_map: np.ndarray | None = None


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
    # skeletal meshes: each bone's reference pose (B, 10: rotation x y z w, translation, scale), the
    # Skeleton asset the mesh is animated with, and per vertex its bones (indices into ``bones``) and weights
    ref_pose: np.ndarray | None = None
    skeleton: ObjectRef | None = None
    joints: np.ndarray | None = None
    weights: np.ndarray | None = None

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
    base = r.u32()  # base vertex index
    if v.cv("UE5Release") >= 15:  # AddClothMappingLODBias: an array per LOD
        for _ in range(r.count(64)):
            r.skip(64 * r.count())
    else:
        r.skip(64 * r.count())  # cloth mapping data
    n = r.count(1 << 16)
    bone_map = np.frombuffer(r.bytes(2 * n), "<u2").astype(np.int64)
    count = r.i32()  # vertices
    r.i32()  # max bone influences
    r.i16()  # cloth asset index
    r.skip(20)  # clothing data
    if v.engine < 423 or not c & 1:
        r.skip(4 * r.count())  # duplicated vertices
        r.skip(8 * r.count())
    if v.cv("Release") >= asset.RELEASE_ADD_SKELETAL_MESH_SECTION_DISABLE:
        r.i32()
    return Section4(mat, first, tris, int(base), int(count), bone_map)


def _skin_weights(r: Reader, v: asset.Versions) -> tuple[np.ndarray, np.ndarray] | None:
    """``FSkinWeightVertexBuffer``: per vertex its bones (indices into its section's bone map) and weights (0-1),
    (V, k) each; None when the buffer was stripped. Layouts after CUE4Parse's reader."""
    unlimited = v.cv("Anim") >= 5  # UnlimitedBoneInfluences (4.25)
    g, _c = _strip(r)
    variable, wide_index, wide_weight = False, False, False
    if v.engine < 424:
        extra = bool(r.i32())
        nv = r.u32()
    elif not unlimited:
        extra = bool(r.i32())
        if v.cv("SkeletalMesh") >= asset.SKELETAL_SPLIT_MODEL_AND_RENDER_DATA:
            r.u32()  # stride
        nv = r.u32()
    else:
        variable = bool(r.i32())
        most = r.u32()
        r.u32()  # bone entries
        nv = r.u32()
        extra = most > 4
        if v.cv("Anim") >= 4:  # IncreaseBoneIndexLimitPerChunk
            wide_index = bool(r.i32())
        if v.cv("UE5Main") >= 90:  # IncreasedSkinWeightPrecision
            wide_weight = bool(r.i32())
    raw = b""
    if not g & 2:
        _esize, _n, raw = r.bulk_array()
    lookup = None
    if unlimited:
        lg, _lc = _strip(r)
        r.i32()  # vertices
        if not lg & 2:
            _e, _n, lraw = r.bulk_array()
            lookup = np.frombuffer(lraw, "<u4").astype(np.int64)
    if not raw or nv <= 0:
        return None
    isz, wsz = (2 if wide_index else 1), (2 if wide_weight else 1)
    data = np.frombuffer(raw, np.uint8)
    if variable and lookup is not None and len(lookup) == nv:
        offsets, counts = lookup >> 8, lookup & 0xFF
        k = max(int(counts.max()), 1)
        joints = np.full((nv, k), -1, np.int64)
        weights = np.zeros((nv, k), np.float64)
        for j in range(k):
            has = counts > j
            o = offsets[has]
            joints[has, j] = _unpack(data, o + j * isz, isz)
            weights[has, j] = _unpack(data, o + counts[has] * isz + j * wsz, wsz)
    else:
        stride = len(raw) // nv
        k = stride // (isz + wsz) if stride else 0
        if k == 0 or k * (isz + wsz) != stride:
            k = 8 if extra else 4
            stride = k * (isz + wsz)
        if stride * nv > len(raw):
            raise FormatError("the skin weights don't match the vertex count")
        rows = np.arange(nv, dtype=np.int64)[:, None] * stride
        cols = np.arange(k, dtype=np.int64)[None, :]
        joints = _unpack(data, rows + cols * isz, isz)
        weights = _unpack(data, rows + k * isz + cols * wsz, wsz).astype(np.float64)
    weights = weights / (65535.0 if wide_weight else 255.0)
    joints = np.where(weights > 0, joints, -1)
    return joints, weights


def _unpack(data: np.ndarray, at: np.ndarray, size: int) -> np.ndarray:
    at = np.asarray(at, np.int64)
    if size == 1:
        return data[at].astype(np.int64)
    return data[at].astype(np.int64) | (data[at + 1].astype(np.int64) << 8)


def _skel_streamed(r: Reader, v: asset.Versions):
    _strip(r)
    indices = _index_container(r)
    positions = _positions(r)
    normals, uvs, _n = _vertex_buffer(r, v)
    return indices, positions, normals, uvs, _weights_after(r, v)


def _weights_after(r: Reader, v: asset.Versions):
    """The skin weights that follow the vertex buffers, or None (with the reason) when they can't be read."""
    try:
        return _skin_weights(r, v)
    except (FormatError, ValueError, IndexError) as e:
        return str(e) or "the skin weights couldn't be read"


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
        return sections, indices, positions, normals, uvs, _weights_after(r, v)
    cooked_out = bool(r.i32())
    inlined = bool(r.i32())
    r.skip(2 * r.count())  # required bones
    if g & 2 or cooked_out:
        return None
    sections = [_skel_section(r, v) for _ in range(r.count(4096))]
    r.skip(2 * r.count())  # active bones
    r.u32()  # buffers size
    if inlined:
        streamed = _skel_streamed(r, v)
    else:
        data = asset.read_bulk(r)
        if not data:
            return None
        streamed = _skel_streamed(Reader(data, pkg), v)
    return (sections,) + streamed


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
    bones, ref_pose = read_reference_skeleton(r, v)
    if v.cv("SkeletalMesh") < asset.SKELETAL_SPLIT_MODEL_AND_RENDER_DATA:
        raise FormatError(f"{e.name}: skeletal meshes from before Unreal 4.19 aren't supported")
    if not g & 1:
        raise FormatError(f"{e.name} holds editor data (an uncooked mesh); only cooked games are supported")
    cooked = bool(r.i32())
    if not cooked:
        raise FormatError(f"{e.name} has no cooked render data")
    skeleton = skeleton_of(pkg, props)
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
        sections, indices, positions, normals, uvs, skin = got
        if len(positions) == 0:
            continue
        if lod:
            notes.append(f"Level of detail {lod} is the most detailed one this mesh ships with.")
        joints = weights = None
        if isinstance(skin, tuple):
            joints, weights = _global_joints(skin[0], sections, len(positions)), skin[1]
            if len(joints) != len(positions):
                joints = weights = None
        if joints is None and bones:
            why = skin if isinstance(skin, str) else "the mesh ships without them"
            notes.append(f"Its skin weights couldn't be read ({why}), so it can't be posed.")
        return Mesh4(e.name, "SkeletalMesh", positions, normals, uvs, indices, sections, mats, slots, bones, n, lod,
                     sockets, notes, ref_pose, skeleton, joints, weights)
    return Mesh4(e.name, "SkeletalMesh", np.zeros((0, 3)), None, [], np.zeros(0, np.int64), [], mats, slots, bones,
                 n, 0, sockets, ["No level of detail has geometry."], ref_pose, skeleton)


def read_reference_skeleton(r: Reader, v: asset.Versions) -> tuple[list[tuple[str, int]], np.ndarray]:
    """``FReferenceSkeleton``: the bones (name, parent index) and their reference pose, (B, 10) as rotation
    (x, y, z, w), translation and scale, in Unreal's frame and units."""
    bones = []
    for _ in range(r.count(1 << 16)):
        bones.append((r.name(), r.i32()))
    n = r.count(1 << 16)
    wide = v.engine >= 500
    raw = r.bytes((80 if wide else 40) * n)
    pose = np.frombuffer(raw, "<f8" if wide else "<f4").reshape(n, 10).astype(np.float64) if n else np.zeros((0, 10))
    for _ in range(r.count(1 << 16)):  # name -> index
        r.name()
        r.i32()
    if len(pose) != len(bones):
        raise FormatError(f"the skeleton has {len(bones)} bones but {len(pose)} reference transforms")
    return bones, pose


def skeleton_of(pkg: Package, props: Props) -> ObjectRef | None:
    """The Skeleton asset a skeletal mesh (or animation) names; without readable properties, the package's
    only Skeleton import."""
    ref = props.get("Skeleton")
    if isinstance(ref, ObjectRef) and ref:
        return ref
    found = [-(i + 1) for i, imp in enumerate(pkg.imports) if imp.class_name == "Skeleton"]
    found += [e.index + 1 for e in pkg.exports if e.class_name == "Skeleton"]
    return ObjectRef(pkg, found[0]) if len(found) == 1 else None


def _global_joints(local: np.ndarray, sections: list[Section4], count: int) -> np.ndarray:
    """Per-vertex bone indices into the mesh's skeleton: each section's weights index its own bone map."""
    out = np.full(local.shape, -1, np.int64)
    for s in sections:
        if s.bone_map is None or not len(s.bone_map):
            continue
        lo, hi = s.base_vertex, min(s.base_vertex + (s.vertices or count), count, len(local))
        if hi <= lo:
            continue
        j = local[lo:hi]
        ok = (j >= 0) & (j < len(s.bone_map))
        out[lo:hi] = np.where(ok, s.bone_map[np.clip(j, 0, len(s.bone_map) - 1)], -1)
    return out


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
