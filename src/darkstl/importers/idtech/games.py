"""The id Tech games Dark STL reads and what differs between them.

Once game profiles are JSON files (docs/profiles.md), these move there; until then a profile can be tuned with a
JSON file of the same fields in ``<settings>/profiles/<game>.json`` (see :func:`override`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

ETERNAL = "DOOM Eternal"
DOOM2016 = "DOOM (2016)"
DARKAGES = "DOOM: The Dark Ages"


@dataclass(frozen=True)
class Profile:
    game: str
    badge: str
    executables: tuple[str, ...]  # any of these beside base/ says it's this game
    container_version: int  # IDCL version of its .resources files (0: DOOM 2016's .index)
    darkages_stream_ids: bool = False  # how StreamDB ids are made from a resource's hash
    shared_maps: tuple[str, ...] = ("common", "warehouse")
    unit: float = 0.0254  # metres per game unit (id units are taken as inches; to confirm on real files)
    normal_green: str | None = None  # "directx" / "opengl" once checked on a flat sample; None: detected
    notes: tuple[str, ...] = field(default_factory=tuple)


PROFILES = {
    ETERNAL: Profile(ETERNAL, "DE", ("DOOMEternalx64vk.exe",), 12),
    DARKAGES: Profile(DARKAGES, "TDA", ("DOOMTheDarkAges.exe",), 13, darkages_stream_ids=True,
                      shared_maps=("common", "warehouse", "init")),
}
GAMES = tuple(PROFILES)
_overrides: dict[str, Profile] = {}


def profile(game: str | None) -> Profile:
    p = PROFILES.get(game or "", PROFILES[ETERNAL])
    return _overrides.get(p.game, p)


def override(folder: str | Path | None) -> None:
    """Read profile tweaks from ``<folder>/<game>.json`` (any of the fields above but the game's name)."""
    _overrides.clear()
    if not folder:
        return
    for game, p in PROFILES.items():
        f = Path(folder) / f"{game.replace(':', '')}.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        known = {k: v for k, v in data.items() if k in Profile.__dataclass_fields__ and k != "game"}
        for k in ("executables", "shared_maps", "notes"):
            if k in known:
                known[k] = tuple(known[k])
        _overrides[game] = replace(p, **known)


def game_of_container_version(version: int) -> str | None:
    return next((p.game for p in PROFILES.values() if p.container_version == version), None)
