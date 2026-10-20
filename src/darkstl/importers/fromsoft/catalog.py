"""Scan extracted game folders and group the models they contain.

Files are classified by FromSoftware's naming scheme, which is shared across
Dark Souls, Bloodborne, Dark Souls III and Elden Ring:

=================  ===============================================
``wp_a_0300``      weapons and shields
``hd_ bd_ am_ lg_``  armour: head, body, arms, legs (``_m_``/``_f_``)
``fc_ hr_ fg_``    faces, hair and other body parts
``c1234``          characters and enemies (``c0000`` is the player)
``o123456``        objects (``aeg123_456`` assets in Elden Ring)
``m10_00_00_00``   map pieces
=================  ===============================================
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable

from ...model import CatalogEntry
from .files import file_stem, strip_dcx
from .games import detect_game

ARMOR_SLOTS = {"hd": "Head", "bd": "Body", "am": "Arms", "lg": "Legs"}
BODY_PARTS = {"fc": "Face", "fg": "Face", "hr": "Hair", "ey": "Eyes", "eb": "Eyebrows", "be": "Beard",
              "ma": "Mask", "fe": "Face"}

_RX_PART = re.compile(r"^(?P<kind>[a-z]{2})_(?P<sex>[a-z])_(?P<id>\d{3,5})(?P<rest>.*)$", re.I)
_RX_CHR = re.compile(r"^c(?P<id>\d{4})(?P<rest>.*)$", re.I)
_RX_OBJ = re.compile(r"^o(?P<id>\d{4,6})(?P<rest>.*)$", re.I)
_RX_AEG = re.compile(r"^aeg(?P<a>\d{3})_(?P<b>\d{3})(?P<rest>.*)$", re.I)
_RX_MAP = re.compile(r"^m(?P<area>\d{2})_(?P<b>\d{2})_\d{2}_\d{2}.*$", re.I)
_RX_MAP_DS1 = re.compile(r"^m\d{4}b\d", re.I)

MODEL_FILE_RX = re.compile(r"\.(partsbnd|chrbnd|objbnd|geombnd|mapbnd|flver|flv)(\.dcx)?$", re.I)


def classify(stem: str) -> tuple[str, str, str, bool]:
    """Return ``(category, group, label, is_lod)`` for a file stem."""
    s = stem.lower()
    is_lod = bool(re.search(r"_l$|_lod\d*$", s))
    if is_lod:
        s = re.sub(r"_l$|_lod\d*$", "", s)
    m = _RX_PART.match(s)
    if m:
        kind, sex, pid = m.group("kind"), m.group("sex"), m.group("id")
        sex_label = {"m": "Male", "f": "Female", "a": "All"}.get(sex, sex.upper())
        if kind == "wp":
            return "Weapons", f"IDs {pid[:-2]}xx", f"Weapon {pid}", is_lod
        if kind in ARMOR_SLOTS:
            slot = ARMOR_SLOTS[kind]
            return "Armor", slot, f"{slot} {pid} ({sex_label})", is_lod
        if kind in BODY_PARTS:
            part = BODY_PARTS[kind]
            return "Faces & Hair", part, f"{part} {pid} ({sex_label})", is_lod
        return "Other", kind.upper(), stem, is_lod
    m = _RX_CHR.match(s)
    if m:
        cid = m.group("id")
        if cid == "0000":
            return "Characters", "Player", "Player (c0000)", is_lod
        return "Characters", f"c{cid[:2]}xx", f"Character c{cid}", is_lod
    m = _RX_AEG.match(s)
    if m:
        return "Objects", f"aeg{m.group('a')}", f"Asset aeg{m.group('a')}_{m.group('b')}", is_lod
    m = _RX_OBJ.match(s)
    if m:
        oid = m.group("id")
        return "Objects", f"o{oid[:2]}xxxx", f"Object o{oid}", is_lod
    m = _RX_MAP.match(s)
    if m:
        return "Map Pieces", f"m{m.group('area')}_{m.group('b')}", stem, is_lod
    if _RX_MAP_DS1.match(s):
        return "Map Pieces", s[:3], stem, is_lod
    return "Other", "", stem, is_lod


def iter_model_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if MODEL_FILE_RX.search(name):
                yield Path(dirpath) / name


def scan(root: str | os.PathLike, game: str | None = None,
         progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
    """Find every model under ``root``. Packed and unpacked copies of one model are listed once."""
    root = Path(root)
    game = game or detect_game(root) or root.name
    by_stem: dict[str, CatalogEntry] = {}
    for i, path in enumerate(iter_model_files(root)):
        name = path.name
        packed = not strip_dcx(name).lower().endswith((".flver", ".flv"))
        stem = file_stem(name)
        if not packed:
            parent_stem = file_stem(path.parent.name)
            if path.parent.name.lower() != parent_stem.lower():
                stem = parent_stem or stem  # inside an unpacked '<stem>-partsbnd-dcx' folder
        category, group, label, is_lod = classify(stem)
        key = stem.lower()
        existing = by_stem.get(key)
        if existing is not None and (existing.packed or not packed):
            continue
        by_stem[key] = CatalogEntry(game, category, group, stem, label, path, importer="fromsoft",
                                    is_lod=is_lod, packed=packed)
        if progress and i % 50 == 0:
            progress(i, name)
    entries = sorted(by_stem.values(), key=lambda e: e.sort_key)
    return entries
