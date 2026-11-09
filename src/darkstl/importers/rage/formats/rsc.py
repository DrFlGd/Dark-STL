"""RSC7 resources: a "system" part and a "graphics" part, deflated together, with pointers between blocks.

A pointer ``0x5xxxxxxx`` points into the system part and ``0x6xxxxxxx`` into the graphics part. The sizes
of the parts are given by page flags (in the archive entry, or in an exported file's ``RSC7`` header). The
resource version tells the file kind's layout apart, which is how GTA V Enhanced's files (the "gen9"
layouts) are told from the original PC release's. Layout from CodeWalker (MIT).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from ....errors import FormatError

MAGIC = 0x37435352  # "RSC7"
SYSTEM = 0x50000000
GRAPHICS = 0x60000000

# resource versions: the original PC release, and GTA V Enhanced
LEGACY = {".ydr": 165, ".ydd": 165, ".yft": 162, ".ytd": 13, ".ycd": 46}
ENHANCED = {".ydr": (159, 154), ".ydd": (159,), ".yft": (171,), ".ytd": (5,)}


def size_from_flags(flags: int) -> int:
    s = (((flags >> 27) & 0x1) << 0) + (((flags >> 26) & 0x1) << 1) + (((flags >> 25) & 0x1) << 2) \
        + (((flags >> 24) & 0x1) << 3) + (((flags >> 17) & 0x7F) << 4) + (((flags >> 11) & 0x3F) << 5) \
        + (((flags >> 7) & 0xF) << 6) + (((flags >> 5) & 0x3) << 7) + (((flags >> 4) & 0x1) << 8)
    return (0x200 << (flags & 0xF)) * s


def flags_for(size: int) -> int:
    """Page flags giving at least ``size`` bytes (runs of sixteen pages; for writing test files)."""
    shift = 0
    while True:
        unit = (0x200 << shift) * 16
        n = (size + unit - 1) // unit
        if n <= 0x7F:
            return (n << 17) | shift
        shift += 1


@dataclass
class Resource:
    version: int
    system: bytes
    graphics: bytes
    ptr_size: int = 8  # GTA IV's resources (RSC5) point with 4 bytes

    def reader(self, gen9: bool = False) -> Reader:
        return Reader(self, gen9)


def is_enhanced(ext: str, version: int) -> bool:
    return version in ENHANCED.get(ext.lower(), ())


def split(data: bytes, version: int, sys_flags: int, gfx_flags: int) -> Resource:
    ss = size_from_flags(sys_flags)
    gs = size_from_flags(gfx_flags)
    ss = min(ss, len(data))
    return Resource(version, bytes(data[:ss]), bytes(data[ss:ss + gs]))


def parse_file(data: bytes) -> Resource:
    """An exported resource file (``RSC7`` header, then the deflated parts)."""
    if len(data) < 16:
        raise FormatError("Not a GTA V resource: too short")
    magic, version, sysf, gfxf = struct.unpack_from("<4I", data, 0)
    if magic != MAGIC:
        raise FormatError("Not a GTA V resource (no RSC7 header)")
    try:
        body = zlib.decompress(data[16:], -15)
    except zlib.error:
        try:
            body = zlib.decompress(data[16:])
        except zlib.error as exc:
            raise FormatError(f"The resource couldn't be inflated ({exc})") from exc
    return split(body, version, sysf, gfxf)


def is_resource_file(data: bytes) -> bool:
    return len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == MAGIC


class Reader:
    """Reads through a resource's pointers."""

    def __init__(self, res: Resource, gen9: bool = False):
        self.res = res
        self.gen9 = gen9

    def _at(self, ptr: int, n: int) -> tuple[bytes, int]:
        if ptr & 0xF0000000 == SYSTEM:
            buf, off = self.res.system, ptr & 0x0FFFFFFF
        elif ptr & 0xF0000000 == GRAPHICS:
            buf, off = self.res.graphics, ptr & 0x0FFFFFFF
        else:
            raise FormatError(f"The resource has a bad pointer (0x{ptr:x})")
        if off + n > len(buf):
            raise FormatError(f"The resource is cut short at 0x{ptr:x}")
        return buf, off

    def bytes(self, ptr: int, n: int) -> bytes:
        if n <= 0:
            return b""
        buf, off = self._at(ptr, n)
        return buf[off:off + n]

    def view(self, ptr: int, n: int) -> memoryview:
        buf, off = self._at(ptr, n)
        return memoryview(buf)[off:off + n]

    def unpack(self, fmt: str, ptr: int) -> tuple:
        size = struct.calcsize("<" + fmt)
        buf, off = self._at(ptr, size)
        return struct.unpack_from("<" + fmt, buf, off)

    def u8(self, ptr: int) -> int:
        return self.unpack("B", ptr)[0]

    def u16(self, ptr: int) -> int:
        return self.unpack("H", ptr)[0]

    def i16(self, ptr: int) -> int:
        return self.unpack("h", ptr)[0]

    def u32(self, ptr: int) -> int:
        return self.unpack("I", ptr)[0]

    def u64(self, ptr: int) -> int:
        return self.unpack("Q", ptr)[0]

    def f32(self, ptr: int) -> float:
        return self.unpack("f", ptr)[0]

    def string(self, ptr: int) -> str:
        if not ptr:
            return ""
        buf, off = self._at(ptr, 1)
        end = buf.find(b"\0", off)
        if end < 0:
            end = len(buf)
        return buf[off:end].decode("utf-8", "replace")

    def ptr(self, at: int) -> int:
        """The pointer stored at ``at`` (8 bytes in GTA V, 4 in GTA IV)."""
        return self.u64(at) if self.res.ptr_size == 8 else self.u32(at)

    def pointers(self, ptr: int, n: int) -> list[int]:
        if not ptr or n <= 0:
            return []
        return list(self.unpack(f"{n}{'Q' if self.res.ptr_size == 8 else 'I'}", ptr))

    def pointer_list(self, at: int) -> list[int]:
        """A ``(pointer, count u16, capacity u16)`` list header at ``at``: the pointers it lists."""
        ptr, count = self.unpack(("Q" if self.res.ptr_size == 8 else "I") + "H", at)
        return self.pointers(ptr, count)

    def ok(self, ptr: int) -> bool:
        return ptr & 0xF0000000 in (SYSTEM, GRAPHICS)
