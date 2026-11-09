"""A game's resources: every live resource in its containers, its StreamDB pieces, and loose copies.

Opened once per game folder and kept. The index of a game install (which resources are where) is read from the
containers' tables, with the container mask saying which copy of a resource is live, and kept in the importer's
cache until a container changes. A folder Collect wrote holds the resources decompressed instead, under
``base/<container>.resources/<name>`` with a ``.idmeta`` file beside each (its type and hash), and the StreamDB
pieces under ``base/streamdb/<id>.bin``; it is read the same way, without the game or Oodle.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import threading
from dataclasses import dataclass
from pathlib import Path

from ...errors import FormatError
from .formats import resources as res
from .formats import streamdb as sdb
from .games import Profile, game_of_container_version, profile

# the resource types the importer reads (others aren't indexed)
TYPES = ("baseModel", "model", "image", "skeleton", "anim", "rs_streamfile")
_DECLS = ("generated/decls/material2/", "generated/decls/entitydef/")
META = ".idmeta"
INDEX_VERSION = 1


@dataclass(frozen=True)
class Loc:
    container: str  # path of the container below base/ (posix), or of the loose file below base/
    offset: int
    stored: int
    size: int
    comp: int
    hash: int
    version: int
    loose: bool = False


def _wanted(type_: str, name: str) -> bool:
    if type_ not in TYPES:
        return False
    return type_ != "rs_streamfile" or name.startswith(_DECLS)


def find_root(path: Path) -> Path | None:
    """The game folder (the one holding ``base``) a resource path, container or folder is in."""
    path = Path(path)
    for p in [path, *path.parents]:
        if p.name.lower() == "base":
            return p.parent
        if (p / "base").is_dir() and p != path.anchor:
            return p
    return None


def split_path(path: Path) -> tuple[Path, str, str] | None:
    """A resource's path -> (game folder, its container below base/, its name)."""
    parts = Path(path).parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower().endswith(".resources"):
            for j in range(i - 1, -1, -1):
                if parts[j].lower() == "base":
                    root = Path(*parts[:j]) if j else Path(parts[0])
                    return root, "/".join(parts[j + 1:i + 1]), "/".join(parts[i + 1:])
            return None
    return None


