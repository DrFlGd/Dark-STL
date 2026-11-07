"""Scan a Cyberpunk 2077 folder and group the models in it.

Loose ``.mesh`` files are listed from their depot paths; ``.archive`` files
are opened and every CMesh in them listed too. An archive stores no names, so
each resource's import table (the textures, materials, meshes it refers to)
is read while scanning and the names learnt that way are kept in the app's
cache; a mesh nothing refers to by name is listed by its hash.

Depot paths group the models::

    base\\characters\\garment\\...\\<slot>\\   clothing (Armor), by slot
    base\\characters\\head|body|...\\       characters and their parts
    base\\weapons\\<kind>\\                  weapons
    base\\vehicles\\<kind>\\                 vehicles
    base\\environment\\<kind>\\              props and architecture (Objects)
    base\\gameplay\\<kind>\\                 items (Objects)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...model import LOD_SUFFIX, CatalogEntry
from ..fromsoft.formats import oodle
from .depot import ARCHIVE_DIRS, DEPOT_ROOTS, member_path
from .formats import archive as arc
from .formats import cr2w

VERSION = 2  # 2: weapon appearance files listed too
GAME = "Cyberpunk 2077"
_MESH = re.compile(r"\.mesh$", re.IGNORECASE)
_MARKERS = ("bin/x64/Cyberpunk2077.exe", "archive/pc/content", "r6/config", "REDprelauncher.exe")
_PATH_HINT = re.compile(r"cyberpunk|cp2077|cp77", re.IGNORECASE)
_NAMED_REFS = (".mesh", ".xbm", ".mi", ".mt", ".mlsetup", ".mlmask", ".mltemplate", ".ent", ".app", ".rig",
               ".anims", ".morphtarget", ".physicalscene", ".es", ".hp")


def detect_game(root: str | os.PathLike) -> str | None:
    root = Path(root)
    for candidate in [root, *list(root.parents)[:3]]:
        if any((candidate / m).exists() for m in _MARKERS):
            return GAME
    if _PATH_HINT.search(str(root)):
        return GAME
    # a folder of files WolvenKit exported: depot paths starting with base\ or ep1\
    for d in DEPOT_ROOTS:
        sub = root / d
        if sub.is_dir():
            for dirpath, _dirs, files in os.walk(sub):
                if any(_MESH.search(f) for f in files):
                    return GAME
    return None


def classify(depot: str | None) -> tuple[str, str, str, bool]:
    """``(category, group, label, is_lod)`` for a depot path (or None for an unnamed file)."""
    if not depot:
        return "Other", "Unnamed", "", False
    norm = depot.replace("/", "\\").lower()
    parts = norm.split("\\")
    stem = parts[-1].rsplit(".", 1)[0]
    label = stem
    is_lod = bool(LOD_SUFFIX.search(stem))
    if parts and parts[0] in DEPOT_ROOTS:
        parts = parts[1:]
    if len(parts) < 2:
        return "Other", "", label, is_lod
    top = parts[0]
    rest = parts[1:-1]
    if top == "characters":
        if rest and rest[0] == "garment":
            slot = next((p for p in rest[1:] if p in ("torso", "legs", "feet", "head", "hands", "arms", "face",
                                                        "inner_torso", "outer_torso", "dress", "full_body")),
                        rest[2] if len(rest) > 2 else (rest[1] if len(rest) > 1 else ""))
            return "Armor", slot.replace("_", " ").title(), label, is_lod
        group = rest[0] if rest else ""
        return "Characters", group.replace("_", " ").title(), label, is_lod
    if top == "weapons":
        group = "/".join(rest[:2]) if rest else ""
        return "Weapons", group.replace("_", " "), label, is_lod
    if top == "vehicles":
        return "Vehicles", (rest[0] if rest else "").replace("_", " ").title(), label, is_lod
    if top == "environment":
        return "Objects", (rest[0] if rest else "").replace("_", " ").title(), label, is_lod
    if top == "gameplay":
        return "Objects", "gameplay " + (rest[0] if rest else "").replace("_", " "), label, is_lod
    return "Other", top.replace("_", " ").title(), label, is_lod


def _entry(game: str, path: Path, depot: str | None, packed: bool) -> CatalogEntry:
    category, group, label, is_lod = classify(depot)
    stem = (depot.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0] if depot else path.name.rsplit(".", 1)[0])
    return CatalogEntry(game, category, group, stem, label or stem, path, importer="redengine", is_lod=is_lod,
                        packed=packed)


# ---------------------------------------------------------------------------
# Archives


def _cache_file(cache: Path | None, path: Path) -> Path | None:
    if cache is None:
        return None
    return cache / "archives" / f"{hashlib.sha1(os.path.normcase(str(path)).encode('utf-8')).hexdigest()[:16]}.json"


def _read_cache(file: Path | None, path: Path) -> dict | None:
    if file is None:
        return None
    try:
        data = json.loads(file.read_text("utf-8"))
        st = path.stat()
    except (OSError, ValueError):
        return None
    if data.get("version") != VERSION or data.get("size") != st.st_size or data.get("mtime") != st.st_mtime_ns:
        return None
    return data


def _write_cache(file: Path, path: Path, meshes: list[str], names: dict[str, str], complete: bool,
                 apps: list[str] | None = None, textures: list[str] | None = None) -> None:
    try:
        st = path.stat()
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(f".{threading.get_ident()}.tmp")
        data = {"version": VERSION, "size": st.st_size, "mtime": st.st_mtime_ns, "meshes": meshes, "names": names,
                "complete": complete, "apps": apps or []}
        if textures is not None:
            data["textures"] = textures
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, file)
    except OSError:
        pass


def referenced_textures(path: Path, cache: Path | None = None, read: bool = True,
                        progress: Callable[[int, str], None] | None = None) -> list[str]:
    """The textures (``.xbm`` depot paths) the resources in an archive refer to, wherever they're kept (the
    game keeps its textures in archives of their own, which name nothing). Kept in the archive's cache; one
    the scan cached before this was kept is read through again (unless not ``read``: none, then)."""
    cache_file = _cache_file(cache, path)
    cached = _read_cache(cache_file, path)
    if cached is not None and "textures" in cached:
        return list(cached["textures"])
    if not read:
        return []
    ar = arc.open_archive(path)
    found: set[str] = set()
    total, last = len(ar.entries), time.monotonic()
    for i, e in enumerate(ar.entries.values()):
        if progress and time.monotonic() - last > 0.5:
            last = time.monotonic()
            progress(-1, f"Reading {path.name}: {i + 1:,} of {total:,} files")
        try:
            head = ar.read_head(e.hash)
        except (FormatError, OSError):
            continue
        if not cr2w.is_cr2w(head):
            continue
        try:
            f = cr2w.read_cr2w(head, info_only=True)
        except (FormatError, ValueError, IndexError):
            continue
        found.update(p for p in (arc.normalize_path(imp.path) for imp in f.imports) if p.endswith(".xbm"))
    out = sorted(found)
    if cache_file is not None and cached is not None:
        _write_cache(cache_file, path, cached.get("meshes", []), cached.get("names", {}), cached.get("complete", True),
                     cached.get("apps"), out)
    return out


def index_archive(path: Path, cache: Path | None = None, progress: Callable[[int, str], None] | None = None) \
        -> tuple[arc.Archive, list[int]]:
    """Open an archive and find its meshes, learning file names from every resource's imports. The result is
    cached per archive, so the game is read through once."""
    ar = arc.open_archive(path)
    cache_file = _cache_file(cache, path)
    cached = _read_cache(cache_file, path)
    if cached is not None:
        meshes = [int(h, 16) for h in cached.get("meshes", [])]
        ar.names.update({int(h, 16): n for h, n in cached.get("names", {}).items()})
        if cached.get("complete", True):
            return ar, meshes
    meshes_set: set[int] = set()
    apps_set: set[int] = set()
    textures_set: set[str] = set()  # the textures its resources name, wherever they're kept
    names: dict[int, str] = dict(ar.names)
    total = len(ar.entries)
    last = time.monotonic()
    complete = True
    for i, e in enumerate(ar.entries.values()):
        if progress and (i % 200 == 0 or time.monotonic() - last > 0.5):
            last = time.monotonic()
            progress(-1, f"Reading {path.name}: {i + 1:,} of {total:,} files, {len(meshes_set):,} models")
        try:
            head = ar.read_head(e.hash)
        except FormatError as exc:
            if "Oodle" in str(exc):
                complete = False  # no decompressor yet: tried again next scan
                break
            continue
        except OSError:
            continue
        if not cr2w.is_cr2w(head):
            continue
        try:
            f = cr2w.read_cr2w(head, info_only=True)
        except (FormatError, ValueError, IndexError):
            continue
        if f.root_class == "CMesh":
            meshes_set.add(e.hash)
        weapon_app = f.root_class == "appearanceAppearanceResource"
        for imp in f.imports:
            p = arc.normalize_path(imp.path)
            if p.endswith(".xbm"):
                textures_set.add(p)
            if weapon_app and "\\weapons\\" in p and p.endswith((".mesh", ".ent")):
                apps_set.add(e.hash)
            if p.endswith(_NAMED_REFS):
                h = arc.fnv1a64(p)
                if h in ar.entries:
                    names.setdefault(h, p)
    ar.names.update(names)
    meshes = sorted(meshes_set)
    if cache_file is not None and complete:
        _write_cache(cache_file, path, [f"{h:016x}" for h in meshes], {f"{h:016x}": n for h, n in names.items()},
                     complete, [f"{h:016x}" for h in sorted(apps_set)], sorted(textures_set))
    _APPS[str(path)] = sorted(apps_set)
    return ar, meshes


_APPS: dict[str, list[int]] = {}  # archive path -> weapon appearance files found by the last index without a cache


def weapon_apps(path: Path, cache: Path | None) -> list[int]:
    """The weapon appearance files (``.app`` naming meshes under ``weapons``) in an archive, by hash."""
    cached = _read_cache(_cache_file(cache, path), path)
    if cached is None or "apps" not in cached:
        index_archive(path, cache)
        cached = _read_cache(_cache_file(cache, path), path)
    if cached is not None:
        return [int(h, 16) for h in cached.get("apps", [])]
    return _APPS.get(str(path), [])


def restore_names(paths: list[Path], cache: Path | None) -> None:
    """Ready a game folder's archives as a scan leaves them (each archive's names, from the cache, shared
    between them), without looking through the folder."""
    if not paths:
        return
    oodle.add_hint_path(paths[0].parent)
    opened = []
    for path in paths:
        try:
            opened.append(index_archive(path, cache)[0])
        except (FormatError, OSError):
            continue
    learn_names(opened)


def learn_names(archives: Iterable[arc.Archive]) -> None:
    """Names found in one archive apply to the others (gamedata names the textures in basegame_3...)."""
    archives = list(archives)
    known: dict[int, str] = {}
    for a in archives:
        known.update(a.names)
    if not known:
        return
    hashes = np.fromiter(known, dtype=np.uint64, count=len(known))
    for a in archives:  # a million names and dozens of archives: looked up all at once
        for h in hashes[a.entries.contains(hashes)].tolist():
            if h not in a.names:
                a.names[h] = known[h]


# ---------------------------------------------------------------------------
# Folders


def iter_files(root: Path) -> Iterable[Path]:
    from ...listing import files_under

    return files_under(root)


def scan(root: str | os.PathLike, game: str | None = None, progress: Callable[[int, str], None] | None = None,
         cache: Path | None = None) -> list[CatalogEntry]:
    root = Path(root)
    game = game or GAME
    entries: list[CatalogEntry] = []
    archives: list[Path] = []
    for i, path in enumerate(iter_files(root)):
        name = path.name
        if _MESH.search(name):
            depot = None
            parts = [p.lower() for p in path.relative_to(root).parts]
            for j in range(len(parts) - 1, -1, -1):
                if parts[j] in DEPOT_ROOTS:
                    depot = "\\".join(parts[j:])
                    break
            entries.append(_entry(game, path, depot or path.name, False))
        elif name.lower().endswith(".archive") and arc.is_archive_file(path):
            archives.append(path)
        if progress and i % 200 == 0:
            progress(i, name)
    if archives:
        oodle.add_hint_path(archives[0].parent)
        opened = []
        for path in archives:
            try:
                ar, meshes = index_archive(path, cache, progress)
            except (FormatError, OSError):
                continue
            opened.append((ar, meshes))
        learn_names([a for a, _ in opened])
        for ar, meshes in opened:
            for h in meshes:
                entries.append(_entry(game, member_path(ar.path, h), ar.names.get(h), True))
    return sorted(entries, key=lambda e: e.sort_key)


def folder_notes(root: Path, found: list[CatalogEntry]) -> list[str]:
    root = Path(root)
    notes = []
    archives = [p for sub in ARCHIVE_DIRS for p in ((root / sub).glob("*.archive") if (root / sub).is_dir() else [])]
    ours = [e for e in found if e.importer == "redengine"]
    if archives and not ours:
        if oodle.located() is None:
            notes.append("Cyberpunk 2077's archives are Kraken-compressed. Dark STL uses the game's own "
                         "bin\\x64\\oo2ext_7_win64.dll, which wasn't found here; point Settings > Oodle at it, "
                         "then rescan this folder.")
        else:
            notes.append(f"No models were found in the {len(archives)} archives of this folder.")
    unnamed = sum(1 for e in ours if e.packed and e.group == "Unnamed")
    if unnamed and ours:
        notes.append(f"{unnamed:,} of the {len(ours):,} models in the archives are listed by hash: nothing in the "
                     "archives names them.")
    return notes


__all__ = ["GAME", "classify", "detect_game", "folder_notes", "index_archive", "iter_files", "learn_names", "scan",
           "weapon_apps"]
