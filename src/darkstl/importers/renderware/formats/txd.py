"""RenderWare texture dictionaries (``.txd``) of the PC games.

A Texture Dictionary holds Texture Natives. Each is the platform (8: Direct3D 8, GTA III and Vice
City; 9: Direct3D 9, San Andreas), the texture's name and mask name, its raster format, size and
mip count, then the palette (PAL8, PAL4) and each level's pixels behind its byte count. Pixels
are DXT1/3/5, palette indices, or raw 8888, 888, 1555, 565, 4444, 555 and LUM8 in Direct3D's
BGRA order. Layout from the gtamods.com wiki and librw (MIT).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ....textures.dds import build_dds, decode_surface, wrap_mips
from . import chunks as ck
from .chunks import Reader

PLATFORM_D3D8 = 8
PLATFORM_D3D9 = 9
PLATFORM_PS2 = 0x00325350
PLATFORM_XBOX = 5

C1555, C565, C4444, LUM8, C8888, C888, C555 = 0x100, 0x200, 0x300, 0x400, 0x500, 0x600, 0xA00
PAL8, PAL4, MIPMAP = 0x2000, 0x4000, 0x8000
FORMAT_NAMES = {C1555: "1555", C565: "565", C4444: "4444", LUM8: "LUM8", C8888: "8888", C888: "888",
                C555: "555"}
_FOURCC = {b"DXT1": "BC1", b"DXT2": "BC2", b"DXT3": "BC2", b"DXT4": "BC3", b"DXT5": "BC3"}


@dataclass
class NativeTexture:
    name: str
    mask: str
    platform: int
    raster: int  # raster format flags
    codec: str  # "BC1", "BC2", "BC3", "PAL8", "PAL4" or one of FORMAT_NAMES' values
    width: int
    height: int
    has_alpha: bool
    levels: list[bytes] = field(repr=False)
    palette: np.ndarray | None = field(default=None, repr=False)  # (n, 4) uint8 RGBA

    def describe(self) -> str:
        return f"{self.width} x {self.height}, {self.codec}, {len(self.levels)} mip{'s' if len(self.levels) != 1 else ''}"

    def decode(self, level: int = 0) -> np.ndarray:
        """Level ``level`` as (H, W, 4) uint8 RGBA."""
        level = min(level, len(self.levels) - 1)
        w, h = max(1, self.width >> level), max(1, self.height >> level)
        return decode_level(self, self.levels[level], w, h)

    def to_dds(self) -> bytes:
        """The texture as a DDS file: block-compressed levels as they are, anything else as RGBA."""
        if self.codec in ("BC1", "BC2", "BC3"):
            mips = [(max(1, self.width >> i), max(1, self.height >> i), lv) for i, lv in enumerate(self.levels)]
            out = wrap_mips(self.codec, mips)
            if out is not None:
                return out
        rgba = self.decode(0)
        return build_dds("R8G8B8A8", rgba.shape[1], rgba.shape[0], np.ascontiguousarray(rgba).tobytes())


@dataclass
class TextureDictionary:
    version: int
    textures: list[NativeTexture]

    def by_name(self) -> dict[str, NativeTexture]:
        out: dict[str, NativeTexture] = {}
        for t in self.textures:
            out.setdefault(t.name.lower(), t)
        return out


def _expand(v: np.ndarray, bits: int) -> np.ndarray:
    """``bits``-bit channel values as 0-255."""
    top = (1 << bits) - 1
    return ((v.astype(np.uint32) * 255 + top // 2) // top).astype(np.uint8)


def decode_level(tex: NativeTexture, raw: bytes, w: int, h: int) -> np.ndarray:
    codec = tex.codec
    if codec in ("BC1", "BC2", "BC3"):
        out = decode_surface(codec, raw, w, h)
        if codec == "BC1" and not tex.has_alpha:
            out = np.array(out, copy=True)
            out[..., 3] = 255
        return out
    if codec in ("PAL8", "PAL4"):
        pal = tex.palette if tex.palette is not None else np.zeros((256, 4), np.uint8)
        if codec == "PAL8":
            idx = np.frombuffer(raw, np.uint8, count=w * h)
        else:
            packed = np.frombuffer(raw, np.uint8, count=(w * h + 1) // 2)
            idx = np.empty(len(packed) * 2, np.uint8)
            idx[0::2] = packed & 0x0F
            idx[1::2] = packed >> 4
            idx = idx[:w * h]
        img = pal[np.minimum(idx, len(pal) - 1)].reshape(h, w, 4).copy()
        if not tex.has_alpha:
            img[..., 3] = 255
        return img
    if codec in ("8888", "888"):
        px = np.frombuffer(raw, np.uint8, count=w * h * 4).reshape(h, w, 4)
        img = px[..., [2, 1, 0, 3]].copy()
        if codec == "888" or not tex.has_alpha and tex.raster & 0x0F00 == C888:
            img[..., 3] = 255
        return img
    if codec == "LUM8":
        v = np.frombuffer(raw, np.uint8, count=w * h).reshape(h, w)
        return np.dstack([v, v, v, np.full_like(v, 255)])
    v = np.frombuffer(raw, "<u2", count=w * h).reshape(h, w)
    if codec == "1555":
        a, r, g, b = (v >> 15) & 1, (v >> 10) & 31, (v >> 5) & 31, v & 31
        return np.dstack([_expand(r, 5), _expand(g, 5), _expand(b, 5), _expand(a, 1)])
    if codec == "555":
        r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
        return np.dstack([_expand(r, 5), _expand(g, 5), _expand(b, 5), np.full((h, w), 255, np.uint8)])
    if codec == "565":
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        return np.dstack([_expand(r, 5), _expand(g, 6), _expand(b, 5), np.full((h, w), 255, np.uint8)])
    if codec == "4444":
        a, r, g, b = (v >> 12) & 15, (v >> 8) & 15, (v >> 4) & 15, v & 15
        return np.dstack([_expand(r, 4), _expand(g, 4), _expand(b, 4), _expand(a, 4)])
    raise FormatError(f"Unsupported RenderWare raster {codec}")


def _level_bytes(codec: str, w: int, h: int) -> int:
    if codec == "BC1":
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8
    if codec in ("BC2", "BC3"):
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
    if codec == "PAL4":
        return (w * h + 1) // 2
    if codec in ("PAL8", "LUM8"):
        return w * h
    if codec in ("8888", "888"):
        return w * h * 4
    return w * h * 2


def read_native(chunk: ck.Chunk) -> NativeTexture:
    r = Reader.of(chunk.struct())
    platform = r.u32()
    if platform not in (PLATFORM_D3D8, PLATFORM_D3D9):
        names = {PLATFORM_PS2: "PlayStation 2", PLATFORM_XBOX: "Xbox"}
        raise FormatError(f"Texture for {names.get(platform, f'platform {platform}')}, not PC")
    r.u32()  # filter and addressing
    name = bytes(r.take(32)).split(b"\0", 1)[0].decode("latin-1")
    mask = bytes(r.take(32)).split(b"\0", 1)[0].decode("latin-1")
    raster = r.u32()
    fourcc = bytes(r.take(4))
    width, height, _depth, levels, _rtype, comp = r.unpack("HHBBBB")
    if platform == PLATFORM_D3D8:
        has_alpha = struct.unpack("<I", fourcc)[0] != 0
        codec = {1: "BC1", 2: "BC2", 3: "BC2", 4: "BC3", 5: "BC3"}.get(comp)
    else:
        has_alpha = bool(comp & 1)
        codec = _FOURCC.get(fourcc) if comp & 8 else None
        if codec is None and fourcc in _FOURCC:
            codec = _FOURCC[fourcc]
    palette = None
    if codec is None:
        if raster & PAL8:
            codec = "PAL8"
        elif raster & PAL4:
            codec = "PAL4"
        else:
            codec = FORMAT_NAMES.get(raster & 0x0F00)
            if codec is None:
                raise FormatError(f"{name}: unknown raster format 0x{raster:x}")
            if codec == "8888" and platform == PLATFORM_D3D9 and struct.unpack("<I", fourcc)[0] == 22:
                codec = "888"  # X8R8G8B8
    if codec in ("PAL8", "PAL4"):
        entries = 256 if codec == "PAL8" else 16
        start = r.pos
        pal = np.frombuffer(r.take(entries * 4), np.uint8).reshape(entries, 4)
        if codec == "PAL4" and r.left() >= 4:
            # PAL4 palettes are 16 entries on PC in the wiki, 32 in librw: take what makes the level size fit
            (size,) = struct.unpack_from("<I", r.data, r.pos)
            if size != _level_bytes("PAL4", width, height) and r.left() >= 68:
                r.pos = start
                pal = np.frombuffer(r.take(32 * 4), np.uint8).reshape(32, 4)
        palette = pal.copy()
    data = []
    for i in range(max(levels, 1)):
        if r.left() < 4:
            break
        size = r.u32()
        if size > r.left():
            break
        data.append(bytes(r.take(size)))
    if not data:
        raise FormatError(f"{name}: no pixels")
    return NativeTexture(name, mask, platform, raster, codec, width, height, has_alpha, data, palette)


def read_txd(data: bytes) -> TextureDictionary:
    root = ck.read_root(data, ck.TEXTURE_DICTIONARY)
    textures = []
    for c in root.children():
        if c.type == ck.TEXTURE_NATIVE:
            try:
                textures.append(read_native(c))
            except FormatError:
                continue
    return TextureDictionary(root.version, textures)
