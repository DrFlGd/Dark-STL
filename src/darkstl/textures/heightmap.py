"""Height fields for displacement.

The games mostly ship tangent-space normal maps, not height maps. A normal
map stores the surface slope at every texel, so integrating the slopes gives
back the relief it was baked from. Integration uses a least-squares Poisson
solve (DCT, Neumann borders), restricted to texels that the model's UVs
actually cover, followed by a high-pass that removes the slow drift every
integration accumulates (large-scale shape already lives in the mesh).

Heights produced here are measured in texels: multiply by the world size of a
texel (see :func:`texel_world_size`) to get model units.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.fft import dctn, idctn


def to_float(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.uint8:
        return img.astype(np.float32) / 255.0
    return img.astype(np.float32)


@dataclass
class HeightField:
    height: np.ndarray  # (H, W) float32
    units: str  # "texels" (from a normal map) or "normalized" (grayscale map, 0 = mid level)
    convention: str = ""  # normal map green convention that was used
    confidence: float = 0.0  # how clearly the convention was detected (curl ratio)

    @property
    def shape(self) -> tuple[int, int]:
        return self.height.shape


def normal_map_slopes(rgba: np.ndarray, convention: str = "directx", max_slope: float = 4.0):
    """Per-texel height gradient ``(dh/dx, dh/dy)`` with x right and y down the image."""
    f = to_float(rgba)
    nx = f[..., 0] * 2.0 - 1.0
    ny = f[..., 1] * 2.0 - 1.0
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
    nz = np.maximum(nz, 1.0 / (max_slope + 1e-6))
    p = -nx / nz
    q = (-ny / nz) if convention == "directx" else (ny / nz)
    return np.clip(p, -max_slope, max_slope), np.clip(q, -max_slope, max_slope)


def _staggered(p: np.ndarray, q: np.ndarray, mask: np.ndarray | None):
    """Slopes at texel centres -> forward differences between neighbours, zeroed across mask borders."""
    fx = np.zeros_like(p)
    fy = np.zeros_like(q)
    fx[:, :-1] = 0.5 * (p[:, :-1] + p[:, 1:])
    fy[:-1, :] = 0.5 * (q[:-1, :] + q[1:, :])
    if mask is not None:
        fx[:, :-1] *= mask[:, :-1] & mask[:, 1:]
        fy[:-1, :] *= mask[:-1, :] & mask[1:, :]
    return fx, fy


def curl_energy(p: np.ndarray, q: np.ndarray, mask: np.ndarray | None = None) -> float:
    fx, fy = _staggered(p, q, mask)
    curl = (fy[:-1, 1:] - fy[:-1, :-1]) - (fx[1:, :-1] - fx[:-1, :-1])
    active = (np.abs(p) + np.abs(q))[:-1, :-1] > 1e-3
    if mask is not None:
        active &= mask[:-1, :-1]
    if not active.any():
        return 0.0
    return float(np.mean(np.abs(curl[active])))


def detect_convention(rgba: np.ndarray, mask: np.ndarray | None = None,
                      sigma: float = 1.5) -> tuple[str, float]:
    """Pick the green-channel convention whose slopes are closest to curl-free.

    Slopes from a real height field have zero curl. Reading the green channel
    with the wrong sign makes the curl large, so comparing the two is a
    reliable test. Block compression adds texel-sized noise whose curl can
    drown that difference (Bloodborne's coat and body maps read as OpenGL
    without this), so the slopes are blurred by ``sigma`` texels first.
    Returns ``(convention, ratio)`` where ratio > 1 means the choice is clear.
    """
    p, q_dx = normal_map_slopes(rgba, "directx")
    if sigma > 0:
        p, q_dx = ndimage.gaussian_filter(p, sigma), ndimage.gaussian_filter(q_dx, sigma)
    e_dx = curl_energy(p, q_dx, mask)
    e_gl = curl_energy(p, -q_dx, mask)
    if e_dx == e_gl:
        return "directx", 1.0
    if e_dx < e_gl:
        return "directx", (e_gl + 1e-9) / (e_dx + 1e-9)
    return "opengl", (e_dx + 1e-9) / (e_gl + 1e-9)


def integrate_slopes(p: np.ndarray, q: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Least-squares height field whose forward differences best match ``(p, q)``."""
    fx, fy = _staggered(p.astype(np.float64), q.astype(np.float64), mask)
    h, w = p.shape
    div = fx.copy()
    div[:, 1:] -= fx[:, :-1]
    div += fy
    div[1:, :] -= fy[:-1, :]
    d = dctn(div, type=2, norm="ortho")
    ky = 2.0 * np.cos(np.pi * np.arange(h) / h) - 2.0
    kx = 2.0 * np.cos(np.pi * np.arange(w) / w) - 2.0
    denom = ky[:, None] + kx[None, :]
    denom[0, 0] = 1.0
    d /= denom
    d[0, 0] = 0.0
    return idctn(d, type=2, norm="ortho").astype(np.float32)


