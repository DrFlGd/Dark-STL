"""Which textures an Unreal 4/5 material uses, and for what.

Meshes name material instances; each overrides some texture parameters of its
parent (``TextureParameterValues``: a parameter name and a texture), up to the
material itself. Roles come from the parameter names, then from the textures
(compressed as normal maps, or named with a role suffix). For the textures a
base material uses directly, the textures its package imports are taken.
"""

from __future__ import annotations

import re

from ....errors import FormatError
from ..formats.props import ObjectRef
from ..materials import ALBEDO, NORMAL, OTHER, SPECULAR, ResolvedMaterial, role_from_param, role_from_texture
from . import guess
from .asset import Export, Package
from .mesh import object_props
from .props import Array4, Struct4
from .texture import Texture4, read_texture

TEXTURE_CLASSES = ("Texture2D", "VirtualTexture2D", "LightMapTexture2D")


def resolve(ref: ObjectRef) -> tuple[Package, Export] | None:
    """The package and export behind a reference, opening the package an import comes from."""
    pkg = ref.pkg
    if not ref:
        return None
    if ref.ref > 0:
        return pkg, pkg.exports[ref.ref - 1]
    imp = pkg.imports[-ref.ref - 1]
    files = getattr(pkg, "files", None)
    if files is None:
        return None
    name = pkg.import_package(ref.ref)
    if name == pkg.name:
        e = pkg.find_export(imp.name, imp.class_name)
        return (pkg, e) if e else None
    try:
        other = files.open_named(name)
    except (FormatError, OSError):
        return None
    if other is None:
        return None
    e = other.find_export(imp.name, imp.class_name) or other.find_export(imp.name)
    return (other, e) if e else None


def texture(ref: ObjectRef) -> Texture4 | None:
    hit = resolve(ref)
    if hit is None:
        return None
    pkg, e = hit
    if e.class_name not in TEXTURE_CLASSES:
        return None
    try:
        return read_texture(pkg, e)
    except (FormatError, OSError):
        return None


def _param_name(p) -> str:
    info = p.get("ParameterInfo")
    if isinstance(info, Struct4):
        try:
            return str(info.props().get("Name", ""))
        except FormatError:
            return ""
    return str(p.get("ParameterName", ""))


def _imported_textures(pkg: Package) -> list[ObjectRef]:
    out = [ObjectRef(pkg, -(i + 1)) for i, imp in enumerate(pkg.imports) if imp.class_name in TEXTURE_CLASSES]
    out += [ObjectRef(pkg, e.index + 1) for e in pkg.exports if e.class_name in TEXTURE_CLASSES]
    return out


_SHARED = re.compile(r"/engine/|texturelibrary|noise|grunge|/(dirt|scratch|smudge|tiling|detail|utility|util)"
                     r"[^/]*/[^/]*$", re.I)


def _shared_texture(path: str) -> bool:
    """A generic texture a master material layers over everything (noise, grunge, detail tiles, the engine's
    defaults): never one model's colour or normal map, so not taken for one by its name."""
    return bool(_SHARED.search(path or ""))


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
    pkg, e = hit
    for _ in range(12):
        out.details.append(f"{e.class_name} {e.name}")
        try:
            props = object_props(pkg, pkg.export_data(e), e)
        except FormatError as err:
            out.details.append(str(err))
            if e.class_name.startswith("MaterialInstance"):
                # the texture parameters can still be picked out: a texture reference after each one's name
                try:
                    found = guess.texture_parameters(pkg, pkg.export_data(e).data)
                except FormatError:
                    found = []
                for name, ref_t in found:
                    params.append((name, ref_t))
                    out.details.append(f"parameter {name or '(no name)'}: {ref_t.name}")
                    out.guessed = True
            leftovers += _imported_textures(pkg)
            # its parameters can't be read (no type mappings): take the textures it and the material it
            # imports (its parent, most likely) use, sorted into roles by their names
            parent = next((ObjectRef(pkg, -(i + 1)) for i, imp in enumerate(pkg.imports)
                           if imp.class_name.startswith("Material") and imp.class_name != "MaterialFunction"
                           and "Parameter" not in imp.class_name), None)
            nxt = resolve(parent) if parent is not None and e.class_name.startswith("MaterialInstance") else None
            if nxt is None:
                break
            pkg, e = nxt
            continue
        values = props.get("TextureParameterValues")
        if isinstance(values, Array4):
            try:
                for p in values.structs():
                    tex = p.get("ParameterValue")
                    if isinstance(tex, ObjectRef) and tex:
                        params.append((_param_name(p), tex))
            except FormatError:
                pass
        leftovers += _imported_textures(pkg)
        parent = props.get("Parent")
        if not e.class_name.startswith("MaterialInstance") or not isinstance(parent, ObjectRef) or not parent:
            break
        nxt = resolve(parent)
        if nxt is None:
            out.details.append(f"parent {parent.path} not found")
            break
        pkg, e = nxt
    for name, ref_t in params:  # nearest instance first: it overrides its parents
        role = role_from_param(name)
        if role in out.roles:
            continue
        if role == OTHER and _shared_texture(ref_t.path):
            continue
        tex = texture(ref_t)
        if tex is None:
            continue
        if role == OTHER:
            role = role_from_texture(tex)
            if role == OTHER or role in out.roles:
                continue
        out.roles[role] = tex
        out.details.append(f"{name}: {tex.name}")
    seen = set()
    for ref_t in leftovers:
        if all(r in out.roles for r in (ALBEDO, NORMAL, SPECULAR)):
            break
        key = ref_t.path.lower()
        if key in seen or _shared_texture(key):
            continue
        seen.add(key)
        tex = texture(ref_t)
        if tex is None:
            continue
        role = role_from_texture(tex)
        if role != OTHER and role not in out.roles:
            out.roles[role] = tex
            out.guessed = True
            out.details.append(f"{role} (from the material): {tex.name}")
    return out


__all__ = ["TEXTURE_CLASSES", "resolve", "resolve_material", "texture"]
