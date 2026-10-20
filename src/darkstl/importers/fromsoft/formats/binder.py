"""BND3/BND4 archives and their split BXF3/BXF4 (header + data) variants.

Binders are the archives that hold a model's parts: a ``.partsbnd`` holds the
weapon's FLVER mesh and its TPF textures, a ``.chrbnd`` holds a character's
mesh, skeleton and animations, a ``.texbnd`` holds textures, and so on.
Split binders keep their file table in a ``*bhd`` file and the data in a
matching ``*bdt`` file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import dcx
from .binary import BinaryReader, FormatError, reverse_bits

# Binder.Format flags (after bit-order normalisation)
FMT_BIG_ENDIAN = 0x01
FMT_IDS = 0x02
FMT_NAMES1 = 0x04
FMT_NAMES2 = 0x08
FMT_LONG_OFFSETS = 0x10
FMT_COMPRESSION = 0x20

BINDER_MAGICS = (b"BND3", b"BND4", b"BHF3", b"BHF4")


@dataclass
class BinderFile:
    id: int
    name: str
    flags: int
    _loader: Callable[[], bytes] = field(repr=False)
    _data: bytes | None = field(default=None, repr=False)

    @property
    def data(self) -> bytes:
        if self._data is None:
            raw = self._loader()
            self._data = dcx.decompress(raw)[0] if self.flags & 0x01 else raw
        return self._data

    @property
    def basename(self) -> str:
        return self.name.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class Binder:
    kind: str
    version: str
    files: list[BinderFile]

    def find(self, predicate: Callable[[BinderFile], bool]) -> list[BinderFile]:
        return [f for f in self.files if predicate(f)]

    def by_extension(self, *exts: str) -> list[BinderFile]:
        exts = tuple(e.lower() for e in exts)
        return [f for f in self.files if f.basename.lower().endswith(exts)]


def _has(fmt: int, flag: int) -> bool:
    return bool(fmt & flag)


def _has_names(fmt: int) -> bool:
    return bool(fmt & (FMT_NAMES1 | FMT_NAMES2))


def _read_format(raw: int, bit_big_endian: bool) -> int:
    reverse = bit_big_endian or ((raw & 1) != 0 and (raw & 0x80) == 0)
    return raw if reverse else reverse_bits(raw)


def _read_file_flags(raw: int, bit_big_endian: bool) -> int:
    return raw if bit_big_endian else reverse_bits(raw)


def _slice_loader(data: bytes, offset: int, size: int) -> Callable[[], bytes]:
    def load() -> bytes:
        if offset < 0 or offset + size > len(data):
            raise FormatError(f"Binder entry 0x{offset:X}+0x{size:X} is outside the data")
        return data[offset:offset + size]
    return load


def _read_bnd3_style(br: BinaryReader, magic: str) -> tuple[str, int, list[tuple]]:
    br.assert_ascii(magic)
    version = br.fixed_str(8)
    bit_big_endian = br.data[0x0E] != 0
    fmt = _read_format(br.u8(), bit_big_endian)
    big_endian = br.boolean()
    br.skip(2)
    br.big_endian = big_endian or _has(fmt, FMT_BIG_ENDIAN)
    count = br.i32()
    br.skip(12)
    headers = []
    for _ in range(count):
        flags = _read_file_flags(br.u8(), bit_big_endian)
        br.skip(3)
        compressed_size = br.i32()
        offset = br.i64() if _has(fmt, FMT_LONG_OFFSETS) else br.u32()
        file_id = br.i32() if _has(fmt, FMT_IDS) else -1
        name = br.get_shift_jis(br.i32()) if _has_names(fmt) else ""
        if _has(fmt, FMT_COMPRESSION):
            br.i32()  # uncompressed size
        headers.append((file_id, name, flags, offset, compressed_size))
    return version, fmt, headers


def _read_bnd4_style(br: BinaryReader, magic: str) -> tuple[str, int, list[tuple]]:
    br.assert_ascii(magic)
    br.skip(5)  # unk04, unk05, 3 zero bytes
    big_endian = br.boolean()
    bit_big_endian = not br.boolean()
    br.skip(1)
    br.big_endian = big_endian
    count = br.i32()
    br.i64()  # header size (0x40)
    version = br.fixed_str(8)
    br.i64()  # file header size
    br.i64()  # headers end
    unicode = br.boolean()
    fmt = _read_format(br.u8(), bit_big_endian)
    br.skip(2)  # extended, 0
    br.skip(4)
    br.i64()  # hash table offset
    headers = []
    for _ in range(count):
        flags = _read_file_flags(br.u8(), bit_big_endian)
        br.skip(3)
        br.i32()  # -1
        compressed_size = br.i64()
        if _has(fmt, FMT_COMPRESSION):
            br.i64()  # uncompressed size
        offset = br.i64() if _has(fmt, FMT_LONG_OFFSETS) else br.u32()
        file_id = br.i32() if _has(fmt, FMT_IDS) else -1
        name = br.get_string(br.u32(), unicode) if _has_names(fmt) else ""
        if fmt == FMT_NAMES1:
            file_id = br.i32()
            br.i32()
        headers.append((file_id, name, flags, offset, compressed_size))
    return version, fmt, headers


def is_binder(data: bytes) -> bool:
    return data[:4] in BINDER_MAGICS


def read_binder(data: bytes, bdt: bytes | Callable[[], bytes] | None = None) -> Binder:
    """Parse a BND3/BND4 archive, or a BHF3/BHF4 header with its BDT data.

    ``data`` may still be DCX-compressed. For split binders ``bdt`` is the
    data file's bytes (or a callable that loads them on first access).
    """
    data = dcx.maybe_decompress(data)
    magic = data[:4]
    br = BinaryReader(data)
    if magic == b"BND3":
        version, _, headers = _read_bnd3_style(br, "BND3")
        source = data
        kind = "BND3"
    elif magic == b"BND4":
        version, _, headers = _read_bnd4_style(br, "BND4")
        source = data
        kind = "BND4"
    elif magic in (b"BHF3", b"BHF4"):
        if bdt is None:
            raise FormatError(f"{magic.decode()} header needs its matching BDT data file")
        if magic == b"BHF3":
            version, _, headers = _read_bnd3_style(br, "BHF3")
        else:
            version, _, headers = _read_bnd4_style(br, "BHF4")
        kind = "BXF" + magic.decode()[3]
        source = None
    else:
        raise FormatError(f"Not a binder (magic {magic!r})")

    files = []
    if source is not None:
        for file_id, name, flags, offset, size in headers:
            files.append(BinderFile(file_id, name, flags, _slice_loader(source, offset, size)))
    else:
        cache: dict[str, bytes] = {}

        def bdt_bytes() -> bytes:
            if "bdt" not in cache:
                cache["bdt"] = bdt() if callable(bdt) else bdt
            return cache["bdt"]

        for file_id, name, flags, offset, size in headers:
            def load(offset=offset, size=size) -> bytes:
                return _slice_loader(bdt_bytes(), offset, size)()
            files.append(BinderFile(file_id, name, flags, load))
    return Binder(kind, version, files)
