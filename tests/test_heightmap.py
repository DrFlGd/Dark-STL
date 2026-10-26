import numpy as np
import pytest

from darkstl.textures import heightmap as hm


def bumps(n=128):
    y, x = np.mgrid[0:n, 0:n] / n
    h = (np.exp(-((x - 0.3) ** 2 + (y - 0.4) ** 2) / 0.005) * 6
         + np.sin(2 * np.pi * x * 6) * np.cos(2 * np.pi * y * 4) * 1.5)
    return h  # in texels


def encode_normals(h, convention="directx"):
    gy, gx = np.gradient(h)  # per texel; gy along rows (down)
    n = np.stack([-gx, -gy if convention == "directx" else gy, np.ones_like(h)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    rgba = np.zeros(h.shape + (4,), np.uint8)
    rgba[..., :3] = np.clip(np.rint((n * 0.5 + 0.5) * 255), 0, 255)
    rgba[..., 3] = 255
    return rgba


def corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def test_integrate_exact_gradients():
    h = bumps()
    p = np.zeros_like(h)
    q = np.zeros_like(h)
    p[:, :-1] = h[:, 1:] - h[:, :-1]
    q[:-1, :] = h[1:, :] - h[:-1, :]
    # integrate_slopes averages neighbouring slopes onto edges, so feed centred slopes
    gy, gx = np.gradient(h)
    rec = hm.integrate_slopes(gx, gy)
    assert corr(rec, h) > 0.995


@pytest.mark.parametrize("convention", ["directx", "opengl"])
def test_normal_map_round_trip_and_convention(convention):
    h = bumps()
    rgba = encode_normals(h, convention)
    detected, ratio = hm.detect_convention(rgba)
    assert detected == convention
    assert ratio > 2
    field = hm.height_from_normal_map(rgba, "auto", detail_fraction=0)
    assert field.convention == convention
    assert corr(field.height, h) > 0.98
    # magnitude is physical: same scale as the source heights (in texels)
    assert np.std(field.height) == pytest.approx(np.std(h), rel=0.15)


@pytest.mark.parametrize("convention", ["directx", "opengl"])
def test_convention_through_texel_noise(convention):
    # gentle relief under texel-sized noise, as block compression leaves it: the raw curl can't tell
    rgba = encode_normals(bumps() * 0.15, convention).astype(np.int16)
    rgba[..., :2] += np.random.default_rng(1).integers(-6, 7, rgba[..., :2].shape)
    rgba = np.clip(rgba, 0, 255).astype(np.uint8)
    assert hm.detect_convention(rgba, sigma=0)[1] < 1.3
    detected, ratio = hm.detect_convention(rgba)
    assert detected == convention and ratio > 2


def test_wrong_convention_is_worse():
    h = bumps()
    rgba = encode_normals(h, "directx")
    good = hm.height_from_normal_map(rgba, "directx", detail_fraction=0)
    bad = hm.height_from_normal_map(rgba, "opengl", detail_fraction=0)
    assert corr(good.height, h) > corr(bad.height, h) + 0.2


def test_masked_integration_ignores_background():
    h = bumps()
    mask = np.zeros_like(h, bool)
    mask[10:70, 10:100] = True
    rgba = encode_normals(np.where(mask, h, 0))
    field = hm.height_from_normal_map(rgba, "directx", mask=mask, detail_fraction=0)
    assert (field.height[~mask] == 0).all()
    inner = mask.copy()
    inner[:13, :] = inner[67:, :] = False
    inner[:, :13] = inner[:, 97:] = False
    assert corr(field.height[inner], h[inner]) > 0.95


def test_highpass_removes_ramp():
    y, x = np.mgrid[0:64, 0:64].astype(float)
    detail = np.sin(x) * np.sin(y)
    out = hm.highpass(detail + x * 0.5, 6)
    assert corr(out[16:-16, 16:-16], detail[16:-16, 16:-16]) > 0.9


def test_uv_mask_and_texel_size():
    corner_uv = np.array([[[0, 0], [0.5, 0], [0, 0.5]]], np.float32)
    mask = hm.uv_coverage_mask(corner_uv, 64, 64, dilate=0)
    assert mask[2, 2] and not mask[60, 60]
    assert 400 < mask.sum() < 600  # half of a 32x32 square
    verts = np.array([[0, 0, 0], [1.0, 0, 0], [0, 1.0, 0]])
    # 1 m maps to 32 texels
    assert hm.texel_world_size(verts, np.array([[0, 1, 2]]), corner_uv, 64, 64) == pytest.approx(1 / 32)


def test_sample_bilinear_wraps():
    img = np.arange(16, dtype=float).reshape(4, 4)
    uv = np.array([[0.125, 0.125], [1.125, 0.125], [0.375, 0.125]])
    vals = hm.sample_bilinear(img, uv)
    assert vals[0] == pytest.approx(0) and vals[1] == pytest.approx(0) and vals[2] == pytest.approx(1)


def test_grayscale_height():
    img = np.full((4, 4, 4), 255, np.uint8)
    img[0, 0, :3] = 0
    f = hm.height_from_grayscale(img)
    assert f.height[1, 1] == pytest.approx(0.5) and f.height[0, 0] == pytest.approx(-0.5)


def _ring_slopes(n=96, hr=8, sample="point"):
    """Slopes of a raised ring with steep 1.5-texel walls, sampled at texel centres (stair-stepped, as an
    under-resolved bake) or averaged over each texel (the smooth truth)."""
    y, x = (np.mgrid[0:n * hr, 0:n * hr] + 0.5) / hr
    r = np.hypot(x - n / 2, y - n / 2)
    h = np.clip((n * 0.3 - r) / 1.5, 0, 1) * 1.5
    gy, gx = np.gradient(h)
    gx, gy = gx * hr, gy * hr
    if sample == "point":
        return gx[hr // 2::hr, hr // 2::hr], gy[hr // 2::hr, hr // 2::hr]
    return (gx.reshape(n, hr, n, hr).mean(axis=(1, 3)), gy.reshape(n, hr, n, hr).mean(axis=(1, 3)))


def test_smooth_jagged_edges_takes_out_stair_steps():
    p, q = _ring_slopes()
    tp, tq = _ring_slopes(sample="area")
    sp, sq = hm.smooth_jagged_edges(p, q)
    before = np.hypot(p - tp, q - tq)
    after = np.hypot(sp - tp, sq - tq)
    edge = np.hypot(tp, tq) > 0.05
    assert np.sqrt((after[edge] ** 2).mean()) < 0.85 * np.sqrt((before[edge] ** 2).mean())
    # the wall stays as steep as it was: smoothed along the edge, not across it
    assert np.abs(sp).max() > 0.8 * np.abs(p).max()


def test_smooth_jagged_edges_leaves_grid_edges_and_grain():
    rng = np.random.default_rng(3)
    p = np.zeros((64, 64), np.float32)
    p[:, 20:24] = 1.0  # a wall running down the texel grid: no steps to take out
    p += rng.normal(0, 0.02, p.shape).astype(np.float32)
    q = rng.normal(0, 0.02, p.shape).astype(np.float32)
    sp, sq = hm.smooth_jagged_edges(p, q)
    assert np.abs(sp - p).max() < 0.02 and np.abs(sq - q).max() < 0.02
    assert hm.smooth_jagged_edges(p, q, strength=0)[0] is p
