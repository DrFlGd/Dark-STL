"""RE Engine ``.mesh`` files (magic ``MESH``).

A mesh file holds a few levels of detail. Each level is a list of mesh groups (the parts a game
can hide, numbered by ``visconGroupID``), each a list of sub-meshes, one per material. All the
geometry of a file sits in one planar vertex buffer: every attribute (positions, normals and
tangents, UVs, bone weights, colours) is its own contiguous stream, described by a table of
"vertex elements" (type, stride, start offset). Faces are a flat 16-bit (or 32-bit) index buffer;
a sub-mesh takes a range of it and adds its ``vertex_start`` to every index.

The layout changed with the engine. Versions are told apart by the number in the file's
extension (``.mesh.1808312334`` is RE2, ``.mesh.221108797`` RE4...), mapped to an "era" ordinal so
that comparisons read as "RE8 and later" rather than as a comparison of two date-like numbers.

Since Street Fighter 6 (and in Resident Evil 4), the top levels of detail may be "streamed":
their vertex and face buffers live in a second file of the same name under the
``streaming/`` folder. :func:`read_mesh` takes that buffer as an optional argument and reads
the levels it can without it.

Positions are in metres, Y up, right-handed. UVs use the DirectX convention (V down from the
top of the texture). Normals and tangents are stored as signed bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .binary import FormatError, Reader

MAGIC = 0x4853454D  # "MESH"
MAGIC_MPLY = 0x594C504D  # "MPLY": stage meshes made of many tiny parts, not supported

# File-extension version -> era ordinal and game. Eras follow the modding community's names for
# each generation of the format; the ordinal is what the layout decisions compare against.
ERA_DMC5, ERA_RE2, ERA_RE3, ERA_RE8, ERA_RERT, ERA_RE7RT, ERA_MHRSB = 75, 80, 85, 90, 95, 96, 100
ERA_SF6, ERA_RE4, ERA_DD2, ERA_KG, ERA_DD2NEW, ERA_DR, ERA_ONI2, ERA_WILDS, ERA_PRAG, ERA_MHS3, ERA_RE9 = (
    105, 110, 115, 120, 124, 125, 127, 130, 135, 136, 140)

VERSIONS: dict[int, tuple[int, str]] = {
    1808282334: (ERA_DMC5, "Devil May Cry 5"),
    1808312334: (ERA_RE2, "Resident Evil 2"),
    1902042334: (ERA_RE3, "Resident Evil 3"),
    2101050001: (ERA_RE8, "Resident Evil Village"),
    2102020001: (ERA_RE8, "Resident Evil Re:Verse"),
    2109108288: (ERA_RERT, "Resident Evil 2"),  # also RE3; the ray-tracing re-releases
    220128762: (ERA_RE7RT, "Resident Evil 7"),  # the ray-tracing re-release
    2109148288: (ERA_MHRSB, "Monster Hunter Rise"),
    230110883: (ERA_SF6, "Street Fighter 6"),
    221108797: (ERA_RE4, "Resident Evil 4"),
    231011879: (ERA_DD2, "Dragon's Dogma 2"),
    240306278: (ERA_KG, "Kunitsu-Gami"),
    240423143: (ERA_DD2NEW, "Dragon's Dogma 2"),
    240424828: (ERA_DR, "Dead Rising Deluxe Remaster"),
    240820143: (ERA_ONI2, "Monster Hunter Wilds"),
    240827123: (ERA_ONI2, "Onimusha 2"),
    241111606: (ERA_WILDS, "Monster Hunter Wilds"),
    250604100: (ERA_MHS3, "Monster Hunter Stories 3"),
    250925211: (ERA_RE9, "Resident Evil Requiem"),
}

# Vertex element types, by the ``type`` field of the element table.
EL_POSITION, EL_NORTAN, EL_UV, EL_UV2, EL_WEIGHT, EL_COLOR, EL_UNKNOWN6, EL_EXTRA_WEIGHT = range(8)
ELEMENT_NAMES = {EL_POSITION: "position", EL_NORTAN: "normal/tangent", EL_UV: "uv", EL_UV2: "uv2",
                 EL_WEIGHT: "weights", EL_COLOR: "colour", EL_UNKNOWN6: "unknown", EL_EXTRA_WEIGHT: "extra weights"}


def era_of(version: int) -> int:
    """The era of a file version; an unknown version takes the nearest known one's."""
    if version in VERSIONS:
        return VERSIONS[version][0]
    nearest = min(VERSIONS, key=lambda v: abs(v - version))
    return VERSIONS[nearest][0]


