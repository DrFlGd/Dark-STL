"""Which textures a Cyberpunk material uses, and for what.

A chunk's material is a ``CMaterialInstance``: a list of parameter values
over a base material, itself an instance (``.mi``) or a template (``.mt``,
the shader). Walking the chain and letting each instance override its base
gives the full set of parameters, among them the textures: ``BaseColor``
(or ``DiffuseTexture``), ``Normal`` (``NormalTexture``, ``GlobalNormal`` for
multilayered materials), ``Roughness``, ``Metalness``, detail normals. The
many-layered skin and metal materials (``MultilayerSetup`` + ``MultilayerMask``)
blend several tiled layers at run time; :mod:`.multilayer` blends them here
into one colour map and one normal map (with the global normal map added).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import multilayer
from .depot import Source, Store, TextureRef
from .formats import archive as arc
from .formats.cr2w import Red, Ref
from .formats.mesh import RedMesh

ALBEDO = ("BaseColor", "DiffuseTexture", "Diffuse", "Albedo", "ColorTexture", "BaseColorTexture", "BaseColor_tex")
NORMAL = ("Normal", "NormalTexture", "GlobalNormal", "NormalMap", "Normals")
ROUGHNESS = ("Roughness", "RoughnessTexture", "RoughnessMetalness", "Metalness", "MetalnessTexture")
HEIGHT = ("Height", "HeightTexture", "Displacement", "ParalaxTexture", "ParallaxTexture")
DETAIL_NORMAL = ("DetailNormal", "NormalDetail", "DetailNormalTexture", "MicroNormal")
DETAIL_SCALE = ("DetailNormalTile", "DetailNormalScale", "NormalDetailTile", "DetailTile", "MicroNormalTile")
PLACEHOLDERS = ("base\\materials\\placeholder\\",)


@dataclass
class MaterialInfo:
    name: str
    template: str = ""
    albedo: str | None = None
    normal: str | None = None
    height: str | None = None
    specular: str | None = None
    detail_normal: str | None = None
    detail_scale: tuple[float, float] = (1.0, 1.0)
    guessed: bool = False
    slots: list[tuple[str, str]] = field(default_factory=list)  # (parameter, depot path) of every texture
    multilayer: bool = False
    missing: list[str] = field(default_factory=list)  # depot paths that were not found


def _values(inst: Red) -> dict:
    out: dict = {}
    for name, value in inst.get("values") or []:
        out.setdefault(str(name), value)
    return out


def _chain(start: Red | None, start_path: str | None, store: Store, depth: int = 8) -> tuple[dict, str, list[str]]:
    """Parameters of an instance with those of its bases filled in beneath; the template's name; missing files."""
    values: dict = {}
    template = ""
    missing: list[str] = []
    seen: set[str] = set()
    inst = start
    path = start_path
    while depth > 0:
        depth -= 1
        if inst is None:
            if path is None:
                break
            norm = arc.normalize_path(path)
            if norm in seen:
                break
            seen.add(norm)
            f = store.read(norm)
            if f is None or not f.chunks:
                missing.append(norm)
                break
            inst = f.root
        if inst.cls == "CMaterialTemplate":
            template = str(inst.get("name", "")) or (path.rsplit("\\", 1)[-1] if path else "")
            if path:
                template = path.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
            break
        for k, v in _values(inst).items():
            values.setdefault(k, v)
        base = inst.get("baseMaterial")
        inst = None
        path = base.path if isinstance(base, Ref) else None
        if path is None:
            break
    return values, template, missing


def _tex(values: dict, names: tuple[str, ...]) -> tuple[str, str] | None:
    for n in names:
        v = values.get(n)
        if isinstance(v, Ref) and v.path:
            p = arc.normalize_path(v.path)
            if p.endswith(".xbm") and not p.startswith(PLACEHOLDERS):
                return n, p
    return None


