"""Where a Cyberpunk model's files are, and how to get at the others it names.

Every resource names the others by depot path (``base\\...``). A model may be

* a loose ``.mesh`` file as WolvenKit exports it, kept under its depot path
  (``<somewhere>\\base\\weapons\\...\\w_x.mesh``): the files it names are looked
  for under the same root, then in the game's archives if the folder is the
  game's, then in the extra texture folders;
* a member of an ``.archive``, addressed as the pseudo path
  ``<pack>.archive\\<hash>.mesh``: the files it names are looked for in the
  game's archives (all of them, as the game does), then loose.

A :class:`Store` does these lookups; :class:`Source` is one opened model.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ..fromsoft.formats import oodle
from .formats import archive as arc
from .formats import cr2w
from .formats.mesh import RedMesh, read_mesh
from .formats.texture import RedTexture, read_texture

ARCHIVE_DIRS = ("archive/pc/content", "archive/pc/ep1", "archive/pc/mod", "archive/pc/dlc")
DEPOT_ROOTS = ("base", "ep1", "engine")  # the first folder of a depot path
_MEMBER = re.compile(r"^([0-9a-f]{16})(\.[a-z0-9_]+)?$", re.IGNORECASE)
_LOADED: dict[tuple[str, int, int], cr2w.Cr2wFile] = {}


# ---------------------------------------------------------------------------
# Locations


def is_archive_member(path: Path) -> bool:
    """``<pack>.archive/<hash>.<ext>``: a file inside an archive."""
    path = Path(path)
    return path.parent.suffix.lower() == ".archive" and bool(_MEMBER.match(path.name)) and path.parent.is_file()


def member_path(archive_path: Path, h: int, ext: str = ".mesh") -> Path:
    return Path(archive_path) / f"{h:016x}{ext}"


def member_hash(path: Path) -> int:
    m = _MEMBER.match(Path(path).name)
    if not m:
        raise FormatError(f"{path} is not a file inside an archive")
    return int(m.group(1), 16)


def depot_path_of(path: Path) -> str | None:
    """The depot path a loose file sits under (from its ``base``/``ep1`` folder down), if it does."""
    parts = [p.lower() for p in Path(path).parts]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in DEPOT_ROOTS:
            return "\\".join(parts[i:])
    return None


def depot_root_of(path: Path) -> Path | None:
    """The folder a loose file's depot path is relative to."""
    parts = Path(path).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() in DEPOT_ROOTS:
            return Path(*parts[:i]) if i else Path(".")
    return None


def game_root_of(path: Path) -> Path | None:
    """The game's install folder above ``path`` (the one holding ``archive/pc``), if any."""
    for d in [Path(path), *Path(path).parents]:
        if (d / "archive" / "pc").is_dir() or (d / "bin" / "x64" / "Cyberpunk2077.exe").is_file():
            return d
    return None


