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
from ...model import CatalogEntry, SourceModel
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
        return unreal4.load(self.key, Path(path), [Path(d) for d in texture_dirs], GAME)

    def describe(self, path: Path) -> list[str] | None:
        return unreal4.describe(Path(path)) if catalog.is_returnal_path(path) else None

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        return unreal4.related_files(Path(path), textures)


__all__ = ["GAME", "ReturnalImporter"]
