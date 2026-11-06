"""Blending multilayered materials into plain colour and normal maps.

Skin, metal and most weapon surfaces in Cyberpunk are ``MultilayerSetup``
materials: a ``.mlsetup`` lists up to twenty layers, each a ``.mltemplate``
(a small tiled colour / normal / roughness set with named colour scales) with
its tiling, offset, opacity and chosen overrides, and a ``.mlmask`` gives
each layer's coverage over the model's UV space. The game blends them on the
GPU; here they are blended once into one texture at a resolution that keeps
the tiled detail (at least 2048, at most 4096 texels a side):

    colour = layer 0, then for each layer i: mix(colour, tile_i(uv) * scale_i, mask_i(uv) * opacity_i)

and the normal map likewise, each layer's tangent-space tilt scaled by its
normal strength, added to the material's ``GlobalNormal``. Microblends (a
fine noise mixed into masks) are left out. The blend follows the Cyberpunk
Blender add-on's multilayered material (GPL, reference only).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .depot import Store
from .formats import archive as arc
from .formats.cr2w import Red, Ref
from .formats.mlmask import MlMask, read_mlmask

MIN_SIZE = 2048
MAX_SIZE = 4096
BAND = 256


@dataclass
class Layer:
    template: str  # depot path of the .mltemplate
    color_texture: str | None
    normal_texture: str | None
    tile: float = 1.0  # UV repeats across the model
    offset: tuple[float, float] = (0.0, 0.0)
    opacity: float = 1.0
    color_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    normal_strength: float = 1.0
    index: int = 0


@dataclass
class Setup:
    path: str
    layers: list[Layer] = field(default_factory=list)
    mask: str | None = None
    missing: list[str] = field(default_factory=list)


def _override(template: Red, kind: str, name: str | None, default):
    """``template.overrides.<kind>`` entry named ``name`` (else the default selection's), as its value."""
    over = template.get("overrides")
    table = (over.get(kind) or []) if isinstance(over, Red) else []
    if not name or name in ("None", "null", "null_null"):
        sel = template.get("defaultOverrides")
        name = str(sel.get(kind, "") or "") if isinstance(sel, Red) else ""
    for entry in table:
        if isinstance(entry, Red) and str(entry.get("n", "")) == name:
            v = entry.get("v")
            if isinstance(v, (list, tuple)):
                return tuple(float(x) for x in v)
            if isinstance(v, (int, float)):
                return float(v)
    return default


def _path(v) -> str | None:
    if isinstance(v, Ref) and v.path:
        p = arc.normalize_path(v.path)
        return p if p.endswith(".xbm") else None
    return None


def read_setup(store: Store, setup_path: str, mask_path: str | None) -> Setup:
    """The layers of a ``.mlsetup`` with their templates read."""
    out = Setup(arc.normalize_path(setup_path), mask=arc.normalize_path(mask_path) if mask_path else None)
    f = store.read(out.path)
    if f is None or not f.chunks:
        out.missing.append(out.path)
        return out
    for i, lay in enumerate(f.root.get("layers") or []):
        if not isinstance(lay, Red):
            continue
        mat = lay.get("material")
        tpath = arc.normalize_path(mat.path) if isinstance(mat, Ref) and mat.path else None
        if not tpath:
            continue
        tf = store.read(tpath)
        if tf is None or not tf.chunks:
            out.missing.append(tpath)
            continue
        t = tf.root
        tiling = float(t.get("tilingMultiplier", 1.0) or 1.0)
        layer = Layer(tpath, _path(t.get("colorTexture")), _path(t.get("normalTexture")),
                      float(lay.get("matTile", 1.0) or 1.0) * tiling,
                      (float(lay.get("offsetU", 0.0) or 0.0), float(lay.get("offsetV", 0.0) or 0.0)),
                      float(lay.get("opacity", 1.0) if lay.get("opacity") is not None else 1.0), index=i)
        scale = _override(t, "colorScale", str(lay.get("colorScale", "") or ""), (1.0, 1.0, 1.0))
        if isinstance(scale, tuple) and len(scale) >= 3:
            layer.color_scale = (scale[0], scale[1], scale[2])
        strength = _override(t, "normalStrength", str(lay.get("normalStrength", "") or ""), 1.0)
        if isinstance(strength, (int, float)):
            layer.normal_strength = float(strength)
        out.layers.append(layer)
    return out


# ---------------------------------------------------------------- blending

_SRGB_TO_LINEAR = ((np.arange(256) / 255.0) ** 2.2).astype(np.float32)


def _linear_to_srgb(v: np.ndarray) -> np.ndarray:
    return (np.clip(v, 0.0, 1.0) ** (1 / 2.2) * 255.0 + 0.5).astype(np.uint8)


def output_size(mask_w: int, mask_h: int) -> tuple[int, int]:
    big = max(mask_w, mask_h, 1)
    k = max(1, -(-MIN_SIZE // big))
    while big * k > MAX_SIZE and k > 1:
        k -= 1
    return min(mask_w * k, MAX_SIZE), min(mask_h * k, MAX_SIZE)


def _reduce(tex: np.ndarray, k: int) -> np.ndarray:
    """``tex`` box-filtered ``k`` times smaller along each side (k texels of the tile per output texel)."""
    th, tw = tex.shape[:2]
    h, w = max(th // k, 1), max(tw // k, 1)
    t = tex[:h * k, :w * k].astype(np.float32).reshape(h, k, w, k, -1).mean(axis=(1, 3))
    return t.astype(tex.dtype) if tex.dtype != np.uint8 else (t + 0.5).astype(np.uint8)


def _sample(tex: np.ndarray, rows: np.ndarray, out_w: int, out_h: int, tile: float, offset: tuple[float, float]) \
        -> np.ndarray:
    """A tiled texture at the texels of ``rows`` (nearest texel of the texture reduced to about the output's
    texel size, so a finely tiled texture averages rather than sparkles), as (len(rows), out_w, channels)."""
    k = int(tex.shape[1] * max(tile, 1e-6) // out_w)
    if k > 1:
        tex = _reduce(tex, k)
    th, tw = tex.shape[:2]
    u = ((np.arange(out_w) + 0.5) / out_w * tile + offset[0]) * tw
    v = ((rows + 0.5) / out_h * tile + offset[1]) * th
    ix = np.floor(u).astype(np.int64) % tw
    iy = np.floor(v).astype(np.int64) % th
    return tex[iy[:, None], ix[None, :]]


def _upsample(mask: np.ndarray, rows: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """A mask at the texels of ``rows`` (bilinear), as float32 0..1."""
    mh, mw = mask.shape
    u = (np.arange(out_w) + 0.5) / out_w * mw - 0.5
    v = (rows + 0.5) / out_h * mh - 0.5
    x0 = np.floor(u).astype(np.int64)
    y0 = np.floor(v).astype(np.int64)
    fx = (u - x0).astype(np.float32)
    fy = (v - y0).astype(np.float32)
    x1 = np.clip(x0 + 1, 0, mw - 1)
    y1 = np.clip(y0 + 1, 0, mh - 1)
    x0 = np.clip(x0, 0, mw - 1)
    y0 = np.clip(y0, 0, mh - 1)
    m = mask.astype(np.float32) / 255.0
    top = m[y0[:, None], x0[None, :]] * (1 - fx)[None, :] + m[y0[:, None], x1[None, :]] * fx[None, :]
    bottom = m[y1[:, None], x0[None, :]] * (1 - fx)[None, :] + m[y1[:, None], x1[None, :]] * fx[None, :]
    return top * (1 - fy)[:, None] + bottom * fy[:, None]


class Blend:
    """One material's layers blended into colour and normal maps, read lazily and kept."""

    def __init__(self, store: Store, setup: Setup, global_normal: str | None = None,
                 global_scale: tuple[float, float] = (1.0, 1.0)):
        self.store = store
        self.setup = setup
        self.global_normal = global_normal
        self.global_scale = global_scale
        self._mask: MlMask | None = None
        self._textures: dict[str, np.ndarray | None] = {}
        self._masks: dict[int, np.ndarray | None] = {}
        self.size: tuple[int, int] | None = None
        self.notes: list[str] = []

    # -- inputs ---------------------------------------------------------------

    def mask(self) -> MlMask | None:
        if self._mask is None and self.setup.mask:
            f = self.store.read(self.setup.mask)
            if f is not None:
                try:
                    self._mask = read_mlmask(f)
                except Exception as exc:  # noqa: BLE001 - blended without masks, with a note
                    self.notes.append(f"{self.setup.mask} could not be read ({exc}); layers are blended by opacity")
            else:
                self.notes.append(f"{self.setup.mask} was not found; the layers are blended by their opacity alone")
        return self._mask

    def texture(self, depot: str | None) -> np.ndarray | None:
        if not depot:
            return None
        if depot not in self._textures:
            ref = self.store.texture_ref(depot)
            img = None
            if ref is not None:
                try:
                    img = ref.decode()
                except Exception:  # noqa: BLE001
                    img = None
            if img is None:
                self.notes.append(f"{depot} was not found")
            self._textures[depot] = img
        return self._textures[depot]

    def layer_mask(self, index: int) -> np.ndarray | None:
        if index not in self._masks:
            m = self.mask()
            self._masks[index] = m.layer(index) if m is not None and index < m.layers else None
        return self._masks[index]

    def dimensions(self) -> tuple[int, int]:
        if self.size is None:
            m = self.mask()
            self.size = output_size(m.width, m.height) if m is not None else (MIN_SIZE, MIN_SIZE)
        return self.size

    # -- outputs --------------------------------------------------------------

    def albedo(self) -> np.ndarray:
        """(H, W, 4) uint8 sRGB."""
        w, h = self.dimensions()
        out = np.zeros((h, w, 4), np.uint8)
        out[:, :, 3] = 255
        layers = [(lay, self.texture(lay.color_texture)) for lay in self.setup.layers]
        layers = [(lay, tex) for lay, tex in layers if tex is not None]
        if not layers:
            out[:, :, :3] = 128
            return out
        for start in range(0, h, BAND):
            rows = np.arange(start, min(start + BAND, h))
            acc = None
            for lay, tex in layers:
                rgba = _sample(tex, rows, w, h, lay.tile, lay.offset)
                col = _SRGB_TO_LINEAR[rgba[:, :, :3]] if rgba.dtype == np.uint8 else np.clip(rgba[:, :, :3], 0, 1)
                scale = np.asarray(lay.color_scale, np.float32)
                if rgba.shape[2] > 3 and not np.allclose(scale, 1.0):
                    a = (rgba[:, :, 3:4] / 255.0 if rgba.dtype == np.uint8 else rgba[:, :, 3:4]).astype(np.float32)
                    col = col * (1.0 + (scale - 1.0) * a)
                else:
                    col = col * scale
                if acc is None:
                    acc = col
                    continue
                mask = self.layer_mask(lay.index)
                weight = (_upsample(mask, rows, w, h) if mask is not None else 1.0) * lay.opacity
                weight = np.asarray(weight, np.float32)
                if weight.ndim == 2:
                    weight = weight[:, :, None]
                acc = acc * (1 - weight) + col * weight
            out[rows[0]:rows[-1] + 1, :, :3] = _linear_to_srgb(acc)
        return out

    def normal(self) -> np.ndarray:
        """(H, W, 4) uint8: the layers' tangent-space tilts summed with the global normal map's."""
        w, h = self.dimensions()
        out = np.zeros((h, w, 4), np.uint8)
        out[:, :, 3] = 255
        layers = [(lay, self.texture(lay.normal_texture)) for lay in self.setup.layers]
        layers = [(lay, tex) for lay, tex in layers if tex is not None]
        glob = self.texture(self.global_normal)
        for start in range(0, h, BAND):
            rows = np.arange(start, min(start + BAND, h))
            acc = np.zeros((len(rows), w, 2), np.float32)
            blended = None
            for lay, tex in layers:
                xy = _tilt(_sample(tex, rows, w, h, lay.tile, lay.offset)) * lay.normal_strength
                if blended is None:
                    blended = xy
                    continue
                mask = self.layer_mask(lay.index)
                weight = (_upsample(mask, rows, w, h) if mask is not None else 1.0) * lay.opacity
                weight = np.asarray(weight, np.float32)
                if weight.ndim == 2:
                    weight = weight[:, :, None]
                blended = blended * (1 - weight) + xy * weight
            if blended is not None:
                acc += blended
            if glob is not None:
                acc += _tilt(_sample(glob, rows, w, h, 1.0, (0.0, 0.0)) if self.global_scale == (1.0, 1.0)
                             else _sample(glob, rows, w, h, self.global_scale[0], (0.0, 0.0)))
            z = np.sqrt(np.clip(1.0 - np.sum(acc * acc, axis=2, keepdims=True), 0.0, 1.0))
            n = np.concatenate([acc, z], axis=2)
            n /= np.maximum(np.linalg.norm(n, axis=2, keepdims=True), 1e-6)
            out[rows[0]:rows[-1] + 1, :, :3] = np.clip((n * 0.5 + 0.5) * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return out

    def describe(self, what: str) -> str:
        w, h = self.dimensions()
        return f"{w} x {h}, blended, {len(self.setup.layers)} layers ({what})"


@dataclass(eq=False)
class BlendRef:
    """A blended map, as :mod:`.materials` hands it to the model: decoded when first shown."""

    name: str
    source: str
    _decode: Callable[[], np.ndarray] = field(repr=False)
    _describe: Callable[[], str] = field(repr=False)

    def decode(self) -> np.ndarray:
        return self._decode()

    def describe(self) -> str:
        return self._describe()


def _tilt(rgba: np.ndarray) -> np.ndarray:
    """A normal map's red and green as -1..1 tilts."""
    if rgba.dtype == np.uint8:
        return rgba[:, :, :2].astype(np.float32) / 127.5 - 1.0
    return np.clip(rgba[:, :, :2], 0, 1).astype(np.float32) * 2.0 - 1.0


__all__ = ["Blend", "BlendRef", "Layer", "Setup", "output_size", "read_setup"]
