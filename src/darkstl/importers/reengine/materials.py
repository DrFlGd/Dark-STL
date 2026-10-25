"""Which texture plays which role in an RE Engine material.

The materials file names each slot by type. Early games use long names (``BaseMetalMap``,
``NormalRoughnessMap``, ``BaseDielectricMap``); Resident Evil 4 and later use four-letter codes
(``ALBD`` albedo, ``NRMR`` normal + roughness, ``ATOS`` ambient occlusion/translucency/..., ``OCMA``
occlusion/cavity/metallic/...). RE Engine normal maps keep X and Y in red and green with Y pointing
up the texture (the OpenGL convention), and roughness or another mask in blue and alpha.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...model import Material
from .formats.mdf import MdfMaterial

ROLE_ALBEDO, ROLE_NORMAL, ROLE_HEIGHT, ROLE_DETAIL_NORMAL, ROLE_SPECULAR, ROLE_OTHER = (
    "albedo", "normal", "height", "detail_normal", "specular", "other")

_TYPE_RULES = [
    (re.compile(r"detail.*(normal|nrm)|(normal|nrm).*detail|^nrmd|^dtn", re.I), ROLE_DETAIL_NORMAL),
    (re.compile(r"height|displace|^hght|^disp", re.I), ROLE_HEIGHT),
    (re.compile(r"normal|^nrm|^nrr|^nram|bumpmap", re.I), ROLE_NORMAL),
    (re.compile(r"^base|albedo|^alb[a-z]?$|^alb|basecolor|diffuse|^col", re.I), ROLE_ALBEDO),
    (re.compile(r"metal|rough|specular|^atos|^ocma|^mtl|^rgh|cavity|occlusion", re.I), ROLE_SPECULAR),
]
_SECONDARY = re.compile(r"(2|_1|_2|second|layer\d|sub|array)$", re.I)


def role_of(slot_type: str) -> str:
    for rx, role in _TYPE_RULES:
        if rx.search(slot_type or ""):
            return role
    return ROLE_OTHER


@dataclass
class ResolvedMaterial:
    name: str
    shader: str
    albedo: str | None = None
    normal: str | None = None
    height: str | None = None
    specular: str | None = None
    detail_normal: str | None = None
    details: list[str] = field(default_factory=list)


def resolve(name: str, mdf_material: MdfMaterial | None, textures: dict) -> ResolvedMaterial:
    """Pick the textures of a mesh material from its mdf2 entry (``textures``: lower-case stem -> texture)."""
    out = ResolvedMaterial(name, mdf_material.shader if mdf_material else "")
    if mdf_material is None:
        out.details.append("no materials file")
        return out
    shader = mdf_material.shader.replace("\\", "/").rsplit("/", 1)[-1]
    out.details.append(f"shader {shader}" if shader else "shader -")
    for binding in sorted(mdf_material.textures, key=lambda b: bool(_SECONDARY.search(b.type))):
        stem = binding.path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        key = stem.lower()
        found = key in textures
        out.details.append(f"{binding.type}: {stem or '-'}{'' if found or not stem else ' (missing)'}")
        if not found:
            continue
        role = role_of(binding.type)
        if role == ROLE_ALBEDO and out.albedo is None:
            out.albedo = key
        elif role == ROLE_NORMAL and out.normal is None:
            out.normal = key
        elif role == ROLE_HEIGHT and out.height is None:
            out.height = key
        elif role == ROLE_SPECULAR and out.specular is None:
            out.specular = key
        elif role == ROLE_DETAIL_NORMAL and out.detail_normal is None:
            out.detail_normal = key
    return out


def to_material(info: ResolvedMaterial) -> Material:
    return Material(info.name, info.albedo, info.normal, info.height, (1.0, 1.0), False, info.details,
                    normal_green="opengl", specular=info.specular, detail_normal=info.detail_normal)


__all__ = ["ResolvedMaterial", "resolve", "role_of", "to_material"]
