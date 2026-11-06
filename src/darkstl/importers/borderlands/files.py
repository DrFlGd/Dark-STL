"""Where Borderlands models live: packages in a game folder, objects inside them, and how to find them again.

A library entry names one object inside a package with a pseudo path,
``<package file>/<object path>.<class>``, for example
``.../CookedPCConsole/Startup.upk/Weap_Pistol.Pistol_Gestalt.SkeletalMesh``. Objects a
package only imports are looked up in the package named by their path's first part,
found anywhere under the same game folder.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from ...errors import FormatError
from .formats import upk
from .formats.props import ObjectRef

MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")
_MEMBER = re.compile(r"^(?P<path>.+)\.(?P<cls>SkeletalMesh|StaticMesh)$")

GAME_BL1 = "Borderlands"
GAME_BL2 = "Borderlands 2"
GAME_TPS = "Borderlands: The Pre-Sequel"
GAME_BL3 = "Borderlands 3"
GAME_WL = "Tiny Tina's Wonderlands"
GAME_BL4 = "Borderlands 4"
UE3_GAMES = (GAME_BL1, GAME_BL2, GAME_TPS)

# executables and folders that say which game a folder is
_MARKERS = [
    (GAME_BL4, ("OakGame/Binaries/Win64/Borderlands4.exe", "Borderlands4.exe")),
    (GAME_WL, ("OakGame/Binaries/Win64/Wonderlands.exe", "Wonderlands.exe")),
    (GAME_BL3, ("OakGame/Binaries/Win64/Borderlands3.exe", "Borderlands3.exe")),
    (GAME_TPS, ("Binaries/Win32/BorderlandsPreSequel.exe", "BorderlandsPreSequel.exe")),
    (GAME_BL2, ("Binaries/Win32/Borderlands2.exe", "Borderlands2.exe")),
    (GAME_BL1, ("Binaries/Borderlands.exe", "Binaries/Win64/BorderlandsGOTY.exe", "Borderlands.exe",
                "BorderlandsGOTY.exe")),
]
_HINTS = [
    (GAME_BL4, re.compile(r"borderlands ?4|\bbl4\b", re.I)),
    (GAME_WL, re.compile(r"wonderlands|\bttwl?\b", re.I)),
    (GAME_BL3, re.compile(r"borderlands ?3|\bbl3\b", re.I)),
    (GAME_TPS, re.compile(r"pre[-_ ]?sequel|\btps\b", re.I)),
    (GAME_BL2, re.compile(r"borderlands ?2|\bbl2\b", re.I)),
    (GAME_BL1, re.compile(r"borderlands(?! ?[234])|\bbl1\b", re.I)),
]


def is_member(path: Path) -> bool:
    path = Path(path)
    return path.parent.suffix.lower() in upk.PACKAGE_SUFFIXES and bool(_MEMBER.match(path.name)) \
        and path.parent.is_file()


def member_path(package: Path, object_path: str, class_name: str) -> Path:
    return Path(package) / f"{object_path}.{class_name}"


def split_member(path: Path) -> tuple[Path, str, str]:
    path = Path(path)
    m = _MEMBER.match(path.name)
    if not m:
        raise FormatError(f"{path} is not an object inside a package")
    return path.parent, m.group("path"), m.group("cls")


def game_root(path: Path) -> Path:
    """The folder that holds a package's game: above ``WillowGame``/``Cooked*`` when there is one, and for a
    DLC's packages (``<game>/DLC/<name>/...``) the game's own folder above the ``DLC`` folder."""
    root = _root(Path(path))
    if root.parent.name.lower() == "dlc" and root.parent.parent != root.parent:
        up = root.parent.parent
        return up.parent if up.name.lower() == "willowgame" else up
    return root


def _root(path: Path) -> Path:
    for p in [path, *path.parents]:
        if p.name.lower() == "willowgame":
            return p.parent
    for p in [path, *path.parents]:
        if p.name.lower().startswith("cooked"):
            return p.parent
    return path if path.is_dir() else path.parent


def detect_game(folder: Path) -> str | None:
    folder = Path(folder)
    for candidate in [folder, *list(folder.parents)[:3]]:
        for game, markers in _MARKERS:
            if any((candidate / m).exists() for m in markers):
                return game
    text = str(folder)
    unreal4 = is_unreal4_folder(folder)
    has_packages = not unreal4 and _has_packages(folder)
    for game, rx in _HINTS:
        if game in UE4_GAMES and not unreal4:
            continue
        if rx.search(text) and (has_packages or unreal4 or (folder / "WillowGame").is_dir()):
            return game
    if has_packages:
        return _game_from_packages(folder)
    if unreal4:
        return _game_from_paks(folder)
    return None


UE4_GAMES = (GAME_BL3, GAME_WL, GAME_BL4)


def is_unreal4_folder(folder: Path) -> bool:
    """Whether a folder holds an Unreal 4/5 game (paks, IoStore containers) or files extracted from one."""
    folder = Path(folder)
    for d in (folder, folder / "OakGame", folder.parent):
        if (d / "Content" / "Paks").is_dir() or (d / "OakGame" / "Content" / "Paks").is_dir():
            return True
    from .ue4 import pak as pakfile

    try:
        for dirpath, dirs, names in os.walk(folder):
            if any(n.lower().endswith((".utoc", ".uasset")) for n in names):
                return True
            paks = [n for n in names if n.lower().endswith(".pak")]
            if any(pakfile.is_pak_file(Path(dirpath) / n) for n in paks[:3]):
                return True
            if dirpath.count(os.sep) - str(folder).count(os.sep) > 4:
                dirs.clear()
    except OSError:
        pass
    return False


def _game_from_paks(folder: Path) -> str | None:
    """Borderlands 4 ships IoStore containers; Wonderlands' paks are a newer version than Borderlands 3's."""
    from .ue4 import pak as pakfile

    for dirpath, _dirs, names in os.walk(folder):
        if any(n.lower().endswith(".utoc") for n in names) and "oak" in str(folder).lower() + dirpath.lower():
            return GAME_BL4
        for n in sorted(names):
            if n.lower().endswith(".pak"):
                try:
                    with open(Path(dirpath) / n, "rb") as f:
                        version = pakfile._read_footer(f, (Path(dirpath) / n).stat().st_size)[0]
                except (OSError, FormatError):
                    continue
                if "oak" in (str(folder) + dirpath).lower():
                    return GAME_WL if version >= 10 else GAME_BL3
                return None
    return None


def _has_packages(folder: Path) -> bool:
    for name in ("WillowGame/CookedPCConsole", "WillowGame/CookedPC", "CookedPCConsole", "CookedPC"):
        if (folder / name).is_dir():
            return True
    try:
        return any(e.name.lower().endswith(".upk") for e in os.scandir(folder))
    except OSError:
        return False


def _game_from_packages(folder: Path) -> str | None:
    """Borderlands packages say which engine build made them (UE Viewer: 584/57-58 Borderlands, 832/46 BL2)."""
    for p in iter_packages(folder, limit=20):
        try:
            s = upk.open_package(p).summary
        except (FormatError, OSError):
            continue
        if s.version == 584 and s.licensee in (57, 58):
            return GAME_BL1
        if s.version >= 832 and s.licensee >= 46:
            return GAME_BL2 if s.licensee == 46 else GAME_TPS
    return None


def iter_packages(folder: Path, limit: int | None = None):
    from ...listing import files_under

    n = 0
    for p in files_under(folder):
        if p.name.lower().endswith((".upk", ".u")):
            yield p
            n += 1
            if limit is not None and n >= limit:
                return


class PackageIndex:
    """Package files under a game folder by lower-case stem."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.by_stem: dict[str, Path] = {}
        for p in iter_packages(self.root):
            self.by_stem.setdefault(p.stem.lower(), p)

    def find(self, stem: str) -> Path | None:
        return self.by_stem.get(stem.lower())


