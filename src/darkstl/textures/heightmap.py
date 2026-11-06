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


def slopes_to_normal_map(p: np.ndarray, q: np.ndarray, convention: str = "directx") -> np.ndarray:
    """The inverse of :func:`normal_map_slopes`: slopes drawn as an RGBA uint8 normal map."""
    inv = 1.0 / np.sqrt(1.0 + p * p + q * q)
    nx = -p * inv
    ny = (-q if convention == "directx" else q) * inv
    out = np.empty(p.shape + (4,), np.uint8)
    out[..., 0] = np.clip(np.rint((nx * 0.5 + 0.5) * 255), 0, 255)
    out[..., 1] = np.clip(np.rint((ny * 0.5 + 0.5) * 255), 0, 255)
    out[..., 2] = np.clip(np.rint((inv * 0.5 + 0.5) * 255), 0, 255)
    out[..., 3] = 255
    return out


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
    return height_from_slopes(p, q, mask, detail_fraction, convention, confidence)


def height_from_slopes(p: np.ndarray, q: np.ndarray, mask: np.ndarray | None = None,
                       detail_fraction: float = 1.0 / 16.0, convention: str = "",
                       confidence: float = 0.0) -> HeightField:
    """:func:`height_from_normal_map` for slopes already read (and maybe cleaned or upsampled)."""
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
    los, his = px.min(axis=1), px.max(axis=1)
    inside = (los[:, 0] >= 0) & (los[:, 1] >= 0) & (his[:, 0] <= width) & (his[:, 1] <= height)
    for tri, lo, hi, ok in zip(px.tolist(), los, his, inside):
        pts = [tuple(v) for v in tri]
        draw.polygon(pts, fill=255)
        if ok:
            continue
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


