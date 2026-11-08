"""Byte-built Unreal Engine 3 packages shaped like Borderlands 2's, for the Borderlands importer tests.

:class:`PackageBuilder` writes the summary, name, import and export tables and each
export's data; ``build(compress="lzo")`` compresses the body in LZO blocks the way the
game's packages are (as single literal runs, which the decoder reads like any LZO data).
Helpers write tagged properties and the binary parts of meshes, textures and materials.
"""

from __future__ import annotations

import math
import struct
import zlib

import numpy as np

TAG = 0x9E2A83C1


def lzo_literal(data: bytes) -> bytes:
    """A valid LZO1X stream holding ``data`` as one literal run (``len(data) >= 4``), then the end marker."""
    n = len(data)
    assert n >= 4
    if n - 3 <= 15:
        head = bytes([n - 3])
    else:
        rem = n - 3 - 15
        z = (rem - 1) // 255
        head = b"\0" + b"\0" * z + bytes([rem - 255 * z])
    return head + data + b"\x11\x00\x00"


def fstring(s: str) -> bytes:
    b = s.encode("latin-1") + b"\0"
    return struct.pack("<i", len(b)) + b


class PackageBuilder:
    def __init__(self, version: int = 832, licensee: int = 46):
        self.version, self.licensee = version, licensee
        self.names: list[str] = []
        self._name_index: dict[str, int] = {}
        self.imports: list[tuple] = []
        self.exports: list[dict] = []
        self.name("None")
        core = self.add_import("Core", "Package", 0, "Core")
        engine = self.add_import("Core", "Package", 0, "Engine")
        self._class_outer = {"Core": core, "Engine": engine}
        self._classes: dict[str, int] = {}

    # -- tables
    def name(self, s: str) -> int:
        if s not in self._name_index:
            self._name_index[s] = len(self.names)
            self.names.append(s)
        return self._name_index[s]

    def fname(self, s: str) -> bytes:
        return struct.pack("<ii", self.name(s), 0)

    def add_import(self, class_package: str, class_name: str, outer: int, name: str) -> int:
        self.imports.append((self.name(class_package), self.name(class_name), outer, self.name(name)))
        return -len(self.imports)

    def class_ref(self, cls: str) -> int:
        if cls not in self._classes:
            pkg = "Core" if cls in ("Package", "Class") else "Engine"
            self._classes[cls] = self.add_import("Core", "Class", self._class_outer[pkg], cls)
        return self._classes[cls]

    def add_export(self, cls: str, name: str, data: bytes = b"", outer: int = 0, export_flags: int = 0) -> int:
        self.name(name)
        self.exports.append({"class": self.class_ref(cls), "outer": outer, "name": name, "data": data,
                             "eflags": export_flags})
        return len(self.exports)

    def set_data(self, ref: int, data: bytes) -> None:
        self.exports[ref - 1]["data"] = data

    # -- properties
    def prop(self, name: str, kind: str, value: bytes, index: int = 0, struct_name: str | None = None) -> bytes:
        out = self.fname(name) + self.fname(kind) + struct.pack("<ii", len(value), index)
        if struct_name is not None:
            out += self.fname(struct_name)
        return out + value

    def p_int(self, name, v, index=0):
        return self.prop(name, "IntProperty", struct.pack("<i", v), index)

    def p_float(self, name, v):
        return self.prop(name, "FloatProperty", struct.pack("<f", v))

    def p_bool(self, name, v):
        return self.fname(name) + self.fname("BoolProperty") + struct.pack("<ii", 0, 0) + bytes([1 if v else 0])

    def p_name(self, name, v, index=0):
        return self.prop(name, "NameProperty", self.fname(v), index)

    def p_obj(self, name, ref):
        return self.prop(name, "ObjectProperty", struct.pack("<i", ref))

    def p_enum(self, name, enum, value):
        return self.fname(name) + self.fname("ByteProperty") + struct.pack("<ii", 8, 0) + self.fname(enum) \
            + self.fname(value)

    def p_array(self, name, count, raw):
        return self.prop(name, "ArrayProperty", struct.pack("<i", count) + raw)

    def p_struct(self, name, struct_name, raw):
        return self.prop(name, "StructProperty", raw, struct_name=struct_name)

    def end(self) -> bytes:
        return self.fname("None")

    def props(self, *items: bytes) -> bytes:
        return struct.pack("<i", -1) + b"".join(items) + self.end()  # net index, then the tagged list

    # -- writing
    def _summary(self, offsets: tuple, compression: int, chunks: list) -> bytes:
        name_off, imp_off, exp_off, dep_off, header_size = offsets
        s = struct.pack("<II", TAG, self.version | (self.licensee << 16))
        s += struct.pack("<i", header_size) + fstring("None") + struct.pack("<I", 0x8)
        s += struct.pack("<iiii", len(self.names), name_off, len(self.exports), exp_off)
        s += struct.pack("<ii", len(self.imports), imp_off)
        s += struct.pack("<i", dep_off)
        s += struct.pack("<iii", 0, 0, 0) + struct.pack("<i", 0)  # f38..f40, unk38
        s += b"\x11" * 16  # guid
        s += struct.pack("<i", 1) + struct.pack("<iii", len(self.exports), len(self.names), 0)
        s += struct.pack("<ii", 8710, 136)
        s += struct.pack("<I", compression) + struct.pack("<i", len(chunks))
        for c in chunks:
            s += struct.pack("<iiii", *c)
        s += struct.pack("<i", 0)
        return s

    def build(self, compress: str | None = None, block: int = 4096, chunk: int = 16384) -> bytes:
        n_chunks = 0
        for _ in range(3):  # the header size depends on the chunk count, which depends on the body
            head_len = len(self._summary((0, 0, 0, 0, 0), 0, [(0, 0, 0, 0)] * n_chunks))
            body, offsets = self._body(head_len)
            want = (len(body) + chunk - 1) // chunk if compress else 0
            if want == n_chunks:
                break
            n_chunks = want
        if not compress:
            return self._summary(offsets, 0, []) + body
        method = 2 if compress == "lzo" else 1
        pieces, chunks = [], []
        pos = head_len
        for i in range(n_chunks):
            raw = body[i * chunk:(i + 1) * chunk]
            blocks = [raw[j:j + block] for j in range(0, len(raw), block)]
            packed = [lzo_literal(b) if method == 2 else zlib.compress(b) for b in blocks]
            data = struct.pack("<Iiii", TAG, block, sum(len(p) for p in packed), len(raw))
            data += b"".join(struct.pack("<ii", len(p), len(b)) for p, b in zip(packed, blocks)) + b"".join(packed)
            chunks.append((head_len + i * chunk, len(raw), pos, len(data)))
            pieces.append(data)
            pos += len(data)
        return self._summary(offsets, method, chunks) + b"".join(pieces)

    def _body(self, head_len: int) -> tuple[bytes, tuple]:
        names = b"".join(fstring(n) + struct.pack("<Q", 0x0007001000000000) for n in self.names)
        imports = b"".join(struct.pack("<ii", cp, 0) + struct.pack("<ii", cn, 0) + struct.pack("<i", outer)
                           + struct.pack("<ii", nm, 0) for cp, cn, outer, nm in self.imports)
        name_off = head_len
        imp_off = name_off + len(names)
        exp_entry = 4 * 3 + 8 + 4 + 8 + 4 + 4 + 4 + 4 + 16 + 4
        exp_off = imp_off + len(imports)
        dep_off = exp_off + exp_entry * len(self.exports)
        depends = struct.pack("<i", 0) * len(self.exports)
        data_off = dep_off + len(depends)
        table = b""
        datas = b""
        for e in self.exports:
            data = e["data"]
            if callable(data):
                data = data(data_off + len(datas))
            table += struct.pack("<iii", e["class"], 0, e["outer"]) + self.fname(e["name"])
            table += struct.pack("<i", 0) + struct.pack("<II", 0, 0x000F0004)
            table += struct.pack("<ii", len(data), data_off + len(datas))
            table += struct.pack("<I", e["eflags"]) + struct.pack("<i", 0) + b"\0" * 16 + struct.pack("<i", 0)
            datas += data
        assert len(table) == exp_entry * len(self.exports)
        # the name table was written before the export data added names: rebuild it if it grew
        names2 = b"".join(fstring(n) + struct.pack("<Q", 0x0007001000000000) for n in self.names)
        if len(names2) != len(names):
            return self._body(head_len)
        return names + imports + table + depends + datas, (name_off, imp_off, exp_off, dep_off, data_off)


