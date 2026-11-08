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
from ..materials import (ALBEDO, NORMAL, OTHER, SPECULAR, ResolvedMaterial, role_from_name, role_from_param,
                         role_from_texture)
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
            out.details.append(f"{name}: {ref_t.name} couldn't be read")
            continue
        if role == OTHER:
            role = _texture_role(tex)
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
        role = _texture_role(tex)
        if role != OTHER and role not in out.roles:
            out.roles[role] = tex
            out.guessed = True
            out.details.append(f"{role} (from the material): {tex.name}")
    if out.roles and not all(r in out.roles for r in (ALBEDO, NORMAL)):
        _siblings(pkg, out)
    return out


_SUFFIX = re.compile(r"_[A-Za-z]+\d*$")

# Texture names particular to one game's studio, read before the usual suffixes. Returnal (Housemarque) keeps
# its normal maps as ``_Comp``: the normal's X and Y in red and green, a mask in blue (Displace only reads red
# and green, so the mask doesn't disturb it).
_GAME_NAMES = {
    "Returnal": [(re.compile(r"_comp\d*$", re.I), NORMAL)],
}


def _name_role(name: str, game: str | None) -> str:
    for rx, role in _GAME_NAMES.get(game or "", ()):
        if rx.search(name or ""):
            return role
    return role_from_name(name)


def _texture_role(tex) -> str:
    """A texture's role from what it is (a normal map's compression) and its name, the game's own names first."""
    game = getattr(getattr(tex.pkg, "files", None), "game", None)
    for rx, role in _GAME_NAMES.get(game or "", ()):
        if rx.search(tex.name or ""):
            return role
    return role_from_texture(tex)


def _folder_index(files) -> dict[str, list[str]]:
    """The game's packages by folder (lower-case): kept on the game's file list, built once."""
    index = getattr(files, "_texture_folders", None)
    if index is None:
        index = {}
        for name in files.packages:
            folder, _, leaf = name.rpartition("/")
            index.setdefault(folder, []).append(leaf)
        try:
            files._texture_folders = index
        except AttributeError:
            pass
    return index


def _siblings(pkg: Package, out: ResolvedMaterial) -> None:
    """Fill a missing colour or normal map from the textures beside the ones found, by their shared name:
    ``T_Gun_BaseColor`` found, ``T_Gun_Normal`` in the same folder. Games whose materials take their maps
    through layers or functions the material instance doesn't name still keep them side by side."""
    files = getattr(pkg, "files", None)
    if files is None or not hasattr(files, "packages"):
        return
    index = _folder_index(files)
    game = getattr(files, "game", None)
    for have in list(out.roles.values()):
        if all(r in out.roles for r in (ALBEDO, NORMAL)):
            return
        name = getattr(have.pkg, "name", "") or ""
        folder, _, leaf = name.lower().rpartition("/")
        if not leaf or _name_role(leaf, game) == OTHER:
            continue
        stem = _SUFFIX.sub("", leaf)
        candidates = sorted((x for x in index.get(folder, []) if x != leaf and x.startswith(stem + "_")), key=len)
        for x in candidates:
            role = _name_role(x, game)
            if role not in (ALBEDO, NORMAL) or role in out.roles:
                continue
            try:
                other = files.open_named(f"{folder}/{x}")
            except (FormatError, OSError):
                other = None
            e = other.main_export(tuple(TEXTURE_CLASSES)) if other is not None else None
            if e is None:
                continue
            try:
                tex = read_texture(other, e)
            except (FormatError, OSError):
                out.details.append(f"{role} (beside {have.name}): {e.name} couldn't be read")
                continue
            out.roles[role] = tex
            out.guessed = True
            out.details.append(f"{role} (beside {have.name}): {tex.name}")


__all__ = ["TEXTURE_CLASSES", "resolve", "resolve_material", "texture"]
