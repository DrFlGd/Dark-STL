"""The enhancement pipeline: even remesh -> texture displacement -> export.

It works on :class:`~darkstl.model.SourceModel`, so it is the same for every
game; only the importer that produced the model differs.

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
    upsample: str = "off"  # off | guided (albedo-guided) | ai
    ai_model: str = ""  # upscaling model file in models_dir
    deblock_model: str = ""  # 1x deblocking model file in models_dir
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


def build_fields(model: SourceModel, source: TriMesh, materials: list[Material], settings: EnhanceSettings,
                 spacing: float, warnings: list[str], progress: ProgressFn | None = None) -> dict[int, MaterialField]:
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
                cache[cache_key] = _normal_layers(model, info, img, all_uv, green, texel, spacing, settings, warnings)
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


def _ai(settings: EnhanceSettings, name: str, what: str, warnings: list[str]):
    from .textures import upscale

    path = _model_path(settings, name)
    if path is None:
        warnings.append(f"Pick a model for AI {what} in the Enhance settings; skipped it")
        return None
    try:
        return upscale.load(str(path))
    except upscale.UpscaleError as e:
        warnings.append(f"AI {what} skipped: {e}")
        return None


def _normal_layers(model: SourceModel, info: Material | None, img: np.ndarray, all_uv: np.ndarray, green: str,
                   texel: float, spacing: float, settings: EnhanceSettings, warnings: list[str]) -> dict:
    """Slopes from one normal map, cleaned and upsampled as the settings ask, plus its texture-space height."""
    from .textures import upscale

    steps: list[str] = []
    h, w = img.shape[:2]
    mask = uv_coverage_mask(all_uv, w, h)
    confidence = 0.0
    convention = green
    if convention == "auto":
        convention, confidence = hm.detect_convention(img, mask)
    bias = hm.slope_bias(*hm.normal_map_slopes(img, convention), mask)
    p0, q0 = hm.normal_map_slopes(img, convention)
    hf = hm.height_from_slopes(p0 - bias[0], q0 - bias[1], mask, settings.detail_fraction, convention, confidence)
    if max(abs(bias[0]), abs(bias[1])) > 0.004:
        steps.append(f"removed a {max(abs(bias[0]), abs(bias[1])):.3f} slope bias")
    work = img
    if settings.deblock == "ai":
        m = _ai(settings, settings.deblock_model, "deblocking", warnings)
        if m is not None:
            work = upscale.normal_map(m, work, rg_only=False)
            steps.append(f"deblocked by {m.path.name}")
    factor = 1
    if settings.upsample == "ai":
        m = _ai(settings, settings.ai_model, "upscaling", warnings)
        if m is not None and max(h, w) * m.scale <= MAX_UPSCALED:
            work = upscale.normal_map(m, work)
            factor = m.scale
            steps.append(f"{m.scale}x by {m.path.name}")
        elif m is not None:
            warnings.append(f"AI upscaling skipped for {w} x {h}: the result would pass {MAX_UPSCALED} px")
    p, q = hm.normal_map_slopes(work, convention)
    p, q = p - bias[0], q - bias[1]
    if settings.deblock == "smooth" and factor == 1:
        p, q = hm.smooth_slopes(p, q, 0.7)
        steps.append("smoothed compression blocks")
    if settings.upsample == "guided" and texel > 0:
        need = texel / max(spacing, 1e-12)
        g = 1
        while g < 4 and need > 1.5 * g and max(h, w) * g * 2 <= min(MAX_UPSCALED, 4096):
            g *= 2
        if g > 1:
            guide = None
            if info is not None and info.albedo and info.albedo in model.textures:
                guide = hm.luminance(model.textures[info.albedo].decode())
            p, q = hm.guided_upsample(p, q, guide, g)
            factor *= g
            steps.append(f"{g}x guided by the albedo" if guide is not None else f"{g}x cubic upsampling")
    out = {"hf": hf, "p": p.astype(np.float32), "q": q.astype(np.float32), "texel_up_ratio": 1.0 / factor,
           "steps": steps}
    if settings.integration == INTEGRATE_TEXTURE:
        if factor == 1 and work is img and settings.deblock != "smooth" and settings.upsample == "off":
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


def enhance(model: SourceModel, settings: EnhanceSettings | None = None,
            progress: ProgressFn | None = None) -> EnhanceResult:
    settings = settings or EnhanceSettings()
    warnings: list[str] = []
    _report(progress, 0.02, "Building mesh")
    source = model.build_mesh(settings.mesh_options)
    if source.n_faces == 0:
        raise ValueError(f"{model.name} has no triangles to work on" if not model.parts
                         else "The selected parts have no triangles")
    materials = model.materials
    area = source.surface_area()
    target = max(int(settings.target_vertices), 4)
    spacing = edge_length_for_vertex_count(area, target)

    def sub(lo: float, hi: float) -> ProgressFn | None:
        if progress is None:
            return None
        return lambda f, m: progress(lo + (hi - lo) * f, m)

    method = settings.method
    if method == METHOD_ISOTROPIC and not acvd_available():
        warnings.append("Isotropic remeshing needs the 'pyacvd' package; used adaptive subdivision instead")
        method = METHOD_SUBDIVIDE

    if method == METHOD_ISOTROPIC:
        resampled = isotropic_remesh(source, target, progress=sub(0.05, 0.6), smooth=settings.smooth)
        carrier = resampled.metadata.pop("dense")  # heights are worked out here, where UVs are exact
        projection = resampled.metadata.pop("projection")
    else:
        if target <= source.n_vertices:
            warnings.append(f"Target ({target:,}) is not above the model's {source.n_vertices:,} vertices; "
                            "adaptive subdivision only adds vertices, so the mesh is unchanged")
        resampled = subdivide_to_vertex_count(source, target, progress=sub(0.05, 0.6), smooth=settings.smooth)
        carrier, projection = resampled, None

    fields = build_fields(model, source, materials, settings, spacing, warnings, sub(0.6, 0.8))
    heights = np.zeros(resampled.n_vertices)
    if fields and carrier.corner_uvs is not None:
        heights = vertex_heights(carrier, fields, materials, settings, spacing, sub(0.8, 0.92))
        if projection is not None:
            face, bary = projection
            heights = np.einsum("ij,ij->i", bary, heights[carrier.faces[face]])
    _report(progress, 0.92, "Displacing vertices")
    max_disp = settings.max_displacement_mm / 1000.0 if settings.max_displacement_mm else None
    displaced = displace(resampled, heights, max_disp)
    _report(progress, 1.0, "Done")
    return EnhanceResult(source, resampled, displaced, fields, materials, warnings)
