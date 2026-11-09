"""The games' text (``american.gxt``): what vehicle and weapon names read as in the game.

- GTA III: ``TKEY`` (12-byte entries: offset into ``TDAT``, 8-byte key) and ``TDAT`` (UTF-16).
- Vice City: ``TABL`` (12-byte entries: 8-byte table name, offset), each table (after an 8-byte
  name, except the first, ``MAIN``) a ``TKEY`` and ``TDAT`` as in III.
- San Andreas: a u16 version (4) and bits per character (8 or 16), then ``TABL`` as in Vice
  City, but ``TKEY`` entries are 8 bytes: offset and the CRC32 of the upper-case key.

Keys are looked up case-insensitively; San Andreas' by their hash.
"""

from __future__ import annotations

import struct
import zlib

from ....errors import FormatError


def key_hash(key: str) -> int:
    """San Andreas' key hash: CRC32 without the final inversion, of the upper-case key."""
    return (~zlib.crc32(key.upper().encode("latin-1"))) & 0xFFFFFFFF


class GxtText:
    def __init__(self, by_key: dict[str, str], by_hash: dict[int, str]):
        self.by_key = by_key
        self.by_hash = by_hash

    def get(self, key: str) -> str | None:
        if not key:
            return None
        found = self.by_key.get(key.upper())
        if found is None and self.by_hash:
            h = key_hash(key)
            found = self.by_hash.get(h)
            if found is None:
                found = self.by_hash.get(zlib.crc32(key.upper().encode("latin-1")) & 0xFFFFFFFF)
        return found

    def __len__(self) -> int:
        return len(self.by_key) + len(self.by_hash)


def _tag(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 8 > len(data):
        raise FormatError("GXT is cut short")
    return data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]


def _utf16(data: bytes, start: int, end: int) -> str:
    pos = start
    while pos + 1 < end and data[pos:pos + 2] != b"\0\0":
        pos += 2
    return data[start:pos].decode("utf-16-le", "replace")


def _bytes8(data: bytes, start: int, end: int) -> str:
    stop = data.find(b"\0", start, end)
    return data[start:stop if stop >= 0 else end].decode("latin-1")


def _keys_tdat(data: bytes, pos: int, hashed: bool, wide: bool, by_key: dict, by_hash: dict) -> None:
    tag, size = _tag(data, pos)
    if tag != b"TKEY":
        raise FormatError(f"GXT: expected TKEY, found {tag!r}")
    keys = data[pos + 8:pos + 8 + size]
    tpos = pos + 8 + size
    tag, tsize = _tag(data, tpos)
    if tag != b"TDAT":
        raise FormatError("GXT: TKEY without TDAT")
    tdat, tend = tpos + 8, tpos + 8 + tsize
    read = _utf16 if wide else _bytes8
    if hashed:
        for i in range(len(keys) // 8):
            off, h = struct.unpack_from("<II", keys, i * 8)
            by_hash.setdefault(h, read(data, tdat + off, tend))
    else:
        for i in range(len(keys) // 12):
            off = struct.unpack_from("<I", keys, i * 12)[0]
            key = keys[i * 12 + 4:i * 12 + 12].split(b"\0", 1)[0].decode("latin-1").upper()
            by_key.setdefault(key, read(data, tdat + off, tend))


def read_gxt(data: bytes) -> GxtText:
    by_key: dict[str, str] = {}
    by_hash: dict[int, str] = {}
    try:
        if data[:4] == b"TKEY":
            _keys_tdat(data, 0, False, True, by_key, by_hash)
            return GxtText(by_key, by_hash)
        sa = data[4:8] == b"TABL" and struct.unpack_from("<H", data, 0)[0] in (4, 6)
        start = 4 if sa else 0
        wide = (struct.unpack_from("<H", data, 2)[0] == 16) if sa else True
        tag, size = _tag(data, start)
        if tag != b"TABL":
            raise FormatError("Not a GXT file")
        table = data[start + 8:start + 8 + size]
        for i in range(len(table) // 12):
            name = table[i * 12:i * 12 + 8].split(b"\0", 1)[0]
            off = struct.unpack_from("<I", table, i * 12 + 8)[0]
            pos = off if name == b"MAIN" else off + 8
            try:
                _keys_tdat(data, pos, sa, wide, by_key, by_hash)
            except FormatError:
                continue
    except struct.error as exc:
        raise FormatError(f"GXT is cut short: {exc}") from exc
    return GxtText(by_key, by_hash)
