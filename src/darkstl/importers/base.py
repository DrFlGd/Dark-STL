"""The interface every importer implements."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

from ..collect import RelatedFile
from ..errors import FormatError
from ..model import CatalogEntry, SourceModel, Texture

ScanProgress = Callable[[int, str], None]  # (files looked at, the file now); a count below 0: just the text


def made_by(entry: CatalogEntry, key: str) -> bool:
    """Whether ``entry`` was listed by the importer ``key``, or is a model package made from a file it reads."""
    return entry.importer == key or (entry.importer == "package" and entry.__dict__.get("_source_importer") == key)


def source_path(entry: CatalogEntry) -> Path:
    """The path of the game file a model is read from: for a model package, where that file would be in the
    package's folder (the package mirrors the game's folders: ``<packages>/<path in the game>.darkpkg``)."""
    if entry.importer == "package":
        folder = Path(entry.path).parent
        if folder.name.endswith(".darkpkg"):
            return folder.with_name(folder.name[:-len(".darkpkg")])
    return Path(entry.path)


_indexes: dict[tuple, tuple[list, dict]] = {}
_indexes_lock = threading.Lock()
KEEP_INDEXES = 8  # lists indexed at once per importer: one per game


def index_entries(entries: list[CatalogEntry], name: str, key: Callable[[CatalogEntry], Any]) -> dict[Any, list[CatalogEntry]]:
    """``entries`` grouped by ``key`` (None: left out), worked out once per list (``name`` tells the importers'
    indexes apart): for :meth:`Importer.related`, which is asked about many models of the same list."""
    ident = (name, id(entries), len(entries))
    with _indexes_lock:
        kept = _indexes.get(ident)
        if kept is not None and kept[0] is entries:
            return kept[1]
    out: dict[Any, list[CatalogEntry]] = {}
    for e in entries:
        k = key(e)
        if k is not None:
            out.setdefault(k, []).append(e)
    with _indexes_lock:
        _indexes[ident] = (entries, out)  # (the list is held, so its id isn't reused meanwhile)
        mine = [i for i in _indexes if i[0] == name]
        for old in mine[:-KEEP_INDEXES]:  # the oldest: lists of the library before it changed
            del _indexes[old]
    return out


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
    # Collect opens each model while recording what it reads (see darkstl.access) and copies that too. An importer
    # whose related_files already name everything a model reads turns it off, as it would only read the model twice.
    record_reads = True
    # Whether several of its models may be loaded at once, in threads, as a whole game's import does: true for an
    # importer whose reading shares nothing between loads that isn't safe so (open files, unguarded caches).
    parallel = False
    # Raised when its models' packages come to hold more (see darkstl.package), so that those written before are
    # written again by the next import rather than kept.
    package_version = 1

    def bulk(self) -> AbstractContextManager:
        """Within: a whole game's models are being loaded one after another, from folders that don't change
        meanwhile, so what the importer looks up in them (folder listings) may be kept."""
        return nullcontext()

    def detect_game(self, folder: Path) -> str | None:
        """Name of the game a folder of extracted files belongs to, if this importer recognises it."""
        return None

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        """Every model this importer can open under ``folder``."""
        return []

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        """Things to tell the user about a folder they added (for example that its files are still packed)."""
        return []

    def scan_state(self, folder: Path) -> Any:
        """What a scan of ``folder`` readied for opening its models (an index of its textures, say), to be
        kept with the scan's findings: when the folder hasn't changed by the next start, it isn't scanned
        again and this is given back to :meth:`restore_state` instead. Must pickle. None: nothing."""
        return None

    def restore_state(self, folder: Path, state: Any) -> None:
        """Ready ``folder`` for opening its models from what :meth:`scan_state` gave after its last scan."""

    def names(self, folder: Path, game: str, entries: list[CatalogEntry], fresh: bool = False) -> dict[str, str]:
        """The names the game gives the models it scanned in ``folder`` (``entries``), from the game's own
        text and item tables: {overlay key (see :func:`darkstl.overlay.model_key`, the model's path in the
        folder): name}. Called after each scan; kept in the game's library overlay, under the names the user
        gives. ``fresh``: read the game's files again rather than what was kept from them. Empty when the
        importer can't tell (the default)."""
        return {}

    def stats(self, path: Path) -> dict | None:
        """A quick look at the model in ``path`` without opening it fully, for the library's search and
        columns: {"size_mm": [x, y, z], "vertices", "triangles", "parts", "textures", "animated"}, any
        of them left out when not known. None when the importer can't tell without opening the model (it's
        then learnt when the model's preview is drawn or it's opened)."""
        return None

    def related(self, entry: CatalogEntry, entries: list[CatalogEntry]) -> list[tuple[str, CatalogEntry]]:
        """Other models of the game that go with ``entry`` (one this importer listed, or a package made from one:
        see :func:`made_by`): the rest of an armour set, a weapon's sheath, a character's face and hair, its
        low-detail copy. ``entries`` is every model of the game in the library, the same list object until the
        library changes, so it can be indexed once (:func:`index_entries`). Each as (how it's related, in a few
        plain words, the entry). Empty by default. (Configurable models' parts are added by the library.)"""
        return []

    def configurations(self, folder: Path, game: str, entries: list[CatalogEntry]) -> list:
        """Models in ``folder`` that are built from parts chosen per slot (a weapon and its magazines,
        sights, barrels), as :class:`~darkstl.configs.Configuration` objects. Called after the folder is
        scanned, with what the scan found. Files may be named as in a configuration list (see configs.py)."""
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

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        """Every texture a scanned game folder holds, as (its name in the game, a reference to it that
        :meth:`open_texture` takes), for the texture picker and for keeping every texture of a game. Empty
        when the importer can't list them. ``deep``: the listing may take a while (reading the game's
        archives through once), as keeping every texture does; without, only what's at hand is listed."""
        return []

    def open_texture(self, folder: Path, ref: str) -> Texture:
        """A texture :meth:`list_textures` listed."""
        raise FormatError(f"{self.name} can't open textures on their own")

    def open_textures(self, folder: Path, refs: list[str]) -> Iterator[tuple[str, Texture | Exception]]:
        """Each of ``refs`` (as :meth:`list_textures` listed them) opened, in any order: (the ref, its texture,
        or why it couldn't be opened). An importer whose textures share files reads each file once."""
        for ref in refs:
            try:
                yield ref, self.open_texture(folder, ref)
            except (FormatError, OSError, ValueError) as exc:
                yield ref, exc

    def review_info(self, path: Path, files: list[str]) -> dict:
        """For a review of a model's files (Export model files): where the game keeps them, when that is worth
        knowing (which of the game's archives each of ``files``, paths below the game folder, is read from)."""
        return {}

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """Files a game folder holds that every model from it may need (a compression library, the
        player's animations for armour)."""
        return []
