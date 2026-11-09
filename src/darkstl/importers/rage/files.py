"""A GTA V folder's files: its archives (nested ones too), the files inside them, and loose exports.

A file inside an archive has a pseudo path: the archive's path followed by the file's path inside it,
so ``<game>/x64e.rpf/levels/gta5/vehicles.rpf/adder.yft`` is ``adder.yft`` in ``vehicles.rpf`` in
``x64e.rpf``. A collected copy keeps the same paths as folders (``x64e.rpf/levels/...`` on disk), so it is
read the same way. Files are found by name as the game finds them, loose files first, then the update
archive, then DLC, then the base archives.
"""

from __future__ import annotations

import os
import threading
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ...access import note_file, note_member
from ...errors import FormatError
from ...listing import files_under
from . import games
from .formats import keys as keysmod
from .formats import rpf, rsc
from .formats import text as textfmt
from .formats.crypto import Keys

MODEL_EXT = (".ydr", ".ydd", ".yft")
KEEP_EXT = MODEL_EXT + (".ytd", ".ycd", ".meta", ".gxt2", ".xml")
SKIP_DIRS = ("audio", "movies", "sfx", "videos", "redistributables", "installers")


@dataclass
class Member:
    archive: int  # index into GameFiles.archives
    entry: rpf.Entry


@dataclass
class GameFiles:
    root: Path
    profile: games.Profile | None
    archives: list[rpf.Archive] = field(default_factory=list)
    archive_paths: list[str] = field(default_factory=list)  # each archive's path below the root
    members: dict[str, Member] = field(default_factory=dict)  # lower-case path below the root -> member
    loose: dict[str, Path] = field(default_factory=dict)  # lower-case path below the root -> file
    names: dict[str, list[str]] = field(default_factory=dict)  # file name -> paths below the root, in order
    vehicles: dict[str, textfmt.VehicleInfo] = field(default_factory=dict)
    peds: dict[str, textfmt.PedInfo] = field(default_factory=dict)
    weapons: dict[str, textfmt.WeaponInfo] = field(default_factory=dict)  # by model
    txd_parents: dict[str, str] = field(default_factory=dict)  # child dictionary -> parent (gtxd.meta)
    text: dict[int, str] = field(default_factory=dict)
    data_files: list[str] = field(default_factory=list)  # paths below the root the lists above came from
    components: dict[str, list[str]] = field(default_factory=dict)  # .ydd path -> drawable names
    notes: list[str] = field(default_factory=list)
    keys_found: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __getstate__(self):
        d = dict(self.__dict__)
        d.pop("_lock", None)
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._lock = threading.Lock()

    @property
    def game(self) -> str | None:
        return self.profile.name if self.profile else None

    @property
    def keys(self) -> Keys | None:
        return keysmod.cached()

    # -- paths -------------------------------------------------------------------------------------------

    def rel(self, path: Path) -> str | None:
        try:
            return Path(path).relative_to(self.root).as_posix().lower()
        except ValueError:
            return None

    def path(self, rel: str) -> Path:
        return self.root / rel if rel not in self.loose else self.loose[rel]

    def find(self, name: str) -> Path | None:
        """The first file of that name (``adder.ytd``), as the game would pick it."""
        places = self.names.get(name.lower())
        return self.path(places[0]) if places else None

    def find_all(self, name: str) -> list[Path]:
        return [self.path(p) for p in self.names.get(name.lower(), [])]

    def member(self, path: Path) -> Member | None:
        rel = self.rel(path)
        if rel is None or rel in self.loose:
            return None
        return self.members.get(rel)

    def exists(self, path: Path) -> bool:
        rel = self.rel(path)
        return rel is not None and (rel in self.loose or rel in self.members)

    def size(self, path: Path) -> int:
        rel = self.rel(path)
        if rel in self.loose:
            try:
                return self.loose[rel].stat().st_size
            except OSError:
                return 0
        m = self.members.get(rel or "")
        return m.entry.stored if m else 0

    def archive_of(self, path: Path) -> str | None:
        m = self.member(path)
        return self.archive_paths[m.archive] if m else None

    # -- reading -----------------------------------------------------------------------------------------

    def read(self, path: Path) -> bytes:
        """A file's bytes: a loose file as it is, an archive member decrypted and inflated (a resource as
        its pages, without a header: use :meth:`resource`)."""
        rel = self.rel(path)
        if rel is not None and rel in self.loose:
            note_file(self.loose[rel])
            return self.loose[rel].read_bytes()
        m = self.members.get(rel or "")
        if m is None:
            p = Path(path)
            if p.is_file():
                note_file(p)
                return p.read_bytes()
            raise FormatError(f"{Path(path).name} isn't in {self.root.name}")
        self.note(path)
        return self.archives[m.archive].read(m.entry, self.keys)

    def note(self, path: Path, kind: str = "other") -> None:
        """For Collect: a loose file as it is, an archive member as the loose file it would be, at the same
        path below the game folder."""
        rel = self.rel(path)
        if rel is not None and rel in self.loose:
            note_file(self.loose[rel], kind)
            return
        m = self.members.get(rel or "")
        if m is None:
            if Path(path).is_file():
                note_file(path, kind)
            return
        note_member(self.root / rel, rel, lambda p=Path(path): self.raw(p), m.entry.stored, kind)

    def raw(self, path: Path) -> bytes:
        """What a member would be as a loose file: a resource with its RSC7 header, a binary file as it is."""
        m = self.member(path)
        if m is None:
            return self.read(path)
        arc = self.archives[m.archive]
        if m.entry.resource:
            data = arc.raw(m.entry)
            if m.entry.encrypted:
                raise FormatError(f"{m.entry.name} is an encrypted resource")
            return data
        return arc.read(m.entry, self.keys)

    def resource(self, path: Path) -> rsc.Resource:
        rel = self.rel(path)
        m = self.members.get(rel or "") if rel not in self.loose else None
        if m is None:
            return _resource_file(self.read(path))
        arc = self.archives[m.archive]
        self.note(path)
        if not m.entry.resource:
            return _resource_file(arc.read(m.entry, self.keys))
        data = arc.read(m.entry, self.keys)
        return rsc.split(data, m.entry.version, m.entry.sys_flags, m.entry.gfx_flags)

    def resource_system(self, path: Path) -> rsc.Resource:
        """Only the system part of a resource (what lists and names are in), read without inflating the rest."""
        m = self.member(path)
        if m is None or not m.entry.resource:
            return self.resource(path)
        arc = self.archives[m.archive]
        raw = arc.raw(m.entry, 16)
        size = rsc.size_from_flags(m.entry.sys_flags)
        try:
            d = zlib.decompressobj(-15)
            sys_part = d.decompress(raw, size)
        except zlib.error as exc:
            raise FormatError(f"{m.entry.name} couldn't be inflated ({exc})") from exc
        return rsc.Resource(m.entry.version, sys_part, b"")

    # -- names -------------------------------------------------------------------------------------------

    def label(self, key: str) -> str | None:
        if not key:
            return None
        t = self.text.get(textfmt.joaat(key))
        if t and t.upper() != "NULL":
            return t
        return None


