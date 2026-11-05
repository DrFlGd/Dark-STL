"""Little-endian struct reading for the RE Engine formats, which are little-endian throughout."""

from __future__ import annotations

import struct

from ....errors import FormatError  # noqa: F401 - re-exported for the format modules


class Reader:
    def __init__(self, data: bytes | bytearray | memoryview, offset: int = 0):
        self.data = data if isinstance(data, bytes) else bytes(data)
        self.pos = offset

    def __len__(self) -> int:
        return len(self.data)

    def seek(self, pos: int) -> None:
        self.pos = pos

    def skip(self, count: int) -> None:
        self.pos += count

    def align(self, alignment: int) -> None:
        rem = self.pos % alignment
        if rem:
            self.pos += alignment - rem

    def unpack(self, fmt: str) -> tuple:
        fmt = "<" + fmt
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise FormatError(f"Unexpected end of file at offset {self.pos}")
        out = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return out

    def u8(self) -> int:
        return self.unpack("B")[0]

    def i8(self) -> int:
        return self.unpack("b")[0]

    def u16(self) -> int:
        return self.unpack("H")[0]

    def i16(self) -> int:
        return self.unpack("h")[0]

    def u32(self) -> int:
        return self.unpack("I")[0]

    def i32(self) -> int:
        return self.unpack("i")[0]

    def u64(self) -> int:
        return self.unpack("Q")[0]

    def f32(self) -> float:
        return self.unpack("f")[0]

    def bytes(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise FormatError(f"Unexpected end of file at offset {self.pos}")
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def cstring(self, offset: int) -> str:
        """A zero-terminated UTF-8 string at ``offset``."""
        end = self.data.find(b"\0", offset)
        if end < 0 or offset < 0 or offset > len(self.data):
            raise FormatError(f"String at offset {offset} runs past the end of the file")
        return self.data[offset:end].decode("utf-8", errors="replace")

    def wstring(self, offset: int) -> str:
        """A zero-terminated UTF-16LE string at ``offset``."""
        if offset < 0 or offset > len(self.data):
            raise FormatError(f"String at offset {offset} runs past the end of the file")
        end = offset
        while end + 1 < len(self.data) and self.data[end:end + 2] != b"\0\0":
            end += 2
        return self.data[offset:end].decode("utf-16-le", errors="replace")
