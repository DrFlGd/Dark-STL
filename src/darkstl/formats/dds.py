"""DDS parsing and decoding to RGBA arrays.

Block-compressed formats BC1-BC5 are decoded with vectorised numpy code; BC6H
and BC7 (used heavily by Elden Ring) go through ``texture2ddecoder`` with a
Pillow fallback. Only the first surface of cubemaps/arrays is decoded.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .binary import FormatError

DDSD_MIPMAPCOUNT = 0x20000
DDPF_ALPHAPIXELS = 0x1
DDPF_ALPHA = 0x2
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000
DDSCAPS2_CUBEMAP = 0x200

BLOCK_BYTES = {"BC1": 8, "BC2": 16, "BC3": 16, "BC4": 8, "BC4S": 8, "BC5": 16, "BC5S": 16, "BC6H": 16, "BC7": 16}

FOURCC_CODECS = {
    b"DXT1": "BC1", b"DXT2": "BC2", b"DXT3": "BC2", b"DXT4": "BC3", b"DXT5": "BC3",
    b"ATI1": "BC4", b"BC4U": "BC4", b"BC4S": "BC4S", b"ATI2": "BC5", b"BC5U": "BC5", b"BC5S": "BC5S",
    b"$\0\0\0": "R16G16B16A16", b"o\0\0\0": "R16F", b"q\0\0\0": "R16G16B16A16F",
    b"r\0\0\0": "R32F", b"t\0\0\0": "R32G32B32A32F",
}

DXGI_CODECS = {
    2: "R32G32B32A32F", 10: "R16G16B16A16F", 11: "R16G16B16A16", 27: "R8G8B8A8", 28: "R8G8B8A8", 29: "R8G8B8A8",
    41: "R32F", 49: "R8G8", 54: "R16F", 56: "R16", 61: "R8", 65: "A8",
    70: "BC1", 71: "BC1", 72: "BC1", 73: "BC2", 74: "BC2", 75: "BC2", 76: "BC3", 77: "BC3", 78: "BC3",
    79: "BC4", 80: "BC4", 81: "BC4S", 82: "BC5", 83: "BC5", 84: "BC5S", 85: "B5G6R5", 86: "B5G5R5A1",
    87: "B8G8R8A8", 88: "B8G8R8X8", 90: "B8G8R8A8", 91: "B8G8R8A8", 92: "B8G8R8X8", 93: "B8G8R8X8",
    94: "BC6H", 95: "BC6H", 96: "BC6H", 97: "BC7", 98: "BC7", 99: "BC7",
}

# Uncompressed codecs: bytes per pixel
PIXEL_BYTES = {
    "R8G8B8A8": 4, "B8G8R8A8": 4, "B8G8R8X8": 4, "R8G8": 2, "R8": 1, "A8": 1, "R16": 2, "R16F": 2,
    "R32F": 4, "R16G16B16A16": 8, "R16G16B16A16F": 8, "R32G32B32A32F": 16, "B5G6R5": 2, "B5G5R5A1": 2,
}

CODEC_DXGI = {"BC1": 71, "BC2": 74, "BC3": 77, "BC4": 80, "BC5": 83, "BC6H": 95, "BC7": 98,
              "R8G8B8A8": 28, "B8G8R8A8": 87, "A8": 65, "R16G16B16A16F": 10}


@dataclass
class DDSInfo:
    width: int
    height: int
    mipmaps: int
    codec: str  # e.g. "BC1", "R8G8B8A8", or "MASK" for legacy bitmask formats
    data_offset: int
    is_cubemap: bool = False
    bit_count: int = 0
    masks: tuple[int, int, int, int] = (0, 0, 0, 0)

    def mip_size(self, level: int) -> tuple[int, int, int]:
        w = max(1, self.width >> level)
        h = max(1, self.height >> level)
        if self.codec in BLOCK_BYTES:
            nbytes = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * BLOCK_BYTES[self.codec]
        elif self.codec == "MASK":
            nbytes = w * h * (self.bit_count // 8)
        else:
            nbytes = w * h * PIXEL_BYTES[self.codec]
        return w, h, nbytes


def parse_dds(data: bytes) -> DDSInfo:
    if data[:4] != b"DDS ":
        raise FormatError("Not a DDS file")
    (size, flags, height, width, _pitch, _depth, mipmaps) = struct.unpack_from("<7I", data, 4)
    if size != 124:
        raise FormatError(f"Unexpected DDS header size {size}")
    pf_flags, fourcc = struct.unpack_from("<I4s", data, 0x50)
    bit_count, rmask, gmask, bmask, amask = struct.unpack_from("<5I", data, 0x58)
    (caps2,) = struct.unpack_from("<I", data, 0x70)
    offset = 128
    codec = None
    if pf_flags & DDPF_FOURCC:
        if fourcc == b"DX10":
            dxgi, _dim, misc = struct.unpack_from("<3I", data, 128)
            offset += 20
            codec = DXGI_CODECS.get(dxgi)
            if codec is None:
                raise FormatError(f"Unsupported DXGI format {dxgi}")
            if misc & 0x4:
                caps2 |= DDSCAPS2_CUBEMAP
        else:
            codec = FOURCC_CODECS.get(fourcc)
            if codec is None:
                raise FormatError(f"Unsupported DDS FourCC {fourcc!r}")
    elif pf_flags & (DDPF_RGB | DDPF_LUMINANCE | DDPF_ALPHA | DDPF_ALPHAPIXELS):
        codec = "MASK"
        if not pf_flags & (DDPF_ALPHAPIXELS | DDPF_ALPHA):
            amask = 0
        if pf_flags & DDPF_ALPHA and not (pf_flags & (DDPF_RGB | DDPF_LUMINANCE)):
            rmask = gmask = bmask = 0
        if pf_flags & DDPF_LUMINANCE:
            gmask = bmask = rmask
    else:
        raise FormatError(f"Unsupported DDS pixel format flags 0x{pf_flags:X}")
    if not (flags & DDSD_MIPMAPCOUNT) or mipmaps == 0:
        mipmaps = 1
    return DDSInfo(width, height, mipmaps, codec, offset, bool(caps2 & DDSCAPS2_CUBEMAP),
                   bit_count, (rmask, gmask, bmask, amask))


# ---------------------------------------------------------------------------
# Block decoders


def _blocks_to_image(texels: np.ndarray, bw: int, bh: int, width: int, height: int) -> np.ndarray:
    """(nblocks, 16, C) texels in row-major block order -> (H, W, C) image."""
    c = texels.shape[-1]
    img = texels.reshape(bh, bw, 4, 4, c).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, c)
    return img[:height, :width]


def _expand565(c: np.ndarray) -> np.ndarray:
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1).astype(np.int32)


def _bc1_colors(blocks: np.ndarray, force_four: bool) -> tuple[np.ndarray, np.ndarray]:
    """Returns ((n, 16, 3) uint8 rgb, (n, 16) uint8 alpha) from 8-byte BC1 colour blocks."""
    c0 = blocks[:, 0].astype(np.int32) | (blocks[:, 1].astype(np.int32) << 8)
    c1 = blocks[:, 2].astype(np.int32) | (blocks[:, 3].astype(np.int32) << 8)
    idx = blocks[:, 4:8].copy().view("<u4").reshape(-1).astype(np.int64)
    e0 = _expand565(c0)
    e1 = _expand565(c1)
    four = (c0 > c1) | force_four
    pal = np.empty((blocks.shape[0], 4, 3), np.int32)
    pal[:, 0] = e0
    pal[:, 1] = e1
    pal[:, 2] = np.where(four[:, None], (2 * e0 + e1) // 3, (e0 + e1) // 2)
    pal[:, 3] = np.where(four[:, None], (e0 + 2 * e1) // 3, 0)
    alpha_pal = np.full((blocks.shape[0], 4), 255, np.uint8)
    alpha_pal[~four, 3] = 0
    shifts = np.arange(16, dtype=np.int64) * 2
    sel = (idx[:, None] >> shifts[None, :]) & 3
    rows = np.arange(blocks.shape[0])[:, None]
    return pal[rows, sel].astype(np.uint8), alpha_pal[rows, sel]


def _bc4_values(blocks: np.ndarray, signed: bool = False) -> np.ndarray:
    """(n, 8) BC4 blocks -> (n, 16) uint8 values (signed blocks remapped to 0..255)."""
    if signed:
        a0 = blocks[:, 0].view(np.int8).astype(np.float32).clip(-127, 127)
        a1 = blocks[:, 1].view(np.int8).astype(np.float32).clip(-127, 127)
        lo, hi = -127.0, 127.0
    else:
        a0 = blocks[:, 0].astype(np.float32)
        a1 = blocks[:, 1].astype(np.float32)
        lo, hi = 0.0, 255.0
    pal = np.empty((blocks.shape[0], 8), np.float32)
    pal[:, 0] = a0
    pal[:, 1] = a1
    eight = a0 > a1
    for i in range(1, 7):
        pal[:, i + 1] = np.where(eight, ((7 - i) * a0 + i * a1) / 7.0, 0)
    for i in range(1, 5):
        pal[~eight, i + 1] = ((5 - i) * a0[~eight] + i * a1[~eight]) / 5.0
    pal[~eight, 6] = lo
    pal[~eight, 7] = hi
    bits = np.zeros(blocks.shape[0], np.uint64)
    for i in range(6):
        bits |= blocks[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    shifts = (np.arange(16, dtype=np.uint64) * np.uint64(3))
    sel = ((bits[:, None] >> shifts[None, :]) & np.uint64(7)).astype(np.int64)
    vals = pal[np.arange(blocks.shape[0])[:, None], sel]
    if signed:
        vals = (vals + 127.0) * (255.0 / 254.0)
    return np.rint(vals).clip(0, 255).astype(np.uint8)


def _decode_bcn_numpy(codec: str, raw: bytes, width: int, height: int) -> np.ndarray:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    size = BLOCK_BYTES[codec]
    need = bw * bh * size
    if len(raw) < need:
        raise FormatError(f"{codec} data is truncated ({len(raw)} < {need} bytes)")
    blocks = np.frombuffer(raw, np.uint8, count=need).reshape(-1, size)
    n = blocks.shape[0]
    out = np.empty((n, 16, 4), np.uint8)
    if codec == "BC1":
        rgb, a = _bc1_colors(blocks, force_four=False)
        out[..., :3] = rgb
        out[..., 3] = a
    elif codec == "BC2":
        rgb, _ = _bc1_colors(blocks[:, 8:], force_four=True)
        out[..., :3] = rgb
        alpha = np.zeros((n, 16), np.uint8)
        for i in range(8):
            byte = blocks[:, i]
            alpha[:, 2 * i] = (byte & 0x0F) * 17
            alpha[:, 2 * i + 1] = (byte >> 4) * 17
        out[..., 3] = alpha
    elif codec == "BC3":
        rgb, _ = _bc1_colors(blocks[:, 8:], force_four=True)
        out[..., :3] = rgb
        out[..., 3] = _bc4_values(blocks[:, :8])
    elif codec in ("BC4", "BC4S"):
        v = _bc4_values(blocks, signed=codec == "BC4S")
        out[..., 0] = out[..., 1] = out[..., 2] = v
        out[..., 3] = 255
    elif codec in ("BC5", "BC5S"):
        signed = codec == "BC5S"
        out[..., 0] = _bc4_values(blocks[:, :8], signed)
        out[..., 1] = _bc4_values(blocks[:, 8:], signed)
        out[..., 2] = 0
        out[..., 3] = 255
    else:
        raise FormatError(f"numpy decoder does not handle {codec}")
    return _blocks_to_image(out, bw, bh, width, height)


def _decode_bc67(codec: str, raw: bytes, width: int, height: int) -> np.ndarray:
    pw, ph = ((width + 3) // 4) * 4, ((height + 3) // 4) * 4
    try:
        import texture2ddecoder

        fn = texture2ddecoder.decode_bc7 if codec == "BC7" else texture2ddecoder.decode_bc6
        bgra = np.frombuffer(fn(raw, pw, ph), np.uint8).reshape(ph, pw, 4)
        return np.ascontiguousarray(bgra[:height, :width, [2, 1, 0, 3]])
    except ImportError:
        pass
    import io

    from PIL import Image

    dds_bytes = build_dds(codec, width, height, raw)
    with Image.open(io.BytesIO(dds_bytes)) as im:
        return np.asarray(im.convert("RGBA"))


def _decode_uncompressed(info: DDSInfo, raw: bytes, width: int, height: int) -> np.ndarray:
    codec = info.codec
    n = width * height
    if codec == "MASK":
        bpp = info.bit_count // 8
        buf = np.frombuffer(raw, np.uint8, count=n * bpp).reshape(n, bpp)
        px = np.zeros(n, np.uint64)
        for i in range(bpp):
            px |= buf[:, i].astype(np.uint64) << np.uint64(8 * i)
        chans = []
        for mask, default in zip(info.masks, (0, 0, 0, 255)):
            if mask == 0:
                chans.append(np.full(n, default, np.uint8))
                continue
            shift = (mask & -mask).bit_length() - 1
            maxv = mask >> shift
            vals = (px >> np.uint64(shift)) & np.uint64(maxv)
            chans.append(np.rint(vals.astype(np.float64) * (255.0 / maxv)).astype(np.uint8))
        return np.stack(chans, -1).reshape(height, width, 4)
    bpp = PIXEL_BYTES[codec]
    buf = np.frombuffer(raw, np.uint8, count=n * bpp)
    if codec in ("R8G8B8A8", "B8G8R8A8", "B8G8R8X8"):
        img = buf.reshape(height, width, 4).copy()
        if codec != "R8G8B8A8":
            img = img[..., [2, 1, 0, 3]]
        if codec == "B8G8R8X8":
            img[..., 3] = 255
        return img
    out = np.zeros((height, width, 4), np.uint8)
    out[..., 3] = 255
    if codec == "R8G8":
        rg = buf.reshape(height, width, 2)
        out[..., :2] = rg
        return out
    if codec in ("R8", "A8"):
        v = buf.reshape(height, width)
        if codec == "A8":
            out[..., 3] = v
        else:
            out[..., 0] = out[..., 1] = out[..., 2] = v
        return out
    if codec in ("B5G6R5", "B5G5R5A1"):
        px = buf.view("<u2").reshape(height, width).astype(np.int32)
        if codec == "B5G6R5":
            rgb = _expand565(px)
            out[..., :3] = rgb
        else:
            r, g, b = (px >> 10) & 31, (px >> 5) & 31, px & 31
            out[..., 0], out[..., 1], out[..., 2] = (r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2)
            out[..., 3] = np.where(px & 0x8000, 255, 0)
        return out
    # High-precision formats come back as float32 in [0, 1] (floats are clamped)
    if codec == "R16":
        v = buf.view("<u2").reshape(height, width).astype(np.float32) / 65535.0
        return np.dstack([v, v, v, np.ones_like(v)])
    if codec in ("R16F", "R32F"):
        v = buf.view("<f2" if codec == "R16F" else "<f4").reshape(height, width).astype(np.float32)
        return np.dstack([v, v, v, np.ones_like(v)])
    if codec == "R16G16B16A16":
        return buf.view("<u2").reshape(height, width, 4).astype(np.float32) / 65535.0
    if codec in ("R16G16B16A16F", "R32G32B32A32F"):
        return buf.view("<f2" if codec.endswith("16F") else "<f4").reshape(height, width, 4).astype(np.float32)
    raise FormatError(f"Unsupported uncompressed codec {codec}")


def decode_surface(codec: str, raw: bytes, width: int, height: int, info: DDSInfo | None = None) -> np.ndarray:
    """Decode one mip level. Returns uint8 RGBA, or float32 RGBA for >8-bit formats."""
    if codec in ("BC6H", "BC7"):
        return _decode_bc67(codec, raw, width, height)
    if codec in BLOCK_BYTES:
        return _decode_bcn_numpy(codec, raw, width, height)
    if info is None:
        info = DDSInfo(width, height, 1, codec, 0)
    return _decode_uncompressed(info, raw, width, height)


def decode_dds(data: bytes, mip: int = 0) -> np.ndarray:
    info = parse_dds(data)
    mip = min(mip, info.mipmaps - 1)
    offset = info.data_offset
    for level in range(mip):
        offset += info.mip_size(level)[2]
    w, h, nbytes = info.mip_size(mip)
    raw = data[offset:offset + nbytes]
    if len(raw) < nbytes:
        raise FormatError(f"DDS mip {mip} is truncated")
    return decode_surface(info.codec, raw, w, h, info)


def build_dds(codec: str, width: int, height: int, data: bytes, mipmaps: int = 1, cubemap: bool = False) -> bytes:
    """Wrap raw surface data in a DDS header (DX10 header for codecs without a FourCC)."""
    fourcc = {"BC1": b"DXT1", "BC2": b"DXT3", "BC3": b"DXT5", "BC4": b"ATI1", "BC5": b"ATI2"}.get(codec)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | (DDSD_MIPMAPCOUNT if mipmaps > 1 else 0)
    if codec in BLOCK_BYTES:
        flags |= 0x80000
        pitch = max(1, (width + 3) // 4) * BLOCK_BYTES[codec]
    else:
        flags |= 0x8
        pitch = width * PIXEL_BYTES.get(codec, 4)
    caps = 0x1000 | ((0x8 | 0x400000) if mipmaps > 1 else 0) | (0x8 if cubemap else 0)
    caps2 = 0xFE00 if cubemap else 0
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<7I", header, 4, 124, flags, height, width, pitch, 0, mipmaps)
    dx10 = b""
    if fourcc is not None:
        struct.pack_into("<II4s5I", header, 0x4C, 32, DDPF_FOURCC, fourcc, 0, 0, 0, 0, 0)
    elif codec == "R8G8B8A8":
        struct.pack_into("<II4s5I", header, 0x4C, 32, DDPF_RGB | DDPF_ALPHAPIXELS, b"\0\0\0\0", 32,
                         0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
    elif codec == "B8G8R8A8":
        struct.pack_into("<II4s5I", header, 0x4C, 32, DDPF_RGB | DDPF_ALPHAPIXELS, b"\0\0\0\0", 32,
                         0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    else:
        dxgi = CODEC_DXGI.get(codec)
        if dxgi is None:
            raise FormatError(f"Cannot build a DDS header for {codec}")
        struct.pack_into("<II4s5I", header, 0x4C, 32, DDPF_FOURCC, b"DX10", 0, 0, 0, 0, 0)
        dx10 = struct.pack("<5I", dxgi, 3, 0x4 if cubemap else 0, 1, 0)
    struct.pack_into("<4I", header, 0x6C, caps, caps2, 0, 0)
    return bytes(header) + dx10 + data