def _resource_file(data: bytes) -> rsc.Resource:
    return rsc.parse_file(data)


def _priority(rel: str) -> int:
    """Which copy of a file the game uses: the update archive's, then patch DLCs', then the DLC packs'
    (``update/x64/dlcpacks/...``), then the original archives'."""
    if "/dlcpacks/" in rel or rel.startswith("dlcpacks/"):
        return 2 if "patchday" in rel or "/update" in rel.split("/dlcpacks/", 1)[-1] else 3
    if rel.startswith("update/"):
        return 1
    return 4


def _rank(gf: GameFiles, rel: str) -> int:
    return 0 if rel in gf.loose else _priority(rel)


def _walk(root: Path):
    for p in files_under(root):
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(x.lower() in SKIP_DIRS for x in parts[:-1]):
            continue
        yield p


def build(root: Path, profile: games.Profile | None, keys: Keys | None, progress=None) -> GameFiles:
    root = Path(root)
    gf = GameFiles(root, profile, keys_found=keys is not None)
    ordered: list[tuple[int, str, object]] = []
    failed: list[str] = []
    for p in _walk(root):
        rel = p.relative_to(root).as_posix().lower()
        low = p.name.lower()
        if low.endswith(".rpf"):
            try:
                arc = rpf.read_archive(p, keys)
            except (FormatError, OSError) as exc:
                failed.append(f"{rel} ({exc})")
                continue
            if progress is not None:
                progress(-1, f"Reading {p.name}")
            with open(p, "rb") as fh:
                _add_archive(gf, arc, rel, keys, ordered, failed, fh)
        elif low.endswith(KEEP_EXT):
            gf.loose[rel] = p
            ordered.append((0, rel, None))
    ordered.sort(key=lambda t: t[0])  # stable: walk order within a priority
    for _prio, rel, _ in ordered:
        gf.names.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    if failed:
        gf.notes.append(f"{len(failed)} archive{'s' if len(failed) != 1 else ''} couldn't be opened: "
                        + "; ".join(failed[:4]) + ("..." if len(failed) > 4 else ""))
    _read_data(gf)
    return gf


