"""The files of an Unreal 4/5 game, wherever they are: in its ``.pak`` files, its IoStore containers, or loose.

Every file has a "virtual" path, the one the game's packer gave it
(``OakGame/Content/Gear/Weapons/.../SK_Foo.uasset``). The library lists a model
as ``<game folder>/<virtual path>``, which is also where it would be if the
game's files were extracted, so a path works whether the file is packed or
lies on disk. Later patch paks (``*_P.pak``) replace files of earlier ones.

Packages are named the engine's way (``/Game/Gear/...`` for the project's
content, ``/<Plugin>/...`` for a plugin's, ``/Engine/...``); imports name the
package they come from, so the table from names to files is what follows a
material to its textures.

Encrypted paks need the game's AES key. Borderlands 3's is public and comes
with Dark STL; other keys can be put, one per line, in ``unreal-keys.txt`` in
the app's settings folder or in the game folder, or in ``DARKSTL_AES_KEYS``.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .... import access
from ....errors import FormatError
from . import asset, pak

KEY_FILE = "unreal-keys.txt"
# Public AES keys of games this importer reads (they are needed to read the paks the games ship).
BUNDLED_KEYS = {
    "Borderlands 3": ["0x115EE4F8C625C792F37A503308048E79726E512F0BF8D2AD7C4C87BC5947CBA7"],
}
# game -> function(game folder) giving its Unreal Engine release (425 for 4.25), for games that cook their
# packages unversioned; importers register theirs
ENGINE_OF_GAME: dict = {}
# games whose animations aren't all named like animations (A_, AS_, Anim_...): when a skeleton has none by
# name, every package is looked through for them (see animset.AnimIndex); importers add theirs
LOOK_THROUGH_FOR_ANIMATIONS: set = set()
ASSET_SUFFIXES = (".uasset", ".umap")
PAYLOAD_SUFFIXES = (".uexp", ".ubulk", ".uptnl", ".m.ubulk")


def _config_key_file() -> Path | None:
    try:
        from ....app.config import config_dir
    except Exception:  # noqa: BLE001 - the engine runs without the app's settings in tests
        return None
    return config_dir() / KEY_FILE


def known_keys(root: Path | None = None, game: str | None = None) -> list[bytes]:
    """AES keys to try: the bundled ones, then the user's (settings folder, game folder, environment)."""
    texts: list[str] = []
    for g, keys in BUNDLED_KEYS.items():
        if game is None or g == game:
            texts += keys
    if game is not None:  # the other bundled keys last: a folder may be labelled differently
        texts += [k for g, keys in BUNDLED_KEYS.items() if g != game for k in keys]
    files = [_config_key_file()]
    if root is not None:
        files += [Path(root) / KEY_FILE]
    for f in files:
        if f is not None and f.is_file():
            try:
                texts += f.read_text("utf-8", "replace").split()
            except OSError:
                pass
    texts += os.environ.get("DARKSTL_AES_KEYS", "").replace(",", " ").split()
    out: list[bytes] = []
    for t in texts:
        t = t.strip().split("#", 1)[0].strip()
        if not t:
            continue
        try:
            k = pak.parse_key(t)
        except FormatError:
            continue
        if k not in out:
            out.append(k)
    return out


def package_name(virtual: str) -> str | None:
    """``OakGame/Content/Gear/X.uasset`` -> ``/Game/Gear/X``; plugins -> ``/<Plugin>/...``."""
    parts = virtual.replace("\\", "/").strip("/").split("/")
    low = [p.lower() for p in parts]
    if "content" not in low:
        return None
    i = len(low) - 1 - low[::-1].index("content")
    stem = parts[-1]
    for suf in ASSET_SUFFIXES + PAYLOAD_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[:-len(suf)]
            break
    rest = parts[i + 1:-1] + [stem]
    if i == 0:
        mount = "Game"
    elif "plugins" in low[:i]:
        mount = parts[i - 1]
    elif low[i - 1] == "engine":
        mount = "Engine"
    else:
        mount = "Game"
    return "/" + "/".join([mount] + rest)


def _mount_prefix(mount: str) -> str:
    m = mount.replace("\\", "/")
    while m.startswith("../"):
        m = m[3:]
    return m.lstrip("/")


def pak_root(pak_path: Path) -> Path:
    """The game folder a pak's paths count from (``<game>/<Project>/Content/Paks/x.pak`` -> ``<game>``)."""
    p = Path(pak_path).parent
    for d in [p, *p.parents][:5]:
        if d.name.lower() == "paks" and d.parent.name.lower() == "content":
            return d.parent.parent.parent
    return Path(pak_path).parent


@dataclass
class FileRef:
    container: object | None  # a Pak, an IoStore reader, or None for a loose file
    entry: object  # the pak entry, chunk id, or the file's Path


