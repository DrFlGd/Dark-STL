"""Borderlands 3 and Wonderlands guns put together from the games' own part lists.

Every gun the game can drop has a balance (``InventoryBalanceData``,
``.../_Design/_Manufacturer/Balance/Balance_PS_JAK_01_Common``, a unique's in
``.../_Unique/<Name>/Balance/``). Its ``RuntimePartList`` holds every part the
gun may use (``AllParts``: ``PartData``, ``Weight``), and ``PartTypeTOC`` cuts
that list into the part categories (body, body accessory, barrel, barrel
accessory, magazine, sight...) as ``StartIndex``/``NumParts`` pairs. When the
runtime list is empty the balance's ``BaseSelectionData`` is followed, then its
part set's ``ActorPartLists`` (``PartSetData``). This is how BLCM's modding
documentation describes them (github.com/BLCM/BLCMods/wiki/Borderlands-3-Item-and-Weapon-Parts).

A part (``Part_PS_JAK_Barrel_01``) is a data object; the mesh it shows is
found among the objects its package imports (and, a step further, the objects
the packages it imports import), else by name (``SK_PS_JAK_Barrel_01``). Parts
that show no mesh (stat changes, elements, anointments) are left out, and so are
categories left with no mesh. The categories' names aren't in the game data in
a usable form, so they come from the words in their parts' names.

Each weapon type and maker becomes one configuration holding the parts of all
its ordinary balances, and each unique gun (Lyuda, Maggie...) one of its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ....errors import FormatError
from ....model import CatalogEntry
from . import vfs
from .props import Array4, Struct4, read_props

MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")
MAX_BALANCES = 6000
_BALANCE = re.compile(r"^balance_", re.I)
_FOLLOW_SKIP = re.compile(r"^(part|balance|partset|invbald|t|mi|m|mat|mif|tex|ui|uistat|att|attr|bpd|dt|curve|"
                          r"sound|wwise|ak|ns|fx|anim|abp)_", re.I)
_ACCESSORY = re.compile(r"_[a-h]$", re.I)
_TOKEN = re.compile(r"[a-z]+|\d+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# category label words (first match wins), from the parts' names
_WORDS = [
    ("body", "Body", ("body",)),
    ("barrel", "Barrel", ("barrel", "brl")),
    ("magazine", "Magazine", ("mag", "magazine", "clip", "drum")),
    ("sight", "Sight", ("scope", "sight", "sights", "optic", "irons", "frontsight")),
    ("rail", "Rail", ("rail", "scopemount")),
    ("grip", "Grip", ("grip", "handle")),
    ("foregrip", "Foregrip", ("foregrip", "handguard")),
    ("stock", "Stock", ("stock", "butt")),
    ("underbarrel", "Under-barrel", ("underbarrel", "ub")),
    ("muzzle", "Muzzle", ("muzzle", "silencer", "suppressor", "brake", "cap", "slidecap")),
    ("trigger", "Trigger", ("trigger",)),
    ("element", "Element", ("ele", "element", "elemental")),
    ("accessory", "Accessory", ("acc", "accessory", "attachment", "mount")),
]
_REQUIRED = ("body", "barrel", "magazine")


def _slot_words(stem: str) -> tuple[str, str] | None:
    words = _TOKEN.findall(stem.lower())
    for sid, label, keys in _WORDS:
        if any(w in keys for w in words):
            return sid, label
    return None


def category_slot(stems: list[str], index: int) -> tuple[str, str]:
    """(slot id, label) for a part category, by a vote of its parts' names; the parts that add a lettered
    variant (``Part_PS_JAK_Body_A``) make an accessory category of their kind."""
    votes: dict[tuple[str, str], int] = {}
    lettered = 0
    for s in stems:
        hit = _slot_words(s)
        if hit is not None:
            votes[hit] = votes.get(hit, 0) + 1
        if _ACCESSORY.search(s):
            lettered += 1
    if not votes:
        return f"part{index + 1}", f"Part {index + 1}"
    sid, label = max(votes.items(), key=lambda kv: kv[1])[0]
    if lettered * 2 > len(stems) and sid in ("body", "barrel", "sight", "grip", "magazine", "stock"):
        return f"{sid}_accessory", f"{label} accessory"
    return sid, label


def _props(pkg, e):
    r = pkg.export_data(e)
    if getattr(pkg, "unversioned_props", None) is not None:
        return pkg.unversioned_props(r, e)
    return read_props(r)


def _struct_list(value) -> list:
    if isinstance(value, Array4):
        try:
            return value.structs()
        except FormatError:
            return []
    return []


def _ref_package(pkg, ref) -> str | None:
    if ref is None or not ref:
        return None
    if ref.ref < 0:
        return pkg.import_package(ref.ref)
    return pkg.name


class _Reader:
    """Reads balances, part sets and parts by package name; what each part shows is worked out once."""

    def __init__(self, gf):
        self.gf = gf
        self.missing: set[str] = set()
        self.meshes: dict[str, list[str]] = {}

    def open(self, name: str):
        """The package (the game files keep the last few open), or None; misses are remembered."""
        key = name.lower()
        if key in self.missing:
            return None
        try:
            pkg = self.gf.open_named(name)
        except (FormatError, OSError, ValueError, KeyError, IndexError):
            pkg = None
        if pkg is None:
            self.missing.add(key)
        return pkg

    def balance_categories(self, name: str, depth: int = 0) -> list[list[str]]:
        """The part package names of each part category of a balance."""
        pkg = self.open(name)
        if pkg is None or depth > 3:
            return []
        e = pkg.main_export()
        if e is None:
            return []
        try:
            props = _props(pkg, e)
        except (FormatError, ValueError, IndexError, KeyError):
            return []
        runtime = props.get("RuntimePartList")
        cats: list[list[str]] = []
        if isinstance(runtime, Struct4):
            try:
                rp = runtime.props()
            except FormatError:
                rp = None
            if rp is not None:
                parts = [_ref_package(pkg, p.get("PartData")) for p in _struct_list(rp.get("AllParts"))]
                for t in _struct_list(rp.get("PartTypeTOC")):
                    start, n = int(t.get("StartIndex", 0) or 0), int(t.get("NumParts", 0) or 0)
                    cats.append([p for p in parts[start:start + n] if p])
                if not cats and any(parts):
                    cats = [[p for p in parts if p]]
        if any(cats):
            return cats
        base = _ref_package(pkg, props.get("BaseSelectionData"))
        if base and base.lower() != name.lower():
            cats = self.balance_categories(base, depth + 1)
            if any(cats):
                return cats
        part_set = _ref_package(pkg, props.get("PartSetData"))
        return self.part_set_categories(part_set) if part_set else []

    def part_set_categories(self, name: str) -> list[list[str]]:
        pkg = self.open(name)
        if pkg is None:
            return []
        e = pkg.main_export()
        if e is None:
            return []
        try:
            props = _props(pkg, e)
        except (FormatError, ValueError, IndexError, KeyError):
            return []
        cats = []
        for entry in _struct_list(props.get("ActorPartLists")):
            parts = [_ref_package(pkg, p.get("PartData")) for p in _struct_list(entry.get("Parts"))]
            cats.append([p for p in parts if p])
        return cats

    def part_meshes(self, name: str) -> list[str]:
        """The mesh packages a part shows: imported by its package, or by a package it imports."""
        key = name.lower()
        if key in self.meshes:
            return self.meshes[key]
        found: list[str] = []
        seen = {key}
        queue = [(name, 0)]
        opened = 0
        while queue and opened < 12:
            pname, depth = queue.pop(0)
            pkg = self.open(pname)
            opened += 1
            if pkg is None:
                continue
            here, follow = [], []
            for e in pkg.exports:
                if e.class_name in MODEL_CLASSES and e.outer == 0:
                    here.append(pkg.name)
            for i, imp in enumerate(pkg.imports):
                ref = -i - 1
                if imp.class_name in MODEL_CLASSES:
                    here.append(pkg.import_package(ref))
                elif imp.class_name == "Package" and imp.outer == 0:
                    follow.append(imp.name)
            for m in here:
                if m.lower() not in {f.lower() for f in found}:
                    found.append(m)
            if found or depth >= 2:
                continue
            for f in follow:
                stem = f.rsplit("/", 1)[-1]
                low = f.lower()
                if low in seen or not low.startswith(("/game/gear/", "/game/patchdlc/", "/game/weapons/")) \
                        or _FOLLOW_SKIP.match(stem) or "/_shared/_design/" in low and "/parts/" not in low:
                    continue
                seen.add(low)
                queue.append((f, depth + 1))
        self.meshes[key] = found
        return found


def _rank(part: str, meshes: list[str]) -> list[str]:
    """The meshes most like the part's name first."""
    words = set(_TOKEN.findall(part.rsplit("/", 1)[-1].lower())) - {"part"}
    return sorted(meshes, key=lambda m: -len(words & set(_TOKEN.findall(m.rsplit("/", 1)[-1].lower()))))


