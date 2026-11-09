"""Finding an Unreal 4/5 game's folder, listing its models and sorting them, by the game's profile.

The paks' (and IoStore containers') indexes list every file. Meshes named the Unreal way (``SK_``,
``SM_``, as the profile says) are listed straight from the names; packages whose names don't say what
they hold may have their class read from their headers, once: what was read is kept in the importer's
cache until the paks change. Kinds and groups follow the profile's sorting rules
(:class:`~darkstl.importers.unreal.profile.Sorting`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from ...errors import FormatError
from ...model import LOD_SUFFIX, CatalogEntry
from .. import profiles as game_profiles
from ..borderlands.ue4 import asset, vfs
from .profile import MODEL_CLASSES, UnrealProfile, projects_shared

log = logging.getLogger(__name__)

MAX_FAILED_PEEKS = 200  # give up reading headers when this many in a row can't be read (an Oodle pak with no DLL)
ENGINE_FILE_NAME = "engine-version.txt"  # in a collected copy: <project>/Binaries/engine-version.txt, as 4.26
_BRANCH = re.compile(rb"\+\+UE([45])\+Release-\1\.(\d\d?)(?!\d)|"
                     rb"\+\x00\+\x00U\x00E\x00([45])\x00\+\x00R\x00e\x00l\x00e\x00a\x00s\x00e\x00-\x00[45]\x00"
                     rb"\.\x00(\d)\x00(\d)?\x00?")  # the engine writes it as UTF-16 (TCHAR) on Windows


# -- where the game's files are --------------------------------------------------------------------------


def is_unreal_folder(folder: Path) -> bool:
    """Whether a folder holds an Unreal 4/5 game (paks, IoStore containers) or files extracted from one."""
    from ..borderlands.files import is_unreal4_folder

    return is_unreal4_folder(Path(folder))


def root_for(folder: Path, profile: UnrealProfile | None = None) -> Path:
    """The install folder above the project folder, so virtual paths read ``<Project>/Content/...``."""
    folder = Path(folder)
    for d in [folder, *folder.parents][:5]:
        if d.name.lower() == "paks" and d.parent.name.lower() == "content":
            return d.parent.parent.parent
    project = (profile.project if profile is not None else "").lower()
    if (folder / "Content" / "Paks").is_dir() and folder.name.lower() != "engine":
        return folder.parent  # the project folder itself
    if project and folder.name.lower() == project and (folder / "Content").is_dir():
        return folder.parent
    return folder


def find_project(root: Path) -> str:
    """The project folder's name below an install folder (the one with ``Content/Paks``; else ``Content``)."""
    root = Path(root)
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name.lower())
    except OSError:
        return ""
    for want_paks in (True, False):
        for d in dirs:
            if d.name.lower() in ("engine", "binaries"):
                continue
            if (d / "Content" / ("Paks" if want_paks else "")).is_dir():
                return d.name
    return ""


def executable(root: Path, profile: UnrealProfile | None = None) -> Path | None:
    """The game's executable: the profile's, else ``<Project>/Binaries/Win64/*-Shipping.exe`` (or any .exe)."""
    root = Path(root)
    if profile is not None:
        for f in profile.recognise_files:
            if f.lower().endswith(".exe"):
                found = game_profiles.find_ci(root, f)
                if found is not None and found.is_file():
                    return found
    project = (profile.project if profile is not None and profile.project else "") or find_project(root)
    bins = game_profiles.find_ci(root, f"{project}/Binaries/Win64") if project else None
    if bins is not None and bins.is_dir():
        exes = sorted(bins.glob("*.exe"), key=lambda p: (not p.stem.lower().endswith("-shipping"), p.name.lower()))
        if exes:
            return exes[0]
    return None


