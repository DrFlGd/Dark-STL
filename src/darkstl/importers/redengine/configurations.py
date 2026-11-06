"""Weapon configurations from the game's own appearance files.

A Cyberpunk weapon is drawn from an ``.app``: each of its appearances
(``frank``, ``military1``, ``neon2``...) lists the mesh components it shows,
and the same component name (``mag``, ``stock``, ``decal``...) carries a
different mesh or mesh appearance from one appearance to the next. So the
components become the slots of a configuration, the meshes they carry its
options, and the appearances its presets: the body is the component every
appearance draws the same way. Attachment points (``entSlotComponent`` slots
named ``scope``, ``muzzle``...) in the weapon or its base entity become slots
for the attachment entities of the same kind, placed at the slot (on the
rig's bone, when the body mesh knows it).

Which attachments the game lets a weapon take is in TweakDB, which isn't
read: every scope is offered on a scope slot, every silencer on a muzzle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...model import CatalogEntry
from . import catalog, depot, entities
from .formats import archive as arc
from .formats import cr2w

SOURCE = "Read from the game's appearance files"
log = logging.getLogger(__name__)
ATTACHMENT_KINDS = (  # (slot id, label, words in a slot's name, words in an attachment's name)
    ("scope", "Scope", ("scope", "sight", "optic"), ("scope", "sight", "optic")),
    ("muzzle", "Muzzle", ("muzzle", "silencer", "suppressor", "barrel"), ("silencer", "suppressor", "muzzle")),
    ("underbarrel", "Underbarrel", ("underbarrel", "grip", "rail"), ("grip", "laser", "flashlight", "rail")),
)
_ATTACHMENT_PATH = re.compile(r"\\attachments?\\|^w_att_|\\w_att", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z]+|\d+")


@dataclass
class _Option:
    entry: CatalogEntry
    comp: entities.MeshComponent
    appearance: str

    @property
    def key(self) -> tuple:
        return (self.entry.path, self.comp.appearance)


@dataclass
class Report:
    """What the last build found in a folder, for its notes."""

    apps: int = 0  # appearance files read
    weapons: int = 0  # with meshes in the library
    attachments: int = 0
    configured: int = 0
    unresolved: int = 0  # components naming no model of the library
    errors: list[str] = field(default_factory=list)

    def notes(self) -> list[str]:
        if not self.apps:
            return [("No weapon appearance files (.app) were found in this folder's archives or files, so weapons "
                     "are put together from their names only.")]
        text = (f"{self.apps} weapon appearance files read: {self.weapons} weapons and {self.attachments} "
                f"attachments named models of this folder, {self.configured} became configurable")
        if self.unresolved:
            text += f"; {self.unresolved} components name meshes that aren't in the library"
        if self.errors:
            text += f". {len(self.errors)} couldn't be read ({self.errors[0]})"
        return [text + "."]


REPORTS: dict[str, Report] = {}  # folder -> its last report


def notes_for(folder: Path) -> list[str]:
    r = REPORTS.get(str(folder))
    return r.notes() if r is not None else []


@dataclass
class _Weapon:
    name: str  # the weapon's stem, as the game names it
    source: str
    app: entities.AppResource
    options: dict[str, list[_Option]] = field(default_factory=dict)  # component name -> its options, in order
    present: dict[str, dict[str, _Option]] = field(default_factory=dict)  # component -> appearance -> option
    slots: list[entities.SlotDef] = field(default_factory=list)
    unresolved: int = 0


class Lookup:
    """Library entries by mesh hash and by stem."""

    def __init__(self, entries: list[CatalogEntry]):
        self.by_hash: dict[int, CatalogEntry] = {}
        self.by_stem: dict[str, CatalogEntry] = {}
        for e in entries:
            if e.config is not None or e.importer != "redengine":
                continue
            try:
                if e.packed and depot.is_archive_member(e.path):
                    h = depot.member_hash(e.path)
                else:
                    d = depot.depot_path_of(e.path)
                    h = arc.path_hash(d) if d else None
            except FormatError:
                h = None
            if h is not None and (h not in self.by_hash or self.by_hash[h].is_lod and not e.is_lod):
                self.by_hash[h] = e
            self.by_stem.setdefault(e.stem.lower(), e)

    def find(self, comp: entities.MeshComponent) -> CatalogEntry | None:
        e = self.by_hash.get(comp.mesh_hash)
        if e is None and not isinstance(comp.mesh, int):
            e = self.by_stem.get(comp.mesh_stem)
        return e


# ---------------------------------------------------------------- reading the weapons


def _app_files(folder: Path, entries: list[CatalogEntry], cache: Path | None):
    """(where it came from, Cr2wFile) for every weapon appearance file of the folder: in its archives, and loose."""
    archives: list[Path] = []
    for e in entries:
        if e.packed and depot.is_archive_member(e.path) and e.path.parent not in archives:
            archives.append(e.path.parent)
    for a in archives:
        try:
            ar = arc.open_archive(a)
            hashes = catalog.weapon_apps(a, cache)
        except (FormatError, OSError):
            continue
        for h in hashes:
            try:
                f = cr2w.read_cr2w(ar.read(h))
            except (FormatError, OSError, ValueError, IndexError):
                continue
            yield ar.name_of(h), f
    for p in catalog.iter_files(folder):
        if p.suffix.lower() != ".app" or "weapons" not in [x.lower() for x in p.parts]:
            continue
        try:
            f = cr2w.read_cr2w(p.read_bytes())
        except (FormatError, OSError, ValueError, IndexError):
            continue
        yield str(p), f


def _weapon_name(stems: list[str]) -> str:
    """The weapon's own name: what its meshes' names share (``w_rifle_precision__midnight_sor22``)."""
    if not stems:
        return ""
    prefix = stems[0]
    for s in stems[1:]:
        n = 0
        for x, y in zip(prefix, s):
            if x != y:
                break
            n += 1
        prefix = prefix[:n]
    prefix = prefix.rstrip("_-. ")
    if len(prefix) < 4 and stems:
        return min(stems, key=len)
    return prefix


def _read_weapons(folder: Path, entries: list[CatalogEntry], cache: Path | None, report: Report) -> list[_Weapon]:
    lookup = Lookup(entries)
    out: list[_Weapon] = []
    seen: set[tuple] = set()
    for source, f in _app_files(folder, entries, cache):
        report.apps += 1
        try:
            app = entities.read_app(f)
        except (ValueError, FormatError) as exc:
            report.errors.append(f"{source}: {exc}")
            log.warning("Weapon appearance %s could not be read: %s", source, exc)
            continue
        if app.partial:
            report.errors.append(f"{source}: an appearance's components could not be read")
        w = _Weapon("", source, app)
        stems: list[str] = []
        for a in app.appearances:
            for c in a.meshes:
                e = lookup.find(c)
                if e is None:
                    w.unresolved += 1
                    continue
                if e.category != "Weapons":
                    continue
                name = c.name or e.stem
                opt = _Option(e, c, a.name)
                have = w.options.setdefault(name, [])
                existing = next((o for o in have if o.key == opt.key), None)
                if existing is None:
                    have.append(opt)
                    existing = opt
                w.present.setdefault(name, {})[a.name] = existing
                stems.append(e.stem.lower())
            w.slots += a.slots
        if not w.options:
            report.unresolved += w.unresolved
            continue
        report.unresolved += w.unresolved
        w.name = _weapon_name(sorted(set(stems)))
        if app.base_entity:
            w.name = Path(app.base_entity.replace("\\", "/")).stem or w.name
        key = tuple(sorted(o.key for opts in w.options.values() for o in opts))
        if key in seen:  # the same weapon from another archive (ep1 copies)
            continue
        seen.add(key)
        out.append(w)
    return out


# ---------------------------------------------------------------- building the configurations


def _humanize(text: str) -> str:
    return re.sub(r"[_\-]+", " ", text).strip().capitalize() or text


def _is_attachment(w: _Weapon) -> bool:
    depots = [str(o.comp.mesh) if not isinstance(o.comp.mesh, int) else "" for opts in w.options.values()
              for o in opts]
    paths = [str(o.entry.path) for opts in w.options.values() for o in opts]
    return any(_ATTACHMENT_PATH.search(p) for p in depots + paths + [w.name, w.source])


def _bone_matrices(path: Path) -> dict[str, np.ndarray]:
    """Bone name -> rest-pose matrix of a mesh's rig, from its inverse bind matrices."""
    try:
        m = depot.open_file(path).mesh()
    except (FormatError, OSError, ValueError):
        return {}
    out = {}
    for name, inv in zip(m.bone_names, m.bone_matrices):
        try:
            out[name] = np.linalg.inv(inv)
        except np.linalg.LinAlgError:
            continue
    return out


