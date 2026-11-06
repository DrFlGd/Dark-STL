"""Borderlands 3, Wonderlands and Borderlands 4 guns as configurations of their part meshes.

These games build a gun from separate part meshes (body, barrel, grip,
magazine, scope, stock, under-barrel...) kept per weapon type and maker
(``Gear/Weapons/<type>/<maker>/...``), modelled in place around the gun's
origin. Each weapon type and maker becomes one configuration: the body is the
base, and the other parts are sorted into slots by the words in their names.
The slots are guessed from the file names, so they are marked as such.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ....model import CatalogEntry

_TOKEN = re.compile(r"[a-z]+|\d+")
SLOTS = [  # (slot id, label, words in a part's name, required)
    ("body", "Body", ("body", "receiver", "base", "frame"), True),
    ("barrel", "Barrel", ("barrel", "brl"), False),
    ("grip", "Grip", ("grip", "handle"), False),
    ("magazine", "Magazine", ("mag", "magazine", "clip", "drum", "ammo"), False),
    ("sight", "Sight", ("scope", "sight", "optic", "optics", "irons"), False),
    ("stock", "Stock", ("stock", "butt"), False),
    ("underbarrel", "Under-barrel", ("underbarrel", "ub", "under"), False),
    ("muzzle", "Muzzle", ("muzzle", "silencer", "suppressor", "brake"), False),
    ("foregrip", "Foregrip", ("foregrip", "handguard"), False),
    ("element", "Element", ("element", "elemental", "elem"), False),
    ("accessory", "Accessory", ("acc", "accessory", "attachment", "rail", "mount"), False),
]


def _slot_of(stem: str) -> tuple[str, str] | None:
    words = _TOKEN.findall(stem.lower())
    for sid, label, keys, _req in SLOTS:
        if any(w in keys for w in words):
            return sid, label
    return None


def _label(stem: str) -> str:
    s = re.sub(r"^(SK|SKM|SM)_", "", stem, flags=re.I)
    return s.replace("_", " ").strip()


def gun_configurations(entries: Iterable[CatalogEntry], game: str, max_parts: int = 400) -> list:
    from ....configs import Configuration, ModelRef, PartOption, Slot

    groups: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        if e.game == game and e.category == "Weapons" and not e.is_lod and e.config is None and "/" in e.group:
            groups.setdefault(e.group, []).append(e)
    out = []
    for group, items in sorted(groups.items()):
        if len(items) < 2 or len(items) > max_parts:
            continue
        slots: dict[str, Slot] = {}
        bodies = []
        for e in sorted(items, key=lambda x: x.stem.lower()):
            hit = _slot_of(e.stem)
            if hit is None:
                continue
            sid, label = hit
            if sid == "body":
                bodies.append(e)
                continue
            slot = slots.setdefault(sid, Slot(sid, label, [], False, None))
            slot.options.append(PartOption(e.stem.lower(), _label(e.stem), ModelRef(str(e.path))))
        if not bodies or not slots:
            continue
        base = bodies[0]
        if len(bodies) > 1:  # several bodies: the body is a slot of its own, the first one the base
            slots = {"body": Slot("body", "Body", [PartOption(b.stem.lower(), _label(b.stem), ModelRef(str(b.path)))
                                                   for b in bodies[1:]], False, None), **slots}
        order = {s[0]: i for i, s in enumerate(SLOTS)}
        ordered = sorted(slots.values(), key=lambda s: order.get(s.id, 99))
        for s in ordered:
            s.default = None if s.id == "body" else s.options[0].id
        kind = group.replace(" / ", " ")
        out.append(Configuration(f"gun:{group.lower().replace(' / ', '-').replace(' ', '_')}", f"{kind} (any parts)",
                                 game, ModelRef(str(base.path)), ordered, {}, "Weapons", group,
                                 "Guessed from the part meshes' names", guessed=True))
    return out


__all__ = ["SLOTS", "gun_configurations"]
