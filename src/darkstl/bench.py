"""Displacement test bench: known relief in, error numbers out.

Each scene is a height field with a known answer. It is baked into a normal
map the way a game's would be (DirectX green, 8 bits, optionally through
simulated BC5 or BC1 compression), mapped onto a flat panel in two UV islands
(one mirrored, so there is a real UV seam down the middle), and run through
the same pipeline the app uses. The heights that come out are compared with
the truth.

Two numbers per run, both as a percentage of the truth's RMS (lower is better):

- ``error``: everything that differs, after removing a constant offset.
- ``detail``: the error left after removing shapes broader than 1/16 of the
  texture, i.e. how well the fine relief came through.

Run ``python -m darkstl.bench`` for the table.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

from .geometry.mesh import TriMesh
from .model import Material, SourceModel, Texture
from .pipeline import EnhanceSettings, enhance
from .textures import bc

N = 256  # texture size; the panel is N mm across, so one texel is 1 mm


def _grid(n=N):
    y, x = np.mgrid[0:n, 0:n] + 0.5
    return x / n, y / n


def scene_bumps(n=N) -> np.ndarray:
    x, y = _grid(n)
    return (np.exp(-((x - 0.3) ** 2 + (y - 0.4) ** 2) / 0.004) * 5
            + np.exp(-((x - 0.72) ** 2 + (y - 0.65) ** 2) / 0.002) * 3
            + np.sin(2 * np.pi * x * 9) * np.cos(2 * np.pi * y * 7) * 0.8)


def _box(x, y, x0, x1, y0, y1, bevel):
    """1 inside the rectangle, 0 outside, with a linear bevel ``bevel`` (UV units) wide."""
    fx = np.clip(np.minimum(x - x0, x1 - x) / bevel, 0, 1)
    fy = np.clip(np.minimum(y - y0, y1 - y) / bevel, 0, 1)
    return np.minimum(fx, fy)


def scene_plates(n=N) -> np.ndarray:
    """Raised armour plates with steep 1.5-texel bevels, the shape a least-squares solve rounds off."""
    x, y = _grid(n)
    b = 1.5 / n
    h = (_box(x, y, 0.08, 0.42, 0.1, 0.35, b) * 3 + _box(x, y, 0.55, 0.9, 0.08, 0.6, b) * 4
         + _box(x, y, 0.15, 0.45, 0.55, 0.9, b) * 2.5 + _box(x, y, 0.62, 0.8, 0.7, 0.88, b) * 3)
    return h


def scene_engraving(n=N) -> np.ndarray:
    """Narrow grooves (2 texels wide, 1.5 deep): lines and a ring."""
    x, y = _grid(n)
    g = np.zeros_like(x)
    for c in (0.2, 0.35, 0.5, 0.65, 0.8):
        g = np.maximum(g, np.clip(1 - np.abs(y - c) * n / 1.0, 0, 1) * (x > 0.1) * (x < 0.45))
    r = np.hypot(x - 0.7, y - 0.5)
    g = np.maximum(g, np.clip(1 - np.abs(r - 0.18) * n / 1.0, 0, 1))
    return -1.5 * g


def scene_cloth(n=N) -> np.ndarray:
    """Fine, dense weave-like relief."""
    rng = np.random.default_rng(7)
    noise = ndimage.gaussian_filter(rng.normal(size=(n, n)), 1.2, mode="wrap")
    x, y = _grid(n)
    weave = np.sin(2 * np.pi * x * 48) * np.sin(2 * np.pi * y * 48)
    return noise / noise.std() * 0.4 + weave * 0.3


SCENES = {"bumps": scene_bumps, "plates": scene_plates, "engraving": scene_engraving, "cloth": scene_cloth}


def _gradients(h: np.ndarray):
    gy, gx = np.gradient(h)
    return gx, gy


def _island_texture(values: np.ndarray, flip_sign: bool = False) -> np.ndarray:
    """Lay a panel-space image out the way the panel's UVs do (left half -> right of the texture, right half
    mirrored vertically -> left of the texture)."""
    n = values.shape[1]
    half = n // 2
    out = np.empty_like(values)
    out[:, half:] = values[:, :half]
    right = values[::-1, half:]
    out[:, :half] = -right if flip_sign else right
    return out


def normal_texture(h: np.ndarray, compression: str = "8bit", painted: float = 0.0) -> np.ndarray:
    """Bake the panel's relief into the texture's normal map (DirectX green)."""
    gx, gy = _gradients(h)
    if painted > 0:  # hand-painted, non-integrable detail: slopes no real surface has
        rng = np.random.default_rng(3)
        swirl = ndimage.gaussian_filter(rng.normal(size=h.shape), 2.0, mode="wrap")
        sx, sy = np.gradient(swirl)
        gx, gy = gx - sy * painted / sy.std(), gy + sx * painted / sx.std()
    tx = _island_texture(gx)
    ty = _island_texture(gy, flip_sign=True)  # the mirrored island's v runs the other way
    n = np.stack([-tx, -ty, np.ones_like(tx)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    rgba = np.zeros(h.shape + (4,), np.uint8)
    rgba[..., :3] = np.clip(np.rint((n * 0.5 + 0.5) * 255), 0, 255)
    rgba[..., 3] = 255
    if compression == "bc5":
        rgba = bc.bc5(rgba)
    elif compression == "bc1":
        rgba = bc.bc1(rgba)
    return rgba


def panel_mesh(k: int, size_mm: float = N) -> TriMesh:
    """A flat panel of k x k quads in two UV islands meeting at x = 0.5, the right one mirrored in v."""
    xs = np.arange(k + 1) / k
    vx, vy = np.meshgrid(xs, xs)
    verts = np.stack([vx.ravel() * size_mm / 1000.0, (1 - vy.ravel()) * size_mm / 1000.0, np.zeros(vx.size)], 1)
    idx = np.arange((k + 1) ** 2).reshape(k + 1, k + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    faces = np.concatenate([np.stack([a, c, b], 1), np.stack([b, c, d], 1)])
    px, py = vx.ravel()[faces], vy.ravel()[faces]
    left = px.mean(axis=1) < 0.5
    u = np.where(left[:, None], px + 0.5, px - 0.5)
    v = np.where(left[:, None], py, 1 - py)
    uvs = np.stack([u, v], -1).astype(np.float32)
    return TriMesh(verts, faces, uvs, np.zeros(len(faces), np.int32))


def truth_at_vertices(h: np.ndarray, mesh: TriMesh) -> np.ndarray:
    n = h.shape[0]
    x = mesh.vertices[:, 0] * 1000.0 / n
    y = 1 - mesh.vertices[:, 1] * 1000.0 / n
    return ndimage.map_coordinates(h, [y * n - 0.5, x * n - 0.5], order=3, mode="nearest") / 1000.0


def source_model(mesh: TriMesh, normal: np.ndarray, albedo: np.ndarray | None = None) -> SourceModel:
    textures = {"panel_n": Texture("panel_n", "bench", lambda: normal)}
    mat = Material("panel", normal="panel_n", normal_green="directx")
    if albedo is not None:
        textures["panel_a"] = Texture("panel_a", "bench", lambda: albedo)
        mat.albedo = "panel_a"
    return SourceModel("bench panel", Path("bench"), "bench", lambda opts: mesh, materials=[mat], textures=textures)


@dataclass
class Method:
    name: str
    settings: dict = field(default_factory=dict)


METHODS = [
    Method("v0.1: texture integration", {"integration": "texture", "smooth_sampling": False}),
    Method("mesh integration (default)", {}),
    Method("mesh + guided upsampling", {"upsample": "guided"}),
    Method("mesh + deblock smoothing", {"deblock": "smooth"}),
    Method("mesh + edge keeping", {"edge_keeping": 0.5}),
]


def _highpass(err: np.ndarray, mesh: TriMesh, n: int, sigma_texels: float) -> np.ndarray:
    """``err`` minus its blur over ``sigma_texels``, by averaging into texel bins on the panel."""
    x = np.clip((mesh.vertices[:, 0] * 1000.0).astype(np.int64), 0, n - 1)
    y = np.clip(((n / 1000.0 - mesh.vertices[:, 1]) * 1000.0).astype(np.int64), 0, n - 1)
    cell = y * n + x
    total = np.bincount(cell, weights=err, minlength=n * n)
    count = np.bincount(cell, minlength=n * n).astype(np.float64)
    blur_t = ndimage.gaussian_filter(total.reshape(n, n), sigma_texels, mode="nearest")
    blur_c = ndimage.gaussian_filter(count.reshape(n, n), sigma_texels, mode="nearest")
    low = (blur_t / np.maximum(blur_c, 1e-9)).ravel()
    return err - low[cell]


def run(scene: str, compression: str, method: Method, vertices: int = 70_000, painted: float = 0.0) -> dict:
    """One bench run. The panel starts as a coarse game-like mesh (4 texels per quad) and is subdivided
    to ``vertices`` by the pipeline, as the app does."""
    h = SCENES[scene]()
    mesh = panel_mesh(N // 4)
    normal = normal_texture(h, compression, painted)
    s = EnhanceSettings(target_vertices=vertices, smooth=0.0, prefilter=True, **method.settings)
    t0 = time.time()
    result = enhance(source_model(mesh, normal), s)
    seconds = time.time() - t0
    out = result.displaced
    got = out.metadata["displacement"]
    want = truth_at_vertices(h, out)
    err = (got - got.mean()) - (want - want.mean())
    rms = float(np.sqrt(np.mean((want - want.mean()) ** 2))) or 1.0
    detail = _highpass(err, out, N, N / 16)
    return {"scene": scene, "compression": compression, "method": method.name,
            "error": 100 * float(np.sqrt(np.mean(err ** 2))) / rms,
            "detail": 100 * float(np.sqrt(np.mean(detail ** 2))) / rms, "seconds": seconds}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenes", default=",".join(SCENES))
    ap.add_argument("--compression", default="8bit,bc5,bc1")
    ap.add_argument("--vertices", type=int, default=70_000, help="vertex count to subdivide the panel to")
    ap.add_argument("--painted", type=float, default=0.0, help="add hand-painted (non-integrable) slopes")
    args = ap.parse_args(argv)
    print(f"{'scene':10} {'texture':7} {'method':28} {'error %':>8} {'detail %':>9} {'s':>5}")
    for scene in args.scenes.split(","):
        for comp in args.compression.split(","):
            for m in METHODS:
                r = run(scene, comp, m, args.vertices, painted=args.painted)
                print(f"{scene:10} {comp:7} {m.name:28} {r['error']:8.1f} {r['detail']:9.1f} {r['seconds']:5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
