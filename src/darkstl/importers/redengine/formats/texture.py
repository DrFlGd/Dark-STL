"""CBitmapTexture: the pixels of an ``.xbm`` file.

The root chunk's ``setup`` says how the texture is compressed (BC1 for colour
without alpha, BC3 with, BC5 for normal maps, BC7 for quality colour, raw
formats for the rest) and its ``renderTextureResource`` points at a
``rendRenderTextureBlobPC`` whose header lists the mip levels and whose
buffer holds them, largest first, exactly as the GPU wants them, so a mip is
decoded like a DDS surface of the matching DXGI format.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ....textures import dds
from ...fromsoft.formats.binary import FormatError
from .cr2w import Cr2wFile, Red

COMPRESSION_CODECS = {
    "TCM_DXTNoAlpha": "BC1", "TCM_DXTAlpha": "BC3", "TCM_DXTAlphaLinear": "BC3", "TCM_Normalmap": "BC5",
    "TCM_Normals_DEPRECATED": "BC1", "TCM_NormalsHigh_DEPRECATED": "BC3", "TCM_NormalsGloss_DEPRECATED": "BC3",
    "TCM_QualityR": "BC4", "TCM_QualityRG": "BC5", "TCM_QualityColor": "BC7", "TCM_HalfHDR_Unsigned": "BC6H",
    "TCM_HalfHDR_Signed": "BC6H", "TCM_RGBE": "R32G32B32A32F",
}
RAW_CODECS = {
    "TRF_TrueColor": "R8G8B8A8", "TRF_Grayscale": "R8", "TRF_Grayscale_Font": "A8", "TRF_HDRFloat": "R32G32B32A32F",
    "TRF_HDRHalf": "R16G16B16A16F", "TRF_DeepColor": "R16G16B16A16", "TRF_HDRFloatGrayscale": "R32F",
    "TRF_R8G8": "R8G8",
}
OVERRIDE_CODECS = {
    "TEXFMT_BC1": "BC1", "TEXFMT_BC1_SRGB": "BC1", "TEXFMT_BC2": "BC2", "TEXFMT_BC2_SRGB": "BC2", "TEXFMT_BC3": "BC3",
    "TEXFMT_BC3_SRGB": "BC3", "TEXFMT_BC4": "BC4", "TEXFMT_BC5": "BC5", "TEXFMT_BC6H_UNSIGNED": "BC6H",
    "TEXFMT_BC6H_SIGNED": "BC6H", "TEXFMT_BC7": "BC7", "TEXFMT_BC7_SRGB": "BC7", "TEXFMT_R8G8B8A8_Unorm": "R8G8B8A8",
    "TEXFMT_R8G8B8A8_Unorm_SRGB": "R8G8B8A8", "TEXFMT_B8G8R8A8": "B8G8R8A8", "TEXFMT_B8G8R8A8_SRGB": "B8G8R8A8",
    "TEXFMT_R8_Unorm": "R8", "TEXFMT_A8_Unorm": "A8", "TEXFMT_R8G8_Unorm": "R8G8", "TEXFMT_R16G16B16A16_Float":
    "R16G16B16A16F", "TEXFMT_R32G32B32A32_Float": "R32G32B32A32F", "TEXFMT_R32_Float": "R32F",
    "TEXFMT_R16_Float": "R16F", "TEXFMT_R16G16B16A16_Unorm": "R16G16B16A16",
}


@dataclass
class RedTexture:
    width: int
    height: int
    mips: int
    codec: str
    compression: str
    raw_format: str
    group: str
    data: bytes  # every mip, largest first
    mip_offsets: list[tuple[int, int]]  # (offset, size) of each mip in ``data``

    def mip_size(self, level: int) -> tuple[int, int, int]:
        w = max(1, self.width >> level)
        h = max(1, self.height >> level)
        if self.codec in dds.BLOCK_BYTES:
            nbytes = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * dds.BLOCK_BYTES[self.codec]
        else:
            nbytes = w * h * dds.PIXEL_BYTES.get(self.codec, 4)
        return w, h, nbytes

    def surface(self, level: int = 0) -> bytes:
        level = min(level, max(self.mips - 1, 0))
        _w, _h, nbytes = self.mip_size(level)
        if level < len(self.mip_offsets):
            off, size = self.mip_offsets[level]
        else:
            off = sum(self.mip_size(i)[2] for i in range(level))
            size = nbytes
        raw = self.data[off:off + max(size, nbytes)]
        if len(raw) < nbytes:
            raise FormatError(f"Texture mip {level} is truncated ({len(raw)} of {nbytes} bytes)")
        return raw[:nbytes]

    def decode(self, level: int = 0) -> np.ndarray:
        w, h, _ = self.mip_size(level)
        return dds.decode_surface(self.codec, self.surface(level), w, h)

    def to_dds(self) -> bytes:
        """The texture as a DDS file (all mips), for saving it out."""
        return dds.build_dds(self.codec, self.width, self.height,
                             b"".join(self.surface(i) for i in range(max(self.mips, 1))), max(self.mips, 1))


def codec_for(setup: Red, header: Red | None) -> str:
    if header is not None:
        over = header.get("formatOverride")
        if over and over != "TEXFMT_NULL" and over in OVERRIDE_CODECS:
            return OVERRIDE_CODECS[over]
    comp = setup.get("compression", "TCM_None")
    if comp != "TCM_None":
        if comp in COMPRESSION_CODECS:
            return COMPRESSION_CODECS[comp]
        raise FormatError(f"Unsupported texture compression {comp}")
    raw = setup.get("rawFormat", "TRF_TrueColor")
    if raw in RAW_CODECS:
        return RAW_CODECS[raw]
    raise FormatError(f"Unsupported raw texture format {raw}")


def read_texture(f: Cr2wFile) -> RedTexture:
    root = f.root
    if root.cls not in ("CBitmapTexture", "CTextureArray", "CCubeTexture"):
        raise FormatError(f"Not a texture: the file holds a {root.cls}")
    setup = root.get("setup") if isinstance(root.get("setup"), Red) else Red("STextureGroupSetup")
    res = root.get("renderTextureResource")
    blob = f.chunk(res.get("renderResourceBlobPC")) if isinstance(res, Red) else None
    if blob is None:
        blob = f.chunk(root.get("renderResourceBlob"))
    if not isinstance(blob, Red):
        raise FormatError("The texture has no PC render blob (console textures are not read)")
    header = blob.get("header") if isinstance(blob.get("header"), Red) else None
    codec = codec_for(setup, header)
    width = int(root.get("width", 0))
    height = int(root.get("height", 0))
    mips = 1
    offsets: list[tuple[int, int]] = []
    if header is not None:
        size_info = header.get("sizeInfo")
        if isinstance(size_info, Red):
            width = int(size_info.get("width", width)) or width
            height = int(size_info.get("height", height)) or height
        tex_info = header.get("textureInfo")
        if isinstance(tex_info, Red):
            mips = int(tex_info.get("mipCount", 1)) or 1
        for m in header.get("mipMapInfo") or []:
            p = m.get("placement") if isinstance(m, Red) else None
            if isinstance(p, Red):
                offsets.append((int(p.get("offset", 0)), int(p.get("size", 0))))
    if width <= 0 or height <= 0:
        raise FormatError("The texture has no size")
    data = f.buffer(blob.get("textureData"))
    return RedTexture(width, height, mips, codec, str(setup.get("compression", "TCM_None")),
                      str(setup.get("rawFormat", "TRF_TrueColor")), str(setup.get("group", "")), data, offsets)


__all__ = ["COMPRESSION_CODECS", "RAW_CODECS", "RedTexture", "codec_for", "read_texture"]
