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

from ...model import LOD_SUFFIX, CatalogEntry
from .files import file_stem, strip_dcx
from .games import detect_game

ARMOR_SLOTS = {"hd": "Head", "bd": "Body", "am": "Arms", "lg": "Legs"}
BODY_PARTS = {"fc": "Face", "fg": "Face", "hr": "Hair", "ey": "Eyes", "eb": "Eyebrows", "be": "Beard",
              "ma": "Mask", "fe": "Face"}

_RX_PART = re.compile(r"^(?P<kind>[a-z]{2})_(?P<sex>[a-z])_(?P<id>\d{3,5})(?P<rest>.*)$", re.I)
_RX_PART_DS2 = re.compile(r"^(?P<kind>[a-z]{2})_(?P<id>\d{4})_(?P<sex>[mfa])(?P<rest>.*)$", re.I)
_RX_OBJ_DS2 = re.compile(r"^o(?P<a>\d{2})_(?P<b>\d{4})(?P<rest>.*)$", re.I)
_RX_CHR = re.compile(r"^c(?P<id>\d{4})(?P<rest>.*)$", re.I)
_RX_OBJ = re.compile(r"^o(?P<id>\d{4,6})(?P<rest>.*)$", re.I)
_RX_AEG = re.compile(r"^aeg(?P<a>\d{3})_(?P<b>\d{3})(?P<rest>.*)$", re.I)
_RX_MAP = re.compile(r"^m(?P<area>\d{2})_(?P<b>\d{2})_\d{2}_\d{2}.*$", re.I)
_RX_MAP_DS1 = re.compile(r"^m\d{4}b\d", re.I)

# Dark Souls II packs its models in plain .bnd archives (c1000.bnd, wp_1000_m.bnd, o00_0002.bnd)
DS2_MODEL = r"^(c\d{4}|[a-z]{2}_\d{4}_[mfa](_[la])?|o\d{2}_\d{4})\.bnd$"
MODEL_FILE_RX = re.compile(r"\.(partsbnd|chrbnd|objbnd|geombnd|mapbnd|flver|flv)(\.dcx)?$|" + DS2_MODEL, re.I)


def classify(stem: str) -> tuple[str, str, str, bool]:
    """Return ``(category, group, label, is_lod)`` for a file stem."""
    s = stem.lower()
    is_lod = bool(LOD_SUFFIX.search(s))
    if is_lod:
        s = LOD_SUFFIX.sub("", s)
    m = _RX_PART.match(s) or _RX_PART_DS2.match(s)
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
    m = _RX_OBJ_DS2.match(s)
    if m:
        return "Objects", f"o{m.group('a')}", f"Object o{m.group('a')}_{m.group('b')}", is_lod
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
         progress: Callable[[int, str], None] | None = None,
         paths: Iterable[Path] | None = None) -> list[CatalogEntry]:
    """Find every model under ``root`` (or among ``paths``, its files when they're listed already).
    Packed and unpacked copies of one model are listed once."""
    root = Path(root)
    game = game or detect_game(root) or root.name
    by_stem: dict[str, CatalogEntry] = {}
    models = iter_model_files(root) if paths is None else (p for p in paths if MODEL_FILE_RX.search(p.name))
    for i, path in enumerate(models):
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


# ---------------------------------------------------------------------------
# Models that go together (Importer.related)

_RX_TAIL = re.compile(r"^(?P<base>.+?)(?P<tail>_\d{1,2}|_[a-z])$", re.I)


def family(stem: str) -> tuple[str, str] | None:
    """The family a model's file belongs to, and what it is in it: (key, role). Armour pieces with the same id
    make a set (key ``set:<sex>:<id>``, role the slot); a weapon and its ``_1`` sheath or variant files share
    the weapon's key (role ``Weapon`` or ``Variant``), a character and its variant files the character's. The
    low-detail copy of a model (``_l``) is in its model's family with the role ``Low-detail copy``."""
    s = LOD_SUFFIX.sub("", stem.lower())
    lod = s != stem.lower()
    m = _RX_PART.match(s) or _RX_PART_DS2.match(s)
    if m:
        kind, sex, pid, rest = m.group("kind"), m.group("sex"), m.group("id"), m.group("rest")
        if kind in ARMOR_SLOTS:
            return f"set:{sex}:{pid}", "Low-detail copy" if lod else ARMOR_SLOTS[kind]
        if kind == "wp":
            return f"wp:{sex}:{pid}", "Low-detail copy" if lod else "Variant" if rest else "Weapon"
        return None
    m = _RX_CHR.match(s)
    if m:
        return f"c:{m.group('id')}", "Low-detail copy" if lod else "Variant" if m.group("rest") else "Character"
    m = _RX_OBJ.match(s) or _RX_AEG.match(s)
    if m:
        t = _RX_TAIL.match(s)
        base = t.group("base") if t and (_RX_OBJ.match(t.group("base")) or _RX_AEG.match(t.group("base"))) else s
        return f"o:{base}", "Low-detail copy" if lod else "Variant" if base != s else "Object"
    return None


def relation(mine: tuple[str, str], other: tuple[str, str], same_model: bool) -> str | None:
    """How a model of the same family (``other``: its key and role) is related to one (``mine``), in a few words;
    None for one not worth listing (another piece's low-detail copy)."""
    if other[1] == "Low-detail copy":
        return "Low-detail copy" if same_model else None
    if mine[1] == "Low-detail copy":
        return "Full-detail model" if same_model else None
    if mine[0].startswith("set:"):
        return f"Same armour set: {other[1].lower()}"
    if other[1] == "Variant":
        return "Sheath or variant" if mine[0].startswith("wp:") else "Variant"
    return {"Weapon": "Its weapon", "Character": "Its character", "Object": "Its object"}.get(other[1], other[1])
