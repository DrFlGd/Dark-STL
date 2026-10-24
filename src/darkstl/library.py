"""The model explorer's catalogue: the game folders the user added and the models in them.

Each folder is scanned by every registered importer, so one library can hold
models from several games (and plain OBJ files) side by side, grouped by game,
category and sub-group. A game can have several folders (the install folder
and a folder of extracted files, say); they are listed together.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .model import CATEGORY_ORDER, LOD_SUFFIX, CatalogEntry

OTHER_MODELS = "Other models"  # the "game" for folders of plain model files


def detect_game(root: str | os.PathLike) -> str | None:
    """The game a folder of extracted files belongs to, as recognised by the first importer that knows it."""
    from .importers import IMPORTERS

    for imp in IMPORTERS:
        game = imp.detect_game(Path(root))
        if game:
            return game
    return None


def known_games() -> list[str]:
    """Every game an importer can read, for the game picker, then the catch-all."""
    from .importers import IMPORTERS

    return [g for imp in IMPORTERS for g in imp.games] + [OTHER_MODELS]


def scan(root: str | os.PathLike, game: str | None = None,
         progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
    """Every model any importer finds under ``root``."""
    return scan_folder(root, game, progress)[0]


def scan_folder(root: str | os.PathLike, game: str | None = None,
                progress: Callable[[int, str], None] | None = None) -> tuple[list[CatalogEntry], list[str]]:
    """Every model any importer finds under ``root``, and what the user should know about the folder."""
    from .importers import IMPORTERS

    root = Path(root)
    game = game or detect_game(root) or root.name
    found: list[CatalogEntry] = []
    for imp in IMPORTERS:
        found += imp.scan(root, game, progress)
    notes = [n for imp in IMPORTERS for n in imp.folder_notes(root, found)]
    if not found and not notes:
        notes.append("No models were found in this folder. Choose the game's folder (unpacked), or a folder of "
                     "extracted model files such as .partsbnd, .chrbnd, .objbnd, .flver or .obj.")
    return sorted(found, key=lambda e: e.sort_key), notes


def folder_relation(folder: str | os.PathLike, roots) -> tuple[Path | None, list[Path]]:
    """The added folder that already holds ``folder`` (if any), and the added folders inside it."""
    folder = Path(folder)
    outer = next((r for r in roots if r != folder and _is_under(folder, r)), None)
    inner = [r for r in roots if r != folder and _is_under(r, folder)]
    return outer, inner


@dataclass
class Library:
    roots: dict[Path, str] = field(default_factory=dict)  # folder -> game label
    entries: list[CatalogEntry] = field(default_factory=list)
    notes: dict[Path, list[str]] = field(default_factory=dict)  # folder -> what the scan found worth saying
    _by_id: dict[str, CatalogEntry] | None = field(default=None, repr=False)
    _full: set[tuple[str, str]] | None = field(default=None, repr=False)  # (game, stem) of every full model
    _first: dict[tuple[str, str], str] | None = field(default=None, repr=False)  # (game, stem) -> the entry shown

    def add_root(self, root: str | os.PathLike, game: str | None = None,
                 progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
        root = Path(root)
        game = game or detect_game(root) or root.name
        found, notes = scan_folder(root, game, progress)
        self.roots[root] = game
        self.notes[root] = notes
        self.entries = [e for e in self.entries if not _is_under(e.path, root)] + found
        self.entries.sort(key=lambda e: e.sort_key)
        self._by_id = self._full = self._first = None
        return found

    def remove_root(self, root: str | os.PathLike) -> None:
        root = Path(root)
        self.roots.pop(root, None)
        self.notes.pop(root, None)
        self.entries = [e for e in self.entries if not _is_under(e.path, root)]
        self._by_id = self._full = self._first = None

    def set_game(self, root: str | os.PathLike, game: str) -> None:
        """File a folder's models under another game."""
        root = Path(root)
        if root not in self.roots:
            raise KeyError(root)
        self.roots[root] = game
        self.entries = sorted((dataclasses.replace(e, game=game) if _is_under(e.path, root) else e
                               for e in self.entries), key=lambda e: e.sort_key)
        self._by_id = self._full = self._first = None

    def merge(self, other: "Library") -> None:
        """Take over ``other``'s roots, replacing any entries this library already had under them."""
        for root, game in other.roots.items():
            self.roots[root] = game
            self.notes[root] = other.notes.get(root, [])
            self.entries = [e for e in self.entries if not _is_under(e.path, root)]
        self.entries = sorted(self.entries + other.entries, key=lambda e: e.sort_key)
        self._by_id = self._full = self._first = None

    def find(self, entry_id: str) -> CatalogEntry | None:
        if self._by_id is None:
            self._by_id = {e.id: e for e in self.entries}
        return self._by_id.get(entry_id)

    def hides(self, entry: CatalogEntry, include_lod: bool = False, copies: bool = True) -> bool:
        """Whether the lists leave ``entry`` out: a low-detail copy is hidden while its full model is there
        (unless ``include_lod``), and a model held by several of a game's folders (a copy collected into
        the app's library beside the game's own folder) is listed once, from the folder added first."""
        if entry.is_lod and not include_lod:
            if self._full is None:
                self._full = {(e.game, e.stem.lower()) for e in self.entries if not e.is_lod}
            if (entry.game, LOD_SUFFIX.sub("", entry.stem.lower())) in self._full:
                return True
        if not copies:
            return False
        if self._first is None:
            order = {r: i for i, r in enumerate(self.roots)}
            best: dict[tuple[str, str], tuple[int, str]] = {}
            for e in self.entries:
                key = (e.game, e.stem.lower())
                rank = order.get(self.root_of(e), len(order))
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, e.id)
            self._first = {k: v[1] for k, v in best.items()}
        return self._first.get((entry.game, entry.stem.lower()), entry.id) != entry.id

    def root_of(self, entry: CatalogEntry) -> Path | None:
        return next((r for r in self.roots if _is_under(entry.path, r)), None)

    def games(self) -> list[str]:
        """Games in the library, in the order their folders were added."""
        return list(dict.fromkeys(self.roots.values()))

    def query(self, game: str | None = None, category: str | None = None, group: str | None = None,
              text: str = "", include_lod: bool = False) -> list[CatalogEntry]:
        """The models of a game (and category, group) whose names contain every word of ``text``."""
        words = text.lower().split()
        out = []
        for e in self.entries:
            if (game and e.game != game) or self.hides(e, include_lod) or (category and e.category != category):
                continue
            if group is not None and e.group != group:
                continue
            if words:
                hay = f"{e.label} {e.stem} {e.group} {e.category}".lower()
                if not all(w in hay for w in words):
                    continue
            out.append(e)
        return out

    def grouped(self, include_lod: bool = False) -> dict[str, dict[str, dict[str, list[CatalogEntry]]]]:
        tree: dict[str, dict[str, dict[str, list[CatalogEntry]]]] = {}
        for e in self.entries:
            if self.hides(e, include_lod):
                continue
            tree.setdefault(e.game, {}).setdefault(e.category, {}).setdefault(e.group, []).append(e)
        return tree

    def overview(self, include_lod: bool = False) -> list[dict]:
        """Each game with its folders, and its categories and groups with how many models each holds."""
        tree = self.grouped(include_lod)
        games = []
        for game in self.games() + [g for g in tree if g not in self.roots.values()]:
            cats = tree.get(game, {})
            categories = []
            for cat in sorted(cats, key=_category_index):
                groups = cats[cat]
                categories.append({"name": cat, "count": sum(len(v) for v in groups.values()),
                                   "groups": [{"name": g, "count": len(v)} for g, v in groups.items() if g]})
            folders = [{"path": str(r), "count": sum(1 for e in self.entries if _is_under(e.path, r)
                                                     and not self.hides(e, include_lod, copies=False)),
                        "notes": self.notes.get(r, []), "missing": not r.is_dir()}
                       for r, g in self.roots.items() if g == game]
            games.append({"name": game, "count": sum(c["count"] for c in categories),
                          "folders": folders, "categories": categories})
        return games


def _category_index(name: str) -> int:
    return CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
