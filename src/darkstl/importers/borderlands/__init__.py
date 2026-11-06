"""Importer for the Borderlands games.

Borderlands, Borderlands 2 and The Pre-Sequel are Unreal Engine 3 games: their
models are ``SkeletalMesh`` and ``StaticMesh`` objects inside ``.upk``
packages (LZO compressed on PC), their textures ``Texture2D`` objects whose
large mips may sit in ``.tfc`` texture caches. Everything is read straight
from the game's ``WillowGame/CookedPC(Console)`` folder; nothing has to be
extracted first.

Borderlands 2 and The Pre-Sequel build guns from parts on one shared "gestalt"
mesh per weapon type; the importer lists that mesh's fragments as its parts and
offers each weapon type as a configuration (body, grip, barrel, sight, stock,
elemental, accessory) for the app's configuration menu.

The package and mesh layouts follow UE Viewer (gildor2/UEViewer, MIT licence).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, files, materials, unreal4, weapons
from .files import GAME_BL1, GAME_BL2, GAME_BL3, GAME_BL4, GAME_TPS, GAME_WL
from .formats import upk
from .formats.props import ObjectRef
from .formats.skeletal import read_skeletal_mesh
from .formats.static import read_static_mesh
from .formats.texture import Texture2D
from .mesh import build_trimesh

GAMES = (GAME_BL1, GAME_BL2, GAME_TPS, GAME_BL3, GAME_WL, GAME_BL4)


def _texture(tex: Texture2D, dirs: list[Path]) -> Texture:
    return Texture(tex.name, f"{tex.pkg.path.name}: {tex.path}", lambda: tex.decode(dirs), tex.describe)


def _open(path: Path) -> tuple[upk.Package, upk.Export, str]:
    package, obj_path, cls = files.split_member(path)
    pkg = upk.open_package(package)
    e = pkg.find_export(obj_path, cls)
    if e is None:
        raise FormatError(f"{obj_path} isn't in {package.name} any more")
    return pkg, e, cls


def _materials(refs: list[ObjectRef], dirs: list[Path]) -> tuple[list[Material], dict[str, Texture], list[str]]:
    mats, textures, notes = [], {}, []
    for ref in refs:
        try:
            info = materials.resolve_material(ref)
        except FormatError as e:
            info = materials.ResolvedMaterial(ref.name if ref else "None", details=[str(e)])
        keys = {}
        for role, tex in info.roles.items():
            key = f"{tex.pkg.stem}:{tex.path}"
            keys[role] = key
            if key not in textures:
                textures[key] = _texture(tex, dirs)
                if not tex.available(dirs):
                    notes.append(f"{tex.name}: its pixels are in {tex.cache_name}.tfc, which wasn't found near "
                                 f"{tex.pkg.path.name}")
        mats.append(Material(info.name, keys.get(materials.ALBEDO), keys.get(materials.NORMAL),
                             keys.get(materials.HEIGHT), guessed=info.guessed, details=info.details,
                             normal_green="directx", specular=keys.get(materials.SPECULAR)))
    return mats, textures, notes


class BorderlandsImporter(Importer):
    key = "borderlands"
    name = "Borderlands (Unreal Engine)"
    file_filter = "Borderlands packages (*.upk *.u *.uasset)"
    games = GAMES

    def detect_game(self, folder: Path) -> str | None:
        return files.detect_game(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        if game in unreal4.UE4_GAMES or (game not in files.UE3_GAMES and files.is_unreal4_folder(folder)):
            from .ue4 import catalog4
            return catalog4.scan(folder, game, progress)
        return catalog.scan(folder, game, progress, self.cache)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        ours = [e for e in found if e.importer == self.key]
        game = ours[0].game if ours else files.detect_game(folder)
        if game in unreal4.UE4_GAMES or (not ours and files.is_unreal4_folder(folder)):
            from .ue4 import catalog4
            return catalog4.folder_notes(folder, game, ours)
        return catalog.folder_notes(folder, found)

    def can_open(self, path: Path) -> bool:
        return files.is_member(path) or unreal4.can_open(path)

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        if unreal4.is_asset_path(path):
            return unreal4.load(self.key, path, [Path(d) for d in texture_dirs])
        pkg, e, cls = _open(path)
        dirs = [Path(d) for d in texture_dirs]
        game = files.detect_game(files.game_root(pkg.path)) or None
        if cls == "SkeletalMesh":
            return self._load_skeletal(path, pkg, e, dirs, game)
        return self._load_static(path, pkg, e, dirs, game)

    def _load_skeletal(self, path, pkg, e, dirs, game) -> SourceModel:
        m = read_skeletal_mesh(pkg, e)
        mats, textures, notes = _materials(m.materials, dirs)
        notes = list(m.notes) + notes
        tris = m.triangles()
        uv = m.uvs[0] if m.uvs else None
        gestalt = weapons.gestalt_for_mesh(pkg, e)
        pieces: list[tuple[str, int, int, int]] = []  # name, material, first index, triangles

        def section_material(first: int) -> int:
            for s in m.sections:
                if s.first_index <= first < s.first_index + 3 * s.triangles:
                    return s.material
            return 0

        if gestalt is not None:
            for f in gestalt.fragments:
                pieces.append((f.name, section_material(f.first_index), f.first_index, f.triangles))
            notes.append(f"This is the shared {gestalt.kind} mesh: every part of every manufacturer is in it. "
                         "Pick parts in the parts list, or build a gun in the configuration menu.")
        else:
            for i, s in enumerate(m.sections):
                pieces.append((f"Section {i}" + (f" ({mats[s.material].name})" if s.material < len(mats) else ""),
                               s.material, s.first_index, s.triangles))
        parts = []
        for name, mat, first, count in pieces:
            used = np.unique(tris[first // 3:first // 3 + count]) if count else np.zeros(0, int)
            parts.append(Part(name, mat, len(used), count))
        if m.lod_count == 0 or len(tris) == 0:
            notes.append(f"{m.name} has no geometry.")

        def build(options: MeshOptions):
            if options.pose is not None:
                raise FormatError(f"{m.name} can't be posed yet")
            ranges = []
            for i, (_n, mat, first, count) in enumerate(pieces):
                if options.parts is not None and i not in options.parts:
                    continue
                ranges.append((tris[first // 3:first // 3 + count], mat, i))
            return build_trimesh(m.positions, m.normals, uv, ranges)

        info = [f"SkeletalMesh in {pkg.path.name} (Unreal 3, version {pkg.summary.version}/{pkg.summary.licensee})",
                f"{len(m.positions):,} vertices, {len(tris):,} triangles, {len(m.sections)} sections, "
                f"{len(m.bones)} bones, {m.lod_count} levels of detail", f"object {e.path}"]
        if gestalt is not None:
            info.append(f"gestalt definition {gestalt.export.path}: {len(gestalt.fragments)} fragments")
        return SourceModel(name=m.name, path=path, importer=self.key, build=build, game=game, parts=parts,
                           materials=mats, textures=textures, info=info, notes=notes)

    def _load_static(self, path, pkg, e, dirs, game) -> SourceModel:
        m = read_static_mesh(pkg, e)
        mats, textures, notes = _materials([s.material for s in m.sections], dirs)
        notes = list(m.notes) + notes
        tris = m.indices.reshape(-1, 3)
        uv = m.uvs[0] if m.uvs else None
        parts = [Part(f"Section {i} ({mats[i].name})", i, 0, s.triangles) for i, s in enumerate(m.sections)]
        if not len(tris):
            notes.append(f"{m.name} has no geometry.")

        def build(options: MeshOptions):
            if options.pose is not None:
                raise FormatError(f"{m.name} has no animations to pose it with")
            ranges = [(tris[s.first_index // 3:s.first_index // 3 + s.triangles], i, i)
                      for i, s in enumerate(m.sections) if options.parts is None or i in options.parts]
            return build_trimesh(m.positions, m.normals, uv, ranges)

        info = [f"StaticMesh in {pkg.path.name} (Unreal 3, version {pkg.summary.version}/{pkg.summary.licensee})",
                f"{len(m.positions):,} vertices, {len(tris):,} triangles, {len(m.sections)} sections, "
                f"{m.lod_count} levels of detail", f"object {e.path}"]
        return SourceModel(name=m.name, path=path, importer=self.key, build=build, game=game, parts=parts,
                           materials=mats, textures=textures, info=info, notes=notes)

    def describe(self, path: Path) -> list[str] | None:
        path = Path(path)
        if unreal4.is_asset_path(path):
            return unreal4.describe(path)
        if not upk.is_package_file(path):
            return None
        pkg = upk.open_package(path)
        s = pkg.summary
        lines = [f"Unreal package version {s.version}/{s.licensee}, {len(pkg.names):,} names, "
                 f"{len(pkg.imports):,} imports, {len(pkg.exports):,} exports"
                 + (f", {'LZO' if s.compression & 2 else 'zlib'} compressed" if s.compression else "")]
        counts: dict[str, int] = {}
        for e in pkg.exports:
            counts[e.class_name] = counts.get(e.class_name, 0) + 1
        for cls, n in sorted(counts.items(), key=lambda kv: -kv[1])[:40]:
            lines.append(f"  {cls}: {n:,}")
        return lines

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        path = Path(path)
        if unreal4.is_asset_path(path):
            return unreal4.related_files(path, textures)
        if not files.is_member(path):
            return [RelatedFile(path, "model")]
        package, _p, _c = files.split_member(path)
        out = [RelatedFile(package, "model")]
        if textures:
            seen = {package}
            for tfc in sorted(package.parent.glob("*.tfc")):
                if tfc not in seen:
                    out.append(RelatedFile(tfc, "texture"))
        return out

    def configurations(self, folder: Path, game: str, entries: list[CatalogEntry]) -> list:
        """Each gun type as a configuration of its gestalt mesh's parts (Borderlands 2, The Pre-Sequel)."""
        if game in unreal4.UE4_GAMES:
            from .ue4.guns import gun_configurations
            return gun_configurations(entries, game)
        from .configurations import build_configurations

        return build_configurations(Path(folder), game, entries, self.cache)


__all__ = ["BorderlandsImporter", "GAMES"]
