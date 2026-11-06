"""Multilayer masks (``.mlmask``): one grayscale coverage map per layer.

A ``Multilayer_Mask`` keeps its layers as tiles in one BC4 atlas. A tile
table says, for each tile position of the full-size map (and of a smaller
map used where no full-size tile exists), which layers have a tile there and
where in the atlas it sits (``tileDecl``: 10-bit x and y tile indices, then
4-bit x and y shifts for tiles that cover more than one map tile). Tiles are
padded by one texel in the atlas. Decoding follows WolvenKit's MlmaskTools
(GPL, reference only), vectorised over the whole map.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ....textures import dds
from ...fromsoft.formats.binary import FormatError
from .cr2w import Cr2wFile, Red

PADDING = 2


@dataclass
class MlMask:
    width: int
    height: int
    layers: int
    tile_size: int
    atlas: np.ndarray  # (atlas height, atlas width) uint8
    tiles: np.ndarray  # uint32 table
    width_low: int = 0
    height_low: int = 0

    def layer(self, index: int) -> np.ndarray:
        """Layer ``index`` as (height, width) uint8, 0 where no tile covers a texel."""
        if not 0 <= index < self.layers:
            raise FormatError(f"The mask has {self.layers} layers, not {index + 1}")
        w, h, ts = self.width, self.height, self.tile_size
        out = np.zeros((h, w), np.uint8)
        done = np.zeros((h, w), bool)
        full_w, full_h = -(-w // ts), -(-h // ts)
        self._decode(index, out, done, 0, 1, full_w)
        small = 1 if (self.width_low == 0 or w < self.width_low) else max(w // self.width_low, 1)
        self._decode(index, out, done, full_w * full_h, small, -(-(w // small) // ts))  # the smaller map fills in
        return out

    def _decode(self, index: int, out: np.ndarray, done: np.ndarray, offset: int, scale: int, width_in_tiles: int) \
            -> None:
        w, h, ts = self.width, self.height, self.tile_size
        tiles = self.tiles
        ys, xs = np.mgrid[0:h, 0:w]
        tx = xs // ts // scale
        ty = ys // ts // scale
        tile_index = (width_in_tiles * ty + tx + offset).astype(np.int64)
        ok = (tile_index * 2 + 1 < len(tiles)) & ~done
        if not ok.any():
            return
        ti = np.where(ok, tile_index, 0)
        param_offset = tiles[ti * 2].astype(np.int64)
        param_bits = tiles[ti * 2 + 1]
        ok &= (param_bits >> index) & 1 == 1
        below = param_bits & ((1 << index) - 1)
        extra = _popcount(below)
        decl_index = param_offset + extra
        ok &= decl_index < len(tiles)
        decl = tiles[np.where(ok, decl_index, 0)]
        dx = decl & 0x3FF
        dy = (decl >> 10) & 0x3FF
        sx = (decl >> 20) & 0xF
        sy = (decl >> 24) & 0xF
        local_x = np.minimum((xs >> sx) % ts, ts - 1)
        local_y = np.minimum((ys >> sy) % ts, ts - 1)
        atlas_tile = ts + PADDING
        ux = local_x + 1 + dx * atlas_tile
        uy = local_y + 1 + dy * atlas_tile
        ah, aw = self.atlas.shape
        ok &= (ux < aw) & (uy < ah)
        values = self.atlas[np.where(ok, uy, 0), np.where(ok, ux, 0)]
        out[ok] = values[ok]
        done |= ok


def _popcount(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.uint32)
    v = v - ((v >> 1) & 0x55555555)
    v = (v & 0x33333333) + ((v >> 2) & 0x33333333)
    v = (v + (v >> 4)) & 0x0F0F0F0F
    return ((v * 0x01010101) & 0xFFFFFFFF) >> 24


def read_mlmask(f: Cr2wFile) -> MlMask:
    root = f.root
    if root.cls != "Multilayer_Mask":
        raise FormatError(f"Not a multilayer mask: the file holds a {root.cls}")
    res = root.get("renderResourceBlob")
    blob = f.chunk(res.get("renderResourceBlobPC")) if isinstance(res, Red) else None
    if not isinstance(blob, Red):
        raise FormatError("The mask has no PC render blob")
    header = blob.get("header")
    if not isinstance(header, Red):
        raise FormatError("The mask blob has no header")
    aw, ah = int(header.get("atlasWidth", 0)), int(header.get("atlasHeight", 0))
    w, h = int(header.get("maskWidth", 0)), int(header.get("maskHeight", 0))
    layers = int(header.get("numLayers", 0))
    ts = int(header.get("maskTileSize", 0))
    if min(aw, ah, w, h, layers, ts) <= 0:
        raise FormatError("The mask header is empty")
    atlas_raw = f.buffer(blob.get("atlasData"))
    atlas = dds.decode_surface("BC4", atlas_raw, aw, ah)[:, :, 0]
    tiles = np.frombuffer(f.buffer(blob.get("tilesData")), dtype="<u4").astype(np.uint32)
    return MlMask(w, h, layers, ts, np.ascontiguousarray(atlas), tiles,
                  int(header.get("maskWidthLow", 0)), int(header.get("maskHeightLow", 0)))


__all__ = ["MlMask", "read_mlmask"]