class Store:
    def __init__(self, root: Path, game: str | None = None, cache: Path | None = None):
        self.root = Path(root)
        self.base = self.root / "base"
        self.entries: dict[tuple[str, str], Loc] = {}
        self.streamdbs: list[sdb.StreamDb] = []
        self.containers: list[str] = []
        self.notes: list[str] = []
        self.version = 0
        self._lock = threading.Lock()
        loose = self._loose_files()
        if loose:
            self._index_loose(loose)
        self._index_install(cache)
        self.game = game or self._game_from_files()
        self._images: dict[str, list[str]] | None = None

    @property
    def profile(self) -> Profile:
        """The game's profile, read each time so a change in the settings folder applies to the next model opened."""
        return profile(self.game)

    # -- indexing ------------------------------------------------------------

    def _loose_files(self) -> list[Path]:
        if not self.base.is_dir():
            return []
        return [p for p in self.base.rglob("*" + META)]

    def _index_loose(self, metas: list[Path]) -> None:
        for m in metas:
            sp = split_path(m.with_suffix(""))
            if sp is None:
                continue
            try:
                meta = json.loads(m.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            data = m.with_suffix("")
            if not data.is_file():
                continue
            size = data.stat().st_size
            rel = data.relative_to(self.base).as_posix()
            self.entries.setdefault((meta.get("type", ""), sp[2]),
                                    Loc(rel, 0, size, size, 0, int(meta.get("hash", 0)), int(meta.get("version", 0)),
                                        True))
            self.version = self.version or int(meta.get("container_version", 0))
            self._loose_game = meta.get("game")

    def _container_files(self) -> tuple[list[Path], list[Path]]:
        spec = self.base / "packagemapspec.json"
        files: list[str] = []
        if spec.is_file():
            try:
                data = json.loads(spec.read_text("utf-8", "replace"))
                files = [f["name"] for f in data.get("files", []) if isinstance(f, dict) and "name" in f]
            except (OSError, ValueError, TypeError):
                files = []
        if files:
            paths = [self.base / f for f in files]
        else:
            paths = sorted(p for p in self.base.rglob("*") if p.suffix.lower() in (".resources", ".streamdb"))
        containers = [p for p in paths if p.suffix.lower() == ".resources" and p.is_file()]
        dbs = [p for p in paths if p.suffix.lower() == ".streamdb" and p.is_file()]
        # patches first: their copies are the newest
        containers.sort(key=lambda p: (0 if "patch" in p.name.lower() else 1))
        return containers, dbs

    def _index_install(self, cache: Path | None) -> None:
        containers, dbs = self._container_files()
        for p in dbs:
            try:
                self.streamdbs.append(sdb.read_streamdb(p))
            except (FormatError, OSError) as exc:
                self.notes.append(f"{p.name}: {exc}")
        if not containers:
            return
        sig = hashlib.sha1(repr([(p.as_posix(), p.stat().st_size, int(p.stat().st_mtime))
                                 for p in containers]).encode()).hexdigest()[:20]
        cached = cache / f"index-{sig}.pickle" if cache else None
        if cached is not None and cached.is_file():
            try:
                with open(cached, "rb") as f:
                    data = pickle.load(f)
                if data.get("v") == INDEX_VERSION:
                    for k, v in data["entries"].items():
                        self.entries.setdefault(k, v)
                    self.containers, self.version, self.notes = data["containers"], data["version"], self.notes + \
                        data["notes"]
                    return
            except Exception:  # noqa: BLE001 - a bad cache is rebuilt
                pass
        tables = []
        for p in containers:
            try:
                tables.append(res.read_container(p))
            except (FormatError, OSError) as exc:
                self.notes.append(f"{p.name}: {exc}")
        if not tables:
            return
        self.version = tables[0].version
        mask = self._mask(tables)
        found: dict[tuple[str, str], Loc] = {}
        for t in tables:
            rel = t.path.relative_to(self.base).as_posix()
            self.containers.append(rel)
            alive = res.live(mask, t)
            for e, ok in zip(t.entries, alive):
                if ok and e.size > 0 and _wanted(e.type, e.name):
                    found.setdefault((e.type, e.name), Loc(rel, e.offset, e.stored, e.size, e.comp, e.hash,
                                                           e.version))
        for k, v in found.items():
            self.entries.setdefault(k, v)
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    pickle.dump({"v": INDEX_VERSION, "entries": found, "containers": self.containers,
                                 "version": self.version, "notes": self.notes}, f)
                tmp.replace(cached)
            except OSError:
                pass

    def _mask(self, tables: list[res.Container]) -> dict:
        meta = next((t for t in tables if t.path.name.lower() == "meta.resources"), None)
        if meta is None:
            return {}
        e = next((e for e in meta.entries if e.name == res.MASK_NAME), None)
        if e is None:
            return {}
        try:
            return res.read_container_mask(res.read_entry(meta.path, e), meta.version)
        except (FormatError, OSError, ValueError) as exc:
            self.notes.append(f"Every copy of each resource is listed, as the container mask couldn't be read: {exc}")
            return {}

    def _game_from_files(self) -> str | None:
        loose_game = getattr(self, "_loose_game", None)
        if loose_game:
            return loose_game
        for p in profile_list():
            if any((self.root / exe).is_file() for exe in p.executables):
                return p.game
        return game_of_container_version(self.version)

    # -- reading -------------------------------------------------------------

    def has(self, type_: str, name: str) -> bool:
        return (type_, name) in self.entries

    def names(self, type_: str) -> list[str]:
        return sorted(n for t, n in self.entries if t == type_)

    def loc(self, type_: str, name: str) -> Loc:
        try:
            return self.entries[(type_, name)]
        except KeyError:
            raise FormatError(f"{name} isn't in the game's files") from None

    def read(self, type_: str, name: str) -> bytes:
        loc = self.loc(type_, name)
        path = self.base / loc.container
        if loc.loose:
            return path.read_bytes()
        return res.read_entry(path, res.Entry(type_, name, loc.offset, loc.stored, loc.size, loc.comp, loc.hash,
                                              loc.version))

    def stream(self, ident: int, size: int) -> bytes | None:
        """A StreamDB piece decompressed to ``size`` bytes, or None when the game doesn't have it."""
        loose = self.base / "streamdb" / f"{ident:016x}.bin"
        if loose.is_file():
            return loose.read_bytes()
        for db in self.streamdbs:
            try:
                piece = sdb.read_piece(db, ident, size)
            except OSError:
                continue
            if piece is not None:
                return piece
        return None

    def geometry_id(self, hash_: int, lod: int) -> int:
        """The StreamDB id of a model's level of detail ``lod`` (0: the finest)."""
        return sdb.darkages_id(hash_, 4 - lod, 0) if self.profile.darkages else sdb.eternal_id(hash_, lod)

    def anim_piece_id(self, hash_: int, layout: int) -> int:
        """The StreamDB id of a Dark Ages animation's streamed frame sets (its stream layout ``layout``)."""
        return sdb.darkages_id(hash_, 0, layout)

    def path_of(self, type_: str, name: str) -> Path:
        """Where a resource is, as the library lists it: ``base/<container>/<name>``."""
        loc = self.loc(type_, name)
        if loc.loose:
            return self.base / loc.container
        return self.base / loc.container / name

    def image_names(self, base_path: str) -> list[str]:
        """The image resources made from one source picture (its path before the ``$`` options)."""
        if self._images is None:
            with self._lock:
                if self._images is None:
                    images: dict[str, list[str]] = {}
                    for t, n in self.entries:
                        if t == "image":
                            images.setdefault(n.split("$", 1)[0].lower(), []).append(n)
                    self._images = images
        return self._images.get(base_path.lower(), [])

    def meta(self, type_: str, name: str) -> dict:
        loc = self.loc(type_, name)
        return {"type": type_, "name": name, "hash": loc.hash, "version": loc.version, "game": self.game,
                "container_version": self.version}


def profile_list() -> list[Profile]:
    from .games import PROFILES

    return [profile(g) for g in PROFILES]


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def open_store(root: Path, game: str | None = None, cache: Path | None = None, fresh: bool = False) -> Store:
    key = str(Path(root).resolve())
    with _stores_lock:
        s = None if fresh else _stores.get(key)
        if s is None:
            from ..fromsoft.formats import oodle

            oodle.add_hint_path(Path(root), levels=0)
            # The Dark Ages ships no Oodle library (it's built into the exe): DOOM Eternal's, when it is installed
            # beside it (the same Steam library), is tried too
            eternal = Path(root).parent / "DOOMEternal"
            if eternal.is_dir():
                oodle.add_hint_path(eternal, levels=0)
            s = Store(Path(root), game, cache)
            _stores[key] = s
        return s


def forget(root: Path) -> None:
    with _stores_lock:
        _stores.pop(str(Path(root).resolve()), None)