@dataclass
class GameFiles:
    root: Path
    game: str | None = None
    files: dict[str, FileRef] = field(default_factory=dict)  # lower-case virtual path -> where
    spelled: dict[str, str] = field(default_factory=dict)  # lower-case -> as the packer wrote it
    packages: dict[str, str] = field(default_factory=dict)  # lower-case package name -> virtual path of its asset
    paks: list = field(default_factory=list)
    replaced: dict[str, list[FileRef]] = field(default_factory=dict)  # lower-case -> the copies later paks replaced
    locked: list[Path] = field(default_factory=list)  # paks no key opened
    errors: list[str] = field(default_factory=list)
    zen: object | None = None  # the IoStore reader (Unreal 5), if the game has one
    _engine: int | None = field(default=None, repr=False)
    _cache: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, virtual: str, ref: FileRef) -> None:
        low = virtual.lower()
        before = self.files.get(low)
        if before is not None:
            self.replaced.setdefault(low, []).append(before)
        self.files[low] = ref
        self.spelled[low] = virtual
        if low.endswith(ASSET_SUFFIXES):
            name = package_name(virtual)
            if name:
                self.packages[name.lower()] = virtual

    @staticmethod
    def _where(ref: FileRef) -> dict:
        c = ref.container
        if c is None:
            return {"loose": str(ref.entry)}
        name = getattr(getattr(c, "path", None), "name", None) or type(c).__name__
        return {"container": name, "size": int(getattr(ref.entry, "usize", 0) or 0)}

    def where(self, virtual: str) -> dict | None:
        """Which pak a file is read from, and the copies of it in earlier paks that one replaces (for a review)."""
        low = virtual.lower()
        ref = self.files.get(low)
        if ref is None:
            return None
        out = self._where(ref)
        if low in self.replaced:
            out["replaces"] = [self._where(r) for r in self.replaced[low]]
        return out

    def summary(self) -> dict:
        """The game's paks in the order they're read (later ones replace earlier ones' files), for a review."""
        per: dict[str, int] = {}
        for refs in self.replaced.values():
            for r in refs:
                name = self._where(r).get("container", "loose")
                per[name] = per.get(name, 0) + 1
        return {"root": str(self.root), "game": self.game, "engine": self._engine,
                "paks": [{"name": p.path.name, "version": p.version, "mount": p.mount, "files": len(p.entries),
                          "methods": list(p.methods), "replaced_by_later": per.get(p.path.name, 0)} for p in self.paks],
                "replaced_files": len(self.replaced), "locked": [p.name for p in self.locked],
                "errors": list(self.errors[:20]), "zen": self.zen is not None}

    def assets(self) -> list[str]:
        return [self.spelled[k] for k in self.files if k.endswith(ASSET_SUFFIXES)]

    def exists(self, virtual: str) -> bool:
        return virtual.lower() in self.files

    def read(self, virtual: str) -> bytes:
        ref = self.files.get(virtual.lower())
        if ref is None:
            raise FormatError(f"{virtual} isn't in the game's files")
        if ref.container is None:
            return Path(ref.entry).read_bytes()
        return ref.container.read_entry(ref.entry)

    def read_optional(self, virtual: str) -> bytes | None:
        return self.read(virtual) if self.exists(virtual) else None

    def virtual_of_package(self, name: str) -> str | None:
        return self.packages.get(name.lower().split(".", 1)[0])

    def note(self, virtual: str) -> None:
        """For collecting: the package at ``virtual`` and its payload files, each kept as a loose file (a zen
        package can't be: its whole container is)."""
        if not access.active():
            return
        if self.zen is not None and not self.exists(virtual):
            self.zen.note(virtual)
            return
        stem = virtual[:virtual.rfind(".")]
        for name in [virtual] + [stem + ext for ext in PAYLOAD_SUFFIXES]:
            ref = self.files.get(name.lower())
            if ref is None:
                continue
            spelled = self.spelled.get(name.lower(), name)
            if ref.container is None:
                access.note_file(Path(ref.entry), "model")
            else:
                access.note_member(self.root / spelled, spelled, lambda n=spelled: self.read(n),
                                   getattr(ref.entry, "usize", 0) or 0, "model")

    def open_package(self, virtual: str) -> asset.Package:
        """The package whose ``.uasset`` (or ``.umap``) is at ``virtual``, its ``.uexp``/``.ubulk`` with it."""
        self.note(virtual)
        key = virtual.lower()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        if self.zen is not None and not self.exists(virtual):
            pkg = self.zen.open_package(virtual)
        else:
            stem = virtual[:virtual.rfind(".")]
            uasset = self.read(virtual)
            uexp = self.read_optional(stem + ".uexp")
            name = package_name(virtual) or "/" + stem

            def payload(ext: str, stem=stem) -> bytes | None:
                return self.read_optional(stem + ext)

            pkg = asset.read_package(name, uasset, uexp, payload, game=_layout_game(self.game),
                                     assume_engine=self.assumed_engine)
            if pkg.package_flags & asset.PKG_UNVERSIONED_PROPERTIES:
                from .unversioned import reader_for
                pkg.unversioned_props = reader_for(pkg, self.root, self.game)
        pkg.virtual = virtual
        pkg.files = self
        with self._lock:
            self._cache[key] = pkg
            while len(self._cache) > 48:
                self._cache.popitem(last=False)
        return pkg

    def assumed_engine(self) -> int | None:
        """The Unreal Engine release (425 for 4.25) of a game whose packages are cooked unversioned, as the
        game's importer works it out (see :data:`ENGINE_OF_GAME`); asked once."""
        if self._engine is None:
            find = ENGINE_OF_GAME.get(self.game or "")
            self._engine = (find(self.root) if find else None) or 0
        return self._engine or None

    def open_named(self, name: str) -> asset.Package | None:
        """The package called ``/Game/...`` (None when the game doesn't have it)."""
        v = self.virtual_of_package(name)
        if v is None:
            if self.zen is not None:
                v = self.zen.by_name.get(name.lower().split(".", 1)[0])
                return self.open_package(v) if v else None
            return None
        return self.open_package(v)


