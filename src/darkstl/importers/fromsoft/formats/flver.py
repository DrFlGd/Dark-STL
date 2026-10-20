"""FLVER2 model reader (Dark Souls 1/2/3, Bloodborne, Sekiro, Elden Ring).

The layout follows SoulsFormats' FLVER2 implementation. Vertex buffers are
decoded with numpy in one pass per layout member, so large models load fast.

Coordinates are returned exactly as stored (left-handed, Y up, metres);
conversion to a right-handed export frame happens in
:mod:`darkstl.geometry.model_mesh`.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np

from .binary import BinaryReader, FormatError

# LayoutType
FLOAT2, FLOAT3, FLOAT4 = 0x01, 0x02, 0x03
BYTE4A, BYTE4B, SHORT2_TO_FLOAT2, BYTE4C = 0x10, 0x11, 0x12, 0x13
UV_SHORT, UV_PAIR, SHORT_BONE_INDICES = 0x15, 0x16, 0x18
SHORT4_TO_FLOAT4A, SHORT4_TO_FLOAT4B, BYTE4E, EDGE_COMPRESSED = 0x1A, 0x2E, 0x2F, 0xF0

LAYOUT_SIZES = {
    FLOAT2: 8, FLOAT3: 12, FLOAT4: 16, BYTE4A: 4, BYTE4B: 4, SHORT2_TO_FLOAT2: 4, BYTE4C: 4, UV_SHORT: 4,
    UV_PAIR: 8, SHORT_BONE_INDICES: 8, SHORT4_TO_FLOAT4A: 8, SHORT4_TO_FLOAT4B: 8, BYTE4E: 4, EDGE_COMPRESSED: 1,
}

# LayoutSemantic
SEM_POSITION, SEM_BONE_WEIGHTS, SEM_BONE_INDICES, SEM_NORMAL = 0, 1, 2, 3
SEM_UV, SEM_TANGENT, SEM_BITANGENT, SEM_COLOR = 5, 6, 7, 10

# FaceSet flags
FS_LOD1 = 0x0100_0000
FS_LOD2 = 0x0200_0000
FS_EDGE_COMPRESSED = 0x4000_0000
FS_MOTION_BLUR = 0x8000_0000

# Games by FLVER version (several versions are shared, so this is a hint only)
VERSION_GAMES = {
    0x2000B: "Dark Souls", 0x2000C: "Dark Souls", 0x2000D: "Dark Souls",
    0x2000F: "Dark Souls II", 0x20010: "Dark Souls II", 0x20009: "Dark Souls II",
    0x20013: "Bloodborne / Dark Souls III", 0x20014: "Bloodborne / Dark Souls III",
    0x20016: "Dark Souls III / Sekiro", 0x2001A: "Sekiro / Elden Ring", 0x2001B: "Elden Ring / later",
}


@dataclass
class Bone:
    name: str
    translation: np.ndarray
    rotation: np.ndarray  # euler radians, applied X then Z then Y
    scale: np.ndarray
    parent: int
    child: int
    next_sibling: int
    prev_sibling: int

    def local_matrix(self) -> np.ndarray:
        """Column-vector 4x4 matrix equal to SoulsFormats' ComputeLocalTransform."""
        rx, ry, rz = (float(a) for a in self.rotation)
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
        my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        m = np.eye(4)
        m[:3, :3] = my @ mz @ mx @ np.diag(self.scale.astype(np.float64))
        m[:3, 3] = self.translation
        return m


@dataclass
class Texture:
    path: str
    type: str
    scale: tuple[float, float] = (1.0, 1.0)

    @property
    def stem(self) -> str:
        """Texture file name without folders or extension (matches TPF texture names)."""
        base = self.path.replace("\\", "/").rsplit("/", 1)[-1]
        return base.rsplit(".", 1)[0] if "." in base else base


@dataclass
class Material:
    name: str
    mtd: str
    flags: int
    textures: list[Texture] = field(default_factory=list)


@dataclass
class LayoutMember:
    unk00: int
    type: int
    semantic: int
    index: int
    offset: int

    @property
    def size(self) -> int:
        try:
            return LAYOUT_SIZES[self.type]
        except KeyError:
            raise FormatError(f"Unknown vertex layout type 0x{self.type:X}") from None


