"""Scan a Borderlands (Unreal 3) folder: every skeletal and static mesh in its packages, grouped.

Packages are opened only as far as their tables; what each holds is kept in the
importer's cache (one small JSON file per package, keyed by size and date), so
a rescan only opens packages that changed. Cooked packages carry copies of the
objects they use, so a mesh found in several packages is listed once, from the
package it belongs to when that one is there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ...errors import FormatError
from ...model import LOD_SUFFIX, CatalogEntry
from . import files
from .formats import upk

VERSION = 2
# what materials need too, so an object cooked into some other package can be found (see object_index)
MATERIAL_CLASSES = ("MaterialInstanceConstant", "MaterialInstanceTimeVarying", "Material")
LOOKUP_CLASSES = {"Material", "MaterialInstanceConstant", "MaterialInstanceTimeVarying", "Texture2D",
                  "TextureFlipBook"}
KEEP_CLASSES = set(files.MODEL_CLASSES) | {"GestaltSkeletalMeshDefinition", "WeaponPartDefinition",
                                           "WeaponTypeDefinition"} | LOOKUP_CLASSES

_WEAPON_KINDS = [
    (re.compile(r"pistol", re.I), "Pistols"), (re.compile(r"smg|submachine", re.I), "SMGs"),
    (re.compile(r"assault|(^|[._])ar[._]", re.I), "Assault rifles"), (re.compile(r"shotgun", re.I), "Shotguns"),
    (re.compile(r"sniper", re.I), "Sniper rifles"), (re.compile(r"launcher|rocket", re.I), "Launchers"),
    (re.compile(r"shield", re.I), "Shields"), (re.compile(r"grenade", re.I), "Grenades"),
    (re.compile(r"artifact|relic", re.I), "Relics"), (re.compile(r"melee|axe|sword|knife", re.I), "Melee"),
]
_WEAPON = re.compile(r"weap|gun|gestalt|pistol|smg|shotgun|sniper|launcher|rifle|grenade|shield|artifact", re.I)
_HEAD = re.compile(r"head|hair|face", re.I)
_VEHICLE = re.compile(r"vehicle|runner|technical|buggy|hovercraft|fan_?boat", re.I)
_MAP = re.compile(r"(_p|_dynamic|_combat|_light|_lighting|_geo|_art|_fx|_audio|_mission\w*|_ld|_skybox)$", re.I)


def _cache_file(cache: Path | None, path: Path) -> Path | None:
    if cache is None:
        return None
    h = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:20]
    return cache / "packages" / f"{h}.json"


def package_contents(path: Path, cache: Path | None = None) -> list[dict]:
    """The objects of interest in one package: ``{"path", "canonical", "class", "name"}``."""
    st = path.stat()
    cf = _cache_file(cache, path)
    if cf is not None and cf.is_file():
        try:
            data = json.loads(cf.read_text("utf-8"))
            same = data.get("size") == st.st_size and data.get("mtime") == st.st_mtime_ns
            if data.get("version") == VERSION and same:
                return data["objects"]
        except (OSError, ValueError, KeyError):
            pass
    pkg = upk.open_package(path)
    objects = []
    for e in pkg.exports:
        if e.class_name in KEEP_CLASSES and not e.name.startswith("Default__"):
            objects.append({"path": e.path, "canonical": files.canonical_path(pkg, e), "class": e.class_name,
                            "name": e.name})
    if cf is not None:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps({"version": VERSION, "size": st.st_size, "mtime": st.st_mtime_ns,
                                      "package": str(path), "objects": objects}), "utf-8")
        except OSError:
            pass
    return objects


class ObjectIndex:
    """Where the materials and textures of a game folder are, by their original path: cooked packages
    carry copies of what they use, so an object a package only imports may sit in any other package.

    An object is found by its original path (``Common_GunMaterials.Materials.Mati_X``), else by its path in
    the package that holds a copy (a copy whose outer isn't flagged as another package's), else by its name
    and class, the copy whose path ends the most like the one wanted winning."""

    def __init__(self, root: Path, cache: Path | None):
        self.where: dict[str, tuple[str, str, str]] = {}  # canonical path -> (package file, export path, class)
        self.by_path: dict[str, tuple[str, str, str]] = {}  # path inside the package that holds it -> same
        self.by_name: dict[tuple[str, str], list[tuple[str, str]]] = {}  # (name, class) -> [(package, path)]
        self.packages = 0  # how many packages were read
        for path in files.iter_packages(root):
            try:
                objects = package_contents(path, cache)
            except (FormatError, OSError, ValueError):
                continue
            self.packages += 1
            home = path.stem.lower()
            for o in objects:
                if o["class"] not in LOOKUP_CLASSES:
                    continue
                entry = (str(path), o["path"], o["class"])
                key = o["canonical"].lower()
                prev = self.where.get(key)
                # the package the object belongs to wins over copies of it
                if prev is None or (key.split(".", 1)[0] == home and Path(prev[0]).stem.lower() != home):
                    self.where[key] = entry
                self.by_path.setdefault(o["path"].lower(), entry)
                same = self.by_name.setdefault((o["name"].lower(), o["class"]), [])
                if len(same) < 64 and not any(p == o["path"] for _f, p in same):
                    same.append((str(path), o["path"]))

    def find(self, path: str, class_name: str | None = None) -> tuple[Path, str] | None:
        low = path.lower()
        for table in (self.where, self.by_path):
            hit = table.get(low)
            if hit is not None and (not class_name or hit[2] == class_name):
                return Path(hit[0]), hit[1]
        if not class_name:
            return None
        want = low.split(".")
        # a material's copy may be of another material class than the reference says (a parent that's a
        # MaterialInstanceTimeVarying where a MaterialInstanceConstant was named, say): those count too, after
        classes = [class_name] + ([c for c in MATERIAL_CLASSES if c != class_name] if class_name in MATERIAL_CLASSES
                                  else [])
        for cls in classes:
            best, best_score = None, 0
            for file, p in self.by_name.get((want[-1], cls), []):
                have = p.lower().split(".")
                score = 0
                while score < min(len(want), len(have)) and want[-1 - score] == have[-1 - score]:
                    score += 1
                if score > best_score:
                    best, best_score = (Path(file), p), score
            if best is not None:
                return best
        return None


_OBJECT_INDEXES: dict[str, ObjectIndex] = {}
_OBJECT_LOCK = threading.Lock()


def object_index(root: Path, cache: Path | None) -> ObjectIndex:
    """The game folder's :class:`ObjectIndex`, made on first use (from the scan's cached package lists)."""
    key = os.path.normcase(str(root))
    with _OBJECT_LOCK:
        idx = _OBJECT_INDEXES.get(key)
        if idx is None:
            idx = _OBJECT_INDEXES[key] = ObjectIndex(Path(root), cache)
    return idx


def forget_object_index(root: Path | None = None) -> None:
    with _OBJECT_LOCK:
        if root is None:
            _OBJECT_INDEXES.clear()
        else:
            _OBJECT_INDEXES.pop(os.path.normcase(str(root)), None)


def classify(obj: dict, package: Path, gestalt: bool = False) -> tuple[str, str]:
    canonical = obj["canonical"]
    top = canonical.split(".", 1)[0]
    text = canonical
    if obj["class"] == "SkeletalMesh" and gestalt:
        return "Weapons", _weapon_kind(text) or "Weapon parts"
    if _WEAPON.search(text):
        return "Weapons", _weapon_kind(text) or "Other weapons"
    if _VEHICLE.search(text):
        return "Objects", "Vehicles"
    if obj["class"] == "SkeletalMesh":
        if _HEAD.search(obj["name"]):
            return "Faces & Hair", _group_name(top)
        return "Characters", _group_name(top)
    if _MAP.search(package.stem):
        return "Map Pieces", _map_name(package.stem)
    return "Objects", _group_name(top)


def _weapon_kind(text: str) -> str | None:
    for rx, kind in _WEAPON_KINDS:
        if rx.search(text):
            return kind
    return None


def _group_name(top: str) -> str:
    name = re.sub(r"^(gd|cd|sk|sm|pc|hud|fx)_", "", top, flags=re.I)
    name = re.sub(r"_(streaming|data|meshes?|mats?|assets|content|skins?)$", "", name, flags=re.I)
    return name.replace("_", " ") or top


def _map_name(stem: str) -> str:
    return _MAP.sub("", stem).replace("_", " ")


def scan(root: str | os.PathLike, game: str, progress: Callable[[int, str], None] | None = None,
         cache: Path | None = None) -> list[CatalogEntry]:
    root = Path(root)
    forget_object_index(root)
    packages = list(files.iter_packages(root))
    found: dict[str, tuple[dict, Path]] = {}
    gestalt_meshes: set[str] = set()
    last = time.monotonic()
    for i, path in enumerate(packages):
        if progress and (i % 20 == 0 or time.monotonic() - last > 0.5):
            last = time.monotonic()
            progress(i, f"{path.name} ({i + 1:,} of {len(packages):,} packages)")
        try:
            objects = package_contents(path, cache)
        except (FormatError, OSError, ValueError):
            continue
        for obj in objects:
            if obj["class"] == "GestaltSkeletalMeshDefinition":
                # the gestalt mesh sits beside its definition (Weap_Pistol.GestaltDef_Pistol -> Weap_Pistol.*)
                gestalt_meshes.add(obj["canonical"].rsplit(".", 1)[0].lower())
            if obj["class"] not in files.MODEL_CLASSES:
                continue
            key = obj["canonical"].lower()
            prev = found.get(key)
            home = obj["canonical"].split(".", 1)[0].lower() == path.stem.lower()
            if prev is None or (home and prev[1].stem.lower() != key.split(".", 1)[0]):
                found[key] = (obj, path)
    entries = []
    for key, (obj, path) in found.items():
        category, group = classify(obj, path, key.rsplit(".", 1)[0] in gestalt_meshes)
        label = obj["name"]
        entries.append(CatalogEntry(game, category, group, obj["name"], label,
                                    files.member_path(path, obj["path"], obj["class"]), "borderlands",
                                    is_lod=bool(LOD_SUFFIX.search(obj["name"])), packed=True))
    return sorted(entries, key=lambda e: e.sort_key)


def folder_notes(root: Path, found: list[CatalogEntry]) -> list[str]:
    ours = [e for e in found if e.importer == "borderlands"]
    notes = []
    if not ours:
        n = sum(1 for _ in files.iter_packages(root, limit=1))
        if n:
            notes.append("No models were found in this folder's packages. Add the game's WillowGame folder "
                         "(or the whole install) so the packages that hold the models are included.")
    return notes


__all__ = ["ObjectIndex", "classify", "folder_notes", "forget_object_index", "object_index", "package_contents",
           "scan"]
