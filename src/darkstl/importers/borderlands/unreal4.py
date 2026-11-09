"""Opening Borderlands 3, Wonderlands and Borderlands 4 models (Unreal 4/5) for the importer.

A model is a ``.uasset`` path: either a real file (extracted, or collected into
the app's library) or where the file would be inside the game folder, found
in the game's paks or IoStore containers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import Material, MeshOptions, Part, Rig, SourceModel, Texture
from . import materials as ue3_materials
from .files import GAME_BL3, GAME_BL4, GAME_WL
from .mesh import build_trimesh
from .ue4 import vfs
from .ue4.asset import Package
from .ue4.materials import resolve, resolve_material
from .ue4.mesh import Mesh4, read_skeletal_mesh, read_static_mesh

UE4_GAMES = (GAME_BL3, GAME_WL, GAME_BL4)
MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")
MATERIAL_CLASSES = ("MaterialInstanceConstant", "Material")
RELATED_BUDGET = 1000  # materials and textures looked at for one model's files, at most


def is_asset_path(path: Path) -> bool:
    return Path(path).suffix.lower() in vfs.ASSET_SUFFIXES


def open_asset(path: Path, game: str | None = None) -> Package:
    path = Path(path)
    hit = vfs.locate(path)
    if hit is not None:
        root, virtual = hit
        gf = vfs.game_files(root, game)
        if gf.exists(virtual) or (gf.zen is not None and gf.zen.exists(virtual)):
            return gf.open_package(virtual)
    if path.is_file():
        root = _loose_root(path)
        gf = vfs.game_files(root, game)
        return gf.open_package(path.relative_to(root).as_posix())
    raise FormatError(f"{path.name} wasn't found in the game's files")


def _loose_root(path: Path) -> Path:
    """For an extracted file: the folder above its project folder (``.../OakGame/Content/...`` -> ``...``)."""
    parts = [p.lower() for p in path.parts]
    if "content" in parts:
        i = len(parts) - 1 - parts[::-1].index("content")
        if "plugins" in parts[:i]:
            i = parts.index("plugins")
        return Path(*path.parts[:max(1, i - 1)])
    return path.parent


def can_open(path: Path) -> bool:
    path = Path(path)
    if not is_asset_path(path):
        return False
    return path.is_file() or vfs.locate(path) is not None


def _texture(tex, dirs) -> Texture:
    return Texture(tex.name, f"{tex.pkg.name}", lambda: tex.decode(dirs), tex.describe, raw=tex.dds_bytes)


def _stems(files) -> dict[str, list[str]]:
    """The game's packages by their last name (lower-case): kept on the game's file list, built once."""
    index = getattr(files, "_stem_index", None)
    if index is None:
        index = {}
        names = list(getattr(files, "packages", {}))
        zen = getattr(files, "zen", None)
        if zen is not None:
            names += list(getattr(zen, "by_name", {}))
        for name in names:
            index.setdefault(name.rsplit("/", 1)[-1], []).append(name)
        try:
            files._stem_index = index
        except AttributeError:
            pass
    return index


def _named_materials(pkg: Package, mesh: Mesh4) -> dict[int, str]:
    """Fill the slots that name no material (a blueprint sets theirs as the game runs) with the material the
    slot is called after, when the game has one: Returnal's Helios has slots M_Helios_Head and M_Helios_hair
    and no material in either. Returns, per slot filled, the material's package."""
    files = getattr(pkg, "files", None)
    if files is None or not hasattr(files, "open_named"):
        return {}
    empty = [i for i, ref in enumerate(mesh.materials)
             if not ref and i < len(mesh.slot_names) and mesh.slot_names[i] and mesh.slot_names[i] != "None"]
    if not empty:
        return {}
    index = _stems(files)
    here = (pkg.name or "").lower().rsplit("/", 1)[0] + "/"
    out = {}
    for i in empty:
        slot = mesh.slot_names[i]
        names = [slot]
        if slot.lower().startswith("m_"):
            names.append("MI_" + slot[2:])
        elif slot.lower().startswith("mi_"):
            names.append("M_" + slot[3:])
        found = [name for n in names for name in index.get(n.lower(), [])]
        found.sort(key=lambda name: (-len(os.path.commonprefix([name, here])), len(name)))  # nearest the mesh
        for name in found[:8]:
            try:
                other = files.open_named(name)
            except (FormatError, OSError, ValueError):
                continue
            e = other.main_export(MATERIAL_CLASSES) if other is not None else None
            if e is not None:
                mesh.materials[i] = ue3_materials.ObjectRef(other, e.index + 1)
                out[i] = other.name
                break
    return out