@dataclass
class FaceSet:
    flags: int
    triangle_strip: bool
    cull_backfaces: bool
    indices: np.ndarray

    @property
    def is_lod0(self) -> bool:
        return (self.flags & (FS_LOD1 | FS_LOD2 | FS_MOTION_BLUR)) == 0

    def triangles(self, allow_restart: bool) -> np.ndarray:
        """Triangle list ``(N, 3)`` with degenerate strip triangles removed."""
        idx = self.indices.astype(np.int64)
        if not self.triangle_strip:
            n = len(idx) // 3
            return idx[: n * 3].reshape(n, 3)
        if len(idx) < 3:
            return np.zeros((0, 3), np.int64)
        a, b, c = idx[:-2], idx[1:-1], idx[2:]
        m = len(a)
        if allow_restart:
            r = idx == 0xFFFF
            valid = ~(r[:-2] | r[1:-1] | r[2:])
        else:
            valid = np.ones(m, bool)
        pos = np.arange(m)
        last_invalid = np.maximum.accumulate(np.where(valid, -1, pos))
        flip = ((pos - last_invalid - 1) % 2) == 1
        keep = valid & (a != b) & (b != c) & (a != c)
        tris = np.stack([np.where(flip, c, a), b, np.where(flip, a, c)], axis=1)
        return tris[keep]


@dataclass
class VertexData:
    count: int
    positions: np.ndarray
    normals: np.ndarray | None = None
    uvs: list[np.ndarray] = field(default_factory=list)
    bone_indices: np.ndarray | None = None
    bone_weights: np.ndarray | None = None


@dataclass
class Mesh:
    dynamic: int
    material_index: int
    default_bone_index: int
    bone_indices: list[int]
    face_sets: list[FaceSet]
    vertices: VertexData

    def lod0_face_set(self) -> FaceSet | None:
        for fs in self.face_sets:
            if fs.is_lod0:
                return fs
        return self.face_sets[0] if self.face_sets else None

    def triangles(self) -> np.ndarray:
        fs = self.lod0_face_set()
        if fs is None:
            return np.zeros((0, 3), np.int64)
        tris = fs.triangles(allow_restart=self.vertices.count < 0xFFFF)
        if len(tris) and tris.max() >= self.vertices.count:
            tris = tris[(tris < self.vertices.count).all(axis=1)]
        return tris


@dataclass
class Flver:
    version: int
    big_endian: bool
    unicode: bool
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    bones: list[Bone]
    materials: list[Material]
    meshes: list[Mesh]
    dummy_count: int = 0

    @property
    def game_hint(self) -> str:
        return VERSION_GAMES.get(self.version, f"FLVER 0x{self.version:X}")

    def bone_world_matrices(self) -> np.ndarray:
        """Model-space 4x4 transform for every bone (column-vector convention)."""
        n = len(self.bones)
        world = np.zeros((n, 4, 4))
        done = np.zeros(n, bool)

        def resolve(i: int, depth: int = 0) -> np.ndarray:
            if done[i]:
                return world[i]
            local = self.bones[i].local_matrix()
            parent = self.bones[i].parent
            if 0 <= parent < n and parent != i and depth < 512:
                local = resolve(parent, depth + 1) @ local
            world[i] = local
            done[i] = True
            return local

        for i in range(n):
            resolve(i)
        return world


# ---------------------------------------------------------------------------
# Reading


def is_flver(data: bytes) -> bool:
    return data[:6] == b"FLVER\0"


def _column(raw: np.ndarray, member: LayoutMember, dtype: str) -> np.ndarray:
    col = np.ascontiguousarray(raw[:, member.offset:member.offset + member.size])
    if dtype in ("u1", "i1"):
        return col.view(dtype)
    return col.view(dtype).astype(np.dtype(dtype).newbyteorder("="))


