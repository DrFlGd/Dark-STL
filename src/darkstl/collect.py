"""Copy the files chosen models need into the app's own library folder.

A game's install takes tens of gigabytes; the files Dark STL reads for a handful of models are
a few megabytes. Collecting copies the model archives with the textures and animations that go
with them into ``<library folder>/<game>/``, keeping each file's path relative to the game
folder it came from, so the same lookups (sibling ``.texbnd``, ``.anibnd``, split
``.tpfbhd``/``.tpfbdt`` pairs, unpacked folders) work on the copies. The game's folder can then
be removed from the library, or the game uninstalled, and the copies stay.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .model import CatalogEntry

KINDS = ("model", "texture", "animation", "other")


@dataclass(frozen=True)
class RelatedFile:
    """A file a model needs: where it is and what it is for."""

    path: Path
    kind: str  # one of KINDS


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


@dataclass
class Plan:
    game: str
    folder: Path  # <library>/<game>
    models: list[str]
    files: list[PlannedFile] = field(default_factory=list)

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
    out = Plan(game, folder, [e.stem for e in entries])
    seen: set[Path] = set()

    def add(f: RelatedFile, model: str) -> None:
        src = Path(f.path)
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
        shutil.copy2(f.source, tmp)
        os.replace(tmp, f.target)
        done += f.size
        copied += 1
    if progress:
        progress(1.0, "Done")
    return {"game": plan.game, "folder": str(plan.folder), "models": len(plan.models), "copied": copied,
            "bytes": sum(f.size for f in todo), "present": len(plan.files) - len(todo)}


def folder_size(folder: Path) -> tuple[int, int]:
    """(files, bytes) under a folder."""
    files = total = 0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
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


__all__ = ["KINDS", "CollectOptions", "Plan", "PlannedFile", "RelatedFile", "folder_size", "plan", "run"]
