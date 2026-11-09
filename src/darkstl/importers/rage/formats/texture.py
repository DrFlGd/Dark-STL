"""Texture dictionaries (``.ytd``, and the ones drawables carry) and their textures.

A texture names its format (Direct3D 9 codes and FourCCs in the original PC release, DXGI codes in GTA V
Enhanced), its size and mip count, and points at the levels, stored one after another. Block-compressed
textures are kept as they are (a DDS file around the same blocks); the rest are decoded to RGBA.
Layout from CodeWalker (MIT).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ....textures.dds import BLOCK_BYTES, PIXEL_BYTES, build_dds, decode_surface, surface_bytes, wrap_mips
from .rsc import Reader

LEGACY_CODECS = {
    21: "B8G8R8A8", 22: "B8G8R8X8", 25: "B5G5R5A1", 28: "A8", 32: "R8G8B8A8", 50: "L8",
    0x31545844: "BC1", 0x33545844: "BC2", 0x35545844: "BC3", 0x31495441: "BC4", 0x32495441: "BC5",
    0x20374342: "BC7",
}
DXGI_CODECS = {
    28: "R8G8B8A8", 29: "R8G8B8A8", 87: "B8G8R8A8", 91: "B8G8R8A8", 88: "B8G8R8X8", 65: "A8", 61: "L8",
    86: "B5G5R5A1", 70: "BC1", 71: "BC1", 72: "BC1", 73: "BC2", 74: "BC2", 75: "BC2", 76: "BC3", 77: "BC3",
    78: "BC3", 79: "BC4", 80: "BC4", 82: "BC5", 83: "BC5", 97: "BC7", 98: "BC7", 99: "BC7",
}
_PIXEL = dict(PIXEL_BYTES, L8=1)


@dataclass
class Texture:
    name: str
    width: int
    height: int
    codec: str | None  # None: a format that isn't read
    format: int  # as stored
    levels: list[bytes] = field(default_factory=list, repr=False)

    def level_size(self, i: int) -> tuple[int, int]:
        return max(1, self.width >> i), max(1, self.height >> i)

    def decode(self, level: int = 0) -> np.ndarray:
        if self.codec is None or not self.levels:
            raise FormatError(f"Texture {self.name} is in a format that isn't read (0x{self.format:x})")
        level = min(level, len(self.levels) - 1)
        w, h = self.level_size(level)
        raw = self.levels[level]
        if self.codec == "L8":
            g = np.frombuffer(raw, np.uint8, count=w * h).reshape(h, w)
            out = np.empty((h, w, 4), np.uint8)
            out[..., :3] = g[..., None]
            out[..., 3] = 255
            return out
        return decode_surface(self.codec, raw, w, h)

    def to_dds(self) -> bytes:
        """The texture as a DDS file: block-compressed levels as they are, anything else as RGBA."""
        if self.codec in BLOCK_BYTES or self.codec in ("R8G8B8A8", "B8G8R8A8"):
            mips = [(*self.level_size(i), lv) for i, lv in enumerate(self.levels)]
            out = wrap_mips(self.codec, mips)
            if out is not None:
                return out
        rgba = self.decode(0)
        return build_dds("R8G8B8A8", rgba.shape[1], rgba.shape[0], np.ascontiguousarray(rgba).tobytes())


def _level_bytes(codec: str, w: int, h: int) -> int:
    if codec in BLOCK_BYTES:
        return surface_bytes(codec, w, h)
    return w * h * _PIXEL.get(codec, 4)


def read_texture(r: Reader, ptr: int) -> Texture:
    """A texture block (with its levels when it has them; a texture a shader only names has none)."""
    name = r.string(r.u64(ptr + 0x28))
    if r.gen9:
        w, h, depth = r.unpack("HHH", ptr + 0x18)
        fmt = r.u8(ptr + 0x1F)
        count = r.u8(ptr + 0x22)
        data_ptr = r.u64(ptr + 0x38)
        codec = DXGI_CODECS.get(fmt)
    else:
        try:
            w, h, depth, _stride, fmt = r.unpack("HHHHI", ptr + 0x50)
            count = r.u8(ptr + 0x5D)
            data_ptr = r.u64(ptr + 0x70)
        except FormatError:  # only the 80-byte base: a name
            return Texture(name, 0, 0, None, 0)
        codec = LEGACY_CODECS.get(fmt)
    tex = Texture(name, w, h, codec, fmt)
    if codec is None or not data_ptr or not r.ok(data_ptr) or w == 0 or h == 0:
        return tex
    p = data_ptr
    for i in range(max(1, count)):
        lw, lh = tex.level_size(i)
        n = _level_bytes(codec, lw, lh)
        try:
            tex.levels.append(r.bytes(p, n))
        except FormatError:
            break
        p += n
    return tex


def read_dictionary(r: Reader, ptr: int) -> dict[str, Texture]:
    """A texture dictionary: its textures by lower-case name."""
    out: dict[str, Texture] = {}
    if not ptr:
        return out
    for tp in r.pointer_list(ptr + 0x30):
        if not tp:
            continue
        try:
            t = read_texture(r, tp)
        except FormatError:
            continue
        out.setdefault(t.name.lower(), t)
    return out