def _add_archive(gf: GameFiles, arc: rpf.Archive, rel: str, keys, ordered, failed, fh) -> None:
    idx = len(gf.archives)
    gf.archives.append(arc)
    gf.archive_paths.append(rel)
    prio = _priority(rel)
    keep = []
    for e in arc.entries:
        low = e.name.lower()
        p = f"{rel}/{e.path}"
        if e.is_archive:
            try:
                sub = rpf.nested(arc, e, keys, fh)
            except (FormatError, OSError) as exc:
                failed.append(f"{p} ({exc})")
                continue
            _add_archive(gf, sub, p, keys, ordered, failed, fh)
            continue
        if not low.endswith(KEEP_EXT):
            continue
        keep.append(e)
        gf.members[p] = Member(idx, e)
        ordered.append((prio, p, None))
    arc.entries = keep  # only what the app reads, so the index stays small


def _read_data(gf: GameFiles) -> None:
    lang = (gf.profile.language if gf.profile else "american")
    text_files = []
    for name, places in gf.names.items():
        if name.endswith(".gxt2"):
            for rel in places:
                if f"/{lang}" in rel or f"{lang}" in rel.rsplit("/", 2)[0]:
                    text_files.append(rel)
    for rel in sorted(text_files, key=lambda r: -_rank(gf, r)):  # the base first, so updates win
        try:
            gf.text.update(textfmt.read_gxt2(gf.read(gf.path(rel))))
            gf.data_files.append(rel)
        except (FormatError, OSError):
            continue
    for name, places in gf.names.items():
        if not name.endswith(".meta"):
            continue
        kind = ("vehicles" if name == "vehicles.meta" else "peds" if name == "peds.meta"
                else "weapons" if name.startswith("weapon") else "gtxd" if name == "gtxd.meta" else None)
        if kind is None:
            continue
        for rel in reversed(places):  # base first, so the update and DLC lists win
            try:
                data = gf.read(gf.path(rel))
            except (FormatError, OSError):
                continue
            gf.data_files.append(rel)
            if kind == "vehicles":
                for v in textfmt.vehicles(data):
                    gf.vehicles[v.model] = v
            elif kind == "peds":
                for p in textfmt.peds(data):
                    gf.peds[p.model] = p
            elif kind == "weapons":
                for w in textfmt.weapons(data):
                    gf.weapons[w.model] = w
            else:
                for child, parent in _gtxd(data):
                    gf.txd_parents[child] = parent


def _gtxd(data: bytes) -> list[tuple[str, str]]:
    root = textfmt._xml(data)
    if root is None:
        return []
    out = []
    for item in root.iter("Item"):
        c, p = textfmt._t(item, "child"), textfmt._t(item, "parent")
        if c and p:
            out.append((c.lower(), p.lower()))
    return out


# -- the folders opened, by root -----------------------------------------------------------------------

_REGISTRY: dict[str, GameFiles] = {}
_LOCK = threading.Lock()


def _key(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _is_under(path: Path, root: Path) -> bool:
    a, b = _key(path), _key(root)
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def register(gf: GameFiles) -> None:
    with _LOCK:
        _REGISTRY[_key(gf.root)] = gf


def forget() -> None:
    with _LOCK:
        _REGISTRY.clear()
    _for_path.cache_clear()


def for_root(root: Path, profile: games.Profile | None = None, keys: Keys | None = None,
             rebuild: bool = False, progress=None) -> GameFiles:
    k = _key(root)
    with _LOCK:
        gf = _REGISTRY.get(k)
    if gf is None or rebuild:
        gf = build(Path(root), profile, keys, progress)
        register(gf)
        _for_path.cache_clear()
    return gf


def root_of(path: Path) -> GameFiles | None:
    k = _key(path)
    best = None
    with _LOCK:
        for rk, gf in _REGISTRY.items():
            if k == rk or k.startswith(rk.rstrip("\\/") + os.sep):
                if best is None or len(rk) > len(_key(best.root)):
                    best = gf
    return best


@lru_cache(maxsize=256)
def _for_path(key: str) -> GameFiles | None:
    return root_of(Path(key))


def for_path(path: Path) -> GameFiles:
    """The game files a model path belongs to (its folder's, opened now if it wasn't)."""
    gf = root_of(Path(path))
    if gf is not None:
        return gf
    path = Path(path)
    found = games.detect(path.parent)
    if found is not None:
        prof, root = found
        k, _why = keysmod.for_game(root)
        return for_root(root, prof, k)
    # an exported file on its own: its folder
    return for_root(path.parent, None, keysmod.cached())


def split_member(gf: GameFiles, path: Path) -> Member | None:
    return gf.member(path)
