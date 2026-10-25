"""CR2W: the container of every REDengine resource (.mesh, .xbm, .mi, .mt, .ent...).

A CR2W file is a header, a few tables (strings, names, imports, chunks,
buffers, embedded files) and then the chunks: serialised class instances.
Every property is written as ``name, type, size, value`` with name and type
indices into the names table, so a file can be read without knowing the
classes in advance: this reader turns chunks into :class:`Red` objects holding
plain Python values, and the mesh and texture readers pick what they need.

Type names follow the engine's reflection syntax: ``array:Vector4``,
``static:4,Uint32``, ``[8]Float``, ``handle:CMaterialInstance``,
``rRef:ITexture``... Whether a plain name is a class or an enum is not stored;
the property size tells (an enum is two bytes, a class is at least three)
and arrays rely on the list of enums the mesh and texture classes use.

Large data (vertex buffers, texture pixels) lives in buffers after the chunks,
each possibly compressed with Oodle Kraken behind a ``KARK`` header.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ...fromsoft.formats.binary import BinaryReader, FormatError
from ...fromsoft.formats.oodle import oodle_decompress

MAGIC = b"CR2W"
HEADER_SIZE = 4 + 36 + 10 * 12  # magic, file header, ten table headers
KARK = b"KARK"

FUNDAMENTALS = {"Bool": "B", "Int8": "b", "Uint8": "B", "Int16": "h", "Uint16": "H", "Int32": "i", "Uint32": "I",
                "Int64": "q", "Uint64": "Q", "Float": "f", "Double": "d"}
# enums the mesh, material and texture classes use (an array of these has no size to tell them apart by)
KNOWN_ENUMS = {
    "GpuWrapApiVertexPackingePackingType", "GpuWrapApiVertexPackingePackingUsage",
    "GpuWrapApiVertexPackingEStreamType", "GpuWrapApieIndexBufferChunkType", "GpuWrapApieTextureFormat",
    "GpuWrapApieTextureGroup", "GpuWrapApieTextureType", "ETextureCompression", "ETextureRawFormat",
    "EAstcQuality", "ERenderObjectType", "EMaterialPriority", "ERenderMaterialType", "ETextureAddressing",
    "ETextureFilteringMin", "ETextureFilteringMag", "ETextureFilteringMip", "ETextureComparisonFunction",
    "EMaterialVertexFactory", "EMaterialShaderTarget", "ECompileFlags",
}
KNOWN_BITFIELDS = {"EMeshChunkFlags", "EMaterialTechniqueFlags"}


# ---------------------------------------------------------------------------
# Values


class Red:
    """One serialised class instance: its class name and properties (missing ones were default)."""

    __slots__ = ("cls", "props")

    def __init__(self, cls: str, props: dict[str, Any] | None = None):
        self.cls = cls
        self.props = props if props is not None else {}

    def get(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self.props[name]

    def __contains__(self, name: str) -> bool:
        return name in self.props

    def __repr__(self) -> str:
        return f"Red({self.cls}, {sorted(self.props)})"

    def vec(self, *names: str, default: float = 0.0) -> tuple[float, ...]:
        return tuple(float(self.props.get(n, default)) for n in names)


@dataclass(frozen=True)
class Handle:
    """A reference to another chunk of the same file (``handle:`` / ``whandle:``), or None when empty."""

    index: int


@dataclass(frozen=True)
class Ref:
    """A reference to another resource by depot path (``rRef:`` / ``raRef:``)."""

    path: str
    class_name: str = ""


@dataclass(frozen=True)
class BufferRef:
    """A ``DataBuffer`` or ``serializationDeferredDataBuffer`` kept in the file's buffer table."""

    index: int


@dataclass(frozen=True)
class Raw:
    """A value of a type this reader does not interpret, kept as bytes."""

    type_name: str
    data: bytes


@dataclass(frozen=True)
class Import:
    path: str
    class_name: str
    flags: int


@dataclass(frozen=True)
class ChunkInfo:
    class_name: str
    parent: int
    data_offset: int
    data_size: int


@dataclass(frozen=True)
class BufferInfo:
    flags: int
    index: int
    offset: int
    disk_size: int
    mem_size: int


