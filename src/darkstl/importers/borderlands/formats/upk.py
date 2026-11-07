"""Unreal Engine 3 packages (``.upk``, ``.u``) as Borderlands 1, 2 and The Pre-Sequel ship them.

A package is a header, a name table, an import table (objects other packages
hold) and an export table (objects this package holds, each a byte range of
serialized properties followed by the class's own binary data). Packages on PC
are usually compressed after the header in chunks of LZO (or zlib) blocks; the
reader decompresses only the blocks a read touches, so listing a package's
contents costs little more than its tables.

Layouts follow UE Viewer (gildor2/UEViewer, MIT licence), the reference for
Unreal packages; Borderlands-specific differences are marked where they are
used.
"""

from __future__ import annotations

import os
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .... import access
from ....errors import FormatError
from . import lzo

TAG = 0x9E2A83C1
COMPRESS_ZLIB = 1
COMPRESS_LZO = 2
COMPRESS_LZX = 4
PACKAGE_SUFFIXES = (".upk", ".u", ".umap", ".udk", ".xxx")

# Borderlands package versions (UE Viewer's game detection): Borderlands 584/57-58, Borderlands 2 832/46;
# The Pre-Sequel shares Borderlands 2's engine.


class Reader:
    """Little-endian reads over bytes, with UE3's names resolved through a package."""

    __slots__ = ("data", "pos", "pkg", "base")

    def __init__(self, data: bytes, pkg: "Package | None" = None, pos: int = 0, base: int = 0):
        self.data = data
        self.pos = pos
        self.pkg = pkg
        self.base = base  # file (uncompressed stream) offset of data[0]

    @property
    def tell(self) -> int:
        """Position in the package's uncompressed stream."""
        return self.base + self.pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def bytes(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError(f"read past the end of the data ({n} bytes at {self.pos} of {len(self.data)})")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def skip(self, n: int) -> None:
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError("skip past the end of the data")
        self.pos += n

    def _u(self, fmt: str, size: int):
        if self.pos + size > len(self.data):
            raise FormatError("read past the end of the data")
        v = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += size
        return v

    def u8(self) -> int:
        return self._u("<B", 1)

    def i16(self) -> int:
        return self._u("<h", 2)

    def u16(self) -> int:
        return self._u("<H", 2)

    def i32(self) -> int:
        return self._u("<i", 4)

    def u32(self) -> int:
        return self._u("<I", 4)

    def i64(self) -> int:
        return self._u("<q", 8)

    def u64(self) -> int:
        return self._u("<Q", 8)

    def f32(self) -> float:
        return self._u("<f", 4)

    def floats(self, n: int) -> tuple:
        v = struct.unpack_from(f"<{n}f", self.data, self.pos)
        self.pos += 4 * n
        return v

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            if n > 1 << 20:
                raise FormatError("string too long")
            return self.bytes(n).rstrip(b"\0").decode("latin-1")
        n = -n
        if n > 1 << 20:
            raise FormatError("string too long")
        return self.bytes(2 * n).decode("utf-16-le", "replace").rstrip("\0")

    def name(self) -> str:
        index = self.i32()
        number = self.i32()
        return self.pkg.name_at(index, number)

    def count(self, limit: int = 1 << 26) -> int:
        n = self.i32()
        if n < 0 or n > limit:
            raise FormatError(f"bad array length {n}")
        return n

    def bulk_array(self) -> tuple[int, int, bytes]:
        """UE3 ``TArray::BulkSerialize``: element size, count, then the raw elements."""
        size = self.i32()
        n = self.i32()
        if size < 0 or n < 0 or size * n > len(self.data) - self.pos:
            raise FormatError(f"bad bulk array ({n} x {size} bytes)")
        return size, n, self.bytes(size * n)


@dataclass
class Summary:
    version: int
    licensee: int
    headers_size: int
    group: str
    flags: int
    name_count: int
    name_offset: int
    export_count: int
    export_offset: int
    import_count: int
    import_offset: int
    depends_offset: int
    engine_version: int
    cooker_version: int
    compression: int
    chunks: list[tuple[int, int, int, int]]  # (uncompressed offset, size, compressed offset, size)


@dataclass
class Import:
    class_package: str
    class_name: str
    outer: int  # package index of the outer object (0 = none)
    name: str


@dataclass
class Export:
    index: int  # 0-based
    class_index: int
    super_index: int
    outer: int
    name: str
    archetype: int
    flags: int
    size: int
    offset: int
    export_flags: int
    class_name: str = ""
    path: str = ""  # Outer.Group.Name inside the package


@dataclass
class _Block:
    uoff: int
    usize: int
    coff: int
    csize: int
    stored: bool = False


@dataclass
class Package:
    path: Path
    summary: Summary
    names: list[str] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    _blocks: list[_Block] = field(default_factory=list, repr=False)
    _header: bytes = field(default=b"", repr=False)  # the stream before the first compressed chunk
    _cache: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _plain: bool = True
    _method: int = 0  # a fully compressed package's compression (its own summary says none)

    # -- names and objects ------------------------------------------------

    @property
    def stem(self) -> str:
        return self.path.stem

    def name_at(self, index: int, number: int = 0) -> str:
        if not 0 <= index < len(self.names):
            raise FormatError(f"name index {index} out of range")
        base = self.names[index]
        return f"{base}_{number - 1}" if number > 0 else base

    def object_name(self, ref: int) -> str:
        if ref > 0:
            return self.exports[ref - 1].name
        if ref < 0:
            return self.imports[-ref - 1].name
        return "None"

    def object_class(self, ref: int) -> str:
        if ref > 0:
            return self.exports[ref - 1].class_name
        if ref < 0:
            return self.imports[-ref - 1].class_name
        return ""

    def object_path(self, ref: int) -> str:
        """``Outer.Name`` for an export or import; imports start with the package that holds them."""
        parts = []
        seen = 0
        while ref and seen < 64:
            seen += 1
            if ref > 0:
                e = self.exports[ref - 1]
                parts.append(e.name)
                ref = e.outer
            else:
                i = self.imports[-ref - 1]
                parts.append(i.name)
                ref = i.outer
        return ".".join(reversed(parts))

    def import_package(self, ref: int) -> str:
        """The top-level package an import lives in."""
        path = self.object_path(ref)
        return path.split(".", 1)[0]

    def find_export(self, path: str, class_name: str | None = None) -> Export | None:
        low = path.lower()
        for e in self.exports:
            if e.path.lower() == low and (class_name is None or e.class_name == class_name):
                return e
        # cooked packages keep objects of other packages under a forced-export outer named after it
        for e in self.exports:
            if e.path.lower().endswith("." + low) or e.name.lower() == low:
                if class_name is None or e.class_name == class_name:
                    return e
        return None

    # -- data -------------------------------------------------------------

    @property
    def stream_size(self) -> int:
        if self._plain:
            return os.path.getsize(self.path)
        last = self._blocks[-1]
        return last.uoff + last.usize

    def read(self, offset: int, size: int) -> bytes:
        """``size`` bytes of the uncompressed stream at ``offset``."""
        if size <= 0:
            return b""
        if self._plain:
            with open(self.path, "rb") as f:
                f.seek(offset)
                data = f.read(size)
            if len(data) != size:
                raise FormatError(f"{self.path.name} ends early")
            return data
        out = bytearray()
        end = offset + size
        pos = offset
        if pos < len(self._header):
            take = min(end, len(self._header)) - pos
            out += self._header[pos:pos + take]
            pos += take
        while pos < end:
            b = self._block_at(pos)
            if b is None:
                raise FormatError(f"{self.path.name}: offset {pos} is outside the package")
            data = self._decompressed(b)
            take = min(end, b.uoff + b.usize) - pos
            out += data[pos - b.uoff:pos - b.uoff + take]
            pos += take
        return bytes(out)

    def _block_at(self, pos: int) -> _Block | None:
        lo, hi = 0, len(self._blocks) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            b = self._blocks[mid]
            if pos < b.uoff:
                hi = mid - 1
            elif pos >= b.uoff + b.usize:
                lo = mid + 1
            else:
                return b
        return None

    def _decompressed(self, b: _Block) -> bytes:
        with self._lock:
            hit = self._cache.get(b.uoff)
            if hit is not None:
                self._cache.move_to_end(b.uoff)
                return hit
        with open(self.path, "rb") as f:
            f.seek(b.coff)
            raw = f.read(b.csize)
        data = raw if b.stored else decompress_block(raw, b.usize, self._method or self.summary.compression)
        with self._lock:
            self._cache[b.uoff] = data
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return data

    def export_data(self, e: Export) -> Reader:
        return Reader(self.read(e.offset, e.size), self, base=e.offset)


def decompress_block(raw: bytes, usize: int, method: int) -> bytes:
    if method == 0:
        return raw
    if method & COMPRESS_LZO:
        return lzo.decompress(raw, usize)
    if method & COMPRESS_ZLIB:
        return zlib.decompress(raw)
    if method & COMPRESS_LZX:
        raise FormatError("This package uses Xbox 360 LZX compression, which Dark STL can't read")
    raise FormatError(f"unknown package compression {method}")


def read_compressed_chunk(read, offset: int, method: int) -> bytes:
    """A chunk in UE3's compressed format (used for compressed bulk data): header, block sizes, blocks.

    ``read(offset, size)`` reads the containing file or stream."""
    head = read(offset, 16)
    tag, block_size, csize, usize = struct.unpack("<Iiii", head)
    if tag != TAG:
        raise FormatError("compressed data has no chunk header")
    count = max(1, (usize + block_size - 1) // block_size) if block_size > 0 else 1
    table = struct.unpack(f"<{2 * count}i", read(offset + 16, 8 * count))
    pos = offset + 16 + 8 * count
    out = bytearray()
    for i in range(count):
        bc, bu = table[2 * i], table[2 * i + 1]
        out += decompress_block(read(pos, bc), bu, method)
        pos += bc
    return bytes(out)


def _summary(r: Reader) -> Summary:
    tag = r.u32()
    if tag != TAG:
        if tag == 0xC1832A9E:
            raise FormatError("This is a big-endian (console) package, which Dark STL can't read")
        raise FormatError("not an Unreal package")
    fv = r.u32()
    version, licensee = fv & 0xFFFF, fv >> 16
    if version > 900 or version < 400:
        raise FormatError(f"Unreal package version {version} is not Unreal Engine 3 (or is too old)")
    headers = r.i32() if version >= 249 else 0
    group = r.fstring() if version >= 269 else ""
    flags = r.u32()
    name_count, name_offset, export_count, export_offset = r.i32(), r.i32(), r.i32(), r.i32()
    import_count, import_offset = r.i32(), r.i32()
    depends = r.i32() if version >= 415 else 0
    if version >= 623:
        r.skip(12)
    if version >= 584:
        r.skip(4)
    r.skip(16)  # guid
    for _ in range(r.count(1 << 16)):
        r.skip(12 if version >= 322 else 8)
    engine = r.i32() if version >= 245 else 0
    cooker = r.i32() if version >= 277 else 0
    compression, chunks = 0, []
    if version >= 334:
        compression = r.u32()
        for _ in range(r.count(1 << 16)):
            chunks.append((r.i32(), r.i32(), r.i32(), r.i32()))
    return Summary(version, licensee, headers, group, flags, name_count, name_offset, export_count, export_offset,
                   import_count, import_offset, depends, engine, cooker, compression, chunks)


_OPEN: OrderedDict[tuple, Package] = OrderedDict()
_OPEN_LOCK = threading.Lock()


def open_package(path: str | os.PathLike) -> Package:
    """Read a package's header and tables (kept for the last few packages opened)."""
    path = Path(path)
    access.note_file(path, "model")
    st = path.stat()
    key = (os.path.normcase(str(path.resolve())), st.st_size, st.st_mtime_ns)
    with _OPEN_LOCK:
        pkg = _OPEN.get(key)
        if pkg is not None:
            _OPEN.move_to_end(key)
            return pkg
    pkg = _read_package(path)
    with _OPEN_LOCK:
        _OPEN[key] = pkg
        while len(_OPEN) > 24:
            _OPEN.popitem(last=False)
    return pkg


def _fully_compressed(path: Path) -> tuple[list[_Block], int, bytes] | None:
    """A package compressed as a whole (Borderlands 2's Core, Engine, Startup... with a ``.uncompressed_size``
    file beside them): the file is a run of compressed chunks, each a header (tag, block size, sizes), its
    blocks' sizes and the blocks. Returns the blocks, the compression and the first block decompressed."""
    size = os.path.getsize(path)
    blocks: list[_Block] = []
    pos = u = 0
    with open(path, "rb") as f:
        while pos + 16 <= size:
            f.seek(pos)
            tag, block_size, _csize, usize = struct.unpack("<Iiii", f.read(16))
            if tag != TAG or block_size <= 0 or usize < 0:
                if blocks:
                    break  # padding after the last chunk
                return None
            count = max(1, (usize + block_size - 1) // block_size)
            raw = f.read(8 * count)
            if len(raw) != 8 * count:
                raise FormatError(f"{path.name} ends early")
            table = struct.unpack(f"<{2 * count}i", raw)
            p = pos + 16 + 8 * count
            for i in range(count):
                bc, bu = table[2 * i], table[2 * i + 1]
                if bc < 0 or bu < 0:
                    raise FormatError(f"{path.name}: bad compressed block sizes")
                blocks.append(_Block(u, bu, p, bc))
                p += bc
                u += bu
            pos = p
        if not blocks:
            return None
        first = blocks[0]
        f.seek(first.coff)
        raw = f.read(first.csize)
    for method in (COMPRESS_LZO, COMPRESS_ZLIB):
        try:
            data = decompress_block(raw, first.usize, method)
        except (FormatError, zlib.error, IndexError, ValueError):
            continue
        if len(data) == first.usize:
            return blocks, method, data
    raise FormatError(f"{path.name} is compressed in a way Dark STL can't read")


def _read_package(path: Path) -> Package:
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    whole = None
    if len(head) >= 16:
        tag, fv = struct.unpack("<II", head[:8])
        if tag == TAG and fv >= 1024 and fv & (fv - 1) == 0:  # a block size, not a version: compressed whole
            whole = _fully_compressed(path)
    if whole is not None:
        blocks, method, first = whole
        try:
            s = _summary(Reader(first))
        except (struct.error, IndexError):
            raise FormatError(f"{path.name} is not an Unreal package") from None
        pkg = Package(path, s, _blocks=blocks, _plain=False, _method=method)
        pkg._cache[0] = first
        return _read_tables(pkg)
    try:
        s = _summary(Reader(head))
    except (struct.error, IndexError):
        raise FormatError(f"{path.name} is not an Unreal package") from None
    pkg = Package(path, s)
    if s.compression and s.chunks:
        pkg._plain = False
        first = min(c[0] for c in s.chunks)
        with open(path, "rb") as f:
            pkg._header = f.read(first)
            for uoff, usize, coff, csize in sorted(s.chunks):
                if csize == usize:
                    # stored as it is, without a chunk header (seen in Borderlands; UE Viewer notes the same)
                    pkg._blocks.append(_Block(uoff, usize, coff, csize, True))
                    continue
                f.seek(coff)
                tag, block_size, _sc, su = struct.unpack("<Iiii", f.read(16))
                if tag != TAG:
                    raise FormatError(f"{path.name}: compressed chunk has no header")
                count = max(1, (su + block_size - 1) // block_size) if block_size > 0 else 1
                table = struct.unpack(f"<{2 * count}i", f.read(8 * count))
                pos = coff + 16 + 8 * count
                u = uoff
                for i in range(count):
                    bc, bu = table[2 * i], table[2 * i + 1]
                    pkg._blocks.append(_Block(u, bu, pos, bc))
                    pos += bc
                    u += bu
    return _read_tables(pkg)


def _read_tables(pkg: Package) -> Package:
    s = pkg.summary
    v = s.version
    table_end = _table_end(pkg)
    tables = pkg.read(s.name_offset, max(0, min(table_end, pkg.stream_size) - s.name_offset))
    r = Reader(tables, pkg, base=s.name_offset)
    names = []
    for _ in range(s.name_count):
        names.append(r.fstring())
        r.skip(8 if v >= 195 else 4)
    pkg.names = names
    r.pos = s.import_offset - s.name_offset
    for _ in range(s.import_count):
        cp, cn = r.name(), r.name()
        outer = r.i32()
        pkg.imports.append(Import(cp, cn, outer, r.name()))
    r.pos = s.export_offset - s.name_offset
    for i in range(s.export_count):
        ci, si, outer = r.i32(), r.i32(), r.i32()
        name = r.name()
        arch = r.i32() if v >= 220 else 0
        flags = r.u32()
        if v >= 195:
            flags |= r.u32() << 32
        size = r.i32()
        offset = r.i32() if (size or v >= 249) else 0
        if v < 543:
            for _ in range(r.count()):
                r.skip(12)
        eflags = r.u32() if v >= 247 else 0
        if v >= 322:
            n = r.count()
            r.skip(4 * n + 16)
        if v >= 475:
            r.skip(4)
        pkg.exports.append(Export(i, ci, si, outer, name, arch, flags, size, offset, eflags))
    for e in pkg.exports:
        e.class_name = pkg.object_name(e.class_index) if e.class_index else "Class"
        e.path = pkg.object_path(e.index + 1)
    return pkg


def _table_end(pkg: Package) -> int:
    """Where the tables end: the first export's data, or the header size the summary gives."""
    s = pkg.summary
    candidates = [s.headers_size] if s.headers_size > max(s.name_offset, s.import_offset, s.export_offset) else []
    if s.depends_offset > max(s.name_offset, s.import_offset, s.export_offset):
        candidates.append(s.depends_offset)
    if candidates:
        return min(candidates)
    # no hint: assume the tables fit in the first megabytes
    return max(s.name_offset, s.import_offset, s.export_offset) + 8 * 1024 * 1024


def is_package_file(path: Path) -> bool:
    if path.suffix.lower() not in PACKAGE_SUFFIXES:
        return False
    try:
        with open(path, "rb") as f:
            return struct.unpack("<I", f.read(4))[0] == TAG
    except (OSError, struct.error):
        return False


__all__ = ["COMPRESS_LZO", "COMPRESS_ZLIB", "Export", "Import", "Package", "Reader", "Summary", "TAG",
           "decompress_block", "is_package_file", "open_package", "read_compressed_chunk"]
