"""The RE Engine games Dark STL knows, and how a folder is recognised as one of them.

All of them share the mesh, tex, mdf2 and pak formats; they differ in the format versions (the
number each file's extension ends in) and in where their files sit (``natives/x64/`` for the
first games, ``natives/stm/`` from Resident Evil Village on). A game folder is recognised by its
executable, then by its name in the path, then by the versions of the first model or texture
files found in it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .formats import mesh as meshfmt
from .formats import tex as texfmt


@dataclass(frozen=True)
class GameProfile:
    name: str
    markers: tuple[str, ...]  # file names in the game folder
    path_hint: re.Pattern
    notes: str = ""


GAME_PROFILES: tuple[GameProfile, ...] = (
    GameProfile("Resident Evil 2", ("re2.exe",),
                re.compile(r"resident\s*evil\s*2\b|\bre2(r|rt)?\b|biohazard\s*re[:\s]*2", re.I)),
    GameProfile("Resident Evil 3", ("re3.exe",),
                re.compile(r"resident\s*evil\s*3\b|\bre3(r|rt)?\b|biohazard\s*re[:\s]*3", re.I)),
    GameProfile("Resident Evil 4", ("re4.exe",),
                re.compile(r"resident\s*evil\s*4\b|\bre4(r|rt)?\b|biohazard\s*re[:\s]*4", re.I)),
    GameProfile("Resident Evil 7", ("re7.exe",), re.compile(r"resident\s*evil\s*7\b|\bre7(rt)?\b|biohazard\s*7", re.I),
                notes="the ray-tracing re-release; the original RE7 uses an older format"),
    GameProfile("Resident Evil Village", ("re8.exe",),
                re.compile(r"resident\s*evil\s*(village|8)\b|\bre8\b|biohazard\s*village", re.I)),
    GameProfile("Devil May Cry 5", ("devilmaycry5.exe",), re.compile(r"devil\s*may\s*cry\s*5|\bdmc5\b", re.I)),
)

GAMES = tuple(p.name for p in GAME_PROFILES)

MESH_FILE = re.compile(r"\.mesh(\.\d+)?$", re.I)
TEX_FILE = re.compile(r"\.tex(\.\d+)?$", re.I)
PAK_FILE = re.compile(r"\.pak$", re.I)


def version_of(name: str) -> int | None:
    """The format version in a file's extension ('pl0000.mesh.1808312334' -> 1808312334)."""
    m = re.search(r"\.(\d+)$", name)
    return int(m.group(1)) if m else None


def game_from_versions(mesh_version: int | None = None, tex_version: int | None = None) -> str | None:
    if mesh_version is not None and mesh_version in meshfmt.VERSIONS:
        return meshfmt.VERSIONS[mesh_version][1]
    if tex_version is not None and tex_version in texfmt.VERSIONS:
        return texfmt.VERSIONS[tex_version]
    return None


def detect_game(root: str | os.PathLike, max_files: int = 4000) -> str | None:
    """Which game a folder came from: by its executable, its name, then its files' format versions."""
    root = Path(root)
    for candidate in [root, *list(root.parents)[:3]]:
        try:
            names = {p.name.lower() for p in candidate.iterdir()}
        except OSError:
            continue
        for p in GAME_PROFILES:
            if any(m in names for m in p.markers):
                return p.name
    text = str(root)
    for p in GAME_PROFILES:
        if p.path_hint.search(text):
            return p.name
    seen = 0
    tex_version = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            seen += 1
            if seen > max_files:
                return game_from_versions(None, tex_version)
            if MESH_FILE.search(name):
                game = game_from_versions(version_of(name), None)
                if game:
                    return game
            elif tex_version is None and TEX_FILE.search(name):
                tex_version = version_of(name)
    return game_from_versions(None, tex_version)


__all__ = ["GAMES", "GAME_PROFILES", "MESH_FILE", "PAK_FILE", "TEX_FILE", "detect_game", "game_from_versions",
           "version_of"]
