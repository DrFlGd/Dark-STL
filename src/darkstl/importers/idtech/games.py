"""The id Tech games Dark STL reads and what differs between them.

The table is kept in code, as for the other engines' games; a game profile in the settings folder
(``<settings>/profiles/*.json`` with ``"engine": "idtech"`` and the game's name, see docs/profiles.md) can
change what isn't confirmed on the real files yet: ``{"idtech": {"unit": 0.01}}`` for the size,
``{"textures": {"normal_green": "directx"}}`` for the normal maps, ``{"recognise": {"files": [...]}}`` for
the executables a folder is recognised by.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

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
ENGINE = "idtech"


def profile(game: str | None) -> Profile:
    p = PROFILES.get(game or "", PROFILES[ETERNAL])
    from .. import profiles as shared

    data = shared.overrides(ENGINE, p.game)
    if not data:
        return p
    known: dict = {}
    own = data.get(ENGINE) if isinstance(data.get(ENGINE), dict) else {}
    try:
        if own.get("unit") is not None and float(own["unit"]) > 0:
            known["unit"] = float(own["unit"])
    except (TypeError, ValueError):
        pass
    green = str((data.get("textures") or {}).get("normal_green") or "").lower()
    if green in ("directx", "opengl"):
        known["normal_green"] = green
    files = [str(f).rsplit("/", 1)[-1] for f in (data.get("recognise") or {}).get("files") or [] if str(f).strip()]
    if files:
        known["executables"] = tuple(dict.fromkeys(files + list(p.executables)))
    return replace(p, **known)


def game_of_container_version(version: int) -> str | None:
    return next((p.game for p in PROFILES.values() if p.container_version == version), None)