# ---------------------------------------------------------------------------
# Type names


def parse_type(name: str) -> tuple:
    """``array:handle:Foo`` -> ("array", ("handle", ("leaf", "Foo"))); ``static:4,Uint32`` -> ("static", 4, ...)."""
    if name.startswith("["):
        n, rest = name[1:].split("]", 1)
        return ("fixed", int(n), parse_type(rest))
    head, sep, rest = name.partition(":")
    if not sep:
        return ("leaf", name)
    if head == "static":
        n, _, inner = rest.partition(",")
        return ("static", int(n), parse_type(inner))
    if head in ("array", "handle", "whandle", "rRef", "raRef", "curveData", "multiChannelCurve", "ptr",
                "script_ref"):
        return (head, parse_type(rest))
    return ("leaf", name)  # a class name with a namespace-like colon? treat it as one name


# ---------------------------------------------------------------------------
# Reading


def is_cr2w(data: bytes) -> bool:
    return data[:4] == MAGIC


def decompress_buffer(data: bytes, expected: int | None = None) -> bytes:
    """A buffer as stored: raw, or Kraken-compressed behind ``KARK`` + uncompressed size."""
    if data[:4] != KARK:
        return data
    size = struct.unpack_from("<I", data, 4)[0]
    return oodle_decompress(data[8:], size if expected is None else max(size, 0) or expected)


