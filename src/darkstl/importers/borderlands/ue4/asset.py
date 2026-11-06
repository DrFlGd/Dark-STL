"""Unreal Engine 4 packages: a ``.uasset`` header with its ``.uexp`` (and ``.ubulk``) data.

Cooked UE4 games split every package in two: the ``.uasset`` holds the summary
and the name, import and export tables, the ``.uexp`` the exports' data (its
offsets count from the start of the ``.uasset``, so the two read as one
stream), and large payloads such as texture mips and mesh LODs sit in a
``.ubulk`` beside them. Unlike UE3 the summary lists "custom versions" (one
number per engine subsystem), which decide many of the binary layouts.

Layouts follow CUE4Parse (Apache 2.0) and UE Viewer (MIT).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable

from ....errors import FormatError
from ..formats.upk import Reader

TAG = 0x9E2A83C1
PKG_FILTER_EDITOR_ONLY = 0x80000000

# Custom versions the mesh and texture readers ask about: their GUID (four u32) and, for packages that don't list
# them, the value each engine version has. A table entry (v, n) means "engines older than v have n"
# (engine versions as 420 for 4.20, 505 for 5.5).
CUSTOM_VERSIONS: dict[str, tuple[tuple[int, int, int, int], list[tuple[int, int]], int]] = {
    "Rendering": ((0x12F88B9F, 0x88754AFC, 0xA67CD90C, 0x383ABD29),
                  [(418, 19), (419, 20), (420, 25), (421, 26), (422, 27), (423, 28), (424, 31), (425, 36), (426, 43),
                   (427, 44), (504, 45), (505, 47), (506, 48)], 49),
    "SkeletalMesh": ((0xD78A4A00, 0xE8584697, 0xBAA819B5, 0x487D46B4),
                     [(418, 9), (419, 10), (420, 15), (423, 16), (426, 17)], 18),
    "Editor": ((0xE4B068ED, 0xF49442E9, 0xA231DA0B, 0x2E46BB41),
               [(419, 20), (420, 23), (421, 24), (422, 26), (423, 30), (424, 34), (425, 37), (426, 38)], 40),
    "Core": ((0x375EC13C, 0x06E448FB, 0xB50084F0, 0x262A717E), [(422, 2), (425, 3)], 4),
    "Release": ((0x9C54D522, 0xA8264FBE, 0x94210746, 0x61B482D0),
                [(419, 10), (420, 12), (421, 17), (423, 20), (424, 23), (425, 28), (426, 30), (427, 38),
                 (500, 43)], 44),
    "FortniteMain": ((0x601D1886, 0xAC644F84, 0xAA16D3DE, 0x0DEAC7D6),
                     [(420, 0), (421, 12), (422, 17), (423, 25), (424, 27), (426, 31), (427, 43), (500, 47),
                      (501, 59), (502, 80), (503, 83), (504, 111), (505, 134), (506, 170)], 268),
    "RecomputeTangent": ((0x5579F886, 0x933A4C1F, 0x83BA087B, 0x6361B92F), [(412, 0), (426, 1)], 2),
    "Anim": ((0xAF43A65D, 0x7FD34947, 0x98733E8E, 0xD9C1BB05), [(421, 0), (425, 2), (426, 7)], 15),
    "UE5Main": ((0x697DD581, 0xE64F41AB, 0xAA4A51EC, 0xBEB7B628),
                [(500, -1), (501, 0), (502, 59), (503, 88), (504, 104), (505, 118), (507, 119)], 123),
    "UE5Release": ((0xD89B5E42, 0x24BD4D46, 0x8412ACA8, 0xDF641779),
                   [(500, -1), (501, 0), (502, 36), (504, 41), (505, 47), (507, 51)], 68),
}

# Values of the custom versions the readers compare against.
RENDERING_TEXTURE_STREAMING_UV = 10
RENDERING_INCREASE_NORMAL_PRECISION = 26
SKELETAL_SPLIT_MODEL_AND_RENDER_DATA = 12
EDITOR_REFACTOR_MESH_EDITOR_MATERIALS = 8
CORE_SKELETAL_MATERIAL_EDITOR_DATA_STRIPPING = 3
RELEASE_ADD_SKELETAL_MESH_SECTION_DISABLE = 12
RELEASE_WEIGHT_MESH_TO_MESH_VERT_DATA = 33
FORTNITE_ALLOW_SKELETAL_MESH_TO_REDUCE_BASE_LOD = 22
RECOMPUTE_TANGENT_RUNTIME = 1
RECOMPUTE_TANGENT_VERTEX_COLOR_MASK = 2


@dataclass
class Versions:
    """What decides the binary layouts: the package's file version, its engine and its custom versions."""

    ue4: int = 522
    ue5: int = 0
    licensee: int = 0
    engine: int = 426  # major * 100 + minor
    game: str = ""  # "bl3" for Borderlands 3's own changes to the layouts
    custom: dict[tuple[int, int, int, int], int] = field(default_factory=dict)
    filter_editor_only: bool = True

    def cv(self, name: str) -> int:
        guid, table, latest = CUSTOM_VERSIONS[name]
        if guid in self.custom:
            return self.custom[guid]
        for bound, value in table:
            if self.engine < bound:
                return value
        return latest

    @property
    def bl3(self) -> bool:
        return self.game == "bl3"