def game_archives(root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ARCHIVE_DIRS:
        d = root / sub
        if d.is_dir():
            out += sorted(p for p in d.iterdir() if p.suffix.lower() == ".archive" and p.is_file())
    return out


# ---------------------------------------------------------------------------
# Files


def _read_file(path: Path) -> cr2w.Cr2wFile:
    """A CR2W file, loose or in an archive, parsed once while it is unchanged."""
    path = Path(path)
    if is_archive_member(path):
        ar = arc.open_archive(path.parent)
        key = (str(path), ar.path.stat().st_size, 0)
        if key not in _LOADED:
            oodle.add_hint_path(path.parent)
            data = ar.read(member_hash(path))
            _LOADED[key] = cr2w.read_cr2w(data)
    else:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in _LOADED:
            data = path.read_bytes()
            if not cr2w.is_cr2w(data):
                raise FormatError(f"{path.name} is not a REDengine resource")
            oodle.add_hint_path(path)
            _LOADED[key] = cr2w.read_cr2w(data)
    while len(_LOADED) > 64:
        _LOADED.pop(next(iter(_LOADED)))
    return _LOADED[key]


@dataclass
class Source:
    """One opened model file."""

    path: Path
    notes: list[str] = field(default_factory=list)
    _file: cr2w.Cr2wFile | None = field(default=None, repr=False)
    _mesh: RedMesh | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        if is_archive_member(self.path):
            ar = arc.open_archive(self.path.parent)
            known = ar.names.get(member_hash(self.path))
            if known:
                return Path(known.replace("\\", "/")).stem
        return self.path.name.rsplit(".", 1)[0]

    @property
    def depot_path(self) -> str | None:
        if is_archive_member(self.path):
            return arc.open_archive(self.path.parent).names.get(member_hash(self.path))
        return depot_path_of(self.path)

    def file(self) -> cr2w.Cr2wFile:
        if self._file is None:
            self._file = _read_file(self.path)
        return self._file

    def mesh(self) -> RedMesh:
        if self._mesh is None:
            self._mesh = read_mesh(self.file())
        return self._mesh

    def appearance_names(self) -> list[str]:
        return [a.name for a in self.mesh().appearances]


def open_file(path: Path) -> Source:
    path = Path(path)
    if not is_archive_member(path) and not path.is_file():
        raise FormatError(f"There is no file at {path}")
    return Source(path)


# ---------------------------------------------------------------------------
# The store: depot path -> bytes


@dataclass(eq=False)
class TextureRef:
    name: str  # depot path
    source: str  # where it was found, for display
    _load: Callable[[], cr2w.Cr2wFile] = field(repr=False)
    _texture: RedTexture | None = field(default=None, repr=False)

    def texture(self) -> RedTexture:
        if self._texture is None:
            self._texture = read_texture(self._load())
        return self._texture

    def decode(self) -> np.ndarray:
        return self.texture().decode(0)

    def dds_bytes(self) -> bytes:
        return self.texture().to_dds()


class Store:
    """Finds resources by depot path: in loose folders, then in archives, then by file name."""

    def __init__(self, roots: list[Path], archives: list[Path], texture_dirs: tuple[str, ...] = ()):
        self.roots = [Path(r) for r in roots]
        self.archive_paths = [Path(a) for a in archives]
        self.texture_dirs = [Path(d) for d in texture_dirs]
        self._by_name: dict[str, Path] | None = None

    def _loose(self, depot: str) -> Path | None:
        rel = Path(*arc.normalize_path(depot).split("\\"))
        for root in self.roots + self.texture_dirs:
            p = root / rel
            if p.is_file():
                return p
        return None

    def _named(self, depot: str) -> Path | None:
        """A loose file with the same name anywhere in the extra texture folders (WolvenKit exports kept flat)."""
        if self._by_name is None:
            self._by_name = {}
            for d in self.texture_dirs:
                for dirpath, dirnames, filenames in os.walk(d):
                    dirnames.sort()
                    for n in filenames:
                        self._by_name.setdefault(n.lower(), Path(dirpath) / n)
        return self._by_name.get(arc.normalize_path(depot).rsplit("\\", 1)[-1])

    def locate(self, depot: str) -> Path | None:
        """The loose file or archive member that holds ``depot``, or None."""
        p = self._loose(depot)
        if p is not None:
            return p
        h = arc.path_hash(depot)
        for a in self.archive_paths:
            try:
                ar = arc.open_archive(a)
            except (FormatError, OSError):
                continue
            if h in ar.entries:
                ar.names.setdefault(h, arc.normalize_path(depot))
                return member_path(a, h, Path(depot).suffix.lower())
        return self._named(depot)

    def read(self, depot: str) -> cr2w.Cr2wFile | None:
        p = self.locate(depot)
        if p is None:
            return None
        try:
            return _read_file(p)
        except (FormatError, OSError):
            return None

    def describe_location(self, depot: str) -> str:
        p = self.locate(depot)
        if p is None:
            return "not found"
        if is_archive_member(p):
            return f"{p.parent.name} > {arc.normalize_path(depot)}"
        return str(p)

    def texture_ref(self, depot: str) -> TextureRef | None:
        p = self.locate(depot)
        if p is None:
            return None
        return TextureRef(arc.normalize_path(depot), self.describe_location(depot), lambda p=p: _read_file(p))


@lru_cache(maxsize=8)
def _store(roots: tuple[str, ...], archives: tuple[str, ...], texture_dirs: tuple[str, ...]) -> Store:
    return Store([Path(r) for r in roots], [Path(a) for a in archives], texture_dirs)


def store_for(path: Path, texture_dirs: tuple[str, ...] = ()) -> Store:
    """The store a model at ``path`` reads its materials and textures from."""
    path = Path(path)
    roots: list[Path] = []
    archives: list[Path] = []
    if is_archive_member(path):
        game = game_root_of(path.parent)
        archives = [path.parent] + [a for a in game_archives(game) if a != path.parent] if game else [path.parent]
        if game:
            roots.append(game)  # files extracted beside the game (mods, WolvenKit exports)
    else:
        root = depot_root_of(path)
        if root is not None:
            roots.append(root)
        else:
            roots.append(path.parent)
        game = game_root_of(path)
        if game:
            archives = game_archives(game)
        for d in texture_dirs:
            g = game_root_of(Path(d))
            if g and g not in roots:
                archives += [a for a in game_archives(g) if a not in archives]
    for d in texture_dirs:
        if Path(d) not in roots:
            roots.append(Path(d))
    return _store(tuple(str(r) for r in roots), tuple(str(a) for a in archives), tuple(texture_dirs))


# ---------------------------------------------------------------------------
# Collecting


def _resource_refs(f: cr2w.Cr2wFile, store: Store, seen: set[str], out: list[tuple[str, Path]], depth: int) -> None:
    """Every resource ``f`` imports (and those they import, for materials), located."""
    for imp in f.imports:
        depot = arc.normalize_path(imp.path)
        if not depot or depot in seen:
            continue
        seen.add(depot)
        p = store.locate(depot)
        if p is None:
            continue
        out.append((depot, p))
        if depot.endswith((".mi", ".mt", ".mlsetup", ".mltemplate")) and depth < 6:
            sub = store.read(depot)
            if sub is not None:
                _resource_refs(sub, store, seen, out, depth + 1)


def related_files(path: Path, textures: bool = True) -> list[RelatedFile]:
    """The model and what it names (materials, textures), as files to copy or extract under their depot paths."""
    path = Path(path)
    out: list[RelatedFile] = []
    source = open_file(path)
    store = store_for(path)
    own = source.depot_path
    if is_archive_member(path):
        ar = arc.open_archive(path.parent)
        h = member_hash(path)
        save_as = own or f"_unnamed/{h:016x}.mesh"
        out.append(RelatedFile(path, "model", save_as.replace("\\", "/"), lambda ar=ar, h=h: ar.read(h), ar.size_of(h)))
    else:
        out.append(RelatedFile(path, "model"))
    if not textures:
        return out
    refs: list[tuple[str, Path]] = []
    seen = {own} if own else set()
    _resource_refs(source.file(), store, seen, refs, 0)
    if source.file().root_class == "CMesh":  # materials kept inside the mesh name their textures themselves
        for lf in source.mesh().local_material_files:
            _resource_refs(lf, store, seen, refs, 1)
    for depot, p in refs:
        kind = "texture" if depot.endswith((".xbm", ".mlmask")) else "other"
        if is_archive_member(p):
            ar = arc.open_archive(p.parent)
            h = member_hash(p)
            out.append(RelatedFile(p, kind, depot.replace("\\", "/"), lambda ar=ar, h=h: ar.read(h), ar.size_of(h)))
        elif depot_path_of(p) == depot:
            out.append(RelatedFile(p, kind))  # keeps its relative path under the same root
        else:
            out.append(RelatedFile(p, kind, depot.replace("\\", "/"), lambda p=p: p.read_bytes(), p.stat().st_size))
    return out


def shared_files(root: Path) -> list[Path]:
    """The game's Oodle library, which its archives and exported buffers need."""
    out = []
    for sub in ("bin/x64", "."):
        d = Path(root) / sub
        try:
            out += sorted(p for p in d.iterdir() if re.match(r"^oo2ext.*\.dll$|^(lib)?kraken\.(dll|so|dylib)$",
                                                             p.name, re.IGNORECASE))
        except OSError:
            pass
    return out


__all__ = ["ARCHIVE_DIRS", "Source", "Store", "TextureRef", "depot_path_of", "depot_root_of", "game_archives",
           "game_root_of", "is_archive_member", "member_hash", "member_path", "open_file", "related_files",
           "shared_files", "store_for"]