def _layout_game(game: str | None) -> str:
    return "bl3" if game == "Borderlands 3" else ""


def _pak_order(p: Path) -> tuple:
    stem = p.stem.lower()
    return (stem.endswith("_p"), stem)


_OPEN: dict[str, GameFiles] = {}
_OPEN_LOCK = threading.Lock()


def find_paks(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ("binaries", "saved", "intermediate", "__pycache__")]
        for f in filenames:
            if f.lower().endswith(".pak"):
                out.append(Path(dirpath) / f)
    return sorted(out, key=_pak_order)


def game_files(root: str | os.PathLike, game: str | None = None, refresh: bool = False) -> GameFiles:
    """Every packed (and, for a folder of extracted files, loose) file under a game folder. Kept open."""
    root = Path(root)
    key = os.path.normcase(str(root))
    with _OPEN_LOCK:
        if not refresh and key in _OPEN:
            gf = _OPEN[key]
            if game and not gf.game:
                gf.game = game
            return gf
    gf = GameFiles(root, game)
    keys = known_keys(root, game)
    paks = find_paks(root)
    for p in paks:
        try:
            pk = pak.open_pak(p, keys)
        except FormatError as e:
            if "not an Unreal pak" in str(e):
                continue  # another engine's .pak (RE Engine games use the extension too)
            if "AES" in str(e) or "encrypted" in str(e):
                gf.locked.append(p)
            else:
                gf.errors.append(f"{p.name}: {e}")
            continue
        base = pak_root(p)
        try:
            rel = base.relative_to(root)
            prefix = "" if str(rel) == "." else rel.as_posix() + "/"
        except ValueError:
            prefix = ""
        mount = prefix + _mount_prefix(pk.mount)
        gf.paks.append(pk)
        for e in pk.entries.values():
            gf.add(mount + e.name, FileRef(pk, e))
    try:
        from . import iostore
        gf.zen = iostore.open_game(root, keys, gf)
    except FormatError as e:
        gf.errors.append(str(e))
    except ImportError:
        pass
    if not gf.files and gf.zen is None:  # a folder of extracted files
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                low = f.lower()
                if low.endswith(ASSET_SUFFIXES) or low.endswith(PAYLOAD_SUFFIXES):
                    path = Path(dirpath) / f
                    gf.add(path.relative_to(root).as_posix(), FileRef(None, path))
    with _OPEN_LOCK:
        _OPEN[key] = gf
    return gf


def forget(root: str | os.PathLike) -> None:
    """Let a game folder go, closing the containers kept open (so its files can be moved or deleted)."""
    with _OPEN_LOCK:
        gone = _OPEN.pop(os.path.normcase(str(Path(root))), None)
    if gone is not None and gone.zen is not None:
        for c in gone.zen.containers:
            c.close()  # each reopens what it reads if it's used again


def locate(path: str | os.PathLike) -> tuple[Path, str] | None:
    """(game folder, virtual path) for a library path, by finding the folder whose paks hold it."""
    path = Path(path)
    with _OPEN_LOCK:
        opened = list(_OPEN.values())
    for gf in opened:
        try:
            rel = path.relative_to(gf.root).as_posix()
        except ValueError:
            continue
        if gf.exists(rel) or (gf.zen is not None and gf.zen.exists(rel)):
            return gf.root, rel
    for d in path.parents:
        try:
            first = path.relative_to(d).parts[0]
        except (ValueError, IndexError):
            continue
        if (d / first / "Content" / "Paks").is_dir():
            return d, path.relative_to(d).as_posix()
        if (d / "Engine").is_dir() and (d / first / "Content").is_dir():
            return d, path.relative_to(d).as_posix()
    return None


__all__ = ["BUNDLED_KEYS", "FileRef", "GameFiles", "KEY_FILE", "find_paks", "forget", "game_files", "known_keys",
           "locate", "package_name", "pak_root"]
