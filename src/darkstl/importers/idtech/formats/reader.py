"""A small little-endian reader for id Tech's binary resources."""

from __future__ import annotations

import struct

from ....errors import FormatError


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def unpack(self, fmt: str):
        s = struct.Struct("<" + fmt)
        if self.pos + s.size > len(self.data):
            raise FormatError("the file is cut short")
        out = s.unpack_from(self.data, self.pos)
        self.pos += s.size
        return out

    def u8(self) -> int:
        return self.unpack("B")[0]

    def u16(self) -> int:
        return self.unpack("H")[0]

    def i16(self) -> int:
        return self.unpack("h")[0]

    def u32(self) -> int:
        return self.unpack("I")[0]

    def i32(self) -> int:
        return self.unpack("i")[0]

    def f32(self) -> float:
        return self.unpack("f")[0]

    def vec(self, n: int) -> tuple[float, ...]:
        return self.unpack(f"{n}f")

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise FormatError("the file is cut short")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def string(self) -> str:
        """A string after its 32-bit length."""
        n = self.u32()
        if n > 1 << 20:
            raise FormatError("a string in the file is too long to be one")
        return self.bytes(n).decode("utf-8", "replace").rstrip("\0")