class _Placer:
    """Where a component or an attachment goes, relative to the weapon's body."""

    def __init__(self, base: _Option, slots: list[entities.SlotDef]):
        self.base = base
        self.slots = {s.name.lower(): s for s in slots if s.name}
        self._bones: dict[str, np.ndarray] | None = None
        self.base_inverse = np.linalg.inv(base.comp.transform) if np.isfinite(base.comp.transform).all() else np.eye(4)

    def bones(self) -> dict[str, np.ndarray]:
        if self._bones is None:
            self._bones = _bone_matrices(self.base.entry.path)
        return self._bones

    def socket(self, slot_name: str) -> np.ndarray | None:
        s = self.slots.get(slot_name.lower())
        if s is None:
            return None
        m = s.matrix()
        if s.bone:
            bone = self.bones().get(s.bone)
            if bone is not None:
                m = bone @ m
        return m

    def component(self, c: entities.MeshComponent) -> np.ndarray:
        m = c.transform
        if c.slot_name:
            sock = self.socket(c.slot_name)
            if sock is not None:
                m = sock @ m
        elif c.bind_name and self.bones().get(c.bind_name) is not None:
            m = self.bones()[c.bind_name] @ m
        return self.base_inverse @ m


def _attach(configs, matrix: np.ndarray):
    if np.allclose(matrix, np.eye(4), atol=1e-6):
        return None
    return configs.Attach(matrix=[float(x) for x in matrix.reshape(-1)])