def recognises(profile: UnrealProfile, folder: Path) -> bool:
    """Whether ``folder`` is the profile's game: its install folder (or a folder inside it, up to three
    up), a folder of files extracted from it or collected by Dark STL, or a folder named as it says."""
    folder = Path(folder)
    project = profile.project.lower()
    unique = bool(project) and not projects_shared(profile.project)
    for candidate in [folder, *list(folder.parents)[:3]]:
        if any(game_profiles.find_ci(candidate, f) is not None for f in profile.recognise_files):
            return True
        if unique:
            if candidate.name.lower() == project and (candidate / "Content" / "Paks").is_dir() \
                    and candidate.parent.name.lower() != "content":
                return True
            if game_profiles.find_ci(candidate, f"{profile.project}/Content/Paks") is not None:
                return True
    if unique:  # files extracted (FModel, UModel) or collected by Dark STL: <somewhere>/<Project>/Content/...
        if game_profiles.find_ci(folder, f"{profile.project}/Content") is not None \
                or (folder.name.lower() == project and (folder / "Content").is_dir()):
            return True
    name = folder.name.lower()
    if (name in profile.folder_names or (profile.folder_pattern and profile.folder_pattern.search(folder.name))) \
            and is_unreal_folder(folder):
        return True
    return False


def is_profile_path(profile: UnrealProfile, path: Path) -> bool:
    """Whether a model path lies in the profile's game's files (``.../<Project>/Content/...``)."""
    project = profile.project.lower()
    if not project:
        return False
    parts = [p.lower() for p in Path(path).parts]
    return any(parts[i] == project and parts[i + 1] == "content" for i in range(len(parts) - 1))


# -- the engine release ------------------------------------------------------------------------------------


def engine_file(root: Path, profile: UnrealProfile | None = None) -> str:
    """Where a collected copy keeps the engine release, below its folder: ``<Project>/Binaries/...``."""
    project = (profile.project if profile is not None and profile.project else "") or find_project(root) or "Game"
    return f"{project}/Binaries/{ENGINE_FILE_NAME}"


def engine_version(root: Path, profile: UnrealProfile | None = None) -> int | None:
    """The Unreal Engine release the game was built with (425 for 4.25): as a collected copy wrote it down,
    as the profile fixes it, as the branch name the engine writes into the game's executable says
    (``++UE4+Release-4.25``), or the profile's default. Games that cook their packages unversioned need it
    to read their layouts."""
    root = root_for(Path(root), profile)
    try:  # a collected copy has no executable: Collect writes down what it said
        found = game_profiles.find_ci(root, engine_file(root, profile))
        if found is not None:
            v = UnrealProfile.parse_release(found.read_text("utf-8"))
            if v and v in asset.UE4_FILE_VERSIONS:
                return v
    except OSError:
        pass
    if profile is not None and profile.release and profile.release != "auto":
        v = UnrealProfile.parse_release(profile.release)
        if v:
            return v
    exe = executable(root, profile)
    if exe is not None:
        found = branch_in(exe)
        if found:
            return found
    if profile is not None and profile.default_release:
        return UnrealProfile.parse_release(profile.default_release)
    return None


def branch_in(exe: Path) -> int | None:
    """The engine release an executable names in its build branch (``++UE4+Release-4.26``), or None."""
    try:
        with open(exe, "rb") as f:
            tail = b""
            while True:
                block = f.read(1 << 24)
                if not block:
                    return None
                m = _BRANCH.search(tail + block)
                if m:
                    if m.group(1):
                        major, minor = int(m.group(1)), int(m.group(2))
                    else:
                        major, minor = int(m.group(3)), int(m.group(4) + (m.group(5) or b""))
                    if major == 4:
                        return 400 + minor if 20 <= minor <= 27 else None
                    return 500 + minor if minor < 10 else None
                tail = block[-128:]
    except OSError:
        return None


# -- listing the models ------------------------------------------------------------------------------------


def _class_of(gf: vfs.GameFiles, virtual: str) -> str | None:
    """The class of a package's main export, from its header (the ``.uasset`` alone)."""
    if gf.zen is not None and not gf.exists(virtual):
        return gf.zen.class_of(virtual)
    data = gf.read(virtual)
    name = vfs.package_name(virtual) or virtual
    pkg = asset.read_package(name, data, assume_engine=gf.assumed_engine)
    stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    best = None
    for e in pkg.exports:
        if e.outer == 0:
            if e.name.lower() == stem:
                return e.class_name
            best = best or e.class_name
    return best


