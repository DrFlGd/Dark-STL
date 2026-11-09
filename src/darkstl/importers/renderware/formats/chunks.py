"""RenderWare binary streams: a tree of chunks.

Every chunk is ``type, size, library id`` (three little-endian u32) and then ``size`` bytes, which
are either raw data or more chunks. The library id says which RenderWare version wrote the file;
the layouts that changed between versions are chosen from it (see :func:`version_of`). Format
from the gtamods.com wiki and librw (MIT).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from ....errors import FormatError

STRUCT = 0x01
STRING = 0x02
EXTENSION = 0x03
TEXTURE = 0x06
MATERIAL = 0x07
MATERIAL_LIST = 0x08
FRAME_LIST = 0x0E
GEOMETRY = 0x0F
CLUMP = 0x10
ATOMIC = 0x14
TEXTURE_NATIVE = 0x15
TEXTURE_DICTIONARY = 0x16
GEOMETRY_LIST = 0x1A
HANIM_PLG = 0x11E
SKIN_PLG = 0x116
MATFX_PLG = 0x120
BINMESH_PLG = 0x50E
NATIVE_DATA_PLG = 0x510
FRAME_NAME = 0x0253F2FE  # Rockstar's node name extension

NAMES = {CLUMP: "Clump", TEXTURE_DICTIONARY: "Texture Dictionary", GEOMETRY: "Geometry"}


@dataclass
class Chunk:
    type: int
    offset: int  # where its data starts in the buffer
    size: int
    library: int
    data: memoryview  # the whole buffer (the chunk's data is data[offset:offset + size])

    @property
    def version(self) -> int:
        return version_of(self.library)

    @property
    def body(self) -> memoryview:
        return self.data[self.offset:self.offset + self.size]

    def children(self) -> Iterator[Chunk]:
        """The chunks this chunk holds (its data read as chunks, as far as they fit)."""
        yield from iter_chunks(self.data, self.offset, self.offset + self.size)

    def child(self, kind: int) -> Chunk | None:
        for c in self.children():
            if c.type == kind:
                return c
        return None

    def struct(self) -> Chunk:
        """The Struct chunk every container begins with."""
        c = self.child(STRUCT)
        if c is None:
            raise FormatError(f"RenderWare chunk 0x{self.type:x} has no Struct")
        return c

    def string(self) -> str:
        """A String chunk's text (NUL-terminated, padded)."""
        raw = bytes(self.body)
        return raw.split(b"\0", 1)[0].decode("latin-1")


def version_of(library: int) -> int:
    """The RenderWare version (``0x36003`` for 3.6.0.3) a chunk's library id stands for."""
    if library & 0xFFFF0000:
        return ((library >> 14) & 0x3FF00) + 0x30000 | ((library >> 16) & 0x3F)
    return library << 8


def library_id(version: int, build: int = 0xFFFF) -> int:
    """The library id for a version (as files from 3.1 on write it); the inverse of :func:`version_of`."""
    if version < 0x31000:
        return version >> 8
    v = version - 0x30000
    return ((v & 0x3FF00) << 14) | ((v & 0x3F) << 16) | build


def iter_chunks(data, start: int = 0, end: int | None = None) -> Iterator[Chunk]:
    view = memoryview(data)
    end = len(view) if end is None else min(end, len(view))
    pos = start
    while pos + 12 <= end:
        kind, size, lib = struct.unpack_from("<3I", view, pos)
        if pos + 12 + size > end:
            break
        yield Chunk(kind, pos + 12, size, lib, view)
        pos += 12 + size


def read_root(data: bytes, expected: int | None = None) -> Chunk:
    """The first chunk of a file (of type ``expected`` if given)."""
    if len(data) < 12:
        raise FormatError("Not a RenderWare file: too short")
    kind, size, lib = struct.unpack_from("<3I", data, 0)
    if expected is not None and kind != expected:
        raise FormatError(f"Not a RenderWare {NAMES.get(expected, hex(expected))} (chunk 0x{kind:x})")
    if 12 + size > len(data):
        size = len(data) - 12  # some files (and archive members padded to sectors) lie by a little
    return Chunk(kind, 12, size, lib, memoryview(data))


def peek_type(data: bytes) -> int | None:
    if len(data) < 12:
        return None
    kind, size, lib = struct.unpack_from("<3I", data, 0)
    v = version_of(lib)
    if not 0x30000 <= v < 0x40000:
        return None
    return kind


def is_dff(data: bytes) -> bool:
    return peek_type(data) == CLUMP


def is_txd(data: bytes) -> bool:
    return peek_type(data) == TEXTURE_DICTIONARY


class Reader:
    """Little-endian reads from a chunk's data."""

    def __init__(self, data, pos: int = 0, end: int | None = None):
        self.data = memoryview(data)
        self.pos = pos
        self.end = len(self.data) if end is None else end

    @classmethod
    def of(cls, chunk: Chunk) -> Reader:
        return cls(chunk.data, chunk.offset, chunk.offset + chunk.size)

    def left(self) -> int:
        return self.end - self.pos

    def take(self, n: int) -> memoryview:
        if self.pos + n > self.end:
            raise FormatError("RenderWare chunk is cut short")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.pos + size > self.end:
            raise FormatError("RenderWare chunk is cut short")
        out = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return out

    def u32(self) -> int:
        return self.unpack("I")[0]

    def i32(self) -> int:
        return self.unpack("i")[0]

    def f32(self) -> float:
        return self.unpack("f")[0]
