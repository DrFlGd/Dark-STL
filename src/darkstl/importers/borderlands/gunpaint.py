"""Paint a Borderlands 2 / Pre-Sequel gun the way its skin's shader does, baked into a colour texture.

The game has no colour texture for a gun: ``Master_Gun`` mixes it at run time from the skin's
material instance chain. The reading of that shader below follows BL2_ReBased's
``tools/weapon_paint_model.py`` and ``tools/material_static_parameters.py`` (MIT licence,
zuhuHix and the OpenWillow contributors), which recovered it from the game's compiled shader
cache. Per texel, with UV0 and UV1 the mesh's two texture coordinate sets:

- ``p_Masks`` holds two maps stacked vertically: its lower half (``v0 * 0.5 + 0.5``) is the
  zone mask (red, green, blue = zones A, B, C), its upper half (``v0 * 0.5``) a light/dark map
  whose red times ``p_HighlightsIntensity`` and green times ``p_ShadowsIntensity`` (each
  clamped) give a highlight and a shadow amount.
- Each zone's colour goes from its Midtone towards Hilight by the highlight amount, then
  towards Shadow by the shadow amount; starting from ``p_DColor`` the zones are blended in, A by
  the mask's red, B by green, C by blue.
- The pattern (``p_Pattern`` at ``UV1 * xy + zw`` of ``p_PatternScalePosition``, its channels
  picked by the static mask ``p_PatternChannel``, times ``p_PatternColor``) multiplies the colour
  towards it by the squared length of mask x ``p_PatternChannelScale`` (or replaces it, with
  ``p_ReplacePattern``). The decal (``p_Decal`` on UV1 shifted, rotated by ``p_DecalRotate`` x pi
  and scaled about the centre by ``p_DecalScalePosition``) does the same with ``p_DecalColor``
  and ``p_DecalChannel``.
- The result is multiplied by the ``p_Diffuse`` detail atlas channel that ``p_WeapClassSelect``
  picks (UV0).
- Vector parameters are linear; the detail, pattern and decal textures are sRGB, the masks
  linear. The lit passes scale the colour by 0.4, which the baked base colour includes.

Left out: the environment reflection (it depends on the view), emissive and lighting. The
static channel picks are read from the material instance's static parameter set when it can
be read exactly; otherwise the detail atlas isn't used and the pattern and decal use their red
channel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...model import PaintRecipe, PaintUVs, SourceModel, Texture
from . import files, materials
from .files import resolve
from .formats import upk
from .formats.props import ArrayValue, ObjectRef, StructValue, object_props
from .formats.texture import Texture2D

log = logging.getLogger(__name__)

BAKE_VERSION = 1
RECIPE = "borderlands-gun-paint"  # PaintRecipe.kind
DIFFUSE_SCALE = 0.4
MAX_SIZE = 2048
ZONES = "ABC"
TONES = ("Shadow", "Midtone", "Hilight")


@dataclass
class GunPaint:
    """What a skin's material instance chain says about its paint."""

    name: str
    vectors: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    scalars: dict[str, float] = field(default_factory=dict)
    textures: dict[str, Texture2D] = field(default_factory=dict)  # parameter name (lower case) -> texture
    masks: dict[str, str] = field(default_factory=dict)  # static component mask (lower case) -> channels "RGB"
    details: list[str] = field(default_factory=list)

    def vec(self, name: str, default=(0.0, 0.0, 0.0, 0.0)) -> np.ndarray:
        return np.asarray(self.vectors.get(name.lower(), default)[:4], np.float32)

    def sca(self, name: str, default: float = 0.0) -> float:
        return float(self.scalars.get(name.lower(), default))

    def texture(self, name: str) -> Texture2D | None:
        return self.textures.get(name.lower())


