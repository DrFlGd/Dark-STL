"""Unreal 5 IoStore containers (``.utoc``/``.ucas``) and the zen packages inside them (Borderlands 4).

An IoStore container is a table of contents (``.utoc``: chunk ids, where each
chunk lies in the uncompressed address space, the compression blocks, the
compression methods, and a directory index naming the chunks that are files)
and the data (``.ucas``, split into ``_s1``, ``_s2``... partitions when large).
Blocks are compressed one by one (Oodle in shipped games) and, in encrypted
containers, AES-encrypted in 16-byte units.

A package is a chunk of type "export bundle data" whose id is the package's id;
its bulk data (the old ``.ubulk``) is another chunk with the same id. The chunk
starts with a zen header instead of the old summary: a name batch, the bulk
data map, the hashes of the exports it imports, the import map (script
objects, listed once in ``global.utoc``, or exports of other packages) and the
export map; the exports' data follow. Each container's header chunk lists,
per package, the packages it imports from.

This module turns a zen package into the same :class:`asset.Package` the pak
reader makes, so the mesh, texture and material readers work on both. The
layouts follow CUE4Parse (Apache 2.0).
"""

from __future__ import annotations

import os
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .... import access
from ....errors import FormatError
from ..formats.upk import Reader
from . import asset, pak
from .asset import Bulk, Export, Import, Package, Versions

MAGIC = b"-==--==--==--==-"
HEADER_SIZE = 144
F_COMPRESSED, F_ENCRYPTED, F_SIGNED, F_INDEXED = 1, 2, 4, 8
# chunk types (Unreal 5)
EXPORT_BUNDLE_DATA, BULK_DATA, OPTIONAL_BULK_DATA, MEMORY_MAPPED_BULK_DATA = 1, 2, 3, 4
SCRIPT_OBJECTS, CONTAINER_HEADER = 5, 6
CONTAINER_HEADER_MAGIC = 0x496F436E
INVALID = 0xFFFFFFFF
NULL_INDEX = (1 << 64) - 1
TYPE_EXPORT, TYPE_SCRIPT_IMPORT, TYPE_PACKAGE_IMPORT = 0, 1, 2
NAME_HASH_VERSION = 0xC1640000


# -- the table of contents ------------------------------------------------------------

@dataclass
class Block:
    offset: int
    csize: int
    usize: int
    method: int


@dataclass
class Toc:
    path: Path
    version: int
    entry_count: int
    block_size: int
    container_id: int
    flags: int
    partition_size: int
    partitions: int
    chunk_ids: list[tuple[int, int, int]]  # (id, index, type)
    offsets: list[tuple[int, int]]  # (offset, length) in the uncompressed address space
    blocks: list[Block]
    methods: list[str]  # index 0 = none
    dir_index: bytes = b""

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & F_ENCRYPTED)


def read_toc(path: Path) -> Toc:
    data = Path(path).read_bytes()
    if data[:16] != MAGIC:
        raise FormatError(f"{Path(path).name} isn't an IoStore table of contents")
    version = data[16]
    (hsize, entries, nblocks, block_entry_size, nmethods, method_len, block_size, dir_size, partitions,
     container_id) = struct.unpack_from("<9IQ", data, 20)
    flags = data[80]
    seeds, partition_size, no_hash = struct.unpack_from("<IQI", data, 84)
    if version < 3:
        partitions, partition_size = 1, 1 << 62
    seeds_n = seeds if version >= 4 else 0
    no_hash_n = no_hash if version >= 5 else 0
    pos = (hsize or HEADER_SIZE)
    pos = (pos + 3) & ~3
    if block_entry_size != 12:
        raise FormatError(f"{Path(path).name}: compression blocks of {block_entry_size} bytes aren't supported")
    ids = []
    for i in range(entries):
        cid = struct.unpack_from("<Q", data, pos + 12 * i)[0]
        cidx = struct.unpack_from(">H", data, pos + 12 * i + 8)[0]
        ids.append((cid, cidx, data[pos + 12 * i + 11]))
    pos += 12 * entries
    offs = []
    for i in range(entries):
        b = data[pos + 10 * i:pos + 10 * i + 10]
        offs.append((int.from_bytes(b[:5], "big"), int.from_bytes(b[5:], "big")))
    pos += 10 * entries
    pos += 4 * seeds_n + 4 * no_hash_n
    blocks = []
    for i in range(nblocks):
        b = data[pos + 12 * i:pos + 12 * i + 12]
        blocks.append(Block(int.from_bytes(b[:5], "little"), int.from_bytes(b[5:8], "little"),
                            int.from_bytes(b[8:11], "little"), b[11]))
    pos += 12 * nblocks
    methods = ["none"]
    for i in range(nmethods):
        methods.append(data[pos + i * method_len:pos + (i + 1) * method_len].rstrip(b"\0").decode("ascii", "replace"))
    pos += nmethods * method_len
    if flags & F_SIGNED:
        n = struct.unpack_from("<i", data, pos)[0]
        pos += 4 + 2 * n + 20 * nblocks
    dir_index = b""
    if version >= 2 and flags & F_INDEXED and dir_size:
        dir_index = data[pos:pos + dir_size]
    return Toc(Path(path), version, entries, block_size, container_id, flags, partition_size, max(1, partitions),
               ids, offs, blocks, methods, dir_index)


