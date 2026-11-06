"""Gun configurations for Borderlands 2 and The Pre-Sequel, read from the game's own part data.

For each gestalt mesh (one per weapon type), the weapon part definitions that
draw its fragments become the options of their slot: every body, grip,
barrel, sight, stock, elemental and accessory part of every manufacturer. An
option keeps only its fragments of the gestalt mesh (``ModelRef.parts``), and
since every fragment is modelled in place on the shared skeleton, no
attachment points are needed. Parts that draw the same fragments are listed
once. When the part definitions aren't in the folder (only ``Startup.upk``
was added), options come from the fragment names instead.
"""

from __future__ import annotations

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
    if not gestalt_pkgs:
        return []
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
    return out


def _configuration(configs, g: weapons.Gestalt, pkg: upk.Package, mesh_export: upk.Export, game: str,
                   parts: list[weapons.WeaponPart]):
    file = str(files.member_path(pkg.path, mesh_export.path, "SkeletalMesh"))
    index = g.index()
    by_slot: dict[str, list[tuple[str, str, list[int]]]] = {}  # slot -> (id, label, fragments)
    seen: dict[str, set] = {}
    for p in sorted(parts, key=lambda p: p.path.lower()):
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
    if not slots:
        return None
    kind = g.kind
    return configs.Configuration(
        id=f"gestalt-{kind.lower()}", label=_label(kind), game=game, base=configs.ModelRef(file, None, []),
        slots=slots, category="Weapons", group=_GROUPS.get(kind.lower(), "Weapon parts"),
        source=f"{pkg.path.name}: {g.export.path}", guessed=guessed)


def _label(kind: str) -> str:
    return {"AssaultRifle": "Assault rifle", "SniperRifle": "Sniper rifle"}.get(kind, kind) + " (any parts)"


__all__ = ["build_configurations"]
