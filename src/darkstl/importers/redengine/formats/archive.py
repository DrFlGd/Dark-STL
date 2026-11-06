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
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import numpy as np

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


class Entries(Mapping):
    """The archive's files by hash, held as arrays: the big archives list hundreds of thousands of files,
    and the game has dozens of archives, which all stay open (a dict of objects per file would cost
    hundreds of megabytes and take seconds to build)."""

    def __init__(self, hashes: np.ndarray, starts: np.ndarray, ends: np.ndarray, sha1: np.ndarray,
                 order: np.ndarray):
        self._hashes = hashes  # sorted, for lookups
        self._starts = starts
        self._ends = ends
        self._sha1 = sha1
        self._order = order  # positions in the order the archive lists them

    def _find(self, h) -> int:
        try:
            h = int(h)
        except (TypeError, ValueError):
            return -1
        if not 0 <= h <= 0xFFFFFFFFFFFFFFFF:
            return -1
        i = int(np.searchsorted(self._hashes, np.uint64(h)))
        return i if i < len(self._hashes) and int(self._hashes[i]) == h else -1

    def _entry(self, i: int) -> Entry:
        return Entry(int(self._hashes[i]), int(self._starts[i]), int(self._ends[i]), bytes(self._sha1[i]))

    def __contains__(self, h) -> bool:
        return self._find(h) >= 0

    def __getitem__(self, h) -> Entry:
        i = self._find(h)
        if i < 0:
            raise KeyError(h)
        return self._entry(i)

    def get(self, h, default=None):
        i = self._find(h)
        return self._entry(i) if i >= 0 else default

    def __iter__(self):
        return (int(self._hashes[i]) for i in self._order)

    def values(self):
        return (self._entry(int(i)) for i in self._order)

    def __len__(self) -> int:
        return len(self._hashes)


_ENTRY = np.dtype([("hash", "<u8"), ("ts", "<i8"), ("inline", "<u4"), ("s_start", "<u4"), ("s_end", "<u4"),
                   ("d_start", "<u4"), ("d_end", "<u4"), ("sha1", "V20")])
_SEGMENT = np.dtype([("offset", "<u8"), ("zsize", "<u4"), ("size", "<u4")])


class Archive:
    def __init__(self, path: Path):
        self.path = Path(path)
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
        if len(index) < 28:
            raise FormatError(f"{self.path.name} is truncated")
        (_table_off, _table_size, _crc, n_entries, n_segments, _n_deps) = struct.unpack_from("<IIQIII", index, 0)
        end = 28 + n_entries * _ENTRY.itemsize + n_segments * _SEGMENT.itemsize
        if end > len(index):
            raise FormatError(f"{self.path.name} is truncated")
        rows = np.frombuffer(index, _ENTRY, n_entries, 28)
        seg = np.frombuffer(index, _SEGMENT, n_segments, 28 + n_entries * _ENTRY.itemsize)
        # one entry per hash, the first listed (as a dict's setdefault would keep), sorted for lookups
        hashes, first = np.unique(rows["hash"], return_index=True)
        self.entries = Entries(hashes, rows["s_start"][first].copy(), rows["s_end"][first].copy(),
                               rows["sha1"][first].copy(), np.argsort(first, kind="stable"))
        self._seg = seg.copy()

    @property
    def segments(self) -> list[Segment]:
        return [Segment(int(o), int(z), int(s)) for o, z, s in self._seg.tolist()]

    def _segment(self, i: int) -> Segment:
        o, z, s = self._seg[i].tolist()
        return Segment(int(o), int(z), int(s))

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
            out = self._segment_bytes(f, self._segment(e.segments_start), True)
            if buffers:
                for i in range(e.segments_start + 1, e.segments_end):
                    out += self._segment_bytes(f, self._segment(i), False)
        return out

    def read_head(self, h: int) -> bytes:
        """Only the resource part of a file (no buffers), for reading its tables."""
        return self.read(h, buffers=False)

    def size_of(self, h: int) -> int:
        e = self.entries[h]
        return int(self._seg["size"][e.segments_start:e.segments_end].sum())

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


# Every archive of the game stays open: a model's materials and textures are looked for in all of them
# (Cyberpunk has 50 or more), and the names learned while scanning live on these objects. With fewer kept,
# each lookup read the indexes of the archives that had been let go again, one after another, and a model
# took minutes to open.
@lru_cache(maxsize=1024)
def _open(path: str, size: int, mtime: int) -> Archive:
    return Archive(Path(path))


def open_archive(path: str | os.PathLike) -> Archive:
    """An archive's index, kept open across calls (re-read when the file changes)."""
    p = Path(path)
    st = p.stat()
    return _open(str(p), st.st_size, st.st_mtime_ns)


__all__ = ["Archive", "Entries", "Entry", "Segment", "fnv1a64", "is_archive_file", "normalize_path", "open_archive",
           "path_hash"]
