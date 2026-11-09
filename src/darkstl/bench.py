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

Thin scenes (``blade``, ``claw``, ``cape``) are closed parts and sheets thinner
than their relief, the shapes displacement used to push through themselves: a
blade tapering to its edges, three curled claws side by side, and a cape hanging
just behind a body with its hem folded up. For each, the number of triangle
pairs that pass through each other is counted with thin parts kept and without,
with the share of the relief kept.

Run ``python -m darkstl.bench`` for the tables.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

from .geometry.intersect import count_crossings
from .geometry.mesh import TriMesh, concatenate, weld
from .model import Material, SourceModel, Texture
from .pipeline import EnhanceSettings, enhance, resample
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
    Method("v0.1: texture integration", {"integration": "texture", "smooth_sampling": False, "edge_smoothing": 0.0}),
    Method("mesh integration (default)", {}),
    Method("mesh, jagged edges left", {"edge_smoothing": 0.0}),
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


# ---------------------------------------------------------------- thin parts


def surface(fn, nu: int, nv: int, flip: bool = False) -> TriMesh:
    """A grid of ``nu`` x ``nv`` quads over ``fn(u, v) -> (..., 3)``, (u, v) in [0, 1], with UVs (u, v); welded,
    so closed loops and collapsed tips join up."""
    u, v = np.meshgrid(np.linspace(0, 1, nu + 1), np.linspace(0, 1, nv + 1), indexing="ij")
    pts = fn(u, v).reshape(-1, 3)
    idx = np.arange((nu + 1) * (nv + 1)).reshape(nu + 1, nv + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel()
    faces = np.concatenate([np.stack([a, b, d], 1), np.stack([a, d, c], 1)])
    if flip:
        faces = faces[:, ::-1]
    uvs = np.stack([u.ravel(), v.ravel()], 1)[faces].astype(np.float32)
    return weld(TriMesh(pts, faces, uvs))


def box_mesh(lo, hi, step: float) -> TriMesh:
    """A closed box from ``lo`` to ``hi`` (metres), each side a grid about ``step`` apart with UVs over the whole
    texture, normals out."""
    lo, hi = np.asarray(lo, np.float64), np.asarray(hi, np.float64)
    size = hi - lo
    sides = []
    for axis in range(3):
        a, b = (axis + 1) % 3, (axis + 2) % 3
        for end, flip in ((0, True), (1, False)):
            na, nb = (max(1, int(round(size[x] / step))) for x in (a, b))

            def fn(u, v, axis=axis, a=a, b=b, end=end):
                p = np.empty(u.shape + (3,))
                p[..., axis] = lo[axis] + end * size[axis]
                p[..., a] = lo[a] + u * size[a]
                p[..., b] = lo[b] + v * size[b]
                return p
            sides.append(surface(fn, na, nb, flip))
    return weld(concatenate(sides))


def bake_normal_map(h: np.ndarray) -> np.ndarray:
    """A height field in texels (rows down the texture's v, columns along u) as a DirectX normal map."""
    gy, gx = np.gradient(h)
    n = np.stack([-gx, -gy, np.ones_like(gx)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    rgba = np.zeros(h.shape + (4,), np.uint8)
    rgba[..., :3] = np.clip(np.rint((n * 0.5 + 0.5) * 255), 0, 255)
    rgba[..., 3] = 255
    return rgba


def relief_texture(rows: int, cols: int, amplitude: float, seed: int = 1) -> np.ndarray:
    """Studs, grooves and fine ripples, about ``amplitude`` texels high either way."""
    y, x = np.mgrid[0:rows, 0:cols] + 0.5
    rng = np.random.default_rng(seed)
    h = np.sin(2 * np.pi * x / 12.0) * np.sin(2 * np.pi * y / 10.0) * 0.6
    for _ in range(int(rows * cols / 200)):
        cy, cx, r = rng.uniform(0, rows), rng.uniform(0, cols), rng.uniform(1.5, 3.5)
        h += np.sign(rng.uniform(-1, 1)) * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (r * r))
    return amplitude * h / np.abs(h).max()


def _model(name: str, parts: list[tuple[TriMesh, np.ndarray]]) -> SourceModel:
    """Parts, each with its own normal map, as one model."""
    meshes, mats, textures = [], [], {}
    for i, (mesh, normal) in enumerate(parts):
        mesh = mesh.copy()
        mesh.face_material = np.full(mesh.n_faces, i, np.int32)
        meshes.append(mesh)
        key = f"{name}_{i}_n"
        textures[key] = Texture(key, "bench", lambda normal=normal: normal)
        mats.append(Material(f"{name} {i}", normal=key, normal_green="directx"))
    whole = concatenate(meshes)
    return SourceModel(name, Path("bench"), "bench", lambda opts: whole, materials=mats, textures=textures)


def scene_blade() -> SourceModel:
    """A blade 200 mm long, 30 mm wide and 4 mm thick at its middle, tapering to sharp edges and a point;
    relief up to about 2.5 mm, more than half its thickness everywhere."""
    length, half_w, half_t = 0.2, 0.015, 0.002

    def fn(u, v):
        taper = np.clip(3.0 * (1.0 - u), 0.0, 1.0)
        a = 2 * np.pi * v
        return np.stack([u * length, half_w * taper * np.cos(a), half_t * taper * np.sin(a)], -1)

    mesh = surface(fn, 120, 48, flip=True)
    return _model("blade", [(mesh, bake_normal_map(relief_texture(64, 256, 3.0)))])


def scene_claws() -> SourceModel:
    """Three claws 60 mm long, 16 mm across at the base, curling a quarter turn to a point, 2 mm apart at their
    bases; relief up to about 2.5 mm, more than the tips are thick and the gaps are wide."""
    curl = 0.06 / (np.pi / 2)
    parts = []
    for i in range(3):
        def fn(u, v, off=i * 0.018):
            phi = u * np.pi / 2
            spine = np.stack([curl * np.sin(phi), np.zeros_like(phi) + off, curl * (1 - np.cos(phi))], -1)
            n1 = np.stack([-np.sin(phi), np.zeros_like(phi), np.cos(phi)], -1)
            n2 = np.stack([np.zeros_like(phi), np.ones_like(phi), np.zeros_like(phi)], -1)
            rho = 0.008 * (1 - u)
            a = 2 * np.pi * v
            return spine + rho[..., None] * (np.cos(a)[..., None] * n1 + np.sin(a)[..., None] * n2)
        parts.append(surface(fn, 80, 48, flip=False))
    claws = concatenate(parts)
    return _model("claws", [(claws, bake_normal_map(relief_texture(64, 128, 3.0, seed=2)))])


def scene_cape() -> SourceModel:
    """A body (a box 120 x 40 x 200 mm) with a cape hanging 3 mm behind its back, the cape's hem folded up
    behind it 4 mm out; both with relief up to about 2.5 mm."""
    body = box_mesh((0.0, 0.0, 0.0), (0.12, 0.04, 0.2), 0.004)
    gap, fold, top, bottom, hem = 0.003, 0.002, 0.195, 0.02, 0.06

    def fn(u, v):
        # down the back at y = 0.043, round the hem (radius 2 mm) and up again at y = 0.047; u across
        straight, turn, back = top - bottom, np.pi * fold, hem - bottom
        s = v * (straight + turn + back)
        x = 0.005 + u * 0.11
        y = np.where(s < straight, 0.04 + gap, 0.0)
        z = np.where(s < straight, top - s, 0.0)
        t = np.clip((s - straight) / fold, 0, np.pi)
        y = np.where((s >= straight) & (s < straight + turn), 0.04 + gap + fold - fold * np.cos(t), y)
        z = np.where((s >= straight) & (s < straight + turn), bottom - fold * np.sin(t), z)
        y = np.where(s >= straight + turn, 0.04 + gap + 2 * fold, y)
        z = np.where(s >= straight + turn, bottom + (s - straight - turn), z)
        return np.stack([x, y, z], -1)

    cape = surface(fn, 60, 120)  # its outside facing away from the body
    return _model("cape", [(body, bake_normal_map(relief_texture(128, 128, 3.0, seed=3))),
                           (cape, bake_normal_map(relief_texture(128, 128, 3.0, seed=4)))])


THIN_SCENES = {"blade": scene_blade, "claw": scene_claws, "cape": scene_cape}


def run_thin(scene: str, vertices: int = 70_000, room: float = 0.8, strength: float = 1.0) -> dict:
    """One thin scene displaced without keeping thin parts and with: crossing triangle pairs before
    displacement, without, after the cap alone (before crossings are damped) and with, the share of the
    relief kept, vertices capped, the seconds keeping thin parts took and the whole displacement took."""
    model = THIN_SCENES[scene]()
    base = EnhanceSettings(target_vertices=vertices, smooth=0.0, strength=strength)
    mesh = resample(model, base)
    off = enhance(model, EnhanceSettings(**{**base.__dict__, "thin_parts": False}), mesh=mesh)
    t0 = time.time()
    on = enhance(model, EnhanceSettings(**{**base.__dict__, "thin_room": room}), mesh=mesh)
    on_seconds = time.time() - t0
    h_off, h_on = off.displaced.metadata["displacement"], on.displaced.metadata["displacement"]
    return {"scene": scene, "vertices": mesh.resampled.n_vertices, "before": count_crossings(mesh.resampled),
            "without": count_crossings(off.displaced), "capped_only": on.thin["crossings_found"],
            "with": count_crossings(on.displaced),
            "relief_mm": float(np.abs(h_off).max() * 1000),
            "kept": 100 * float(np.sqrt(np.mean(h_on ** 2)) / max(np.sqrt(np.mean(h_off ** 2)), 1e-30)),
            "capped": on.thin["capped"], "seconds": on.thin["seconds"], "total_seconds": on_seconds}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenes", default=",".join([*SCENES, *THIN_SCENES]))
    ap.add_argument("--compression", default="8bit,bc5,bc1")
    ap.add_argument("--vertices", type=int, default=70_000, help="vertex count to subdivide the panel to")
    ap.add_argument("--painted", type=float, default=0.0, help="add hand-painted (non-integrable) slopes")
    ap.add_argument("--room", type=float, default=0.8, help="thin scenes: share of the room the relief may use")
    args = ap.parse_args(argv)
    scenes = args.scenes.split(",")
    relief = [s for s in scenes if s in SCENES]
    if relief:
        print(f"{'scene':10} {'texture':7} {'method':28} {'error %':>8} {'detail %':>9} {'s':>5}")
    for scene in relief:
        for comp in args.compression.split(","):
            for m in METHODS:
                r = run(scene, comp, m, args.vertices, painted=args.painted)
                print(f"{scene:10} {comp:7} {m.name:28} {r['error']:8.1f} {r['detail']:9.1f} {r['seconds']:5.1f}")
    thin = [s for s in scenes if s in THIN_SCENES]
    if thin:
        if relief:
            print()
        print(f"{'scene':8} {'vertices':>9} {'relief':>7}  {'crossing triangle pairs':^35}  {'relief':>6} {'capped':>7} "
              f"{'thin':>5} {'all':>5}")
        print(f"{'':8} {'':>9} {'mm':>7}  {'before':>7} {'without':>8} {'capped':>8} {'with':>8}  {'kept %':>6} {'':>7} "
              f"{'s':>5} {'s':>5}")
        for scene in thin:
            r = run_thin(scene, args.vertices, args.room)
            print(f"{scene:8} {r['vertices']:9,} {r['relief_mm']:7.2f}  {r['before']:7,} {r['without']:8,} "
                  f"{r['capped_only']:8,} {r['with']:8,}  {r['kept']:6.1f} {r['capped']:7,} {r['seconds']:5.1f} "
                  f"{r['total_seconds']:5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