def _fstring(data: bytes, pos: int) -> tuple[str, int]:
    n = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if n == 0:
        return "", pos
    if n > 0:
        if n > 4096 or pos + n > len(data):
            raise FormatError("bad string in the directory index")
        return data[pos:pos + n].rstrip(b"\0").decode("utf-8", "replace"), pos + n
    n = -n
    if n > 4096 or pos + 2 * n > len(data):
        raise FormatError("bad string in the directory index")
    return data[pos:pos + 2 * n].decode("utf-16-le", "replace").rstrip("\0"), pos + 2 * n


def read_directory(index: bytes) -> tuple[str, list[tuple[str, int]]]:
    """(mount point, [(path below it, toc entry)]) from a decrypted directory index."""
    mount, pos = _fstring(index, 0)
    n = struct.unpack_from("<I", index, pos)[0]
    pos += 4
    dirs = [struct.unpack_from("<4I", index, pos + 16 * i) for i in range(n)]
    pos += 16 * n
    n = struct.unpack_from("<I", index, pos)[0]
    pos += 4
    files = [struct.unpack_from("<3I", index, pos + 12 * i) for i in range(n)]
    pos += 12 * n
    n = struct.unpack_from("<I", index, pos)[0]
    pos += 4
    strings = []
    for _ in range(n):
        s, pos = _fstring(index, pos)
        strings.append(s)
    out = []
    stack = [(0, "")] if dirs else []
    seen = 0
    while stack and seen < 1 << 22:
        seen += 1
        d, parent = stack.pop()
        name, child, sibling, first = dirs[d]
        path = parent
        if name != INVALID and strings[name]:
            path = f"{parent}{strings[name]}/"
        if sibling != INVALID:
            stack.append((sibling, parent))
        if child != INVALID:
            stack.append((child, path))
        f = first
        guard = 0
        while f != INVALID and guard < 1 << 22:
            guard += 1
            fname, nxt, user = files[f]
            out.append((path + strings[fname], user))
            f = nxt
    return mount, out


class Container:
    """One .utoc with its .ucas partitions."""

    def __init__(self, toc: Toc, key: bytes | None):
        self.toc = toc
        self.key = key
        stem = str(toc.path)[:-5]
        self.parts = [Path(stem + (f"_s{i}" if i else "") + ".ucas") for i in range(toc.partitions)]
        self._fh: dict[int, object] = {}
        self._lock = threading.Lock()
        self.by_id: dict[tuple[int, int, int], int] = {c: i for i, c in enumerate(toc.chunk_ids)}

    @property
    def name(self) -> str:
        return self.toc.path.name

    def _raw(self, part: int, offset: int, size: int) -> bytes:
        with self._lock:
            fh = self._fh.get(part)
            if fh is None:
                fh = open(self.parts[part], "rb")  # noqa: SIM115 - kept open while the game is
                self._fh[part] = fh
            fh.seek(offset)
            return fh.read(size)

    def read(self, entry: int, start: int = 0, size: int | None = None) -> bytes:
        """A chunk's bytes (or ``size`` of them from ``start``)."""
        offset, length = self.toc.offsets[entry]
        if size is None or start + size > length:
            size = max(0, length - start)
        if size == 0:
            return b""
        offset += start
        bs = self.toc.block_size
        first, last = offset // bs, (offset + size - 1) // bs
        out = bytearray()
        for bi in range(first, last + 1):
            if bi >= len(self.toc.blocks):
                raise FormatError(f"{self.name}: chunk {entry} lies outside the container")
            b = self.toc.blocks[bi]
            raw_size = (b.csize + 15) & ~15
            part, pos = divmod(b.offset, self.toc.partition_size)
            raw = self._raw(part, pos, raw_size)
            if self.toc.encrypted:
                if self.key is None:
                    raise FormatError(f"{self.name} is encrypted and no AES key opens it")
                raw = pak.aes_decrypt(self.key, raw)
            raw = raw[:b.csize]
            if b.method:
                m = self.toc.methods[b.method] if b.method < len(self.toc.methods) else "?"
                raw = pak.decompress(m, raw, b.usize)
            out += raw[:b.usize]
        skip = offset - first * bs
        return bytes(out[skip:skip + size])

    def close(self) -> None:
        for fh in self._fh.values():
            fh.close()
        self._fh.clear()


