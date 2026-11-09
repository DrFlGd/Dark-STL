"""id Tech ``image`` resources (BIM): a header, a record per mip, and the mips kept in the container.

The largest mips of most textures sit in the StreamDB (see :mod:`.streamdb`); :func:`read_image` takes a callable
that fetches a streamed piece. DOOM Eternal writes version 21, DOOM: The Dark Ages 24 and later (one more byte).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ....textures import dds

# texture formats (the engine's enum) -> Dark STL's codec names; the sRGB forms decode the same way
FORMATS = {
    3: "R8G8B8A8", 5: "A8", 7: "R8G8", 10: "BC1", 11: "BC3", 19: "A8", 22: "BC6H", 23: "BC7", 24: "BC4", 25: "BC5",
    32: "R8G8B8A8", 33: "BC1", 34: "BC3", 35: "BC7", 36: "BC6H", 54: "BC1",
}
SRGB = {32, 33, 34, 35}
FORMAT_NAMES = {3: "RGBA8", 5: "Alpha", 7: "RG8", 10: "BC1", 11: "BC3", 19: "R8", 22: "BC6H", 23: "BC7", 24: "BC4",
                25: "BC5", 32: "RGBA8 sRGB", 33: "BC1 sRGB", 34: "BC3 sRGB", 35: "BC7 sRGB", 36: "BC6H", 54: "BC1"}
# what the texture is for (TextureMaterialKind)
KINDS = {0: "", 1: "albedo", 2: "specular", 3: "normal", 4: "smoothness", 5: "cover", 6: "sssmask", 7: "colormask",
         8: "bloommask", 9: "heightmap", 10: "decalalbedo", 11: "decalnormal", 12: "decalspecular",
         17: "lightmap", 22: "blendmask"}

TT_2D, TT_3D, TT_CUBIC = 0, 1, 2


@dataclass
class Mip:
    level: int
    width: int
    height: int
    size: int
    cumulative: int  # where it starts among the streamed bytes (single-stream images)


@dataclass
class Image:
    version: int
    type: int
    kind: str
    width: int
    height: int
    mip_count: int
    format: int
    streamed: bool
    single_stream: bool
    streamed_mips: int  # how many of the largest mips are in the StreamDB
    mips: list[Mip]
    sdb_raw: int = 0  # the header's StreamDB mip count as stored
    data: list[bytes | None] = field(default_factory=list)  # per mip; None where it wasn't found

    @property
    def codec(self) -> str:
        c = FORMATS.get(self.format)
        if c is None:
            raise FormatError(f"texture format {self.format} isn't read")
        return c

    @property
    def srgb(self) -> bool:
        return self.format in SRGB

    @property
    def faces(self) -> int:
        return 6 if self.type == TT_CUBIC else 1

    def first_mip(self) -> int:
        """The largest mip there is data for."""
        for i in range(0, len(self.mips), self.faces):
            if self.data[i] is not None:
                return i // self.faces
        raise FormatError("none of the texture's mips were found")

    def describe(self) -> str:
        top = self.mips[self.first_mip() * self.faces] if self.mips else None
        w, h = (top.width, top.height) if top else (self.width, self.height)
        note = "" if (w, h) == (self.width, self.height) else f" (full size {self.width} x {self.height} not found)"
        return f"{w} x {h}, {FORMAT_NAMES.get(self.format, self.format)}, {self.mip_count} mips{note}"

    def decode(self, mip: int | None = None) -> np.ndarray:
        m = self.first_mip() if mip is None else mip
        info = self.mips[m * self.faces]
        raw = self.data[m * self.faces]
        if raw is None:
            raise FormatError("that mip wasn't found")
        # BC5 normal maps decode with an empty blue channel, as every game's do: the height pipeline works Z out
        # itself, and a package's copy of the DDS then shows the same picture
        return dds.decode_surface(self.codec, raw, info.width, info.height)

    def to_dds(self) -> bytes | None:
        start = self.first_mip()
        mips = [(self.mips[i * self.faces].width, self.mips[i * self.faces].height, self.data[i * self.faces])
                for i in range(start, self.mip_count) if self.data[i * self.faces] is not None]
        return dds.wrap_mips(self.codec, mips) if self.faces == 1 else None


def read_header(data: bytes) -> tuple[Image, int]:
    if data[:3] != b"BIM":
        raise FormatError("not an id Tech image (no BIM header)")
    version = data[3]
    ttype, kind, w, h, _depth, mip_count, _unk = struct.unpack_from("<7i", data, 4)
    pos = 4 + 28 + 8 + 1  # bias, scale, environment
    (fmt,) = struct.unpack_from("<i", data, pos)
    pos += 4 + 4 + 4 + 2  # format, always 7/8, padding, atlas padding
    streamed, single, _nomips, _fft = data[pos:pos + 4]
    pos += 4
    if version >= 24:
        pos += 1
    (sdb,) = struct.unpack_from("<i", data, pos)
    pos += 4
    faces = 6 if ttype == TT_CUBIC else 1
    mask = 0x0F if ttype == TT_2D else (0xFFFF if version < 24 else (0x0F if ttype == TT_3D else 0xFF))
    mips = []
    for _ in range(mip_count * faces):
        level, _slice, mw, mh, _x, size, _flag, _csize, cumulative = struct.unpack_from("<9i", data, pos)
        pos += 36
        mips.append(Mip(level, mw, mh, size, cumulative))
    img = Image(version, ttype, KINDS.get(kind, str(kind)), w, h, mip_count, fmt, bool(streamed), bool(single),
                min(sdb & mask, len(mips)), mips, sdb)
    img.data = [None] * len(mips)
    return img, pos


def read_image(data: bytes, fetch: Callable[[int, int], bytes | None] | None = None) -> Image:
    """An image resource. ``fetch(n, size)`` gives streamed piece ``n`` (the index the game keys it by, see
    :func:`stream_key`) decompressed to ``size`` bytes, or None; without it only the mips in the container are read."""
    img, pos = read_header(data)
    for i in range(img.streamed_mips, len(img.mips)):
        size = img.mips[i].size
        img.data[i] = data[pos:pos + size]
        pos += size
        if len(img.data[i]) < size:
            raise FormatError("the texture's data is cut short")
    if fetch is not None and img.streamed_mips:
        if img.single_stream:
            last = img.mips[-1]
            blob = fetch(-1, last.cumulative + last.size)
            if blob is not None:
                for i, m in enumerate(img.mips):
                    img.data[i] = blob[m.cumulative:m.cumulative + m.size]
        else:
            for i in range(img.streamed_mips):
                piece = fetch(i, img.mips[i].size)
                if piece is not None and len(piece) >= img.mips[i].size:
                    img.data[i] = piece[:img.mips[i].size]
    return img


def mip_stream_id(img: Image, i: int, hash_: int, darkages: bool) -> int:
    """The StreamDB id of streamed mip ``i`` of ``img`` (``-1``: the whole single stream), from the resource's hash."""
    from .streamdb import darkages_id, eternal_id

    if darkages:
        return darkages_id(hash_, 0 if i < 0 else img.sdb_raw - img.mips[i].level - 1, 0)
    return hash_ if i < 0 else eternal_id(hash_, img.mip_count - img.mips[i].level)