def sample_bicubic(img: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Catmull-Rom sample of an (H, W) or (H, W, C) array at DirectX-convention UVs, with wrap-around.

    Smoother than bilinear between texels, so a mesh much denser than the
    texture does not show the texel grid as facets.
    """
    h, w = img.shape[:2]
    flat = img.reshape(h * w, -1)
    x = np.asarray(uv[:, 0], np.float64) * w - 0.5
    y = np.asarray(uv[:, 1], np.float64) * h - 0.5
    x0 = np.floor(x)
    y0 = np.floor(y)
    tx = (x - x0).astype(np.float32)
    ty = (y - y0).astype(np.float32)
    x0 = x0.astype(np.int64)
    y0 = y0.astype(np.int64)

    def weights(t):
        t2 = t * t
        t3 = t2 * t
        return (-0.5 * t3 + t2 - 0.5 * t, 1.5 * t3 - 2.5 * t2 + 1.0, -1.5 * t3 + 2.0 * t2 + 0.5 * t, 0.5 * t3 - 0.5 * t2)

    wx, wy = weights(tx), weights(ty)
    cols = [(x0 + b - 1) % w for b in range(4)]
    out = np.zeros((len(x), flat.shape[1]), np.float64)
    for a in range(4):
        row = ((y0 + a - 1) % h) * w
        acc = flat[row + cols[0]] * wx[0][:, None]
        for b in range(1, 4):
            acc += flat[row + cols[b]] * wx[b][:, None]
        out += acc * wy[a][:, None]
    return out[:, 0] if img.ndim == 2 else out


def slope_bias(p: np.ndarray, q: np.ndarray, mask: np.ndarray | None = None) -> tuple[float, float]:
    """The slope a normal map reads where its surface is flat (the most common value).

    Normal maps cannot store "flat" exactly: 8-bit channels have no exact
    middle, and BC1's 5/6-bit endpoints round it further. That leaves a small
    constant slope everywhere, which integrates into a tilt across the whole
    part. The median over the near-flat texels estimates it; real relief is
    left alone, and with too few flat texels to tell, nothing is removed.
    """
    used = mask if mask is not None and mask.any() else np.ones(p.shape, bool)
    flat = used & (np.abs(p) < 0.06) & (np.abs(q) < 0.06)
    if flat.sum() < 0.05 * used.sum():
        return 0.0, 0.0
    return float(np.median(p[flat])), float(np.median(q[flat]))


def smooth_slopes(p: np.ndarray, q: np.ndarray, sigma: float = 0.7) -> tuple[np.ndarray, np.ndarray]:
    """A sub-texel blur that melts 4x4 block-compression steps before they are integrated."""
    if sigma <= 0:
        return p, q
    return (ndimage.gaussian_filter(p, sigma, mode="wrap"), ndimage.gaussian_filter(q, sigma, mode="wrap"))


def edge_flow(p: np.ndarray, q: np.ndarray, sigma: float = 0.7, rho: float = 2.0):
    """The direction edges run in (``tx``, ``ty``) and how much each texel sits on a clear, straight-ish
    edge (0..1), from the slopes' structure tensor. ``rho`` (texels) is the area the direction is judged
    over: wide enough that a staircase reads as the line it approximates."""
    gp = [ndimage.gaussian_filter(p, sigma, order=o, mode="wrap") for o in ((0, 1), (1, 0))]
    gq = [ndimage.gaussian_filter(q, sigma, order=o, mode="wrap") for o in ((0, 1), (1, 0))]
    jxx = ndimage.gaussian_filter(gp[0] * gp[0] + gq[0] * gq[0], rho, mode="wrap")
    jyy = ndimage.gaussian_filter(gp[1] * gp[1] + gq[1] * gq[1], rho, mode="wrap")
    jxy = ndimage.gaussian_filter(gp[0] * gp[1] + gq[0] * gq[1], rho, mode="wrap")
    trace = jxx + jyy
    coherence = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2) / np.maximum(trace, 1e-12)
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy)  # across the edge
    mag = np.sqrt(trace)
    ref = float(np.percentile(mag, 90)) + 1e-9
    # edges along the texel grid have no steps to take out (and stripes running that way keep their grain);
    # the steps come with edges at an angle, in full from 20 degrees off the grid
    oblique = np.clip(np.abs(np.sin(2 * theta)) / np.sin(np.radians(40)), 0, 1)
    weight = np.clip((coherence - 0.3) / 0.5, 0, 1) * np.clip(mag / ref, 0, 1) * oblique
    return (-np.sin(theta)).astype(np.float32), np.cos(theta).astype(np.float32), weight.astype(np.float32)


def smooth_jagged_edges(p: np.ndarray, q: np.ndarray, length: float = 2.0,
                        strength: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Take the stair-steps out of edges a normal map was baked or sampled with too few texels for.

    Each texel is averaged with the texels up to ``length`` texels away along the edge it lies on,
    following the edge as it curves (line integral convolution), so the edge stays as sharp across
    as before but its steps blend into the line they approximate. Only clear edges are touched:
    fine grain and noise have no single direction and are left as they are. An upscaler can't do
    this itself: it learnt to keep the steps it is shown, and at any factor they just get bigger.
    """
    if strength <= 0 or length <= 0:
        return p, q
    h, w = p.shape
    tx, ty, wt = edge_flow(p, q)
    step = 0.5
    n = int(np.ceil(length / step))
    sigma = length / 2
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    acc_p, acc_q = p.astype(np.float32), q.astype(np.float32)
    # cubic reads: linear ones between texels would also blur across the edge, the one way it must stay sharp
    cp, cq = (ndimage.spline_filter(a, 3, np.float32, mode="grid-wrap") for a in (acc_p, acc_q))
    total = 1.0
    for sign in (1.0, -1.0):
        x, y = xx, yy
        dx, dy = tx * sign, ty * sign
        for k in range(1, n + 1):
            cx = ndimage.map_coordinates(tx, [y, x], order=1, mode="grid-wrap")
            cy = ndimage.map_coordinates(ty, [y, x], order=1, mode="grid-wrap")
            flip = np.where(cx * dx + cy * dy < 0, -1.0, 1.0).astype(np.float32)  # a line field: keep going the same way
            dx, dy = cx * flip, cy * flip
            x, y = x + step * dx, y + step * dy
            g = float(np.exp(-0.5 * (k * step / sigma) ** 2))
            acc_p = acc_p + g * ndimage.map_coordinates(cp, [y, x], order=3, mode="grid-wrap", prefilter=False)
            acc_q = acc_q + g * ndimage.map_coordinates(cq, [y, x], order=3, mode="grid-wrap", prefilter=False)
            total += g
    dp, dq = acc_p / total - p, acc_q / total - q
    # A stair-step moves a texel by a good part of the edge's height; grain along the edge moves it by about
    # its own small size. Changes no bigger than the texture's grain are left out, so the grain stays.
    grain = float(np.median(np.hypot(p - ndimage.gaussian_filter(p, 1.0, mode="wrap"),
                                     q - ndimage.gaussian_filter(q, 1.0, mode="wrap")))) * 1.5 + 1e-6
    size = np.hypot(dp, dq)
    a = np.clip(wt * strength, 0, 1) * np.clip((size - grain) / grain, 0, 1)
    return (p + a * dp).astype(np.float32), (q + a * dq).astype(np.float32)


def luminance(rgba: np.ndarray) -> np.ndarray:
    f = to_float(rgba)
    if f.ndim == 2 or f.shape[2] < 3:
        return f if f.ndim == 2 else f[..., 0]
    return 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]


