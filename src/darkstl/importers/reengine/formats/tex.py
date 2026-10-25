"""RE Engine ``.tex`` textures (magic ``TEX\\0``).

A tex file is a small header, one 16-byte entry per mip of each image (offset, scanline length
and size), then the raw DXGI-format surfaces, the same block data a DDS file carries. Rows
("scanlines") can be padded: when a mip's stored size is larger than its pixels need, each row
is ``scanline`` bytes apart and the surplus is skipped.

Versions (from the file extension, ``.tex.10`` for RE2, ``.tex.143221013`` for RE4...) change two
things: whether the mip count is given directly or as the size of the mip table, and whether the
header carries eight extra bytes of swizzle data (RE8 and later). Textures of Monster Hunter
Wilds and later compress each mip with GDeflate, which is not supported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....textures import dds
from .binary import FormatError, Reader

MAGIC = 0x00584554  # "TEX\0"
VERSION_RE3 = 190820018  # keeps the old header layout despite its large number
GDEFLATE_VERSIONS = {241106027, 250813143, 251111100}  # MH Wilds, Pragmata/RE Requiem, MH Stories 3

# tex version -> game, for recognising extracted folders
VERSIONS: dict[int, str] = {
    10: "Resident Evil 2", 11: "Devil May Cry 5", 190820018: "Resident Evil 3", 28: "Monster Hunter Rise",
    30: "Resident Evil Village", 34: "Resident Evil 2", 35: "Resident Evil 7", 143221013: "Resident Evil 4",
    241101895: "Street Fighter 6", 760230703: "Dragon's Dogma 2", 231106777: "Kunitsu-Gami",
    240606151: "Dead Rising Deluxe Remaster", 241106027: "Monster Hunter Wilds", 240701001: "Onimusha 2",
    251111100: "Monster Hunter Stories 3", 250813143: "Resident Evil Requiem",
}


@dataclass
class Mip:
    offset: int
    scanline: int
    size: int


@dataclass
class TexFile:
    version: int
    width: int
    height: int
    depth: int
    image_count: int
    mip_count: int
    format: int  # DXGI_FORMAT number
    cubemap: bool
    mips: list[list[Mip]] = field(default_factory=list)  # [image][mip]
    data: bytes = field(default=b"", repr=False)

    @property
    def codec(self) -> str:
        codec = dds.DXGI_CODECS.get(self.format)
        if codec is None:
            raise FormatError(f"Texture format {self.format} (DXGI) is not supported")
        return codec

    def mip_size(self, level: int) -> tuple[int, int, int]:
        info = dds.DDSInfo(self.width, self.height, self.mip_count, self.codec, 0)
        return info.mip_size(level)

    def surface(self, level: int = 0, image: int = 0) -> bytes:
        """The raw surface of one mip, rows packed as DDS expects them."""
        mip = self.mips[image][level]
        w, h, nbytes = self.mip_size(level)
        raw = self.data[mip.offset:mip.offset + mip.size]
        if len(raw) < min(mip.size, nbytes):
            raise FormatError(f"Mip {level} is truncated")
        if mip.size == nbytes or mip.scanline <= 0:
            return raw[:nbytes]
        # padded rows: keep the first row_bytes of every scanline
        if self.codec in dds.BLOCK_BYTES:
            rows = max(1, (h + 3) // 4)
            row_bytes = max(1, (w + 3) // 4) * dds.BLOCK_BYTES[self.codec]
        else:
            rows = h
            row_bytes = w * dds.PIXEL_BYTES.get(self.codec, 4)
        if mip.scanline < row_bytes or rows * mip.scanline > len(raw):
            return raw[:nbytes]
        arr = np.frombuffer(raw, np.uint8, count=rows * mip.scanline).reshape(rows, mip.scanline)
        return arr[:, :row_bytes].tobytes()

    def decode(self, level: int = 0) -> np.ndarray:
        level = min(level, self.mip_count - 1)
        w, h, _ = self.mip_size(level)
        return dds.decode_surface(self.codec, self.surface(level), w, h)

    def to_dds(self) -> bytes:
        """The first image as a DDS file, every mip included."""
        payload = b"".join(self.surface(i) for i in range(self.mip_count))
        return dds.build_dds(self.codec, self.width, self.height, payload, self.mip_count)

    def describe(self) -> str:
        return f"{self.width} x {self.height}, {self.codec}, {self.mip_count} mips"


def is_tex(data: bytes) -> bool:
    return len(data) >= 8 and int.from_bytes(data[:4], "little") == MAGIC


def read_tex(data: bytes) -> TexFile:
    r = Reader(data)
    if not is_tex(data):
        raise FormatError("Not an RE Engine texture file")
    r.u32()
    version = r.u32()
    width, height, depth = r.unpack("3H")
    if version > 11 and version != VERSION_RE3:
        image_count = r.u8()
        mip_count = r.u8() // 16
    else:
        mip_count = r.u8()
        image_count = r.u8()
    fmt = r.u32()
    _swizzle_control = r.i32()
    cubemap = r.u32()
    r.unpack("2BH")
    if version > 27 and version != VERSION_RE3:
        r.unpack("2B3H")
    if version in GDEFLATE_VERSIONS:
        raise FormatError("Textures of this game are GDeflate-compressed, which is not supported yet")
    if mip_count <= 0 or image_count <= 0 or width <= 0 or height <= 0:
        raise FormatError("Texture header has no images")
    tex = TexFile(version, width, height, depth, image_count, mip_count, fmt, bool(cubemap), data=data)
    for _ in range(image_count):
        mips = []
        for _ in range(mip_count):
            offset = r.u64()
            scanline, size = r.unpack("2I")
            mips.append(Mip(offset, scanline, size))
        tex.mips.append(mips)
    return tex


__all__ = ["TexFile", "VERSIONS", "is_tex", "read_tex"]
