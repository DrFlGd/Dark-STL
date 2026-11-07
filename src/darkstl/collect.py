"""Copy the files chosen models need into the app's own library folder.

A game's install takes tens of gigabytes; the files Dark STL reads for a handful of models are
a few megabytes. Collecting copies the model archives with the textures and animations that go
with them into ``<library folder>/<game>/``, keeping each file's path relative to the game
folder it came from, so the same lookups (sibling ``.texbnd``, ``.anibnd``, split
``.tpfbhd``/``.tpfbdt`` pairs, unpacked folders) work on the copies. Textures a model takes from
elsewhere in the game (a map's texture pack) are written out on their own instead of copying
the whole pack. Files a model reads from inside an archive (a ``.pak``, ``.archive``...) are written out
as loose files, and what an importer can't name ahead of time is found by opening each model while
recording what it reads (see :mod:`darkstl.access`). The configurable models collected (a gun and its
parts and skins) are written beside the copies as a configuration list, naming the copies. The game's
folder can then be removed from the library, or the game uninstalled, and the copies stay.
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
CONFIGS = "collected.darkstl-configs.json"  # the configurable models collected (see configs.in_folder)


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
    configurations: list = field(default_factory=list)  # configs.Configuration, naming the copies

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
                "configurations": len(self.configurations),
                "sample": [str(f.source) for f in self.files[:8]]}


def _same_file(src: Path, dst: Path) -> bool:
    try:
        a, b = src.stat(), dst.stat()
    except OSError:
        return False
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def relative_path(path: Path, roots: list[Path]) -> Path:
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
         extras: Callable[[Path], list[RelatedFile]] | None = None,
         progress: Callable[[int, str], None] | None = None, configurations: list = ()) -> Plan:
    """What collecting ``entries`` would copy, and what is already there. ``progress(i, model)`` is called
    before each model is read (it may raise to stop). ``configurations`` (with their files resolved) are
    kept with the copies, naming them."""
    folder = Path(library) / _folder_name(game)
    out = Plan(game, folder, [e.stem for e in entries], roots=[Path(r) for r in roots if not _is_under(Path(r), folder)])
    seen: set[Path] = set()
    moved: dict[Path, str] = {}  # archive member -> where its copy goes

    def add(f: RelatedFile, model: str) -> None:
        src = Path(f.path)
        if f.extract is not None:
            target = folder / f.save_as
            if target in seen or _is_under(src, folder):
                return
            seen.add(target)
            moved.setdefault(src, f.save_as)
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
        target = folder / relative_path(src, roots)
        if _is_under(src, folder):
            return  # already a library copy
        out.files.append(PlannedFile(src, target, f.kind, size, model, _same_file(src, target)))

    last = 0.0
    for i, e in enumerate(entries):
        if progress is not None and (i == 0 or time.monotonic() - last > 0.2):
            last = time.monotonic()
            progress(i, e.label)
        for f in related(e):
            add(f, e.stem)
    if options.extras and extras is not None:
        for root in roots:
            for f in extras(root):
                add(f, "")
    out.configurations = [moved_config(c, moved, roots, folder) for c in configurations]
    return out


def collected_path(path: str | Path, moved: dict[Path, str], roots: list[Path], folder: Path) -> str:
    """Where a model file of the game is in its collected copy, below the copy's folder (``/`` separators)."""
    p = Path(path)
    if _is_under(p, folder):
        return p.relative_to(folder).as_posix()
    if p in moved:
        return moved[p].replace("\\", "/")
    return relative_path(p, roots).as_posix()


def moved_config(config, moved: dict[Path, str], roots: list[Path], folder: Path):
    """``config`` naming the collected copies (paths below the copy's folder), its variants' files too
    (a Borderlands 2 skin's ``...|<package file>``)."""
    import dataclasses

    from . import configs

    def ref(r: configs.ModelRef) -> configs.ModelRef:
        variant = r.variant
        if variant and "|" in variant:
            head, _, file = variant.partition("|")
            if file:
                variant = f"{head}|{collected_path(file, moved, roots, folder)}"
        return configs.ModelRef(collected_path(r.file, moved, roots, folder), variant, r.parts)

    slots = [dataclasses.replace(s, options=[dataclasses.replace(o, model=ref(o.model)) for o in s.options])
             for s in config.slots]
    return dataclasses.replace(config, base=ref(config.base), slots=slots)


def write_configurations(folder: Path, game: str, found: list) -> Path:
    """Keep the configurable models collected in the copy's folder, with those collected before."""
    from . import configs
    from .errors import FormatError

    path = Path(folder) / CONFIGS
    have = []
    if path.is_file():
        try:
            have = configs.load_file(path, game)
        except FormatError:
            have = []
    ids = {c.id for c in found}
    keep = [c for c in have if c.id not in ids] + list(found)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(configs.dump(keep, game), indent=2), "utf-8")
    os.replace(tmp, path)
    return path


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
    if plan.configurations:
        write_configurations(plan.folder, plan.game, plan.configurations)
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


def write_manifest(folder: Path, game: str, models: list[str] = (), sources: list[str] = (),
                   packages: bool | None = None) -> dict:
    """Record (or bring up to date) which game a folder of copies belongs to and what it holds.
    ``packages``: the folder holds model packages (see darkstl.package) rather than copies of game files."""
    from . import __version__

    folder = Path(folder)
    old = read_manifest(folder) or {}
    data = {"kind": "collection", "app": "Dark STL", "format": 1, "game": game,
            "packages": bool(old.get("packages") if packages is None else packages),
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
    write_manifest(dst, data.get("game") or dst.name, data.get("models") or [], data.get("sources") or [],
                   bool(data.get("packages")))
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


def packages_folder(library: Path, game: str) -> Path:
    """Where a game's model packages go in the app's library folder, beside its collected copies."""
    return Path(library) / f"{_folder_name(game)} packages"


def _folder_name(game: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_." else "_" for c in game).strip(" .")
    return keep or "Game"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ["CONFIGS", "KINDS", "MANIFEST", "CollectOptions", "Plan", "PlannedFile", "RelatedFile", "collected_path",
           "collections_in", "folder_size", "move_folder", "packages_folder", "plan", "read_manifest", "relative_path",
           "run", "write_configurations", "write_manifest"]