def resize(img: np.ndarray, shape: tuple[int, int], order: int = 1) -> np.ndarray:
    """Resample a 2D float array to ``shape`` (rows, cols), wrapping at the borders."""
    if img.shape == tuple(shape):
        return img
    zy, zx = shape[0] / img.shape[0], shape[1] / img.shape[1]
    out = ndimage.zoom(img, (zy, zx), order=order, mode="grid-wrap", grid_mode=True)
    return out[: shape[0], : shape[1]]


def guided_upsample(p: np.ndarray, q: np.ndarray, guide: np.ndarray | None, factor: int,
                    range_sigma: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """Upsample slopes by ``factor``, snapping their edges to the guide's (joint bilateral upsampling).

    ``guide`` is a luminance image (any size; resized to the output). Without
    one this is plain cubic upsampling.
    """
    if factor <= 1:
        return p, q
    shape = (p.shape[0] * factor, p.shape[1] * factor)
    up_p, up_q = resize(p, shape, 3), resize(q, shape, 3)
    if guide is None:
        return up_p, up_q
    g = resize(guide.astype(np.float64), shape, 1)
    step = max(factor // 2, 1)
    acc_p = np.zeros(shape)
    acc_q = np.zeros(shape)
    acc_w = np.zeros(shape)
    spatial = 2.0 * (factor * 0.75) ** 2
    for dy in range(-factor, factor + 1, step):
        for dx in range(-factor, factor + 1, step):
            gs = np.roll(g, (dy, dx), axis=(0, 1))
            wgt = np.exp(-(dx * dx + dy * dy) / spatial - (g - gs) ** 2 / (2 * range_sigma**2))
            acc_p += wgt * np.roll(up_p, (dy, dx), axis=(0, 1))
            acc_q += wgt * np.roll(up_q, (dy, dx), axis=(0, 1))
            acc_w += wgt
    return (acc_p / acc_w).astype(np.float32), (acc_q / acc_w).astype(np.float32)


def albedo_detail(rgba: np.ndarray, band: float = 1.5, delight: float = 12.0) -> np.ndarray:
    """Fine relief guessed from the albedo: darker than its surroundings reads as deeper.

    The albedo is first divided by a heavy blur of itself, which removes baked
    shading and broad colour changes ("delighting"); then only the band finer
    than ``band`` texels is kept. Returns roughly -1..1 (unitless).
    """
    lum = luminance(rgba).astype(np.float64)
    lit = lum / np.maximum(ndimage.gaussian_filter(lum, delight, mode="wrap"), 1e-3)
    fine = lit - ndimage.gaussian_filter(lit, band, mode="wrap")
    s = float(np.percentile(np.abs(fine), 99)) or 1.0
    return np.clip(fine / s, -1.5, 1.5).astype(np.float32)


def edge_hint(rgba: np.ndarray) -> np.ndarray:
    """Where a mask texture (specular, metallic) changes sharply: 0 flat, 1 a clear material edge."""
    lum = ndimage.gaussian_filter(luminance(rgba).astype(np.float64), 0.8, mode="wrap")
    mag = np.hypot(ndimage.sobel(lum, 0, mode="wrap"), ndimage.sobel(lum, 1, mode="wrap"))
    s = float(np.percentile(mag, 99.5))
    if s <= 1e-6:
        return np.zeros_like(lum, np.float32)
    return np.clip(mag / s, 0, 1).astype(np.float32)


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