def _directory(toc: Toc, keys: list[bytes]) -> tuple[bytes | None, str, list[tuple[str, int]]]:
    """(key, mount, files): tries the keys on an encrypted index."""
    if not toc.dir_index:
        return None, "", []
    if not toc.encrypted:
        mount, files = read_directory(toc.dir_index)
        return None, mount, files
    for k in keys:
        try:
            idx = pak.aes_decrypt(k, toc.dir_index)
            mount, files = read_directory(idx)
        except (FormatError, struct.error, IndexError, ValueError):
            continue
        if mount.startswith("../") or mount.startswith("/"):
            return k, mount, files
    raise FormatError(f"{toc.path.name} is encrypted and no AES key Dark STL knows opens it")


# -- names, script objects, container headers ----------------------------------------

def name_batch(r: Reader) -> list[str]:
    n = r.i32()
    if n <= 0:
        return []
    if n > 1 << 22:
        raise FormatError("bad name batch")
    r.u32()  # string bytes
    r.u64()  # hash version
    r.skip(8 * n)
    heads = r.bytes(2 * n)
    out = []
    for i in range(n):
        a, b = heads[2 * i], heads[2 * i + 1]
        length = ((a & 0x7F) << 8) | b
        if a & 0x80:
            out.append(r.bytes(2 * length).decode("utf-16-le", "replace"))
        else:
            out.append(r.bytes(length).decode("utf-8", "replace"))
    return out


def _mapped(value: int, local: list[str], global_names: list[str]) -> str:
    index = value & 0x3FFFFFFF
    number = value >> 32
    names = global_names if (value >> 30) & 3 else local
    base = names[index] if 0 <= index < len(names) else f"name{index}"
    return f"{base}_{number - 1}" if number else base


@dataclass
class ScriptObject:
    name: str
    outer: int
    cdo_class: int


def read_script_objects(data: bytes) -> tuple[list[str], dict[int, ScriptObject]]:
    r = Reader(data)
    names = name_batch(r)
    out = {}
    for _ in range(r.count(1 << 22)):
        mapped = r.u64()
        gidx, outer, cdo = r.u64(), r.u64(), r.u64()
        out[gidx] = ScriptObject(_mapped(mapped, names, names), outer, cdo)
    return names, out


def read_container_header(data: bytes) -> dict[int, list[int]]:
    """Package id -> the ids of the packages it imports."""
    r = Reader(data)
    if r.u32() != CONTAINER_HEADER_MAGIC:
        raise FormatError("bad IoStore container header")
    version = r.u32()
    r.u64()  # container id
    if version < 2:
        r.u32()  # package count
    out: dict[int, list[int]] = {}
    entry = 16 if version >= 3 else 24  # (export counts,) imported packages, shader map hashes
    for _segment in range(2 if version >= 2 else 1):
        ids = [r.u64() for _ in range(r.count(1 << 24))]
        size = r.i32()
        start = r.pos
        for i, pid in enumerate(ids):
            p = start + entry * i + (0 if version >= 3 else 8)
            n, off = struct.unpack_from("<ii", data, p)
            imported = list(struct.unpack_from(f"<{n}Q", data, p + off)) if n > 0 else []
            out.setdefault(pid, imported)
        r.pos = start + size
    return out


# -- zen packages ---------------------------------------------------------------------

