"""RSC5 resources: GTA IV's models and textures (``.wdr``, ``.wdd``, ``.wft``, ``.wtd``).

The same design as GTA V's RSC7 (a system and a graphics part, pointers ``0x5xxxxxxx`` and
``0x6xxxxxxx`` between their blocks), but with 4-byte pointers and a 12-byte header: magic ``RSC\\x05``,
the resource type (8 textures, 110 drawables and dictionaries, 112 fragments) and flags giving each part's
size (``a << (b + 8)``), then the parts deflated with a zlib header. Layout as RageLib (SparkIV) documents it.
"""

from __future__ import annotations

import struct
import zlib

from ....errors import FormatError
from .rsc import Resource

MAGIC = 0x05435352  # "RSC\x05"
MAGIC_BE = 0x52534305  # the Xbox 360's big-endian files
TEXTURES, MODEL, FRAGMENT = 8, 110, 112


def system_size(flags: int) -> int:
    return (flags & 0x7FF) << (((flags >> 11) & 0xF) + 8)


def graphics_size(flags: int) -> int:
    return ((flags >> 15) & 0x7FF) << (((flags >> 26) & 0xF) + 8)


def flags_for(system: int, graphics: int) -> int:
    """Flags giving at least these sizes (for writing test files)."""
    def part(size: int) -> tuple[int, int]:
        a, b = (size + 0xFF) >> 8, 0
        while a > 0x7FF:
            a, b = (a + 1) >> 1, b + 1
        return a, b

    sa, sb = part(system)
    ga, gb = part(graphics)
    return sa | (sb << 11) | (ga << 15) | (gb << 26)


def is_resource_file(data: bytes) -> bool:
    return len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == MAGIC


def parse_file(data: bytes) -> Resource:
    if len(data) < 12:
        raise FormatError("Not a GTA IV resource: too short")
    magic, kind, flags = struct.unpack_from("<3I", data, 0)
    if magic == MAGIC_BE:
        raise FormatError("This is an Xbox 360 resource; only the PC's are read")
    if magic != MAGIC:
        raise FormatError("Not a GTA IV resource (no RSC5 header)")
    try:
        body = zlib.decompress(data[12:])
    except zlib.error:
        try:
            body = zlib.decompress(data[14:], -15)
        except zlib.error as exc:
            raise FormatError(f"The resource couldn't be inflated ({exc})") from exc
    ss, gs = system_size(flags), graphics_size(flags)
    return Resource(kind, bytes(body[:ss]), bytes(body[ss:ss + gs]), 4)
