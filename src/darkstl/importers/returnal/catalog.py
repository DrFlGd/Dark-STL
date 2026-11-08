"""Finding a Returnal folder and listing its models.

The paks' indexes list every file. Meshes named the Unreal way (``SK_``, ``SKM_``,
``SM_``) are listed straight from the names; for other packages whose names
don't say what they hold (and don't say they are textures, materials,
animations, sounds...), the class is read from the package's header, once:
what was read is kept in the importer's cache until the paks change.

Groups follow the folders below ``Returnal/Content``: weapons, characters
(Selene, the creatures and bosses), the biomes' map pieces, and props.
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
from ..borderlands.ue4 import asset, vfs

log = logging.getLogger(__name__)

GAME = "Returnal"
PROJECT = "Returnal"  # the project folder: <install>/Returnal/Content/Paks
MAX_PEEKS = 60000  # packages whose class the scan reads, when their names don't give it
MAX_FAILED_PEEKS = 200  # give up reading headers when this many in a row can't be read (an Oodle pak with no DLL)
MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")

_MARKERS = (f"{PROJECT}/Binaries/Win64/Returnal-Win64-Shipping.exe", "Returnal-Win64-Shipping.exe", "Returnal.exe")
_SKELETAL = re.compile(r"^(sk|skm|skel_mesh)_", re.I)
_STATIC = re.compile(r"^(sm|stm)_", re.I)
_SKIP = re.compile(r"(_physics|_phys|_phy|_physicsasset|_collision|_col|_pa|_anim|_abp|_bp|_skeleton)$", re.I)
# names that say a package isn't a mesh: textures, materials, animations, blueprints, sounds, effects, UI, data
_NOT_A_MESH = re.compile(
    r"^(t|tx|tex|m|mi|mic|mf|mm|mat|mpc|pm|a|an|anim|am|as|abp|bs|bp|bpi|wbp|w|ui|ico|s|sc|sw|sfx|snd|cue|ak|"
    r"mus|vo|dt|da|ct|cv|curve|e|enum|st|f|font|ns|ne|nps|p|ps|fx|vfx|pfx|cs|ls|l|lvl|seq|skel|sks|phys|pa|"
    r"pp|ppm|lut|hlod|ndf|ds|dlg|gc|ga|ge|gd|tag|ai|bt|bb|eqs|env|rt|vt|cube|hdr|media|mp)_", re.I)
_SKIP_FOLDERS = {"audio", "sound", "sounds", "wwise", "wwiseaudio", "ui", "hud", "movies", "localization",
                 "textures", "materials", "animations", "anims", "blueprints", "fx", "vfx", "effects", "particles",
                 "niagara", "data", "datatables", "curves", "fonts", "cinematics", "sequences", "developers"}

_WEAPONS = ("weapons", "weapon", "guns", "gun", "alt_fires", "altfires")
_CHARACTERS = ("characters", "character", "chars", "player", "players", "selene", "hero", "astronaut")
_ENEMIES = ("enemies", "enemy", "creatures", "creature", "monsters", "monster", "bosses", "boss", "ai", "npc",
            "npcs", "mobs", "aliens")
_MAPS = ("environment", "environments", "env", "levels", "level", "maps", "map", "biomes", "biome", "levelart",
         "world", "architecture", "rooms", "tiles", "kits", "foliage", "vegetation", "terrain", "landscape")
_OBJECTS = ("props", "prop", "pickups", "pickup", "items", "item", "interactables", "interactable", "consumables",
            "artifacts", "artefacts", "parasites", "ship", "helios", "house", "vehicles", "objects", "gameplay")


ENGINE_FILE = f"{PROJECT}/Binaries/engine-version.txt"  # in a collected copy: the release, as 4.26
MAPPINGS_FILE = "Returnal.usmap"  # where a collected copy keeps the game's type mappings
DEFAULT_ENGINE = 425  # if the game's executable doesn't say: Returnal's PS5 release came out on Unreal 4.25/4.26
_BRANCH = re.compile(rb"\+\+UE4\+Release-4\.(\d\d)|\+\x00\+\x00U\x00E\x004\x00\+\x00R\x00e\x00l\x00e\x00a\x00s\x00e\x00"
                     rb"-\x004\x00\.\x00(\d)\x00(\d)\x00")  # the engine writes it as UTF-16 (TCHAR) on Windows


def engine_version(root: Path) -> int:
    """The Unreal Engine release Returnal was built with (425 for 4.25), read from the branch name the engine
    writes into the game's executable (``++UE4+Release-4.25``). Returnal cooks its packages unversioned, so
    this is what says their layouts."""
    root = root_for(Path(root))
    try:  # a collected copy has no executable: Collect writes down what it said
        text = (root / ENGINE_FILE).read_text("utf-8").strip()
        major, minor = text.split(".")[:2]
        if major == "4" and 400 + int(minor) in asset.UE4_FILE_VERSIONS:
            return 400 + int(minor)
    except (OSError, ValueError):
        pass
    for exe in (root / PROJECT / "Binaries" / "Win64" / "Returnal-Win64-Shipping.exe",
                *sorted((root / PROJECT / "Binaries" / "Win64").glob("*-Shipping.exe"))):
        found = _branch_in(exe)
        if found:
            return found
    return DEFAULT_ENGINE


def _branch_in(exe: Path) -> int | None:
    try:
        with open(exe, "rb") as f:
            tail = b""
            while True:
                block = f.read(1 << 24)
                if not block:
                    return None
                m = _BRANCH.search(tail + block)
                if m:
                    minor = int(m.group(1) or (m.group(2) + m.group(3)))
                    return 400 + minor if 20 <= minor <= 27 else None
                tail = block[-128:]
    except OSError:
        return None


vfs.ENGINE_OF_GAME[GAME] = engine_version
vfs.LOOK_THROUGH_FOR_ANIMATIONS.add(GAME)  # what its animations are named isn't known


def _label(folder: str) -> str:
    return folder.replace("_", " ").strip()


def detect_game(folder: Path) -> str | None:
    """Returnal when the folder is its install folder (or the project or pak folder inside it)."""
    folder = Path(folder)
    for candidate in [folder, *list(folder.parents)[:3]]:
        if any((candidate / m).exists() for m in _MARKERS):
            return GAME
        if candidate.name.lower() == PROJECT.lower() and (candidate / "Content" / "Paks").is_dir() \
                and candidate.parent.name.lower() != "content":
            return GAME
        if (candidate / PROJECT / "Content" / "Paks").is_dir():
            return GAME
    # files extracted (FModel, UModel) or collected by Dark STL: <somewhere>/Returnal/Content/...uasset
    if (folder / PROJECT / "Content").is_dir() or (folder.name.lower() == PROJECT.lower()
                                                     and (folder / "Content").is_dir()):
        return GAME
    return None


def is_returnal_path(path: Path) -> bool:
    """Whether a model path lies in Returnal's files (``.../Returnal/Content/...``)."""
    parts = [p.lower() for p in Path(path).parts]
    for i in range(len(parts) - 1):
        if parts[i] == PROJECT.lower() and parts[i + 1] == "content":
            return True
    return False