def read_paint(ref: ObjectRef) -> GunPaint | None:
    """The paint of a skin's material instance and its parents (nearest wins), or ``None`` when the chain
    has no ``p_Masks``: not a gun paint."""
    hit = resolve(ref)
    if hit is None:
        return None
    pkg, e = hit
    paint = GunPaint(e.name)
    for _ in range(12):
        if not e.class_name.startswith("MaterialInstance"):
            break
        r = pkg.export_data(e)
        props = object_props(pkg, r)
        for p in _structs(props.get("VectorParameterValues")):
            v = p.get("ParameterValue")
            if isinstance(v, StructValue):
                paint.vectors.setdefault(str(p.get("ParameterName", "")).lower(), v.linear_color())
        for p in _structs(props.get("ScalarParameterValues")):
            v = p.get("ParameterValue")
            if isinstance(v, (int, float)):
                paint.scalars.setdefault(str(p.get("ParameterName", "")).lower(), float(v))
        for p in _structs(props.get("TextureParameterValues")):
            name = str(p.get("ParameterName", "")).lower()
            t = p.get("ParameterValue")
            if name in paint.textures or not isinstance(t, ObjectRef) or not t:
                continue
            tex = materials.texture_of(t)
            if tex is not None:
                paint.textures[name] = tex
        if not paint.masks:
            try:
                paint.masks = static_masks(pkg, r)
            except FormatError:
                pass
        parent = props.get("Parent")
        if not isinstance(parent, ObjectRef) or not parent:
            break
        nxt = resolve(parent)
        if nxt is None:
            paint.details.append(f"parent {parent.path} not found")
            break
        pkg, e = nxt
    if paint.texture("p_Masks") is None:
        return None
    paint.details.append("paint: " + ", ".join(sorted(paint.textures)) +
                         (f"; channels {paint.masks}" if paint.masks else "; static channels not read"))
    return paint


def _structs(value) -> list:
    if not isinstance(value, ArrayValue):
        return []
    try:
        return value.structs()
    except FormatError:
        return []


def static_masks(pkg: upk.Package, r: upk.Reader) -> dict[str, str]:
    """The static component masks of a material instance with its own static permutation: the bytes after
    its properties are its compiled resource, then its static parameter set. Accepted only when they're
    read to the last byte (the layout BL2_ReBased recovered for package version 832)."""
    if r.remaining() <= 0:
        return {}
    if r.count(1 << 12) or r.count(1 << 12):  # compile errors, texture dependencies: not seen
        raise FormatError("unexpected material resource")
    r.i32()
    r.skip(16)
    r.i32()
    r.skip(4 * r.count(1 << 12))
    r.skip(24)
    r.skip(16 * r.count(1 << 12))
    r.i32()
    r.skip(16)  # base material id
    for _ in range(r.count(1 << 12)):  # static switches
        r.name()
        r.skip(8 + 16)
    out = {}
    for _ in range(r.count(1 << 12)):
        name = r.name().lower()
        channels = "".join(c for c in "RGBA" if r.i32())
        r.skip(4 + 16)
        out[name] = channels
    for _ in range(r.count(1 << 12)):  # normal parameters
        r.name()
        r.skip(1 + 4 + 16)
    for _ in range(r.count(1 << 12)):  # terrain layer weights
        r.name()
        r.skip(4 + 16 + 4)
    if r.remaining() != 0:
        raise FormatError(f"{r.remaining()} bytes left after the static parameters")
    return out


# -- baking ----------------------------------------------------------------------------------------------

