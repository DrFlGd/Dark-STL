"""id Tech 7/8 ``.streamdb`` files: the large mips of textures and the full levels of detail of models.

A header (magic, header length, entry count, flags) and a table of entries: a 64-bit id, an offset in 16-byte
units and a length. A piece is Oodle-compressed when its length differs from the size the resource expects.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ....errors import FormatError

MAGIC = 0x61C7F32E29C2A550
ENTRY = np.dtype([("id", "<u8"), ("offset16", "<u4"), ("length", "<u4")])
_MASK64 = (1 << 64) - 1


@dataclass
class StreamDb:
    path: Path
    ids: np.ndarray  # sorted
    offsets: np.ndarray  # bytes
    lengths: np.ndarray

    def find(self, ident: int) -> tuple[int, int] | None:
        i = int(np.searchsorted(self.ids, np.uint64(ident & _MASK64)))
        if i < len(self.ids) and int(self.ids[i]) == ident & _MASK64:
            return int(self.offsets[i]), int(self.lengths[i])
        return None


def is_streamdb(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return len(head) == 8 and struct.unpack("<Q", head)[0] == MAGIC


def read_streamdb(path: str | Path) -> StreamDb:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(32)
        if len(head) < 32 or struct.unpack_from("<Q", head)[0] != MAGIC:
            raise FormatError(f"{path.name} isn't a StreamDB file")
        count = struct.unpack_from("<I", head, 24)[0]
        rows = np.frombuffer(f.read(count * ENTRY.itemsize), ENTRY)
    if len(rows) < count:
        raise FormatError(f"{path.name} is cut short")
    # the first copy of an id wins, as the game reads them
    ids, first = np.unique(rows["id"], return_index=True)
    return StreamDb(path, ids, rows["offset16"][first].astype(np.int64) * 16, rows["length"][first].astype(np.int64))


def read_piece(db: StreamDb, ident: int, size: int) -> bytes | None:
    """The piece ``ident`` decompressed to ``size`` bytes, or None when this file doesn't hold it."""
    found = db.find(ident)
    if found is None:
        return None
    offset, length = found
    with open(db.path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    if size > 0 and length != size:
        from ...fromsoft.formats.oodle import oodle_decompress

        return oodle_decompress(data, size)
    return data


def eternal_id(hash_: int, n: int) -> int:
    """DOOM Eternal: a resource's piece ``n`` (level of detail, or ``mipCount - mipLevel`` for a mip)."""
    return ((hash_ << 4) | n) & _MASK64


def _fmix64(k: int) -> int:
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & _MASK64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & _MASK64
    k ^= k >> 33
    return k


def _combine(seed: int, value: int) -> int:
    return (seed ^ ((value + 0x9E3779B9 + (seed << 6) + (seed >> 2)) & _MASK64)) & _MASK64


def darkages_id(hash_: int, a: int, b: int = 0) -> int:
    """DOOM: The Dark Ages: ``hash_combine(hash, hash_combine(fmix64(a), fmix64(b)))``."""
    return _combine(hash_ & _MASK64, _combine(_fmix64(a & 0xFFFFFFFF), _fmix64(b & 0xFFFFFFFF)))
