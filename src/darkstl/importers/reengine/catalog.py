"""Group the models of an RE Engine game by what they are.

The engine files everything by path below ``natives/<platform>/``, and the folder names say what
a model is::

    character/player/pl0000/pl0000.mesh.1808312334      a character (RE2: player, enemy, npc...)
    _chainsaw/character/ch/cha0/cha000_00/...           a character (RE4: ch<series>)
    weapon/wp0000/...   _chainsaw/weapon/wp/wp1000/...  a weapon
    item/..., props/..., gimmick/..., objectroot/...    objects
    environment/..., sectionroot/..., stage/...         map pieces

Files under ``streaming/`` hold the high-detail buffers of other models, not models of their own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ...model import LOD_SUFFIX, CatalogEntry
from .files import ModelPath, is_streaming

_SKIP = {"natives", "x64", "stm", "ps4", "ps5", "nsw", "msg", "model", "models", "mesh", "mod", "ch", "wp", "sm", "it",
         "em", "pl", "ev", "sectionroot", "objectroot", "prefab"}
_GROUP_WORDS = {"player": "Player", "enemy": "Enemies", "npc": "NPCs", "em": "Enemies", "pl": "Player", "ev": "Events"}


def _segments(rel: str) -> list[str]:
    return [s for s in rel.lower().replace("\\", "/").split("/") if s]


def _group_after(segments: list[str], index: int) -> str:
    """A sub-group from the folder names after ``segments[index]``: a word such as 'player', else the
    first folder with a name longer than two characters (a series such as 'cha0' or 'wp1000')."""
    for s in segments[index + 1:-1]:
        if s in _GROUP_WORDS:
            return _GROUP_WORDS[s]
        if s not in _SKIP and len(s) > 2 and not s.startswith("_"):
            return s
    return ""


def classify(rel_path: str, stem: str) -> tuple[str, str, str, bool]:
    """``(category, group, label, is_lod)`` for a mesh at ``rel_path`` (its path below the game folder)."""
    segs = _segments(rel_path)
    s = stem.lower()
    is_lod = bool(LOD_SUFFIX.search(s))
    folders = segs[:-1] if segs else []
    for i, seg in enumerate(folders):
        if seg in ("character", "characters", "chara"):
            return "Characters", _group_after(folders + [""], i), stem, is_lod
        if seg in ("weapon", "weapons", "wp"):
            return "Weapons", _group_after(folders + [""], i), stem, is_lod
    for i, seg in enumerate(folders):
        if seg in ("item", "items", "props", "prop", "gimmick", "gimmicks", "object", "setmodel", "sm", "furniture"):
            return "Objects", _group_after(folders + [""], i), stem, is_lod
        if seg in ("environment", "env", "stage", "stages", "map", "level", "levels", "bg"):
            return "Map Pieces", _group_after(folders + [""], i), stem, is_lod
    if "objectroot" in folders:
        return "Objects", _group_after(folders + [""], folders.index("objectroot")), stem, is_lod
    for i, seg in enumerate(folders):
        if seg in ("ui", "gui", "effect", "effects", "vfx", "fx", "efx", "camera", "light"):
            return "Other", seg.upper() if len(seg) <= 3 else seg.capitalize(), stem, is_lod
    group = next((f for f in folders if f not in _SKIP and not f.startswith("_") and len(f) > 2), "")
    return "Models", group, stem, is_lod


def scan(root: Path, game: str, paths: Iterable[ModelPath], importer: str) -> list[CatalogEntry]:
    """Catalogue entries for the mesh files among ``paths`` (real files and the entries of pak archives)."""
    entries: dict[str, CatalogEntry] = {}
    for mp in paths:
        if not mp.is_mesh or is_streaming(mp.rel):
            continue
        category, group, label, is_lod = classify(mp.rel, mp.stem)
        key = mp.rel.lower()
        if key in entries:
            continue
        entries[key] = CatalogEntry(game, category, group, mp.stem, label, mp.path, importer=importer, is_lod=is_lod,
                                    packed=mp.in_pak)
    return sorted(entries.values(), key=lambda e: e.sort_key)


def group_label(group: str) -> str:
    return re.sub(r"^(\w)", lambda m: m.group(1).upper(), group)


__all__ = ["classify", "scan"]