def _configuration(configs, w: _Weapon, game: str, attachments: list[_Weapon], entity_slots: list[entities.SlotDef]):
    appearances = [a.name for a in w.app.appearances if a.meshes]
    names = list(w.options)
    # the body: the one mesh every appearance draws (at its own finish, often)
    constant = [n for n in names if len({o.entry.path for o in w.options[n]}) == 1
                and len(w.present[n]) == len(appearances)]

    def base_score(n: str) -> tuple:
        stem = w.options[n][0].entry.stem.lower()
        words = _TOKEN.findall(stem.removeprefix(w.name))
        return (0 if any(x in ("base", "body") for x in words) else 1, len(stem))

    base_name = min(constant, key=base_score) if constant else max(names, key=lambda n: len(w.present[n]))
    base = w.options[base_name][0]
    placer = _Placer(base, w.slots + entity_slots)
    slots = []
    finishes = w.options[base_name]
    if len(finishes) > 1:  # the body's own appearances: a finish slot, repainting the body only
        options = [configs.PartOption(o.comp.appearance or f"finish{i}", _humanize(o.comp.appearance or f"finish {i}"),
                                      configs.ModelRef(str(base.entry.path), o.comp.appearance or None))
                   for i, o in enumerate(finishes)]
        slots.append(configs.Slot("finish", "Finish", options, True, options[0].id, kind="finish"))
    for n in names:
        if n == base_name:
            continue
        opts = w.options[n]
        stems = [o.entry.stem.lower() for o in opts]
        several = len({o.entry.path for o in opts}) < len(opts)
        options = []
        for o in opts:
            stem = o.entry.stem.lower()
            rest = stem[len(w.name):].strip("_-. ") if stem.startswith(w.name) else stem
            oid = stem + (f"@{o.comp.appearance}" if several and o.comp.appearance else "")
            label = _humanize(rest or stem) + (f" ({o.comp.appearance})" if several and o.comp.appearance else "")
            options.append(configs.PartOption(oid, label, configs.ModelRef(str(o.entry.path), o.comp.appearance or None),
                                              _attach(configs, placer.component(o.comp))))
        first = w.present[n].get(appearances[0]) if appearances else None
        default = next((opt.id for opt, o in zip(options, opts) if first is not None and o is first), options[0].id)
        label = _humanize(n if n and not n.startswith(w.name) else (stems[0][len(w.name):] if stems else n))
        slots.append(configs.Slot(_slot_id(n), label or n, options, len(w.present[n]) == len(appearances), default))
    presets = {}
    for a in appearances:
        choice = {}
        if len(finishes) > 1:
            body = w.present[base_name].get(a)
            choice["finish"] = (body.comp.appearance or f"finish{finishes.index(body)}") if body else ""
        for s, n in zip([s for s in slots if s.id != "finish"], [x for x in names if x != base_name]):
            o = w.present[n].get(a)
            if o is None:
                choice[s.id] = ""
            else:
                stem = o.entry.stem.lower()
                several = len({x.entry.path for x in w.options[n]}) < len(w.options[n])
                choice[s.id] = stem + (f"@{o.comp.appearance}" if several and o.comp.appearance else "")
        presets[a] = choice
    for sid, label, slot_words, att_words in ATTACHMENT_KINDS:
        sockets = [s for s in placer.slots.values() if any(x in s.name.lower() for x in slot_words)]
        if not sockets:
            continue
        fitting = [a for a in attachments if any(x in a.name.lower() for x in att_words)]
        if not fitting:
            continue
        sock = sockets[0]
        socket_matrix = placer.socket(sock.name)
        options = []
        for a in fitting:
            first_name = next(iter(a.options))
            o = a.options[first_name][0]
            m = placer.base_inverse @ (socket_matrix if socket_matrix is not None else np.eye(4)) @ o.comp.transform
            options.append(configs.PartOption(a.name, _humanize(a.name.removeprefix("w_att_").removeprefix("w_")),
                                              configs.ModelRef(str(o.entry.path), o.comp.appearance or None),
                                              _attach(configs, m)))
        slots.append(configs.Slot(f"attach_{sid}", f"{label} (on {sock.name})", options, False, None))
        for choice in presets.values():
            choice[f"attach_{sid}"] = ""
    category, group = base.entry.category, base.entry.group
    return configs.Configuration(f"app:{w.name}", f"{w.name} (configurable)", game, configs.ModelRef(str(base.entry.path),
                                 base.comp.appearance or None), slots, presets, category, group, SOURCE)


