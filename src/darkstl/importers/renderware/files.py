"""A RenderWare GTA game folder: its archives, loose files and data, looked up by name.

The games find every model, texture dictionary and animation package by file name, wherever it
is (loose under ``models/`` or ``anim/``, or in an ``.img`` archive), so Dark STL does the same:
:class:`GameFiles` lists every ``.dff``, ``.txd`` and ``.ifp`` of a folder once, loose files
first. Archive members are addressed as pseudo paths ``<archive>.img/<name>`` (the archive is a
file, the member a name below it), and their copies made by Collect go to
``<archive's folder>/<archive name>/<name>`` (``models/gta3/landstal.dff``), which a later
listing finds as loose files.

The data files (``gta.dat`` and the ``.ide`` files it names, ``carcols.dat``, the ``.gxt``) are
read when the folder is first needed and kept.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ... import access
from ...errors import FormatError
from . import games
from .formats import gxt as gxtfmt
from .formats import img as imgfmt
from .formats import text as textfmt
from .formats import txd as txdfmt

MODEL_EXT = ".dff"
TEXTURE_EXT = ".txd"
ANIM_EXT = ".ifp"
LISTED = (MODEL_EXT, TEXTURE_EXT, ANIM_EXT, ".ipl")
_SKIP_DIRS = {"audio", "movies", "mp3", "mss", "screens", "skins", "userfiles", "user files"}


def is_archive(path: Path) -> bool:
    return Path(path).suffix.lower() == ".img"


def split_member(path: Path) -> tuple[Path, str] | None:
    """(archive, member name) when ``path`` names a file inside an ``.img`` archive."""
    path = Path(path)
    parent = path.parent
    if is_archive(parent) and not path.exists() and parent.is_file():
        return parent, path.name
    return None


@lru_cache(maxsize=64)
def _open_archive(path: str, stamp: tuple) -> imgfmt.ImgArchive:
    return imgfmt.read_img(path)


def open_archive(path: Path) -> imgfmt.ImgArchive:
    try:
        st = os.stat(path)
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError as exc:
        raise FormatError(f"{path} can't be read: {exc}") from exc
    return _open_archive(str(path), stamp)


def member_save_as(archive: Path, name: str, root: Path | None) -> str:
    """Where Collect keeps a member: below the game folder, in a folder named after its archive."""
    archive = Path(archive)
    folder = archive.with_suffix("")
    if root is not None:
        try:
            folder = folder.relative_to(root)
        except ValueError:
            folder = Path(folder.name)
    else:
        folder = Path(folder.name)
    return (folder / name).as_posix()


def note(path: Path, kind: str = "other") -> None:
    """For Collect: a loose file as it is, an archive member as a loose copy (see :func:`member_save_as`)."""
    if not access.active():
        return
    path = Path(path)
    member = split_member(path)
    if member is None:
        access.note_file(path, kind)
        return
    archive_path, name = member
    try:
        arc = open_archive(archive_path)
    except FormatError:
        return
    entry = arc.find(name)
    if entry is None:
        return
    gf = for_path(path)
    access.note_member(path, member_save_as(archive_path, entry.name, gf.root if gf.profile else None),
                       lambda a=arc, e=entry: a.read(e), entry.size, kind)


def read_bytes(path: Path, kind: str = "other") -> bytes:
    """A file's bytes, from disk or from its archive (noted for Collect either way)."""
    path = Path(path)
    note(path, kind)
    member = split_member(path)
    if member is None:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FormatError(f"{path.name} can't be read: {exc}") from exc
    archive_path, name = member
    arc = open_archive(archive_path)
    entry = arc.find(name)
    if entry is None:
        raise FormatError(f"{archive_path.name} holds no file named {name}")
    return arc.read(entry)


def exists(path: Path) -> bool:
    path = Path(path)
    if path.is_file():
        return True
    member = split_member(path)
    if member is None:
        return False
    try:
        return open_archive(member[0]).find(member[1]) is not None
    except FormatError:
        return False


def size_of(path: Path) -> int:
    member = split_member(Path(path))
    try:
        if member is None:
            return Path(path).stat().st_size
        e = open_archive(member[0]).find(member[1])
        return e.size if e is not None else 0
    except (OSError, FormatError):
        return 0


@lru_cache(maxsize=256)
def _txd_cached(key: str, stamp: tuple) -> txdfmt.TextureDictionary:
    return txdfmt.read_txd(read_bytes(Path(key), "texture"))


def read_txd(path: Path) -> txdfmt.TextureDictionary:
    """A texture dictionary (kept once read; noted for Collect every time)."""
    path = Path(path)
    member = split_member(path)
    if member is None:
        try:
            st = path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError as exc:
            raise FormatError(f"{path.name} can't be read: {exc}") from exc
    else:
        a = os.stat(member[0])
        stamp = (a.st_size, a.st_mtime_ns)
    note(path, "texture")
    return _txd_cached(str(path), stamp)


# ---------------------------------------------------------------------------
# The game folder


