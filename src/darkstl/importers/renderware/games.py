"""What differs between GTA III, Vice City and San Andreas: one game profile (JSON) each.

The profiles ship with the other games' in ``importers/profiles/`` (``gta3.json``, ``gtavc.json``,
``gtasa.json``) and are read by the shared loader (:mod:`darkstl.importers.profiles`): a file of the same
name in the settings folder's ``profiles/`` folder replaces one, and a new file with ``"engine":
"renderware"`` adds a game. Each names its engine backend (``renderware``) and:

- ``recognise``: files (below the game folder) and folder names that say a folder is the game;
- ``level_files``: the level lists naming the game's ``.ide`` and ``.ipl`` files, in load order;
- ``text``: the game's text (``.gxt``), for vehicle names; ``car_colours``: ``carcols.dat``;
- ``ped_animations``: the animation package every ped is posed with;
- ``paint``: the placeholder material colours the game swaps for a car's colours, in order;
  ``lights``: the placeholder colours of lights (shown white);
- ``shared_textures``: dictionaries models fall back on (``vehicles``: for vehicles, ``all``: any);
- ``bone_match``: how animations find bones (``id``: HAnim ids, San Andreas; ``name``: III, VC).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import profiles as shared

ENGINE = "renderware"


@dataclass
class Profile:
    name: str
    short: str = ""
    recognise_files: list[str] = field(default_factory=list)
    folder_names: list[str] = field(default_factory=list)
    level_files: list[str] = field(default_factory=list)
    text: str = "text/american.gxt"
    car_colours: str = "data/carcols.dat"
    ped_animations: str = "ped.ifp"
    paint: list[tuple[int, int, int]] = field(default_factory=list)
    lights: list[tuple[int, int, int]] = field(default_factory=list)
    vehicle_textures: list[str] = field(default_factory=lambda: ["vehicle"])
    shared_textures: list[str] = field(default_factory=lambda: ["generic"])
    bone_match: str = "name"
    source: str = ""  # the file it came from

    @classmethod
    def from_json(cls, data: dict, source: str = "") -> Profile:
        rec = data.get("recognise") or {}
        shared = data.get("shared_textures") or {}
        return cls(
            name=str(data["name"]), short=str(data.get("short") or ""),
            recognise_files=[str(f) for f in rec.get("files") or []],
            folder_names=[str(f).lower() for f in rec.get("folder_names") or []],
            level_files=[str(f) for f in data.get("level_files") or []],
            text=str(data.get("text") or "text/american.gxt"),
            car_colours=str(data.get("car_colours") or "data/carcols.dat"),
            ped_animations=str(data.get("ped_animations") or "ped.ifp"),
            paint=[tuple(int(v) for v in c[:3]) for c in data.get("paint") or []],
            lights=[tuple(int(v) for v in c[:3]) for c in data.get("lights") or []],
            vehicle_textures=[str(t).lower() for t in shared.get("vehicles") or ["vehicle"]],
            shared_textures=[str(t).lower() for t in shared.get("all") or ["generic"]],
            bone_match=str(data.get("bone_match") or "name"),
            source=source,
        )


def profiles() -> tuple[Profile, ...]:
    """The RenderWare games' profiles, through the shared loader (shipped, or replaced by the settings folder)."""
    found = tuple(shared.profiles(ENGINE))
    known = _cache.get("from", ())
    if len(known) != len(found) or any(a is not b for a, b in zip(known, found)):
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


find_ci = shared.find_ci


def recognise(folder: Path) -> Profile | None:
    """The game whose install (or collected copy) ``folder`` is, from its files."""
    folder = Path(folder)
    for p in profiles():
        if any(find_ci(folder, f) is not None for f in p.recognise_files):
            return p
    return None


def detect(folder: Path) -> tuple[Profile, Path] | None:
    """The game a folder is, or is inside (up to three folders up), and that game's folder."""
    folder = Path(folder)
    for cand in [folder, *list(folder.parents)[:3]]:
        p = recognise(cand)
        if p is not None:
            return p, cand
    # a folder named after the game, holding its models (not the Definitive Edition's, which has none of these)
    name = folder.name.lower()
    if "definitive" in name or not _has_models(folder):
        return None
    for p in profiles():
        if any(n == name or n in name for n in p.folder_names):
            return p, folder
    return None


def _has_models(folder: Path) -> bool:
    if find_ci(folder, "models") is not None:
        return True
    try:
        return any(n.lower().endswith((".dff", ".img", ".txd")) for n in os.listdir(folder))
    except OSError:
        return False
