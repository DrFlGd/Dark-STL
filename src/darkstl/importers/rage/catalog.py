"""Sorting GTA V's and GTA IV's models into the library (GTA IV's kinds in brackets).

- Vehicles (``.yft`` in a ``vehicles`` archive or named in ``vehicles.meta``): by class, named from the
  text (``gameName``). A vehicle's ``_hi`` model is the one listed; the plain one is its lower detail.
- Characters: peds' component dictionaries (``.ydd``), by name prefix (ambient, service, gangs...), and
  the components of streamed peds (the online characters) by ped.
- Weapons (``w_*`` models, or named in ``weapons*.meta``): by weapon group, named from the text.
- Objects (props) and Map Pieces (everything else under ``levels/``, or GTA IV's ``data/maps/``), by
  archive.
- GTA IV's names come from the ``.ide`` files and the ``.gxt`` text; models of its episodes (``TLAD/``,
  ``TBoGT/``) have the episode's name after their group.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ...model import CatalogEntry
from . import files


def _stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _archive_name(rel: str) -> str:
    parts = rel.split("/")
    for p in reversed(parts[:-1]):
        if p.endswith(".rpf"):
            return p[:-4]
    return parts[-2] if len(parts) > 1 else ""


def ped_group(gf: files.GameFiles, stem: str) -> str:
    prefixes = gf.profile.ped_prefixes if gf.profile else {}
    for pre in sorted(prefixes, key=len, reverse=True):
        if stem.startswith(pre):
            return prefixes[pre]
    return "Other characters"


def _component_ped(rel: str) -> str | None:
    """The ped a streamed component belongs to (``mp_m_freemode_01/jbib_000_u.ydd`` or
    ``mp_m_freemode_01^jbib_000_u.ydd``), or None."""
    stem = _stem(rel)
    if "^" in stem:
        return stem.split("^", 1)[0]
    parts = rel.split("/")
    if len(parts) >= 2 and not parts[-2].endswith((".rpf", ".img")) and "peds" in rel:
        return parts[-2]
    return None


def classify(gf: files.GameFiles, rel: str) -> tuple[str, str, str, bool] | None:
    """(category, group, label, is lower detail) of a model, or None to leave it out."""
    c = _classify(gf, rel)
    episode = files.EPISODES.get(rel.split("/", 1)[0])
    if c is not None and episode:
        return c[0], f"{c[1]} ({episode})", c[2], c[3]
    return c


def _classify(gf: files.GameFiles, rel: str) -> tuple[str, str, str, bool] | None:
    stem = _stem(rel)
    ext = rel.rsplit(".", 1)[-1]
    low = rel.lower()
    prof = gf.profile
    if stem.endswith("_lod") or stem.endswith("_slod") or "_slod" in stem or stem.startswith("lod_"):
        return None
    frag, dictionary, drawable = ext in ("yft", "wft"), ext in ("ydd", "wdd"), ext in ("ydr", "wdr")
    if frag and ("vehicles" in low or stem.removesuffix("_hi") in gf.vehicles):
        base = stem.removesuffix("_hi")
        info = gf.vehicles.get(base)
        label = (gf.label(info.label) if info else None) or base
        maker = gf.label(info.maker) if info and info.maker else None
        if maker and not label.lower().startswith(maker.lower()):
            label = f"{maker} {label}"
        group = (prof.vehicle_classes.get(info.vclass.upper(), "") if info and prof else "") or "Vehicles"
        lower = not stem.endswith("_hi") and gf.find(f"{base}_hi.{ext}") is not None
        return "Vehicles", group, label + (" (high detail)" if stem.endswith("_hi") else ""), lower
    if "peds" in low or stem in gf.peds or dictionary and stem.split("_")[0] in ("a", "s", "g", "u", "cs", "ig"):
        if frag:
            return None  # a ped's skeleton and ragdoll; its components are in the .ydd
        if "pedprops" in low or stem.endswith("_p"):
            return "Characters", "Props", stem, False
        ped = _component_ped(rel)
        if ped is not None and ped != stem:
            return "Characters", f"{ped} components", stem.split("^", 1)[-1], False
        return "Characters", ped_group(gf, stem), stem, False
    weapon = gf.weapons.get(stem)
    if weapon is not None or stem.startswith("w_") and ("weapons" in low or drawable):
        label = (gf.label(weapon.label) if weapon else None) or stem
        group = (prof.weapon_groups.get(weapon.group.upper(), "") if weapon and prof else "") or "Weapons"
        if weapon is None and stem.startswith("w_"):
            group = {"pi": "Pistols", "sb": "SMGs", "ar": "Rifles", "mg": "Machine guns", "sg": "Shotguns",
                     "sr": "Snipers", "lr": "Heavy", "me": "Melee", "ex": "Thrown", "at": "Attachments"}.get(
                         stem.split("_")[1] if stem.count("_") >= 1 else "", "Weapons")
        return "Weapons", group, label, False
    if "/props/" in low or "props" in _archive_name(rel) or stem.startswith("prop_"):
        return "Objects", _archive_name(rel) or "Props", stem, False
    if "/levels/" in "/" + low or "/data/maps/" in "/" + low:
        return "Map Pieces", _archive_name(rel) or "Map", stem, False
    return "Models", _archive_name(rel) or PurePosixPath(rel).parent.name or "Models", stem, False


def scan(gf: files.GameFiles, game: str | None, importer: str) -> list[CatalogEntry]:
    out = []
    seen: set[str] = set()
    for name, places in gf.names.items():
        if not name.endswith(files.MODEL_EXT):
            continue
        for rel in places:  # the first of a name is the one the game uses; a ped's parts share names
            key = f"{_component_ped(rel) or ''}/{name}"
            if key in seen:
                continue
            seen.add(key)
            c = classify(gf, rel)
            if c is None:
                continue
            cat, group, label, lower = c
            out.append(CatalogEntry(game or gf.game or ("GTA IV files" if gf.generation == 4 else "GTA V files"), cat, group, _stem(rel), label,
                                    gf.path(rel), importer, lower, rel not in gf.loose))
    return out