def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def _sample(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear, wrapping; ``img`` (H, W, C) float; u, v any shape -> (..., C)."""
    h, w = img.shape[:2]
    x = np.mod(u, 1.0) * w - 0.5
    y = np.mod(v, 1.0) * h - 0.5
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    x0 %= w
    y0 %= h
    x1 = (x0 + 1) % w
    y1 = (y0 + 1) % h
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bottom = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def _pick(texel: np.ndarray, channels: str) -> np.ndarray:
    """A static component mask: one channel broadcasts, several keep their places."""
    picked = [c for c in channels if c in "RGB"]
    if len(picked) == 1:
        return np.repeat(texel[..., "RGB".index(picked[0])][..., None], 3, axis=-1)
    out = np.zeros(texel.shape[:-1] + (3,), np.float32)
    for c in picked:
        out[..., "RGB".index(c)] = texel[..., "RGB".index(c)]
    return out


def _layer(colour, tint, amount, replace: float):
    multiplied = colour * (1.0 + (tint - 1.0) * amount)
    replaced = colour + (tint - colour) * amount
    return multiplied + (replaced - multiplied) * replace


def _rasterize_uv1(size: tuple[int, int], uv0: np.ndarray, uv1: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """UV1 at each texel of a (H, W) texture laid out by UV0, from triangles (n, 3, 2) of both; and which
    texels a triangle covers."""
    h, w = size
    out = np.zeros((h, w, 2), np.float32)
    covered = np.zeros((h, w), bool)
    p = uv0.astype(np.float64) * [w, h]
    for t in range(len(p)):
        a, b, c = p[t]
        lo = np.floor(np.minimum(np.minimum(a, b), c)).astype(int)
        hi = np.ceil(np.maximum(np.maximum(a, b), c)).astype(int)
        x0, y0 = max(lo[0], 0), max(lo[1], 0)
        x1, y1 = min(hi[0], w), min(hi[1], h)
        if x0 >= x1 or y0 >= y1:
            continue
        den = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(den) < 1e-12:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        l1 = ((b[1] - c[1]) * (xs - c[0]) + (c[0] - b[0]) * (ys - c[1])) / den
        l2 = ((c[1] - a[1]) * (xs - c[0]) + (a[0] - c[0]) * (ys - c[1])) / den
        l3 = 1 - l1 - l2
        inside = (l1 >= -1e-4) & (l2 >= -1e-4) & (l3 >= -1e-4)
        if not inside.any():
            continue
        q = uv1[t].astype(np.float64)
        vals = l1[..., None] * q[0] + l2[..., None] * q[1] + l3[..., None] * q[2]
        region = out[y0:y1, x0:x1]
        region[inside] = vals[inside]
        covered[y0:y1, x0:x1] |= inside
    return out, covered


def _decode(tex: Texture2D | None, dirs, linear: bool) -> np.ndarray | None:
    if tex is None:
        return None
    try:
        img = np.asarray(tex.decode(dirs), np.float32)
    except FormatError:
        return None
    if img.dtype != np.float32 or img.max() > 1.5:
        img = img / 255.0
    if img.ndim == 2:
        img = np.repeat(img[..., None], 4, axis=-1)
    if img.shape[-1] == 3:
        img = np.concatenate([img, np.ones(img.shape[:-1] + (1,), np.float32)], -1)
    if not linear:
        img = img.copy()
        img[..., :3] = _srgb_to_linear(img[..., :3])
    return img


def bake(paint: GunPaint, uv0_tris: np.ndarray | None, uv1_tris: np.ndarray | None, dirs=()) -> np.ndarray:
    """The skin's colour as an (H, W, 4) uint8 sRGB texture laid out by UV0. Without UV1 triangles the
    pattern and decal are left out."""
    masks = _decode(paint.texture("p_Masks"), dirs, linear=True)
    if masks is None:
        raise FormatError(f"{paint.name}: its masks texture couldn't be read")
    detail = _decode(paint.texture("p_Diffuse"), dirs, linear=False)
    w = max(masks.shape[1], detail.shape[1] if detail is not None else 0)
    h = max(masks.shape[0] // 2, detail.shape[0] if detail is not None else 0)
    if max(w, h) > MAX_SIZE:
        s = MAX_SIZE / max(w, h)
        w, h = max(1, int(w * s)), max(1, int(h * s))
    v, u = np.meshgrid((np.arange(h) + 0.5) / h, (np.arange(w) + 0.5) / w, indexing="ij")
    light = _sample(masks, u, v * 0.5)
    mask = _sample(masks, u, v * 0.5 + 0.5)[..., :3]
    high = np.clip(light[..., 0] * paint.sca("p_HighlightsIntensity", 1.0), 0, 1)[..., None]
    low = np.clip(light[..., 1] * paint.sca("p_ShadowsIntensity", 1.0), 0, 1)[..., None]
    colour = np.broadcast_to(paint.vec("p_DColor", (1.0, 1.0, 1.0, 1.0))[:3], (h, w, 3)).astype(np.float32)
    for i, zone in enumerate(ZONES):
        names = [f"p_{zone}Color{t}".lower() for t in TONES]
        if not any(n in paint.vectors for n in names):
            continue
        shadow, mid, hil = (paint.vec(n)[:3] for n in names)
        tone = mid + (hil - mid) * high
        tone = tone + (shadow - tone) * low
        colour = colour + (tone - colour) * mask[..., i:i + 1]
    uv1 = None
    if uv0_tris is not None and uv1_tris is not None and len(uv0_tris):
        uv1, covered = _rasterize_uv1((h, w), uv0_tris, uv1_tris)
    pattern = _decode(paint.texture("p_Pattern"), dirs, linear=False)
    if uv1 is not None and pattern is not None and "p_patterncolor" in paint.vectors:
        sp = paint.vec("p_PatternScalePosition", (1, 1, 0, 0))
        texel = _sample(pattern, uv1[..., 0] * sp[0] + sp[2], uv1[..., 1] * sp[1] + sp[3])
        tint = _pick(texel, paint.masks.get("p_patternchannel", "R")) * paint.vec("p_PatternColor")[:3]
        weight = np.clip(((mask * paint.vec("p_PatternChannelScale")[:3]) ** 2).sum(-1), 0, 1)[..., None]
        weight = weight * covered[..., None]
        colour = _layer(colour, tint, weight, paint.sca("p_ReplacePattern"))
    decal = _decode(paint.texture("p_Decal"), dirs, linear=False)
    if uv1 is not None and decal is not None and "p_decalcolor" in paint.vectors:
        sp = paint.vec("p_DecalScalePosition", (1, 1, 0, 0))
        angle = paint.sca("p_DecalRotate") * np.pi
        s, c = np.sin(angle), np.cos(angle)
        x, y = uv1[..., 0] + sp[2] - 0.5, uv1[..., 1] + sp[3] - 0.5
        rx, ry = c * x - s * y + 0.5, s * x + c * y + 0.5
        du, dv = rx * sp[0] + (1 - sp[0]) * 0.5, ry * sp[1] + (1 - sp[1]) * 0.5
        texel = _sample(decal, du, dv)
        rgb = _pick(texel, paint.masks.get("p_decalchannel", "R"))
        full = paint.sca("p_UseFullColorDecal")
        tint = paint.vec("p_DecalColor")[:3]
        tint = tint + (tint * rgb - tint) * full
        weight = np.clip(((mask * paint.vec("p_DecalChannel")[:3]) ** 2).sum(-1), 0, 1)[..., None]
        amount = rgb * weight + (weight * texel[..., 3:4] - rgb * weight) * full
        colour = _layer(colour, tint, amount * covered[..., None], paint.sca("p_ReplaceDecal"))
    if detail is not None and "p_weapclassselect" in paint.masks:
        d = _sample(detail, u, v)
        colour = colour * _pick(d, paint.masks["p_weapclassselect"])[..., :1]
    rgb = _linear_to_srgb(colour * DIFFUSE_SCALE)
    out = np.empty((h, w, 4), np.uint8)
    out[..., :3] = np.round(rgb * 255)
    out[..., 3] = 255
    return out


# -- on request, for each part, kept on disk -----------------------------------------------------------------

def _stamp(path: Path) -> list:
    try:
        st = os.stat(path)
        return [str(path), st.st_size, int(st.st_mtime)]
    except OSError:
        return [str(path)]


def signature(paint: GunPaint) -> dict:
    """Everything the bake reads from the paint, so a changed skin or game file makes a new bake."""
    return {"v": BAKE_VERSION, "name": paint.name,
            "vectors": {k: [round(float(x), 6) for x in v] for k, v in sorted(paint.vectors.items())},
            "scalars": {k: round(v, 6) for k, v in sorted(paint.scalars.items())},
            "masks": dict(sorted(paint.masks.items())),
            "textures": {k: t.key if isinstance(t, _StoredTexture) else [t.path, *_stamp(t.pkg.path)]
                         for k, t in sorted(paint.textures.items())}}


def uvs_of(path: Path) -> PaintUVs:
    """A gun part's (``SourceModel.paint_uvs``): read from its package when a paint is first mixed for it."""
    path = Path(path)
    return PaintUVs([str(path), _stamp(path.parent)], lambda: mesh_uvs(path))


def recipe(paint: GunPaint, dirs=()) -> PaintRecipe:
    """The paint as a model package keeps it: its numbers, and the textures it mixes from."""
    values = {"name": paint.name, "vectors": {k: [float(x) for x in v] for k, v in paint.vectors.items()},
              "scalars": dict(paint.scalars), "masks": dict(paint.masks), "details": list(paint.details)}
    textures = {k: Texture(t.name, f"{t.pkg.path.name}: {t.path}", lambda t=t: t.decode(dirs),
                           lambda t=t: t.describe(dirs)) for k, t in paint.textures.items()}
    return PaintRecipe(RECIPE, values, textures)


@dataclass
class _StoredTexture:
    """A paint's texture kept in a model package (the bake asks it as it asks a game's Texture2D)."""

    texture: Texture
    key: list  # its file and date

    def decode(self, _dirs=None) -> np.ndarray:
        return self.texture.decode()


def from_recipe(values: dict, textures: dict[str, Texture], keys: dict[str, list]) -> GunPaint:
    """The paint a model package kept (see :func:`recipe`); ``keys`` say which file each texture is, and its date."""
    return GunPaint(str(values.get("name") or "paint"),
                    {k: tuple(float(x) for x in v) for k, v in (values.get("vectors") or {}).items()},
                    {k: float(v) for k, v in (values.get("scalars") or {}).items()},
                    {k: _StoredTexture(t, list(keys.get(k) or [k])) for k, t in textures.items()},
                    dict(values.get("masks") or {}), list(values.get("details") or []))


def mesh_uvs(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """The UV0 and UV1 triangles (n, 3, 2) of a gun part's first material, read from the part's package."""
    from .formats.skeletal import read_skeletal_mesh
    from .formats.static import read_static_mesh
    package, obj_path, cls = files.split_member(path)
    pkg = upk.open_package(package)
    e = pkg.find_export(obj_path, cls)
    if e is None:
        raise FormatError(f"{obj_path} isn't in {package.name} any more")
    if cls == "SkeletalMesh":
        m = read_skeletal_mesh(pkg, e)
        sections = [s for s in m.sections if s.material == 0]
    else:
        m = read_static_mesh(pkg, e)
        sections = m.sections[:1]
    if not m.uvs:
        raise FormatError(f"{m.name} has no texture coordinates")
    tris = m.indices.reshape(-1, 3)
    pick = np.concatenate([tris[s.first_index // 3:s.first_index // 3 + s.triangles] for s in sections]) \
        if sections else tris[:0]
    uv1 = m.uvs[1][pick] if len(m.uvs) > 1 else None
    return m.uvs[0][pick], uv1


def painter(paint: GunPaint, dirs=(), cache: Path | None = None):
    """``SourceModel.paint_for`` for a gun skin: each part's colour texture, baked from ``paint`` on its UVs
    when it's first decoded and kept in ``<cache>/gunpaint``."""
    sig = signature(paint)

    def paint_for(target: SourceModel) -> Texture | None:
        uvs = target.paint_uvs
        if uvs is None:  # not a gun part (or a package written before parts kept theirs)
            return None
        key = hashlib.sha1(json.dumps([sig, *uvs.key], sort_keys=True).encode()).hexdigest()
        where = cache / "gunpaint" / f"{key}.npy" if cache is not None else None
        made: list[np.ndarray] = []

        def load() -> np.ndarray:
            if where is not None and where.is_file():
                try:
                    made[:] = [np.load(where)]
                    return made[0]
                except (OSError, ValueError):
                    pass
            uv0, uv1 = uvs.load()
            img = bake(paint, uv0, uv1, dirs)
            if where is not None:
                try:
                    where.parent.mkdir(parents=True, exist_ok=True)
                    tmp = where.with_suffix(".tmp.npy")
                    np.save(tmp, img)
                    os.replace(tmp, where)
                except OSError as exc:
                    log.warning("couldn't keep the baked paint %s: %s", where, exc)
            made[:] = [img]
            return img

        def describe() -> str:
            if not made:
                return f"mixed from {paint.name} when shown"
            h, w = made[0].shape[:2]
            return f"{w} x {h}, mixed from {paint.name}"

        part = target.name or Path(target.path).name
        return Texture(f"{paint.name} on {part}", f"{paint.name}: the skin's paint, baked", load, describe)

    return paint_for


__all__ = ["BAKE_VERSION", "RECIPE", "GunPaint", "bake", "from_recipe", "mesh_uvs", "painter", "read_paint", "recipe",
           "signature", "static_masks", "uvs_of"]