@dataclass
class ZenExport:
    serial_offset: int
    serial_size: int
    name: str
    outer: int
    class_index: int
    super_index: int
    template_index: int
    public_hash: int
    flags: int


class ZenPackage(Package):
    """A zen package as an :class:`asset.Package`; imports from other packages are looked up when first needed."""

    @property
    def imports(self) -> list[Import]:
        pending = self.__dict__.get("_pending_imports")
        if pending is not None:
            self.__dict__["_pending_imports"] = None
            self.__dict__["_imports"] = pending()
        return self.__dict__.get("_imports", [])

    @imports.setter
    def imports(self, value: list[Import]) -> None:
        self.__dict__["_imports"] = value


@dataclass
class _Header:
    name: str
    names: list[str]
    versions: Versions
    flags: int
    header_size: int
    cooked_header_size: int
    bulk_map: list[Bulk]
    import_hashes: list[int]
    import_map: list[int]
    exports: list[ZenExport]


def _versions_for(store_version: int) -> Versions:
    # Unversioned packages say nothing about their engine; the container header's version tells 5.3 from 5.4+.
    if store_version >= 5:
        return Versions(ue4=522, ue5=1013, engine=505)
    return Versions(ue4=522, ue5=1010, engine=503)


def read_zen_header(data: bytes, store_version: int = 5) -> _Header:
    r = Reader(data)
    has_versioning = r.u32()
    header_size = r.u32()
    mapped_name = r.u64()
    flags = r.u32()
    cooked_header_size = r.u32()
    hashes_off, imports_off, exports_off, bundles_off = r.i32(), r.i32(), r.i32(), r.i32()
    r.i32(), r.i32(), r.i32()  # dependency bundle headers / entries, imported package names
    v = _versions_for(store_version)
    if has_versioning:
        r.u32()  # zen version
        v.ue4, v.ue5, v.licensee = r.i32(), r.i32(), r.i32()
        for _ in range(r.count(4096)):
            guid = struct.unpack("<4I", r.bytes(16))
            v.custom[guid] = r.i32()
        v.engine = asset._engine_from_file_version(v.ue4, v.ue5)
    v.filter_editor_only = True  # zen packages are always cooked
    # Unreal 5.6+ write two cell offsets here; the name batch's hash version tells where it starts.
    if r.remaining() >= 24:
        at = r.pos
        if struct.unpack_from("<Q", data, at + 8)[0] != NAME_HASH_VERSION and \
                struct.unpack_from("<Q", data, at + 16)[0] == NAME_HASH_VERSION:
            r.skip(8)
    names = name_batch(r)
    bulk_map: list[Bulk] = []
    p = r.pos
    if p + 8 <= hashes_off:
        pad = struct.unpack_from("<Q", data, p)[0]
        size_a = struct.unpack_from("<q", data, p + 8 + pad)[0] if pad < 64 and p + 16 + pad <= hashes_off else -1
        size_b = struct.unpack_from("<q", data, p)[0]
        if size_a >= 0 and p + 16 + pad + size_a == hashes_off:
            start, size = p + 16 + pad, size_a
        elif p + 8 + size_b == hashes_off:
            start, size = p + 8, size_b
        else:
            start, size = p, 0
        for i in range(size // 32):
            off, _dup, sz, fl = struct.unpack_from("<QQQI", data, start + 32 * i)
            bulk_map.append(Bulk(fl | asset.BULK_NO_OFFSET_FIXUP, sz, sz, off))
    import_hashes = list(struct.unpack_from(f"<{(imports_off - hashes_off) // 8}Q", data, hashes_off))
    import_map = list(struct.unpack_from(f"<{(exports_off - imports_off) // 8}Q", data, imports_off))
    exports = []
    for i in range((bundles_off - exports_off) // 72):
        at = exports_off + 72 * i
        (off, size, ename, outer, cls, sup, tmpl, phash, oflags) = struct.unpack_from("<QQQQQQQQI", data, at)
        exports.append(ZenExport(off, size, _mapped(ename, names, []), outer, cls, sup, tmpl, phash, oflags))
    pname = _mapped(mapped_name, names, [])
    return _Header(pname, names, v, flags, header_size, cooked_header_size, bulk_map, import_hashes, import_map,
                   exports)


def _export_ref(index: int) -> int:
    """A zen object index pointing at an export -> the classic package index (1-based)."""
    if index == NULL_INDEX:
        return 0
    if index >> 62 == TYPE_EXPORT:
        return (index & 0xFFFFFFFF) + 1
    return 0


# -- the game's containers -----------------------------------------------------------

class ZenStore:
    """Every IoStore container of a game: files by virtual path, packages by id, the global script objects."""

    def __init__(self, root: Path, game: str | None):
        self.root = Path(root)
        self.game = game
        self.gf = None  # the GameFiles this store belongs to, so packages follow their imports through it
        self.containers: list[Container] = []
        self.files: dict[str, tuple[Container, int]] = {}
        self.spelled: dict[str, str] = {}
        self.packages: dict[int, tuple[Container, int, str]] = {}  # package id -> (container, entry, virtual)
        self.by_name: dict[str, str] = {}  # lower-case package name -> virtual
        self.imported: dict[int, list[int]] = {}
        self.store_version = 5
        self.script_names: list[str] = []
        self.script_objects: dict[int, ScriptObject] = {}
        self.locked: list[Path] = []
        self.errors: list[str] = []
        self._headers: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    # files ---------------------------------------------------------------------------

    def exists(self, virtual: str) -> bool:
        return virtual.lower() in self.files

    def assets(self) -> list[str]:
        return [self.spelled[k] for k in self.files if k.endswith((".uasset", ".umap"))]

    def read(self, virtual: str) -> bytes:
        hit = self.files.get(virtual.lower())
        if hit is None:
            raise FormatError(f"{virtual} isn't in the game's containers")
        c, entry = hit
        return c.read(entry)

    def _chunk(self, pid: int, kind: int) -> tuple[Container, int] | None:
        for c in self.containers:
            e = c.by_id.get((pid, 0, kind))
            if e is not None:
                return c, e
        return None

    # packages ------------------------------------------------------------------------

    def _header(self, pid: int) -> tuple[_Header, bytes] | None:
        with self._lock:
            if pid in self._headers:
                self._headers.move_to_end(pid)
                return self._headers[pid]
        hit = self.packages.get(pid)
        if hit is None:
            return None
        c, entry, _virtual = hit
        data = c.read(entry)
        h = read_zen_header(data, self.store_version)
        with self._lock:
            self._headers[pid] = (h, data)
            while len(self._headers) > 64:
                self._headers.popitem(last=False)
        return h, data

    def class_of(self, virtual: str) -> str | None:
        """The class of a package's main export, reading only the start of its chunk."""
        hit = self.files.get(virtual.lower())
        if hit is None:
            return None
        c, entry = hit
        size = c.toc.offsets[entry][1]
        for n in (min(size, 1 << 16), size):
            try:
                h = read_zen_header(c.read(entry, 0, n), self.store_version)
            except (FormatError, struct.error, IndexError):
                continue
            stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            best = None
            for e in h.exports:
                if e.outer == NULL_INDEX:
                    cls = self._script_name(e.class_index)
                    if e.name.lower() == stem:
                        return cls
                    best = best or cls
            return best
        return None

    def _script_name(self, index: int) -> str:
        so = self.script_objects.get(index)
        return so.name if so else ""

    def _script_path(self, index: int) -> list[str]:
        out, seen = [], 0
        while index != NULL_INDEX and index in self.script_objects and seen < 16:
            seen += 1
            so = self.script_objects[index]
            out.append(so.name)
            index = so.outer
        return out[::-1]

    def note(self, virtual: str) -> None:
        """For collecting: a zen package is read through its container's table, so the container (its .utoc,
        .ucas and .pak) and the global one are kept whole."""
        hit = self.files.get(virtual.lower())
        if hit is None:
            return
        for c in {id(x): x for x in [hit[0]] + [x for x in self.containers
                                                  if x.toc.path.stem.lower() == "global"]}.values():
            access.note_file(c.toc.path, "model")
            for part in c.parts:
                access.note_file(part, "model")
            pak_file = c.toc.path.with_suffix(".pak")
            if pak_file.is_file():
                access.note_file(pak_file, "model")

    def open_package(self, virtual: str) -> Package:
        hit = self.files.get(virtual.lower())
        if hit is None:
            raise FormatError(f"{virtual} isn't in the game's containers")
        if access.active():
            self.note(virtual)
        c, entry = hit
        pid = c.toc.chunk_ids[entry][0]
        got = self._header(pid)
        if got is None:
            raise FormatError(f"{virtual} isn't a package")
        h, data = got
        return self._package(pid, h, data, virtual)

    def open_named(self, name: str) -> Package | None:
        v = self.by_name.get(name.lower().split(".", 1)[0])
        return self.open_package(v) if v else None

    def _package(self, pid: int, h: _Header, data: bytes, virtual: str) -> Package:
        from .vfs import package_name

        body = bytes(h.cooked_header_size) + data[h.header_size:]
        name = package_name(virtual) or h.name
        exports = []
        for i, ze in enumerate(h.exports):
            off = h.cooked_header_size + ze.serial_offset
            e = Export(i, 0, 0, 0, _export_ref(ze.outer), ze.name, ze.flags, ze.serial_size, off)
            exports.append(e)
        pkg = ZenPackage(name, h.versions, list(h.names), [], exports, body, 0, None, virtual, self.gf,
                         package_flags=h.flags, bulk_map=h.bulk_map or None)
        n_imports = len(h.import_map)
        extra: list[Import] = []

        def class_ref(index: int) -> tuple[int, str]:
            if index == NULL_INDEX:
                return 0, "Class"
            kind = index >> 62
            if kind == TYPE_EXPORT:
                i = index & 0xFFFFFFFF
                return i + 1, exports[i].name if i < len(exports) else ""
            if kind == TYPE_SCRIPT_IMPORT:
                return 0, self._script_name(index)
            if kind == TYPE_PACKAGE_IMPORT:
                try:
                    j = h.import_map.index(index)
                    return -(j + 1), ""
                except ValueError:
                    return 0, ""
            return 0, ""

        pending_class = []
        for ze, e in zip(h.exports, exports):
            ref, cls = class_ref(ze.class_index)
            e.class_index = ref
            e.class_name = cls
            if not cls and ref < 0:
                pending_class.append(e)
        for e in exports:
            e.path = pkg.object_path(e.index + 1)

        def resolve_imports() -> list[Import]:
            out: list[Import] = []
            packages: dict[str, int] = {}

            def package_import(pname: str) -> int:
                if pname not in packages:
                    extra.append(Import("/Script/CoreUObject", "Package", 0, pname))
                    packages[pname] = -(n_imports + len(extra))
                return packages[pname]

            imported_ids = self.imported.get(pid, [])
            for index in h.import_map:
                kind = index >> 62
                if index == NULL_INDEX:
                    out.append(Import("", "", 0, "None"))
                elif kind == TYPE_SCRIPT_IMPORT:
                    path = self._script_path(index)
                    so = self.script_objects.get(index)
                    if not path or so is None:
                        out.append(Import("/Script/CoreUObject", "Class", 0, f"script{index & 0xFFFF}"))
                        continue
                    outer = package_import(path[0]) if len(path) > 1 else 0
                    cls = "Package" if len(path) == 1 else ("Class" if so.cdo_class != NULL_INDEX else "Object")
                    out.append(Import("/Script/CoreUObject", cls, outer, path[-1]))
                elif kind == TYPE_PACKAGE_IMPORT:
                    pkg_index = (index & ((1 << 62) - 1)) >> 32
                    hash_index = index & 0xFFFFFFFF
                    target = imported_ids[pkg_index] if pkg_index < len(imported_ids) else None
                    want = h.import_hashes[hash_index] if hash_index < len(h.import_hashes) else None
                    found = None
                    if target is not None and want is not None:
                        try:
                            got = self._header(target)
                        except (FormatError, OSError, struct.error):
                            got = None
                        if got is not None:
                            th, _data = got
                            for te in th.exports:
                                if te.public_hash == want:
                                    tv = self.packages[target][2]
                                    found = (package_name(tv) or th.name, te.name, self._script_name(te.class_index))
                                    break
                    if found is None:
                        out.append(Import("", "", 0, "None"))
                    else:
                        pname, ename, cls = found
                        out.append(Import("/Script/Engine", cls, package_import(pname), ename))
                else:
                    out.append(Import("", "", 0, "None"))
            out += extra
            for e in pending_class:
                if -e.class_index - 1 < len(out):
                    e.class_name = out[-e.class_index - 1].name
            return out

        pkg.__dict__["_pending_imports"] = resolve_imports
        if pending_class:
            pkg.imports  # noqa: B018 - resolves the class names now

        def payload(ext: str, pid=pid) -> bytes | None:
            kind = {".ubulk": BULK_DATA, ".uptnl": OPTIONAL_BULK_DATA, ".m.ubulk": MEMORY_MAPPED_BULK_DATA}.get(ext)
            if kind is None:
                return None
            hit = self._chunk(pid, kind)
            if hit is None:
                return None
            c, entry = hit
            return c.read(entry)

        pkg.payload = payload
        if h.flags & asset.PKG_UNVERSIONED_PROPERTIES:
            pkg.unversioned_props = self.unversioned_reader(pkg)
        return pkg

    def unversioned_reader(self, pkg: Package):
        from .unversioned import reader_for

        return reader_for(pkg, self.root, self.game)


def find_tocs(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ("binaries", "saved", "intermediate", "__pycache__")]
        for f in filenames:
            if f.lower().endswith(".utoc"):
                out.append(Path(dirpath) / f)
    # patches (_P) last, so their files replace the base game's
    return sorted(out, key=lambda p: (p.stem.lower().endswith("_p"), p.stem.lower()))


def open_game(root: Path, keys: list[bytes], gf=None) -> ZenStore | None:
    """The game's IoStore containers, or None when it has none."""
    from .vfs import _mount_prefix, package_name, pak_root

    tocs = find_tocs(Path(root))
    if not tocs:
        return None
    store = ZenStore(Path(root), getattr(gf, "game", None))
    store.gf = gf
    for t in tocs:
        try:
            toc = read_toc(t)
        except (FormatError, OSError, struct.error) as e:
            store.errors.append(f"{t.name}: {e}")
            continue
        if t.stem.lower() == "global":
            try:
                c = Container(toc, keys[0] if toc.encrypted and keys else None)
                e = c.by_id.get((0, 0, SCRIPT_OBJECTS))
                if e is not None:
                    store.script_names, store.script_objects = read_script_objects(c.read(e))
                store.containers.append(c)
            except (FormatError, OSError, struct.error) as err:
                store.errors.append(f"{t.name}: {err}")
            continue
        try:
            key, mount, files = _directory(toc, keys)
        except FormatError:
            store.locked.append(t)
            if gf is not None:
                gf.locked.append(t)
            continue
        if not all(p.is_file() for p in Container(toc, None).parts):
            store.errors.append(f"{t.name}: its .ucas file is missing")
            continue
        c = Container(toc, key)
        store.containers.append(c)
        base = pak_root(t)
        try:
            rel = base.relative_to(root)
            prefix = "" if str(rel) == "." else rel.as_posix() + "/"
        except ValueError:
            prefix = ""
        mount = prefix + _mount_prefix(mount)
        for path, entry in files:
            virtual = mount + path
            low = virtual.lower()
            store.files[low] = (c, entry)
            store.spelled[low] = virtual
            if entry < len(toc.chunk_ids) and low.endswith((".uasset", ".umap")):
                cid, cidx, ctype = toc.chunk_ids[entry]
                if ctype == EXPORT_BUNDLE_DATA and cidx == 0:
                    store.packages[cid] = (c, entry, virtual)
                    name = package_name(virtual)
                    if name:
                        store.by_name[name.lower()] = virtual
        he = c.by_id.get((toc.container_id, 0, CONTAINER_HEADER))
        if he is not None:
            try:
                raw = c.read(he)
                store.store_version = struct.unpack_from("<I", raw, 4)[0] if len(raw) >= 8 else 5
                for pid, imported in read_container_header(raw).items():
                    store.imported.setdefault(pid, imported)
            except (FormatError, struct.error, IndexError) as err:
                store.errors.append(f"{t.name}: its package list couldn't be read ({err})")
    if not store.files and not store.locked:
        return None
    if gf is not None:
        gf.errors += store.errors
    if store.files and not store.script_objects:
        msg = "global.utoc (the engine's class list) is missing or couldn't be read; models can't be opened without it"
        store.errors.append(msg)
        if gf is not None:
            gf.errors.append(msg)
    return store


__all__ = ["Container", "ZenPackage", "ZenStore", "find_tocs", "name_batch", "open_game", "read_container_header",
           "read_directory", "read_toc", "read_zen_header"]
