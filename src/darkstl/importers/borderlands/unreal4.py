"""Opening Borderlands 3, Wonderlands and Borderlands 4 models (Unreal 4/5) for the importer.

A model is a ``.uasset`` path: either a real file (extracted, or collected into
the app's library) or where the file would be inside the game folder, found
in the game's paks or IoStore containers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import Material, MeshOptions, Part, SourceModel, Texture
from . import materials as ue3_materials
from .files import GAME_BL3, GAME_BL4, GAME_WL
from .mesh import build_trimesh
from .ue4 import vfs
from .ue4.asset import Package
from .ue4.materials import resolve, resolve_material
from .ue4.mesh import Mesh4, read_skeletal_mesh, read_static_mesh

UE4_GAMES = (GAME_BL3, GAME_WL, GAME_BL4)
MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")


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
    return Texture(tex.name, f"{tex.pkg.name}", lambda: tex.decode(dirs), tex.describe)


def _materials(mesh: Mesh4, dirs) -> tuple[list[Material], dict[str, Texture], list[str]]:
    mats, textures, notes = [], {}, []
    for i, ref in enumerate(mesh.materials):
        try:
            info = resolve_material(ref)
        except FormatError as e:
            info = ue3_materials.ResolvedMaterial(ref.name if ref else "None", details=[str(e)])
        if not ref and i < len(mesh.slot_names) and mesh.slot_names[i]:
            info.name = mesh.slot_names[i]
        keys = {}
        for role, tex in info.roles.items():
            key = f"{tex.pkg.name}:{tex.name}"
            keys[role] = key
            if key not in textures:
                textures[key] = _texture(tex, dirs)
                if not tex.available():
                    notes.append(f"{tex.name}: its pixels weren't found (its .ubulk is missing)")
        mats.append(Material(info.name, keys.get(ue3_materials.ALBEDO), keys.get(ue3_materials.NORMAL),
                             keys.get(ue3_materials.HEIGHT), guessed=info.guessed, details=info.details,
                             normal_green="directx", specular=keys.get(ue3_materials.SPECULAR)))
    return mats, textures, notes


def load(importer_key: str, path: Path, dirs: list[Path], game: str | None = None) -> SourceModel:
    path = Path(path)
    pkg = open_asset(path, game)
    e = pkg.main_export(MODEL_CLASSES)
    if e is None:
        classes = sorted({x.class_name for x in pkg.exports if x.outer == 0})
        raise FormatError(f"{path.stem} isn't a mesh (it holds {', '.join(classes) or 'nothing'})")
    files = getattr(pkg, "files", None)
    game = game or (files.game if files is not None else None)
    m = read_skeletal_mesh(pkg, e) if e.class_name == "SkeletalMesh" else read_static_mesh(pkg, e)
    mats, textures, notes = _materials(m, dirs)
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

    def build(options: MeshOptions):
        if options.pose is not None:
            raise FormatError(f"{m.name} can't be posed yet")
        ranges = [(tris[s.first_index // 3:s.first_index // 3 + s.triangles], s.material, i)
                  for i, s in enumerate(m.sections) if options.parts is None or i in options.parts]
        return build_trimesh(m.positions, m.normals, uv, ranges)

    v = pkg.versions
    engine = f"Unreal {v.engine // 100}.{v.engine % 100}"
    info = [f"{e.class_name} {pkg.name} ({engine}, package version {v.ue5 or v.ue4}/{v.licensee})",
            f"{len(m.positions):,} vertices, {len(tris):,} triangles, {len(m.sections)} sections"
            + (f", {len(m.bones)} bones" if m.bones else "") + f", level of detail {m.lod} of {m.lod_count}"]
    if m.sockets:
        info.append("sockets: " + ", ".join(s.name for s in m.sockets[:12]))
    return SourceModel(name=m.name, path=path, importer=importer_key, build=build, game=game, parts=parts,
                       materials=mats, textures=textures, info=info, notes=notes)


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


def related_files(path: Path, textures: bool = True) -> list[RelatedFile]:
    path = Path(path)
    try:
        pkg = open_asset(path)
    except (FormatError, OSError):
        return [RelatedFile(path, "model")]
    gf = pkg.files
    if gf is None or not gf.exists(pkg.virtual):
        return [RelatedFile(path, "model")]
    out = _package_files(gf, pkg.virtual, "model")
    if not textures:
        return out
    seen = {pkg.virtual.lower()}
    e = pkg.main_export(MODEL_CLASSES)
    try:
        m = read_skeletal_mesh(pkg, e) if e and e.class_name == "SkeletalMesh" else read_static_mesh(pkg, e)
    except (FormatError, TypeError):
        return out
    for ref in m.materials:
        queue = [ref]
        for _ in range(16):
            if not queue:
                break
            r = queue.pop()
            hit = resolve(r)
            if hit is None:
                continue
            opkg, oe = hit
            if opkg.virtual and opkg.virtual.lower() not in seen and gf.exists(opkg.virtual):
                seen.add(opkg.virtual.lower())
                out += _package_files(gf, opkg.virtual, "texture")
            for i, imp in enumerate(opkg.imports):
                if imp.class_name in ("Texture2D", "MaterialInstanceConstant", "Material") and opkg is not pkg:
                    queue.append(type(r)(opkg, -(i + 1)))
    return out


__all__ = ["UE4_GAMES", "can_open", "describe", "is_asset_path", "load", "open_asset", "related_files"]
