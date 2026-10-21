"""Simulated block compression, for testing how artifacts survive integration.

The games' textures are BC1/BC5/BC7 compressed. These encoders are not
bit-exact copies of any real encoder; they reproduce what matters for
displacement: 4x4 blocks, a pair of 8-bit (BC5) or RGB565 (BC1) endpoints
per block, and a few interpolated levels in between.
"""

from __future__ import annotations

import numpy as np


def _blocks(a: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """(H, W, C) -> (H/4, W/4, 16, C), padding by edge repeat."""
    h, w = a.shape[:2]
    ph, pw = -h % 4, -w % 4
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw), (0, 0)), mode="edge")
    bh, bw = a.shape[0] // 4, a.shape[1] // 4
    b = a.reshape(bh, 4, bw, 4, -1).transpose(0, 2, 1, 3, 4).reshape(bh, bw, 16, -1)
    return b, (h, w)


def _unblocks(b: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    bh, bw = b.shape[:2]
    a = b.reshape(bh, bw, 4, 4, -1).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, -1)
    return a[: size[0], : size[1]]


def bc5(rgba: np.ndarray) -> np.ndarray:
    """Red and green through BC5 (per channel: two 8-bit endpoints, 8 levels); blue and alpha dropped."""
    b, size = _blocks(rgba[..., :2].astype(np.float64))
    lo = np.rint(b.min(axis=2, keepdims=True))
    hi = np.rint(b.max(axis=2, keepdims=True))
    span = np.maximum(hi - lo, 1e-9)
    t = np.rint((b - lo) / span * 7) / 7
    rg = _unblocks(np.clip(np.rint(lo + t * (hi - lo)), 0, 255), size)
    out = np.zeros(rgba.shape[:2] + (4,), np.uint8)
    out[..., :2] = rg
    out[..., 3] = 255
    return out


def bc1(rgba: np.ndarray) -> np.ndarray:
    """RGB through BC1 (RGB565 endpoints along the block's main colour axis, 4 levels)."""
    b, size = _blocks(rgba[..., :3].astype(np.float64))
    mean = b.mean(axis=2, keepdims=True)
    c = b - mean
    cov = np.einsum("...ki,...kj->...ij", c, c)
    _, vecs = np.linalg.eigh(cov + np.eye(3) * 1e-9)
    axis = vecs[..., :, -1][..., None, :]
    proj = (c * axis).sum(-1, keepdims=True)
    lo = mean + proj.min(axis=2, keepdims=True) * axis
    hi = mean + proj.max(axis=2, keepdims=True) * axis
    q = np.array([31, 63, 31], np.float64) / 255.0
    lo = np.rint(np.clip(lo, 0, 255) * q) / q
    hi = np.rint(np.clip(hi, 0, 255) * q) / q
    d = hi - lo
    t = ((b - lo) * d).sum(-1, keepdims=True) / np.maximum((d * d).sum(-1, keepdims=True), 1e-9)
    t = np.rint(np.clip(t, 0, 1) * 3) / 3
    rgb = _unblocks(np.clip(np.rint(lo + t * d), 0, 255), size)
    out = np.zeros(rgba.shape[:2] + (4,), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255
    return out