_INDEXES: dict[str, PackageIndex] = {}
_INDEX_LOCK = threading.Lock()


def index_for(path: Path) -> PackageIndex:
    root = game_root(path)
    key = os.path.normcase(str(root))
    with _INDEX_LOCK:
        idx = _INDEXES.get(key)
    if idx is None:
        idx = PackageIndex(root)
        with _INDEX_LOCK:
            _INDEXES[key] = idx
    return idx


def resolve(ref: ObjectRef, class_name: str | None = None) -> tuple[upk.Package, upk.Export] | None:
    """The package and export behind an object reference, following an import to its own package."""
    pkg = ref.pkg
    if ref.ref > 0:
        return pkg, pkg.exports[ref.ref - 1]
    if ref.ref == 0:
        return None
    path = pkg.object_path(ref.ref)
    cls = class_name or pkg.object_class(ref.ref)
    # a forced export of the same object in this package
    e = pkg.find_export(path, cls)
    if e is not None and e.path.lower() == path.lower():
        return pkg, e
    top, _, rest = path.partition(".")
    other = index_for(pkg.path).find(top)
    if other is not None and other != pkg.path:
        try:
            opkg = upk.open_package(other)
        except (FormatError, OSError):
            return None
        e = opkg.find_export(rest or path, cls) or opkg.find_export(path, cls)
        if e is not None:
            return opkg, e
    # anywhere else in this package (cooked packages flatten other packages' objects into their own)
    e = pkg.find_export(path, cls) or pkg.find_export(path.rsplit(".", 1)[-1], cls)
    if e is not None:
        return pkg, e
    return _elsewhere(pkg, path, cls)


_CONTENTS_CACHE: list[Path | None] = [None]


def use_cache(cache: Path | None) -> None:
    """The importer's cache folder, whose package lists :func:`resolve` searches for objects cooked elsewhere."""
    _CONTENTS_CACHE[0] = cache


def _elsewhere(pkg: upk.Package, path: str, cls: str | None) -> tuple[upk.Package, upk.Export] | None:
    """A copy of the object cooked into any other package of the game folder."""
    from .catalog import LOOKUP_CLASSES, object_index
    if cls not in LOOKUP_CLASSES:
        return None
    hit = object_index(game_root(pkg.path), _CONTENTS_CACHE[0]).find(path, cls)
    if hit is None or hit[0] == pkg.path:
        return None
    try:
        opkg = upk.open_package(hit[0])
    except (FormatError, OSError):
        return None
    e = opkg.find_export(hit[1], cls)
    return (opkg, e) if e is not None else None


def canonical_path(pkg: upk.Package, e: upk.Export) -> str:
    """The object's path with its original package first, so copies cooked into several packages match."""
    top = e
    seen = 0
    while top.outer > 0 and seen < 64:
        top = pkg.exports[top.outer - 1]
        seen += 1
    if top is not e and top.class_name == "Package" and top.export_flags & 0x01:  # EF_ForcedExport
        return e.path
    return f"{pkg.stem}.{e.path}"


__all__ = ["GAME_BL1", "GAME_BL2", "GAME_BL3", "GAME_BL4", "GAME_TPS", "GAME_WL", "MODEL_CLASSES", "PackageIndex",
           "UE3_GAMES", "canonical_path", "detect_game", "game_root", "index_for", "is_member", "iter_packages",
           "member_path", "resolve", "split_member", "use_cache"]
