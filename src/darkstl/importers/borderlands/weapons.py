"""Borderlands 2 and The Pre-Sequel guns: gestalt meshes, their fragments, and the parts that pick them.

Each weapon type (pistol, SMG, assault rifle, shotgun, sniper rifle, launcher)
has one shared skeletal mesh that holds every part of every manufacturer, all
in place on one skeleton. A ``GestaltSkeletalMeshDefinition`` (in
``Startup.upk``) names each fragment and its triangle range in the mesh's
index buffer (``GestaltInfos[0].Parts``: ``SkeletalMeshFragmentName``,
``MaterialIndex``, ``FirstIndex``, ``NumPrimitives``). A gun draws the
fragments its parts name: a ``WeaponPartDefinition``'s
``GestaltModeSkeletalMeshName`` plus its ``AdditionalGestaltModeSkeletalMeshNames``
(body variants); ``*_None`` parts draw nothing.

The gestalt mesh's own materials are placeholders: the game paints a gun with
the skin its Material part (``PartType=WP_Material``, ``GD_Weap_Pistol.
ManufacturerMaterials.Mat_Jakobs_1``...) names in its ``Material`` property, a
``MaterialInstanceConstant`` whose chain ends at ``Master_Gun``, and puts it in
the mesh's first material slot (``WillowWeapon.BuildWeaponMaterial``, as traced
by BL2_ReBased's docs/research/NATIVE_WEAPON_VISUALS.md).

These rules are documented by the BL2_ReBased project (github.com/zuhuHix/BL2_ReBased,
docs/verification/WEAPON_PART_ASSETS.md), which checked them against the running game.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...errors import FormatError
from .formats import upk
from .formats.props import ArrayValue, ObjectRef, Props, StructValue, object_props

SLOTS = [  # slot id, label, required, words in part paths and fragment names
    ("body", "Body", True, ("body",)),
    ("grip", "Grip", False, ("grip", "frontgrip")),
    ("barrel", "Barrel", True, ("barrel",)),
    ("sight", "Sight", False, ("sight", "scope")),
    ("stock", "Stock", False, ("stock",)),
    ("elemental", "Elemental", False, ("elemental",)),
    ("accessory", "Accessory", False, ("accessory", "acc")),
]
_SLOT_WORDS = {w: sid for sid, _l, _r, words in SLOTS for w in words}
_PART_TYPES = {"WP_Body": "body", "WP_Grip": "grip", "WP_Barrel": "barrel", "WP_Sight": "sight",
               "WP_Stock": "stock", "WP_Elemental": "elemental", "WP_Accessory1": "accessory",
               "WP_Accessory2": "accessory", "WP_Material": "material"}


@dataclass
class Fragment:
    name: str
    material: int
    first_index: int
    triangles: int


@dataclass
class Gestalt:
    pkg: upk.Package
    export: upk.Export
    mesh: ObjectRef | None
    fragments: list[Fragment] = field(default_factory=list)

    @property
    def kind(self) -> str:
        name = self.export.name
        return name.split("GestaltDef_", 1)[1] if "GestaltDef_" in name else name

    def index(self) -> dict[str, int]:
        return {f.name.lower(): i for i, f in enumerate(self.fragments)}


def _find_mesh(value, depth: int = 0) -> ObjectRef | None:
    if depth > 4:
        return None
    if isinstance(value, ObjectRef):
        return value if value and value.class_name == "SkeletalMesh" else None
    if isinstance(value, Props):
        for name in value.names():
            for v in value.all(name):
                hit = _find_mesh(v, depth + 1)
                if hit is not None:
                    return hit
    if isinstance(value, StructValue):
        try:
            return _find_mesh(value.props(), depth + 1)
        except FormatError:
            return None
    if isinstance(value, ArrayValue) and value.count:
        try:
            for s in value.structs():
                hit = _find_mesh(s, depth + 1)
                if hit is not None:
                    return hit
        except FormatError:
            for o in value.objects():
                if o and o.class_name == "SkeletalMesh":
                    return o
    return None


def read_gestalt(pkg: upk.Package, export: upk.Export) -> Gestalt:
    props = object_props(pkg, pkg.export_data(export))
    g = Gestalt(pkg, export, _find_mesh(props))
    infos = props.get("GestaltInfos")
    if isinstance(infos, ArrayValue) and infos.count:
        first = infos.structs()[0]
        parts = first.get("Parts")
        if isinstance(parts, ArrayValue):
            for p in parts.structs():
                g.fragments.append(Fragment(str(p.get("SkeletalMeshFragmentName", "")), int(p.get("MaterialIndex", 0)),
                                            int(p.get("FirstIndex", 0)), int(p.get("NumPrimitives", 0))))
    if g.mesh is None:
        # the mesh beside its definition
        for e in pkg.exports:
            if e.class_name == "SkeletalMesh" and e.outer == export.outer:
                g.mesh = ObjectRef(pkg, e.index + 1)
                break
    return g


_GESTALTS: dict[tuple, list[Gestalt]] = {}


def gestalts_in(pkg: upk.Package) -> list[Gestalt]:
    key = (str(pkg.path), pkg.summary.export_count, id(pkg))
    hit = _GESTALTS.get(key)
    if hit is None:
        hit = []
        for e in pkg.exports:
            if e.class_name == "GestaltSkeletalMeshDefinition":
                try:
                    hit.append(read_gestalt(pkg, e))
                except FormatError:
                    continue
        _GESTALTS[key] = hit
    return hit


def gestalt_for_mesh(pkg: upk.Package, export: upk.Export) -> Gestalt | None:
    for g in gestalts_in(pkg):
        if g.mesh is not None and g.mesh.ref == export.index + 1 and g.fragments:
            return g
    return None


@dataclass
class WeaponPart:
    path: str
    name: str
    slot: str  # a slot of SLOTS, or "skin" for a Material part
    fragments: list[str]
    package: str = ""  # the stem of the package it was read from
    material: str = ""  # a skin's material instance (its object path)
    file: str = ""  # the package file it was read from


SKIN = "skin"
# weapon type (a gestalt definition's kind, lower case) -> words in a skin's paths that name it
_KIND_WORDS = [
    ("sniperrifle", ("sniper",)),
    ("assaultrifle", ("assaultrifle", "assault_rifle", "_ar_", ".ar.")),
    ("launcher", ("launcher", "rocket")),
    ("shotgun", ("shotgun",)),
    ("smg", ("smg",)),
    ("pistol", ("pistol",)),
]


def _kind_key(kind: str) -> str:
    """``Pistol``, ``GestaltDef_Pistol``, ``SniperRifle``... -> a key of _KIND_WORDS."""
    k = re.sub(r"[^a-z]", "", kind.lower())
    if k in ("ar", "assaultrifle"):
        return "assaultrifle"
    for key, words in _KIND_WORDS:
        if key in k or any(w.strip("._") in k for w in words if len(w.strip("._")) > 2):
            return key
    return k


def skin_kind(part: WeaponPart) -> str | None:
    """The weapon type a skin is for, from its part's path (``GD_Weap_Pistol...``), then its material's."""
    for text in (part.path, part.material):
        low = f".{text.lower()}."
        for kind, words in _KIND_WORDS:
            if any(w in low for w in words):
                return kind
    return None


def skins_for(kind: str, parts: list[WeaponPart]) -> list[WeaponPart]:
    """The skins of one weapon type, one per material instance, by name."""
    want = _kind_key(kind)
    out, seen = [], set()
    for p in sorted(parts, key=lambda p: (p.name.lower(), p.path.lower())):
        if p.slot != SKIN or not p.material or skin_kind(p) != want:
            continue
        if p.material.lower() in seen:
            continue
        seen.add(p.material.lower())
        out.append(p)
    return out


def slot_of(text: str) -> str | None:
    for word in re.split(r"[._\s]+", text.lower()):
        if word in _SLOT_WORDS:
            return _SLOT_WORDS[word]
    return None


def read_part(pkg: upk.Package, export: upk.Export) -> WeaponPart | None:
    props = object_props(pkg, pkg.export_data(export))
    slot = _PART_TYPES.get(str(props.get("PartType", ""))) or slot_of(export.path) or slot_of(export.name)
    material = props.get("Material")
    if slot == "material" or (slot is None and isinstance(material, ObjectRef) and material):
        if not isinstance(material, ObjectRef) or not material:
            return None
        return WeaponPart(export.path, export.name, SKIN, [], pkg.path.stem, material.path, str(pkg.path))
    if slot is None:
        return None
    names = []
    if not export.name.lower().endswith("_none"):
        for n in [props.get("GestaltModeSkeletalMeshName"), *props.all("AdditionalGestaltModeSkeletalMeshNames")]:
            if n and str(n) != "None" and str(n) not in names:
                names.append(str(n))
    return WeaponPart(export.path, export.name, slot, names, pkg.path.stem, file=str(pkg.path))


def fragments_by_slot(g: Gestalt) -> dict[str, list[tuple[str, list[int]]]]:
    """Options per slot from the fragment names alone (when the part definitions aren't there):
    ``Pistol_Body_Vladof_Var1`` goes with ``Pistol_Body_Vladof``."""
    out: dict[str, list[tuple[str, list[int]]]] = {}
    base_of: dict[str, tuple[str, list[int]]] = {}
    for i, f in enumerate(g.fragments):
        slot = slot_of(f.name) or "accessory"
        base = re.sub(r"_Var\d+$", "", f.name, flags=re.I)
        if base.lower() != f.name.lower() and base.lower() in base_of:
            base_of[base.lower()][1].append(i)
            continue
        option = (f.name, [i])
        base_of[f.name.lower()] = option
        out.setdefault(slot, []).append(option)
    return out


def option_label(name: str, kind: str) -> str:
    """``Pistol_Barrel_Bandit_3`` -> ``Bandit 3``."""
    words = [w for w in re.split(r"_+", name) if w]
    drop = {kind.lower(), "pistol", "smg", "ar", "assaultrifle", "shotgun", "sniper", "sniperrifle", "launcher", "l",
            "sr", "sg", "sm", "acc", "mat", "mati"} | set(_SLOT_WORDS)
    kept = [w for w in words if w.lower() not in drop]
    return " ".join(kept) or name


__all__ = ["Fragment", "Gestalt", "SKIN", "SLOTS", "WeaponPart", "fragments_by_slot", "gestalt_for_mesh", "gestalts_in",
           "option_label", "read_gestalt", "read_part", "skin_kind", "skins_for", "slot_of"]
