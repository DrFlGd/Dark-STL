"""PARAM files: the games' data tables (``EquipParamWeapon.param``, ``NpcParam.param`` ...).

A param is a list of rows, each an id and a fixed-size block of fields whose layout (the "paramdef")
isn't in the file. Dark STL reads a few fields at offsets known per game (see :mod:`..names`), so only
the row table is read here. Layout as SoulsFormats' PARAM reader:

* ``0x00`` strings offset (u32), ``0x04`` data start (u16, or 0), ``0x06`` unk, ``0x08`` paramdef data
  version, ``0x0A`` row count (u16), ``0x0C`` param type (32 bytes of text, or with flag 0x80: zero,
  a 64-bit offset to it, 0x14 zero bytes), ``0x2C`` big-endian (``FF``) or not, ``0x2D`` format flags
  (0x01, 0x02: a 32-bit data start follows at 0x30; 0x04: 64-bit offsets, a 64-bit data start at 0x30),
  ``0x2E`` more flags, ``0x2F`` paramdef format version;
* rows from 0x30 (0x40 with the longer header): id, data offset, name offset (32-bit; with 64-bit
  offsets: id, 4 bytes, then the two offsets as 64-bit).

The row size is the distance between two rows' data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .binary import FormatError

FLAG_01 = 0x01
FLAG_INT_DATA_OFFSET = 0x02
FLAG_LONG_DATA_OFFSET = 0x04
FLAG_OFFSET_PARAM_TYPE = 0x80


@dataclass
class Param:
    type: str
    big_endian: bool
    rows: list[tuple[int, int]]  # (row id, offset of its data in ``data``)
    row_size: int
    data: bytes

    def _fmt(self, kind: str) -> str:
        return (">" if self.big_endian else "<") + kind

    def value(self, data_offset: int, field_offset: int, kind: str):
        """A field of a row (``kind``: a struct code such as ``"H"``, ``"i"``)."""
        return struct.unpack_from(self._fmt(kind), self.data, data_offset + field_offset)[0]

    def column(self, field_offset: int, kind: str) -> list[tuple[int, object]]:
        """(row id, the field's value) for every row whose data holds the field."""
        size = struct.calcsize(kind)
        if field_offset + size > self.row_size:
            return []
        out = []
        for row_id, at in self.rows:
            if at + field_offset + size <= len(self.data):
                out.append((row_id, self.value(at, field_offset, kind)))
        return out


def is_param(data: bytes) -> bool:
    return len(data) >= 0x30 and data[0x2C] in (0, 0xFF)


def read_param(data: bytes) -> Param:
    if len(data) < 0x30:
        raise FormatError("PARAM file is too short")
    big = data[0x2C] == 0xFF
    if data[0x2C] not in (0, 0xFF):
        raise FormatError("Not a PARAM file")
    flags = data[0x2D]
    e = ">" if big else "<"
    (strings_offset,) = struct.unpack_from(e + "I", data, 0)
    (row_count,) = struct.unpack_from(e + "H", data, 0x0A)
    if flags & FLAG_OFFSET_PARAM_TYPE:
        (type_at,) = struct.unpack_from(e + "q", data, 0x10)
        end = data.find(b"\0", type_at) if 0 <= type_at < len(data) else -1
        ptype = data[type_at:end].decode("ascii", "replace") if end > 0 else ""
    else:
        ptype = data[0x0C:0x2C].split(b"\0", 1)[0].decode("ascii", "replace")
    long_offsets = bool(flags & FLAG_LONG_DATA_OFFSET)
    rows_at = 0x40 if long_offsets or (flags & FLAG_01 and flags & FLAG_INT_DATA_OFFSET) else 0x30
    row_size = 24 if long_offsets else 12
    if rows_at + row_count * row_size > len(data):
        raise FormatError(f"PARAM {ptype or ''} row table runs past the end of the file")
    rows = []
    for i in range(row_count):
        at = rows_at + i * row_size
        if long_offsets:
            row_id, _pad, data_offset, _name = struct.unpack_from(e + "iiqq", data, at)
        else:
            row_id, data_offset, _name = struct.unpack_from(e + "iII", data, at)
        rows.append((row_id, int(data_offset)))
    starts = sorted({off for _i, off in rows})
    if len(starts) > 1:
        size = min(b - a for a, b in zip(starts, starts[1:]))
    elif starts:
        size = max(0, (strings_offset or len(data)) - starts[0])
    else:
        size = 0
    return Param(ptype, big, rows, size, data)


def write_param(ptype: str, rows: list[tuple[int, bytes]], big_endian: bool = False, long_offsets: bool = True) -> bytes:
    """A PARAM file with ``rows`` [(id, row data)] (for tests and tools): the Dark Souls III / Elden Ring
    layout with 64-bit offsets and the param type kept apart, or the older 32-bit one."""
    e = ">" if big_endian else "<"
    flags = (FLAG_LONG_DATA_OFFSET | FLAG_OFFSET_PARAM_TYPE) if long_offsets else 0
    rows_at = 0x40 if long_offsets else 0x30
    entry = 24 if long_offsets else 12
    data_at = rows_at + len(rows) * entry
    blob = b"".join(r for _i, r in rows)
    strings_at = data_at + len(blob)
    header = bytearray(rows_at)
    struct.pack_into(e + "I", header, 0, strings_at)
    struct.pack_into(e + "H", header, 0x0A, len(rows))
    if long_offsets:
        struct.pack_into(e + "q", header, 0x10, strings_at)
        struct.pack_into(e + "q", header, 0x30, data_at)
    else:
        name = ptype.encode("ascii")[:31]
        header[0x0C:0x0C + len(name)] = name
        struct.pack_into(e + "H", header, 0x04, data_at)
    header[0x2C] = 0xFF if big_endian else 0
    header[0x2D] = flags
    header[0x2E] = 0
    header[0x2F] = 5 if long_offsets else 3
    table = bytearray()
    at = data_at
    for row_id, row in rows:
        if long_offsets:
            table += struct.pack(e + "iiqq", row_id, 0, at, 0)
        else:
            table += struct.pack(e + "iII", row_id, at, 0)
        at += len(row)
    tail = (ptype.encode("ascii") + b"\0") if long_offsets else b""
    return bytes(header + table + blob + tail)


__all__ = ["Param", "is_param", "read_param", "write_param"]
