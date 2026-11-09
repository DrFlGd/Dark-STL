"""Grand Theft Auto V's game profile (JSON), as the RenderWare games have theirs.

``importers/profiles/gta5.json`` ships with the other games' profiles and is read by the shared loader
(:mod:`darkstl.importers.profiles`): a file of the same name in the settings folder's ``profiles/``
folder replaces it. It names the engine (``rage``) and:

- ``recognise``: files below the game folder (an archive or the executable) and folder names;
- ``language``: whose ``.gxt2`` text gives vehicle and weapon names (``american``);
- ``ped_animations``: the clip dictionaries (``.ycd``, without the extension) characters are posed with;
- ``shared_textures``: dictionaries vehicles fall back on (``vehicles``), and any model (``all``);
- ``groups``: names for vehicle classes, ped name prefixes and weapon groups in the library.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import profiles as shared

ENGINE = "rage"
find_ci = shared.find_ci


@dataclass
class Profile:
    name: str
    short: str = ""
    recognise_files: list[str] = field(default_factory=list)
    folder_names: list[str] = field(default_factory=list)
    language: str = "american"
    ped_animations: list[str] = field(default_factory=list)
    vehicle_textures: list[str] = field(default_factory=list)
    shared_textures: list[str] = field(default_factory=list)
    vehicle_classes: dict[str, str] = field(default_factory=dict)
    ped_prefixes: dict[str, str] = field(default_factory=dict)
    weapon_groups: dict[str, str] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def from_json(cls, data: dict, source: str = "") -> Profile:
        rec = data.get("recognise") or {}
        shared = data.get("shared_textures") or {}
        groups = data.get("groups") or {}
        return cls(
            name=str(data["name"]), short=str(data.get("short") or ""),
            recognise_files=[str(f) for f in rec.get("files") or []],
            folder_names=[str(f).lower() for f in rec.get("folder_names") or []],
            language=str(data.get("language") or "american").lower(),
            ped_animations=[str(a).lower() for a in data.get("ped_animations") or []],
            vehicle_textures=[str(t).lower() for t in shared.get("vehicles") or []],
            shared_textures=[str(t).lower() for t in shared.get("all") or []],
            vehicle_classes={str(k).upper(): str(v) for k, v in (groups.get("vehicle_classes") or {}).items()},
            ped_prefixes={str(k).lower(): str(v) for k, v in (groups.get("ped_prefixes") or {}).items()},
            weapon_groups={str(k).upper(): str(v) for k, v in (groups.get("weapon_groups") or {}).items()},
            source=source,
        )


def profiles() -> tuple[Profile, ...]:
    """The RAGE games' profiles, through the shared loader (shipped, or replaced by the settings folder)."""
    found = tuple(shared.profiles(ENGINE))
    known = _cache.get("from", ())
    if len(known) != len(found) or any(x is not y for x, y in zip(known, found)):
        _cache["from"] = found
        _cache["profiles"] = tuple(Profile.from_json(p.data, str(p.source or "")) for p in found)
    return _cache["profiles"]


_cache: dict = {}


def forget() -> None:
    shared.forget()
    _cache.clear()


def profile(game: str | None) -> Profile | None:
    for p in profiles():
        if p.name == game:
            return p
    return None


def game_names() -> tuple[str, ...]:
    return tuple(p.name for p in profiles())


def recognise(folder: Path) -> Profile | None:
    folder = Path(folder)
    for p in profiles():
        if any(find_ci(folder, f) is not None for f in p.recognise_files):
            return p
    return None


def detect(folder: Path) -> tuple[Profile, Path] | None:
    """The game a folder is, or is inside (up to four folders up), and that game's folder."""
    folder = Path(folder)
    for cand in [folder, *list(folder.parents)[:4]]:
        p = recognise(cand)
        if p is not None:
            return p, cand
    name = folder.name.lower()
    for p in profiles():
        if any(n == name for n in p.folder_names) and _has_files(folder):
            return p, folder
    return None


def _has_files(folder: Path) -> bool:
    try:
        return any(n.lower().endswith((".rpf", ".ydr", ".yft", ".ydd", ".ytd")) for n in os.listdir(folder))
    except OSError:
        return False
