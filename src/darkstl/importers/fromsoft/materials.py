"""Work out which texture plays which role (albedo, normal, height) for each material.

The FLVER names each texture slot (``g_DiffuseTexture``, ``g_BumpmapTexture``,
Elden Ring's ``..._AlbedoMap_0``/``..._NormalMap_0``) and gives a file path
whose base name matches a texture inside the model's TPFs. When paths are
empty (common in Elden Ring, where MATBIN files hold them) the texture names'
suffixes are used instead (``_a`` albedo, ``_n`` normal, ...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .files import ModelAsset

ROLE_ALBEDO = "albedo"
ROLE_NORMAL = "normal"
ROLE_HEIGHT = "height"
ROLE_DETAIL_NORMAL = "detail_normal"
ROLE_OTHER = "other"

_TYPE_RULES = [
    (re.compile(r"detail.*(bump|normal)|(bump|normal).*detail", re.I), ROLE_DETAIL_NORMAL),
    (re.compile(r"height|displace|parallax", re.I), ROLE_HEIGHT),
    (re.compile(r"bump|normal", re.I), ROLE_NORMAL),
    (re.compile(r"diffuse|albedo|base_?color|color_?map", re.I), ROLE_ALBEDO),
]

_SUFFIX_RULES = [
    (re.compile(r"_(n|nm|nrm|normal)$", re.I), ROLE_NORMAL),
    (re.compile(r"_(h|height|disp|displacement)$", re.I), ROLE_HEIGHT),
    (re.compile(r"_(a|d|c|albedo|diffuse|col)$", re.I), ROLE_ALBEDO),
]


def role_from_type(type_name: str) -> str:
    for rx, role in _TYPE_RULES:
        if rx.search(type_name or ""):
            return role
    return ROLE_OTHER


def role_from_name(texture_name: str) -> str:
    for rx, role in _SUFFIX_RULES:
        if rx.search(texture_name or ""):
            return role
    return ROLE_OTHER


def _is_secondary(type_name: str) -> bool:
    """Second blend layer slots ('g_DiffuseTexture2', '..._1') are lower priority."""
    return bool(re.search(r"(texture)?2$|_1$|_2$", type_name or "", re.I))


@dataclass
class TextureSlot:
    type: str
    path: str
    texture_name: str | None  # key into ModelAsset.textures when found
    role: str
    scale: tuple[float, float] = (1.0, 1.0)


@dataclass
class MaterialInfo:
    index: int
    name: str
    mtd: str
    slots: list[TextureSlot] = field(default_factory=list)
    albedo: str | None = None
    normal: str | None = None
    height: str | None = None
    normal_scale: tuple[float, float] = (1.0, 1.0)
    guessed: bool = False  # textures were matched by name heuristics, not FLVER paths


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.lower(), b.lower()):
        if x != y:
            break
        n += 1
    return n


def resolve_materials(asset: ModelAsset) -> list[MaterialInfo]:
    names = asset.textures
    infos = []
    for i, mat in enumerate(asset.flver.materials):
        info = MaterialInfo(i, mat.name, mat.mtd)
        for tex in mat.textures:
            stem = tex.stem
            key = stem.lower() if stem and stem.lower() in names else None
            role = role_from_type(tex.type)
            if role == ROLE_OTHER and key:
                role = role_from_name(stem)
            info.slots.append(TextureSlot(tex.type, tex.path, key, role, tex.scale))
        for role_name in (ROLE_ALBEDO, ROLE_NORMAL, ROLE_HEIGHT):
            primary = [s for s in info.slots if s.role == role_name and s.texture_name and not _is_secondary(s.type)]
            any_slot = [s for s in info.slots if s.role == role_name and s.texture_name]
            chosen = (primary or any_slot or [None])[0]
            if chosen is not None:
                setattr(info, role_name, chosen.texture_name)
                if role_name == ROLE_NORMAL:
                    info.normal_scale = chosen.scale
        infos.append(info)

    # Fill gaps from texture-name suffixes, preferring names that look related to the
    # material's other textures or to the model name.
    by_role: dict[str, list[str]] = {}
    for key in names:
        by_role.setdefault(role_from_name(key), []).append(key)
    for info in infos:
        anchors = [k for k in (info.albedo, info.normal, info.height) if k] or [asset.name.lower()]
        for role_name in (ROLE_NORMAL, ROLE_ALBEDO, ROLE_HEIGHT):
            if getattr(info, role_name) or not by_role.get(role_name):
                continue
            cands = by_role[role_name]
            best = max(cands, key=lambda c: (max(_common_prefix_len(c, a) for a in anchors), -len(c)))
            if max(_common_prefix_len(best, a) for a in anchors) >= 3 or len(cands) == 1:
                setattr(info, role_name, best)
                info.guessed = True
    return infos