class Cr2wFile:
    """A parsed CR2W file: tables, chunks as :class:`Red` objects, buffers decoded on demand."""

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset  # where the file starts in ``data`` (files in a material list follow each other)
        self.version = 0
        self.objects_end = 0
        self.buffers_end = 0
        self.strings: dict[int, str] = {}
        self.names: list[str] = []
        self.imports: list[Import] = []
        self.chunk_infos: list[ChunkInfo] = []
        self.buffer_infos: list[BufferInfo] = []
        self.chunks: list[Red] = []
        self._buffers: dict[int, bytes] = {}
        self._info_only = False

    # -- tables -------------------------------------------------------------

    @property
    def root_class(self) -> str:
        return self.chunk_infos[0].class_name if self.chunk_infos else ""

    @property
    def root(self) -> Red:
        if not self.chunks:
            raise FormatError("The CR2W file has no chunks")
        return self.chunks[0]

    @property
    def end(self) -> int:
        """Where the file ends in ``data``."""
        return self.offset + max(self.buffers_end, self.objects_end)

    def chunk(self, handle: Handle | None) -> Red | None:
        if handle is None:
            return None
        if 0 <= handle.index < len(self.chunks):
            return self.chunks[handle.index]
        return None

    def buffer(self, ref: BufferRef | bytes | None) -> bytes:
        """The (decompressed) contents of a buffer."""
        if ref is None:
            return b""
        if isinstance(ref, bytes):
            return decompress_buffer(ref)
        if ref.index in self._buffers:
            return self._buffers[ref.index]
        if not 0 <= ref.index < len(self.buffer_infos):
            raise FormatError(f"CR2W buffer {ref.index} is not in the file")
        info = self.buffer_infos[ref.index]
        start = self.offset + info.offset
        raw = self.data[start:start + info.disk_size]
        if len(raw) < info.disk_size:
            raise FormatError(f"CR2W buffer {ref.index} is truncated: the file's buffers are missing "
                              f"({info.disk_size:,} bytes wanted, {len(raw):,} there)")
        out = decompress_buffer(raw, info.mem_size)
        self._buffers[ref.index] = out
        return out

    def chunks_of(self, class_name: str) -> list[Red]:
        return [c for c in self.chunks if c.cls == class_name]

    # -- parsing --------------------------------------------------------------

    def _read_tables(self, r: BinaryReader) -> list[tuple[int, int]]:
        r.seek(self.offset)
        r.assert_ascii("CR2W")
        (self.version, _flags, _timestamp, _build, self.objects_end, self.buffers_end, _crc,
         _num_chunks) = r.unpack("IIQIIIII")
        if not 163 <= self.version <= 195:
            raise FormatError(f"Unsupported CR2W version {self.version}")
        tables = []
        for _ in range(10):
            off, count, _crc = r.unpack("III")
            tables.append((off, count))
        # strings: table 0's count is the block's length in bytes
        off, length = tables[0]
        block = r.get_bytes(self.offset + off, length)
        pos = 0
        while pos < length:
            end = block.find(b"\0", pos)
            if end < 0:
                end = length
            self.strings[pos] = block[pos:end].decode("utf-8", "replace") or "None"
            pos = end + 1
        off, count = tables[1]
        self.names = [self.strings.get(r.unpack_at(self.offset + off + 8 * i, "II")[0], "") for i in range(count)]
        off, count = tables[2]
        for i in range(count):
            soff, cls, flags = r.unpack_at(self.offset + off + 8 * i, "IHH")
            self.imports.append(Import(self.strings.get(soff, ""), self._name(cls), flags))
        off, count = tables[4]
        for i in range(count):
            cls, _oflags, parent, size, doff, _template, _crc = r.unpack_at(self.offset + off + 24 * i, "HHIIIII")
            self.chunk_infos.append(ChunkInfo(self._name(cls), parent - 1, doff, size))
        off, count = tables[5]
        for i in range(count):
            flags, index, boff, disk, mem, _crc = r.unpack_at(self.offset + off + 24 * i, "6I")
            self.buffer_infos.append(BufferInfo(flags, index, boff, disk, mem))
        return tables

    def _name(self, index: int) -> str:
        if index >= len(self.names):
            raise FormatError(f"CR2W name index {index} out of range")
        return self.names[index]

    def read(self, info_only: bool = False) -> Cr2wFile:
        r = BinaryReader(self.data)
        self._read_tables(r)
        self._info_only = info_only
        if info_only:
            return self
        for ci in self.chunk_infos:
            r.seek(self.offset + ci.data_offset)
            end = self.offset + ci.data_offset + ci.data_size
            obj = self._read_class(r, ci.class_name, end)
            if ci.class_name == "CMaterialInstance" and r.pos + 4 <= end:
                obj.props["values"] = self._read_material_values(r, end)
            self.chunks.append(obj)
        return self

    # -- values -----------------------------------------------------------------

    def _read_class(self, r: BinaryReader, cls: str, end: int | None = None) -> Red:
        zero = r.u8()
        if zero != 0:
            raise FormatError(f"CR2W class {cls} does not start with a zero byte")
        obj = Red(cls)
        while True:
            name_idx = r.u16()
            if name_idx == 0:
                break
            type_name = self._name(r.u16())
            size = r.u32() - 4
            start = r.pos
            try:
                obj.props[self._name(name_idx)] = self._read_value(r, parse_type(type_name), size)
            except (FormatError, struct.error, UnicodeDecodeError, IndexError, ValueError):
                obj.props[self._name(name_idx)] = Raw(type_name, r.get_bytes(start, size))
            r.seek(start + size)
        return obj

    def _read_material_values(self, r: BinaryReader, end: int) -> list[tuple[str, Any]]:
        """CMaterialInstance keeps its parameters after its properties: (name, type, value) triples."""
        values = []
        count = r.i32()
        for _ in range(count):
            if r.pos + 8 > end:
                break
            size = r.i32() - 8
            start = r.pos
            name = self._name(r.u16())
            type_name = self._name(r.u16())
            try:
                value = self._read_value(r, parse_type(type_name), size)
            except (FormatError, struct.error, UnicodeDecodeError, IndexError, ValueError):
                value = Raw(type_name, r.get_bytes(start + 4, size))
            values.append((name, value))
            r.seek(start + 4 + size)
        return values

    def _read_value(self, r: BinaryReader, t: tuple, size: int = -1) -> Any:
        kind = t[0]
        if kind == "array":
            count = r.i32()
            return [self._read_value(r, t[1]) for _ in range(count)]
        if kind in ("static", "fixed"):
            count = r.i32()
            if count > t[1]:
                raise FormatError(f"CR2W static array holds {count} > {t[1]} elements")
            return [self._read_value(r, t[2]) for _ in range(count)]
        if kind in ("handle", "whandle"):
            index = r.i32() - 1
            return Handle(index) if index >= 0 else None
        if kind in ("rRef", "raRef"):
            index = r.u16()
            if index == 0 or index > len(self.imports):
                return None
            imp = self.imports[index - 1]
            return Ref(imp.path, imp.class_name)
        if kind in ("curveData", "multiChannelCurve", "ptr", "script_ref"):
            raise FormatError(f"CR2W type {kind} is not read")
        name = t[1]
        fmt = FUNDAMENTALS.get(name)
        if fmt is not None:
            v = r.unpack(fmt)[0]
            return bool(v) if name == "Bool" else v
        if name == "CName":
            return self._name(r.u16())
        if name in ("String", "NodeRef"):
            return read_string(r)
        if name == "LocalizationString":
            r.u64()
            return read_string(r)
        if name in ("TweakDBID", "CDateTime", "CRUID", "gamedataLocKeyWrapper"):
            return r.u64()
        if name == "CGUID":
            return r.read_bytes(16)
        if name == "DataBuffer":
            n = r.u32()
            if n == 0x80000000:
                return None
            if n > 0x80000000:
                return BufferRef((n ^ 0x80000000) - 1)
            return r.read_bytes(n)
        if name == "serializationDeferredDataBuffer":
            return BufferRef(r.u16() - 1)
        if name == "SharedDataBuffer":
            return r.read_bytes(r.u32())
        if name == "Variant":
            type_name = self._name(r.u16())
            n = r.u32() - 4
            start = r.pos
            try:
                return self._read_value(r, parse_type(type_name), n)
            finally:
                r.seek(start + n)
        if name in KNOWN_ENUMS or (size == 2 and name not in KNOWN_BITFIELDS):
            return self._name(r.u16())
        if name in KNOWN_BITFIELDS:
            flags = []
            while True:
                idx = r.u16()
                if idx == 0:
                    break
                flags.append(self._name(idx))
            return flags
        if size >= 0 and r.data[r.pos] != 0:  # not a class: an unknown enum (or bit field) by its size
            if size % 2:
                raise FormatError(f"Cannot read a {name} of {size} bytes")
            return self._name(r.u16()) if size == 2 else Raw(name, r.read_bytes(size))
        return self._read_class(r, name)