def _blend(info: MaterialInfo, values: dict, store: Store, textures: dict, blends: dict) -> None:
    """Give a multilayered material its blended colour and normal maps."""
    setup = values.get("MultilayerSetup")
    if not (isinstance(setup, Ref) and setup.path):
        return
    mask = values.get("MultilayerMask")
    glob = _tex(values, ("GlobalNormal",))
    scale = values.get("GlobalNormalUVScale")
    uv = (1.0, 1.0)
    if isinstance(scale, Red):
        x, y = scale.vec("X", "Y", default=1.0)
        uv = (x or 1.0, y or 1.0)
    key = (arc.normalize_path(setup.path), arc.normalize_path(mask.path) if isinstance(mask, Ref) else None,
           glob[1] if glob else None, uv)
    blend = blends.get(key)
    if blend is None:
        ms = multilayer.read_setup(store, key[0], key[1])
        if not ms.layers:
            info.missing += ms.missing
            return
        blend = blends[key] = multilayer.Blend(store, ms, key[2], uv)
        if ms.mask and store.locate(ms.mask) is None:
            blend.notes.append(f"{ms.mask} was not found; the layers are blended by their opacity alone")
            ms.mask = None
    where = store.describe_location(key[0])
    color_key, normal_key = key[0] + "#color", key[0] + "#normal"
    textures[color_key] = multilayer.BlendRef(key[0] + " (blended colour)", where, blend.albedo,
                                              lambda b=blend: b.describe("colour"))
    textures[normal_key] = multilayer.BlendRef(key[0] + " (blended normal)", where, blend.normal,
                                               lambda b=blend: b.describe("normal"))
    info.albedo, info.normal = color_key, normal_key
    info.slots.append(("MultilayerSetup layers", f"{len(blend.setup.layers)} templates"))


def resolve(m: RedMesh, source: Source, store: Store, appearance: str | None) \
        -> tuple[list[MaterialInfo], dict[str, TextureRef], list[str]]:
    """Material descriptions for the mesh (one per material name), the textures they need, and notes."""
    m.assign_appearance(appearance)
    textures: dict[str, TextureRef] = {}
    infos: list[MaterialInfo] = []
    notes: list[str] = []
    blended: list[str] = []
    missing_all: list[str] = []
    blends: dict[tuple, multilayer.Blend] = {}

    def texture_key(depot: str) -> str | None:
        key = depot
        if key not in textures:
            ref = store.texture_ref(depot)
            if ref is None:
                return None
            textures[key] = ref
        return key

    by_name = {name: (index, local) for name, index, local in m.material_entries}
    for name in m.material_names:
        info = MaterialInfo(name)
        index, local = by_name.get(name, (0, True))
        inst: Red | None = None
        path: str | None = None
        if local:
            inst = m.local_materials[index] if 0 <= index < len(m.local_materials) else None
        else:
            ref = m.external_materials[index] if 0 <= index < len(m.external_materials) else None
            path = ref.path if isinstance(ref, Ref) else None
        values, template, missing = _chain(inst, path, store)
        info.template = template
        info.missing = missing
        missing_all += missing
        for n, v in values.items():
            if isinstance(v, Ref) and v.path.lower().endswith((".xbm", ".mlmask", ".mlsetup")):
                info.slots.append((n, arc.normalize_path(v.path)))
        if "MultilayerSetup" in values or "MultilayerMask" in values:
            info.multilayer = True
        for role, names in (("albedo", ALBEDO), ("normal", NORMAL), ("height", HEIGHT), ("specular", ROUGHNESS),
                            ("detail_normal", DETAIL_NORMAL)):
            hit = _tex(values, names)
            if hit is None:
                continue
            key = texture_key(hit[1])
            if key is None:
                info.missing.append(hit[1])
                missing_all.append(hit[1])
                continue
            setattr(info, role, key)
        for n in DETAIL_SCALE:
            v = values.get(n)
            if isinstance(v, (int, float)) and v > 0:
                info.detail_scale = (float(v), float(v))
                break
            if isinstance(v, Red):
                x, y = v.vec("X", "Y", default=1.0)
                if x > 0 and y > 0:
                    info.detail_scale = (x, y)
                    break
        if info.multilayer:
            _blend(info, values, store, textures, blends)
            if info.albedo and info.albedo.endswith("#color"):
                blended.append(name)
        infos.append(info)
    if not infos:
        infos.append(MaterialInfo("default"))
    if blended:
        notes.append("Multilayered materials (" + ", ".join(sorted(set(blended))[:4]) +
                     ("..." if len(set(blended)) > 4 else "") + ") are blended here from their layers into one "
                     "colour map and one normal map, as the game draws them; the fine microblend grain is left out.")
    for b in blends.values():
        notes += b.notes[:2]
    missing_all += [m for b in blends.values() for m in b.setup.missing]
    unique_missing = sorted(set(missing_all))
    if unique_missing:
        where = "the game's archives" if store.archive_paths else "a folder of extracted files or the game's folder"
        notes.append(f"{len(unique_missing)} file{'s' if len(unique_missing) != 1 else ''} this model names "
                     f"could not be found (for example {unique_missing[0]}). Add {where} as a texture folder in "
                     "Settings, or export them with WolvenKit beside the model.")
    return infos, textures, notes


__all__ = ["MaterialInfo", "resolve"]
