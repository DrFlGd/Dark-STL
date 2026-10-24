"""The interface every importer implements."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..collect import RelatedFile
from ..model import CatalogEntry, SourceModel

ScanProgress = Callable[[int, str], None]  # (files looked at, the file now); a count below 0: just the text


class Importer:
    """Reads one family of game files into :class:`~darkstl.model.SourceModel`.

    Subclasses set ``key`` and ``name`` and implement :meth:`can_open` and
    :meth:`load`. Scanning folders, game detection and multi-model files are
    optional.
    """

    key = ""
    name = ""
    file_filter = ""  # for open-file dialogs, e.g. "Models (*.flver *.partsbnd)"
    games: tuple[str, ...] = ()  # games this importer knows, offered when adding a folder
    cache: Path | None = None  # a folder of its own in the app's cache, for what scans find (see set_cache_folder)

    def detect_game(self, folder: Path) -> str | None:
        """Name of the game a folder of extracted files belongs to, if this importer recognises it."""
        return None

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        """Every model this importer can open under ``folder``."""
        return []

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        """Things to tell the user about a folder they added (for example that its files are still packed)."""
        return []

    def can_open(self, path: Path) -> bool:
        raise NotImplementedError

    def variants(self, path: Path) -> list[str]:
        """Names of the models inside a file that holds several (empty when it holds one)."""
        return []

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        raise NotImplementedError

    def describe(self, path: Path) -> list[str] | None:
        """Description of a file that is not a model (texture pack, archive...), or None."""
        return None

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        """The files a model needs when it is copied somewhere else: itself, and with it the textures,
        animations and low-detail copies kept beside it. Folders stand for everything in them."""
        return [RelatedFile(Path(path), "model")]

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """Files a game folder holds that every model from it may need (a compression library, the
        player's animations for armour)."""
        return []
