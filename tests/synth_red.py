"""Builders for synthetic REDengine (Cyberpunk 2077) files used by the tests.

They write CR2W files (meshes, textures, material instances) and .archive
packs following the layouts the readers expect, with buffers stored raw (the
tests have no Oodle library to compress with).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from darkstl.importers.redengine.formats.archive import fnv1a64, normalize_path

# ---------------------------------------------------------------------------
# CR2W writer


@dataclass
class Prop:
    name: str
    type: str
    value: Any


@dataclass
class Obj:
    """A class instance: properties in order, each a (name, type, value) triple."""

    cls: str
    props: list[Prop] = field(default_factory=list)
    values: list[Prop] | None = None  # CMaterialInstance parameters, written after the properties

    def add(self, name: str, type_name: str, value: Any) -> Obj:
        self.props.append(Prop(name, type_name, value))
        return self


Ref = str  # a resource reference: its depot path
HANDLE = "handle"


class Cr2wWriter:
    def __init__(self, version: int = 195):
        self.version = version
        self.strings: dict[str, int] = {}
        self.string_block = bytearray(b"\0")  # offset 0 is the empty string ("None")
        self.names: list[str] = [""]
        self.imports: list[tuple[str, str]] = []  # (depot path, class)
        self.chunks: list[Obj] = []
        self.buffers: list[bytes] = []

    def _string(self, s: str) -> int:
        if s not in self.strings:
            self.strings[s] = len(self.string_block)
            self.string_block += s.encode("utf-8") + b"\0"
        return self.strings[s]

    def name(self, s: str) -> int:
        if s not in self.names:
            self.names.append(s)
        return self.names.index(s)

    def import_index(self, path: str, cls: str = "ITexture") -> int:
        key = (path, cls)
        if key not in self.imports:
            self.imports.append(key)
        return self.imports.index(key) + 1

    def add_chunk(self, obj: Obj) -> int:
        self.chunks.append(obj)
        return len(self.chunks)  # handles are 1-based

    def add_buffer(self, data: bytes) -> int:
        self.buffers.append(data)
        return len(self.buffers)  # 1-based pointers

    # -- values --

    def _value(self, type_name: str, value: Any) -> bytes:
        if type_name.startswith("["):
            _n, rest = type_name[1:].split("]", 1)
            return struct.pack("<i", len(value)) + b"".join(self._value(rest, v) for v in value)
        head, _, rest = type_name.partition(":")
        if head == "array":
            return struct.pack("<i", len(value)) + b"".join(self._value(rest, v) for v in value)
        if head == "static":
            _n, _, inner = rest.partition(",")
            return struct.pack("<i", len(value)) + b"".join(self._value(inner, v) for v in value)
        if head in ("handle", "whandle"):
            return struct.pack("<i", int(value))  # 1-based chunk index, 0 = none
        if head in ("rRef", "raRef"):
            return struct.pack("<H", self.import_index(value, rest) if value else 0)
        fmt = {"Bool": "B", "Int8": "b", "Uint8": "B", "Int16": "h", "Uint16": "H", "Int32": "i", "Uint32": "I",
               "Int64": "q", "Uint64": "Q", "Float": "f", "Double": "d"}.get(type_name)
        if fmt:
            return struct.pack("<" + fmt, value)
        if type_name == "CName":
            return struct.pack("<H", self.name(value))
        if type_name == "String":
            raw = value.encode("utf-8")
            assert len(raw) < 64
            return bytes([0x80 | len(raw)]) + raw
        if type_name == "DataBuffer":
            if isinstance(value, bytes):
                return struct.pack("<I", len(value)) + value
            return struct.pack("<I", 0x80000000 | int(value))  # pointer to buffer N (1-based)
        if type_name == "serializationDeferredDataBuffer":
            return struct.pack("<H", int(value))
        if type_name.startswith("enum:"):
            return struct.pack("<H", self.name(value))
        if isinstance(value, Obj):
            return self._class(value)
        raise ValueError(f"synth_red cannot write {type_name}")

    def _class(self, obj: Obj) -> bytes:
        out = bytearray(b"\0")
        for p in obj.props:
            type_name = p.type.removeprefix("enum:")
            data = self._value(p.type, p.value)
            out += struct.pack("<HHI", self.name(p.name), self.name(type_name), len(data) + 4) + data
        out += b"\0\0"
        return bytes(out)

    def _material_values(self, values: list[Prop]) -> bytes:
        out = bytearray(struct.pack("<i", len(values)))
        for p in values:
            type_name = p.type.removeprefix("enum:")
            data = self._value(p.type, p.value)
            out += struct.pack("<iHH", len(data) + 8, self.name(p.name), self.name(type_name)) + data
        return bytes(out)

    def build(self) -> bytes:
        for obj in self.chunks:
            self.name(obj.cls)
        chunk_blobs = []
        for obj in self.chunks:
            blob = self._class(obj)
            if obj.values is not None:
                blob += self._material_values(obj.values)
            chunk_blobs.append(blob)
        for path, cls in self.imports:
            self._string(path)
            self.name(cls)
        for n in self.names[1:]:
            self._string(n)
        header_size = 4 + 36 + 10 * 12
        pos = header_size
        tables = []
        strings_off = pos
        pos += len(self.string_block)
        names_off = pos
        pos += 8 * len(self.names)
        imports_off = pos
        pos += 8 * len(self.imports)
        props_off = pos
        pos += 8  # one empty property entry, as the game writes
        exports_off = pos
        pos += 24 * len(self.chunks)
        buffers_off = pos
        pos += 24 * len(self.buffers)
        embedded_off = pos
        data_start = pos
        chunk_offsets = []
        for blob in chunk_blobs:
            chunk_offsets.append(pos)
            pos += len(blob)
        objects_end = pos
        buffer_offsets = []
        for b in self.buffers:
            buffer_offsets.append(pos)
            pos += len(b)
        buffers_end = pos
        tables = [(strings_off, len(self.string_block)), (names_off, len(self.names)), (imports_off, len(self.imports)),
                  (props_off, 1), (exports_off, len(self.chunks)), (buffers_off, len(self.buffers)),
                  (embedded_off, 0), (0, 0), (0, 0), (0, 0)]
        out = bytearray(b"CR2W")
        out += struct.pack("<IIQIIIII", self.version, 0, 0, 0, objects_end, buffers_end, 0, len(self.chunks))
        for off, count in tables:
            out += struct.pack("<III", off, count, 0)
        assert len(out) == header_size
        out += self.string_block
        for n in self.names:
            out += struct.pack("<II", self.strings.get(n, 0), 0)
        for path, cls in self.imports:
            out += struct.pack("<IHH", self.strings[path], self.name(cls), 0)
        out += b"\0" * 8
        for obj, off, blob in zip(self.chunks, chunk_offsets, chunk_blobs):
            out += struct.pack("<HHIIIII", self.name(obj.cls), 0, 0, len(blob), off, 0, 0)
        for i, (b, off) in enumerate(zip(self.buffers, buffer_offsets)):
            out += struct.pack("<6I", 0, i, off, len(b), len(b), 0)
        assert len(out) == data_start
        for blob in chunk_blobs:
            out += blob
        for b in self.buffers:
            out += b
        return bytes(out)


# ---------------------------------------------------------------------------
# Meshes

PACKING_BYTES = {"PT_Short4N": 8, "PT_UByte4": 4, "PT_UByte4N": 4, "PT_Dec4": 4, "PT_Float16_2": 4}


def vector4(x, y, z, w) -> Obj:
    return Obj("Vector4").add("X", "Float", x).add("Y", "Float", y).add("Z", "Float", z).add("W", "Float", w)


def element(type_name: str, usage: str, usage_index: int = 0, stream: int = 0) -> Obj:
    e = Obj("GpuWrapApiVertexPackingPackingElement")
    e.add("type", "enum:GpuWrapApiVertexPackingePackingType", type_name)
    e.add("usage", "enum:GpuWrapApiVertexPackingePackingUsage", usage)
    if usage_index:
        e.add("usageIndex", "Uint8", usage_index)
    if stream:
        e.add("streamIndex", "Uint8", stream)
    return e


def pack_dec4(n: np.ndarray) -> np.ndarray:
    q = np.clip(np.rint((n + 1.0) * 1023.0 / 2.0), 0, 1023).astype(np.uint32)
    return q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20)


@dataclass
class SynthChunk:
    positions: np.ndarray  # (n, 3) metres
    normals: np.ndarray
    uvs: np.ndarray
    indices: np.ndarray
    lod_mask: int = 1
    skinned: bool = False


def grid_plane(n: int = 4, size: float = 1.0):
    """A size x size square in the XY plane (Z up), n x n quads, normals +Z, UVs over the square."""
    xs = np.linspace(0, size, n + 1)
    gx, gy = np.meshgrid(xs, xs, indexing="xy")
    pos = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1).astype(np.float32)
    nrm = np.tile(np.array([[0, 0, 1]], np.float32), (len(pos), 1))
    uv = np.stack([gx.ravel() / size, gy.ravel() / size], axis=1).astype(np.float32)
    idx = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b, c, d = a + 1, a + n + 1, a + n + 2
            idx += [a, b, d, a, d, c]
    return pos, nrm, uv, np.array(idx, np.uint16)


def build_mesh(chunks: list[SynthChunk], materials: list[tuple[str, Obj | str]],
               appearances: list[tuple[str, list[str]]] | None = None, version: int = 195,
               local_as_buffer: bool = True) -> bytes:
    """A .mesh: ``materials`` are (name, CMaterialInstance Obj) for local ones or (name, depot path) for external.
    ``appearances`` map each chunk to a material name; default: one appearance using the materials in order."""
    w = Cr2wWriter(version)
    all_pos = np.concatenate([c.positions for c in chunks])
    lo, hi = all_pos.min(0), all_pos.max(0)
    scale = np.maximum((hi - lo) / 2.0, 1e-6)
    offset = (hi + lo) / 2.0
    vertex_data = bytearray()
    index_data = bytearray()
    infos = []
    for c in chunks:
        n = len(c.positions)
        stride0 = 8 + (8 if c.skinned else 0)
        pos_off = len(vertex_data)
        q = np.rint((c.positions - offset) / scale * 32767).astype(np.int16)
        for i in range(n):
            vertex_data += struct.pack("<4h", q[i, 0], q[i, 1], q[i, 2], 32767)
            if c.skinned:
                vertex_data += bytes([0, 0, 0, 0, 255, 0, 0, 0])
        nrm_off = len(vertex_data)
        packed = pack_dec4(c.normals)
        for i in range(n):
            vertex_data += struct.pack("<II", int(packed[i]), int(packed[i]))  # normal, tangent
        uv_off = len(vertex_data)
        vertex_data += c.uvs.astype("<f2").tobytes()
        idx_off = len(index_data)
        index_data += c.indices.astype("<u2").tobytes()
        elements = [element("PT_Short4N", "PS_Position")]
        if c.skinned:
            elements += [element("PT_UByte4", "PS_SkinIndices"), element("PT_UByte4N", "PS_SkinWeights")]
        elements += [element("PT_Dec4", "PS_Normal", 0, 1), element("PT_Dec4", "PS_Tangent", 0, 1),
                     element("PT_Float16_2", "PS_TexCoord", 0, 2)]
        layout = Obj("GpuWrapApiVertexLayoutDesc")
        layout.add("elements", "static:32,GpuWrapApiVertexPackingPackingElement", elements)
        layout.add("slotStrides", "static:8,Uint8", [stride0, 8, 4, 0, 0, 0, 0, 0])
        cv = Obj("rendVertexBufferChunk").add("vertexLayout", "GpuWrapApiVertexLayoutDesc", layout)
        cv.add("byteOffsets", "static:5,Uint32", [pos_off, nrm_off, uv_off, 0, 0])
        ci = Obj("rendIndexBufferChunk").add("pe", "enum:GpuWrapApieIndexBufferChunkType", "IBCT_IndexUShort")
        ci.add("teOffset", "Uint32", idx_off)
        info = Obj("rendChunk").add("chunkVertices", "rendVertexBufferChunk", cv)
        info.add("chunkIndices", "rendIndexBufferChunk", ci)
        info.add("numVertices", "Uint16", n).add("numIndices", "Uint32", len(c.indices))
        info.add("lodMask", "Uint8", c.lod_mask)
        infos.append(info)
    while len(vertex_data) % 16:
        vertex_data += b"\0"
    render_buffer = bytes(vertex_data) + bytes(index_data)
    header = Obj("rendRenderMeshBlobHeader").add("version", "Uint32", 7)
    header.add("renderChunkInfos", "array:rendChunk", infos)
    header.add("quantizationScale", "Vector4", vector4(*scale, 0.0))
    header.add("quantizationOffset", "Vector4", vector4(*offset, 1.0))
    header.add("vertexBufferSize", "Uint32", len(vertex_data))
    header.add("indexBufferSize", "Uint32", len(index_data))
    header.add("indexBufferOffset", "Uint32", len(vertex_data))
    buffer_index = w.add_buffer(render_buffer)
    blob = Obj("rendRenderMeshBlob").add("header", "rendRenderMeshBlobHeader", header)
    blob.add("renderBuffer", "DataBuffer", buffer_index)

    root = Obj("CMesh")
    entries = []
    local_instances: list[Obj] = []
    external: list[str] = []
    for name, mat in materials:
        e = Obj("CMeshMaterialEntry").add("name", "CName", name)
        if isinstance(mat, Obj):
            e.add("index", "Uint16", len(local_instances)).add("isLocalInstance", "Bool", True)
            local_instances.append(mat)
        else:
            e.add("index", "Uint16", len(external))
            external.append(mat)
        entries.append(e)
    root.add("materialEntries", "array:CMeshMaterialEntry", entries)
    if external:
        root.add("externalMaterials", "array:raRef:IMaterial", external)
    # chunks come after the root so handles can point forward; reserve their indices first
    appearances = appearances or [("default", [materials[i % len(materials)][0] for i in range(len(chunks))])]
    app_handles = []
    root_index = w.add_chunk(root)
    assert root_index == 1
    for app_name, chunk_mats in appearances:
        a = Obj("meshMeshAppearance").add("name", "CName", app_name)
        a.add("chunkMaterials", "array:CName", chunk_mats)
        app_handles.append(w.add_chunk(a))
    root.add("appearances", "array:handle:meshMeshAppearance", app_handles)
    blob_index = w.add_chunk(blob)
    root.add("renderResourceBlob", "handle:IRenderResourceBlob", blob_index)
    if local_instances:
        if local_as_buffer:
            raw = bytearray()
            headers = []
            for inst in local_instances:
                mw = Cr2wWriter(version)
                mw.add_chunk(inst)
                data = mw.build()
                headers.append(Obj("meshLocalMaterialHeader").add("offset", "Uint32", len(raw))
                               .add("size", "Uint32", len(data)))
                raw += data
            buf = Obj("meshMeshMaterialBuffer").add("rawData", "DataBuffer", w.add_buffer(bytes(raw)))
            buf.add("rawDataHeaders", "array:meshLocalMaterialHeader", headers)
            root.add("localMaterialBuffer", "meshMeshMaterialBuffer", buf)
        else:
            handles = [w.add_chunk(inst) for inst in local_instances]
            root.add("preloadLocalMaterialInstances", "array:handle:IMaterial", handles)
    root.add("boneNames", "array:CName", [])
    return w.build()


def material_instance(base: str, textures: dict[str, str], extra: list[Prop] | None = None) -> Obj:
    """A CMaterialInstance over ``base`` (a depot path) with texture parameters by name."""
    inst = Obj("CMaterialInstance").add("baseMaterial", "rRef:IMaterial", base)
    inst.values = [Prop(k, "rRef:ITexture", v) for k, v in textures.items()] + list(extra or [])
    return inst


def build_material_instance(base: str, textures: dict[str, str], extra: list[Prop] | None = None) -> bytes:
    """A loose .mi file."""
    w = Cr2wWriter()
    w.add_chunk(material_instance(base, textures, extra))
    return w.build()


def build_material_template(name: str) -> bytes:
    w = Cr2wWriter()
    w.add_chunk(Obj("CMaterialTemplate").add("name", "CName", name))
    return w.build()


# ---------------------------------------------------------------------------
# Textures


def build_xbm(width: int, height: int, codec_data: bytes, compression: str = "TCM_None",
              raw_format: str = "TRF_TrueColor", mips: int = 1) -> bytes:
    """A .xbm holding one or more mips (``codec_data``: all of them, largest first)."""
    w = Cr2wWriter()
    setup = Obj("STextureGroupSetup").add("group", "enum:GpuWrapApieTextureGroup", "TEXG_Generic_Color")
    setup.add("rawFormat", "enum:ETextureRawFormat", raw_format)
    setup.add("compression", "enum:ETextureCompression", compression)
    root = Obj("CBitmapTexture").add("width", "Uint32", width).add("height", "Uint32", height)
    root.add("setup", "STextureGroupSetup", setup)
    size_info = Obj("rendRenderTextureBlobSizeInfo").add("width", "Uint16", width).add("height", "Uint16", height)
    tex_info = Obj("rendRenderTextureBlobTextureInfo").add("textureDataSize", "Uint32", len(codec_data))
    tex_info.add("sliceSize", "Uint32", len(codec_data)).add("sliceCount", "Uint16", 1).add("mipCount", "Uint8", mips)
    header = Obj("rendRenderTextureBlobHeader").add("version", "Uint32", 2)
    header.add("sizeInfo", "rendRenderTextureBlobSizeInfo", size_info)
    header.add("textureInfo", "rendRenderTextureBlobTextureInfo", tex_info)
    buffer_index = w.add_buffer(codec_data)
    blob = Obj("rendRenderTextureBlobPC").add("header", "rendRenderTextureBlobHeader", header)
    blob.add("textureData", "serializationDeferredDataBuffer", buffer_index)
    res = Obj("rendRenderTextureResource")
    w.add_chunk(root)
    blob_index = w.add_chunk(blob)
    res.add("renderResourceBlobPC", "handle:IRenderResourceBlob", blob_index)
    root.add("renderTextureResource", "rendRenderTextureResource", res)
    return w.build()


def rgba_xbm(rgba: np.ndarray) -> bytes:
    h, w = rgba.shape[:2]
    return build_xbm(w, h, np.ascontiguousarray(rgba[..., :4], np.uint8).tobytes())


# ---------------------------------------------------------------------------
# Archives


def build_archive(files: dict[str, bytes], main_sizes: dict[str, int] | None = None) -> bytes:
    """An .archive with every file stored uncompressed. ``main_sizes`` says how many bytes of a file are its
    resource part (the rest becomes buffer segments), else the whole file is one segment."""
    entries = []
    segments = []
    data = bytearray(b"\0" * 48)  # header
    for path, content in files.items():
        h = fnv1a64(normalize_path(path))
        start = len(segments)
        split = (main_sizes or {}).get(path, len(content))
        parts = [content[:split]] + ([content[split:]] if split < len(content) else [])
        for part in parts:
            segments.append((len(data), len(part), len(part)))
            data += part
        entries.append((h, start, len(segments)))
    index_pos = len(data)
    entries.sort()
    index = bytearray(struct.pack("<IIQIII", 8, 0, 0, len(entries), len(segments), 0))
    for h, start, end in entries:
        index += struct.pack("<QqIIIII", h, 0, 0, start, end, 0, 0) + b"\0" * 20
    for off, zsize, size in segments:
        index += struct.pack("<QII", off, zsize, size)
    data += index
    struct.pack_into("<4sIQIQIQ", data, 0, b"RDAR", 12, index_pos, len(index), 0, 0, len(data))
    return bytes(data)