# --------------------------------------------------------------------------- objects


def fully_compressed(data: bytes, compress: str = "lzo", block: int = 4096, per_chunk: int = 3) -> bytes:
    """A package compressed as a whole, as Borderlands 2 ships Core/Engine/Startup: a run of compressed chunks
    (header, block sizes, blocks) and nothing else."""
    blocks = [data[i:i + block] for i in range(0, len(data), block)]
    out = b""
    for i in range(0, len(blocks), per_chunk):
        group = blocks[i:i + per_chunk]
        packed = [lzo_literal(b) if compress == "lzo" else zlib.compress(b) for b in group]
        out += struct.pack("<Iiii", TAG, block, sum(map(len, packed)), sum(map(len, group)))
        out += b"".join(struct.pack("<ii", len(p), len(b)) for p, b in zip(packed, group)) + b"".join(packed)
    return out


def bulk_inline(data: bytes, flags: int = 0) -> callable:
    """A bulk data header with its data inline; the absolute offset is filled in when the export is placed."""
    def at(offset: int) -> bytes:
        return struct.pack("<Iiii", flags, len(data), len(data), offset + 16) + data
    return at


def bulk_array(elem: int, data: bytes) -> bytes:
    return struct.pack("<ii", elem, len(data) // elem) + data


def texture_data(b: PackageBuilder, width: int, height: int, mip0: bytes, fmt: str = "PF_DXT1",
                 cache: str | None = None, cache_offset: int = 0, normal: bool = False, extra_mips=()):
    """Texture2D export data. ``cache``: mip 0 lives in that .tfc at ``cache_offset`` (stored raw)."""
    items = [b.p_int("SizeX", width), b.p_int("SizeY", height), b.p_enum("Format", "EPixelFormat", fmt)]
    if normal:
        items.append(b.p_enum("CompressionSettings", "TextureCompressionSettings", "TC_Normalmap"))
    if cache:
        items.append(b.p_name("TextureFileCacheName", cache))
    props = b.props(*items)

    def at(offset: int) -> bytes:
        out = props + b"\0" * 16
        mips = [(width, height, mip0)] + list(extra_mips)
        out += struct.pack("<i", len(mips))
        for i, (w, h, data) in enumerate(mips):
            if i == 0 and cache:
                out += struct.pack("<Iiii", 0x01, len(data), len(data), cache_offset)
            else:
                out += struct.pack("<Iiii", 0, len(data), len(data), offset + len(out) + 16) + data
            out += struct.pack("<ii", w, h)
        out += b"\0" * 16 + b"\0" * 16  # Borderlands hash, texture cache guid
        return out
    return at


def bc1_solid(width: int, height: int, color565: int) -> bytes:
    block = struct.pack("<HHI", color565, color565, 0)
    return block * (max(1, width // 4) * max(1, height // 4))


def material_data(b: PackageBuilder, textures: list[int]) -> bytes:
    """A base Material: no properties of interest, then FMaterialResource with its referenced textures."""
    out = b.props()
    out += struct.pack("<i", 0)  # compile errors (strings)
    out += struct.pack("<i", 0)  # texture dependency map
    out += struct.pack("<i", 0) + b"\0" * 16 + struct.pack("<i", 0)
    out += struct.pack("<i", len(textures)) + b"".join(struct.pack("<i", t) for t in textures)
    return out


def mic_data(b: PackageBuilder, parent: int, params: list[tuple[str, int]], vectors=(), scalars=(),
             static_masks: dict | None = None) -> bytes:
    """A MaterialInstanceConstant: texture, vector (name, (r, g, b, a)) and scalar (name, value) parameters, and
    with ``static_masks`` (name -> channels "RGBA") its own static permutation after the properties."""
    guid = b.p_struct("ExpressionGUID", "Guid", b"\0" * 16)
    elems = b""
    for name, tex in params:
        elems += b.p_name("ParameterName", name) + b.p_obj("ParameterValue", tex) + guid + b.end()
    items = [b.p_obj("Parent", parent), b.p_array("TextureParameterValues", len(params), elems)]
    if vectors:
        raw = b"".join(b.p_name("ParameterName", n) + b.p_struct("ParameterValue", "LinearColor",
                                                                 struct.pack("<4f", *v)) + guid + b.end()
                       for n, v in vectors)
        items.append(b.p_array("VectorParameterValues", len(vectors), raw))
    if scalars:
        raw = b"".join(b.p_name("ParameterName", n) + b.p_float("ParameterValue", v) + guid + b.end()
                       for n, v in scalars)
        items.append(b.p_array("ScalarParameterValues", len(scalars), raw))
    out = b.props(*items)
    if static_masks is not None:  # the compiled resource (nothing in it), then the static parameter set
        out += struct.pack("<ii", 0, 0) + struct.pack("<i", 0) + b"\0" * 16 + struct.pack("<i", 0)
        out += struct.pack("<i", 0) + b"\0" * 24 + struct.pack("<i", 0) + struct.pack("<i", 0)
        out += b"\0" * 16 + struct.pack("<i", 0)  # base material id, no switches
        out += struct.pack("<i", len(static_masks))
        for name, channels in static_masks.items():
            out += b.fname(name) + struct.pack("<4i", *(c in channels for c in "RGBA")) + b"\0" * 20
        out += struct.pack("<ii", 0, 0)  # normal parameters, terrain layer weights
    return out


def pack_normal(n) -> int:
    n = np.asarray(n, np.float64)
    q = np.clip(np.round((n + 1) * 127.5), 0, 255).astype(int)
    return int(q[0]) | (int(q[1]) << 8) | (int(q[2]) << 16) | (255 << 24)


def skeletal_data(b: PackageBuilder, materials: list[int], positions, normals, uvs, indices, sections,
                  bones=(("Root", -1),), half_uvs: bool = True, extra_props=(), uv1=None, chunk_bones=(0,),
                  influences=None) -> bytes:
    """SkeletalMesh export data (Borderlands 2 layout, version 832). ``sections``: (material, first index,
    triangles). ``bones``: (name, parent) or (name, parent, rotation xyzw, position). One chunk, whose bone
    map is ``chunk_bones``; ``influences``: per vertex (chunk bone indices, weights 0..255), else every vertex
    weighted fully to the chunk's first bone."""
    positions = np.asarray(positions, np.float32)
    n = len(positions)
    out = b.props(*extra_props)
    out += b"\0" * 28
    out += struct.pack("<i", len(materials)) + b"".join(struct.pack("<i", m) for m in materials)
    out += b"\0" * 24
    out += struct.pack("<i", len(bones))
    for bone in bones:
        name, parent = bone[0], bone[1]
        rot = bone[2] if len(bone) > 2 else (0, 0, 0, 1)
        pos = bone[3] if len(bone) > 3 else (0, 0, 0)
        out += b.fname(name) + struct.pack("<I", 0) + struct.pack("<4f", *rot) + struct.pack("<3f", *pos)
        out += struct.pack("<ii", 0, max(parent, 0)) + struct.pack("<i", 0)
    out += struct.pack("<i", 1)  # skeletal depth
    out += struct.pack("<i", 1)  # one LOD
    out += struct.pack("<i", len(sections))
    for mat, first, tris in sections:
        out += struct.pack("<hhii", mat, 0, first, tris) + b"\0"
    idx = np.asarray(indices, np.uint16).tobytes()
    out += struct.pack("<i", 0) + b"\x02" + bulk_array(2, idx)
    bone_map = struct.pack("<i", len(chunk_bones)) + b"".join(struct.pack("<h", c) for c in chunk_bones)
    out += bone_map  # used bones
    out += struct.pack("<i", 1)  # chunks
    out += struct.pack("<i", 0) + struct.pack("<i", 0) + struct.pack("<i", 0)  # first vertex, rigid, soft
    out += bone_map  # the chunk's bones
    out += struct.pack("<iii", 0, n, 4)
    out += struct.pack("<ii", 0, n)  # f80, vertex count
    out += struct.pack("<i", 1) + b"\0"  # f24
    out += struct.pack("<Iiii", 0x20, 0, 0, 0)  # unused bulk
    sets = 1 if uv1 is None else 2
    out += struct.pack("<i", sets)  # UV sets
    out += struct.pack("<i", sets)  # GPU skin: UV sets
    out += struct.pack("<i", 0 if half_uvs else 1)
    out += struct.pack("<i", 0) + b"\0" * 24
    verts = b""
    for i in range(n):
        verts += struct.pack("<II", pack_normal((1, 0, 0)), pack_normal(normals[i]))
        joints, weights = influences[i] if influences is not None else ((0, 0, 0, 0), (255, 0, 0, 0))
        verts += bytes(joints) + bytes(weights)
        verts += struct.pack("<3f", *positions[i])
        for uv in (uvs,) if uv1 is None else (uvs, uv1):
            if half_uvs:
                verts += np.asarray(uv[i], np.float16).tobytes()
            else:
                verts += struct.pack("<2f", *uv[i])
    out += bulk_array(len(verts) // n, verts)
    out += struct.pack("<i", 0)  # extra influences
    out += struct.pack("<i", 0)  # rest of the mesh, ignored
    return out


def static_data(b: PackageBuilder, positions, normals, uvs, indices, sections) -> bytes:
    """StaticMesh export data (Borderlands 2 layout). ``sections``: (material ref, first index, triangles)."""
    positions = np.asarray(positions, np.float32)
    n = len(positions)
    out = b.props()
    out += b"\0" * 28 + struct.pack("<i", 0)  # bounds, body setup
    out += b"\0" * 24 + bulk_array(6, b"")  # kDOP bounds + nodes
    out += struct.pack("<ii", 8, 0)  # kDOP triangles
    out += struct.pack("<i", 18)  # internal version
    out += struct.pack("<i", 0)  # no extra LOD
    out += struct.pack("<i", 0)  # unk5 array
    out += struct.pack("<i", 0)  # f74
    out += struct.pack("<i", 1)  # LODs
    out += struct.pack("<Iiii", 0x20, 0, 0, 0)
    out += struct.pack("<i", len(sections))
    for mat, first, tris in sections:
        out += struct.pack("<iiii", mat, 0, 0, 1) + struct.pack("<iiii", first, tris, 0, n - 1)
        out += struct.pack("<i", 0) + struct.pack("<i", 0) + b"\0"
    out += struct.pack("<ii", 12, n) + bulk_array(12, positions.tobytes())
    items = b""
    for i in range(n):
        items += struct.pack("<II", pack_normal((1, 0, 0)), pack_normal(normals[i]))
        items += struct.pack("<2f", *uvs[i])
    out += struct.pack("<iiii", 1, 16, n, 1) + bulk_array(16, items)
    out += struct.pack("<ii", 4, 0)  # colour stream: none
    out += struct.pack("<i", n)
    out += bulk_array(2, np.asarray(indices, np.uint16).tobytes())
    out += struct.pack("<i", 0)
    return out


def gestalt_data(b: PackageBuilder, mesh_ref: int, fragments: list[tuple[str, int, int, int]]) -> bytes:
    parts = b""
    for name, mat, first, tris in fragments:
        parts += b.p_name("SkeletalMeshFragmentName", name) + b.p_int("MaterialIndex", mat) \
            + b.p_int("FirstIndex", first) + b.p_int("NumPrimitives", tris) + b.end()
    info = b.p_array("Parts", len(fragments), parts) + b.end()
    return b.props(b.p_obj("GestaltSkeletalMesh", mesh_ref), b.p_array("GestaltInfos", 1, info))


def part_data(b: PackageBuilder, fragment: str, extra: list[str] = ()) -> bytes:
    items = [b.p_name("GestaltModeSkeletalMeshName", fragment)]
    for i, name in enumerate(extra):
        items.append(b.p_name("AdditionalGestaltModeSkeletalMeshNames", name, index=i))
    return b.props(*items)


# --------------------------------------------------------------------------- animations

POLAR_SCALE = 1.5717963


def _polar(q, w_max: int, d2_max: int) -> tuple[int, int, int]:
    """(w part, packed angles, sign bits) of Borderlands 2's polar encoding of a rotation (x, y, z, w)."""
    q = np.asarray(q, np.float64)
    q = q / np.linalg.norm(q)
    if q[3] < 0:
        q = -q
    w = int(round(math.sqrt(max(0.0, 1.0 - q[3])) * w_max))
    v = np.abs(q[:3])
    d1 = 0
    if np.linalg.norm(v) > 1e-12:
        v = v / np.linalg.norm(v)
        d2 = int(round(math.acos(min(1.0, v[2])) / POLAR_SCALE * d2_max))
        d1 = d2 * d2 + (int(round(math.atan2(v[1], v[0]) / POLAR_SCALE * 2 * d2)) if d2 else 0)
    signs = (int(q[0] < 0) << 31) | (int(q[1] < 0) << 30) | (int(q[2] < 0) << 29)
    return w, d1, signs


def polar48(q) -> bytes:
    """Borderlands 2's ``FQuatPolarEncoded48``: w in a u16, then the angles and signs in a u32."""
    w, d1, signs = _polar(q, 65535, 16383)
    return struct.pack("<HI", w, d1 | signs)


def polar32(q) -> int:
    """Borderlands 2's ``FQuatPolarEncoded32``: w in bits 0-10, the angles in 11-28, signs above."""
    w, d1, signs = _polar(q, 2047, 511)
    return w | (d1 << 11) | signs


def _float96(q) -> bytes:
    q = np.asarray(q, np.float64)
    q = q / np.linalg.norm(q)
    return struct.pack("<3f", *(q[:3] if q[3] >= 0 else -q[:3]))


def _pad(data: bytes) -> bytes:
    return data + b"\x55" * ((-len(data)) % 4)


def uniform_keys(tracks, rotation: str = "ACF_Float96NoW") -> tuple[bytes, list[int]]:
    """A ``CompressedByteStream`` and ``CompressedTrackOffsets`` (4 per track) keyed at even spacing:
    ``tracks`` are (translation keys, rotation keys); translations as plain floats, one rotation key as
    Float96, more in ``rotation`` (Float96NoW or PolarEncoded48)."""
    stream, offsets = b"", []
    for trans, rots in tracks:
        t_off = len(stream)
        stream = _pad(stream + b"".join(struct.pack("<3f", *t) for t in trans))
        q_off = len(stream)
        if len(rots) == 1 or rotation == "ACF_Float96NoW":
            stream += b"".join(_float96(q) for q in rots)
        elif rotation == "ACF_PolarEncoded48":
            stream += b"".join(polar48(q) for q in rots)
        else:
            raise ValueError(rotation)
        stream = _pad(stream)
        offsets += [t_off, len(trans), q_off, len(rots)]
    return stream, offsets


def per_track_keys(tracks) -> tuple[bytes, list[int]]:
    """The same in Unreal 3's per-track layout (2 offsets per track; None leaves a part out): translations as
    Float96 (format 1), rotations polar-encoded in Borderlands 2's numbering (10)."""
    stream, offsets = b"", []
    for trans, rots in tracks:
        if trans is None:
            offsets.append(-1)
        else:
            offsets.append(len(stream))
            stream += struct.pack("<I", (1 << 28) | len(trans)) + b"".join(struct.pack("<3f", *t) for t in trans)
            stream = _pad(stream)
        if rots is None:
            offsets.append(-1)
        else:
            offsets.append(len(stream))
            stream += struct.pack("<I", (10 << 28) | len(rots)) + b"".join(polar48(q) for q in rots)
            stream = _pad(stream)
    return stream, offsets


def anim_set_data(b: PackageBuilder, tracks: list[str], sequences: list[int], rotation_only: bool = True,
                  use_translation=(), force_mesh_translation=()) -> bytes:
    items = [b.p_array("TrackBoneNames", len(tracks), b"".join(b.fname(t) for t in tracks)),
             b.p_array("Sequences", len(sequences), b"".join(struct.pack("<i", s) for s in sequences))]
    if not rotation_only:
        items.append(b.p_bool("bAnimRotationOnly", False))
    if use_translation:
        items.append(b.p_array("UseTranslationBoneNames", len(use_translation),
                               b"".join(b.fname(t) for t in use_translation)))
    if force_mesh_translation:
        items.append(b.p_array("ForceMeshTranslationBoneNames", len(force_mesh_translation),
                               b"".join(b.fname(t) for t in force_mesh_translation)))
    return b.props(*items)


def anim_sequence_data(b: PackageBuilder, name: str, frames: int, length: float, stream: bytes, offsets,
                       rotation: str = "ACF_Float96NoW", key: str = "AKF_ConstantKeyLerp",
                       additive: bool = False, translation: str = "ACF_None") -> bytes:
    items = [b.p_name("SequenceName", name), b.p_float("SequenceLength", length), b.p_int("NumFrames", frames),
             b.p_enum("TranslationCompressionFormat", "AnimationCompressionFormat", translation),
             b.p_enum("RotationCompressionFormat", "AnimationCompressionFormat", rotation),
             b.p_enum("KeyEncodingFormat", "AnimationKeyFormat", key),
             b.p_array("CompressedTrackOffsets", len(offsets), b"".join(struct.pack("<i", o) for o in offsets))]
    if additive:
        items.append(b.p_bool("bIsAdditive", True))
    out = b.props(*items)
    out += struct.pack("<i", 0)  # RawAnimData: none (cooked)
    return out + struct.pack("<i", len(stream)) + stream
