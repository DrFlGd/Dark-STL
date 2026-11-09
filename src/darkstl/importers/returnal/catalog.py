"""Returnal's folder and models, by its game profile (``importers/profiles/returnal.json``).

What was Returnal's own code (finding the game, the engine release, sorting its models) is now the
Unreal importer's, driven by the profile; these names are kept for what used them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ...model import CatalogEntry
from ..unreal import catalog as _catalog
from ..unreal import mappings_file
from ..unreal.profile import for_game

GAME = "Returnal"
PROJECT = "Returnal"  # the project folder: <install>/Returnal/Content/Paks
ENGINE_FILE = f"{PROJECT}/Binaries/{_catalog.ENGINE_FILE_NAME}"  # in a collected copy: the release, as 4.26


def _profile():
    return for_game(GAME)


def engine_version(root: Path) -> int:
    return _catalog.engine_version(Path(root), _profile()) or 425


def detect_game(folder: Path) -> str | None:
    """Returnal when the folder is its install folder (or the project or pak folder inside it)."""
    return GAME if _catalog.recognises(_profile(), Path(folder)) else None


def is_returnal_path(path: Path) -> bool:
    """Whether a model path lies in Returnal's files (``.../Returnal/Content/...``)."""
    return _catalog.is_profile_path(_profile(), Path(path))


def root_for(folder: Path) -> Path:
    """The install folder above the project folder, so virtual paths read ``Returnal/Content/...``."""
    return _catalog.root_for(Path(folder), _profile())


def classify(virtual: str, cls: str | None = None) -> tuple[str, str, str] | None:
    """(class, category, group) for a model file, by Returnal's sorting rules."""
    return _profile().classify(virtual, cls)


def worth_peeking(virtual: str) -> bool:
    """Whether a package whose name doesn't say its class might hold a mesh."""
    return _profile().sorting.worth_peeking(virtual, False)


def scan(folder: Path, progress: Callable[[int, str], None] | None = None,
         cache: Path | None = None) -> list[CatalogEntry]:
    return _catalog.scan(root_for(folder), GAME, _profile(), "returnal", progress, cache)


def folder_notes(folder: Path, found: list[CatalogEntry]) -> list[str]:
    return _catalog.folder_notes(root_for(folder), GAME, _profile(), found)


def __getattr__(name: str):
    if name == "MAPPINGS_FILE":
        return mappings_file(_profile())
    raise AttributeError(name)


__all__ = ["ENGINE_FILE", "GAME", "PROJECT", "classify", "detect_game", "engine_version", "folder_notes",
           "is_returnal_path", "root_for", "scan", "worth_peeking"]
