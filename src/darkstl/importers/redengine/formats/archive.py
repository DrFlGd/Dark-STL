"""The game's ``.archive`` packs (``RDAR``).

An archive is a flat list of files keyed by the FNV-1a hash of their depot
path (``base\\weapons\\...\\w_rifle.mesh``, lower case, backslashes); the
paths themselves are not stored. Each file is one or more segments: the
resource itself, then its buffers (vertex data, pixels), each stored raw or
Kraken-compressed (``KARK``). Names are recovered from the files that refer
to one another (a mesh names its textures and materials, an entity its
meshes), which :mod:`..catalog` collects while scanning.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ...fromsoft.formats.binary import FormatError
from .cr2w import KARK, decompress_buffer

MAGIC = b"RDAR"
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3


def fnv1a64(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def normalize_path(path: str) -> str:
    """Depot paths as the engine hashes them: lower case, backslashes, no doubled separators."""
    p = path.strip().replace("/", "\\").lower()
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    return p.lstrip("\\")


def path_hash(path: str) -> int:
    return fnv1a64(normalize_path(path))


@dataclass(frozen=True)
class Segment:
    offset: int
    zsize: int
    size: int


@dataclass(frozen=True)
class Entry:
    hash: int
    segments_start: int
    segments_end: int
    sha1: bytes

    @property
    def segment_count(self) -> int:
        return self.segments_end - self.segments_start


class Archive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[int, Entry] = {}
        self.segments: list[Segment] = []
        self.names: dict[int, str] = {}  # hash -> depot path, as far as known
        self._read_index()

    def _read_index(self) -> None:
        with open(self.path, "rb") as f:
            head = f.read(40)
            if head[:4] != MAGIC:
                raise FormatError(f"{self.path.name} is not a REDengine archive")
            _version, index_pos, index_size = struct.unpack_from("<IQI", head, 4)
            f.seek(index_pos)
            index = f.read(index_size)
        (_table_off, _table_size, _crc, n_entries, n_segments, _n_deps) = struct.unpack_from("<IIQIII", index, 0)
        pos = 28
        for _ in range(n_entries):
            h, _ts, _inline, s_start, s_end, _d_start, _d_end = struct.unpack_from("<QqIIIII", index, pos)
            sha1 = index[pos + 36:pos + 56]
            pos += 56
            self.entries.setdefault(h, Entry(h, s_start, s_end, sha1))
        for _ in range(n_segments):
            off, zsize, size = struct.unpack_from("<QII", index, pos)
            pos += 16
            self.segments.append(Segment(off, zsize, size))

    # -- names -----------------------------------------------------------------

    def name_of(self, h: int) -> str:
        return self.names.get(h) or f"{h:016x}"

    def learn(self, paths) -> int:
        """Remember the depot paths of the files in this archive among ``paths``; returns how many were new."""
        new = 0
        for p in paths:
            n = normalize_path(p)
            h = fnv1a64(n)
            if h in self.entries and h not in self.names:
                self.names[h] = n
                new += 1
        return new

    def find(self, path: str) -> Entry | None:
        return self.entries.get(path_hash(path))

    # -- data ------------------------------------------------------------------

    def _segment_bytes(self, f, seg: Segment, decompress: bool) -> bytes:
        f.seek(seg.offset)
        raw = f.read(seg.zsize)
        if len(raw) < seg.zsize:
            raise FormatError(f"{self.path.name} is truncated")
        if not decompress or seg.zsize == seg.size:
            return raw
        if raw[:4] != KARK:
            raise FormatError(f"A segment of {self.path.name} uses an unknown compression")
        return decompress_buffer(raw, seg.size)

    def read(self, h: int, buffers: bool = True) -> bytes:
        """A file as WolvenKit would extract it: the resource decompressed, its buffers as stored (the CR2W
        reader decompresses each when it is used), so that the resource's buffer table still points at them."""
        e = self.entries.get(h)
        if e is None:
            raise FormatError(f"File {h:016x} is not in {self.path.name}")
        with open(self.path, "rb") as f:
            out = self._segment_bytes(f, self.segments[e.segments_start], True)
            if buffers:
                for i in range(e.segments_start + 1, e.segments_end):
                    out += self._segment_bytes(f, self.segments[i], False)
        return out

    def read_head(self, h: int) -> bytes:
        """Only the resource part of a file (no buffers), for reading its tables."""
        return self.read(h, buffers=False)

    def size_of(self, h: int) -> int:
        e = self.entries[h]
        return sum(self.segments[i].size for i in range(e.segments_start, e.segments_end))

    def __len__(self) -> int:
        return len(self.entries)


def is_archive_file(path: Path) -> bool:
    path = Path(path)
    if path.suffix.lower() != ".archive" or not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


@lru_cache(maxsize=16)
def _open(path: str, size: int, mtime: int) -> Archive:
    return Archive(Path(path))


def open_archive(path: str | os.PathLike) -> Archive:
    """An archive's index, kept open across calls (re-read when the file changes)."""
    p = Path(path)
    st = p.stat()
    return _open(str(p), st.st_size, st.st_mtime_ns)


__all__ = ["Archive", "Entry", "Segment", "fnv1a64", "is_archive_file", "normalize_path", "open_archive",
           "path_hash"]
