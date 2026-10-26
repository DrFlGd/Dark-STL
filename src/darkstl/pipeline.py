"""The enhancement pipeline: even remesh -> texture displacement -> export.

It works on :class:`~darkstl.model.SourceModel`, so it is the same for every
game; only the importer that produced the model differs.

Re-sampling (2) and displacement (3) are separate steps (:func:`resample`,
:func:`displace_resampled`), so the evened-out mesh can be kept while other
displacement settings are tried on it.

1. Build the model's welded triangle mesh (export frame, metres).
2. Re-sample the surface to the requested number of evenly spaced vertices,
   either by adaptive subdivision (exact UVs, keeps every original edge) or by
   isotropic ACVD remeshing (exact vertex count, uniform triangles).
3. Work out a height for every vertex and push it along its normal. From a
   normal map the slopes are integrated over the mesh itself (default) or over
   each texture (the older route), after optional clean-up: deblocking,
   upsampling (guided by the albedo, or an AI model), and edge hints from the
   specular/metallic mask. Fine extras can be layered on: relief guessed from
   the albedo, and the material's tiled detail normal map. A grayscale height
   map is sampled directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import ndimage

from .geometry.displace import corner_heights, displace
from .geometry.integrate import SlopeLayer, integrate_on_mesh
from .geometry.mesh import TriMesh
from .geometry.remesh import acvd_available, isotropic_remesh
from .geometry.seams import seam_consistent_heights
from .geometry.subdivide import edge_length_for_vertex_count, subdivide_to_vertex_count
from .model import Material, MeshOptions, SourceModel
from .textures import heightmap as hm
from .textures.heightmap import (
    HeightField,
    height_from_grayscale,
    height_from_normal_map,
    texel_world_size,
    uv_coverage_mask,
)

ProgressFn = Callable[[float, str], None]

METHOD_SUBDIVIDE = "subdivide"
METHOD_ISOTROPIC = "isotropic"
SOURCE_NORMAL = "normal"
SOURCE_HEIGHT = "height"
SOURCE_NONE = "none"
INTEGRATE_MESH = "mesh"
INTEGRATE_TEXTURE = "texture"
MAX_UPSCALED = 8192  # largest texture side after upsampling


@dataclass
class EnhanceSettings:
    target_vertices: int = 1_000_000
    method: str = METHOD_SUBDIVIDE
    smooth: float = 0.5  # bend new vertices onto the curved surface the shading normals describe (0 = flat)
    source: str = SOURCE_NORMAL
    strength: float = 1.0  # multiplier on the relief implied by the normal map
    height_range_mm: float = 1.0  # grayscale maps: displacement for the full 0..1 range
    green: str = "auto"  # normal map green channel: auto | directx | opengl
    detail_fraction: float = 1.0 / 16.0  # high-pass radius as a fraction of texture size
    max_displacement_mm: float | None = None
    prefilter: bool = True  # blur textures finer than the vertex spacing
    texture_overrides: dict[int, str] = field(default_factory=dict)  # material -> texture key ("" = none)
    mesh_options: MeshOptions = field(default_factory=MeshOptions)
    integration: str = INTEGRATE_MESH  # where normal-map slopes are integrated: mesh | texture
    edge_keeping: float = 0.0  # 0..1: how strongly inconsistent slopes are kept from smearing (mesh integration)
    smooth_sampling: bool = True  # bicubic instead of bilinear texture reads
    deblock: str = "off"  # block-compression clean-up: off | smooth | ai
    edge_smoothing: float = 1.0  # 0..1: how much stair-stepped edges in normal maps are smoothed along their length
    upsample: str = "off"  # off | guided (albedo-guided) | ai
    ai_model: str = ""  # upscaling model file in models_dir ("": the included one for the texture's compression)
    deblock_model: str = ""  # 1x deblocking model file in models_dir
    upsample_factor: int = 0  # 0: as the vertex spacing needs; else 2 or 4 (an AI model's own factor is brought to it)
    models_dir: Path | None = None
    material_strength: dict[int, float] = field(default_factory=dict)  # material -> extra strength multiplier
    edge_hints: bool = False  # let specular/metallic mask edges decouple the integration
    albedo_detail_mm: float = 0.0  # depth of the finest albedo-derived relief (0 = off)
    detail_normals: bool = False  # layer the material's tiled detail normal map on top
    detail_strength: float = 1.0


@dataclass
class MaterialField:
    material: int
    texture: str
    field: HeightField
    world: np.ndarray  # height in model units (texture-space integration, or a grayscale map)
    texel_size: float  # model units per texel
    slopes: SlopeLayer | None = None  # for integration on the mesh
    extras: list[tuple[np.ndarray, tuple[float, float]]] = field(default_factory=list)  # added heights + UV scale
    steps: list[str] = field(default_factory=list)  # what was done to the textures, for display


@dataclass
class EnhanceResult:
    source: TriMesh
    resampled: TriMesh
    displaced: TriMesh
    fields: dict[int, MaterialField]
    materials: list[Material]
    warnings: list[str] = field(default_factory=list)
    displaced_by_textures: bool = True  # False: step 1 alone (or nothing to displace by chosen)

    def stats(self) -> dict:
        h = self.displaced.metadata.get("displacement")
        return {
            "source_vertices": self.source.n_vertices,
            "source_faces": self.source.n_faces,
            "vertices": self.displaced.n_vertices,
            "faces": self.displaced.n_faces,
            "max_displacement_mm": float(np.abs(h).max() * 1000) if h is not None and len(h) else 0.0,
        }


def _report(progress: ProgressFn | None, frac: float, msg: str) -> None:
    if progress:
        progress(frac, msg)


UPSCALE_SETTINGS = ("green", "deblock", "deblock_model", "edge_smoothing", "upsample", "ai_model",
                    "upsample_factor")  # what a map made ready depends on


@dataclass
class UpscaledMap:
    """A normal map made ready for displacement by the texture settings (:data:`UPSCALE_SETTINGS`): cleaned
    up and upsampled, kept as slopes at the new size. Made once (the Textures tab's step), it serves every
    displacement after it, and drawn as a normal map before and after it shows what the settings did."""

    key: str
    convention: str
    confidence: float
    bias: tuple[float, float]
    p0: np.ndarray  # the texture's own slopes, bias removed (float32)
    q0: np.ndarray
    p: np.ndarray  # the slopes made ready, ``factor`` x the texture's size
    q: np.ndarray
    factor: int
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return self.p0.shape

    @property
    def nbytes(self) -> int:
        return self.p0.nbytes + self.q0.nbytes + self.p.nbytes + self.q.nbytes

    def normal_map(self, upscaled: bool = True, region: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """The slopes drawn as a normal map (RGBA uint8): the whole, or ``region`` = (y0, y1, x0, x1) of it."""
        p, q = (self.p, self.q) if upscaled else (self.p0, self.q0)
        if region is not None:
            y0, y1, x0, x1 = region
            p, q = p[y0:y1, x0:x1], q[y0:y1, x0:x1]
        return hm.slopes_to_normal_map(p, q, self.convention)


@dataclass
class DetailMap:
    """A normal map displacement will read: the materials that use it, the UVs they cover it with and the
    size of its texels on the model (from the first material using it, as :func:`build_fields` does)."""

    key: str
    materials: list[int]
    texel: float
    all_uv: np.ndarray
    green: str
    codec: str | None


def detail_maps(model: SourceModel, source: TriMesh, materials: list[Material], settings: EnhanceSettings) -> list[DetailMap]:
    """The normal maps displacement with these settings reads from ``source``'s materials, in material order."""
    out: dict[str, DetailMap] = {}
    if settings.source != SOURCE_NORMAL or source.corner_uvs is None:
        return []
    for mat in np.unique(source.face_material):
        mat = int(mat)
        if settings.texture_overrides.get(mat, None) == "":
            continue
        key = _material_texture(materials, settings, mat)
        if not key or key not in model.textures:
            continue
        info = materials[mat] if mat < len(materials) else None
        sel = source.face_material == mat
        if key in out:
            out[key].materials.append(mat)
            out[key].all_uv = np.concatenate([out[key].all_uv, source.corner_uvs[sel]])
            continue
        h, w = model.textures[key].decode().shape[:2]
        texel = texel_world_size(source.vertices, source.faces[sel], source.corner_uvs[sel], w, h)
        green = settings.green
        if green == "auto" and info is not None and info.normal_green:
            green = info.normal_green
        out[key] = DetailMap(key, [mat], texel, source.corner_uvs[sel], green, model.textures[key].codec())
    return list(out.values())


def plan_upscale(settings: EnhanceSettings, texel: float, spacing: float, codec: str | None, h: int, w: int,
                 warnings: list[str] | None = None):
    """What the texture settings mean for a texture of ``h`` x ``w``: the factor its slopes are brought to and
    the AI model used (None: none). Automatic (``upsample_factor`` 0) goes by how much closer together the
    vertices are than the texels; a factor asked for is used as it is."""
    from .textures import upscale

    want = int(settings.upsample_factor or 0)
    need = texel / max(spacing, 1e-12) if texel > 0 else 0.0
    if settings.upsample == "ai":
        if not want and need <= 1.5:
            return 1, None
        m = _ai(settings, settings.ai_model, "upscaling", warnings if warnings is not None else [], codec)
        if m is None:
            return 1, None
        if max(h, w) * m.scale > MAX_UPSCALED:
            if warnings is not None:
                warnings.append(f"AI upscaling skipped for {w} x {h}: the result would pass {MAX_UPSCALED} px")
            return 1, None
        return (want or m.scale), m
    if settings.upsample == "guided":
        if want:
            g = want
        else:
            g = 1
            while g < 4 and need > 1.5 * g:
                g *= 2
        while g > 1 and max(h, w) * g > min(MAX_UPSCALED, 4096):
            g //= 2
        return g, None
    return 1, None


def upscale_normal_map(model: SourceModel, info: Material | None, img: np.ndarray, all_uv: np.ndarray, green: str,
                       texel: float, spacing: float, settings: EnhanceSettings, key: str = "",
                       codec: str | None = None, progress: ProgressFn | None = None) -> UpscaledMap:
    """Make one normal map ready for displacement, as the texture settings ask: find its convention, take out
    its slope bias, clean up compression blocks, smooth stair-stepped edges, and upsample it (an AI model or guided by the colour texture)."""
    from .textures import upscale

    steps: list[str] = []
    warnings: list[str] = []
    h, w = img.shape[:2]
    mask = uv_coverage_mask(all_uv, w, h)
    confidence = 0.0
    convention = green
    if convention == "auto":
        convention, confidence = hm.detect_convention(img, mask)
    p0, q0 = hm.normal_map_slopes(img, convention)
    bias = hm.slope_bias(p0, q0, mask)
    p0, q0 = p0 - bias[0], q0 - bias[1]
    if max(abs(bias[0]), abs(bias[1])) > 0.004:
        steps.append(f"removed a {max(abs(bias[0]), abs(bias[1])):.3f} slope bias")
    work = img
    if settings.deblock == "ai":
        _report(progress, 0.1, "Cleaning up compression blocks")
        m = _ai(settings, settings.deblock_model, "deblocking", warnings)
        if m is not None:
            work = upscale.normal_map(m, work, rg_only=False)
            steps.append(f"deblocked by {m.path.name}")
    if settings.edge_smoothing > 0:
        # before upsampling: upscalers keep the steps they are shown (only bigger), so they go first
        _report(progress, 0.2, "Smoothing jagged edges")
        sp, sq = hm.smooth_jagged_edges(*hm.normal_map_slopes(work, convention), strength=settings.edge_smoothing)
        work = hm.slopes_to_normal_map(sp, sq, convention)
        steps.append("smoothed jagged edges")
    factor, ai_model = plan_upscale(settings, texel, spacing, codec, h, w, warnings)
    base = None
    if settings.upsample == "ai" and ai_model is None and not settings.upsample_factor and texel > 0 \
            and texel / max(spacing, 1e-12) <= 1.5:
        steps.append("no AI upscaling needed: the vertices are no closer together than the texels")
    if ai_model is not None:
        _report(progress, 0.3, f"Upscaling {ai_model.scale}x with {ai_model.path.name}")
        base = work
        work = upscale.normal_map(ai_model, work)
        picked = f", picked for {codec}" if codec and codec.startswith("BC") else ""
        steps.append(f"{ai_model.scale}x by {ai_model.path.name}" + ("" if settings.ai_model else f" (included{picked})"))
    p, q = hm.normal_map_slopes(work, convention)
    p, q = p - bias[0], q - bias[1]
    if base is not None:
        # Keep the model's fine detail but the texture's own broader shape: upscalers drift by a
        # degree or two and bend broad slopes, which integration would turn into bulges.
        bp, bq = hm.normal_map_slopes(base, convention)
        sigma = ai_model.scale / 2
        p = hm.resize(bp - bias[0], p.shape, 3) + p - ndimage.gaussian_filter(p, sigma, mode="wrap")
        q = hm.resize(bq - bias[1], q.shape, 3) + q - ndimage.gaussian_filter(q, sigma, mode="wrap")
        if factor != ai_model.scale:
            p, q = hm.resize(p, (h * factor, w * factor), 3), hm.resize(q, (h * factor, w * factor), 3)
            steps.append(f"brought to {factor}x")
    if settings.deblock == "smooth" and factor == 1:
        p, q = hm.smooth_slopes(p, q, 0.7)
        steps.append("smoothed compression blocks")
    if settings.upsample == "guided" and factor > 1:
        _report(progress, 0.3, f"Upsampling {factor}x")
        guide = None
        if info is not None and info.albedo and info.albedo in model.textures:
            guide = hm.luminance(model.textures[info.albedo].decode())
        p, q = hm.guided_upsample(p, q, guide, factor)
        steps.append(f"{factor}x guided by the albedo" if guide is not None else f"{factor}x cubic upsampling")
    _report(progress, 1.0, "Done")
    return UpscaledMap(key, convention, confidence, (float(bias[0]), float(bias[1])), p0.astype(np.float32),
                       q0.astype(np.float32), p.astype(np.float32), q.astype(np.float32), int(factor), steps, warnings)


def build_fields(model: SourceModel, source: TriMesh, materials: list[Material], settings: EnhanceSettings,
                 spacing: float, warnings: list[str], progress: ProgressFn | None = None,
                 prepared: dict[str, UpscaledMap] | None = None) -> dict[int, MaterialField]:
    """``prepared``: normal maps made ready before (:func:`upscale_normal_map`, by texture key); the others
    are made here."""
    fields: dict[int, MaterialField] = {}
    if settings.source == SOURCE_NONE or source.corner_uvs is None:
        return fields
    used = np.unique(source.face_material)
    cache: dict[str, dict] = {}
    for n_done, mat in enumerate(used):
        mat = int(mat)
        info = materials[mat] if mat < len(materials) else None
        if settings.texture_overrides.get(mat, None) == "":
            continue  # displacement switched off for this material
        key = _material_texture(materials, settings, mat)
        if not key or key not in model.textures:
            name = info.name if info else f"#{mat}"
            warnings.append(f"Material '{name}' has no {settings.source} map; it will not be displaced")
            continue
        _report(progress, n_done / max(len(used), 1), f"Building height field from {model.textures[key].name}")
        img = model.textures[key].decode()
        h, w = img.shape[:2]
        sel = source.face_material == mat
        uvs = source.corner_uvs[sel]
        texel = texel_world_size(source.vertices, source.faces[sel], uvs, w, h)
        strength = settings.strength * float(settings.material_strength.get(mat, 1.0))
        cache_key = f"{key}:{settings.source}"
        if cache_key not in cache:
            if settings.source == SOURCE_HEIGHT:
                cache[cache_key] = {"hf": height_from_grayscale(img), "steps": []}
            else:
                all_uv = source.corner_uvs[np.isin(source.face_material,
                                                   [m for m in used if _material_texture(materials, settings, int(m)) == key])]
                green = settings.green
                if green == "auto" and info is not None and info.normal_green:
                    green = info.normal_green  # the importer knows; detection is for normal maps of unknown origin
                cache[cache_key] = _normal_layers(model, info, img, all_uv, green, texel, spacing, settings, warnings,
                                                  model.textures[key].codec(), key, (prepared or {}).get(key))
        entry = cache[cache_key]
        hf = entry["hf"]
        if hf.units == "texels":
            if settings.integration == INTEGRATE_TEXTURE:
                world = entry["hf_up"].height * (entry["texel_up_ratio"] * texel * strength)
                texel_eff = texel * entry["texel_up_ratio"]
            else:
                world = hf.height * (texel * strength)
                texel_eff = texel
        else:
            world = hf.height * (settings.height_range_mm / 1000.0) * strength
            texel_eff = texel
        if settings.prefilter and texel_eff > 0:
            sigma = 0.5 * spacing / texel_eff
            if sigma > 0.6:
                world = ndimage.gaussian_filter(world, sigma, mode="wrap")
        mf = MaterialField(mat, key, hf, world.astype(np.float32), texel, steps=list(entry["steps"]))
        if hf.units == "texels" and settings.integration == INTEGRATE_MESH:
            p, q = entry["p"], entry["q"]
            texel_up = texel * entry["texel_up_ratio"]
            if settings.prefilter and texel_up > 0:
                sigma = 0.5 * spacing / texel_up
                if sigma > 0.6:
                    p, q = hm.smooth_slopes(p, q, sigma)
            scale = np.asarray(info.normal_scale if info is not None else (1.0, 1.0), np.float64)
            mf.slopes = SlopeLayer(p, q, texel_up * strength / float(np.sqrt(abs(scale[0] * scale[1])) or 1.0),
                                   tuple(scale), entry.get("hint"))
        if info is not None:
            mf.extras = _extra_layers(model, info, source, sel, uvs, texel, spacing, strength, settings, warnings, mf.steps)
        fields[mat] = mf
    return fields


def _model_path(settings: EnhanceSettings, name: str) -> Path | None:
    if not name:
        return None
    path = Path(name)
    if not path.is_absolute() and settings.models_dir is not None:
        path = Path(settings.models_dir) / name
    return path


def _ai(settings: EnhanceSettings, name: str, what: str, warnings: list[str], codec: str | None = None):
    from .textures import upscale

    path = _model_path(settings, name)
    if path is None and what == "upscaling":
        path = upscale.automatic_model(codec)
    if path is None:
        warnings.append(f"Pick a model for AI {what} in the Generate settings; skipped it")
        return None
    try:
        return upscale.load(str(path))
    except upscale.UpscaleError as e:
        warnings.append(f"AI {what} skipped: {e}")
        return None


def _normal_layers(model: SourceModel, info: Material | None, img: np.ndarray, all_uv: np.ndarray, green: str,
                   texel: float, spacing: float, settings: EnhanceSettings, warnings: list[str],
                   codec: str | None = None, key: str = "", prepared: UpscaledMap | None = None) -> dict:
    """Slopes from one normal map, cleaned and upsampled as the settings ask (``prepared``, or made now), plus
    its texture-space height."""
    up = prepared if prepared is not None else upscale_normal_map(model, info, img, all_uv, green, texel, spacing,
                                                                  settings, key, codec)
    warnings.extend(up.warnings)
    steps = list(up.steps)
    h, w = img.shape[:2]
    mask = uv_coverage_mask(all_uv, w, h)
    convention = up.convention
    hf = hm.height_from_slopes(up.p0, up.q0, mask, settings.detail_fraction, convention, up.confidence)
    p, q, factor = up.p, up.q, up.factor
    out = {"hf": hf, "p": p, "q": q, "texel_up_ratio": 1.0 / factor, "steps": steps}
    if settings.integration == INTEGRATE_TEXTURE:
        if factor == 1 and settings.deblock == "off" and settings.upsample == "off":
            out["hf_up"] = hf
        else:
            mask_up = mask if factor == 1 else uv_coverage_mask(all_uv, w * factor, h * factor)
            out["hf_up"] = hm.height_from_slopes(p, q, mask_up, settings.detail_fraction, convention, hf.confidence)
    if settings.edge_hints and info is not None and info.specular and info.specular in model.textures:
        hint = hm.edge_hint(model.textures[info.specular].decode())
        out["hint"] = hm.resize(hint, p.shape, 1)
        steps.append(f"edge hints from {model.textures[info.specular].name}")
    if hf.confidence and hf.confidence < 1.3:
        warnings.append(f"{model.textures[info.normal].name if info and info.normal in model.textures else 'Normal map'}: "
                        f"normal map convention unclear (picked {hf.convention}); try the other one if the relief looks wrong")
    return out


def _extra_layers(model: SourceModel, info: Material, source: TriMesh, sel: np.ndarray, uvs: np.ndarray,
                  texel: float, spacing: float, strength: float, settings: EnhanceSettings,
                  warnings: list[str], steps: list[str]) -> list[tuple[np.ndarray, tuple[float, float]]]:
    """Fine height layers added on top of the integrated relief: albedo detail and detail normal maps."""
    out = []

    def prefilter(world: np.ndarray, texel_size: float) -> np.ndarray:
        if settings.prefilter and texel_size > 0 and 0.5 * spacing / texel_size > 0.6:
            return ndimage.gaussian_filter(world, 0.5 * spacing / texel_size, mode="wrap")
        return world

    if settings.albedo_detail_mm > 0 and info.albedo and info.albedo in model.textures:
        tex = model.textures[info.albedo]
        detail = hm.albedo_detail(tex.decode())
        ah, aw = detail.shape
        t = texel_world_size(source.vertices, source.faces[sel], uvs, aw, ah)
        world = detail * (settings.albedo_detail_mm / 1000.0) * (strength / max(settings.strength, 1e-9))
        out.append((prefilter(world, t).astype(np.float32), (1.0, 1.0)))
        steps.append(f"albedo detail from {tex.name}")
    if settings.detail_normals and settings.source == SOURCE_NORMAL:
        if info.detail_normal and info.detail_normal in model.textures:
            tex = model.textures[info.detail_normal]
            dimg = tex.decode()
            dh, dw = dimg.shape[:2]
            scale = np.asarray(info.detail_scale, np.float64)
            t = texel_world_size(source.vertices, source.faces[sel], uvs * scale.astype(np.float32), dw, dh)
            hfd = height_from_normal_map(dimg, info.normal_green or settings.green, None, 0.125)
            world = hfd.height * (t * strength * settings.detail_strength)
            out.append((prefilter(world, t).astype(np.float32), tuple(scale)))
            steps.append(f"detail normal {tex.name} x{scale[0]:g}")
    return out


def _material_texture(materials: list[Material], settings: EnhanceSettings, mat: int) -> str | None:
    if mat in settings.texture_overrides:
        return settings.texture_overrides[mat]
    if mat >= len(materials):
        return None
    return materials[mat].height if settings.source == SOURCE_HEIGHT else materials[mat].normal


def vertex_heights(carrier: TriMesh, fields: dict[int, MaterialField], materials: list[Material],
                   settings: EnhanceSettings, spacing: float, progress: ProgressFn | None = None) -> np.ndarray:
    """One height per carrier vertex (model units) from the materials' fields."""
    slopes = {m: f.slopes for m, f in fields.items() if f.slopes is not None}
    scales = {m: materials[m].normal_scale for m in fields
              if m < len(materials) and settings.source == SOURCE_NORMAL}
    if slopes:
        screen = max((settings.detail_fraction * max(f.field.shape) * f.texel_size for f in fields.values()),
                     default=0.0)
        heights = integrate_on_mesh(carrier, slopes, screen, settings.edge_keeping, progress=progress)
        rest = {m: f.world for m, f in fields.items() if f.slopes is None}
        if rest:  # materials with only a grayscale map, if any are mixed in
            heights = heights + _corner_mean(carrier, corner_heights(carrier, rest, scales, settings.smooth_sampling))
    else:
        _report(progress, 0.0, "Matching heights across UV seams")
        per_corner = corner_heights(carrier, {m: f.world for m, f in fields.items()}, scales, settings.smooth_sampling)
        undisplaced = set(np.unique(carrier.face_material).tolist()) - set(fields)
        heights = seam_consistent_heights(carrier, per_corner, undisplaced)
    extras = [(m, world, scale) for m, f in fields.items() for world, scale in f.extras]
    for m, world, scale in extras:
        heights = heights + _corner_mean(carrier, corner_heights(carrier, {m: world}, {m: scale}, settings.smooth_sampling))
    return heights


def _corner_mean(mesh: TriMesh, per_corner: np.ndarray) -> np.ndarray:
    """Per-vertex mean of per-corner values, over the corners that have one (non-zero)."""
    v = mesh.faces.reshape(-1)
    vals = per_corner.reshape(-1)
    has = vals != 0
    cnt = np.bincount(v[has], minlength=mesh.n_vertices)
    return np.bincount(v, weights=vals, minlength=mesh.n_vertices) / np.maximum(cnt, 1)


@dataclass
class Resampled:
    """Step 1's result: the model's mesh evened out to the vertex count asked for. It depends only on the
    model, its parts and pose (``mesh_options``) and :data:`MESH_SETTINGS`, so trying other displacement
    settings on it (step 2) doesn't need it made again."""

    source: TriMesh
    resampled: TriMesh
    carrier: TriMesh  # where heights are worked out (the dense mesh behind an isotropic remesh)
    projection: tuple[np.ndarray, np.ndarray] | None  # carrier face + barycentrics of each resampled vertex
    spacing: float
    warnings: list[str] = field(default_factory=list)


MESH_SETTINGS = ("target_vertices", "method", "smooth")  # the settings step 1 depends on (with mesh_options)


def resample(model: SourceModel, settings: EnhanceSettings | None = None,
             progress: ProgressFn | None = None) -> Resampled:
    """Step 1: build the model's mesh and even it out to ``settings.target_vertices``."""
    settings = settings or EnhanceSettings()
    warnings: list[str] = []
    _report(progress, 0.02, "Building mesh")
    source = model.build_mesh(settings.mesh_options)
    if source.n_faces == 0:
        raise ValueError(f"{model.name} has no triangles to work on" if not model.parts
                         else "The selected parts have no triangles")
    area = source.surface_area()
    target = max(int(settings.target_vertices), 4)
    spacing = edge_length_for_vertex_count(area, target)
    method = settings.method
    if method == METHOD_ISOTROPIC and not acvd_available():
        warnings.append("Isotropic remeshing needs the 'pyacvd' package; used adaptive subdivision instead")
        method = METHOD_SUBDIVIDE
    if method == METHOD_ISOTROPIC:
        resampled = isotropic_remesh(source, target, progress=_sub(progress, 0.05, 1.0), smooth=settings.smooth)
        carrier = resampled.metadata.pop("dense")  # heights are worked out here, where UVs are exact
        projection = resampled.metadata.pop("projection")
    else:
        if target <= source.n_vertices:
            warnings.append(f"Target ({target:,}) is not above the model's {source.n_vertices:,} vertices; "
                            "adaptive subdivision only adds vertices, so the mesh is unchanged")
        resampled = subdivide_to_vertex_count(source, target, progress=_sub(progress, 0.05, 1.0), smooth=settings.smooth)
        carrier, projection = resampled, None
    return Resampled(source, resampled, carrier, projection, spacing, warnings)


def displace_resampled(model: SourceModel, mesh: Resampled, settings: EnhanceSettings | None = None,
                       progress: ProgressFn | None = None, prepared: dict[str, UpscaledMap] | None = None) -> EnhanceResult:
    """Step 2: work out the heights from the textures (``prepared``: normal maps made ready already, by
    texture key) and push the evened-out mesh's vertices by them."""
    settings = settings or EnhanceSettings()
    warnings = list(mesh.warnings)
    materials = model.materials
    fields = build_fields(model, mesh.source, materials, settings, mesh.spacing, warnings, _sub(progress, 0.0, 0.6), prepared)
    heights = np.zeros(mesh.resampled.n_vertices)
    if fields and mesh.carrier.corner_uvs is not None:
        heights = vertex_heights(mesh.carrier, fields, materials, settings, mesh.spacing, _sub(progress, 0.6, 0.9))
        if mesh.projection is not None:
            face, bary = mesh.projection
            heights = np.einsum("ij,ij->i", bary, heights[mesh.carrier.faces[face]])
    _report(progress, 0.9, "Displacing vertices")
    max_disp = settings.max_displacement_mm / 1000.0 if settings.max_displacement_mm else None
    displaced = displace(mesh.resampled, heights, max_disp)
    _report(progress, 1.0, "Done")
    return EnhanceResult(mesh.source, mesh.resampled, displaced, fields, materials, warnings,
                         displaced_by_textures=settings.source != SOURCE_NONE)


def enhance(model: SourceModel, settings: EnhanceSettings | None = None,
            progress: ProgressFn | None = None, mesh: Resampled | None = None,
            on_mesh: Callable[[Resampled], None] | None = None,
            prepared: dict[str, UpscaledMap] | None = None) -> EnhanceResult:
    """Both steps. ``mesh``: the evened-out mesh made before with the same mesh settings (see
    :data:`MESH_SETTINGS`), to skip making it again; ``on_mesh`` is handed the one made here. ``prepared``:
    normal maps made ready before (:func:`upscale_normal_map`), by texture key."""
    settings = settings or EnhanceSettings()
    if mesh is None:
        mesh = resample(model, settings, _sub(progress, 0.0, 0.6))
        if on_mesh is not None:
            on_mesh(mesh)
        progress = _sub(progress, 0.6, 1.0)
    else:
        _report(progress, 0.0, f"Using the evened-out mesh made before ({mesh.resampled.n_vertices:,} vertices)")
    return displace_resampled(model, mesh, settings, progress, prepared)


def _sub(progress: ProgressFn | None, lo: float, hi: float) -> ProgressFn | None:
    if progress is None:
        return None
    return lambda f, m: progress(lo + (hi - lo) * f, m)
