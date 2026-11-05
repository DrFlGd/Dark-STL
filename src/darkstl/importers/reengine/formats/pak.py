"""RE Engine ``.pak`` archives (magic ``KPKA``).

A pak is a table of contents followed by the files' data. The table does not store names: each
entry carries two MurmurHash3 values of the file's path (``natives/stm/.../x.mesh.221108797``),
one of the path in lower case and one in upper case, so that names come from a list the
modding community keeps for each game (one path per line). Entries are stored as they are, or
deflate- or zstd-compressed.

Supported: table versions 2 and 4, unencrypted tables and entries. Newer games encrypt the
table and some entries; those paks need an external unpacker, and the reader says so.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .binary import FormatError

MAGIC = b"KPKA"
FLAG_EXTRA_DATA = 1 << 2  # 9 extra bytes after the table (RE Requiem)
FLAG_ENCRYPTED_TABLE = 1 << 3
FLAG_EXTRA_U32 = 1 << 4
FLAG_CHUNK_TABLE = 1 << 5  # entries may give a chunk index instead of an offset
ATTR_CHUNK_INDEX = 1 << 24
COMPRESSION_NONE, COMPRESSION_DEFLATE, COMPRESSION_ZSTD = 0, 1, 2


@dataclass
class Entry:
    hash: int  # (upper-case hash << 32) | lower-case hash
    offset: int
    compressed_size: int
    size: int
    compression: int = 0
    encryption: int = 0
    chunked: bool = False
    name: str | None = None


@dataclass
class Pak:
    path: Path
    version: tuple[int, int]
    flags: int
    entries: list[Entry] = field(default_factory=list)
    _by_hash: dict[int, Entry] | None = field(default=None, repr=False)

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & FLAG_ENCRYPTED_TABLE)

    def find(self, name: str) -> Entry | None:
        if self._by_hash is None:
            self._by_hash = {e.hash: e for e in self.entries}
        return self._by_hash.get(int(hash_names([name])[0]))

    def name_entries(self, names: dict[int, str]) -> int:
        """Give entries their paths from a hash -> path table; returns how many got one."""
        n = 0
        for e in self.entries:
            e.name = names.get(e.hash)
            n += e.name is not None
        return n

    def read(self, entry: Entry) -> bytes:
        if entry.encryption:
            raise FormatError(f"{self.path.name}: this entry is encrypted; extract the archive with a pak tool first")
        if entry.chunked:
            raise FormatError(f"{self.path.name}: chunked entries are not supported; extract the archive first")
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(entry.compressed_size)
        if len(raw) < entry.compressed_size:
            raise FormatError(f"{self.path.name}: entry data is truncated")
        if entry.compression == COMPRESSION_NONE:
            return raw
        if entry.compression == COMPRESSION_DEFLATE:
            try:
                return zlib.decompressobj(-15).decompress(raw)
            except zlib.error:
                return zlib.decompress(raw)
        if entry.compression == COMPRESSION_ZSTD:
            try:
                import zstandard
            except ImportError as exc:  # pragma: no cover - a dependency of the package
                raise FormatError("zstd-compressed pak entry needs the 'zstandard' package") from exc
            return zstandard.ZstdDecompressor().decompress(raw, max_output_size=max(entry.size, 1))
        raise FormatError(f"{self.path.name}: unknown compression {entry.compression}")


def is_pak(data: bytes) -> bool:
    return data[:4] == MAGIC


def read_pak(path: str | os.PathLike) -> Pak:
    """The table of contents of a pak file (the data stays on disk)."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(16)
        if len(head) < 16 or head[:4] != MAGIC:
            raise FormatError(f"{path.name} is not an RE Engine pak file")
        major, minor, flags, count, _hash = struct.unpack_from("<BBHII", head, 4)
        if major not in (2, 4):
            raise FormatError(f"{path.name}: pak version {major}.{minor} is not supported")
        pak = Pak(path, (major, minor), flags)
        if flags & FLAG_ENCRYPTED_TABLE:
            return pak  # the table is encrypted: nothing can be listed
        if major == 2 and minor == 0:
            raw = f.read(24 * count)
            if len(raw) < 24 * count:
                raise FormatError(f"{path.name}: table of contents is truncated")
            table = np.frombuffer(raw, np.dtype([("lo", "<u4"), ("hi", "<u4"), ("offset", "<u8"), ("size", "<u8")]))
            for row in table:
                h = (int(row["hi"]) << 32) | int(row["lo"])
                pak.entries.append(Entry(h, int(row["offset"]), int(row["size"]), int(row["size"])))
        else:
            raw = f.read(48 * count)
            if len(raw) < 48 * count:
                raise FormatError(f"{path.name}: table of contents is truncated")
            table = np.frombuffer(raw, np.dtype([("lo", "<u4"), ("hi", "<u4"), ("offset", "<u8"), ("csize", "<u8"),
                                                 ("size", "<u8"), ("attr", "<u8"), ("checksum", "<u8")]))
            for row in table:
                h = (int(row["hi"]) << 32) | int(row["lo"])
                attr = int(row["attr"])
                pak.entries.append(Entry(h, int(row["offset"]), int(row["csize"]), int(row["size"]), attr & 0xF,
                                         (attr >> 16) & 0xFF, bool(attr & ATTR_CHUNK_INDEX)))
    return pak


