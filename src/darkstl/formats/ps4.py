"""PS4 (Bloodborne) texture un-tiling.

PS4 TPFs store headerless pixel data in the GPU's tiled layout. Block
compressed surfaces are stored as 8x8-block tiles (32x32 pixels) laid out row
by row, with the blocks inside each tile in Morton (Z) order. This matches
the deswizzler in SoulsFormats' Headerizer, generalised to every block size.

This path has only been checked against synthetic data so far; set
``DARKSTL_PS4_TILING=linear`` to bypass it if real files decode scrambled.
"""

from __future__ import annotations

import os

import numpy as np

from . import dds
from .binary import FormatError

TILE_BLOCKS = 8  # tiles are 8x8 blocks (32x32 pixels)


def _morton_xy(count: int) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(count)
    x = np.zeros_like(t)
    y = np.zeros_like(t)
    for bit in range(8):
        x |= ((t >> (2 * bit)) & 1) << bit
        y |= ((t >> (2 * bit + 1)) & 1) << bit
    return x, y


def untile(data: bytes, width_elems: int, height_elems: int, elem_bytes: int) -> tuple[bytes, int]:
    """Reorder tiled elements to linear rows.

    ``width_elems``/``height_elems`` are the surface size in elements (blocks
    for BCn). Returns ``(linear_bytes, consumed_bytes)`` where the linear
    surface is cropped to the unpadded size.
    """
    pw = -(-width_elems // TILE_BLOCKS) * TILE_BLOCKS
    ph = -(-height_elems // TILE_BLOCKS) * TILE_BLOCKS
    total = pw * ph * elem_bytes
    if len(data) < total:
        raise FormatError(f"PS4 surface is truncated ({len(data)} < {total} bytes)")
    elems = np.frombuffer(data, np.uint8, count=total).reshape(-1, elem_bytes)
    tiles_x, tiles_y = pw // TILE_BLOCKS, ph // TILE_BLOCKS
    mx, my = _morton_xy(TILE_BLOCKS * TILE_BLOCKS)
    # Source index i -> tile (ty, tx), in-tile morton position
    tile_index = np.arange(tiles_x * tiles_y)
    ty, tx = np.divmod(tile_index, tiles_x)
    dst_y = (ty[:, None] * TILE_BLOCKS + my[None, :]).reshape(-1)
    dst_x = (tx[:, None] * TILE_BLOCKS + mx[None, :]).reshape(-1)
    linear = np.empty((ph, pw, elem_bytes), np.uint8)
    linear[dst_y, dst_x] = elems
    return linear[:height_elems, :width_elems].tobytes(), total


def headerize_ps4(texture) -> bytes:
    """Build a DDS file (top mip only) from a PS4 TPF texture entry."""
    from .tpf import TPF_FORMATS

    codec = dds.DXGI_CODECS.get(texture.dxgi_format) if texture.dxgi_format else None
    if codec is None:
        entry = TPF_FORMATS.get(texture.format)
        if entry is None:
            raise FormatError(f"Unknown PS4 texture format {texture.format} (DXGI {texture.dxgi_format})")
        codec = entry[0]
    width, height = texture.width, texture.height
    if width <= 0 or height <= 0:
        raise FormatError(f"PS4 texture {texture.name!r} has no dimensions")
    if codec in dds.BLOCK_BYTES:
        elem = dds.BLOCK_BYTES[codec]
        we, he = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    elif codec in dds.PIXEL_BYTES:
        elem = dds.PIXEL_BYTES[codec]
        we, he = width, height
    else:
        raise FormatError(f"Unsupported PS4 codec {codec}")
    mode = os.environ.get("DARKSTL_PS4_TILING", "morton").lower()
    if mode == "linear":
        surface = texture.data[: we * he * elem]
    else:
        surface, _ = untile(texture.data, we, he, elem)
    return dds.build_dds(codec, width, height, surface)


def tile(linear: bytes, width_elems: int, height_elems: int, elem_bytes: int) -> bytes:
    """Inverse of :func:`untile` (pads with zeros); used to build test data."""
    pw = -(-width_elems // TILE_BLOCKS) * TILE_BLOCKS
    ph = -(-height_elems // TILE_BLOCKS) * TILE_BLOCKS
    src = np.zeros((ph, pw, elem_bytes), np.uint8)
    src[:height_elems, :width_elems] = np.frombuffer(linear, np.uint8).reshape(height_elems, width_elems, elem_bytes)
    tiles_x, tiles_y = pw // TILE_BLOCKS, ph // TILE_BLOCKS
    mx, my = _morton_xy(TILE_BLOCKS * TILE_BLOCKS)
    tile_index = np.arange(tiles_x * tiles_y)
    ty, tx = np.divmod(tile_index, tiles_x)
    sy = (ty[:, None] * TILE_BLOCKS + my[None, :]).reshape(-1)
    sx = (tx[:, None] * TILE_BLOCKS + mx[None, :]).reshape(-1)
    return src[sy, sx].tobytes()