def read_string(r: BinaryReader) -> str:
    """A length-prefixed string: variable-length count, bit 7 of the first byte set for UTF-8 (else UTF-16)."""
    b = r.u8()
    utf8 = bool(b & 0x80)
    value = b & 0x3F
    more = bool(b & 0x40)
    shift = 6
    while more:
        b = r.u8()
        value |= (b & 0x7F) << shift
        more = bool(b & 0x80)
        shift += 7
    if value == 0:
        return ""
    if utf8:
        return r.read_bytes(value).decode("utf-8", "replace")
    return r.read_bytes(value * 2).decode("utf-16-le", "replace")


def read_cr2w(data: bytes, offset: int = 0, info_only: bool = False) -> Cr2wFile:
    if data[offset:offset + 4] != MAGIC:
        raise FormatError("Not a CR2W file")
    return Cr2wFile(data, offset).read(info_only)


def read_cr2w_list(data: bytes, slices: list[tuple[int, int]] | None = None) -> list[Cr2wFile]:
    """Several CR2W files one after another (a mesh's local material buffer). ``slices``: their
    (offset, size) when the owner lists them; otherwise each file's own header says where it ends."""
    files: list[Cr2wFile] = []
    if slices:
        for off, _size in slices:
            if data[off:off + 4] == MAGIC:
                files.append(read_cr2w(data, off))
        return files
    pos = 0
    while pos + HEADER_SIZE <= len(data) and data[pos:pos + 4] == MAGIC:
        f = read_cr2w(data, pos)
        if f.end <= pos:
            break
        files.append(f)
        pos = f.end
    return files


__all__ = [
    "KARK",
    "BufferRef",
    "Cr2wFile",
    "Handle",
    "Import",
    "Raw",
    "Red",
    "Ref",
    "decompress_buffer",
    "is_cr2w",
    "parse_type",
    "read_cr2w",
    "read_cr2w_list",
    "read_string",
]