def root_for(folder: Path) -> Path:
    """The install folder above the project folder, so virtual paths read ``Returnal/Content/...``."""
    folder = Path(folder)
    for d in [folder, *folder.parents][:5]:
        if d.name.lower() == "paks" and d.parent.name.lower() == "content":
            return d.parent.parent.parent
    if folder.name.lower() == PROJECT.lower() and (folder / "Content").is_dir():
        return folder.parent
    return folder


def classify(virtual: str, cls: str | None = None) -> tuple[str, str, str] | None:
    """(class, category, group) for a model file; None when its name doesn't say it is a mesh and ``cls``
    (the class read from the package) doesn't either."""
    parts = virtual.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    if cls is None:
        if _SKELETAL.match(stem):
            cls = "SkeletalMesh"
        elif _STATIC.match(stem):
            cls = "StaticMesh"
        else:
            return None
    if cls not in MODEL_CLASSES or _SKIP.search(stem):
        return None
    low = [p.lower() for p in parts[:-1]]
    try:
        content = len(low) - 1 - low[::-1].index("content")
    except ValueError:
        content = 0
    sub = [p for p in parts[content + 1:-1]]
    sl = [p.lower() for p in sub]

    def after(words: tuple[str, ...], n: int = 1) -> list[str] | None:
        for i, p in enumerate(sl):
            if p in words:
                return [_label(x) for x in sub[i + 1:i + 1 + n] if not x.startswith("_")
                        and x.lower() not in ("meshes", "mesh", "models", "model", "skeletalmeshes", "staticmeshes",
                                              "common", "shared")]
        return None

    s = stem.lower()
    if (rest := after(_WEAPONS)) is not None:
        return cls, "Weapons", rest[0] if rest else "Weapons"
    if (rest := after(_ENEMIES)) is not None:
        return cls, "Characters", "Creatures" + (f" / {rest[0]}" if rest else "")
    if (rest := after(_CHARACTERS)) is not None:
        if any(w in s for w in ("head", "hair", "face", "helmet")):
            return cls, "Faces & Hair", rest[0] if rest else ""
        return cls, "Characters", rest[0] if rest else ""
    if (rest := after(_MAPS)) is not None:
        return cls, "Map Pieces", rest[0] if rest else _label(sub[0]) if sub else ""
    if (rest := after(_OBJECTS)) is not None:
        return cls, "Objects", _label(sub[0]) + (f" / {rest[0]}" if rest else "")
    if cls == "StaticMesh":
        return cls, "Objects", _label(sub[0]) if sub else ""
    return cls, "Characters", _label(sub[0]) if sub else ""