def highpass(h: np.ndarray, sigma: float, mask: np.ndarray | None = None) -> np.ndarray:
    """Subtract a (mask-normalised) Gaussian blur so only detail remains."""
    if sigma <= 0:
        return h
    if mask is None:
        return h - ndimage.gaussian_filter(h, sigma, mode="reflect")
    m = mask.astype(np.float32)
    num = ndimage.gaussian_filter(h * m, sigma, mode="reflect")
    den = ndimage.gaussian_filter(m, sigma, mode="reflect")
    low = num / np.maximum(den, 1e-4)
    return (h - low) * m


def height_from_normal_map(rgba: np.ndarray, convention: str = "auto", mask: np.ndarray | None = None,
                           detail_fraction: float = 1.0 / 16.0) -> HeightField:
    """Integrate a tangent-space normal map into a height field in texel units.

    ``detail_fraction`` sets the high-pass radius as a fraction of the larger
    texture dimension (0 disables the high-pass).
    """
    confidence = 0.0
    if convention == "auto":
        convention, confidence = detect_convention(rgba, mask)
    p, q = normal_map_slopes(rgba, convention)
    h = integrate_slopes(p, q, mask)
    if detail_fraction > 0:
        h = highpass(h, detail_fraction * max(h.shape), mask)
    elif mask is not None:
        h = h * mask
    if mask is not None and mask.any():
        h = h - np.median(h[mask])
        h = h * mask
    return HeightField(h.astype(np.float32), "texels", convention, confidence)


def height_from_grayscale(rgba: np.ndarray, mid_level: float = 0.5) -> HeightField:
    f = to_float(rgba)
    lum = f[..., 0] if f.ndim == 3 else f
    if f.ndim == 3 and f.shape[2] >= 3 and not np.allclose(f[..., 0], f[..., 1]):
        lum = 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]
    return HeightField((lum - mid_level).astype(np.float32), "normalized")


# ---------------------------------------------------------------------------
# UV-space helpers


def uv_coverage_mask(corner_uvs: np.ndarray, width: int, height: int, dilate: int = 2) -> np.ndarray:
    """Rasterise UV triangles (wrapped into the unit square) into a coverage mask."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    uv = np.asarray(corner_uvs, np.float64)
    shift = np.floor(uv.mean(axis=1))
    uv = uv - shift[:, None, :]
    px = uv * np.array([width, height])
    for tri in px:
        pts = [tuple(v) for v in tri]
        draw.polygon(pts, fill=255)
        lo, hi = tri.min(0), tri.max(0)
        # Triangles hanging over the edge also cover the wrapped side
        for dx in (-width, 0, width):
            for dy in (-height, 0, height):
                if (dx or dy) and hi[0] + dx >= 0 and lo[0] + dx <= width and hi[1] + dy >= 0 and lo[1] + dy <= height:
                    draw.polygon([(x + dx, y + dy) for x, y in pts], fill=255)
    mask = np.asarray(img) > 0
    if dilate > 0:
        mask = ndimage.binary_dilation(mask, iterations=dilate)
    return mask


def texel_world_size(vertices: np.ndarray, faces: np.ndarray, corner_uvs: np.ndarray,
                     width: int, height: int) -> float:
    """Median world length of one texel over the given faces (area weighted)."""
    tri = vertices[faces]
    a3 = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    uv = corner_uvs.astype(np.float64) * np.array([width, height])
    e1 = uv[:, 1] - uv[:, 0]
    e2 = uv[:, 2] - uv[:, 0]
    a2 = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
    ok = (a2 > 1e-6) & (a3 > 0)
    if not ok.any():
        return 0.0
    ratio = np.sqrt(a3[ok] / a2[ok])
    w = a3[ok]
    order = np.argsort(ratio)
    cw = np.cumsum(w[order])
    return float(ratio[order][np.searchsorted(cw, cw[-1] / 2)])


def sample_bilinear(img: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Sample a 2D array at DirectX-convention UVs (v down) with wrap-around."""
    h, w = img.shape[:2]
    x = np.asarray(uv[:, 0], np.float64) * w - 0.5
    y = np.asarray(uv[:, 1], np.float64) * h - 0.5
    x0 = np.floor(x)
    y0 = np.floor(y)
    fx = (x - x0)[:, None] if img.ndim == 3 else x - x0
    fy = (y - y0)[:, None] if img.ndim == 3 else y - y0
    x0 = x0.astype(np.int64) % w
    y0 = y0.astype(np.int64) % h
    x1 = (x0 + 1) % w
    y1 = (y0 + 1) % h
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return top * (1 - fy) + bot * fy