@dataclass
class Import:
    class_package: str
    class_name: str
    outer: int
    name: str


@dataclass
class Export:
    index: int
    class_index: int
    super_index: int
    template_index: int
    outer: int
    name: str
    flags: int
    size: int
    offset: int
    class_name: str = ""
    path: str = ""  # Outer.Name inside the package


@dataclass
class Package:
    """A package's tables and data; ``payload(ext)`` fetches its ``.ubulk``/``.uptnl`` when a read needs it."""

    name: str  # /Game/Path/Name
    versions: Versions
    names: list[str]
    imports: list[Import]
    exports: list[Export]
    data: bytes  # .uasset followed by .uexp
    bulk_start: int = 0
    payload: Callable[[str], bytes | None] | None = None
    virtual: str = ""  # where the game keeps it (OakGame/Content/...uasset)
    files: object | None = None  # the GameFiles it came from, to follow imports
    _payloads: dict[str, bytes | None] = field(default_factory=dict, repr=False)

    # The same object API as the UE3 reader, so the property and material code reads both.
    @property
    def stem(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    def name_at(self, index: int, number: int = 0) -> str:
        if not 0 <= index < len(self.names):
            raise FormatError(f"name index {index} out of range")
        base = self.names[index]
        return f"{base}_{number - 1}" if number > 0 else base

    def object_name(self, ref: int) -> str:
        if ref > 0 and ref <= len(self.exports):
            return self.exports[ref - 1].name
        if ref < 0 and -ref <= len(self.imports):
            return self.imports[-ref - 1].name
        return "None"

    def object_class(self, ref: int) -> str:
        if ref > 0 and ref <= len(self.exports):
            return self.exports[ref - 1].class_name
        if ref < 0 and -ref <= len(self.imports):
            return self.imports[-ref - 1].class_name
        return ""

    def object_path(self, ref: int) -> str:
        parts = []
        seen = 0
        while ref and seen < 64:
            seen += 1
            if 0 < ref <= len(self.exports):
                e = self.exports[ref - 1]
                parts.append(e.name)
                ref = e.outer
            elif ref < 0 and -ref <= len(self.imports):
                i = self.imports[-ref - 1]
                parts.append(i.name)
                ref = i.outer
            else:
                break
        return ".".join(reversed(parts))

    def import_package(self, ref: int) -> str:
        """The package (``/Game/...``) an import lives in."""
        seen = 0
        while ref < 0 and seen < 64:
            seen += 1
            i = self.imports[-ref - 1]
            if i.outer == 0:
                return i.name
            ref = i.outer
        return self.name

    def find_export(self, name: str, class_name: str | None = None) -> Export | None:
        low = name.lower()
        for e in self.exports:
            if (e.path.lower() == low or e.name.lower() == low) and (class_name is None or e.class_name == class_name):
                return e
        return None

    def main_export(self, classes: tuple[str, ...] | None = None) -> Export | None:
        """The asset itself: the export named like the package (of one of ``classes``)."""
        stem = self.stem.lower()
        for e in self.exports:
            if e.outer == 0 and e.name.lower() == stem and (classes is None or e.class_name in classes):
                return e
        for e in self.exports:
            if e.outer == 0 and (classes is None or e.class_name in classes):
                return e
        return None

    def export_data(self, e: Export) -> Reader:
        if e.offset < 0 or e.offset + e.size > len(self.data):
            raise FormatError(f"{self.name}: {e.name}'s data lies outside the package (is its .uexp missing?)")
        r = Reader(self.data[e.offset:e.offset + e.size], self, base=e.offset)
        return r

    def payload_bytes(self, ext: str) -> bytes | None:
        if ext not in self._payloads:
            self._payloads[ext] = self.payload(ext) if self.payload else None
        return self._payloads[ext]

    @property
    def filter_editor_only(self) -> bool:
        return self.versions.filter_editor_only


# -- reading ----------------------------------------------------------------------

def _guid(r: Reader) -> tuple[int, int, int, int]:
    return struct.unpack("<4I", r.bytes(16))


def _engine_version(r: Reader) -> int:
    major, minor, _patch = r.u16(), r.u16(), r.u16()
    r.u32()
    r.fstring()
    return major * 100 + minor if major in (4, 5) else 0


def _engine_from_file_version(ue4: int, ue5: int) -> int:
    if ue5 >= 1000:
        return 500 + max(0, min(ue5 - 1000, 17) // 3)  # rough; zen packages carry no summary like this
    if ue4 >= 522:
        return 426
    if ue4 >= 520:
        return 425
    if ue4 >= 519:
        return 424
    if ue4 >= 517:
        return 422
    return 420


def read_package(name: str, uasset: bytes, uexp: bytes | None = None,
                 payload: Callable[[str], bytes | None] | None = None, game: str = "") -> Package:
    """A package from its ``.uasset`` and ``.uexp`` bytes; ``name`` is its ``/Game/...`` path."""
    r = Reader(uasset, None)
    if r.u32() != TAG:
        raise FormatError(f"{name} isn't an Unreal package")
    legacy = r.i32()
    if legacy >= 0:
        raise FormatError(f"{name} is an Unreal 3 package")
    v = Versions(game=game)
    if legacy != -4:
        r.i32()  # UE3 version
    v.ue4 = r.i32()
    if legacy <= -8:
        v.ue5 = r.i32()
    v.licensee = r.i32()
    if v.ue4 == 0 and v.ue5 == 0 and v.licensee == 0:
        raise FormatError(f"{name} is an unversioned package (it needs the game's type mappings)")
    if v.ue5 >= 1016:
        r.skip(20)  # saved hash
        header_size = r.i32()
    n = r.count(4096)
    for _ in range(n):
        if legacy < -5:
            guid = _guid(r)
            v.custom[guid] = r.i32()
        elif legacy < -2:
            guid = _guid(r)
            v.custom[guid] = r.i32()
            r.fstring()
        else:
            r.i32(), r.i32()
    if v.ue5 < 1016:
        header_size = r.i32()
    r.fstring()  # folder name
    flags = r.u32()
    v.filter_editor_only = bool(flags & PKG_FILTER_EDITOR_ONLY)
    name_count, name_offset = r.i32(), r.i32()
    if v.ue5 >= 1008:
        r.i32(), r.i32()  # soft object paths
    if not v.filter_editor_only and v.ue4 >= 516:
        r.fstring()  # localization id
    if v.ue4 >= 459:
        r.i32(), r.i32()  # gatherable text
    export_count, export_offset = r.i32(), r.i32()
    import_count, import_offset = r.i32(), r.i32()
    if v.ue5 >= 1015:
        r.skip(16)  # verse cells
    if v.ue5 >= 1014:
        r.i32()  # metadata offset
    r.i32()  # depends offset
    if v.ue4 >= 384:
        r.i32(), r.i32()  # soft package references
    if v.ue4 >= 510:
        r.i32()  # searchable names
    r.i32()  # thumbnail table
    if v.ue5 >= 1018:
        r.i32(), r.i32()
    if v.ue5 < 1016:
        r.skip(16)  # guid
    if not v.filter_editor_only:
        if v.ue4 >= 518:
            r.skip(16)
            if v.ue4 < 520:
                r.skip(16)
    gens = r.count(1 << 16)
    r.skip(8 * gens)
    engine = 0
    if v.ue4 >= 336:
        engine = _engine_version(r)
    else:
        r.i32()
    if v.ue4 >= 444:
        compatible = _engine_version(r)
        engine = engine or compatible
    v.engine = engine or _engine_from_file_version(v.ue4, v.ue5)
    compression = r.u32()
    if r.count(1 << 16):
        raise FormatError(f"{name} is a compressed package, which cooked games don't use")
    del compression
    r.u32()  # package source
    for _ in range(r.count(1 << 16)):
        r.fstring()
    if legacy > -7:
        if r.count(1 << 16):
            raise FormatError(f"{name}: unexpected texture allocations")
    r.i32()  # asset registry
    bulk_start = r.i64()
    del header_size
    if not (0 < name_offset < len(uasset)) and name_count:
        raise FormatError(f"{name}: bad name table position")
    pkg = Package(name, v, [], [], [], uasset + (uexp or b""), bulk_start, payload)
    r = Reader(uasset, pkg, name_offset)
    for _ in range(name_count):
        pkg.names.append(r.fstring())
        if v.ue4 >= 504:
            r.skip(4)
    r.pos = import_offset
    for _ in range(import_count):
        cp, cn = r.name(), r.name()
        outer = r.i32()
        nm = r.name()
        if v.ue4 >= 520 and not v.filter_editor_only:
            r.name()
        if v.ue5 >= 1003:
            r.i32()
        pkg.imports.append(Import(cp, cn, outer, nm))
    r.pos = export_offset
    for i in range(export_count):
        ci, si = r.i32(), r.i32()
        ti = r.i32() if v.ue4 >= 508 else 0
        outer = r.i32()
        nm = r.name()
        oflags = r.u32()
        if v.ue4 < 511:
            size, off = r.i32(), r.i32()
        else:
            size, off = r.i64(), r.i64()
        r.skip(12)  # forced export, not for client, not for server
        if v.ue5 < 1005:
            r.skip(16)  # package guid
        if v.ue5 >= 1006:
            r.i32()
        r.u32()  # package flags
        if v.ue4 >= 365:
            r.i32()
        if v.ue4 >= 485:
            r.i32()
        if v.ue5 >= 1003:
            r.i32()
        if v.ue4 >= 507:
            r.skip(20)
        if v.ue5 >= 1010:
            r.skip(16)
        pkg.exports.append(Export(i, ci, si, ti, outer, nm, oflags, size, off))
    for e in pkg.exports:
        e.class_name = pkg.object_name(e.class_index) if e.class_index else "Class"
        e.path = pkg.object_path(e.index + 1)
    return pkg


# -- bulk data --------------------------------------------------------------------

BULK_PAYLOAD_AT_END = 0x01
BULK_ZLIB = 0x02
BULK_UNUSED = 0x20
BULK_INLINE = 0x40
BULK_SEPARATE_FILE = 0x100
BULK_OPTIONAL = 0x800
BULK_MEMORY_MAPPED = 0x1000
BULK_SIZE64 = 0x2000
BULK_DUPLICATE_NON_OPTIONAL = 0x4000
BULK_BAD_DATA_VERSION = 0x8000
BULK_NO_OFFSET_FIXUP = 0x10000


@dataclass
class Bulk:
    flags: int
    count: int
    size: int
    offset: int  # raw, as stored

    @property
    def empty(self) -> bool:
        return self.size == 0 or bool(self.flags & BULK_UNUSED)


def read_bulk_header(r: Reader) -> Bulk:
    flags = r.u32()
    if flags & BULK_SIZE64:
        count, size = r.i64(), r.i64()
    else:
        count, size = r.i32(), r.u32()
    offset = r.i64()
    if flags & BULK_BAD_DATA_VERSION:
        r.skip(2)
    if flags & BULK_DUPLICATE_NON_OPTIONAL:
        r.skip(4 + (8 if flags & BULK_SIZE64 else 4) + 8)
    return Bulk(flags, count, size, offset)


def is_inline(h: Bulk) -> bool:
    return bool(h.flags & BULK_INLINE) or h.flags in (0, 1 << 18)


def read_bulk(r: Reader) -> bytes:
    """A ``FByteBulkData``'s bytes, from wherever they are kept (inline, later in the package, or the .ubulk)."""
    h = read_bulk_header(r)
    if h.empty:
        return b""
    if is_inline(h):
        data = r.bytes(h.size)
        return decompress_ue4_chunks(data) if h.flags & BULK_ZLIB else data
    return bulk_payload(r.pkg, h)


def skip_bulk(r: Reader) -> tuple[Bulk, int]:
    """Read a bulk header and step over inline data, for reading later with :func:`bulk_payload`."""
    h = read_bulk_header(r)
    pos = r.tell
    if not h.empty and is_inline(h):
        r.skip(h.size)
    return h, pos


def bulk_payload(pkg: Package, h: Bulk, inline_pos: int = -1) -> bytes:
    if h.empty:
        return b""
    if is_inline(h):
        src, pos = pkg.data, inline_pos
    else:
        start = h.offset if h.flags & BULK_NO_OFFSET_FIXUP else h.offset + pkg.bulk_start
        if h.flags & (BULK_SEPARATE_FILE | BULK_OPTIONAL):
            ext = ".uptnl" if h.flags & BULK_OPTIONAL else ".ubulk"
            if h.flags & BULK_MEMORY_MAPPED and not h.flags & BULK_OPTIONAL:
                src = pkg.payload_bytes(".m.ubulk") or pkg.payload_bytes(".ubulk")
            else:
                src = pkg.payload_bytes(ext)
            if src is None:
                raise FormatError(f"{pkg.name}: its {ext} file is missing")
            pos = 0 if len(src) == h.size else start - pkg.bulk_start
            if pos < 0 or pos + h.size > len(src):
                pos = h.offset
        else:
            src, pos = pkg.data, start
    if pos < 0 or pos + h.size > len(src):
        raise FormatError(f"{pkg.name}: bulk data outside its file")
    data = src[pos:pos + h.size]
    if h.flags & BULK_ZLIB:
        data = decompress_ue4_chunks(data)
    return data


def decompress_ue4_chunks(data: bytes) -> bytes:
    """``FArchive::SerializeCompressed`` (zlib): a header, per-block sizes, then the blocks."""
    r = Reader(data)
    tag = r.u64()
    if tag & 0xFFFFFFFF != TAG:
        return zlib.decompress(data)
    block = r.i64() or 0x20000
    total_c, total_u = r.i64(), r.i64()
    sizes = []
    n = (total_u + block - 1) // block
    for _ in range(n):
        sizes.append((r.i64(), r.i64()))
    out = bytearray()
    for c, _u in sizes:
        out += zlib.decompress(r.bytes(c))
    del total_c
    return bytes(out)


def bulk_array(r: Reader) -> tuple[int, int, bytes]:
    """``TArray::BulkSerialize``: element size, count, elements."""
    return r.bulk_array()


def strip_flags(r: Reader) -> tuple[int, int]:
    """``FStripDataFlags``: (global, class). Global 1 = editor data stripped, 2 = audio-visual data stripped."""
    return r.u8(), r.u8()


__all__ = ["Bulk", "CUSTOM_VERSIONS", "Export", "Import", "Package", "Versions", "bulk_array", "bulk_payload",
           "is_inline", "read_bulk", "read_bulk_header", "read_package", "skip_bulk", "strip_flags"]
