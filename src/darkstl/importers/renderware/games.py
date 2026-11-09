"""What differs between GTA III, Vice City and San Andreas: one game profile (JSON) each.

The profiles ship in ``profiles/*.json`` beside this file, and a file of the same name in the
settings folder's ``profiles/`` folder replaces one (so a profile can be tuned without a release).
Each names its engine backend (``renderware``) and:

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

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ENGINE = "renderware"
PROFILE_DIR = Path(__file__).with_name("profiles")


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


def _settings_profiles() -> Path | None:
    try:
        from ...app.config import config_dir
    except Exception:  # noqa: BLE001 - no app settings (tests, command line without them)
        return None
    try:
        return Path(config_dir()) / "profiles"
    except Exception:  # noqa: BLE001
        return None


@lru_cache(maxsize=4)
def _load(override_dir: str | None) -> tuple[Profile, ...]:
    files: dict[str, Path] = {p.name.lower(): p for p in sorted(PROFILE_DIR.glob("*.json"))}
    if override_dir and os.path.isdir(override_dir):
        for p in sorted(Path(override_dir).glob("*.json")):
            files[p.name.lower()] = p  # replaces the shipped profile of the same name, or adds one
    out = []
    for p in files.values():
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("engine") != ENGINE or not data.get("name"):
            continue
        out.append(Profile.from_json(data, str(p)))
    return tuple(out)


def profiles() -> tuple[Profile, ...]:
    d = _settings_profiles()
    return _load(str(d) if d is not None else None)


def forget() -> None:
    _load.cache_clear()


def profile(game: str | None) -> Profile | None:
    for p in profiles():
        if p.name == game:
            return p
    return None


def game_names() -> tuple[str, ...]:
    return tuple(p.name for p in profiles())


def find_ci(root: Path, rel: str) -> Path | None:
    """``rel`` below ``root`` as it is named on disk, or None."""
    cur = Path(root)
    for part in rel.replace("\\", "/").split("/"):
        if not part:
            continue
        nxt = cur / part
        if nxt.exists():
            cur = nxt
            continue
        try:
            names = {n.lower(): n for n in os.listdir(cur)}
        except OSError:
            return None
        found = names.get(part.lower())
        if found is None:
            return None
        cur = cur / found
    return cur


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
