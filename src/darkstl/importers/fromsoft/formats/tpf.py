"""TPF texture packs.

A TPF holds one or more named textures. On PC the payload of each texture is
a complete DDS file. Console builds (Bloodborne is PS4-only) store headerless,
tiled pixel data plus enough metadata to rebuild a DDS header; see
:func:`TPFTexture.to_dds`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ....textures import dds
from . import dcx
from .binary import BinaryReader, FormatError

PLATFORM_PC = 0
PLATFORM_X360 = 1
PLATFORM_PS3 = 2
PLATFORM_PS4 = 4
PLATFORM_XBONE = 5

PLATFORM_NAMES = {0: "PC", 1: "Xbox 360", 2: "PS3", 4: "PS4", 5: "Xbox One"}

# TPF format byte -> (codec, bytes per 4x4 block or per pixel, is_block_compressed)
# Taken from the format survey in SoulsFormats' Headerizer.
TPF_FORMATS: dict[int, tuple[str, int, bool]] = {
    0: ("BC1", 8, True), 1: ("BC1", 8, True), 3: ("BC2", 16, True), 5: ("BC3", 16, True),
    23: ("BC3", 16, True), 24: ("BC1", 8, True), 25: ("BC1", 8, True), 33: ("BC3", 16, True),
    100: ("BC6H", 16, True), 102: ("BC7", 16, True), 103: ("BC4", 8, True), 104: ("BC5", 16, True),
    106: ("BC7", 16, True), 107: ("BC7", 16, True), 108: ("BC1", 8, True), 109: ("BC1", 8, True),
    110: ("BC3", 16, True), 112: ("BC7", 16, True), 113: ("BC6H", 16, True),
    9: ("B8G8R8A8", 4, False), 10: ("B8G8R8A8", 4, False), 16: ("A8", 1, False),
    105: ("R8G8B8A8", 4, False), 22: ("R16G16B16A16_FLOAT", 8, False),
}


@dataclass
class TPFTexture:
    name: str
    format: int
    type: int  # 0 texture, 1 cubemap, 2 volume
    mipmaps: int
    flags1: int
    data: bytes
    platform: int
    width: int = 0
    height: int = 0
    dxgi_format: int = 0

    @property
    def is_cubemap(self) -> bool:
        return self.type == 1

    def to_dds(self) -> bytes:
        """Return a complete DDS file for this texture (first surface, all mips when possible)."""
        if self.data[:4] == b"DDS ":
            return self.data
        if self.platform in (PLATFORM_PS4, PLATFORM_XBONE):
            from .ps4 import headerize_ps4

            return headerize_ps4(self)
        raise FormatError(f"Texture {self.name!r} has no DDS header and platform "
                          f"{PLATFORM_NAMES.get(self.platform, self.platform)} is not supported")

    def decode(self):
        """Decode the top mip level to an ``(H, W, 4)`` uint8 RGBA array."""
        return dds.decode_dds(self.to_dds())


@dataclass
class TPF:
    platform: int
    encoding: int
    textures: list[TPFTexture]

    def get(self, name: str) -> TPFTexture | None:
        lname = name.lower()
        for tex in self.textures:
            if tex.name.lower() == lname:
                return tex
        return None


def is_tpf(data: bytes) -> bool:
    return data[:4] == b"TPF\0"


def read_tpf(data: bytes) -> TPF:
    data = dcx.maybe_decompress(data)
    br = BinaryReader(data)
    br.assert_ascii("TPF\0")
    platform = data[0x0C]
    br.big_endian = platform in (PLATFORM_X360, PLATFORM_PS3)
    br.i32()  # data size
    count = br.i32()
    br.skip(1)  # platform
    flag2 = br.u8()
    encoding = br.u8()
    br.skip(1)
    textures = []
    for _ in range(count):
        offset = br.u32()
        size = br.i32()
        fmt = br.u8()
        ttype = br.u8()
        mips = br.u8()
        flags1 = br.u8()
        width = height = 0
        dxgi = 0
        if platform != PLATFORM_PC:
            width = br.i16()
            height = br.i16()
            if platform == PLATFORM_X360:
                br.i32()
            elif platform == PLATFORM_PS3:
                br.i32()
                if flag2 != 0:
                    br.i32()
            elif platform in (PLATFORM_PS4, PLATFORM_XBONE):
                br.i32()  # texture count (1 or 6)
                br.i32()  # 0xD
        name_offset = br.u32()
        has_float_struct = br.i32() == 1
        if platform in (PLATFORM_PS4, PLATFORM_XBONE):
            dxgi = br.i32()
        if has_float_struct:
            br.i32()
            length = br.i32()
            if length < 0 or length % 4:
                raise FormatError(f"Bad TPF float struct length {length}")
            br.skip(length)
        payload = br.get_bytes(offset, size)
        if flags1 in (2, 3):
            # DCP_EDGE compressed payloads (PS3 Demon's Souls era)
            payload = dcx.decompress(payload)[0]
        name = br.get_utf16(name_offset) if encoding == 1 else br.get_shift_jis(name_offset)
        textures.append(TPFTexture(name, fmt, ttype, mips, flags1, payload, platform, width, height, dxgi))
    return TPF(platform, encoding, textures)