def _slot_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "part"


def _entity_slots(w: _Weapon, store_cache: dict) -> list[entities.SlotDef]:
    if not w.app.base_entity:
        return []
    base = next(iter(w.options.values()))[0].entry.path
    key = str(Path(base).parent)
    store = store_cache.get(key)
    if store is None:
        try:
            store = depot.store_for(Path(base))
        except (FormatError, OSError):
            return []
        store_cache[key] = store
    f = store.read(arc.normalize_path(w.app.base_entity))
    if f is None or not f.chunks:
        return []
    try:
        _meshes, slots, _apps = entities.read_entity(f)
    except (ValueError, FormatError):
        return []
    return slots


def build(folder: Path, game: str, entries: list[CatalogEntry], cache: Path | None) -> list:
    """Configurations for the weapons whose appearance files are in ``folder`` (its archives or loose)."""
    try:
        from ... import configs
    except ImportError:
        return []
    report = REPORTS[str(folder)] = Report()
    weapons = _read_weapons(Path(folder), entries, cache, report)
    attachments = [w for w in weapons if _is_attachment(w)]
    report.weapons, report.attachments = len(weapons) - len(attachments), len(attachments)
    out = []
    stores: dict = {}
    for w in weapons:
        if w in attachments or not w.name:
            continue
        if len(w.options) < 2 and not any(s.name for s in w.slots):
            continue  # one mesh and nothing to fit: nothing to configure
        try:
            c = _configuration(configs, w, game, attachments, _entity_slots(w, stores))
        except (FormatError, ValueError, KeyError, IndexError) as exc:
            report.errors.append(f"{w.name}: {exc}")
            log.warning("Weapon %s (%s) could not be configured", w.name, w.source, exc_info=True)
            continue
        if c.slots:
            out.append(c)
    report.configured = len(out)
    log.info("Weapon configurations of %s: %s", folder, report.notes()[0])
    return out


def appearance_files(path: Path, cache: Path | None) -> list[tuple[str, Path, int]]:
    """The appearance file(s) naming the mesh ``path`` (an archive member) and their base entities, as
    (depot path, archive, hash): what Collect copies so the weapon keeps its configuration."""
    if not depot.is_archive_member(path):
        return []
    want = depot.member_hash(path)
    out = []
    game = next((p for p in path.parents if (p / "bin" / "x64").is_dir()), None)
    archives = depot.game_archives(game) if game else [path.parent]
    for a in archives:
        try:
            ar = arc.open_archive(a)
            hashes = catalog.weapon_apps(a, cache)
        except (FormatError, OSError):
            continue
        for h in hashes:
            try:
                app = entities.read_app(cr2w.read_cr2w(ar.read(h)))
            except (FormatError, OSError, ValueError, IndexError):
                continue
            if not any(c.mesh_hash == want for ap in app.appearances for c in ap.meshes):
                continue
            out.append((ar.name_of(h) if h in ar.names else f"_unnamed\\weapons\\{h:016x}.app", a, h))
            if app.base_entity:
                eh = arc.path_hash(app.base_entity)
                holder = next((b for b in archives if _has(b, eh)), None)
                if holder is not None:
                    out.append((arc.normalize_path(app.base_entity), holder, eh))
    return out


def _has(archive: Path, h: int) -> bool:
    try:
        return h in arc.open_archive(archive).entries
    except (FormatError, OSError):
        return False


__all__ = ["REPORTS", "SOURCE", "Report", "appearance_files", "build", "notes_for"]
