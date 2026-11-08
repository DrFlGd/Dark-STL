"""Byte-built Unreal Engine 4 packages and paks shaped like Borderlands 3's (4.20) and Wonderlands' (4.26).

:class:`Ue4Package` writes a ``.uasset`` (summary, names, imports, exports) and its ``.uexp`` and
``.ubulk``; helpers write tagged properties and the binary parts of skeletal and static meshes,
textures and material instances. :func:`build_pak` packs files into a ``.pak`` (version 8 with
the legacy index, or 11 with the directory index), optionally zlib-compressed and with an
AES-encrypted index.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

import numpy as np

TAG = 0x9E2A83C1


def fstring(s: str) -> bytes:
    if not s:
        return struct.pack("<i", 0)
    b = s.encode("latin-1") + b"\0"
    return struct.pack("<i", len(b)) + b


class Ue4Package:
    """``engine`` 420 (Borderlands 3, file version 516) or 426 (Wonderlands, 522)."""

    def __init__(self, name: str, engine: int = 426, custom: dict | None = None, unversioned: bool = False):
        self.pkg_name = name  # /Game/...
        self.engine = engine
        self.ue4 = 522 if engine >= 426 else 516
        # cooked unversioned (Returnal): no versions in the summary, a blank engine version, properties without
        # names (props() writes an empty unversioned header; write others with an unversioned header yourself)
        self.unversioned = unversioned
        self.custom = custom or {}
        self.names: list[str] = []
        self._ni: dict[str, int] = {}
        self.imports: list[tuple] = []
        self.exports: list[dict] = []
        self.ubulk = bytearray()
        self.name("None")
        self._script = self.add_import("/Script/CoreUObject", "Package", 0, "/Script/Engine")
        self._classes: dict[str, int] = {}

    def name(self, s: str) -> int:
        if s not in self._ni:
            self._ni[s] = len(self.names)
            self.names.append(s)
        return self._ni[s]

    def fname(self, s: str) -> bytes:
        return struct.pack("<ii", self.name(s), 0)

    def add_import(self, class_package: str, class_name: str, outer: int, name: str) -> int:
        for s in (class_package, class_name, name):
            self.name(s)
        self.imports.append((class_package, class_name, outer, name))
        return -len(self.imports)

    def class_ref(self, cls: str) -> int:
        if cls not in self._classes:
            self._classes[cls] = self.add_import("/Script/CoreUObject", "Class", self._script, cls)
        return self._classes[cls]

    def import_object(self, package: str, cls: str, name: str) -> int:
        """An object of another package (``/Game/...``)."""
        outer = self.add_import("/Script/CoreUObject", "Package", 0, package)
        return self.add_import("/Script/Engine", cls, outer, name)

    def add_export(self, cls: str, name: str, outer: int = 0, data: bytes = b"") -> int:
        self.name(name)
        self.exports.append({"class": self.class_ref(cls), "name": name, "outer": outer, "data": data})
        return len(self.exports)

    def set_data(self, ref: int, data: bytes) -> None:
        self.exports[ref - 1]["data"] = data

    def add_bulk(self, data: bytes) -> bytes:
        """A bulk header for ``data`` kept in the .ubulk."""
        off = len(self.ubulk)
        self.ubulk += data
        return struct.pack("<IiIq", 0x101, len(data), len(data), off)

    # -- properties
    def tag(self, name: str, kind: str, size: int, extra: bytes = b"") -> bytes:
        return self.fname(name) + self.fname(kind) + struct.pack("<ii", size, 0) + extra + b"\0"

    def p_int(self, name: str, v: int) -> bytes:
        return self.tag(name, "IntProperty", 4) + struct.pack("<i", v)

    def p_bool(self, name: str, v: bool) -> bytes:
        return self.fname(name) + self.fname("BoolProperty") + struct.pack("<ii", 0, 0) + bytes([int(v)]) + b"\0"

    def p_name(self, name: str, v: str) -> bytes:
        return self.tag(name, "NameProperty", 8) + self.fname(v)

    def p_obj(self, name: str, ref: int) -> bytes:
        return self.tag(name, "ObjectProperty", 4) + struct.pack("<i", ref)

    def p_enum(self, name: str, enum: str, v: str) -> bytes:
        return self.tag(name, "ByteProperty", 8, self.fname(enum)) + self.fname(v)

    def p_struct(self, name: str, struct_name: str, body: bytes) -> bytes:
        return self.tag(name, "StructProperty", len(body), self.fname(struct_name) + bytes(16)) + body

    def p_struct_array(self, name: str, struct_name: str, elements: list[bytes]) -> bytes:
        inner_size = sum(len(e) for e in elements)
        inner = self.fname(name) + self.fname("StructProperty") + struct.pack("<ii", inner_size, 0) \
            + self.fname(struct_name) + bytes(16) + b"\0"
        body = struct.pack("<i", len(elements)) + inner + b"".join(elements)
        return self.tag(name, "ArrayProperty", len(body), self.fname("StructProperty")) + body

    def p_obj_array(self, name: str, refs: list[int]) -> bytes:
        body = struct.pack("<i", len(refs)) + b"".join(struct.pack("<i", r) for r in refs)
        return self.tag(name, "ArrayProperty", len(body), self.fname("ObjectProperty")) + body

    def end(self) -> bytes:
        return self.fname("None")

    def props(self, *items: bytes) -> bytes:
        """Tagged properties, the list's end, and the object GUID flag UE4 writes after them."""
        if self.unversioned:
            return struct.pack("<H", 0x100) + struct.pack("<i", 0)
        return b"".join(items) + self.end() + struct.pack("<i", 0)

    # -- build
    def build(self) -> tuple[bytes, bytes, bytes]:
        uasset = self._uasset(0)
        size = len(uasset)
        uasset = self._uasset(size)
        assert len(uasset) == size
        return uasset, b"".join(e["data"] for e in self.exports) + struct.pack("<I", TAG), bytes(self.ubulk)

    def _uasset(self, header_size: int) -> bytes:
        v = self.ue4
        names = b"".join(fstring(n) + b"\0\0\0\0" for n in self.names)
        imports = b"".join(struct.pack("<ii", self.name(cp), 0) + struct.pack("<ii", self.name(cn), 0)
                           + struct.pack("<i", outer) + struct.pack("<ii", self.name(nm), 0)
                           for cp, cn, outer, nm in self.imports)
        exports = b""
        off = header_size
        for e in self.exports:
            exports += struct.pack("<iiii", e["class"], 0, 0, e["outer"]) + self.fname(e["name"])
            exports += struct.pack("<I", 0) + struct.pack("<qq", len(e["data"]), off)
            exports += struct.pack("<iii", 0, 0, 0) + bytes(16) + struct.pack("<I", 0)
            exports += struct.pack("<ii", 0, 0) + struct.pack("<5i", -1, 0, 0, 0, 0)
            off += len(e["data"])
        custom = struct.pack("<i", len(self.custom)) + b"".join(
            struct.pack("<4I", *g) + struct.pack("<i", n) for g, n in self.custom.items())

        if self.unversioned:
            v, custom = 0, struct.pack("<i", 0)

        def engine_version() -> bytes:
            if self.unversioned:
                return struct.pack("<HHHI", 0, 0, 0, 0) + fstring("")
            return struct.pack("<HHHI", self.engine // 100, self.engine % 100, 0, 0) + fstring("")

        def head(name_off, imp_off, exp_off) -> bytes:
            h = struct.pack("<Iiiii", TAG, -7, 864, v, 0) + custom
            flags = 0x80000000 | (0x2000 if self.unversioned else 0)
            h += struct.pack("<i", header_size) + fstring("None") + struct.pack("<I", flags)
            h += struct.pack("<ii", len(self.names), name_off)
            h += struct.pack("<ii", 0, 0)  # gatherable text
            h += struct.pack("<ii", len(self.exports), exp_off) + struct.pack("<ii", len(self.imports), imp_off)
            h += struct.pack("<i", 0) + struct.pack("<ii", 0, 0) + struct.pack("<i", 0) + struct.pack("<i", 0)
            h += bytes(16) + struct.pack("<i", 1) + struct.pack("<ii", len(self.exports), len(self.names))
            h += engine_version() + engine_version()
            h += struct.pack("<Ii", 0, 0) + struct.pack("<I", 0) + struct.pack("<i", 0)
            h += struct.pack("<i", 0) + struct.pack("<q", self._bulk_start(header_size))
            h += struct.pack("<i", 0) + struct.pack("<i", 0) + struct.pack("<ii", 0, 0)
            return h

        h = head(0, 0, 0)
        name_off = len(h)
        imp_off = name_off + len(names)
        exp_off = imp_off + len(imports)
        return head(name_off, imp_off, exp_off) + names + imports + exports

    def _bulk_start(self, header_size: int) -> int:
        return header_size + sum(len(e["data"]) for e in self.exports) if header_size else 0


# -- binary parts -----------------------------------------------------------------

def bulk_array(elem: int, data: bytes) -> bytes:
    return struct.pack("<ii", elem, len(data) // elem if elem else 0) + data


def pack_normal(n, increased: bool = True) -> int:
    b = [int(round((float(x) + 1) * 127.5)) & 0xFF for x in (*n, 1.0)]
    v = b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24
    return v ^ 0x80808080 if increased else v


def _positions(positions) -> bytes:
    p = np.asarray(positions, "<f4")
    return struct.pack("<ii", 12, len(p)) + bulk_array(12, p.tobytes())


def _vertex_buffer(engine: int, normals, uvs) -> bytes:
    n = len(normals)
    out = b"\0\0" + struct.pack("<i", 1)
    if engine < 419:
        out += struct.pack("<i", 16)
    out += struct.pack("<iii", n, 1, 0)
    tangents = b"".join(struct.pack("<II", pack_normal((1, 0, 0)), pack_normal(nm)) for nm in normals)
    out += bulk_array(8, tangents)
    out += bulk_array(8, np.asarray(uvs, "<f4").tobytes())
    return out


def _skel_section(engine: int, material: int, first: int, tris: int, bone_map=(0,), base: int = 0,
                  count: int = 0) -> bytes:
    out = b"\0\0" + struct.pack("<hii", material, first, tris) + struct.pack("<i", 0)
    if engine >= 426:
        out += b"\0"  # recompute tangents vertex mask
    out += struct.pack("<i", 1)  # cast shadow
    out += struct.pack("<I", base) + struct.pack("<i", 0)  # base vertex, cloth mapping
    out += struct.pack("<i", len(bone_map)) + struct.pack(f"<{len(bone_map)}H", *bone_map)  # bone map
    out += struct.pack("<iih", count, 4, -1) + bytes(20)
    out += struct.pack("<ii", 0, 0)  # duplicated vertices
    out += struct.pack("<i", 0)  # disabled
    return out


def _skeletal_material(engine: int, ref: int, slot: bytes) -> bytes:
    out = struct.pack("<i", ref) + slot
    if engine >= 426:
        out += struct.pack("<i", 0)  # no imported slot name
    return out + bytes(24)


def _bounds(positions) -> bytes:
    """FBoxSphereBounds: origin, box extent, sphere radius."""
    p = np.asarray(positions, float)
    lo, hi = p.min(0), p.max(0)
    origin, extent = (lo + hi) / 2, (hi - lo) / 2
    radius = float(np.linalg.norm(p - origin, axis=1).max())
    return struct.pack("<7f", *origin, *extent, radius)


def reference_skeleton(b: Ue4Package, bones) -> bytes:
    """``FReferenceSkeleton``: ``bones`` as (name, parent, (qx, qy, qz, qw), (tx, ty, tz)), unit scale."""
    out = struct.pack("<i", len(bones)) + b"".join(b.fname(n) + struct.pack("<i", p) for n, p, _q, _t in bones)
    out += struct.pack("<i", len(bones)) + b"".join(struct.pack("<10f", *q, *t, 1, 1, 1) for _n, _p, q, t in bones)
    out += struct.pack("<i", len(bones)) + b"".join(b.fname(n) + struct.pack("<i", i)
                                                    for i, (n, _p, _q, _t) in enumerate(bones))
    return out


ROOT_ONLY = [("root", -1, (0, 0, 0, 0), (0, 0, 0))]


def skin_weights(engine: int, joints, weights) -> bytes:
    """``FSkinWeightVertexBuffer`` for (V, 4) section bone indices and 0-255 weights."""
    j = np.asarray(joints, np.uint8).reshape(-1, 4)
    w = np.asarray(weights, np.uint8).reshape(-1, 4)
    rows = np.concatenate([j, w], axis=1).tobytes()
    n = len(j)
    if engine < 424:
        return b"\0\0" + struct.pack("<iI", 0, n) + bulk_array(8, rows)
    if engine < 425:
        return b"\0\0" + struct.pack("<iII", 0, 8, n) + bulk_array(8, rows)
    out = b"\0\0" + struct.pack("<iIII", 0, 4, 4 * n, n) + struct.pack("<i", 0)  # 8-bit bone indices
    out += bulk_array(1, rows)
    lookup = np.array([(i * 8) << 8 | 4 for i in range(n)], "<u4").tobytes()
    return out + b"\0\0" + struct.pack("<i", n) + bulk_array(4, lookup)


def skeletal_data(b: Ue4Package, materials: list[int], positions, normals, uvs, indices, sections,
                  in_bulk: bool = False, cooked_out_first: bool = False, bones=None, joints=None, weights=None,
                  skeleton: int = 0, colors=None, slots=None) -> bytes:
    """``sections``: (material, first index, triangles[, bone map, base vertex, vertices]). ``bones``: see
    :func:`reference_skeleton`; ``joints``/``weights`` (V, 4) write skin weights; ``skeleton`` the Skeleton
    the mesh names; ``colors`` (V, 4) R, G, B, A 0-255 vertex colours (after the skin weights); ``slots`` the
    material slots' names."""
    e = b.engine
    items = [b.p_bool("bHasVertexColors", colors is not None)]
    if skeleton:
        items.append(b.p_obj("Skeleton", skeleton))
    out = b.props(*items)
    out += bytes([1, 0]) + _bounds(positions)
    out += struct.pack("<i", len(materials))
    for i, m in enumerate(materials):
        out += _skeletal_material(e, m, b.fname(slots[i] if slots else f"Slot{i}"))
    out += reference_skeleton(b, bones or ROOT_ONLY)
    out += struct.pack("<i", 1)  # cooked
    idx = np.asarray(indices, "<u2").tobytes()
    secs = struct.pack("<i", len(sections)) + b"".join(_skel_section(e, *s) for s in sections)
    skin = skin_weights(e, joints, weights) if joints is not None else b""
    if colors is not None:
        rgba = np.asarray(colors, np.uint8).reshape(-1, 4)
        skin += b"\0\0" + struct.pack("<ii", 4, len(rgba)) + bulk_array(4, rgba[:, [2, 1, 0, 3]].tobytes())
    lods = []
    if e < 424:
        lod = b"\0\0" + secs + b"\x02" + bulk_array(2, idx) + struct.pack("<ii", 0, 0)
        lod += _positions(positions) + _vertex_buffer(e, normals, uvs) + skin
        lods.append(lod)
    else:
        if cooked_out_first:
            lods.append(b"\0\0" + struct.pack("<ii", 1, 1) + struct.pack("<i", 0))
        streamed = b"\0\0" + b"\x02" + bulk_array(2, idx) + _positions(positions) + _vertex_buffer(e, normals, uvs)
        streamed += skin
        lod = b"\0\0" + struct.pack("<ii", 0, 0 if in_bulk else 1) + struct.pack("<i", 0) + secs
        lod += struct.pack("<i", 0) + struct.pack("<I", 0)
        lod += b.add_bulk(streamed) if in_bulk else streamed
        lods.append(lod)
    return out + struct.pack("<i", len(lods)) + b"".join(lods)


def static_data(b: Ue4Package, materials: list[int], positions, normals, uvs, indices, sections,
                bl3: bool = False) -> bytes:
    e = b.engine
    mats = [b.p_obj("MaterialInterface", m) + b.p_name("MaterialSlotName", f"Slot{i}") + b.end()
            for i, m in enumerate(materials)]
    out = b.props(b.p_struct_array("StaticMaterials", "StaticMaterial", mats))
    out += bytes([1, 0]) + struct.pack("<iii", 1, 0, 0) + bytes(16) + struct.pack("<i", 0)
    secs = b""
    for m, first, tris in sections:
        secs += struct.pack("<iiiiiii", m, first, tris, 0, len(positions) - 1, 1, 1)
        if e >= 426:
            secs += struct.pack("<ii", 0, 1)  # force opaque, visible in ray tracing
    secs = struct.pack("<i", len(sections)) + secs
    idx = np.asarray(indices, "<u2").tobytes()
    index_buffer = struct.pack("<i", 0) + bulk_array(1, idx) + (struct.pack("<i", 0) if e >= 425 else b"")
    color = b"\0\0" + struct.pack("<ii", 4, 0)
    lod = b"\0\0" + secs + struct.pack("<f", 0)
    if e < 423:
        lod += _positions(positions) + _vertex_buffer(e, normals, uvs)
        lod += struct.pack("<i", 0) if bl3 else color
        lod += index_buffer
    else:
        lod += struct.pack("<ii", 0, 1) + b"\0\0" + _positions(positions) + _vertex_buffer(e, normals, uvs)
        lod += color + index_buffer
    return out + struct.pack("<i", 1) + lod


def texture_data(b: Ue4Package, width: int, height: int, mip0: bytes, fmt: str = "PF_DXT1", normal: bool = False,
                 in_bulk: bool = True, bl3: bool = False) -> bytes:
    props = [b.p_enum("CompressionSettings", "TextureCompressionSettings", "TC_Normalmap")] if normal else []
    out = b.props(*props) + bytes([1, 0, 1, 0]) + struct.pack("<i", 1)
    out += b.fname(fmt) + struct.pack("<q", 0)
    out += struct.pack("<iiI", width, height, 1) + fstring(fmt) + struct.pack("<i", 0)
    out += struct.pack("<i", 1) + struct.pack("<i", 1)
    if in_bulk:
        out += b.add_bulk(mip0)
    else:
        out += struct.pack("<IiIq", 0x40, len(mip0), len(mip0), 0) + mip0
    out += struct.pack("<HHH", width, height, 1) if bl3 else struct.pack("<iii", width, height, 1)
    if b.engine >= 423:
        out += struct.pack("<i", 0)
    return out + b.fname("None")


def mic_data(b: Ue4Package, parent: int, params: list[tuple[str, int]], vectors=(), switches=()) -> bytes:
    """``params``: texture parameters (name, texture); ``vectors``: (name, (r, g, b, a)); ``switches``: static
    switches (name, value)."""
    def info(name: str) -> bytes:
        return b.p_struct("ParameterInfo", "MaterialParameterInfo", b.p_name("Name", name) + b.end())

    elems = [info(name) + b.p_obj("ParameterValue", tex) + b.end() for name, tex in params]
    items = [b.p_struct_array("TextureParameterValues", "TextureParameterValue", elems)]
    if vectors:
        items.append(b.p_struct_array("VectorParameterValues", "VectorParameterValue", [
            info(name) + b.p_struct("ParameterValue", "LinearColor", struct.pack("<4f", *rgba)) + b.end()
            for name, rgba in vectors]))
    if switches:
        found = b.p_struct_array("StaticSwitchParameters", "StaticSwitchParameter", [
            info(name) + b.p_bool("Value", value) + b.p_bool("bOverride", True) + b.end()
            for name, value in switches])
        items.append(b.p_struct("StaticParameters", "StaticParameterSet", found + b.end()))
    if parent:
        items.insert(0, b.p_obj("Parent", parent))
    return b.props(*items)


def bc1_solid(width: int, height: int, color565: int) -> bytes:
    blocks = max(1, width // 4) * max(1, height // 4)
    return struct.pack("<HHI", color565, color565, 0) * blocks


# -- paks -------------------------------------------------------------------------

_U8 = False  # write version 8 the way Unreal 4.22 did (one-byte method, four method names)


def _record(version: int, offset: int, size: int, usize: int, method: int, blocks: list[tuple[int, int]],
            encrypted: bool, block_size: int) -> bytes:
    out = struct.pack("<qqqB" if _U8 else "<qqqI", offset, size, usize, method) + bytes(20)
    if method:
        out += struct.pack("<i", len(blocks)) + b"".join(struct.pack("<qq", a, b) for a, b in blocks)
    return out + bytes([int(encrypted)]) + struct.pack("<I", block_size)


def _aes(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = data + bytes(-len(data) % 16)
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def build_pak(files: dict[str, bytes], version: int = 8, mount: str = "../../../", compress: bool = False,
              key: bytes | None = None, block_size: int = 0x10000, four_methods: bool = False) -> bytes:
    global _U8
    _U8 = four_methods and version == 8
    try:
        return _build_pak(files, version, mount, compress, key, block_size)
    finally:
        _U8 = False


def _build_pak(files, version, mount, compress, key, block_size) -> bytes:
    body = bytearray()
    records: dict[str, tuple] = {}
    for name, data in files.items():
        offset = len(body)
        method = 1 if compress else 0
        if compress:
            chunks = [zlib.compress(data[i:i + block_size]) for i in range(0, max(1, len(data)), block_size)]
            head_len = len(_record(version, 0, 0, 0, 1, [(0, 0)] * len(chunks), False, 0))
            blocks, pos = [], head_len
            for c in chunks:
                blocks.append((pos, pos + len(c)))
                pos += len(c)
            payload = b"".join(chunks)
            rec = (offset, len(payload), len(data), method, blocks, False, block_size)
        else:
            payload = data
            rec = (offset, len(data), len(data), 0, [], False, 0)
        body += _record(version, 0, *rec[1:]) + payload
        records[name] = rec
    if version < 10:
        index = fstring(mount) + struct.pack("<i", len(records))
        for name, rec in records.items():
            index += fstring(name) + _record(version, *rec)
        full = b""
    else:
        encoded = bytearray()
        locs = {}
        for name, (offset, size, usize, method, blocks, enc, bs) in records.items():
            locs[name] = len(encoded)
            v = (len(blocks) & 0xFFFF) << 6 | (method & 0x3F) << 23 | 1 << 29 | 1 << 30 | 1 << 31
            v |= 0x3F if bs and (bs >> 11) << 11 != bs else (bs >> 11) & 0x3F
            encoded += struct.pack("<I", v)
            if (v & 0x3F) == 0x3F:
                encoded += struct.pack("<I", bs)
            encoded += struct.pack("<II", offset, usize)
            if method:
                encoded += struct.pack("<I", size)
            if blocks and not (len(blocks) == 1 and not enc):
                for a, b in blocks:
                    encoded += struct.pack("<I", b - a)
        dirs: dict[str, list[str]] = {}
        for name in records:
            d, _, f = name.rpartition("/")
            dirs.setdefault(d + "/" if d else "/", []).append(f)
        full = struct.pack("<i", len(dirs))
        for d, fs in dirs.items():
            full += fstring(d) + struct.pack("<i", len(fs))
            for f in fs:
                full += fstring(f) + struct.pack("<i", locs[(d if d != "/" else "") + f])
        index_head_len = None
        index = b""
        for _ in range(2):
            full_off = len(body) + (index_head_len or 0)
            stored_full = _aes(key, full) if key else full
            index = fstring(mount) + struct.pack("<i", len(records)) + struct.pack("<Q", 0) + struct.pack("<i", 0)
            index += struct.pack("<i", 1) + struct.pack("<qq", full_off, len(stored_full)) + bytes(20)
            index += struct.pack("<i", len(encoded)) + bytes(encoded) + struct.pack("<i", 0)
            index_head_len = len(_aes(key, index)) if key else len(index)
        full = stored_full
    stored = _aes(key, index) if key else index
    index_off = len(body)
    out = bytes(body) + stored + full
    footer = bytes(16) + bytes([int(key is not None)]) + struct.pack("<Ii", 0x5A6F12E1, version)
    footer += struct.pack("<qq", index_off, len(stored)) + hashlib.sha1(stored).digest()
    if version == 9:
        footer += b"\0"
    names = [b"Zlib"] if compress else []
    slots = 4 if _U8 else 5
    footer += b"".join(n.ljust(32, b"\0") for n in names) + bytes(32 * (slots - len(names)))
    return out + footer


__all__ = ["Ue4Package", "bc1_solid", "build_pak", "fstring", "mic_data", "skeletal_data", "static_data",
           "texture_data"]
