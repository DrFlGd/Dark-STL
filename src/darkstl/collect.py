"""Copy the files chosen models need into the app's own library folder.

A game's install takes tens of gigabytes; the files Dark STL reads for a handful of models are
a few megabytes. Collecting copies the model archives with the textures and animations that go
with them into ``<library folder>/<game>/``, keeping each file's path relative to the game
folder it came from, so the same lookups (sibling ``.texbnd``, ``.anibnd``, split
``.tpfbhd``/``.tpfbdt`` pairs, unpacked folders) work on the copies. Textures a model takes from
elsewhere in the game (a map's texture pack) are written out on their own instead of copying
the whole pack. The game's folder can then
be removed from the library, or the game uninstalled, and the copies stay.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .model import CatalogEntry

KINDS = ("model", "texture", "animation", "other")
MANIFEST = "darkstl-collection.json"


@dataclass(frozen=True)
class RelatedFile:
    """A file a model needs: where it is and what it is for. With ``extract``, only part of the file is
    needed (one texture out of a map's pack): ``extract`` reads that part, and it is saved as ``save_as``
    (a path below the game's library folder), which ``size`` estimates the size of."""

    path: Path
    kind: str  # one of KINDS
    save_as: str = ""
    extract: Callable[[], bytes] | None = field(default=None, compare=False)
    size: int = 0


@dataclass
class CollectOptions:
    textures: bool = True
    animations: bool = True
    player_animations: bool = False  # the player's anibnd, which armour pieces are animated with
    lod: bool = False  # low-detail copies of the chosen models too
    extras: bool = True  # what the game needs as a whole (the Oodle library)


@dataclass
class PlannedFile:
    source: Path
    target: Path
    kind: str
    size: int
    model: str  # the model (stem) it belongs to, or "" for the game's shared files
    present: bool = False  # already in the library folder with the same size and date
    extract: Callable[[], bytes] | None = None  # what to write instead of copying the source (see RelatedFile)


@dataclass
class Plan:
    game: str
    folder: Path  # <library>/<game>
    models: list[str]
    files: list[PlannedFile] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)  # the game folders the files come from

    @property
    def to_copy(self) -> list[PlannedFile]:
        return [f for f in self.files if not f.present]

    def summary(self) -> dict:
        kinds = {k: {"files": 0, "bytes": 0} for k in KINDS}
        for f in self.files:
            kinds[f.kind]["files"] += 1
            kinds[f.kind]["bytes"] += f.size
        return {"game": self.game, "folder": str(self.folder), "models": len(self.models),
                "files": len(self.files), "bytes": sum(f.size for f in self.files),
                "to_copy": len(self.to_copy), "bytes_to_copy": sum(f.size for f in self.to_copy),
                "present": len(self.files) - len(self.to_copy), "kinds": kinds,
                "sample": [str(f.source) for f in self.files[:8]]}


def _same_file(src: Path, dst: Path) -> bool:
    try:
        a, b = src.stat(), dst.stat()
    except OSError:
        return False
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def _relative(path: Path, roots: list[Path]) -> Path:
    """Where a file goes under the game's library folder: its path below the game folder it is in,
    else its own folder's name and its name."""
    for root in roots:
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    return Path(path.parent.name) / path.name


def plan(library: Path, game: str, entries: list[CatalogEntry], roots: list[Path], options: CollectOptions,
         related: Callable[[CatalogEntry], list[RelatedFile]],
         extras: Callable[[Path], list[RelatedFile]] | None = None) -> Plan:
    """What collecting ``entries`` would copy, and what is already there."""
    folder = Path(library) / _folder_name(game)
    out = Plan(game, folder, [e.stem for e in entries], roots=[Path(r) for r in roots if not _is_under(Path(r), folder)])
    seen: set[Path] = set()

    def add(f: RelatedFile, model: str) -> None:
        src = Path(f.path)
        if f.extract is not None:
            target = folder / f.save_as
            if target in seen or _is_under(src, folder):
                return
            seen.add(target)
            out.files.append(PlannedFile(src, target, f.kind, f.size, model, target.is_file(), f.extract))
            return
        if src in seen:
            return
        seen.add(src)
        if src.is_dir():
            for dirpath, _dirs, names in os.walk(src):
                for name in sorted(names):
                    add(RelatedFile(Path(dirpath) / name, f.kind), model)
            return
        try:
            size = src.stat().st_size
        except OSError:
            return
        target = folder / _relative(src, roots)
        if _is_under(src, folder):
            return  # already a library copy
        out.files.append(PlannedFile(src, target, f.kind, size, model, _same_file(src, target)))

    for e in entries:
        for f in related(e):
            add(f, e.stem)
    if options.extras and extras is not None:
        for root in roots:
            for f in extras(root):
                add(f, "")
    return out


def run(plan: Plan, progress: Callable[[float, str], None] | None = None) -> dict:
    """Copy the plan's files (those not already there), keeping their dates."""
    todo = plan.to_copy
    total = sum(f.size for f in todo) or 1
    done = 0
    copied = 0
    for f in todo:
        if progress:
            progress(done / total, f"{f.model or f.kind}: {f.source.name}")
        f.target.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.target.with_name(f.target.name + ".part")
        if f.extract is not None:
            tmp.write_bytes(f.extract())
        else:
            shutil.copy2(f.source, tmp)
        os.replace(tmp, f.target)
        done += f.size
        copied += 1
    write_manifest(plan.folder, plan.game, plan.models, sorted({str(r) for r in plan.roots}))
    if progress:
        progress(1.0, "Done")
    return {"game": plan.game, "folder": str(plan.folder), "models": len(plan.models), "copied": copied,
            "bytes": sum(f.size for f in todo), "present": len(plan.files) - len(todo)}


def read_manifest(folder: Path) -> dict | None:
    """What a folder of collected files says about itself, or None for any other folder."""
    try:
        data = json.loads((Path(folder) / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != "collection" or not str(data.get("game") or "").strip():
        return None
    return data


def write_manifest(folder: Path, game: str, models: list[str] = (), sources: list[str] = ()) -> dict:
    """Record (or bring up to date) which game a folder of copies belongs to and what it holds."""
    from . import __version__

    folder = Path(folder)
    old = read_manifest(folder) or {}
    data = {"kind": "collection", "app": "Dark STL", "format": 1, "game": game,
            "models": sorted(set(old.get("models") or []) | set(models), key=str.lower),
            "sources": sorted(set(old.get("sources") or []) | set(sources)),
            "created": old.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": __version__}
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{MANIFEST}.part"
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    os.replace(tmp, folder / MANIFEST)
    return data


def collections_in(folder: Path) -> list[tuple[Path, dict]]:
    """The folders of collected files at ``folder``: itself, or else the game folders directly inside it
    (a whole library folder, one folder per game)."""
    folder = Path(folder)
    own = read_manifest(folder)
    if own is not None:
        return [(folder, own)]
    out = []
    try:
        subs = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return []
    for sub in subs:
        data = read_manifest(sub)
        if data is not None:
            out.append((sub, data))
    return out


def move_folder(src: Path, dst: Path) -> None:
    """Move a game's folder of copies to ``dst``; into what's there already when it exists."""
    src, dst = Path(src), Path(dst)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))  # a rename on the same drive
        return
    for dirpath, _dirs, names in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in names:
            if name == MANIFEST:
                continue
            shutil.move(str(Path(dirpath) / name), str(dst / rel / name))  # replaces an older copy
    data = read_manifest(src) or {}
    write_manifest(dst, data.get("game") or dst.name, data.get("models") or [], data.get("sources") or [])
    shutil.rmtree(src, ignore_errors=True)


def folder_size(folder: Path) -> tuple[int, int]:
    """(files, bytes) under a folder (the copies: not the notes of what they are)."""
    files = total = 0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            if name == MANIFEST:
                continue
            try:
                total += (Path(dirpath) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return files, total


def _folder_name(game: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_." else "_" for c in game).strip(" .")
    return keep or "Game"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["KINDS", "MANIFEST", "CollectOptions", "Plan", "PlannedFile", "RelatedFile", "collections_in", "folder_size",
           "move_folder", "plan", "read_manifest", "run", "write_manifest"]