def _materials(mesh: Mesh4, dirs, named: dict[int, str] | None = None, green: str = "directx"
               ) -> tuple[list[Material], dict[str, Texture], list[str], dict[int, tuple]]:
    """The mesh's materials (one per slot, then one per layer of a layered material the mesh shows), the
    textures they use, notes, and per slot of a layered material how its layers are picked: (a function giving
    each of a section's triangles its layer, -1 for the material's own, and per layer its material index)."""
    mats, textures, notes = [], {}, []
    infos = []

    def keys_of(roles) -> dict:
        keys = {}
        for role, tex in roles.items():
            key = f"{tex.pkg.name}:{tex.name}"
            keys[role] = key
            if key not in textures:
                textures[key] = _texture(tex, dirs)
                if not tex.available():
                    notes.append(f"{tex.name}: its pixels weren't found (its .ubulk is missing)")
        return keys

    def material(name, keys, guessed, details) -> Material:
        return Material(name, keys.get(ue3_materials.ALBEDO), keys.get(ue3_materials.NORMAL),
                        keys.get(ue3_materials.HEIGHT), guessed=guessed, details=details, normal_green=green,
                        specular=keys.get(ue3_materials.SPECULAR))

    for i, ref in enumerate(mesh.materials):
        try:
            info = resolve_material(ref)
        except FormatError as e:
            info = ue3_materials.ResolvedMaterial(ref.name if ref else "None", details=[str(e)])
        if not ref and i < len(mesh.slot_names) and mesh.slot_names[i]:
            info.name = mesh.slot_names[i]
        if named and i in named:
            info.details.insert(0, f"The mesh names no material for its slot {mesh.slot_names[i]}: the game's "
                                   f"material of that name is shown ({named[i]})")
            info.guessed = True
        infos.append((ref, info))
        mats.append(material(info.name, keys_of(info.roles), info.guessed, info.details))
    layers: dict[int, tuple] = {}
    tris = mesh.triangles()
    uv = mesh.uvs[0] if mesh.uvs else None
    made: dict[tuple[str, int], int] = {}  # (material, layer) -> its material index, shared by the slots using it
    for i, (ref, info) in enumerate(infos):
        if not info.layers:
            continue
        segs = [tris[s.first_index // 3:s.first_index // 3 + s.triangles] for s in mesh.sections if s.material == i]
        found = _layer_picker(info, segs, mesh.colors, uv)
        if found is None:
            continue
        pick, where = found
        used = {int(k) for seg in segs for k in np.unique(pick(seg)) if k >= 0}
        indices: list[int | None] = []
        for k, layer in enumerate(info.layers):
            if k not in used:
                indices.append(None)
                continue
            key = (getattr(ref, "path", "") or info.name, layer.index)
            if key not in made:
                made[key] = len(mats)
                details = [f"Layer {layer.index} of {info.name}: shown {where(layer)}", *layer.details]
                mats.append(material(f"{info.name} layer {layer.index}", keys_of(layer.roles), info.guessed,
                                     details))
            indices.append(made[key])
        layers[i] = (pick, indices)
    return mats, textures, notes, layers


def _layer_picker(info, segs: list[np.ndarray], colors: np.ndarray | None, uv: np.ndarray | None):
    """How a layered material's layers are picked on the mesh: by its vertex colours when the material says so,
    or else by the UV tile each triangle sits in when its triangles keep to whole tiles numbered as its layers
    (UDIM texture sets: Returnal's player suits). (pick per triangle, wording of where a layer shows), or None
    when neither fits and the material's own textures are shown everywhere."""
    if info.by_vertex_colour and colors is not None:
        channels = [np.asarray(layer.channel, np.float64) for layer in info.layers]
        return (lambda seg: _layer_picks(seg, channels, colors),
                lambda layer: f"where the mesh's vertex colour is {layer.channel_name()}")
    if uv is None or not segs:
        return None
    every = np.concatenate(segs)
    if not len(every):
        return None
    tiles = _uv_tiles(every, uv)
    wanted = [layer.index for layer in info.layers]
    inside = np.isin(tiles, wanted)
    known = inside | (tiles == 0)
    corner = uv[every] - _tile_corner(tiles)[:, None, :]
    straddle = ((corner < -0.01) | (corner > 1.01)).any(axis=(1, 2))
    if not inside.any() or known.mean() < 0.95 or straddle.mean() > 0.01:
        return None

    def pick(seg: np.ndarray) -> np.ndarray:
        t = _uv_tiles(seg, uv) if len(seg) else np.zeros(0, np.int64)
        out = np.full(len(seg), -1)
        for k, n in enumerate(wanted):
            out[t == n] = k
        return out

    def where(layer) -> str:
        u, v = layer.index % 10, layer.index // 10
        return f"on the triangles whose UVs sit in tile {layer.index} (u {u} to {u + 1}, v {v} to {v + 1})"

    return pick, where


def _uv_tiles(seg: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """The UV tile each triangle sits in, numbered as UDIM tiles from 0: u's whole part, plus ten per v."""
    c = np.floor(np.asarray(uv, np.float64)[seg].mean(axis=1)).astype(np.int64)
    return np.where((c >= 0).all(axis=1) & (c[:, 0] < 10), c[:, 0] + 10 * c[:, 1], -1)


def _tile_corner(tiles: np.ndarray) -> np.ndarray:
    t = np.maximum(tiles, 0)
    return np.stack([t % 10, t // 10], axis=1).astype(np.float64)


def _layer_picks(seg: np.ndarray, channels: list, colors: np.ndarray) -> np.ndarray:
    """Per triangle the layer its vertex colours show (an index into ``channels``), or -1 for the material's
    own: the last layer whose channel reaches half (each is blended over the ones before it)."""
    pick = np.full(len(seg), -1)
    if not len(seg):
        return pick
    c = colors[seg].astype(np.float64).mean(axis=1) / 255.0  # (T, 4): each triangle's mean colour
    for k, channel in enumerate(channels):
        pick[c @ channel >= 0.5] = k
    return pick


def _layer_ranges(seg: np.ndarray, material: int, part: int, layers: dict) -> list:
    """A section's triangles by the material layer each shows."""
    found = layers.get(material)
    if not found or not len(seg):
        return [(seg, material, part)]
    pick, indices = found
    k_of = pick(seg)
    out = [(seg[k_of == -1], material, part)]
    out += [(seg[k_of == k], index, part) for k, index in enumerate(indices) if index is not None]
    return [r for r in out if len(r[0])]


def _animations(pkg: Package, m: Mesh4, cache: Path | None):
    """The animations that can pose a skeletal mesh (None when it has no skeleton or skin weights), and why it
    can't be posed when it can't."""
    from .ue4.animset import UEAnimations

    if m.kind != "SkeletalMesh":
        return None, "It's a static mesh: it has no bones to pose."
    if m.ref_pose is None or not m.bones:
        return None, "It has no bones to pose."
    if m.joints is None:
        return None, next((n for n in m.notes if "skin weights" in n),
                          "Its skin weights couldn't be read, so it can't be posed.")
    files = getattr(pkg, "files", None)
    if m.skeleton is None or files is None:
        return None, "Its Skeleton asset isn't named, so the animations made for it can't be found."
    ref = m.skeleton.ref
    skeleton_package = pkg.import_package(ref) if ref < 0 else pkg.name
    used = np.unique(m.joints[(m.weights > 0) & (m.joints >= 0)])
    return UEAnimations(m.bones, m.ref_pose, skeleton_package, files, cache, used_bones=used), None


def _rig(anims) -> Rig:
    """The mesh's bones in the export frame, posed from its skeleton's animations."""
    from .ue4.animset import to_export

    def pose_locals(anim: str, frame: int):
        return list(anims.names_), to_export(anims.local_pose(anim, anims.clamp(anim, frame)))

    def pose_all(anim: str):
        return list(anims.names_), to_export(anims.local_poses(anim))

    return Rig(list(anims.names_), anims.parents.copy(), to_export(anims.local), to_export(anims.bind_world),
               pose_locals, pose_all)


def load(importer_key: str, path: Path, dirs: list[Path], game: str | None = None,
         cache: Path | None = None) -> SourceModel:
    path = Path(path)
    pkg = open_asset(path, game)
    e = pkg.main_export(MODEL_CLASSES)
    if e is None:
        classes = sorted({x.class_name for x in pkg.exports if x.outer == 0})
        raise FormatError(f"{path.stem} isn't a mesh (it holds {', '.join(classes) or 'nothing'})")
    files = getattr(pkg, "files", None)
    game = game or (files.game if files is not None else None)
    m = read_skeletal_mesh(pkg, e) if e.class_name == "SkeletalMesh" else read_static_mesh(pkg, e)
    named = _named_materials(pkg, m)
    mats, textures, notes, layers = _materials(m, dirs, named, _normal_green(game))
    notes = list(m.notes) + notes
    tris = m.triangles()
    uv = m.uvs[0] if m.uvs else None
    parts = []
    for i, s in enumerate(m.sections):
        name = mats[s.material].name if 0 <= s.material < len(mats) else f"material {s.material}"
        seg = tris[s.first_index // 3:s.first_index // 3 + s.triangles]
        parts.append(Part(f"Section {i} ({name})", s.material, len(np.unique(seg)) if len(seg) else 0, s.triangles))
    if not len(tris):
        notes.append(f"{m.name} has no geometry.")
    anims, why = _animations(pkg, m, cache)
    if why and why not in notes and m.kind == "SkeletalMesh":
        notes.append(why)
    skin = (m.joints, m.weights) if anims is not None else None

    def build(options: MeshOptions):
        positions, normals = m.positions, m.normals
        if options.pose is not None:
            if anims is None:
                raise FormatError(f"{m.name} has no animations to pose it with")
            from .ue4.animset import skin as skin_vertices

            name, frame = options.pose
            matrices = anims.skin_matrices(name, anims.clamp(name, frame))
            positions, normals = skin_vertices(m.positions, m.normals, m.joints, m.weights, matrices)
        ranges = [r for i, s in enumerate(m.sections) if options.parts is None or i in options.parts
                  for r in _layer_ranges(tris[s.first_index // 3:s.first_index // 3 + s.triangles], s.material, i,
                                         layers)]
        return build_trimesh(positions, normals, uv, ranges, skin=skin if options.pose is None else None)

    v = pkg.versions
    engine = f"Unreal {v.engine // 100}.{v.engine % 100}"
    info = [f"{e.class_name} {pkg.name} ({engine}, package version {v.ue5 or v.ue4}/{v.licensee})",
            f"{len(m.positions):,} vertices, {len(tris):,} triangles, {len(m.sections)} sections"
            + (f", {len(m.bones)} bones" if m.bones else "") + f", level of detail {m.lod} of {m.lod_count}"]
    if m.sockets:
        info.append("sockets: " + ", ".join(s.name for s in m.sockets[:12]))
    if m.skeleton is not None and m.kind == "SkeletalMesh":
        info.append(f"skeleton {m.skeleton.path}")
    return SourceModel(name=m.name, path=path, importer=importer_key, build=build, game=game, parts=parts,
                       materials=mats, textures=textures, info=info, notes=notes, animations=anims, pose_note=why,
                       rig=_rig(anims) if anims is not None else None)


def _normal_green(game: str | None) -> str:
    """Which way the game's normal maps' green points, as its profile says (Unreal's own: down, DirectX)."""
    from ..unreal.profile import get

    prof = get(game)
    return prof.normal_green if prof is not None else "directx"


# Unreal's naming convention for textures; the paks' indexes give no classes, so textures are found by name
TEXTURE_NAME = re.compile(r"^(t|tex|tx)_", re.I)


def list_textures(root: Path, game: str | None = None) -> list[tuple[str, str]]:
    """Every texture the game's paks (or extracted files) hold, by Unreal's naming (``T_...``): (its name, its
    path in the game)."""
    gf = vfs.game_files(root, game)
    names = gf.assets() + (gf.zen.assets() if gf.zen is not None else [])
    out: dict[str, tuple[str, str]] = {}
    for virtual in names:
        stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if TEXTURE_NAME.match(stem) and virtual.lower().endswith(".uasset"):
            out.setdefault(virtual.lower(), (stem, virtual))
    return sorted(out.values(), key=lambda t: (t[0].lower(), t[1].lower()))


def open_texture(root: Path, virtual: str, game: str | None = None) -> Texture:
    """A texture :func:`list_textures` listed."""
    from .ue4.materials import TEXTURE_CLASSES
    from .ue4.texture import read_texture

    pkg = vfs.game_files(root, game).open_package(virtual)
    e = pkg.main_export(tuple(TEXTURE_CLASSES))
    if e is None:
        raise FormatError(f"{Path(virtual).stem} isn't a texture")
    return _texture(read_texture(pkg, e), [])


def describe(path: Path) -> list[str] | None:
    if not is_asset_path(path):
        return None
    try:
        pkg = open_asset(path)
    except (FormatError, OSError):
        return None
    v = pkg.versions
    lines = [f"Unreal package {pkg.name}, engine {v.engine // 100}.{v.engine % 100}, {len(pkg.names):,} names, "
             f"{len(pkg.imports):,} imports, {len(pkg.exports):,} exports"]
    for x in pkg.exports[:40]:
        lines.append(f"  {x.class_name} {x.path}")
    return lines


def _package_files(gf, virtual: str, kind: str) -> list[RelatedFile]:
    stem = virtual[:virtual.rfind(".")]
    out = []
    for name in [virtual] + [stem + ext for ext in vfs.PAYLOAD_SUFFIXES]:
        if gf.exists(name):
            ref = gf.files[name.lower()]
            size = getattr(ref.entry, "usize", 0) if ref.container is not None else 0
            out.append(RelatedFile(gf.root / name, kind if name == virtual else kind, save_as=name,
                                   extract=(lambda n=name: gf.read(n)), size=size))
    return out


def related_files(path: Path, textures: bool = True, animations: bool = False,
                  cache: Path | None = None) -> list[RelatedFile]:
    path = Path(path)
    try:
        pkg = open_asset(path)
    except (FormatError, OSError):
        return [RelatedFile(path, "model")]
    gf = pkg.files
    if gf is None or not gf.exists(pkg.virtual):
        return [RelatedFile(path, "model")]
    out = _package_files(gf, pkg.virtual, "model")
    seen = {pkg.virtual.lower()}
    e = pkg.main_export(MODEL_CLASSES)
    try:
        m = read_skeletal_mesh(pkg, e) if e and e.class_name == "SkeletalMesh" else read_static_mesh(pkg, e)
    except (FormatError, TypeError):
        return out
    if animations:
        out += _animation_files(gf, pkg, m, cache, seen)
    if not textures:
        return out
    _named_materials(pkg, m)
    # every material and texture the materials lead to (a layered material may name dozens)
    queue = [ref for ref in m.materials if ref]
    queued = {(ref.path or "").lower() for ref in queue}
    budget = RELATED_BUDGET
    while queue and budget > 0:
        budget -= 1
        r = queue.pop()
        hit = resolve(r)
        if hit is None:
            continue
        opkg, oe = hit
        if opkg.virtual and opkg.virtual.lower() not in seen and gf.exists(opkg.virtual):
            seen.add(opkg.virtual.lower())
            out += _package_files(gf, opkg.virtual, "texture")
        if opkg is pkg:
            continue
        for i, imp in enumerate(opkg.imports):
            if imp.class_name in ("Texture2D", "VirtualTexture2D", "MaterialInstanceConstant", "Material"):
                child = type(r)(opkg, -(i + 1))
                key = (child.path or "").lower()
                if key not in queued:
                    queued.add(key)
                    queue.append(child)
    return out


def review_info(path: Path, files: list[str]) -> dict:
    """The game's paks, and which one each of ``files`` (virtual paths) is read from, with the copies of it in
    earlier paks that one replaces."""
    found = vfs.locate(path)
    if found is None:
        return {}
    gf = vfs.game_files(found[0])
    out = gf.summary()
    out["files"] = {f: gf.where(f) for f in files if gf.exists(f)}
    return out


def _animation_files(gf, pkg: Package, m: Mesh4, cache: Path | None, seen: set[str]) -> list[RelatedFile]:
    """The skeleton and the animations that pose a skeletal mesh, each package with its .uexp/.ubulk."""
    anims, _why = _animations(pkg, m, cache)
    if anims is None:
        return []
    out = []
    try:
        virtuals = anims.files_used()
    except FormatError:
        return []
    for v in virtuals:
        if v.lower() in seen or not gf.exists(v):
            continue
        seen.add(v.lower())
        out += _package_files(gf, v, "animation")
    return out


__all__ = ["UE4_GAMES", "can_open", "describe", "is_asset_path", "load", "open_asset", "related_files", "review_info"]
