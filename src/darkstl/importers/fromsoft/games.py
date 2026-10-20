"""What differs between the FromSoftware games, in one place.

All of these games share the FLVER2 model format and the TPF texture format;
they differ in compression, platform texture layout and where material data
lives. Detection looks for the files each game ships with, then for the
game's name in the folder path. Archives also name the studio's project in
the paths of the files they hold (``N:\\SPRJ\\data\\INTERROOT_ps4\\chr\\...``
is Bloodborne), which recognises a lone extracted file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GameProfile:
    name: str
    markers: tuple[str, ...]  # file names in the game folder; "a+b" means both must be present
    path_hint: re.Pattern
    platform: str = "PC"
    compression: str = "DFLT"  # DCX codec of the shipped files
    texture_paths_in_flver: bool = True  # False: materials name their textures elsewhere (MATBIN)
    notes: str = ""
    archive_path: re.Pattern | None = None  # how the paths inside the game's archives begin


GAME_PROFILES: tuple[GameProfile, ...] = (
    GameProfile("Elden Ring", ("eldenring.exe", "regulation.bin+asset", "start_protected_game.exe"),
                re.compile(r"elden\s*ring", re.I), compression="KRAK", texture_paths_in_flver=False,
                notes="Oodle-compressed; material textures are listed in MATBIN files, so names are matched",
                archive_path=re.compile(r"^N:\\GR\\", re.I)),
    GameProfile("Sekiro", ("sekiro.exe",), re.compile(r"sekiro", re.I), compression="KRAK",
                notes="Oodle-compressed", archive_path=re.compile(r"^N:\\NTC\\", re.I)),
    GameProfile("Dark Souls III", ("darksoulsiii.exe",), re.compile(r"dark\s*souls\s*(iii|3)|\bDS3\b", re.I),
                notes="character textures in separate .texbnd files", archive_path=re.compile(r"^N:\\FDP\\", re.I)),
    GameProfile("Dark Souls II", ("darksoulsii.exe",), re.compile(r"dark\s*souls\s*(ii|2)|\bDS2\b", re.I),
                notes="different folder layout; not yet checked"),
    GameProfile("Dark Souls Remastered", ("darksoulsremastered.exe",),
                re.compile(r"remaster|\bDSR\b", re.I),
                archive_path=re.compile(r"^N:\\FRPG\\data\\INTERROOT_x64\\", re.I)),
    GameProfile("Dark Souls", ("darksouls.exe",), re.compile(r"dark\s*souls|\bDS1\b|PTDE", re.I),
                archive_path=re.compile(r"^N:\\FRPG\\", re.I)),
    GameProfile("Bloodborne", ("eboot.bin+dvdroot_ps4", "dvdroot_ps4"),
                re.compile(r"bloodborne|cusa00900|cusa03173|cusa00207|dvdroot_ps4", re.I), platform="PS4",
                notes="PS4 textures are tiled and have no DDS header; checked on extracted game files",
                archive_path=re.compile(r"^N:\\SPRJ\\", re.I)),
)

GAMES = tuple(p.name for p in GAME_PROFILES)


def profile(name: str | None) -> GameProfile | None:
    return next((p for p in GAME_PROFILES if p.name == name), None)


def detect_game(root: str | os.PathLike) -> str | None:
    """Guess which game a folder came from by marker files, then by path names."""
    root = Path(root)
    for candidate in [root, *list(root.parents)[:3]]:
        try:
            names = {p.name.lower() for p in candidate.iterdir()}
        except OSError:
            continue
        for p in GAME_PROFILES:
            if any(all(part in names for part in marker.split("+")) for marker in p.markers):
                return p.name
    text = str(root)
    for p in GAME_PROFILES:
        if p.path_hint.search(text):
            return p.name
    return None


def game_from_archive_path(name: str | None) -> str | None:
    """The game whose archives hold a file under ``name`` (the path an archive gives its files)."""
    if not name:
        return None
    for p in GAME_PROFILES:
        if p.archive_path is not None and p.archive_path.search(name):
            return p.name
    return None
