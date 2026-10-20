"""The model explorer's catalogue: folders the user added and the models in them.

Each folder is scanned by every registered importer, so one library can hold
models from several games (and plain OBJ files) side by side, grouped by game,
category and sub-group.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .model import CatalogEntry


def detect_game(root: str | os.PathLike) -> str | None:
    """The game a folder of extracted files belongs to, as recognised by the first importer that knows it."""
    from .importers import IMPORTERS

    for imp in IMPORTERS:
        game = imp.detect_game(Path(root))
        if game:
            return game
    return None


def scan(root: str | os.PathLike, game: str | None = None,
         progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
    """Every model any importer finds under ``root``."""
    from .importers import IMPORTERS

    root = Path(root)
    game = game or detect_game(root) or root.name
    found: list[CatalogEntry] = []
    for imp in IMPORTERS:
        found += imp.scan(root, game, progress)
    return sorted(found, key=lambda e: e.sort_key)


@dataclass
class Library:
    roots: dict[Path, str] = field(default_factory=dict)  # folder -> game label
    entries: list[CatalogEntry] = field(default_factory=list)

    def add_root(self, root: str | os.PathLike, game: str | None = None,
                 progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
        root = Path(root)
        game = game or detect_game(root) or root.name
        self.roots[root] = game
        found = scan(root, game, progress)
        self.entries = [e for e in self.entries if not _is_under(e.path, root)] + found
        self.entries.sort(key=lambda e: e.sort_key)
        return found

    def merge(self, other: "Library") -> None:
        """Take over ``other``'s roots, replacing any entries this library already had under them."""
        for root, game in other.roots.items():
            self.roots[root] = game
            self.entries = [e for e in self.entries if not _is_under(e.path, root)]
        self.entries = sorted(self.entries + other.entries, key=lambda e: e.sort_key)

    def grouped(self, include_lod: bool = False) -> dict[str, dict[str, dict[str, list[CatalogEntry]]]]:
        tree: dict[str, dict[str, dict[str, list[CatalogEntry]]]] = {}
        for e in self.entries:
            if e.is_lod and not include_lod:
                continue
            tree.setdefault(e.game, {}).setdefault(e.category, {}).setdefault(e.group, []).append(e)
        return tree


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
