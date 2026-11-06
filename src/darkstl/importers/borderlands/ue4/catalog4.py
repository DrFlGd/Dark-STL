"""Scan an Unreal 4/5 Borderlands folder (Borderlands 3, Wonderlands, Borderlands 4) for models.

The paks' indexes list every file, so the scan reads no package: skeletal and
static meshes are told apart by Unreal's naming convention (``SK_``, ``SM_``),
and grouped by where the game keeps them (``Gear/Weapons/<type>/<maker>``,
``PlayerCharacters``, ``Enemies``...). A model's class is checked when it is
opened.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ....errors import FormatError
from ....model import LOD_SUFFIX, CatalogEntry
from . import vfs

MAX_PEEKS = 40000  # packages whose class the scan reads, when their names don't give it
_SKELETAL = re.compile(r"^(sk|skm|skel)_", re.I)
_STATIC = re.compile(r"^(sm|stm)_", re.I)
_SKIP = re.compile(r"(_physics|_phys|_phy|_collision|_col|_pa|_anim|_abp|_bp)$", re.I)
_WEAPON_KINDS = {
    "assaultrifles": "Assault rifles", "assault_rifles": "Assault rifles", "ar": "Assault rifles",
    "pistols": "Pistols", "ps": "Pistols", "smgs": "SMGs", "sm": "SMGs", "shotguns": "Shotguns", "sg": "Shotguns",
    "sniperrifles": "Sniper rifles", "sr": "Sniper rifles", "heavyweapons": "Heavy weapons", "hw": "Heavy weapons",
    "shields": "Shields", "grenademods": "Grenades", "grenades": "Grenades", "melee": "Melee",
    "artifacts": "Artifacts", "classmods": "Class mods", "spells": "Spells", "wards": "Wards", "rings": "Rings",
    "amulets": "Amulets", "repkits": "Repair kits", "ordnance": "Ordnance", "enhancements": "Enhancements",
    "smg": "SMGs", "sniper": "Sniper rifles", "heavy": "Heavy weapons", "repairkits": "Repair kits",
    "grenadegadgets": "Grenades", "gadgets": "Gadgets", "firmware": "Firmware",
}
_MAKERS = {
    "atl": "Atlas", "cov": "COV", "dal": "Dahl", "hyp": "Hyperion", "jak": "Jakobs", "mal": "Maliwan",
    "ted": "Tediore", "tor": "Torgue", "vla": "Vladof", "ord": "Order", "rip": "Ripper", "dad": "Daedalus",
    "bor": "Ripper", "dpl": "Dahl",
}


def kind_label(folder: str) -> str:
    return _WEAPON_KINDS.get(folder.lower().replace(" ", ""), folder.replace("_", " "))


# Borderlands 4 names weapon part meshes by maker and weapon type (DAD_AR_Barrel_01) rather than SK_/SM_.
_PART_NAME = re.compile(r"^[a-z]{3}_(ar|ps|sg|sm|sr|hw)_", re.I)


def is_part_name(virtual: str) -> bool:
    stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    low = virtual.lower()
    return bool(_PART_NAME.match(stem)) and ("/weapons/" in low or "/gear/" in low) and not _SKIP.search(stem)


def classify(virtual: str, cls: str | None = None) -> tuple[str, str, str] | None:
    """(class, category, group) for a model file, or None when the name doesn't say it is a mesh (``cls``: the
    class, when it was read from the package)."""
    parts = virtual.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    if cls is not None:
        pass
    elif _SKELETAL.match(stem):
        cls = "SkeletalMesh"
    elif _STATIC.match(stem):
        cls = "StaticMesh"
    else:
        return None
    if _SKIP.search(stem):
        return None
    low = [p.lower() for p in parts[:-1]]
    try:
        content = len(low) - 1 - low[::-1].index("content")
    except ValueError:
        content = 0
    sub = parts[content + 1:-1]
    sl = [p.lower() for p in sub]

    def after(word: str, n: int = 2) -> list[str]:
        if word in sl:
            i = sl.index(word)
            return [p for p in sub[i + 1:i + 1 + n] if not p.startswith("_")]
        return []

    if "weapons" in sl or "gear" in sl:
        rest = after("weapons") or after("gear")
        kind = kind_label(rest[0]) if rest else "Gear"
        maker = _MAKERS.get(rest[1].lower(), rest[1]) if len(rest) > 1 else ""
        return cls, "Weapons", f"{kind} / {maker}" if maker else kind
    if any(w in sl for w in ("playercharacters", "player", "characters")):
        rest = after("playercharacters", 1) or after("characters", 1)
        if any(w in stem.lower() for w in ("head", "hair", "face")):
            return cls, "Faces & Hair", rest[0] if rest else ""
        return cls, "Characters", rest[0] if rest else ""
    if any(w in sl for w in ("enemies", "ai", "npcs", "nonplayercharacters", "creatures")):
        rest = after("enemies", 1) or after("ai", 1) or after("npcs", 1) or after("creatures", 1)
        return cls, "Characters", rest[0] if rest else "Enemies"
    if "vehicles" in sl:
        rest = after("vehicles", 1)
        return cls, "Objects", "Vehicles" + (f" / {rest[0]}" if rest else "")
    if any(w in sl for w in ("maps", "levelart", "environments", "world", "geometrycollections")):
        return cls, "Map Pieces", sub[0] if sub else ""
    if any(w in sl for w in ("interactiveobjects", "props", "pickups")):
        return cls, "Objects", sub[0] if sub else ""
    return cls, "Objects" if cls == "StaticMesh" else "Characters", sub[0] if sub else ""


def scan(folder: Path, game: str, progress: Callable[[int, str], None] | None = None) -> list[CatalogEntry]:
    folder = Path(folder)
    gf = vfs.game_files(_root_for(folder), game, refresh=True)
    out = []
    names = gf.assets()
    if gf.zen is not None:
        names += gf.zen.assets()
    peeked = 0
    for i, virtual in enumerate(names):
        if progress and i % 2000 == 0:
            progress(i, virtual)
        hit = classify(virtual)
        if hit is None and gf.zen is not None and peeked < MAX_PEEKS and is_part_name(virtual) \
                and gf.zen.exists(virtual):
            peeked += 1  # read the class from the package's header
            try:
                cls = gf.zen.class_of(virtual)
            except (FormatError, OSError):
                cls = None
            if cls in ("SkeletalMesh", "StaticMesh"):
                hit = classify(virtual, cls)
        if hit is None:
            continue
        cls, category, group = hit
        path = gf.root / virtual
        stem = path.stem
        label = re.sub(r"^(SK|SKM|SM)_", "", stem, flags=re.I).replace("_", " ")
        out.append(CatalogEntry(game, category, group, stem, label, path, importer="borderlands",
                                is_lod=bool(LOD_SUFFIX.search(stem)), packed=not path.is_file()))
    return out


def _root_for(folder: Path) -> Path:
    """The game folder above a pak folder the user picked, so paths count from where the paks mount."""
    for d in [folder, *folder.parents][:5]:
        if d.name.lower() == "paks" and d.parent.name.lower() == "content":
            return d.parent.parent.parent
    if (folder / "Content" / "Paks").is_dir():  # the project folder (OakGame) itself
        return folder.parent
    return folder


def folder_notes(folder: Path, game: str, found: list[CatalogEntry]) -> list[str]:
    gf = vfs.game_files(_root_for(Path(folder)), game)
    notes = []
    if gf.locked:
        names = ", ".join(p.name for p in gf.locked[:3]) + (" and others" if len(gf.locked) > 3 else "")
        notes.append(f"{len(gf.locked)} of the game's .pak files are encrypted and no AES key Dark STL knows opens "
                     f"them ({names}). Put the game's key (0x followed by 64 hexadecimal digits) in a file named "
                     f"{vfs.KEY_FILE} in the game folder or in Dark STL's settings folder, then rescan.")
    for err in gf.errors[:3]:
        notes.append(err)
    if not found and not gf.locked and (gf.paks or gf.zen is not None):
        notes.append("No meshes were found in the game's files.")
    return notes


__all__ = ["classify", "folder_notes", "is_part_name", "kind_label", "scan"]
