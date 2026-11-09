"""The model explorer's catalogue: the game folders the user added and the models in them.

Each folder is scanned by every registered importer, so one library can hold
models from several games (and plain OBJ files) side by side, grouped by game,
category and sub-group. A game can have several folders (the install folder
and a folder of extracted files, say); they are listed together.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
import os
import pickle
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import listing
from .listing import Fingerprint
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
    found, notes, _kept = scan_folder_kept(root, game, progress)
    return found, notes


@dataclass
class Kept:
    """What a scan leaves besides its models: the folder's fingerprint (to tell at the next start whether
    it changed) and what each importer readied for opening models (:meth:`Importer.scan_state`)."""
    fingerprint: Fingerprint | None = None
    state: dict = field(default_factory=dict)  # importer key -> its state
    names: dict = field(default_factory=dict)  # overlay key -> the model's name in the game (Importer.names)

    def restore(self, root: Path) -> None:
        from .importers import IMPORTERS

        for imp in IMPORTERS:
            if imp.key in self.state:
                try:
                    imp.restore_state(Path(root), self.state[imp.key])
                except Exception:  # noqa: BLE001 - that importer's models just open as they did before 0.18
                    pass


def scan_folder_kept(root: str | os.PathLike, game: str | None = None,
                     progress: Callable[[int, str], None] | None = None) \
        -> tuple[list[CatalogEntry], list[str], Kept]:
    """:func:`scan_folder`, and what the scan leaves to keep (:class:`Kept`). The folder is listed once for
    every importer."""
    from .importers import IMPORTERS

    root = Path(root)
    game = game or detect_game(root) or root.name
    found: list[CatalogEntry] = []
    with listing.one_listing():
        listed = listing.listing(root)
        for imp in IMPORTERS:
            found += imp.scan(root, game, progress)
        notes = [n for imp in IMPORTERS for n in imp.folder_notes(root, found)]
        kept = Kept(listed.fingerprint())
        kept.names = folder_names(root, game, found)
        for imp in IMPORTERS:
            try:
                state = imp.scan_state(root)
            except Exception:  # noqa: BLE001 - nothing kept: the folder is scanned again next start
                kept.fingerprint = None
                continue
            if state is not None:
                kept.state[imp.key] = state
    if not found and not notes:
        notes.append("No models were found in this folder. Choose the game's folder (unpacked), or a folder of "
                     "extracted model files such as .partsbnd, .chrbnd, .objbnd, .flver or .obj.")
    return sorted(found, key=lambda e: e.sort_key), notes, kept


def folder_names(root: Path, game: str, found: list[CatalogEntry], fresh: bool = False) -> dict[str, str]:
    """The names the game gives the models a scan of ``root`` found (each importer's :meth:`Importer.names`):
    {overlay key: name}. ``fresh``: read the game's files again."""
    from .importers import IMPORTERS

    out: dict[str, str] = {}
    for imp in IMPORTERS:
        mine = [e for e in found if e.importer == imp.key]
        if not mine:
            continue
        try:
            out.update(imp.names(Path(root), game, mine, fresh))
        except Exception:  # noqa: BLE001 - the models keep the names their files give them
            logging.getLogger(__name__).warning("Names of %s from %s failed", root, imp.key, exc_info=True)
    return out


def folder_configurations(root: Path, game: str, found: list[CatalogEntry]) -> list:
    """The configurable models a scanned folder holds: what each importer works out from its files,
    and the configuration lists kept in the folder."""
    from . import configs
    from .importers import IMPORTERS

    out = []
    for imp in IMPORTERS:
        try:
            out += imp.configurations(Path(root), game, [e for e in found if e.importer == imp.key])
        except Exception:  # a guess that fails leaves the models as they are
            logging.getLogger(__name__).warning("Configurations of %s from %s failed", root, imp.key, exc_info=True)
    listed, _problems = configs.in_folder(Path(root))
    for c in listed:
        c.game = c.game or game
    return out + [c for c in listed if c.game == game]


# ---------------------------------------------------------------------------
# What each folder's last scan found, kept on disk, so the app opens with its library listed straight away
# (the folders are scanned again behind it)


SNAPSHOT_VERSION = 2  # 2: with the folder's fingerprint and the importers' state (Kept)


@dataclass
class Snapshot:
    root: Path
    game: str
    found: list[CatalogEntry]
    notes: list[str]
    configs: list
    version: str = ""
    kept: Kept | None = None

    def unchanged(self) -> bool:
        """Whether the folder is as it was when scanned, so its listing (and the importers' state) can be
        used as they are, without scanning it again."""
        return self.kept is not None and self.kept.fingerprint is not None and self.kept.fingerprint.unchanged()

    @staticmethod
    def file(folder: Path, root: Path) -> Path:
        key = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()[:16]
        return Path(folder) / f"{key}.pickle"

    def save(self, folder: Path) -> None:
        target = self.file(folder, self.root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{threading.get_ident()}.tmp")
            with open(tmp, "wb") as f:
                pickle.dump((SNAPSHOT_VERSION, self), f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, target)
        except (OSError, pickle.PicklingError, TypeError, AttributeError):
            pass  # only a quicker start lost

    @classmethod
    def load(cls, folder: Path, root: Path, game: str | None, version: str) -> "Snapshot | None":
        """The snapshot of ``root`` (written by this version of Dark STL, for the same game), or None."""
        try:
            with open(cls.file(folder, root), "rb") as f:
                kind, snap = pickle.load(f)  # our own cache file, written by save()
        except Exception:  # noqa: BLE001 - missing, cut short or from an older version: scanned instead
            return None
        if (kind != SNAPSHOT_VERSION or not isinstance(snap, cls) or snap.version != version
                or Path(snap.root) != Path(root) or (game and snap.game != game)):
            return None
        return snap


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
    _root: dict[str, Path | None] | None = field(default=None, repr=False)  # entry id -> the folder holding it
    _by_path: dict[str, list[CatalogEntry]] | None = field(default=None, repr=False)  # file -> its entries
    found_configs: dict[Path, list] = field(default_factory=dict)  # folder -> configurations its scan found
    kept: dict[Path, Kept] = field(default_factory=dict)  # folder -> what its last scan here left (scan_folder_kept)
    config_notes: dict[str, list[str]] = field(default_factory=dict)  # game -> what couldn't be found for them
    _configs: list[CatalogEntry] | None = field(default=None, repr=False)
    _all: list[CatalogEntry] | None = field(default=None, repr=False)
    _overview: dict[bool, list[dict]] = field(default_factory=dict, repr=False)  # include_lod -> overview()
    # files a model under the user's own name and place (see darkstl.overlay): called with each model, it may
    # change its label, category and group, which original_place() keeps as the game had them
    arrange: Callable[["Library", CatalogEntry], None] | None = field(default=None, repr=False, compare=False)
    # gives each model (and each configurable model) the user's tags, star and collections (see
    # darkstl.library_collections), which the search looks through
    mark: Callable[["Library", CatalogEntry], None] | None = field(default=None, repr=False, compare=False)
    rev: int = field(default=0, repr=False, compare=False)  # counts the changes, for what's worked out from them
    _games: dict[str, list[CatalogEntry]] | None = field(default=None, repr=False)  # game -> its entries

    def _changed(self) -> None:
        self.rev += 1
        self._by_id = self._full = self._first = self._configs = self._all = self._root = self._by_path = None
        self._games = None
        self._overview = {}
        if self.arrange is not None and self.entries:
            for e in self.entries:
                self.arrange(self, e)
            self.entries.sort(key=lambda e: e.sort_key)
            self._by_id = self._full = self._first = self._configs = self._all = self._root = None
        if self.mark is not None:
            for e in self.entries:
                self.mark(self, e)

    def remark(self, entries: list[CatalogEntry] | None = None) -> None:
        """Give models their tags, star and collections again (the user changed them): ``entries``, or all."""
        if self.mark is None:
            return
        for e in entries if entries is not None else self.all_entries():
            self.mark(self, e)

    def game_entries(self, game: str) -> list[CatalogEntry]:
        """A game's models and configurable models, the same list until the library changes (importers index
        it for :meth:`Importer.related`)."""
        if self._games is None:
            by: dict[str, list[CatalogEntry]] = {}
            for e in self.all_entries():
                by.setdefault(e.game, []).append(e)
            self._games = by
        return self._games.get(game, [])

    def rearrange(self) -> None:
        """File every model again by the user's names and places (they changed)."""
        self._changed()

    def configs_changed(self) -> None:
        """Read the configuration lists again (the user saved one)."""
        self._changed()

    @property
    def configurations(self) -> list[CatalogEntry]:
        """An entry for each configurable model (see configs.py) whose base model is in the library."""
        if self._configs is None:
            self._configs = self._make_configs()
        return self._configs

    def _make_configs(self) -> list[CatalogEntry]:
        from . import configs

        shipped, mine = configs.bundled(), configs.user()
        out, self.config_notes = [], {}
        for game in self.games():
            roots = [r for r, g in self.roots.items() if g == game]
            entries = [e for e in self.entries if e.game == game]
            listed = [c for r in roots for c in self.found_configs.get(r, []) if c.guessed]
            listed += [c for c in shipped if c.game == game]
            listed += [c for r in roots for c in self.found_configs.get(r, []) if not c.guessed]
            listed += [c for c in mine if c.game == game]
            by_id = {}
            for c in listed:  # a later list replaces an earlier one's configuration of the same id
                by_id[c.id] = c
            if not by_id:
                continue
            resolver = configs.Resolver(entries, roots)
            base_entry = {e.path: e for e in entries}
            for c in by_id.values():
                got, missing = configs.resolve(c, resolver)
                if missing:
                    self.config_notes.setdefault(game, []).extend(missing)
                if got is None:
                    continue
                be = base_entry.get(Path(got.base.file))
                out.append(CatalogEntry(game, got.category or (be.category if be else "Weapons"),
                                        got.group or (be.group if be else ""), f"config:{got.id}", got.label,
                                        Path(got.base.file), importer=be.importer if be else "",
                                        packed=be.packed if be else False, config=got))
        if self.mark is not None:
            for e in out:
                self.mark(self, e)
        return sorted(out, key=lambda e: e.sort_key)

    def all_entries(self) -> list[CatalogEntry]:
        """The models and the configurable models, in order."""
        if self._all is None:
            extra = self.configurations
            self._all = sorted(self.entries + extra, key=lambda e: e.sort_key) if extra else self.entries
        return self._all

    def add_root(self, root: str | os.PathLike, game: str | None = None,
                 progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
        root = Path(root)
        game = game or detect_game(root) or root.name
        found, notes, kept = scan_folder_kept(root, game, progress)
        self.add_scanned(root, game, found, notes, folder_configurations(root, game, found))
        self.kept[root] = kept
        return found

    def add_scanned(self, root: Path, game: str, found: list[CatalogEntry], notes: list[str], configs: list) -> None:
        """Take in a folder scanned already (by :meth:`add_root`, or read back from a :class:`Snapshot`)."""
        root = Path(root)
        self.roots[root] = game
        self.notes[root] = list(notes)
        self.found_configs[root] = list(configs)
        self.entries = [e for e in self.entries if not _is_under(e.path, root)] + list(found)
        self.entries.sort(key=lambda e: e.sort_key)
        self._changed()

    def remove_root(self, root: str | os.PathLike) -> None:
        root = Path(root)
        self.roots.pop(root, None)
        self.notes.pop(root, None)
        self.found_configs.pop(root, None)
        self.entries = [e for e in self.entries if not _is_under(e.path, root)]
        self._changed()

    def set_game(self, root: str | os.PathLike, game: str) -> None:
        """File a folder's models under another game."""
        root = Path(root)
        if root not in self.roots:
            raise KeyError(root)
        self.roots[root] = game

        def moved(e: CatalogEntry) -> CatalogEntry:
            category, group, label = original_place(e)
            return dataclasses.replace(e, game=game, category=category, group=group, label=label)

        self.entries = sorted((moved(e) if _is_under(e.path, root) else e for e in self.entries),
                              key=lambda e: e.sort_key)
        self._changed()

    def merge(self, other: "Library") -> None:
        """Take over ``other``'s roots, replacing any entries this library already had under them."""
        for root, game in other.roots.items():
            self.roots[root] = game
            self.notes[root] = other.notes.get(root, [])
            self.found_configs[root] = other.found_configs.get(root, [])
            self.entries = [e for e in self.entries if not _is_under(e.path, root)]
        self.entries = sorted(self.entries + other.entries, key=lambda e: e.sort_key)
        self._changed()

    def find(self, entry_id: str) -> CatalogEntry | None:
        if self._by_id is None:
            self._by_id = {e.id: e for e in self.all_entries()}
        return self._by_id.get(entry_id)

    def hides(self, entry: CatalogEntry, include_lod: bool = False, copies: bool = True) -> bool:
        """Whether the lists leave ``entry`` out: a low-detail copy is hidden while its full model is there
        (unless ``include_lod``), and a model held by several of a game's folders (a copy collected into
        the app's library beside the game's own folder) is listed once: from its model package when it has
        one, else from the folder added first."""
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
                rank = (e.importer != "package", order.get(self.root_of(e), len(order)))
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, e.id)
            self._first = {k: v[1] for k, v in best.items()}
        return self._first.get((entry.game, entry.stem.lower()), entry.id) != entry.id

    def with_path(self, path: Path) -> list[CatalogEntry]:
        """The models listed from the file ``path`` (not the configurable ones built on it)."""
        if self._by_path is None:
            by: dict[str, list[CatalogEntry]] = {}
            for e in self.entries:
                by.setdefault(os.path.normcase(os.fspath(e.path)), []).append(e)
            self._by_path = by
        return list(self._by_path.get(os.path.normcase(os.fspath(path)), []))

    def root_of(self, entry: CatalogEntry) -> Path | None:
        if self._root is None:
            self._root = {}
            roots = [(r, _prefix(r)) for r in self.roots]
            for e in self.entries:  # a game's library can hold 100,000 models: worked out once
                p = os.path.normcase(os.fspath(e.path))
                self._root[e.id] = next((r for r, pre in roots if p.startswith(pre) or p == pre[:-1]), None)
        if entry.id in self._root:
            return self._root[entry.id]
        return next((r for r in self.roots if _is_under(entry.path, r)), None)

    def games(self) -> list[str]:
        """Games in the library, in the order their folders were added."""
        return list(dict.fromkeys(self.roots.values()))

    def query(self, game: str | None = None, category: str | None = None, group: str | None = None,
              text: str = "", include_lod: bool = False) -> list[CatalogEntry]:
        """The models of a game (every game when None; and category, group) whose name, name in the game, file
        name, kind, group or tags contain every word of ``text``."""
        words = text.lower().split()
        out = []
        for e in self.all_entries():
            if (game and e.game != game) or self.hides(e, include_lod) or (category and e.category != category):
                continue
            if group is not None and e.group != group:
                continue
            if words:
                hay = _haystack(e)
                if not all(w in hay for w in words):
                    continue
            out.append(e)
        return out

    def grouped(self, include_lod: bool = False) -> dict[str, dict[str, dict[str, list[CatalogEntry]]]]:
        tree: dict[str, dict[str, dict[str, list[CatalogEntry]]]] = {}
        for e in self.all_entries():
            if self.hides(e, include_lod):
                continue
            tree.setdefault(e.game, {}).setdefault(e.category, {}).setdefault(e.group, []).append(e)
        return tree

    def overview(self, include_lod: bool = False) -> list[dict]:
        """Each game with its folders, and its categories and groups with how many models each holds
        (worked out once per change to the library: the page asks often)."""
        if include_lod not in self._overview:
            self._overview[include_lod] = self._make_overview(include_lod)
        games = copy.deepcopy(self._overview[include_lod])
        for g in games:
            for f in g["folders"]:
                f["missing"] = not Path(f["path"]).is_dir()
        return games

    def _make_overview(self, include_lod: bool) -> list[dict]:
        tree = self.grouped(include_lod)
        counts: dict[Path | None, int] = {}
        for e in self.entries:
            if not self.hides(e, include_lod, copies=False):
                root = self.root_of(e)  # worked out once per library change
                counts[root] = counts.get(root, 0) + 1
        games = []
        for game in self.games() + [g for g in tree if g not in self.roots.values()]:
            cats = tree.get(game, {})
            categories = []
            for cat in sorted(cats, key=_category_index):
                groups = cats[cat]
                categories.append({"name": cat, "count": sum(len(v) for v in groups.values()),
                                   "groups": [{"name": g, "count": len(v)} for g, v in groups.items() if g]})
            folders = [{"path": str(r), "count": counts.get(r, 0), "notes": list(self.notes.get(r, []))}
                       for r, g in self.roots.items() if g == game]
            games.append({"name": game, "count": sum(c["count"] for c in categories),
                          "folders": folders, "categories": categories})
        return games


def _haystack(e: CatalogEntry) -> str:
    """What a search looks through for a model, in lower case (worked out once per name and place)."""
    key = (e.label, e.category, e.group, e.__dict__.get("_game_name"), e.__dict__.get("_tags"))
    kept = e.__dict__.get("_hay")
    if kept is None or kept[0] != key:
        original = e.__dict__.get("_original")
        tags = " ".join(e.__dict__.get("_tags") or ())  # the user's tags (a later version adds them)
        text = " ".join(filter(None, (e.label, e.__dict__.get("_game_name"), original[2] if original else "", e.stem,
                                      e.path.name, e.group, e.category, "configurable" if e.config else "", tags)))
        kept = (key, text.lower())
        e.__dict__["_hay"] = kept
    return kept[1]


def original_place(entry: CatalogEntry) -> tuple[str, str, str]:
    """(category, group, label) as the game's files gave them, before the user renamed or moved the model."""
    return entry.__dict__.get("_original") or (entry.category, entry.group, entry.label)


def place_entry(entry: CatalogEntry, category: str | None, group: str | None, label: str | None) -> None:
    """File ``entry`` under the user's own place and name (None: as the game has it)."""
    orig = entry.__dict__.setdefault("_original", (entry.category, entry.group, entry.label))
    entry.category = category or orig[0]
    entry.group = orig[1] if group is None else group
    entry.label = label or orig[2]


def _category_index(name: str) -> int:
    return CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER)


def _prefix(root: Path) -> str:
    """``root`` as the start of the paths inside it, for comparing as text (see _is_under)."""
    r = os.path.normcase(os.fspath(root))
    return r if r.endswith(("/", os.sep)) else r + os.sep


def _is_under(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or inside it (compared as text: Path.relative_to took seconds over a
    100,000-model library)."""
    p = os.path.normcase(os.fspath(path))
    r = os.path.normcase(os.fspath(root))
    if p == r:
        return True
    if not r.endswith(("/", os.sep)):
        r += os.sep
    return p.startswith(r)
