"""Characters the engine keeps as several meshes: one per body part, each in its own folder.

RE Engine games file a character's parts below its character folder, named after it::

    character/ch/ch03/0200/body/body00/ch03_0200_body00.mesh.2101050001
    character/ch/ch03/0200/face/face00/ch03_0200_face00.mesh.2101050001
    character/ch/ch03/0200/hair/hair00/ch03_0200_hair00.mesh.2101050001
    character/player/pl0000/body/pl0000_body.mesh.1808312334   (RE2: the folder is the id itself)

All the parts are modelled in place on the same skeleton, so putting them together is a matter of
loading them side by side, and one animation poses them all. This module guesses such
configurations (:class:`~darkstl.configs.Configuration`) from the paths of a game's models: the
character folder is the nearest ancestor named after the character id (``ch03_0200``, its number
``0200`` or ``pl0000``), each folder directly below it is a slot (Body, Face, Hair...), and the mesh
files under a slot are its options. The body is the base; a slot's first file is its default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ...configs import Configuration, ModelRef, PartOption, Slot
from ...model import CatalogEntry
from .files import character_id

BASE_SLOTS = ("body", "upperbody", "lowerbody", "torso", "main")
MAX_PARTS = 64


def _character_folder(path: Path, cid: str) -> Path | None:
    """The ancestor folder named after character ``cid``, within four levels of the file."""
    names = {cid}
    if "_" in cid:
        names.add(cid.split("_", 1)[1])  # Village: ch03_0200 lives in ch03/0200/
    parents = list(path.parents)[:4]
    for folder in parents:
        if folder.name.lower() in names:
            return folder
    return None


def _slot_of(path: Path, folder: Path) -> str | None:
    rel = path.parent.relative_to(folder).parts
    return rel[0].lower() if rel else None


def characters_from_folders(entries: Iterable[CatalogEntry], game: str) -> list[Configuration]:
    """Configurations for the characters whose parts are kept as separate meshes (see the module doc)."""
    groups: dict[tuple[str, Path], dict[str, list[CatalogEntry]]] = {}
    for e in entries:
        if e.category != "Characters" or e.is_lod or e.config is not None:
            continue
        cid = character_id(e.stem)
        if not cid:
            continue
        folder = _character_folder(e.path, cid)
        if folder is None:
            continue
        slot = _slot_of(e.path, folder)
        if not slot:
            continue
        groups.setdefault((cid, folder), {}).setdefault(slot, []).append(e)
    out = []
    for (cid, folder), slots in sorted(groups.items(), key=lambda kv: kv[0][0]):
        total = sum(len(v) for v in slots.values())
        if len(slots) < 2 or total > MAX_PARTS:
            continue
        for items in slots.values():
            items.sort(key=lambda e: e.stem.lower())
        base_slot = next((s for s in BASE_SLOTS if s in slots), None) or max(slots, key=lambda s: len(slots[s]))
        base = slots[base_slot][0]
        config_slots = []
        for slot in sorted(slots, key=lambda s: (s != base_slot, s)):
            options = [e for e in slots[slot] if e is not base]
            if not options:
                continue
            opts = [PartOption(e.stem.lower(), _part_label(e.stem, cid, slot), ModelRef(str(e.path)))
                    for e in options]
            label = slot.replace("_", " ").capitalize()
            if slot == base_slot:
                label += " (other)"
            config_slots.append(Slot(slot, label, opts, False, None if slot == base_slot else opts[0].id))
        out.append(Configuration(f"auto:{cid}", f"{cid} (whole character)", game, ModelRef(str(base.path)),
                                 config_slots, {}, base.category, base.group,
                                 "Guessed from the part folders of the character", guessed=True))
    return out


def _part_label(stem: str, cid: str, slot: str) -> str:
    rest = stem[len(cid):].strip("_-. ") if stem.lower().startswith(cid) else stem
    return (rest or slot).replace("_", " ")


__all__ = ["characters_from_folders"]
