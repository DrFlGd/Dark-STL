"""Which textures a Borderlands (Unreal 3) material uses, and for what.

Meshes name material instances (``MaterialInstanceConstant``) that override
texture parameters of a parent instance or material. The chain is followed to
the material; the textures it references (``ReferencedTextures``) fill the
roles no parameter set. Roles come from parameter names (``p_Diffuse``,
``p_Normal``...), then from the texture itself: normal maps are compressed as
such (``TC_Normalmap``), and Gearbox names textures with role suffixes
(``_Dif``, ``_Nrm``, ``_Spec``...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...errors import FormatError
from .files import resolve, searched
from .formats import upk
from .formats.props import ArrayValue, ObjectRef, object_props
from .formats.texture import Texture2D, read_texture

ALBEDO, NORMAL, SPECULAR, HEIGHT, OTHER = "albedo", "normal", "specular", "height", "other"

_PARAM_RULES = [
    (re.compile(r"detail.*norm|norm.*detail", re.I), OTHER),
    (re.compile(r"height|displace|bump", re.I), HEIGHT),
    (re.compile(r"norm|nrm", re.I), NORMAL),
    (re.compile(r"diff|albedo|basecolor|base_color|colou?r|^p_d$|_dif$", re.I), ALBEDO),
    (re.compile(r"spec|gloss|mask|rough|metal", re.I), SPECULAR),
]
_NAME_RULES = [
    (re.compile(r"(_|^)(n|nrm|norm|normal|nm)(_|\d*$)", re.I), NORMAL),
    (re.compile(r"(_|^)(d|dif|diff|diffuse|alb|albedo|c|col|color|colour|bc|basecolou?r|base_colou?r)(_|\d*$)",
                re.I), ALBEDO),
    (re.compile(r"(_|^)(s|spec|specular|m|mask|msk|masks|g|gloss|orm|mra|rma|arm|mr|rm|r|rough|roughness)(_|\d*$)",
                re.I), SPECULAR),
    (re.compile(r"(_|^)(h|height|disp|bump)(_|\d*$)", re.I), HEIGHT),
]


def role_from_param(name: str) -> str:
    for rx, role in _PARAM_RULES:
        if rx.search(name or ""):
            return role
    return OTHER


def role_from_texture(tex: Texture2D) -> str:
    if tex.is_normal_map:
        return NORMAL
    for rx, role in _NAME_RULES:
        if rx.search(tex.name):
            return role
    return OTHER


@dataclass
class ResolvedMaterial:
    name: str
    roles: dict[str, Texture2D] = field(default_factory=dict)  # role -> texture
    details: list[str] = field(default_factory=list)
    guessed: bool = False


def _texture(ref: ObjectRef) -> Texture2D | None:
    hit = resolve(ref, "Texture2D")
    if hit is None:
        return None
    pkg, e = hit
    if e.class_name not in ("Texture2D", "LightMapTexture2D", "ShadowMapTexture2D", "TextureFlipBook"):
        return None
    try:
        return read_texture(pkg, e)
    except FormatError:
        return None


texture_of = _texture  # the Texture2D an object reference names, or None


def _material_textures(pkg: upk.Package, e: upk.Export) -> list[ObjectRef]:
    """A base material's referenced textures (a property before version 656, binary data after)."""
    r = pkg.export_data(e)
    props = object_props(pkg, r)
    refs = props.get("ReferencedTextures")
    if isinstance(refs, ArrayValue):
        return refs.objects()
    v = pkg.summary.version
    try:
        if v >= 858:
            r.i32()
        if v >= 656:
            for _ in range(r.count(1 << 16)):
                r.fstring()
            r.skip(8 * r.count(1 << 20))
            r.i32()
            r.skip(16)
            r.i32()
            return [ObjectRef(pkg, r.i32()) for _ in range(r.count(1 << 16))]
    except FormatError:
        pass
    return []


def resolve_material(ref: ObjectRef) -> ResolvedMaterial:
    out = ResolvedMaterial(ref.name if ref else "None")
    if not ref:
        out.details.append("no material")
        return out
    hit = resolve(ref)
    if hit is None:
        out.details.append(f"{ref.path} not found")
        return out
    params: list[tuple[str, ObjectRef]] = []
    leftovers: list[ObjectRef] = []
    seen = 0
    pkg, e = hit
    while seen < 12:
        seen += 1
        cls = e.class_name
        out.details.append(f"{cls} {e.name}")
        if cls.startswith("MaterialInstance"):
            props = object_props(pkg, pkg.export_data(e))
            values = props.get("TextureParameterValues")
            if isinstance(values, ArrayValue):
                try:
                    for p in values.structs():
                        tex = p.get("ParameterValue")
                        if isinstance(tex, ObjectRef) and tex:
                            params.append((str(p.get("ParameterName", "")), tex))
                except FormatError:
                    pass
            parent = props.get("Parent")
            if not isinstance(parent, ObjectRef) or not parent:
                break
            nxt = resolve(parent)
            if nxt is None:
                out.details.append(f"parent {parent.path} not found (looked in {searched(parent.pkg)})")
                break
            pkg, e = nxt
        elif cls == "Material":
            leftovers = _material_textures(pkg, e)
            break
        else:
            break
    for name, ref_t in params:  # nearest instance first: it overrides its parents
        role = role_from_param(name)
        if role in out.roles:
            continue
        tex = _texture(ref_t)
        if tex is None:
            out.details.append(f"{name}: {ref_t.path} not found (looked in {searched(ref_t.pkg)})")
            continue
        if role == OTHER:
            role = role_from_texture(tex)
            if role == OTHER or role in out.roles:
                out.details.append(f"{name}: {tex.name} (not used)")
                continue
        out.roles[role] = tex
        out.details.append(f"{name}: {tex.name}")
    for ref_t in leftovers:
        if all(r in out.roles for r in (ALBEDO, NORMAL, SPECULAR)):
            break
        tex = _texture(ref_t)
        if tex is None:
            continue
        role = role_from_texture(tex)
        if role != OTHER and role not in out.roles:
            out.roles[role] = tex
            out.guessed = True
            out.details.append(f"{role} (from the material): {tex.name}")
    return out


__all__ = ["ALBEDO", "HEIGHT", "NORMAL", "OTHER", "ResolvedMaterial", "SPECULAR", "resolve_material",
           "role_from_param", "role_from_texture"]