def _label(stem: str) -> str:
    s = re.sub(r"^part_", "", stem, flags=re.I)
    return s.replace("_", " ").strip()


def _unique_name(balance: str) -> str | None:
    parts = balance.split("/")
    low = [p.lower() for p in parts]
    for marker in ("_unique", "_uniques", "uniques", "unique"):
        if marker in low[:-1]:
            i = low.index(marker)
            if i + 1 < len(parts) - 1:
                return " ".join(_CAMEL.split(parts[i + 1].replace("_", " "))).strip()
    return None


def balance_configurations(folder: Path, game: str, entries: Iterable[CatalogEntry]) -> tuple[list, set[str]]:
    """(configurations, the weapon groups they cover) from the game's balances, or ([], set()) without them."""
    from ....configs import Configuration, ModelRef, PartOption, Slot
    from .catalog4 import _root_for, classify

    gf = vfs.game_files(_root_for(Path(folder)), game)
    by_path: dict[str, CatalogEntry] = {}
    by_stem: dict[tuple[str, str], CatalogEntry] = {}
    for e in entries:
        if e.config is None and not e.is_lod:
            by_path[str(e.path).lower()] = e
            by_stem[(e.group, re.sub(r"^(sk|skm|sm)_", "", e.stem.lower()))] = e
    names = [v for v in gf.assets() if _BALANCE.match(v.rsplit("/", 1)[-1]) and "/weapons/" in v.lower()]
    if gf.zen is not None:
        names += [v for v in gf.zen.assets() if _BALANCE.match(v.rsplit("/", 1)[-1]) and "/weapons/" in v.lower()]
    if not names:
        return [], set()
    reader = _Reader(gf)

    def mesh_entry(part: str, group: str) -> CatalogEntry | None:
        for m in _rank(part, reader.part_meshes(part)):
            v = gf.virtual_of_package(m) or (gf.zen.by_name.get(m.lower()) if gf.zen is not None else None)
            if v:
                hit = by_path.get(str(gf.root / v).lower())
                if hit is not None:
                    return hit
        stem = re.sub(r"^part_", "", part.rsplit("/", 1)[-1].lower())
        return by_stem.get((group, stem))

    # group -> slot id -> (label, {mesh path: option}); and the uniques, each on its own
    generic: dict[str, dict] = {}
    uniques: list[tuple[str, str, str, dict]] = []
    for virtual in sorted(names)[:MAX_BALANCES]:
        hit = classify(virtual.rsplit(".", 1)[0] + ".uasset", "SkeletalMesh")
        if hit is None:
            continue
        group = hit[2]
        name = vfs.package_name(virtual)
        if not name:
            continue
        cats = reader.balance_categories(name)
        slots: dict[str, tuple[str, dict]] = {}
        for k, parts in enumerate(cats):
            options = {}
            for p in parts:
                me = mesh_entry(p, group)
                if me is not None and str(me.path) not in options:
                    options[str(me.path)] = (p.rsplit("/", 1)[-1], me)
            if not options:
                continue
            sid, label = category_slot([p.rsplit("/", 1)[-1] for p in parts], k)
            while sid in slots and slots[sid][0] != label:
                sid += "_2"
            have = slots.setdefault(sid, (label, {}))[1]
            for path, opt in options.items():
                have.setdefault(path, opt)
        if not slots:
            continue
        unique = _unique_name(name)
        if unique:
            uniques.append((group, unique, name, slots))
        else:
            mine = generic.setdefault(group, {})
            for sid, (label, opts) in slots.items():
                have = mine.setdefault(sid, (label, {}))[1]
                for path, opt in opts.items():
                    have.setdefault(path, opt)

    order = {sid: i for i, (sid, _l, _w) in enumerate(_WORDS)}

    def make(cid: str, label: str, group: str, slots: dict, source: str):
        body = slots.get("body")
        if body is None or not body[1]:
            return None
        bodies = list(body[1].values())
        base = bodies[0][1]
        out_slots = []
        if len(bodies) > 1:
            out_slots.append(Slot("body", "Other body", [PartOption(s.lower(), _label(s), ModelRef(str(e.path)))
                                                         for s, e in bodies[1:]], False, None))
        for sid, (slabel, opts) in sorted(slots.items(), key=lambda kv: (order.get(kv[0].split("_")[0], 99), kv[0])):
            if sid == "body":
                continue
            options = [PartOption(s.lower(), _label(s), ModelRef(str(e.path))) for s, e in opts.values()]
            required = sid in _REQUIRED
            out_slots.append(Slot(sid, slabel, options, required, options[0].id if options else None))
        if not out_slots:
            return None
        return Configuration(cid, label, game, ModelRef(str(base.path)), out_slots, {}, "Weapons", group, source)

    out = []
    covered = set()
    for group, slots in sorted(generic.items()):
        gid = group.lower().replace(" / ", "-").replace(" ", "_")
        c = make(f"gun:{gid}", f"{group.replace(' / ', ' ')} (any parts)", group, slots, "The game's balances")
        if c is not None:
            out.append(c)
            covered.add(group)
    seen = set()
    for group, unique, name, slots in sorted(uniques, key=lambda u: (u[0], u[1].lower())):
        cid = f"gun:{group.lower().replace(' / ', '-').replace(' ', '_')}:{unique.lower().replace(' ', '_')}"
        if cid in seen:
            continue
        seen.add(cid)
        c = make(cid, unique, group, slots, f"Balance {name.rsplit('/', 1)[-1]}")
        if c is not None:
            out.append(c)
    return out, covered


__all__ = ["balance_configurations", "category_slot"]
