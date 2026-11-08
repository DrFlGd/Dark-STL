"""Importer for Returnal (Housemarque, Unreal Engine 4).

The PC version keeps its models in ``.pak`` files under
``Returnal/Content/Paks``. They are read in place with the Unreal 4 readers the
Borderlands importer brought (paks, packages, skeletal and static meshes,
textures, material instances): only finding the game, listing its models and
sorting them into groups is Returnal's own.

Encrypted paks need the game's AES key, which Dark STL doesn't ship: it goes in
Settings under *Unreal games* (or ``unreal-keys.txt`` in the game's folder).
"""

from __future__ import annotations

from pathlib import Path

from ...collect import RelatedFile
from ...model import CatalogEntry, SourceModel, Texture
from ..base import Importer, ScanProgress
from ..borderlands import unreal4
from . import catalog
from .catalog import GAME


class ReturnalImporter(Importer):
    key = "returnal"
    name = "Returnal (Unreal Engine 4)"
    file_filter = ""  # .uasset files are offered by the Borderlands importer's filter
    games = (GAME,)

    def detect_game(self, folder: Path) -> str | None:
        return catalog.detect_game(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        if game != GAME and catalog.detect_game(folder) != GAME:
            return []
        return catalog.scan(Path(folder), progress, self.cache)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        ours = [e for e in found if e.importer == self.key]
        if not ours and catalog.detect_game(folder) != GAME:
            return []
        return catalog.folder_notes(Path(folder), ours)

    def can_open(self, path: Path) -> bool:
        return catalog.is_returnal_path(path) and unreal4.can_open(path)

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        return unreal4.load(self.key, Path(path), [Path(d) for d in texture_dirs], GAME, self.cache)

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        if catalog.detect_game(folder) != GAME:
            return []
        return unreal4.list_textures(catalog.root_for(Path(folder)), GAME)

    def open_texture(self, folder: Path, ref: str) -> Texture:
        return unreal4.open_texture(catalog.root_for(Path(folder)), ref, GAME)

    def describe(self, path: Path) -> list[str] | None:
        return unreal4.describe(Path(path)) if catalog.is_returnal_path(path) else None

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """What every Returnal model needs besides its own files: the game's type mappings (.usmap) and the
        engine release, which a collected copy can't read from the game's executable."""
        from ..borderlands.ue4 import unversioned, usmap

        root = catalog.root_for(Path(root))
        engine = catalog.engine_version(root)
        out = [RelatedFile(root / catalog.ENGINE_FILE, "other", save_as=catalog.ENGINE_FILE,
                           extract=lambda: f"{engine // 100}.{engine % 100}\n".encode(), size=5)]
        found = usmap.find_mappings_file(root, GAME, unversioned._settings_dir())
        if found is not None:
            out.append(RelatedFile(found, "other", save_as=catalog.MAPPINGS_FILE))
        return out

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        return unreal4.related_files(Path(path), textures, animations, self.cache)

    def review_info(self, path: Path, files: list[str]) -> dict:
        return unreal4.review_info(Path(path), files)


__all__ = ["GAME", "ReturnalImporter"]
