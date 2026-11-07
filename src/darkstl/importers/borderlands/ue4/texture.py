"""Unreal Engine 4/5 ``Texture2D``: the cooked platform data (pixel format, size and the mips' bulk data).

After the properties come two strip-flag pairs, a "cooked" flag and then, per
cooked pixel format, the platform data: size, pixel format name, the first mip
to stream and the mips, each a bulk block (the large ones in the ``.ubulk``).
Borderlands 3 writes each mip's size as three 16-bit numbers. The largest mip
whose data is present is decoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ....textures import dds
from ..formats.props import Props
from ..formats.texture import FORMATS as UE3_FORMATS
from ..formats.texture import _decode
from . import asset
from .asset import Export, Package
from .mesh import object_props

FORMATS = dict(UE3_FORMATS)
FORMATS.update({
    "PF_B8G8R8A8": "B8G8R8A8", "PF_R8G8B8A8": "R8G8B8A8", "PF_BC6H": "BC6H", "PF_BC7": "BC7", "PF_BC4": "BC4",
    "PF_BC5": "BC5", "PF_G16": "R16", "PF_R16F": "R16F", "PF_R32_FLOAT": "R32F", "PF_A32B32G32R32F": "R32G32B32A32F",
    "PF_R8G8": "R8G8", "PF_A16B16G16R16": "R16G16B16A16", "PF_R16G16B16A16_UNORM": "R16G16B16A16",
})


@dataclass
class Mip4:
    width: int
    height: int
    bulk: asset.Bulk
    inline_pos: int


@dataclass
class Texture4:
    pkg: Package
    name: str
    path: str
    props: Props
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    codec: str = ""
    mips: list[Mip4] = field(default_factory=list)
    cache_name: str | None = None

    @property
    def is_normal_map(self) -> bool:
        cs = str(self.props.get("CompressionSettings", ""))
        lod = str(self.props.get("LODGroup", ""))
        if getattr(self.props, "material_refs", None) is not None:  # its properties couldn't be read
            return self.pixel_format == "PF_BC5"  # two-channel compression: what normal maps are cooked as
        return "Normal" in cs or "Normal" in lod

    def describe(self) -> str:
        return f"{self.width} x {self.height}, {self.pixel_format.replace('PF_', '')}, {len(self.mips)} mips"

    def available(self, _dirs=None) -> list[Mip4]:
        out = []
        for m in self.mips:
            if m.bulk.empty:
                continue
            if m.bulk.flags & (asset.BULK_SEPARATE_FILE | asset.BULK_OPTIONAL):
                ext = ".uptnl" if m.bulk.flags & asset.BULK_OPTIONAL else ".ubulk"
                if self.pkg.payload_bytes(ext) is None and self.pkg.payload_bytes(".m.ubulk") is None:
                    continue
            out.append(m)
        return out

    def decode(self, _dirs=None) -> np.ndarray:
        if not self.codec:
            raise FormatError(f"{self.name}: pixel format {self.pixel_format} isn't supported")
        last: Exception | None = None
        for m in self.available():
            try:
                raw = asset.bulk_payload(self.pkg, m.bulk, m.inline_pos)
                return _decode(self.codec, raw, m.width, m.height)
            except FormatError as e:
                last = e
        raise FormatError(f"{self.name}: none of its mips could be read" + (f" ({last})" if last else ""))


def read_texture(pkg: Package, e: Export) -> Texture4:
    v = pkg.versions
    r = pkg.export_data(e)
    props = object_props(pkg, r, e)
    tex = Texture4(pkg, e.name, e.path, props)
    g, _c = r.u8(), r.u8()  # UTexture
    if not g & 1:
        raise FormatError(f"{e.name} holds its source art (an uncooked texture)")
    r.u8(), r.u8()  # UTexture2D
    if not r.i32():
        raise FormatError(f"{e.name} has no cooked data")
    if v.engine >= 503:
        r.i32()  # serialize mip data
    fmt = r.name()
    while fmt != "None":
        if v.engine >= 500:
            here = r.tell
            skip_to = here + r.i64()
        elif v.engine >= 420:
            skip_to = r.i64()
        else:
            skip_to = r.i32()
        if not tex.pixel_format:
            _platform_data(r, pkg, tex)
            tex.pixel_format = tex.pixel_format or fmt
            break
        r.pos = skip_to - r.base
        fmt = r.name()
    tex.codec = FORMATS.get(tex.pixel_format, "")
    return tex


def _platform_data(r, pkg: Package, tex: Texture4) -> None:
    v = pkg.versions
    if v.engine >= 502:
        r.skip(16)
    elif v.engine >= 500 and v.filter_editor_only:
        r.skip(16)
    tex.width, tex.height = r.i32(), r.i32()
    packed = r.u32()
    tex.pixel_format = r.fstring()
    if packed & (1 << 30):
        r.skip(8)
    if packed & (1 << 29):
        raise FormatError(f"{tex.name} keeps a CPU copy, which isn't supported")
    r.i32()  # first mip to serialize
    for _ in range(r.count(32)):
        if v.engine < 500:
            r.i32()  # cooked
        h, pos = asset.skip_bulk(r)
        if v.bl3:
            w, hh, _d = r.u16(), r.u16(), r.u16()
        else:
            w, hh = r.i32(), r.i32()
            if v.engine >= 420:
                r.i32()
        tex.mips.append(Mip4(w, hh, h, pos))


def texture_codec_bytes(codec: str, w: int, h: int) -> int:
    if codec in dds.BLOCK_BYTES:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * dds.BLOCK_BYTES[codec]
    return w * h * dds.PIXEL_BYTES.get(codec, 4)


__all__ = ["FORMATS", "Mip4", "Texture4", "read_texture", "texture_codec_bytes"]
