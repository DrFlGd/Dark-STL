"""Which textures an Unreal 4/5 material uses, and for what.

Meshes name material instances; each overrides some texture parameters of its
parent (``TextureParameterValues``: a parameter name and a texture), up to the
material itself. Roles come from the parameter names, then from the textures
(compressed as normal maps, or named with a role suffix). For the textures a
base material uses directly, the textures its package imports are taken.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

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


# A layered material's parameters name their layer: "BaseColor Texture (L1)". With the static switch
# "UseVertexColorsAsBlendSource" on, layer n shows where the vertex colour channel its "Layern_SourceChannel"
# vector picks is (Returnal's M_MonsterMaster_Layered: red, green and blue for layers 1-3 over layer 0).
_LAYER_PARAM = re.compile(r"^(.*?)\s*\(L(\d+)\)\s*$")
_LAYER_SOURCE = re.compile(r"^layer\s*(\d+)_?source_?channel$", re.I)
_LAYER_ENABLE = re.compile(r"^layer\s*(\d+)_?enable$", re.I)
_VERTEX_BLEND = re.compile(r"^use_?vertex_?colou?rs?_?as_?blend_?source$", re.I)
_CHANNELS = "RGBA"


@dataclass
class MaterialLayer:
    """A layer of a layered material: shown over the layers before it where the mesh's vertex colours,
    weighted by ``channel`` (R, G, B, A), come to half or more."""

    index: int
    channel: tuple[float, float, float, float]
    roles: dict = field(default_factory=dict)  # role -> texture
    details: list[str] = field(default_factory=list)

    def channel_name(self) -> str:
        picked = [_CHANNELS[i] for i, w in enumerate(self.channel) if w > 0]
        return "+".join(picked) or "none"


def _layer_of(name: str) -> tuple[str, int]:
    """A parameter's name without its layer, and its layer (-1 when it names none)."""
    m = _LAYER_PARAM.match(name or "")
    return (m.group(1), int(m.group(2))) if m else (name, -1)


def _linear_color(v) -> tuple[float, float, float, float] | None:
    raw = getattr(v, "raw", None)
    if isinstance(raw, (bytes, bytearray)) and len(raw) == 16:
        return struct.unpack("<4f", raw)
    if isinstance(v, Struct4):
        try:
            p = v.props()
            return tuple(float(p.get(k, 0.0)) for k in ("R", "G", "B", "A"))
        except FormatError:
            return None
    return None


def _vectors_and_switches(props, vectors: dict, switches: dict) -> None:
    """Add a material's vector parameters and static switches to those found so far (the nearest instance's
    win, so ones already there are kept)."""
    values = props.get("VectorParameterValues")
    if isinstance(values, Array4):
        try:
            for p in values.structs():
                colour = _linear_color(p.get("ParameterValue"))
                if colour is not None:
                    vectors.setdefault(_param_name(p).lower(), colour)
        except FormatError:
            pass
    static = props.get("StaticParameters")
    if isinstance(static, Struct4):
        try:
            found = static.props().get("StaticSwitchParameters")
            if isinstance(found, Array4):
                for p in found.structs():
                    value = p.get("Value")
                    if isinstance(value, bool):
                        switches.setdefault(_param_name(p).lower(), value)
        except FormatError:
            pass


def _layers(params: list[tuple[str, ObjectRef]], vectors: dict, switches: dict) -> list[MaterialLayer]:
    """The layers vertex colours pick, with their textures, when the material blends layers by them."""
    if not any(_VERTEX_BLEND.match(k) and v for k, v in switches.items()):
        return []
    enabled = {int(m.group(1)): v for k, v in switches.items() if (m := _LAYER_ENABLE.match(k))}
    sources = {int(m.group(1)): v for k, v in vectors.items() if (m := _LAYER_SOURCE.match(k))}
    by_layer: dict[int, list[tuple[str, ObjectRef]]] = {}
    for name, ref_t in params:
        base, n = _layer_of(name)
        if n >= 1:
            by_layer.setdefault(n, []).append((base, ref_t))
    out = []
    for n in sorted(by_layer):
        if enabled.get(n) is False:
            continue
        channel = sources.get(n)
        if channel is None:
            if n > 4:
                continue
            channel = tuple(1.0 if i == n - 1 else 0.0 for i in range(4))  # layer 1 red, 2 green, 3 blue, 4 alpha
        layer = MaterialLayer(n, tuple(float(x) for x in channel))
        _assign(by_layer[n], layer.roles, layer.details)
        if layer.roles:
            out.append(layer)
    return out


def _assign(params: list[tuple[str, ObjectRef]], roles: dict, details: list[str]) -> None:
    """Fill ``roles`` from texture parameters, nearest instance first (it overrides its parents)."""
    for name, ref_t in params:
        role = role_from_param(name)
        if role in roles:
            continue
        if role == OTHER and _shared_texture(ref_t.path):
            continue
        tex = texture(ref_t)
        if tex is None:
            details.append(f"{name}: {ref_t.name} couldn't be read")
            continue
        if role == OTHER:
            role = _texture_role(tex)
            if role == OTHER or role in roles:
                continue
        roles[role] = tex
        details.append(f"{name}: {tex.name}")


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
    vectors: dict[str, tuple] = {}
    switches: dict[str, bool] = {}
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
        _vectors_and_switches(props, vectors, switches)
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
    out.layers = _layers(params, vectors, switches)
    if out.layers:  # the material's own textures are its first layer's; the others' are shown on their own
        params = [(name, ref_t) for name, ref_t in params if _layer_of(name)[1] <= 0]
    _assign(params, out.roles, out.details)  # nearest instance first: it overrides its parents
    for layer in out.layers:
        out.details.append(f"layer {layer.index}, shown where the vertex colour is {layer.channel_name()}: "
                           + ", ".join(f"{role} {tex.name}" for role, tex in layer.roles.items()))
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


__all__ = ["TEXTURE_CLASSES", "MaterialLayer", "resolve", "resolve_material", "texture"]
