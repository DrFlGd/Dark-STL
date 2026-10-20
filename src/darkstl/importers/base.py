"""The interface every importer implements."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..model import CatalogEntry, SourceModel

ScanProgress = Callable[[int, str], None]


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
