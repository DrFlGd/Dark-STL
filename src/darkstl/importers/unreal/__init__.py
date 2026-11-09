"""Importer for Unreal Engine 4 and 5 games, by each game's profile (see :mod:`darkstl.importers.profiles`).

The game's models are read in place from its ``.pak`` files and IoStore containers (``.utoc``/``.ucas``),
or from files extracted from them, with the Unreal readers the Borderlands importer brought (paks,
packages, skeletal and static meshes, textures, material instances, animations). What differs from one
game to the next (the project folder, the engine release, AES keys, type mappings, how models sort into
kinds and groups) is the game's profile.

A folder of an Unreal game that no profile names is listed too, as an "Unreal game" under the name the
user gives it: the app writes a profile for it into the settings folder (recognised by its executable),
sorted by the shipped defaults, with the AES key and type mappings from Settings › Game tools › Unreal games. Its models
pose and collect as Returnal's do.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...collect import RelatedFile
from ...model import CatalogEntry, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog
from .profile import UnrealProfile, for_game, get, unreal_profiles

log = logging.getLogger(__name__)


def _other_importers(key: str):
    from .. import IMPORTERS

    return [imp for imp in IMPORTERS if imp.key != key]


class UnrealImporter(Importer):
    key = "unreal"
    name = "Unreal Engine games"
    file_filter = ""  # .uasset files are offered by the Borderlands importer's filter
    generic = True  # also lists Unreal folders no importer recognises, under the name the user gives them

    def __init__(self) -> None:
        self._games: dict[str, str] = {}  # folder -> the name it was last scanned under

    @property
    def games(self) -> tuple[str, ...]:  # type: ignore[override]
        return tuple(p.name for p in unreal_profiles(self.key))

    # -- which folders are its own

    def detect_game(self, folder: Path) -> str | None:
        for p in unreal_profiles(self.key):
            try:
                if catalog.recognises(p, Path(folder)):
                    return p.name
            except OSError:
                continue
        return None

    def _claimed_elsewhere(self, folder: Path, game: str | None) -> bool:
        """Whether another importer reads ``game`` or recognises ``folder`` as its game's."""
        for imp in _other_importers(self.key):
            if game and game in imp.games:
                return True
        for imp in _other_importers(self.key):
            try:
                if imp.detect_game(Path(folder)):
                    return True
            except OSError:
                continue
        return False

    def profile_for(self, folder: Path, game: str | None) -> UnrealProfile | None:
        """The profile this importer reads ``folder`` (added as ``game``) with; None when it isn't its own."""
        prof = get(game)
        if prof is not None and prof.importer == self.key:
            return prof
        found = self.detect_game(folder)
        if found:
            return get(found)
        if not self.generic or prof is not None:
            return None
        if self._claimed_elsewhere(folder, game) or not catalog.is_unreal_folder(Path(folder)):
            return None
        return for_game(game)

    def _game_of_path(self, path: Path) -> UnrealProfile | None:
        """The profile of the game a model path belongs to (by its project folder or the folder it was found in)."""
        from ..borderlands.ue4 import vfs

        hit = vfs.locate(path)
        if hit is not None:
            gf = vfs.game_files(hit[0])
            if gf.game:
                return for_game(gf.game)
        for p in unreal_profiles(self.key):
            if catalog.is_profile_path(p, path):
                return p
        return None

    # -- the importer

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        prof = self.profile_for(folder, game)
        if prof is None:
            return []
        self._games[str(Path(folder))] = game or prof.name
        root = catalog.root_for(Path(folder), prof)
        return catalog.scan(root, game or prof.name, prof, self.key, progress, self.cache)

    def names(self, folder: Path, game: str, entries: list[CatalogEntry], fresh: bool = False) -> dict[str, str]:
        """The names the game's string tables give its meshes, where a table names one (see :mod:`.names`)."""
        prof = self.profile_for(folder, game)
        if prof is None or not entries:
            return {}
        from . import names

        return names.names(catalog.root_for(Path(folder), prof), Path(folder), prof, entries, self.cache, fresh)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        ours = [e for e in found if e.importer == self.key]
        game = ours[0].game if ours else self._games.get(str(Path(folder)))
        if not ours and found:
            return []  # another importer's models
        prof = self.profile_for(folder, game)
        if prof is None:
            return []
        return catalog.folder_notes(catalog.root_for(Path(folder), prof), game or prof.name, prof, ours)

    def can_open(self, path: Path) -> bool:
        from ..borderlands import unreal4

        if not unreal4.is_asset_path(Path(path)):
            return False
        if not self.generic:
            return any(catalog.is_profile_path(p, path) for p in unreal_profiles(self.key)) \
                and unreal4.can_open(path)
        return unreal4.can_open(path)

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        from ..borderlands import unreal4

        prof = self._game_of_path(Path(path))
        return unreal4.load(self.key, Path(path), [Path(d) for d in texture_dirs], prof.name if prof else None,
                            self.cache)

    def _folder_profile(self, folder: Path) -> UnrealProfile | None:
        return self.profile_for(folder, self.detect_game(folder))

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        from ..borderlands import unreal4

        prof = self._folder_profile(Path(folder))
        if prof is None:
            return []
        return unreal4.list_textures(catalog.root_for(Path(folder), prof), prof.name)

    def open_texture(self, folder: Path, ref: str) -> Texture:
        from ..borderlands import unreal4

        prof = self._folder_profile(Path(folder))
        name = prof.name if prof is not None else None
        return unreal4.open_texture(catalog.root_for(Path(folder), prof), ref, name)

    def describe(self, path: Path) -> list[str] | None:
        from ..borderlands import unreal4

        return unreal4.describe(Path(path)) if self.can_open(path) else None

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """What every model of the game needs besides its own files: the engine release (a collected copy can't
        read it from the game's executable) and the game's type mappings (.usmap), as its profile says."""
        prof = self.profile_for(Path(root), game)
        return shared_files(Path(root), prof) if prof is not None else []

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        from ..borderlands import unreal4

        return unreal4.related_files(Path(path), textures, animations, self.cache)

    def review_info(self, path: Path, files: list[str]) -> dict:
        from ..borderlands import unreal4

        return unreal4.review_info(Path(path), files)


def shared_files(root: Path, prof: UnrealProfile) -> list[RelatedFile]:
    """The engine release and type mappings a collected copy of an Unreal game keeps (see its profile)."""
    from ..borderlands.ue4 import unversioned, usmap

    root = catalog.root_for(Path(root), prof)
    out = []
    if prof.collect_engine:
        engine = catalog.engine_version(root, prof)
        if engine:
            rel = catalog.engine_file(root, prof)
            text = f"{engine // 100}.{engine % 100}\n".encode()
            out.append(RelatedFile(root / rel, "other", save_as=rel, extract=lambda t=text: t, size=len(text)))
    if prof.collect_usmap:
        found = usmap.find_mappings_file(root, prof.name, unversioned._settings_dir())
        if found is not None:
            out.append(RelatedFile(found, "other", save_as=mappings_file(prof)))
    return out


def mappings_file(prof: UnrealProfile) -> str:
    """Where a collected copy keeps the game's type mappings (``Returnal.usmap``)."""
    from .. import profiles

    return f"{prof.project or profiles.slug(prof.name)}.usmap"


__all__ = ["UnrealImporter", "mappings_file", "shared_files"]