# ---------------------------------------------------------------------------
# Names: MurmurHash3 (32-bit, seed 0xFFFFFFFF) of the UTF-16LE path, in lower and upper case


def _normalise(name: str) -> str:
    return name.strip().replace("\\", "/")


def hash_names(names: list[str]) -> np.ndarray:
    """The pak hash of each path: ``(murmur3(UPPER) << 32) | murmur3(lower)``, vectorised over all names.

    Only ASCII letters change case, as the engine does it."""
    if not names:
        return np.zeros(0, np.uint64)
    units = [np.frombuffer(_normalise(n).encode("utf-16-le"), "<u2") for n in names]
    lengths = np.array([len(u) for u in units], np.int64)  # in UTF-16 units
    width = int(lengths.max()) if len(lengths) else 0
    width += width % 2  # whole 4-byte blocks
    arr = np.zeros((len(units), max(width, 2)), np.uint32)
    for i, u in enumerate(units):
        arr[i, :len(u)] = u
    is_lower = (arr >= ord("a")) & (arr <= ord("z"))
    is_upper = (arr >= ord("A")) & (arr <= ord("Z"))
    lower = np.where(is_upper, arr + 32, arr)
    upper = np.where(is_lower, arr - 32, arr)
    hi = _murmur3_rows(upper, lengths).astype(np.uint64)
    lo = _murmur3_rows(lower, lengths).astype(np.uint64)
    return (hi << np.uint64(32)) | lo


def _rotl(x: np.ndarray, r: int) -> np.ndarray:
    return ((x << np.uint32(r)) | (x >> np.uint32(32 - r))) & np.uint32(0xFFFFFFFF)


def _murmur3_rows(units: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """MurmurHash3_x86_32 of each row of UTF-16 units (the first ``lengths[i]`` of row ``i``), seed 0xFFFFFFFF."""
    c1, c2 = np.uint32(0xCC9E2D51), np.uint32(0x1B873593)
    h = np.full(len(lengths), 0xFFFFFFFF, np.uint32)
    nbytes = (lengths * 2).astype(np.uint32)
    blocks = units.shape[1] // 2
    with np.errstate(over="ignore"):
        for b in range(blocks):
            active = lengths >= 2 * (b + 1)
            if not active.any():
                break
            k = (units[:, 2 * b] | (units[:, 2 * b + 1] << np.uint32(16))).astype(np.uint32)
            k = _rotl(k * c1, 15) * c2
            hb = _rotl(h ^ k, 13) * np.uint32(5) + np.uint32(0xE6546B64)
            h = np.where(active, hb, h)
        # a trailing unit (2 bytes) when the number of units is odd
        odd = (lengths % 2) == 1
        if odd.any():
            idx = np.clip(lengths - 1, 0, units.shape[1] - 1)
            k = units[np.arange(len(lengths)), idx].astype(np.uint32)
            k = _rotl(k * c1, 15) * c2
            h = np.where(odd, h ^ k, h)
        h ^= nbytes
        h ^= h >> np.uint32(16)
        h *= np.uint32(0x85EBCA6B)
        h ^= h >> np.uint32(13)
        h *= np.uint32(0xC2B2AE35)
        h ^= h >> np.uint32(16)
    return h


def read_name_list(path: str | os.PathLike) -> dict[int, str]:
    """A community file list (one path per line, ``#`` comments) as hash -> path."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    names = [_normalise(ln) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    hashes = hash_names(names)
    return {int(h): n for h, n in zip(hashes, names)}


__all__ = ["Entry", "Pak", "hash_names", "is_pak", "read_name_list", "read_pak"]