def _fingerprint(gf: vfs.GameFiles) -> str:
    from ..borderlands.ue4 import iostore

    h = hashlib.sha1()
    for p in vfs.find_paks(gf.root) + (iostore.find_tocs(gf.root) if gf.zen is not None else []):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def _cache_file(cache: Path | None, root: Path) -> Path | None:
    if cache is None:
        return None
    tag = hashlib.sha1(os.path.normcase(str(root)).encode()).hexdigest()[:16]
    return Path(cache) / f"classes-{tag}.json"


def _load_classes(file: Path | None, fingerprint: str) -> dict[str, str]:
    if file is None or not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("fingerprint") != fingerprint:
        return {}
    return dict(data.get("classes", {}))


def _save_classes(file: Path | None, fingerprint: str, classes: dict[str, str]) -> None:
    if file is None:
        return
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fingerprint": fingerprint, "classes": classes}), "utf-8")
        tmp.replace(file)
    except OSError:
        log.warning("Couldn't keep what the scan read in %s", file, exc_info=True)


def scan(root: Path, game: str, profile: UnrealProfile, importer: str,
         progress: Callable[[int, str], None] | None = None, cache: Path | None = None) -> list[CatalogEntry]:
    """Every model the game's files hold, sorted by the profile's rules, as entries of ``importer`` listed under
    ``game`` (the name the user gave the folder)."""
    sorting = profile.sorting
    gf = vfs.game_files(Path(root), profile.name, refresh=True)
    names = gf.assets()
    if gf.zen is not None:
        names += gf.zen.assets()
    packed = bool(gf.paks or gf.zen is not None)
    fingerprint = _fingerprint(gf) if packed else ""
    file = _cache_file(cache, gf.root) if packed else None
    known = _load_classes(file, fingerprint)
    peeked, failed, changed = 0, 0, False
    out = []
    for i, virtual in enumerate(names):
        if progress and i % 2000 == 0:
            progress(i, virtual)
        hit = sorting.classify(virtual)
        if hit is None and sorting.worth_peeking(virtual, gf.zen is not None and not gf.exists(virtual)):
            key = virtual.lower()
            if key in known:
                cls = known[key] or None
            elif peeked < sorting.peek.limit and failed < MAX_FAILED_PEEKS:
                peeked += 1
                try:
                    cls = _class_of(gf, virtual)
                    failed = 0
                except (FormatError, OSError, ValueError, IndexError, KeyError) as e:
                    log.debug("Couldn't read the class of %s: %s", virtual, e)
                    cls, failed = None, failed + 1
                known[key] = cls or ""
                changed = True
            else:
                cls = None
            if cls in MODEL_CLASSES:
                hit = sorting.classify(virtual, cls)
        if hit is None:
            continue
        _cls, category, group = hit
        path = gf.root / virtual
        stem = path.stem
        out.append(CatalogEntry(game, category, group, stem, sorting.label(stem), path, importer=importer,
                                is_lod=bool(LOD_SUFFIX.search(stem)), packed=not path.is_file()))
    if changed:
        _save_classes(file, fingerprint, known)
    return out


def _needs_mappings(gf: vfs.GameFiles, found: list[CatalogEntry], game: str) -> bool:
    """Whether the game's packages are cooked without property names and no type mappings for it were found."""
    from ..borderlands.ue4 import unversioned, usmap

    try:
        virtual = Path(found[0].path).relative_to(gf.root).as_posix()
        if gf.zen is not None and not gf.exists(virtual):
            return usmap.find_mappings(gf.root, game, unversioned._settings_dir()) is None
        pkg = asset.read_package(vfs.package_name(virtual) or virtual, gf.read(virtual),
                                 assume_engine=gf.assumed_engine)
    except (FormatError, OSError, ValueError):
        return False
    if not pkg.package_flags & asset.PKG_UNVERSIONED_PROPERTIES:
        return False
    return usmap.find_mappings(gf.root, game, unversioned._settings_dir()) is None


def _unknown_release(gf: vfs.GameFiles, found: list[CatalogEntry]) -> bool:
    """Whether the game cooks its packages unversioned and which engine release it was built with isn't known."""
    for e in found[:1]:
        try:
            virtual = Path(e.path).relative_to(gf.root).as_posix()
            if gf.zen is not None and not gf.exists(virtual):
                return False
            data = gf.read(virtual)
        except (FormatError, OSError, ValueError):
            return False
        try:
            asset.read_package(vfs.package_name(virtual) or virtual, data, assume_engine=gf.assumed_engine)
        except FormatError as exc:
            return "unversioned" in str(exc)
    return False