def game_of(version: int) -> str | None:
    return VERSIONS[version][1] if version in VERSIONS else None


def is_mesh(data: bytes) -> bool:
    return len(data) >= 4 and int.from_bytes(data[:4], "little") in (MAGIC, MAGIC_MPLY)


@dataclass
class VertexElement:
    type: int
    stride: int
    offset: int


@dataclass
class SubMesh:
    material_index: int
    vertex_buffer: int  # 0: the file's own buffer; n: the n-th streamed buffer
    face_count: int  # indices, not triangles (the field is named "faceCount" but counts indices)
    face_start: int
    vertex_start: int


@dataclass
class MeshGroup:
    group_id: int
    vertex_count: int
    face_count: int
    submeshes: list[SubMesh] = field(default_factory=list)


@dataclass
class Lod:
    distance: float
    vertex_format: int
    groups: list[MeshGroup] = field(default_factory=list)


@dataclass
class VertexBuffer:
    """One planar vertex buffer and its element table, plus the face buffer that goes with it."""

    elements: list[VertexElement]
    data: bytes
    faces: bytes
    available: bool = True  # False for a streamed buffer whose file was not found

    def stream(self, kind: int, vertex_count: int) -> tuple[bytes, int] | None:
        """The bytes of the first element of ``kind`` and its stride, if the buffer has one."""
        for i, el in enumerate(self.elements):
            if el.type != kind:
                continue
            end = self.elements[i + 1].offset if i + 1 < len(self.elements) else el.offset + el.stride * vertex_count
            end = min(end, len(self.data))
            return self.data[el.offset:end], el.stride
        return None

    def vertex_count(self) -> int:
        for i, el in enumerate(self.elements):
            if el.type == EL_POSITION:
                end = self.elements[i + 1].offset if i + 1 < len(self.elements) else len(self.data)
                return max(0, (end - el.offset) // 12)
        return 0


@dataclass
class Bone:
    name: str
    parent: int
    local: np.ndarray  # (4, 4), column-vector convention as stored (translation in the last row)


@dataclass
class ReMesh:
    version: int
    era: int
    lods: list[Lod]
    buffers: list[VertexBuffer]  # [own, streamed 0, streamed 1, ...]
    material_names: list[str]
    bones: list[Bone]
    index_32: bool
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    uv_count: int
    streamed_count: int  # streamed buffers the file refers to
    streamed_found: bool

    @property
    def game(self) -> str | None:
        return game_of(self.version)

    def available(self, buffer: int) -> bool:
        return buffer < len(self.buffers) and self.buffers[buffer].available

    def best_lod(self) -> int | None:
        """The most detailed level whose buffers are all here; failing that, the level with the largest
        share of its sub-meshes readable (:meth:`complete` tells whether a level is whole)."""
        best, best_share = None, 0.0
        for i, lod in enumerate(self.lods):
            subs = [s for g in lod.groups for s in g.submeshes]
            if not subs:
                continue
            share = sum(self.available(s.vertex_buffer) for s in subs) / len(subs)
            if share >= 1.0:
                return i
            if share > best_share:
                best, best_share = i, share
        return best

    def complete(self, lod_index: int) -> bool:
        lod = self.lods[lod_index]
        return all(self.available(s.vertex_buffer) for g in lod.groups for s in g.submeshes)


@dataclass
class SubMeshData:
    """A sub-mesh's geometry, decoded."""

    group: int
    material_index: int
    positions: np.ndarray  # (n, 3) float32
    normals: np.ndarray | None  # (n, 3) float32
    uvs: np.ndarray | None  # (n, 2) float32
    triangles: np.ndarray  # (m, 3) int64, into positions


# ---------------------------------------------------------------------------
# Header structures


def _read_header(r: Reader, era: int) -> dict:
    h: dict = {}
    r.seek(0)
    magic = r.u32()
    if magic == MAGIC_MPLY:
        raise FormatError("MPLY meshes (stage geometry assembled from many pieces) are not supported")
    if magic != MAGIC:
        raise FormatError("Not an RE Engine mesh file")
    h["version"] = r.u32()
    h["file_size"] = r.u32()
    h["lod_name_hash"] = r.u32()
    keys: list[str]
    if era < ERA_SF6:
        h["content_flag"] = r.u16()
        h["name_count"] = r.i16()
        r.u32()
        keys = ["mesh_group", "shadow", "occlusion", "skeleton", "normal_recalc", "blend_shapes", "aabb", "mesh",
                "floats", "material_names", "bone_names", "blend_shape_names", "name_offsets"]
    elif era < ERA_ONI2:
        h["content_flag"] = r.u16()
        r.i16()
        h["name_count"] = r.i16()
        r.i16()
        r.u32()
        r.u32()
        keys = ["mesh_group", "shadow", "occlusion", "normal_recalc", "blend_shapes", "mesh", "unknown1", "floats",
                "aabb", "skeleton", "material_names", "bone_names", "blend_shape_names"]
        keys += ["streaming_info", "name_offsets"] if era < ERA_DD2 else ["name_offsets", "dd2_hash", "streaming_info"]
        keys += ["vertices", "unknown4"]
    else:
        r.u32()
        h["name_count"] = r.i16()
        h["content_flag"] = r.u16()
        r.i16()
        r.u32()
        r.u32()
        r.u32()
        r.i16()
        keys = ["vertices", "mesh_group", "shadow", "occlusion", "normal_recalc", "blend_shapes", "mesh", "unknown1",
                "floats", "aabb", "skeleton", "material_names", "bone_names", "blend_shape_names", "name_offsets",
                "streaming_info", "unknown4"]
    for k in keys:
        h[k] = r.u64()
    return h


def _read_submesh(r: Reader, era: int) -> SubMesh:
    material_index, _is_quad, buffer_index, _pad = r.unpack("4B")
    if era >= ERA_DR:
        r.u32()
    face_count, face_start, vertex_start = r.unpack("3I")
    if era >= ERA_RE8:
        r.unpack("2I")  # streaming offsets
    if era >= ERA_DD2NEW:
        r.u32()
    return SubMesh(material_index, buffer_index, face_count, face_start, vertex_start)


def _read_mesh_group(r: Reader, era: int) -> MeshGroup:
    group_id, count = r.unpack("2B")
    r.unpack("3H")
    vertex_count, face_count = r.unpack("2I")
    group = MeshGroup(group_id, vertex_count, face_count)
    for _ in range(count):
        group.submeshes.append(_read_submesh(r, era))
    return group


def _read_lod(r: Reader, offset: int, era: int) -> Lod:
    r.seek(offset)
    count, vertex_format = r.unpack("2B")
    r.u16()
    distance = r.f32()
    r.u64()
    offsets = [r.u64() for _ in range(count)]
    lod = Lod(distance, vertex_format)
    for off in offsets:
        r.seek(off)
        lod.groups.append(_read_mesh_group(r, era))
    return lod


def _read_main(r: Reader, offset: int, era: int) -> tuple[list[Lod], int, bool, int, np.ndarray, np.ndarray]:
    r.seek(offset)
    lod_count, material_count, uv_count, _weights = r.unpack("4b")
    _total = r.u16()
    index_32 = bool(r.i8())
    r.u8()
    if era < ERA_RE8:
        r.u64()
    r.unpack("4f")  # bounding sphere
    bmin = np.array(r.unpack("4f")[:3], np.float32)
    bmax = np.array(r.unpack("4f")[:3], np.float32)
    r.u64()
    offsets = [r.u64() for _ in range(lod_count)]
    lods = [_read_lod(r, off, era) for off in offsets]
    return lods, material_count, index_32, uv_count, bmin, bmax


def _read_elements(r: Reader, offset: int, count: int) -> list[VertexElement]:
    r.seek(offset)
    out = []
    for _ in range(count):
        kind, stride, start = r.unpack("2HI")
        out.append(VertexElement(kind, stride, start))
    return out


def _pad16(n: int) -> int:
    return (n + 15) & ~15


def _read_buffers(r: Reader, h: dict, era: int, streaming: bytes | None) -> tuple[list[VertexBuffer], int, bool]:
    """The file's own vertex/face buffer, then one per streamed buffer the header lists."""
    r.seek(h["mesh"])
    element_offset = r.u64()
    vertex_offset = r.u64()
    if era < ERA_SF6:
        face_offset = r.u64()
        if era > ERA_RE8:
            r.u64()
        vertex_size, face_size = r.unpack("2I")
        _main_count, element_count = r.unpack("2H")
        r.unpack("2I")
        r.unpack("2h")
        if era > ERA_RE8:
            r.u64()
        streaming_element_offset = 0
        main_count = _main_count
    else:
        r.u64()
        _total, vertex_size = r.unpack("2I")
        main_count, element_count = r.unpack("2H")
        if era >= ERA_PRAG:
            r.unpack("2Q")
        block2 = r.u32()
        r.u32()
        r.unpack("2h")
        r.u64()
        r.u64()
        streaming_element_offset = r.u64()
        r.u64()
        face_offset = vertex_offset + vertex_size
        face_size = max(0, block2 - vertex_size)
    stream_header_pos = r.pos

    elements = _read_elements(r, element_offset, element_count)
    data = r.data[vertex_offset:vertex_offset + vertex_size]
    faces = r.data[face_offset:face_offset + face_size]
    buffers = [VertexBuffer(elements, data, faces)]

    # streamed buffers (SF6 and later): their ranges in the streaming file
    streamed = 0
    found = False
    if era >= ERA_SF6 and h.get("streaming_info"):
        r.seek(h["streaming_info"])
        count, _unk = r.unpack("2I")
        entry_offset = r.u64()
        r.seek(entry_offset)
        ranges = [r.unpack("2I") for _ in range(count)]
        streamed = count
        found = streaming is not None and count > 0
        r.seek(stream_header_pos)
        for i, (start, _length) in enumerate(ranges):
            r.u64()
            _total, vertex_length = r.unpack("2I")
            r.unpack("2H")
            if era >= ERA_PRAG:
                r.unpack("2Q")
            unpadded = r.u32()
            r.unpack("8I")
            r.u32()
            saved = r.pos
            els = _read_elements(r, streaming_element_offset + i * _pad16(8 * main_count), main_count)
            r.seek(saved)
            if streaming is not None:
                vdata = streaming[start:start + vertex_length]
                fdata = streaming[start + vertex_length:start + unpadded]
                buffers.append(VertexBuffer(els, vdata, fdata))
            else:
                buffers.append(VertexBuffer(els, b"", b"", available=False))
    return buffers, streamed, found


def _read_names(r: Reader, h: dict) -> list[str]:
    if not h.get("name_offsets") or h["name_count"] <= 0:
        return []
    r.seek(h["name_offsets"])
    offsets = [r.u64() for _ in range(h["name_count"])]
    return [r.cstring(o) for o in offsets]


def _read_bones(r: Reader, h: dict, names: list[str]) -> list[Bone]:
    if not h.get("skeleton"):
        return []
    r.seek(h["skeleton"])
    bone_count, remap_count = r.unpack("2I")
    r.u64()
    bone_offset, local_offset, _world_offset, _inverse_offset = r.unpack("4Q")
    bone_names: list[str] = []
    if h.get("bone_names"):
        r.seek(h["bone_names"])
        remap = [r.u16() for _ in range(bone_count)]
        bone_names = [names[i] if i < len(names) else f"bone{i}" for i in remap]
    r.seek(bone_offset)
    info = [r.unpack("H7h") for _ in range(bone_count)]
    r.seek(local_offset)
    locals_ = [np.array(r.unpack("16f"), np.float32).reshape(4, 4) for _ in range(bone_count)]
    return [Bone(bone_names[i] if i < len(bone_names) else f"bone{i}", info[i][1], locals_[i])
            for i in range(bone_count)]


# ---------------------------------------------------------------------------
# Public API


def read_mesh(data: bytes, version: int | None = None, streaming: bytes | None = None) -> ReMesh:
    """Parse a mesh file. ``version`` is the number in its extension (read from the header when not
    given); ``streaming`` is the matching file from the ``streaming/`` folder, when there is one."""
    r = Reader(data)
    if len(data) < 16 or int.from_bytes(data[:4], "little") not in (MAGIC, MAGIC_MPLY):
        raise FormatError("Not an RE Engine mesh file")
    file_version = int.from_bytes(data[4:8], "little")
    version = version or file_version
    era = era_of(version)
    h = _read_header(r, era)
    if not h.get("mesh_group") or not h.get("mesh"):
        raise FormatError("The mesh file has no geometry")
    lods, material_count, index_32, uv_count, bmin, bmax = _read_main(r, h["mesh_group"], era)
    buffers, streamed, found = _read_buffers(r, h, era, streaming)
    names = _read_names(r, h)
    material_names: list[str] = []
    if h.get("material_names") and material_count > 0:
        r.seek(h["material_names"])
        remap = [r.u16() for _ in range(material_count)]
        material_names = [names[i] if i < len(names) else f"material{i}" for i in remap]
    bones = _read_bones(r, h, names)
    return ReMesh(version, era, lods, buffers, material_names, bones, index_32, bmin, bmax, uv_count, streamed, found)


def _decode_streams(buffer: VertexBuffer) -> dict:
    n = buffer.vertex_count()
    out: dict = {"count": n}
    pos = buffer.stream(EL_POSITION, n)
    out["positions"] = np.frombuffer(pos[0], "<f4", count=n * 3).reshape(n, 3) if pos else np.zeros((0, 3), np.float32)
    nt = buffer.stream(EL_NORTAN, n)
    out["normals"] = None
    if nt and len(nt[0]) >= n * 8:
        nortan = np.frombuffer(nt[0], np.int8, count=n * 8).reshape(n, 2, 4)
        out["normals"] = nortan[:, 0, :3].astype(np.float32) / 127.0
    uv = buffer.stream(EL_UV, n)
    out["uvs"] = None
    if uv and len(uv[0]) >= n * 4:
        out["uvs"] = np.frombuffer(uv[0], "<f2", count=n * 2).reshape(n, 2).astype(np.float32)
    return out


def submeshes(mesh: ReMesh, lod_index: int | None = None) -> list[SubMeshData]:
    """The decoded sub-meshes of one level of detail (the best available one by default)."""
    if lod_index is None:
        lod_index = mesh.best_lod()
    if lod_index is None or lod_index >= len(mesh.lods):
        return []
    lod = mesh.lods[lod_index]
    decoded: dict[int, dict] = {}
    out: list[SubMeshData] = []
    index_dtype = "<u4" if mesh.index_32 else "<u2"
    index_size = 4 if mesh.index_32 else 2
    for gi, group in enumerate(lod.groups):
        for sub in group.submeshes:
            if sub.vertex_buffer >= len(mesh.buffers) or not mesh.buffers[sub.vertex_buffer].available:
                continue
            buf = mesh.buffers[sub.vertex_buffer]
            if sub.vertex_buffer not in decoded:
                decoded[sub.vertex_buffer] = _decode_streams(buf)
            streams = decoded[sub.vertex_buffer]
            start = sub.face_start * index_size
            count = (sub.face_count // 3) * 3
            raw = buf.faces[start:start + count * index_size]
            if len(raw) < count * index_size:
                raise FormatError("A sub-mesh's faces run past the end of the face buffer")
            idx = np.frombuffer(raw, index_dtype, count=count).astype(np.int64) + sub.vertex_start
            if count == 0:
                continue
            lo, hi = int(idx.min()), int(idx.max())
            if lo < 0 or hi >= streams["count"]:
                raise FormatError("A sub-mesh refers to vertices outside the vertex buffer")
            # take just the vertices this sub-mesh uses, so each part stands alone
            used = np.unique(idx)
            remap = np.full(hi + 1, -1, np.int64)
            remap[used] = np.arange(len(used))
            tris = remap[idx].reshape(-1, 3)
            out.append(SubMeshData(
                gi, sub.material_index, np.ascontiguousarray(streams["positions"][used]),
                np.ascontiguousarray(streams["normals"][used]) if streams["normals"] is not None else None,
                np.ascontiguousarray(streams["uvs"][used]) if streams["uvs"] is not None else None, tris))
    return out


__all__ = ["ReMesh", "SubMeshData", "VERSIONS", "era_of", "game_of", "is_mesh", "read_mesh", "submeshes"]
