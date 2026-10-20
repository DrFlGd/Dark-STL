"""The enhancement pipeline: even remesh -> texture displacement -> export.

1. Convert the FLVER to one welded triangle mesh (export frame, metres).
2. Re-sample the surface to the requested number of evenly spaced vertices,
   either by adaptive subdivision (exact UVs, keeps every original edge) or by
   isotropic ACVD remeshing (exact vertex count, uniform triangles).
3. Build a height field per material (integrated normal map, or a grayscale
   height map) and push every vertex along its normal by the sampled height.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy import ndimage

from .assets import ModelAsset
from .geometry.displace import VertexSamples, displace, vertex_heights
from .geometry.mesh import TriMesh
from .geometry.model_mesh import MeshOptions, flver_to_trimesh
from .geometry.remesh import acvd_available, isotropic_remesh
from .geometry.subdivide import edge_length_for_vertex_count, subdivide_to_vertex_count
from .materials import MaterialInfo, resolve_materials
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


@dataclass
class EnhanceSettings:
    target_vertices: int = 1_000_000
    method: str = METHOD_SUBDIVIDE
    source: str = SOURCE_NORMAL
    strength: float = 1.0  # multiplier on the relief implied by the normal map
    height_range_mm: float = 1.0  # grayscale maps: displacement for the full 0..1 range
    green: str = "auto"  # normal map green channel: auto | directx | opengl
    detail_fraction: float = 1.0 / 16.0  # high-pass radius as a fraction of texture size
    max_displacement_mm: float | None = None
    prefilter: bool = True  # blur textures finer than the vertex spacing
    texture_overrides: dict[int, str] = field(default_factory=dict)  # material -> texture key
    mesh_options: MeshOptions = field(default_factory=MeshOptions)


@dataclass
class MaterialField:
    material: int
    texture: str
    field: HeightField
    world: np.ndarray  # height in model units
    texel_size: float  # model units per texel


@dataclass
class EnhanceResult:
    source: TriMesh
    resampled: TriMesh
    displaced: TriMesh
    fields: dict[int, MaterialField]
    materials: list[MaterialInfo]
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


def build_fields(asset: ModelAsset, source: TriMesh, materials: list[MaterialInfo], settings: EnhanceSettings,
                 spacing: float, warnings: list[str], progress: ProgressFn | None = None) -> dict[int, MaterialField]:
    fields: dict[int, MaterialField] = {}
    if settings.source == SOURCE_NONE or source.corner_uvs is None:
        return fields
    used = np.unique(source.face_material)
    cache: dict[str, tuple[HeightField, np.ndarray]] = {}
    for n_done, mat in enumerate(used):
        mat = int(mat)
        info = materials[mat] if mat < len(materials) else None
        key = settings.texture_overrides.get(mat)
        if key is None and info is not None:
            key = info.height if settings.source == SOURCE_HEIGHT else info.normal
        if not key or key not in asset.textures:
            name = info.name if info else f"#{mat}"
            warnings.append(f"Material '{name}' has no {settings.source} map; it will not be displaced")
            continue
        _report(progress, n_done / max(len(used), 1), f"Building height field from {asset.textures[key].name}")
        img = asset.textures[key].decode()
        h, w = img.shape[:2]
        sel = source.face_material == mat
        uvs = source.corner_uvs[sel]
        texel = texel_world_size(source.vertices, source.faces[sel], uvs, w, h)
        cache_key = f"{key}:{settings.source}"
        if cache_key in cache:
            hf, _ = cache[cache_key]
        else:
            if settings.source == SOURCE_HEIGHT:
                hf = height_from_grayscale(img)
            else:
                all_uv = source.corner_uvs[np.isin(source.face_material,
                                                   [m for m in used if _material_texture(materials, settings, int(m)) == key])]
                mask = uv_coverage_mask(all_uv, w, h)
                hf = height_from_normal_map(img, settings.green, mask, settings.detail_fraction)
                if hf.confidence and hf.confidence < 1.3:
                    warnings.append(f"{asset.textures[key].name}: normal map convention unclear "
                                    f"(picked {hf.convention}); try the other one if the relief looks wrong")
            cache[cache_key] = (hf, img)
        if hf.units == "texels":
            world = hf.height * (texel * settings.strength)
        else:
            world = hf.height * (settings.height_range_mm / 1000.0) * settings.strength
        if settings.prefilter and texel > 0:
            sigma = 0.5 * spacing / texel
            if sigma > 0.6:
                world = ndimage.gaussian_filter(world, sigma, mode="wrap")
        fields[mat] = MaterialField(mat, key, hf, world.astype(np.float32), texel)
    return fields


def _material_texture(materials: list[MaterialInfo], settings: EnhanceSettings, mat: int) -> str | None:
    if mat in settings.texture_overrides:
        return settings.texture_overrides[mat]
    if mat >= len(materials):
        return None
    return materials[mat].height if settings.source == SOURCE_HEIGHT else materials[mat].normal


def enhance(asset: ModelAsset, settings: EnhanceSettings | None = None,
            progress: ProgressFn | None = None) -> EnhanceResult:
    settings = settings or EnhanceSettings()
    warnings: list[str] = []
    _report(progress, 0.02, "Building mesh")
    source = flver_to_trimesh(asset.flver, settings.mesh_options)
    if source.n_faces == 0:
        raise ValueError("The selected meshes have no triangles")
    materials = resolve_materials(asset)
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
        resampled = isotropic_remesh(source, target, progress=sub(0.05, 0.6))
        samples = VertexSamples.from_vertex_uvs(resampled, resampled.metadata["vertex_material"])
    else:
        if target <= source.n_vertices:
            warnings.append(f"Target ({target:,}) is not above the model's {source.n_vertices:,} vertices; "
                            "adaptive subdivision only adds vertices, so the mesh is unchanged")
        resampled = subdivide_to_vertex_count(source, target, progress=sub(0.05, 0.6))
        samples = VertexSamples.from_corners(resampled) if resampled.corner_uvs is not None else None

    fields = build_fields(asset, source, materials, settings, spacing, warnings, sub(0.6, 0.85))
    _report(progress, 0.88, "Displacing vertices")
    if fields and samples is not None:
        heights = vertex_heights(resampled.n_vertices, samples, {m: f.world for m, f in fields.items()},
                                 {m: materials[m].normal_scale for m in fields if m < len(materials)
                                  and settings.source == SOURCE_NORMAL})
    else:
        heights = np.zeros(resampled.n_vertices)
    max_disp = settings.max_displacement_mm / 1000.0 if settings.max_displacement_mm else None
    displaced = displace(resampled, heights, max_disp)
    _report(progress, 1.0, "Done")
    return EnhanceResult(source, resampled, displaced, fields, materials, warnings)
