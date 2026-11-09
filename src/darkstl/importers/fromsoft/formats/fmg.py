"""FMG text files: the games' names and descriptions, by id.

The games keep their text in ``msg/<language>/item.msgbnd.dcx`` (and ``menu``), binders of FMG files:
``WeaponName.fmg`` holds each weapon's name under its id in ``EquipParamWeapon``, ``ProtectorName.fmg``
the armour's, ``NpcName.fmg`` the characters' (Dark Souls names them in Japanese: ``武器名.fmg``,
``防具名.fmg``, ``NPC名.fmg``). The layout follows SoulsFormats' FMG reader:

* header: ``00``, big-endian flag, version (0 Demon's Souls, 1 Dark Souls, 2 Dark Souls III and later:
  "wide", 64-bit offsets), ``00``, file size, ``01``, ``00``/``FF``, ``00 00``, group count, string count,
  (wide: ``FF 00 00 00``), offset of the string offsets, zero;
* groups: (first string index, first id, last id) runs, each id in the run naming one string offset
  (0: no text); strings are zero-ended UTF-16.
"""

from __future__ import annotations

import struct

from .binary import FormatError

WIDE_VERSION = 2


def is_fmg(data: bytes) -> bool:
    return len(data) >= 0x1C and data[0] == 0 and data[1] in (0, 1) and data[2] in (0, 1, 2) and data[3] == 0 \
        and data[8] == 1


def _utf16(data: bytes, offset: int, big: bool) -> str:
    end = offset
    while end + 1 < len(data) and (data[end] or data[end + 1]):
        end += 2
    return data[offset:end].decode("utf-16-be" if big else "utf-16-le", "replace")


def read_fmg(data: bytes) -> dict[int, str]:
    """{id: text} of an FMG file (ids without text are left out)."""
    if not is_fmg(data):
        raise FormatError("Not an FMG text file")
    big = bool(data[1])
    version = data[2]
    wide = version == WIDE_VERSION
    e = ">" if big else "<"
    try:
        group_count, _string_count = struct.unpack_from(e + "ii", data, 0x0C)
        if wide:
            (offsets_at,) = struct.unpack_from(e + "q", data, 0x18)
            groups_at, group_size, offset_size, offset_fmt = 0x28, 16, 8, "q"
        else:
            (offsets_at,) = struct.unpack_from(e + "i", data, 0x14)
            groups_at, group_size, offset_size, offset_fmt = 0x1C, 12, 4, "i"
        if group_count < 0 or groups_at + group_count * group_size > len(data):
            raise FormatError("FMG group table runs past the end of the file")
        out: dict[int, str] = {}
        for g in range(group_count):
            index, first, last = struct.unpack_from(e + "iii", data, groups_at + g * group_size)
            count = last - first + 1
            if count <= 0 or count > 1_000_000:
                continue
            at = offsets_at + index * offset_size
            if at < 0 or at + count * offset_size > len(data):
                raise FormatError("FMG string offsets run past the end of the file")
            offsets = struct.unpack_from(f"{e}{count}{offset_fmt}", data, at)
            for j, off in enumerate(offsets):
                if 0 < off < len(data):
                    out[first + j] = _utf16(data, off, big)
        return out
    except struct.error as exc:
        raise FormatError(f"FMG file cut short: {exc}") from exc


def write_fmg(entries: dict[int, str], version: int = WIDE_VERSION, big_endian: bool = False) -> bytes:
    """An FMG file holding ``entries`` (for tests and tools): one group per run of consecutive ids."""
    wide = version == WIDE_VERSION
    e = ">" if big_endian else "<"
    ids = sorted(entries)
    runs: list[list[int]] = []
    for i in ids:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    header_size = 0x28 if wide else 0x1C
    group_size = 16 if wide else 12
    offset_size = 8 if wide else 4
    offsets_at = header_size + len(runs) * group_size
    strings_at = offsets_at + len(ids) * offset_size
    strings = bytearray()
    string_offsets = []
    for i in ids:
        string_offsets.append(strings_at + len(strings))
        strings += entries[i].encode("utf-16-be" if big_endian else "utf-16-le") + b"\0\0"
    out = bytearray()
    out += bytes([0, 1 if big_endian else 0, version, 0])
    out += struct.pack(e + "i", strings_at + len(strings))
    out += bytes([1, 0xFF if version == 0 else 0, 0, 0])
    out += struct.pack(e + "ii", len(runs), len(ids))
    if wide:
        out += struct.pack(e + "i", 0xFF) + struct.pack(e + "qq", offsets_at, 0)
    else:
        out += struct.pack(e + "ii", offsets_at, 0)
    index = 0
    for run in runs:
        out += struct.pack(e + "iii", index, run[0], run[-1]) + (b"\0" * 4 if wide else b"")
        index += len(run)
    for off in string_offsets:
        out += struct.pack(e + ("q" if wide else "i"), off)
    return bytes(out + strings)


__all__ = ["is_fmg", "read_fmg", "write_fmg"]
