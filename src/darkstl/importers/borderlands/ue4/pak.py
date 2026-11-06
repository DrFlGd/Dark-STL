"""Unreal Engine 4 ``.pak`` files (Borderlands 3, Tiny Tina's Wonderlands).

A pak ends with a footer (magic ``0x5A6F12E1``) that points at the index: the
mount point and every file's entry (offset, sizes, compression blocks, whether
it is encrypted). Versions 10 and later (UE 4.25+) keep the entries bit-packed
and the file names in a separate directory index. The index and the files can
be AES-256 encrypted (ECB, whole 16-byte blocks); the key is the game's. Blocks
are compressed with Oodle (the game's own library), zlib, gzip, LZ4 or zstd.

Layouts follow CUE4Parse (Apache 2.0) and UE Viewer (MIT).
"""

from __future__ import annotations

import gzip
import os
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ....errors import FormatError
from ...fromsoft.formats import oodle

MAGIC = 0x5A6F12E1


class R:
    """Little-endian reads over bytes."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError("read past the end of the index")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            if n > 1 << 16:
                raise FormatError("bad string in the index (wrong AES key?)")
            return self.take(n).rstrip(b"\0").decode("utf-8", "replace")
        n = -n
        if n > 1 << 16:
            raise FormatError("bad string in the index (wrong AES key?)")
        return self.take(2 * n).decode("utf-16-le", "replace").rstrip("\0")


def aes_decrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(data) % 16:
        data = data[:len(data) // 16 * 16]
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def parse_key(text: str) -> bytes:
    t = text.strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    try:
        key = bytes.fromhex(t)
    except ValueError:
        import base64
        try:
            key = base64.b64decode(t, validate=True)
        except ValueError:
            raise FormatError("An AES key is 64 hexadecimal digits (0x...)") from None
    if len(key) != 32:
        raise FormatError("An AES key is 32 bytes (64 hexadecimal digits)")
    return key


def decompress(method: str, data: bytes, size: int) -> bytes:
    m = method.lower()
    if m in ("", "none"):
        return data
    if m == "zlib":
        return zlib.decompress(data)
    if m == "gzip":
        return gzip.decompress(data)
    if m == "oodle":
        return oodle.oodle_decompress(data, size)
    if m == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    if m == "lz4":
        raise FormatError("LZ4-compressed Unreal files aren't supported yet")
    raise FormatError(f"Unknown compression {method!r}")


@dataclass
class Entry:
    name: str  # path below the mount point, forward slashes
    offset: int
    size: int
    usize: int
    method: str
    encrypted: bool
    blocks: list[tuple[int, int]]  # (start, end), absolute in the pak
    block_size: int


@dataclass
class Pak:
    path: Path
    version: int
    mount: str
    encrypted_index: bool
    methods: list[str]
    entries: dict[str, Entry] = field(default_factory=dict)  # lower-case path -> entry
    key: bytes | None = None
    method_u8: bool = False  # version 8 as Unreal 4.22 wrote it (four method names): the method is one byte
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def read(self, name: str) -> bytes:
        e = self.entries.get(name.lower())
        if e is None:
            raise FormatError(f"{name} isn't in {self.path.name}")
        return self.read_entry(e)

    def _raw(self, offset: int, size: int) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        if len(data) != size:
            raise FormatError(f"{self.path.name} ends early")
        return data

    def _decrypt(self, data: bytes) -> bytes:
        if self.key is None:
            raise FormatError(f"{self.path.name} is encrypted: it needs the game's AES key")
        return aes_decrypt(self.key, data)

    def read_entry(self, e: Entry) -> bytes:
        if not e.blocks or e.method.lower() in ("", "none"):
            start = e.offset + _entry_header_size(self.version, e, self.method_u8)
            n = _align(e.size) if e.encrypted else e.size
            data = self._raw(start, n)
            if e.encrypted:
                data = self._decrypt(data)
            return data[:e.size]
        out = bytearray()
        remaining = e.usize
        for start, end in e.blocks:
            n = end - start
            data = self._raw(start, _align(n) if e.encrypted else n)
            if e.encrypted:
                data = self._decrypt(data)[:n]
            want = min(e.block_size, remaining) if e.block_size else remaining
            out += decompress(e.method, data, want)
            remaining -= want
        return bytes(out[:e.usize])

    def names(self) -> list[str]:
        return [e.name for e in self.entries.values()]


def _align(n: int) -> int:
    return (n + 15) & ~15


def _entry_header_size(version: int, e: Entry, method_u8: bool = False) -> int:
    size = 8 + 8 + 8 + 20
    size += 1 if method_u8 else 4
    if version <= 1:
        size += 8
    if version >= 3:
        if e.method.lower() not in ("", "none"):
            size += 4 + 16 * len(e.blocks)
        size += 1 + 4
    return size


def _read_footer(f, file_size: int) -> tuple[int, bool, int, int, list[str]]:
    """(version, encrypted index, index offset, index size, compression methods). For version 8 with four
    method names (Unreal 4.22) the methods list ends with ``"#u8"``."""
    for names in (5, 4, 0):
        for frozen in (0, 1):
            for guid in (16, 0):
                size = guid + 1 + 4 + 4 + 8 + 8 + 20 + frozen + 32 * names
                if size > file_size:
                    continue
                f.seek(file_size - size)
                data = f.read(size)
                pos = guid
                enc = data[pos]
                magic, version = struct.unpack_from("<Ii", data, pos + 1)
                if magic != MAGIC:
                    continue
                if (version >= 8) != bool(names) or (version == 9) != bool(frozen) or (version >= 7) != bool(guid):
                    continue
                off, isize = struct.unpack_from("<qq", data, pos + 9)
                methods = ["None"]
                base = pos + 9 + 16 + 20 + frozen
                for i in range(names):
                    raw = data[base + 32 * i:base + 32 * (i + 1)].split(b"\0", 1)[0]
                    if raw:
                        methods.append(raw.decode("ascii", "replace"))
                if version == 8 and names == 4:
                    methods.append("#u8")
                return version, bool(enc), off, isize, methods
    raise FormatError("not an Unreal pak file")


_LEGACY_FLAGS = {0: "None", 1: "Zlib", 2: "Gzip", 4: "Oodle", 0x10: "Zlib", 0x20: "Gzip"}


def _legacy_entry(r: R, version: int, methods: list[str], name: str) -> Entry:
    offset, size, usize = r.i64(), r.i64(), r.i64()
    if version >= 8:
        mi = r.u8() if methods[-1:] == ["#u8"] else r.u32()
        method = methods[mi] if mi < len(methods) else f"#{mi}"
    else:
        flags = r.i32()
        method = _LEGACY_FLAGS.get(flags & 0xFF, "Oodle" if flags else "None")
    if version <= 1:
        r.i64()
    r.take(20)
    blocks, encrypted, block_size = [], False, 0
    if version >= 3:
        if method.lower() != "none":
            for _ in range(r.i32()):
                blocks.append((r.i64(), r.i64()))
        encrypted = bool(r.u8() & 1)
        block_size = r.u32()
    e = Entry(name, offset, size, usize, method, encrypted, blocks, block_size)
    if version >= 5:  # block offsets relative to the entry
        e.blocks = [(offset + a, offset + b) for a, b in blocks]
    return e


def _encoded_entry(data: bytes, pos: int, version: int, methods: list[str], name: str) -> Entry:
    r = R(data, pos)
    value = r.u32()
    block_size = (value & 0x3F) << 11
    if (value & 0x3F) == 0x3F:
        block_size = r.u32()
    mi = (value >> 23) & 0x3F
    offset = r.u32() if (value >> 31) & 1 else r.u64()
    usize = r.u32() if (value >> 30) & 1 else r.u64()
    size = (r.u32() if (value >> 29) & 1 else r.u64()) if mi else usize
    encrypted = bool((value >> 22) & 1)
    count = (value >> 6) & 0xFFFF
    method = methods[mi] if mi < len(methods) else f"#{mi}"
    e = Entry(name, offset, size, usize, method, encrypted, [], block_size)
    if count:
        header = _entry_header_size(version, Entry(name, 0, 0, 0, method, encrypted, [(0, 0)] * count, 0))
        start = offset + header
        if count == 1 and not encrypted:
            e.blocks = [(start, start + size)]
        else:
            for _ in range(count):
                n = r.u32()
                e.blocks.append((start, start + n))
                start += _align(n) if encrypted else n
    if not block_size and count == 1:
        e.block_size = usize
    return e


def open_pak(path: str | os.PathLike, keys: list[bytes] = ()) -> Pak:
    """Read a pak's index; ``keys`` are tried in turn when the index is encrypted."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        version, enc, off, isize, methods = _read_footer(f, size)
        if off < 0 or isize <= 0 or off + isize > size:
            raise FormatError(f"{path.name}: bad index position")
        f.seek(off)
        raw = f.read(isize)
    candidates = list(keys) if enc else [None]
    if enc and not candidates:
        raise FormatError(f"{path.name} has an encrypted index: it needs the game's AES key")
    last: Exception | None = None
    for key in candidates:
        u8 = methods[-1:] == ["#u8"]
        pak = Pak(path, version, "", enc, [m for m in methods if m != "#u8"], key=key, method_u8=u8)
        try:
            index = aes_decrypt(key, raw) if enc else raw
            _read_index(pak, index)
            return pak
        except (FormatError, struct.error, UnicodeDecodeError, ValueError) as e:
            last = e
    if enc:
        raise FormatError(f"{path.name}: none of the AES keys opens its index") from last
    raise FormatError(f"{path.name}: {last}") from last