@dataclass
class GameFiles:
    root: Path
    profile: games.Profile | None
    names: dict[str, list[Path]] = field(default_factory=dict)  # lower-case file name -> where it is
    archives: list[Path] = field(default_factory=list)
    definitions: dict[str, textfmt.Definition] = field(default_factory=dict)  # lower-case model -> its line
    by_id: dict[int, textfmt.Definition] = field(default_factory=dict)
    txd_parents: dict[str, str] = field(default_factory=dict)
    data_files: list[Path] = field(default_factory=list)  # what the data was read from
    notes: list[str] = field(default_factory=list)
    _text: gxtfmt.GxtText | None = None
    _colours: textfmt.CarColours | None = None
    _areas: dict[str, str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_lock", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.Lock()

    @property
    def game(self) -> str | None:
        return self.profile.name if self.profile else None

    def find(self, name: str) -> Path | None:
        found = self.names.get(name.lower())
        return found[0] if found else None

    def models(self) -> list[Path]:
        return [ps[0] for n, ps in self.names.items() if n.endswith(MODEL_EXT)]

    def definition(self, stem: str) -> textfmt.Definition | None:
        return self.definitions.get(stem.lower())

    # -- texture dictionaries a model reads ------------------------------------

    def texture_chain(self, stem: str, vehicle: bool = False) -> list[Path]:
        """The dictionaries a model's textures are looked up in, in order: its own (from its definition,
        else the one named like it), their parents, then the shared ones."""
        names = []
        d = self.definition(stem)
        first = d.txd.lower() if d is not None and d.txd.lower() != "null" else stem.lower()
        cur, seen = first, set()
        while cur and cur not in seen and len(seen) < 16:
            seen.add(cur)
            names.append(cur)
            cur = self.txd_parents.get(cur)
        if d is not None and d.txd.lower() != stem.lower() and stem.lower() not in names:
            names.append(stem.lower())
        if self.profile is not None:
            if vehicle:
                names += [n for n in self.profile.vehicle_textures if n not in names]
            names += [n for n in self.profile.shared_textures if n not in names]
        out = []
        for n in names:
            p = self.find(n + TEXTURE_EXT)
            if p is not None and p not in out:
                out.append(p)
        return out

    # -- data -----------------------------------------------------------------

    @property
    def text(self) -> gxtfmt.GxtText | None:
        with self._lock:
            if self._text is None and self.profile is not None:
                p = games.find_ci(self.root, self.profile.text)
                self._text = gxtfmt.GxtText({}, {})
                if p is not None and p.is_file():
                    try:
                        self._text = gxtfmt.read_gxt(p.read_bytes())
                        self.data_files.append(p)
                    except (OSError, FormatError):
                        pass
            return self._text

    @property
    def colours(self) -> textfmt.CarColours:
        with self._lock:
            if self._colours is None:
                self._colours = textfmt.CarColours([], {})
                p = games.find_ci(self.root, self.profile.car_colours) if self.profile is not None else None
                if p is not None and p.is_file():
                    try:
                        self._colours = textfmt.car_colours(p.read_text("latin-1"))
                        self.data_files.append(p)
                    except OSError:
                        pass
            return self._colours

    def display_name(self, stem: str) -> str | None:
        """A vehicle's name in the game ("Landstalker" for ``landstal``), when its text has one."""
        d = self.definition(stem)
        if d is None or d.section != "cars" or not d.game_name:
            return None
        text = self.text
        name = text.get(d.game_name) if text is not None else None
        return name.strip() if name and name.strip() else None

    def areas(self) -> dict[str, str]:
        """For map objects, the area whose placements (``.ipl``) first place them: lower-case model -> area."""
        with self._lock:
            if self._areas is not None:
                return self._areas
            areas: dict[str, str] = {}
            ipls = [p for n, ps in self.names.items() if n.endswith(".ipl") for p in ps[:1]]
            ipls += [p for p in self.data_files if p.suffix.lower() == ".ipl"]
            for p in sorted(set(ipls), key=lambda q: str(q).lower()):
                try:
                    data = read_bytes(p, "other")
                except FormatError:
                    continue
                area = _area_name(p, self.root)
                if data[:4] == b"bnry":
                    places = textfmt.binary_placements(data)
                else:
                    places = textfmt.placements(data.decode("latin-1", "replace"))
                for pl in places:
                    model = pl.model.lower() if pl.model else ""
                    if not model:
                        d = self.by_id.get(pl.id)
                        model = d.model.lower() if d is not None else ""
                    if model:
                        areas.setdefault(model, area)
            self._areas = areas
            return areas


def _area_name(path: Path, root: Path) -> str:
    """``data/maps/LA/LAe.ipl`` -> ``LA / LAe``; a streamed ``lae_stream3.ipl`` -> ``lae``."""
    stem = path.stem
    low = stem.lower()
    if "_stream" in low:
        return stem[:low.index("_stream")]
    try:
        rel = Path(path).relative_to(root)
        parts = [p for p in rel.parts[:-1] if p.lower() not in ("data", "maps")]
    except ValueError:
        parts = []
    return " / ".join([*parts[-1:], stem]) if parts and parts[-1].lower() != stem.lower() else stem


def _walk(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(loose files to list, archives, data files) below ``root`` (from the scan's shared listing)."""
    from ...listing import files_under

    loose, archives, other = [], [], []
    for p in files_under(root):
        try:
            parts = p.relative_to(root).parts[:-1]
        except ValueError:
            parts = ()
        if any(x.lower() in _SKIP_DIRS or x.startswith(".") for x in parts):
            continue
        low = p.name.lower()
        if low.endswith(".img"):
            archives.append(p)
        elif low.endswith(LISTED):
            loose.append(p)
        elif low.endswith((".ide", ".dat", ".gxt")):
            other.append(p)
    return loose, archives, other


def _archive_order(p: Path) -> tuple:
    """gta3.img first (where the games look first), then the others by name."""
    name = p.name.lower()
    return (0 if name == "gta3.img" else 1 if p.parent.name.lower() == "models" else 2, str(p).lower())


def build(root: Path, profile: games.Profile | None) -> GameFiles:
    root = Path(root)
    gf = GameFiles(root, profile)
    loose, archives, other = _walk(root)
    # loose files first: the games and mod loaders prefer them; models/ before anything else
    loose.sort(key=lambda p: (0 if "models" in [x.lower() for x in p.relative_to(root).parts[:-1]] else 1,
                              str(p).lower()))
    for p in loose:
        gf.names.setdefault(p.name.lower(), []).append(p)
    gf.archives = sorted(archives, key=_archive_order)
    for a in gf.archives:
        try:
            arc = open_archive(a)
        except (FormatError, OSError) as exc:
            gf.notes.append(f"{a.name} couldn't be read: {exc}")
            continue
        for e in arc.entries:
            low = e.name.lower()
            if low.endswith(LISTED):
                gf.names.setdefault(low, []).append(a / e.name)
    if profile is not None:
        _read_definitions(gf, other)
    return gf


def _read_definitions(gf: GameFiles, other: list[Path]) -> None:
    """The models the game defines, from the IDE files its level lists name (every IDE file of the folder
    when they're missing, as in a collected copy without them)."""
    root, profile = gf.root, gf.profile
    ides: list[Path] = []
    for lf in profile.level_files:
        p = games.find_ci(root, lf)
        if p is None or not p.is_file():
            continue
        gf.data_files.append(p)
        try:
            lines = textfmt.level_files(p.read_text("latin-1"))
        except OSError:
            continue
        for kind, rel in lines:
            q = games.find_ci(root, rel)
            if q is None or not q.is_file():
                continue
            if kind == "IDE" and q not in ides:
                ides.append(q)
            elif kind == "IPL" and q not in gf.data_files:
                gf.data_files.append(q)
    for p in other:  # IDE files no list names (peds.ide, vehicles.ide are in default.dat; this catches the rest)
        if p.suffix.lower() == ".ide" and p not in ides:
            ides.append(p)
    for p in ides:
        try:
            text = p.read_text("latin-1")
        except OSError:
            continue
        gf.data_files.append(p)
        rel = p.relative_to(root).as_posix() if _is_under(p, root) else p.name
        defs, parents = textfmt.definitions(text, rel)
        for d in defs:
            gf.definitions.setdefault(d.model.lower(), d)
            gf.by_id.setdefault(d.id, d)
        for child, parent in parents:
            gf.txd_parents.setdefault(child, parent)


def _is_under(p: Path, root: Path) -> bool:
    try:
        Path(p).relative_to(root)
        return True
    except ValueError:
        return False


_REGISTRY: dict[str, GameFiles] = {}
_REG_LOCK = threading.Lock()


def _key(root: Path) -> str:
    return os.path.normcase(os.path.abspath(root))


def for_root(root: Path, profile: games.Profile | None = None, rebuild: bool = False) -> GameFiles:
    root = Path(root)
    k = _key(root)
    with _REG_LOCK:
        gf = _REGISTRY.get(k)
        if gf is not None and not rebuild:
            return gf
    if profile is None:
        found = games.detect(root)
        profile = found[0] if found is not None and _key(found[1]) == k else None
    gf = build(root, profile)
    with _REG_LOCK:
        _REGISTRY[k] = gf
    return gf


def register(gf: GameFiles) -> None:
    with _REG_LOCK:
        _REGISTRY[_key(gf.root)] = gf


def root_of(path: Path) -> tuple[Path, games.Profile | None]:
    """The game folder a model (file or archive member) is in, and its game; a folder of loose files
    that isn't a game's is its own root."""
    path = Path(path)
    member = split_member(path)
    folder = member[0].parent if member is not None else path.parent
    with _REG_LOCK:
        known = sorted(_REGISTRY.values(), key=lambda g: -len(str(g.root)))
    for gf in known:
        if gf.profile is not None and _is_under(folder, gf.root):
            return gf.root, gf.profile
    found = games.detect(folder)
    if found is not None:
        return found[1], found[0]
    return folder, None


def for_path(path: Path) -> GameFiles:
    root, profile = root_of(Path(path))
    return for_root(root, profile)


def forget(root: Path | None = None) -> None:
    with _REG_LOCK:
        if root is None:
            _REGISTRY.clear()
        else:
            _REGISTRY.pop(_key(Path(root)), None)
