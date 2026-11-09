"""Scan an Unreal 4/5 Borderlands folder (Borderlands 3, Wonderlands, Borderlands 4) for models.

The paks' indexes list every file, so the scan reads no package: skeletal and
static meshes are told apart by Unreal's naming convention (``SK_``, ``SM_``),
and grouped by where the game keeps them (``Gear/Weapons/<type>/<maker>``,
``PlayerCharacters``, ``Enemies``...). Borderlands 4's weapon part meshes, named
by maker and weapon type (``DAD_AR_Barrel_01``), have their class read from
their headers. All of that is in the games' profiles
(``importers/profiles/borderlands3.json`` and the two that extend it), read by
the Unreal importer's catalogue; a model's class is checked when it is opened.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ....model import CatalogEntry
from ...unreal import catalog as _catalog
from ...unreal.profile import for_game
from ..files import GAME_BL3


def _profile(game: str | None):
    prof = for_game(game)
    return prof if prof.known and prof.importer == "borderlands" else for_game(GAME_BL3)


def kind_label(folder: str) -> str:
    """A weapon type's name from its folder (``SniperRifles`` -> Sniper rifles)."""
    table = _profile(GAME_BL3).sorting.tables.get("weapon_kinds", {})
    return table.get(folder.lower().replace(" ", ""), folder.replace("_", " "))


def is_part_name(virtual: str, game: str | None = "Borderlands 4") -> bool:
    """Whether a package is named like a Borderlands 4 weapon part mesh (``DAD_AR_Barrel_01``)."""
    return _profile(game).sorting.worth_peeking(virtual, True)


def classify(virtual: str, cls: str | None = None, game: str | None = None) -> tuple[str, str, str] | None:
    """(class, category, group) for a model file, or None when the name doesn't say it is a mesh (``cls``: the
    class, when it was read from the package)."""
    return _profile(game).classify(virtual, cls)


def scan(folder: Path, game: str, progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
    return _catalog.scan(_root_for(Path(folder)), game, _profile(game), "borderlands", progress)


def _root_for(folder: Path) -> Path:
    """The game folder above a pak folder the user picked, so paths count from where the paks mount."""
    return _catalog.root_for(Path(folder))


def folder_notes(folder: Path, game: str, found: list[CatalogEntry]) -> list[str]:
    return _catalog.folder_notes(_root_for(Path(folder)), game or "The game", _profile(game), found)


__all__ = ["classify", "folder_notes", "is_part_name", "kind_label", "scan"]
