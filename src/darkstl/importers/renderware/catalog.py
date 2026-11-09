"""Listing a RenderWare GTA folder's models and sorting them into kinds and groups.

The kind comes from the section of the ``.ide`` file that defines the model: ``peds`` are
Characters (grouped by ped type), ``cars`` Vehicles (by vehicle type), ``weap`` Weapons,
``objs``/``tobj``/``anim`` Map Pieces or, for the generic and prop lists, Objects (grouped by the
``.ide`` file's area), ``hier`` cutscene Objects. A model no ``.ide`` names (San Andreas'
``player.img`` clothes, cutscene heads, loose files) goes under Characters when it is in the
player's archive and Other otherwise. Vehicles are listed under their name in the game.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...model import CatalogEntry
from . import files
from .formats import text as textfmt

_PED_GROUPS = {
    "civmale": "Civilians", "civfemale": "Civilians", "cop": "Police", "medic": "Emergency services",
    "fireman": "Emergency services", "emergency": "Emergency services", "criminal": "Criminals",
    "prostitute": "Civilians", "dealer": "Criminals", "special": "Story characters", "player1": "Players",
    "player2": "Players", "playernetwork": "Players", "army": "Military", "bum": "Civilians",
}
_VEHICLE_GROUPS = {
    "car": "Cars", "mtruck": "Cars", "quad": "Bikes and quads", "bike": "Bikes and quads", "bmx": "Bikes and quads",
    "boat": "Boats", "heli": "Helicopters", "plane": "Planes", "train": "Trains", "trailer": "Trailers",
}
_PROP_FILES = re.compile(r"generic|props|procobj|barriers|dynamic|multiobj|vegepart|levelxre|txd\.ide", re.IGNORECASE)
LOD_STEM = re.compile(r"^lod|_lod$|^islandlod", re.IGNORECASE)


def classify(gf: files.GameFiles, stem: str, where: Path) -> tuple[str, str, str, bool]:
    """``(category, group, label, is_lod)`` of a model."""
    d = gf.definition(stem)
    label = stem
    if d is None:
        if gf.profile is None:
            return "Models", _folder_group(where, gf.root), label, False
        archive = files.split_member(where)
        source = archive[0].stem.lower() if archive is not None else where.parent.name.lower()
        if source == "player":
            return "Characters", "Player clothes", label, False
        if source == "cutscene" or stem.lower().startswith(("cs", "csplay")):
            return "Characters", "Cutscene characters", label, False
        return "Other", source.title() if source else "", label, bool(LOD_STEM.search(stem))
    if d.section == "peds":
        group = _PED_GROUPS.get(d.ped_type.lower())
        if group is None:
            group = "Gangs" if d.ped_type.lower().startswith("gang") else "Pedestrians"
        if stem.lower().startswith("special"):
            group = "Story characters"
        return "Characters", group, label, False
    if d.section == "cars":
        name = gf.display_name(stem)
        return "Vehicles", _VEHICLE_GROUPS.get(d.vehicle_type, "Vehicles"), name or label, False
    if d.section == "weap":
        return "Weapons", "Weapons", label, False
    if d.section == "hier":
        return "Objects", "Cutscene objects", label, False
    group = _ide_group(d)
    kind = "Objects" if _PROP_FILES.search(d.file) else "Map Pieces"
    return kind, group, label, bool(LOD_STEM.search(stem))


def _ide_group(d: textfmt.Definition) -> str:
    parts = [p for p in d.file.split("/")[:-1] if p.lower() not in ("data", "maps")]
    stem = d.file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if parts and parts[-1].lower() != stem.lower():
        return f"{parts[-1]} / {stem}"
    return stem


def _folder_group(where: Path, root: Path) -> str:
    member = files.split_member(where)
    folder = member[0].with_suffix("") if member is not None else where.parent
    try:
        rel = folder.relative_to(root)
        return rel.as_posix() if rel.parts else ""
    except ValueError:
        return folder.name


def scan(gf: files.GameFiles, game: str, importer: str) -> list[CatalogEntry]:
    out = []
    for name, places in sorted(gf.names.items()):
        if not name.endswith(files.MODEL_EXT):
            continue
        where = places[0]
        stem = where.name[:-len(files.MODEL_EXT)] or name[:-len(files.MODEL_EXT)]
        category, group, label, is_lod = classify(gf, stem, where)
        out.append(CatalogEntry(game, category, group, stem, label, where, importer, is_lod))
    return out
