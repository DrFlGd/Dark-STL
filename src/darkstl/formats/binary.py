"""Small endian-aware binary reader used by all format parsers.

FromSoftware files mix little- and big-endian data (PS3/X360 era files are
big-endian) and switch endianness after reading a header flag, so the reader
keeps the current byte order as mutable state.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from typing import Iterator

import numpy as np


class FormatError(ValueError):
    """Raised when a file does not match the format being parsed."""


class BinaryReader:
    def __init__(self, data: bytes | bytearray | memoryview, big_endian: bool = False, offset: int = 0):
        self.data = data if isinstance(data, bytes) else bytes(data)
        self.pos = offset
        self.big_endian = big_endian

    # -- state -------------------------------------------------------------

    @property
    def endian(self) -> str:
        return ">" if self.big_endian else "<"

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

    @contextmanager
    def step_in(self, offset: int) -> Iterator["BinaryReader"]:
        saved = self.pos
        self.pos = offset
        try:
            yield self
        finally:
            self.pos = saved

    # -- primitive reads ---------------------------------------------------

    def unpack(self, fmt: str) -> tuple:
        fmt = self.endian + fmt
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise FormatError(f"Read of {size} bytes at 0x{self.pos:X} runs past end of data (0x{len(self.data):X})")
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return values

    def unpack_at(self, offset: int, fmt: str) -> tuple:
        fmt = self.endian + fmt
        if offset + struct.calcsize(fmt) > len(self.data):
            raise FormatError(f"Read at 0x{offset:X} runs past end of data")
        return struct.unpack_from(fmt, self.data, offset)

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

    def i64(self) -> int:
        return self.unpack("q")[0]

    def f32(self) -> float:
        return self.unpack("f")[0]

    def boolean(self) -> bool:
        return self.u8() != 0

    def vec2(self) -> tuple[float, float]:
        return self.unpack("2f")

    def vec3(self) -> tuple[float, float, float]:
        return self.unpack("3f")

    def read_bytes(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise FormatError(f"Read of {count} bytes at 0x{self.pos:X} runs past end of data")
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def get_bytes(self, offset: int, count: int) -> bytes:
        if offset < 0 or offset + count > len(self.data):
            raise FormatError(f"Slice 0x{offset:X}+0x{count:X} is outside the data (0x{len(self.data):X})")
        return self.data[offset:offset + count]

    def ascii(self, count: int) -> str:
        return self.read_bytes(count).decode("ascii", errors="replace")

    def fixed_str(self, count: int) -> str:
        return self.read_bytes(count).split(b"\0", 1)[0].decode("ascii", errors="replace")

    def assert_ascii(self, *options: str) -> str:
        size = len(options[0].encode("ascii"))
        value = self.ascii(size)
        if value not in options:
            raise FormatError(f"Expected one of {options!r} at 0x{self.pos - size:X}, got {value!r}")
        return value

    def assert_value(self, value, *options):
        if value not in options:
            raise FormatError(f"Unexpected value {value!r} at 0x{self.pos:X}; expected one of {options!r}")
        return value

    # -- arrays ------------------------------------------------------------

    def array(self, dtype: str, count: int, offset: int | None = None) -> np.ndarray:
        """Read ``count`` items of a numpy scalar dtype code (e.g. 'u2', 'f4')."""
        dt = np.dtype(self.endian + dtype)
        start = self.pos if offset is None else offset
        end = start + dt.itemsize * count
        if end > len(self.data):
            raise FormatError(f"Array read at 0x{start:X} runs past end of data")
        out = np.frombuffer(self.data, dtype=dt, count=count, offset=start).astype(dt.newbyteorder("="))
        if offset is None:
            self.pos = end
        return out

    # -- strings -----------------------------------------------------------

    def get_cstring(self, offset: int, encoding: str = "cp932") -> str:
        end = self.data.find(b"\0", offset)
        if end < 0:
            end = len(self.data)
        return self.data[offset:end].decode(encoding, errors="replace")

    def get_utf16(self, offset: int) -> str:
        data = self.data
        end = offset
        while end + 1 < len(data) and (data[end] or data[end + 1]):
            end += 2
        codec = "utf-16-be" if self.big_endian else "utf-16-le"
        return data[offset:end].decode(codec, errors="replace")

    def get_shift_jis(self, offset: int) -> str:
        return self.get_cstring(offset, "cp932")

    def get_string(self, offset: int, unicode: bool) -> str:
        if offset <= 0 or offset >= len(self.data):
            return ""
        return self.get_utf16(offset) if unicode else self.get_shift_jis(offset)


def reverse_bits(value: int) -> int:
    """Reverse the bit order of a byte (binder format/flag bytes are stored reversed)."""
    result = 0
    for i in range(8):
        if value & (1 << i):
            result |= 1 << (7 - i)
    return result