def _read_index(pak: Pak, index: bytes) -> None:
    r = R(index)
    pak.mount = r.fstring()
    if not pak.mount.startswith("../") and not pak.mount.startswith("/") and pak.mount not in ("", "/"):
        if any(ord(c) < 32 for c in pak.mount):
            raise FormatError("bad mount point (wrong AES key?)")
    count = r.i32()
    if count < 0 or count > 10_000_000:
        raise FormatError("bad entry count (wrong AES key?)")
    if pak.version < 10:
        for _ in range(count):
            name = r.fstring()
            e = _legacy_entry(r, pak.version, pak.methods + (["#u8"] if pak.method_u8 else []), name)
            pak.entries[name.lower()] = e
        return
    r.u64()  # path hash seed
    if r.i32():
        r.i64(), r.i64()
        r.take(20)
    has_full = r.i32()
    full_off = full_size = 0
    if has_full:
        full_off, full_size = r.i64(), r.i64()
        r.take(20)
    n = r.i32()
    encoded = r.take(n)
    files = r.i32()
    plain = [_legacy_entry(r, pak.version, pak.methods, "") for _ in range(files)]
    if not has_full:
        raise FormatError(f"{pak.path.name} has no directory index: its file names aren't known")
    with open(pak.path, "rb") as f:
        f.seek(full_off)
        raw = f.read(full_size)
    d = R(aes_decrypt(pak.key, raw) if pak.encrypted_index else raw)
    for _ in range(d.i32()):
        directory = d.fstring()
        for _ in range(d.i32()):
            fname = d.fstring()
            loc = d.i32()
            name = (directory + fname).lstrip("/")
            if loc >= 0:
                e = _encoded_entry(encoded, loc, pak.version, pak.methods, name)
            elif -loc - 1 < len(plain):
                p = plain[-loc - 1]
                e = Entry(name, p.offset, p.size, p.usize, p.method, p.encrypted, p.blocks, p.block_size)
            else:
                continue
            pak.entries[name.lower()] = e


def is_pak_file(path: Path) -> bool:
    if Path(path).suffix.lower() != ".pak":
        return False
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            _read_footer(f, size)
        return True
    except (OSError, FormatError):
        return False


__all__ = ["Entry", "MAGIC", "Pak", "aes_decrypt", "decompress", "is_pak_file", "open_pak", "parse_key"]
