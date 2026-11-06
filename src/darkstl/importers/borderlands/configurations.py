"""Gun configurations for Borderlands 2 and The Pre-Sequel, read from the game's own part data.

For each gestalt mesh (one per weapon type), the weapon part definitions that
draw its fragments become the options of their slot: every body, grip,
barrel, sight, stock, elemental and accessory part of every manufacturer. An
option keeps only its fragments of the gestalt mesh (``ModelRef.parts``), and
since every fragment is modelled in place on the shared skeleton, no
attachment points are needed. Parts that draw the same fragments are listed
once. When the part definitions aren't in the folder (only ``Startup.upk``
was added), options come from the fragment names instead.

The gestalt mesh's own materials carry no textures worth having: a gun is
painted with the skin of its Material part. Each weapon type's skins become a
"Skin" slot of kind ``material``; an option opens the gestalt mesh with the
variant ``skin:<package stem>:<part path>``, which gives the skin's material
and no geometry.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...errors import FormatError
from ...model import CatalogEntry
from . import catalog, files, weapons
from .formats import upk

_GROUPS = {"pistol": "Pistols", "smg": "SMGs", "assaultrifle": "Assault rifles", "shotgun": "Shotguns",
           "sniperrifle": "Sniper rifles", "launcher": "Launchers"}


def build_configurations(folder: Path, game: str, entries: list[CatalogEntry], cache: Path | None) -> list:
    try:
        from ... import configs
    except ImportError:  # the app without configurations
        return []
    if game not in (files.GAME_BL2, files.GAME_TPS, files.GAME_BL1):
        return []
    gestalt_pkgs, part_pkgs = [], []
    for path in files.iter_packages(folder):
        try:
            objects = catalog.package_contents(path, cache)
        except (FormatError, OSError, ValueError):
            continue
        classes = {o["class"] for o in objects}
        if "GestaltSkeletalMeshDefinition" in classes:
            gestalt_pkgs.append(path)
        if "WeaponPartDefinition" in classes:
            part_pkgs.append(path)
    parts: dict[str, weapons.WeaponPart] = {}
    for path in part_pkgs:
        try:
            pkg = upk.open_package(path)
        except (FormatError, OSError):
            continue
        for e in pkg.exports:
            if e.class_name != "WeaponPartDefinition" or e.name.startswith("Default__"):
                continue
            key = files.canonical_path(pkg, e).lower()
            if key in parts:
                continue
            try:
                p = weapons.read_part(pkg, e)
            except FormatError:
                continue
            if p is not None:
                parts[key] = p
    out, seen = [], set()
    for path in gestalt_pkgs:
        try:
            pkg = upk.open_package(path)
        except (FormatError, OSError):
            continue
        for g in weapons.gestalts_in(pkg):
            if not g.fragments or g.mesh is None or g.mesh.ref <= 0:
                continue
            mesh_export = pkg.exports[g.mesh.ref - 1]
            key = files.canonical_path(pkg, mesh_export).lower()
            if key in seen:
                continue
            seen.add(key)
            c = _configuration(configs, g, pkg, mesh_export, game, list(parts.values()))
            if c is not None:
                out.append(c)
    covered = {c.id.split("-", 1)[1] for c in out}
    out += _from_part_meshes(configs, game, entries, list(parts.values()), covered)
    return out


# weapon type (as weapons._KIND_WORDS keys it) -> (the prefix of its part meshes' names, its gestalt-style kind)
_MESH_PREFIX = re.compile(r"^(ar|assaultrifle|pistol|ps|smg|sm|sg|shotgun|sr|sniper|sniperrifle|rl|launcher)_",
                          re.I)
_PREFIX_KIND = {"ar": "AssaultRifle", "assaultrifle": "AssaultRifle", "pistol": "Pistol", "ps": "Pistol",
                "smg": "SMG", "sm": "SMG", "sg": "Shotgun", "shotgun": "Shotgun", "sr": "SniperRifle",
                "sniper": "SniperRifle", "sniperrifle": "SniperRifle", "rl": "Launcher", "launcher": "Launcher"}


def _from_part_meshes(configs, game: str, entries: list[CatalogEntry], parts: list[weapons.WeaponPart],
                      covered: set[str]) -> list:
    """Guns whose parts are meshes of their own (``AR_Body_Jakobs``, ``AR_Barrel_Dahl``...): each weapon type a
    configuration, its parts in the slots the game's part definitions put them in (whose
    ``GestaltModeSkeletalMeshName`` names the mesh), else the slots their names say, and its skins."""
    slot_of_mesh: dict[str, str] = {}
    for p in parts:
        if p.slot != weapons.SKIN:
            for n in p.fragments:
                slot_of_mesh.setdefault(n.lower(), p.slot)
    by_kind: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        if e.config is not None or e.is_lod or e.category != "Weapons" or not e.path.name.endswith(".SkeletalMesh"):
            continue
        m = _MESH_PREFIX.match(e.stem)
        if m:
            by_kind.setdefault(_PREFIX_KIND[m.group(1).lower()], []).append(e)
    out = []
    for kind, items in sorted(by_kind.items()):
        if kind.lower() in covered:
            continue
        matched = 0
        by_slot: dict[str, list[CatalogEntry]] = {}
        seen = set()
        for e in sorted(items, key=lambda x: x.stem.lower()):
            if e.stem.lower() in seen:
                continue
            seen.add(e.stem.lower())
            slot = slot_of_mesh.get(e.stem.lower())
            if slot is None:
                slot = weapons.slot_of(e.stem[len(_MESH_PREFIX.match(e.stem).group(0)):])
            else:
                matched += 1
            if slot is not None:
                by_slot.setdefault(slot, []).append(e)
        bodies = by_slot.get("body")
        if not bodies or len(by_slot) < 2:
            continue
        slots = []
        for sid, label, required, _words in weapons.SLOTS:
            options = by_slot.get(sid)
            if not options:
                continue
            opts = [configs.PartOption(e.stem, weapons.option_label(e.stem, kind), configs.ModelRef(str(e.path)))
                    for e in options]
            slots.append(configs.Slot(sid, label, opts, required=required))
        base = str(bodies[0].path)
        skins = weapons.skins_for(kind, parts)
        if skins:
            opts = [configs.PartOption(p.name, weapons.option_label(p.name, kind),
                                       configs.ModelRef(base, f"{SKIN_VARIANT}{p.package}:{p.path}", []))
                    for p in skins]
            slots.append(configs.Slot("skin", "Skin", opts, required=True, default=_default_skin(slots, opts),
                                      kind="material"))
        guessed = not matched
        out.append(configs.Configuration(
            id=f"parts-{kind.lower()}", label=gun_label(kind), game=game, base=configs.ModelRef(base, None, []),
            slots=slots, category="Weapons", group=bodies[0].group or _GROUPS.get(kind.lower(), "Weapon parts"),
            source="The game's part definitions" if not guessed else "Matched by the part meshes' names",
            guessed=guessed))
    return out


def _configuration(configs, g: weapons.Gestalt, pkg: upk.Package, mesh_export: upk.Export, game: str,
                   parts: list[weapons.WeaponPart]):
    file = str(files.member_path(pkg.path, mesh_export.path, "SkeletalMesh"))
    index = g.index()
    by_slot: dict[str, list[tuple[str, str, list[int]]]] = {}  # slot -> (id, label, fragments)
    seen: dict[str, set] = {}
    for p in sorted(parts, key=lambda p: p.path.lower()):
        if p.slot == weapons.SKIN:
            continue
        frags = [index[n.lower()] for n in p.fragments if n.lower() in index]
        if not frags or len(frags) != len(p.fragments):
            continue
        key = tuple(sorted(frags))
        if key in seen.setdefault(p.slot, set()):
            continue
        seen[p.slot].add(key)
        by_slot.setdefault(p.slot, []).append((p.name, weapons.option_label(p.name, g.kind), frags))
    guessed = False
    if not by_slot:  # no part definitions: the fragment names say enough
        guessed = True
        for slot, options in weapons.fragments_by_slot(g).items():
            by_slot[slot] = [(name, weapons.option_label(name, g.kind), frags) for name, frags in options]
    slots = []
    for sid, label, required, _words in weapons.SLOTS:
        options = by_slot.get(sid)
        if not options:
            continue
        opts = [configs.PartOption(oid, olabel, configs.ModelRef(file, None, sorted(frags)))
                for oid, olabel, frags in options]
        slots.append(configs.Slot(sid, label, opts, required=required))
    skins = weapons.skins_for(g.kind, parts)
    if skins:
        opts = [configs.PartOption(p.name, weapons.option_label(p.name, g.kind),
                                   configs.ModelRef(file, f"{SKIN_VARIANT}{p.package}:{p.path}", []))
                for p in skins]
        slots.append(configs.Slot("skin", "Skin", opts, required=True, default=_default_skin(slots, opts),
                                  kind="material"))
    if not slots:
        return None
    kind = g.kind
    return configs.Configuration(
        id=f"gestalt-{kind.lower()}", label=gun_label(kind), game=game, base=configs.ModelRef(file, None, []),
        slots=slots, category="Weapons", group=_GROUPS.get(kind.lower(), "Weapon parts"),
        source=f"{pkg.path.name}: {g.export.path}", guessed=guessed)


SKIN_VARIANT = "skin:"


def _default_skin(slots: list, skins: list) -> str:
    """The skin of the default body's maker (``Pistol_Body_Jakobs`` -> ``Mat_Jakobs_1``), else the first."""
    body = next((s for s in slots if s.id == "body"), None)
    if body is not None and body.options:
        words = {w for w in re.split(r"[_.\s]+", body.options[0].id.lower()) if len(w) > 3} - {"body", "pistol"}
        for o in skins:
            if words & set(re.split(r"[_.\s]+", o.id.lower())):
                return o.id
    return skins[0].id


def gun_label(kind: str) -> str:
    return {"AssaultRifle": "Assault rifle", "SniperRifle": "Sniper rifle"}.get(kind, kind) + " (any parts)"


__all__ = ["SKIN_VARIANT", "build_configurations", "gun_label"]
