"""Importer for Returnal (Housemarque, Unreal Engine 4).

The PC version keeps its models in ``.pak`` files under ``Returnal/Content/Paks``. They are read in place
with the Unreal readers, by Returnal's game profile (``importers/profiles/returnal.json``): finding the
game, the engine release its executable names, how its folders sort its models into kinds and groups, its
``_Comp`` normal maps and what Collect keeps besides the models are all in the profile.

Encrypted paks need the game's AES key, which Dark STL doesn't ship: it goes in Settings under *Unreal
games* (or ``unreal-keys.txt`` in the game's folder).
"""

from __future__ import annotations

from ..unreal import UnrealImporter
from .catalog import GAME


class ReturnalImporter(UnrealImporter):
    key = "returnal"
    name = "Returnal (Unreal Engine 4)"
    generic = False


__all__ = ["GAME", "ReturnalImporter"]