def _read_vertex_buffer(br: BinaryReader, data_offset: int, buffer: dict, layout: list[LayoutMember],
                        out: VertexData, uv_factor: float, warned: set) -> None:
    stride = buffer["vertex_size"]
    count = buffer["vertex_count"]
    start = data_offset + buffer["buffer_offset"]
    if start + stride * count > len(br.data):
        raise FormatError("Vertex buffer runs past the end of the file")
    raw = np.frombuffer(br.data, np.uint8, count=stride * count, offset=start).reshape(count, stride)
    e = br.endian
    for m in layout:
        t, s = m.type, m.semantic
        if s == SEM_POSITION:
            if t in (FLOAT3, FLOAT4):
                out.positions = _column(raw, m, e + "f4")[:, :3].astype(np.float32)
            elif t == EDGE_COMPRESSED:
                raise FormatError("Edge-compressed (PS3) vertex positions are not supported")
            else:
                raise FormatError(f"Unsupported position layout type 0x{t:X}")
        elif s == SEM_NORMAL:
            if t in (FLOAT3, FLOAT4):
                n = _column(raw, m, e + "f4")[:, :3]
            elif t in (BYTE4A, BYTE4B, BYTE4C, BYTE4E):
                n = (_column(raw, m, "u1")[:, :3].astype(np.float32) - 127.0) / 127.0
            elif t == SHORT2_TO_FLOAT2:
                sb = _column(raw, m, "i1").astype(np.float32) / 127.0
                n = sb[:, [3, 2, 1]]
            elif t == SHORT4_TO_FLOAT4A:
                n = _column(raw, m, e + "i2")[:, :3].astype(np.float32) / 32767.0
            elif t == SHORT4_TO_FLOAT4B:
                n = (_column(raw, m, e + "u2")[:, :3].astype(np.float32) - 32767.0) / 32767.0
            else:
                raise FormatError(f"Unsupported normal layout type 0x{t:X}")
            out.normals = n.astype(np.float32)
        elif s == SEM_UV:
            if t in (FLOAT2, FLOAT3):
                out.uvs.append(_column(raw, m, e + "f4")[:, :2].astype(np.float32))
            elif t == FLOAT4:
                f = _column(raw, m, e + "f4")
                out.uvs.extend([f[:, :2].astype(np.float32), f[:, 2:4].astype(np.float32)])
            elif t in (BYTE4A, BYTE4B, BYTE4C, SHORT2_TO_FLOAT2, UV_SHORT, SHORT4_TO_FLOAT4B):
                out.uvs.append(_column(raw, m, e + "i2")[:, :2].astype(np.float32) / uv_factor)
            elif t == UV_PAIR:
                sh = _column(raw, m, e + "i2").astype(np.float32) / uv_factor
                out.uvs.extend([sh[:, :2], sh[:, 2:4]])
            else:
                raise FormatError(f"Unsupported UV layout type 0x{t:X}")
        elif s == SEM_BONE_INDICES:
            if t in (BYTE4A, BYTE4B, BYTE4C, BYTE4E):
                out.bone_indices = _column(raw, m, "u1").astype(np.int32)
            elif t == SHORT_BONE_INDICES:
                out.bone_indices = _column(raw, m, e + "u2").astype(np.int32)
            elif (t, s) not in warned:
                warned.add((t, s))
                warnings.warn(f"Ignoring bone indices with layout type 0x{t:X}")
        elif s == SEM_BONE_WEIGHTS:
            if t == BYTE4A:
                out.bone_weights = _column(raw, m, "i1").astype(np.float32) / 127.0
            elif t == BYTE4C:
                out.bone_weights = _column(raw, m, "u1").astype(np.float32) / 255.0
            elif t in (UV_PAIR, SHORT4_TO_FLOAT4A):
                out.bone_weights = _column(raw, m, e + "i2").astype(np.float32) / 32767.0
            elif (t, s) not in warned:
                warned.add((t, s))
                warnings.warn(f"Ignoring bone weights with layout type 0x{t:X}")
        # Tangents, bitangents and vertex colours are not needed by the pipeline.