def folder_notes(root: Path, game: str, profile: UnrealProfile, found: list[CatalogEntry]) -> list[str]:
    gf = vfs.game_files(Path(root), profile.name)
    notes = []
    if gf.locked:
        names = ", ".join(p.name for p in gf.locked[:3]) + (" and others" if len(gf.locked) > 3 else "")
        notes.append(f"{len(gf.locked)} of {game}'s .pak files are encrypted and no AES key Dark STL knows opens "
                     f"them ({names}). Dark STL doesn't come with {game}'s key: add it (0x followed by 64 "
                     f"hexadecimal digits) in Settings under Unreal games, or in a file named {vfs.KEY_FILE} in "
                     "the game's folder, then rescan.")
    for err in gf.errors[:3]:
        notes.append(err)
    if found and _unknown_release(gf, found):
        notes.append(f"{game} is cooked without version numbers and its executable doesn't say which Unreal "
                     "Engine release it was built with, so its models can't be read yet. Write the release "
                     "(for example \"release\": \"4.26\") in the unreal section of its game profile "
                     f"({profile.source or 'Settings, Game profiles'}), then rescan.")
    elif found and _needs_mappings(gf, found, profile.name):
        notes.append(f"{game} is cooked without property names. Its models open without them, with each "
                     "material's textures picked by their names; the game's type mappings (a .usmap file for the "
                     "installed version, added in Settings under Unreal games or put in the game's folder) let "
                     "Dark STL read the materials' own settings instead.")
    if not found and not gf.locked:
        if gf.paks or gf.zen is not None:
            notes.append(f"No meshes were found in {game}'s files.")
        elif not gf.files:
            where = f"{profile.project}\\Content\\Paks" if profile.project else "<Project>\\Content\\Paks"
            notes.append(f"No .pak files were found: choose {game}'s install folder (the one with {where} in it).")
    return notes


def probe(folder: Path) -> dict | None:
    """What a folder of an Unreal game says about itself, for adding it as a game no profile names:
    {"project", "name" (a name to suggest), "engine" (unreal4 or unreal5), "executable"}; None when the
    folder doesn't hold an Unreal game."""
    folder = Path(folder)
    if not is_unreal_folder(folder):
        return None
    root = root_for(folder)
    project = find_project(root)
    exe = executable(root)
    from ..borderlands.ue4 import iostore

    try:
        zen = bool(iostore.find_tocs(root))
    except OSError:
        zen = False
    name = root.name if root.name.lower() not in ("paks", "content", "win64", "windows", "windowsnoeditor") \
        else (project or folder.name)
    if project and name.lower() == project.lower() and exe is not None:
        name = re.sub(r"(-Win64)?-Shipping$", "", exe.stem, flags=re.I) or name
    return {"project": project, "name": name.replace("_", " ").strip(), "engine": "unreal5" if zen else "unreal4",
            "executable": exe.relative_to(root).as_posix() if exe is not None else None, "root": str(root)}


def new_profile(name: str, info: dict) -> dict:
    """A profile for a game no profile names, from what :func:`probe` found: recognised by its executable (or
    its project's pak folder), sorted by the defaults (``unreal-defaults.json``)."""
    files = [info["executable"]] if info.get("executable") else []
    if info.get("project"):
        files.append(f"{info['project']}/Content/Paks")
    return {
        "name": name,
        "engine": info.get("engine") or "unreal4",
        "recognise": {"files": files},
        "unreal": {"project": info.get("project") or "", "release": "auto", "aes_keys": []},
        "notes": "Made by Dark STL when the game's folder was added. Models are sorted by the rules in "
                 "unreal-defaults.json; add a \"models\" section here to sort them your own way "
                 "(see docs/profiles.md).",
    }


__all__ = ["ENGINE_FILE_NAME", "branch_in", "engine_file", "engine_version", "executable", "find_project",
           "folder_notes", "is_profile_path", "is_unreal_folder", "new_profile", "probe", "recognises", "root_for",
           "scan"]