def worth_peeking(virtual: str) -> bool:
    """Whether a package whose name doesn't say its class might hold a mesh."""
    low = virtual.lower()
    if not low.endswith(".uasset"):
        return False
    stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if _NOT_A_MESH.match(stem) or _SKIP.search(stem):
        return False
    folders = low.split("/")[:-1]
    return not any(f in _SKIP_FOLDERS for f in folders)


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
    h = hashlib.sha1()
    for p in vfs.find_paks(gf.root):
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


def scan(folder: Path, progress: Callable[[int, str], None] | None = None,
         cache: Path | None = None) -> list[CatalogEntry]:
    root = root_for(folder)
    gf = vfs.game_files(root, GAME, refresh=True)
    names = gf.assets()
    if gf.zen is not None:
        names += gf.zen.assets()
    packed = bool(gf.paks or gf.zen is not None)
    fingerprint = _fingerprint(gf) if packed else ""
    file = _cache_file(cache, root) if packed else None
    known = _load_classes(file, fingerprint)
    peeked, failed, changed = 0, 0, False
    out = []
    for i, virtual in enumerate(names):
        if progress and i % 2000 == 0:
            progress(i, virtual)
        hit = classify(virtual)
        if hit is None and worth_peeking(virtual):
            key = virtual.lower()
            if key in known:
                cls = known[key] or None
            elif peeked < MAX_PEEKS and failed < MAX_FAILED_PEEKS:
                peeked += 1
                try:
                    cls = _class_of(gf, virtual)
                    failed = 0
                except (FormatError, OSError, ValueError, IndexError) as e:
                    log.debug("Couldn't read the class of %s: %s", virtual, e)
                    cls, failed = None, failed + 1
                known[key] = cls or ""
                changed = True
            else:
                cls = None
            if cls in MODEL_CLASSES:
                hit = classify(virtual, cls)
        if hit is None:
            continue
        _cls, category, group = hit
        path = gf.root / virtual
        stem = path.stem
        label = re.sub(r"^(SK|SKM|SM)_", "", stem, flags=re.I).replace("_", " ")
        out.append(CatalogEntry(GAME, category, group, stem, label, path, importer="returnal",
                                is_lod=bool(LOD_SUFFIX.search(stem)), packed=not path.is_file()))
    if changed:
        _save_classes(file, fingerprint, known)
    return out


def _needs_mappings(gf: vfs.GameFiles, found: list[CatalogEntry]) -> bool:
    """Whether the game's packages are cooked without property names and no type mappings for it were found."""
    from ..borderlands.ue4 import unversioned, usmap

    try:
        virtual = Path(found[0].path).relative_to(gf.root).as_posix()
        pkg = asset.read_package(vfs.package_name(virtual) or virtual, gf.read(virtual),
                                 assume_engine=gf.assumed_engine)
    except (FormatError, OSError, ValueError):
        return False
    if not pkg.package_flags & asset.PKG_UNVERSIONED_PROPERTIES:
        return False
    return usmap.find_mappings(gf.root, GAME, unversioned._settings_dir()) is None


def folder_notes(folder: Path, found: list[CatalogEntry]) -> list[str]:
    gf = vfs.game_files(root_for(Path(folder)), GAME)
    notes = []
    if gf.locked:
        names = ", ".join(p.name for p in gf.locked[:3]) + (" and others" if len(gf.locked) > 3 else "")
        notes.append(f"{len(gf.locked)} of Returnal's .pak files are encrypted and no AES key Dark STL knows opens "
                     f"them ({names}). Dark STL doesn't come with Returnal's key: add it (0x followed by 64 "
                     f"hexadecimal digits) in Settings under Unreal games, or in a file named {vfs.KEY_FILE} in the "
                     "game's folder, then rescan.")
    for err in gf.errors[:3]:
        notes.append(err)
    if found and _needs_mappings(gf, found):
        notes.append("Returnal is cooked without property names. Its models open without them, with each "
                     "material's textures picked by their names; the game's type mappings (a .usmap file for the "
                     "installed version, added in Settings under Unreal games or put in the game's folder) let "
                     "Dark STL read the materials' own settings instead.")
    if not found and not gf.locked:
        if gf.paks or gf.zen is not None:
            notes.append("No meshes were found in Returnal's files.")
        elif not gf.files:
            notes.append(f"No .pak files were found: choose Returnal's install folder (the one with {PROJECT}\\"
                         "Content\\Paks in it).")
    return notes


__all__ = ["GAME", "classify", "detect_game", "folder_notes", "is_returnal_path", "root_for", "scan",
           "worth_peeking"]