def read_flver(data: bytes) -> Flver:
    from . import dcx

    data = dcx.maybe_decompress(data)
    br = BinaryReader(data)
    br.assert_ascii("FLVER\0")
    big_endian = br.assert_ascii("L\0", "B\0") == "B\0"
    br.big_endian = big_endian
    version = br.i32()
    if version < 0x20000:
        raise FormatError(f"FLVER0 (version 0x{version:X}, Demon's Souls era) is not supported yet")
    data_offset = br.i32()
    br.i32()  # data length
    dummy_count = br.i32()
    material_count = br.i32()
    bone_count = br.i32()
    mesh_count = br.i32()
    vertex_buffer_count = br.i32()
    bbox_min = np.array(br.vec3(), np.float32)
    bbox_max = np.array(br.vec3(), np.float32)
    br.i32()
    br.i32()  # face counts
    header_index_size = br.u8()
    unicode = br.boolean()
    br.skip(2)
    br.i32()  # unk4C
    face_set_count = br.i32()
    layout_count = br.i32()
    texture_count = br.i32()
    br.seek(0x80)

    br.skip(dummy_count * 0x40)

    raw_materials = []
    for _ in range(material_count):
        name_off, mtd_off, tex_count, tex_index, flags, _gx, _unk18, _zero = br.unpack("8i")
        raw_materials.append((br.get_string(name_off, unicode), br.get_string(mtd_off, unicode),
                              tex_count, tex_index, flags))

    bones = []
    for _ in range(bone_count):
        translation = np.array(br.vec3(), np.float32)
        name_off = br.i32()
        rotation = np.array(br.vec3(), np.float32)
        parent, child = br.i16(), br.i16()
        scale = np.array(br.vec3(), np.float32)
        next_sibling, prev_sibling = br.i16(), br.i16()
        br.skip(12 + 4 + 12 + 0x34)  # bbox min, unk3C, bbox max, padding
        bones.append(Bone(br.get_string(name_off, unicode), translation, rotation, scale,
                          parent, child, next_sibling, prev_sibling))

    raw_meshes = []
    for _ in range(mesh_count):
        dynamic = br.u8()
        br.skip(3)
        material_index = br.i32()
        br.skip(8)
        default_bone = br.i32()
        mesh_bone_count = br.i32()
        _bbox_off = br.i32()
        bone_off = br.i32()
        fs_count = br.i32()
        fs_off = br.i32()
        vb_count = br.i32()
        vb_off = br.i32()
        raw_meshes.append(dict(
            dynamic=dynamic, material_index=material_index, default_bone=default_bone,
            bone_indices=br.array("i4", mesh_bone_count, bone_off).tolist() if mesh_bone_count else [],
            face_sets=br.array("i4", fs_count, fs_off).tolist(),
            vertex_buffers=br.array("i4", vb_count, vb_off).tolist(),
        ))

    face_sets = []
    for _ in range(face_set_count):
        flags = br.u32()
        strip = br.boolean()
        cull = br.boolean()
        br.i16()
        index_count = br.i32()
        index_off = br.i32()
        index_size = 0
        if version > 0x20005:
            br.i32()
            br.i32()
            index_size = br.i32()
            br.i32()
        if index_size == 0:
            index_size = header_index_size
        if index_size == 16:
            indices = br.array("u2", index_count, data_offset + index_off)
        elif index_size == 32:
            indices = br.array("u4", index_count, data_offset + index_off)
        elif index_size == 8 or flags & FS_EDGE_COMPRESSED:
            raise FormatError("Edge-compressed (PS3) face sets are not supported")
        else:
            raise FormatError(f"Unsupported index size {index_size}")
        face_sets.append(FaceSet(flags, strip, cull, indices))

    vertex_buffers = []
    for _ in range(vertex_buffer_count):
        buffer_index, layout_index, vertex_size, vertex_count = br.unpack("4i")
        br.skip(8)
        br.i32()  # buffer length
        buffer_offset = br.i32()
        vertex_buffers.append(dict(buffer_index=buffer_index, layout_index=layout_index,
                                   vertex_size=vertex_size, vertex_count=vertex_count,
                                   buffer_offset=buffer_offset))

    layouts: list[list[LayoutMember]] = []
    for _ in range(layout_count):
        member_count = br.i32()
        br.skip(8)
        member_off = br.i32()
        members = []
        with br.step_in(member_off):
            struct_offset = 0
            for _ in range(member_count):
                unk00, _declared_offset, ltype, semantic, index = br.unpack("iiIIi")
                member = LayoutMember(unk00, ltype, semantic, index, struct_offset)
                struct_offset += member.size
                members.append(member)
        layouts.append(members)

    textures = []
    for _ in range(texture_count):
        path_off, type_off = br.i32(), br.i32()
        scale = br.vec2()
        br.skip(4 + 12)
        textures.append(Texture(br.get_string(path_off, unicode), br.get_string(type_off, unicode),
                                (float(scale[0]), float(scale[1]))))

    materials = []
    for name, mtd, tex_count, tex_index, flags in raw_materials:
        materials.append(Material(name, mtd, flags, textures[tex_index:tex_index + tex_count]))

    uv_factor = 2048.0 if version >= 0x2000F else 1024.0
    meshes = []
    warned: set = set()
    for rm in raw_meshes:
        buffers = [vertex_buffers[i] for i in rm["vertex_buffers"]]
        count = buffers[0]["vertex_count"] if buffers else 0
        vdata = VertexData(count, np.zeros((count, 3), np.float32))
        for buf in buffers:
            layout = layouts[buf["layout_index"]]
            layout_size = sum(m.size for m in layout)
            if layout_size != buf["vertex_size"]:
                raise FormatError(f"Vertex size {buf['vertex_size']} does not match layout size {layout_size}")
            _read_vertex_buffer(br, data_offset, buf, layout, vdata, uv_factor, warned)
        meshes.append(Mesh(rm["dynamic"], rm["material_index"], rm["default_bone"], rm["bone_indices"],
                           [face_sets[i] for i in rm["face_sets"]], vdata))

    return Flver(version, big_endian, unicode, bbox_min, bbox_max, bones, materials, meshes, dummy_count)
