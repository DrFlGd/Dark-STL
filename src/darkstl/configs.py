"""Configurations: a base model and the parts that can be fitted to it, chosen per slot.

Weapons in Cyberpunk 2077, the RE Engine games and Borderlands are kept as a body and many
separate part models (magazines, sights, barrels, stocks, muzzles). A :class:`Configuration`
says which model is the base and, for each slot, which part models fit it and where they go.
Choosing one option per slot gives an assembled model: a :class:`~darkstl.model.SourceModel` like
any other, so the Mesh, Displace, Cut and Export steps work on it unchanged.

Where configurations come from, the later overriding the earlier when two share a game and id:

1. an importer's :meth:`~darkstl.importers.base.Importer.configurations` hook, run on each game
   folder after it is scanned (relationships read from game data, or guessed from folders with
   :func:`from_folders`);
2. lists shipped with the app: ``importers/<importer>/configurations/*.json``;
3. lists inside a game folder: ``*.darkstl-configs.json``;
4. the user's own lists (made in the app): ``<settings>/configurations/*.json``.

A list is JSON::

    {"format": "darkstl-configurations", "version": 1, "game": "Cyberpunk 2077",
     "configurations": [{
        "id": "ajax", "label": "Militech Ajax", "category": "Weapons", "group": "Assault rifles",
        "base": {"file": "base/weapons/.../w_rifle_assault_militech_ajax.mesh"},
        "slots": [{"id": "mag", "label": "Magazine", "required": true, "default": "std",
                   "options": [{"id": "std", "label": "Standard", "model": {"file": "w_..._mag_std"}},
                               {"id": "ext", "label": "Extended", "model": {"file": "w_..._mag_ext"},
                                "attach": {"socket": "mag", "offset": [0, 0.01, 0]}}]}],
        "presets": {"Stock": {"mag": "std"}}}]}

A model reference's ``file`` is an absolute path, a path below the game folder (forward slashes,
any case; files inside archives too), or a file name or stem looked up in the game's library.
``variant`` picks a model inside a file that holds several and ``parts`` keeps only some of its
parts. An option's ``attach`` places it: ``socket`` names a point of the base model
(``SourceModel.sockets``), or of the part chosen for slot ``to``; ``matrix`` (16 numbers, row by
row, metres), then ``offset`` (metres), ``rotate`` (degrees about X, Y, Z) and ``scale`` follow.
Without any of these the part is taken to be modelled in place, as game weapon parts usually are.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .errors import FormatError
from .geometry.mesh import TriMesh, concatenate
from .model import CatalogEntry, Material, MeshOptions, Part, SourceModel

FORMAT = "darkstl-configurations"
VERSION = 1
FOLDER_PATTERN = "*.darkstl-configs.json"
USER_DIRS: list[Path] = []  # the user's own lists, set by the app (see set_user_folder)


# ---------------------------------------------------------------- the data


@dataclass
class ModelRef:
    file: str
    variant: str | None = None
    parts: list[int] | None = None  # part indices of that model to keep (None: all)


@dataclass
class Attach:
    socket: str | None = None  # a point on the base model (or on the part chosen for slot ``to``)
    to: str | None = None  # the slot whose part holds the socket
    matrix: list[float] | None = None  # 16 numbers, row by row, metres, applied after the socket
    offset: list[float] | None = None  # metres, after the matrix
    rotate: list[float] | None = None  # degrees about X, then Y, then Z
    scale: float | None = None

    def is_identity(self) -> bool:
        return not (self.socket or self.matrix or any(self.offset or []) or any(self.rotate or [])
                    or (self.scale not in (None, 1, 1.0)))


@dataclass
class PartOption:
    id: str
    label: str
    model: ModelRef
    attach: Attach | None = None


SLOT_KINDS = ("parts", "material", "finish")


@dataclass
class Slot:
    id: str
    label: str
    options: list[PartOption] = field(default_factory=list)
    required: bool = False  # not required: "None" can be chosen
    default: str | None = None  # an option id; None: the first option if required, else none
    kind: str = "parts"  # "parts": its option adds a part; "material": its option's materials repaint every
    # part; "finish": they repaint the base model only


@dataclass
class Configuration:
    id: str
    label: str
    game: str
    base: ModelRef
    slots: list[Slot] = field(default_factory=list)
    presets: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> slot id -> option id ("" = none)
    category: str = "Weapons"
    group: str = ""
    source: str = ""  # where it came from, for display ("bundled list", a file path, "folder")
    guessed: bool = False  # made up from file names and folders rather than read from a list

    def defaults(self) -> dict[str, str]:
        out = {}
        for s in self.slots:
            ids = [o.id for o in s.options]
            if s.default in ids:
                out[s.id] = s.default
            elif s.required and ids:
                out[s.id] = ids[0]
            else:
                out[s.id] = ""
        return out

    def choose(self, choices: dict | None) -> dict[str, str]:
        """``choices`` made complete and valid: every slot named, unknown options replaced by the default."""
        out = self.defaults()
        for s in self.slots:
            want = (choices or {}).get(s.id)
            if want is None:
                continue
            want = str(want)
            if want == "" and not s.required:
                out[s.id] = ""
            elif any(o.id == want for o in s.options):
                out[s.id] = want
        return out

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("guessed")
        d.pop("source")
        for s in d["slots"]:
            if s.get("kind") == "parts":
                s.pop("kind")
        return _drop_none(d)


def _drop_none(v):
    if isinstance(v, dict):
        return {k: _drop_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_drop_none(x) for x in v]
    return v


# ---------------------------------------------------------------- reading lists


def _ref(raw, where: str) -> ModelRef:
    if isinstance(raw, str):
        return ModelRef(raw)
    if not isinstance(raw, dict) or not raw.get("file"):
        raise FormatError(f"{where}: a model needs a 'file'")
    parts = raw.get("parts")
    return ModelRef(str(raw["file"]), raw.get("variant") or None,
                    None if parts is None else [int(p) for p in parts])


def _attach(raw, where: str) -> Attach | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return Attach(socket=raw)
    if not isinstance(raw, dict):
        raise FormatError(f"{where}: 'attach' is a socket name or an object")
    matrix = raw.get("matrix")
    if matrix is not None:
        matrix = [float(x) for x in np.asarray(matrix, float).reshape(-1)]
        if len(matrix) != 16:
            raise FormatError(f"{where}: a matrix has 16 numbers")
    def vec(k):
        return None if raw.get(k) is None else [float(x) for x in raw[k]][:3]

    return Attach(raw.get("socket") or None, raw.get("to") or None, matrix, vec("offset"), vec("rotate"),
                  None if raw.get("scale") is None else float(raw["scale"]))


def parse_attach(raw) -> Attach:
    return _attach(raw, "attach") or Attach()


def parse(raw: dict, game: str | None = None, source: str = "") -> list[Configuration]:
    """The configurations in a list already read from JSON."""
    if not isinstance(raw, dict) or raw.get("format", FORMAT) != FORMAT:
        raise FormatError(f"{source or 'This file'} is not a Dark STL configuration list")
    if int(raw.get("version", VERSION)) > VERSION:
        raise FormatError(f"{source or 'This list'} was made by a newer Dark STL")
    game = raw.get("game") or game or ""
    out = []
    for i, c in enumerate(raw.get("configurations") or []):
        where = f"{source}: configuration {c.get('id') or i + 1}"
        slots = []
        for j, s in enumerate(c.get("slots") or []):
            sid = str(s.get("id") or f"slot{j + 1}")
            options = [PartOption(str(o.get("id") or f"{sid}{k + 1}"), str(o.get("label") or o.get("id") or ""),
                                  _ref(o.get("model") or o.get("file"), f"{where}, slot {sid}"),
                                  _attach(o.get("attach"), f"{where}, slot {sid}"))
                       for k, o in enumerate(s.get("options") or [])]
            kind = str(s.get("kind") or "parts")
            if kind not in SLOT_KINDS:
                raise FormatError(f"{where}, slot {sid}: 'kind' is one of {', '.join(SLOT_KINDS)}")
            slots.append(Slot(sid, str(s.get("label") or sid), options, bool(s.get("required", False)),
                              s.get("default") or None, kind))
        presets = {str(k): {str(a): str(b or "") for a, b in (v or {}).items()}
                   for k, v in (c.get("presets") or {}).items()}
        cid = str(c.get("id") or "")
        if not cid or not c.get("base"):
            raise FormatError(f"{where}: needs an 'id' and a 'base'")
        out.append(Configuration(cid, str(c.get("label") or cid), str(c.get("game") or game), _ref(c["base"], where),
                                 slots, presets, str(c.get("category") or "Weapons"), str(c.get("group") or ""),
                                 source))
    return out


def load_file(path: str | os.PathLike, game: str | None = None) -> list[Configuration]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise FormatError(f"{path.name} couldn't be read: {exc}") from exc
    return parse(raw, game, str(path))


def dump(configs: Iterable[Configuration], game: str) -> dict:
    return {"format": FORMAT, "version": VERSION, "game": game,
            "configurations": [c.to_json() for c in configs]}


def _read_all(files: Iterable[Path], source: str | None = None) -> tuple[list[Configuration], list[str]]:
    out, problems = [], []
    for f in sorted(files):
        try:
            got = load_file(f)
        except FormatError as exc:
            problems.append(str(exc))
            continue
        for c in got:
            if source:
                c.source = source
        out += got
    return out, problems


def bundled() -> list[Configuration]:
    """The lists shipped with the app, beside each importer."""
    from .importers import IMPORTERS

    files = []
    for imp in IMPORTERS:
        mod = __import__(type(imp).__module__, fromlist=["__file__"])
        folder = Path(mod.__file__).parent / "configurations"
        if folder.is_dir() and Path(mod.__file__).name == "__init__.py":
            files += folder.glob("*.json")
    return _read_all(files, "Shipped with Dark STL")[0]


def set_user_folder(folder: str | os.PathLike | None) -> None:
    USER_DIRS[:] = [Path(folder)] if folder else []


USER_SOURCE = "Your configurations"


def user() -> list[Configuration]:
    return _read_all([f for d in USER_DIRS if d.is_dir() for f in d.glob("*.json")], USER_SOURCE)[0]


def in_folder(root: Path) -> tuple[list[Configuration], list[str]]:
    """Lists kept inside a game folder (``*.darkstl-configs.json``, at its top or one level down)."""
    root = Path(root)
    files = list(root.glob(FOLDER_PATTERN)) + list(root.glob(f"*/{FOLDER_PATTERN}"))
    return _read_all(files)


def user_file(game: str) -> Path | None:
    if not USER_DIRS:
        return None
    return USER_DIRS[0] / f"{re.sub(r'[^A-Za-z0-9 _.-]+', '_', game).strip() or 'game'}.json"


def save_user(config: Configuration) -> Path:
    """Keep ``config`` in the user's list for its game, in place of one with the same id."""
    path = user_file(config.game)
    if path is None:
        raise FormatError("There is no folder for your own configurations.")
    have = []
    if path.is_file():
        try:
            have = load_file(path, config.game)
        except FormatError:
            have = []
    have = [c for c in have if c.id != config.id] + [config]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dump(have, config.game), indent=2), "utf-8")
    os.replace(tmp, path)
    return path


def delete_user(game: str, config_id: str) -> bool:
    path = user_file(game)
    if path is None or not path.is_file():
        return False
    have = load_file(path, game)
    keep = [c for c in have if c.id != config_id]
    if len(keep) == len(have):
        return False
    path.write_text(json.dumps(dump(keep, game), indent=2), "utf-8")
    return True


# ---------------------------------------------------------------- finding the files


class Resolver:
    """Finds the library entries model references name, among one game's models."""

    def __init__(self, entries: Iterable[CatalogEntry], roots: Iterable[Path] = ()):
        self.roots = [Path(r) for r in roots]
        self.by_path: dict[str, CatalogEntry] = {}
        self.by_name: dict[str, list[CatalogEntry]] = {}
        # a model package stands for the game's file it was made from, found by that file's path and name
        # when the file itself isn't there (a game file, or a collected copy, is what configurations open)
        self.by_source: dict[str, CatalogEntry] = {}
        self.seen_as: dict[int, str] = {}
        for e in entries:
            if e.config is not None:
                continue
            names = {e.path.name.lower(), e.stem.lower()}
            self.by_path[_norm(e.path)] = e
            if e.importer == "package" and e.path.parent.name.lower().endswith(".darkpkg"):
                source = e.path.parent.with_name(e.path.parent.name[:-len(".darkpkg")])
                self.seen_as[id(e)] = _norm(source)
                self.by_source[_norm(source)] = e
                names.add(source.name.lower())
            for key in names:
                self.by_name.setdefault(key, []).append(e)

    def find(self, ref: ModelRef) -> Path | None:
        f = ref.file.strip().replace("\\", "/")
        if not f:
            return None
        p = Path(f)
        if p.is_absolute():
            hit = self.by_path.get(_norm(p))
            if hit is not None:
                return hit.path
            if p.is_file():
                return p
            hit = self.by_source.get(_norm(p))
            return hit.path if hit is not None else None
        low = f.lower().lstrip("./")
        for found in (self.by_path, self.by_source):
            for r in self.roots:
                hit = found.get(_norm(r / low))
                if hit is not None:
                    return hit.path
        name = low.rsplit("/", 1)[-1]
        cands = self.by_name.get(name, [])
        if "/" in low:
            cands = [e for e in cands if any(q.endswith("/" + low) or q == low
                                             for q in (_norm(e.path), self.seen_as.get(id(e), "")))]
        if not cands and "/" not in low:  # 'w_rifle_mag' for 'w_rifle_mag.mesh'
            cands = self.by_name.get(name.split(".", 1)[0], [])
        if not cands:
            return None
        cands.sort(key=lambda e: (e.importer == "package", e.is_lod, len(str(e.path))))
        return cands[0].path


def _norm(p) -> str:
    return os.path.normcase(str(p)).replace("\\", "/").lower()


def resolve(config: Configuration, resolver: Resolver) -> tuple[Configuration | None, list[str]]:
    """``config`` with every file an absolute path; options whose file isn't there are left out (and
    slots left empty), and a configuration whose base isn't there is None."""
    base = resolver.find(config.base)
    if base is None:
        return None, [f"{config.label}: the base model {config.base.file} wasn't found"]
    missing = []
    slots = []
    for s in config.slots:
        options = []
        for o in s.options:
            p = resolver.find(o.model)
            if p is None:
                missing.append(f"{config.label}, {s.label}: {o.model.file} wasn't found")
                continue
            options.append(PartOption(o.id, o.label, ModelRef(str(p), o.model.variant, o.model.parts), o.attach))
        if options:
            slots.append(Slot(s.id, s.label, options, s.required, s.default, s.kind))
    out = Configuration(config.id, config.label, config.game, ModelRef(str(base), config.base.variant, config.base.parts),
                        slots, config.presets, config.category, config.group, config.source, config.guessed)
    return out, missing


# ---------------------------------------------------------------- guessing from folders

SLOT_WORDS = [  # (slot id, label, words in a part's file name)
    ("magazine", "Magazine", ("mag", "magazine", "clip", "ammo", "drum", "cylinder")),
    ("sight", "Sight", ("scope", "sight", "optic", "optics", "reddot", "holo", "irons", "ironsight")),
    ("muzzle", "Muzzle", ("muzzle", "silencer", "suppressor", "compensator", "brake", "flash")),
    ("barrel", "Barrel", ("barrel",)),
    ("stock", "Stock", ("stock", "butt")),
    ("grip", "Grip", ("grip", "handle", "foregrip", "handguard")),
    ("rail", "Rail", ("rail", "mount")),
    ("accessory", "Accessory", ("laser", "flashlight", "light", "bayonet", "attachment", "acc")),
    ("blade", "Blade", ("blade",)),
    ("sheath", "Sheath", ("sheath", "scabbard", "holster")),
]
_BASE_WORDS = ("base", "body", "receiver", "frame", "main")
_TOKEN = re.compile(r"[a-z]+|\d+")
_BASE_END = re.compile(r"(?:^|[_\-. ])(?:" + "|".join(_BASE_WORDS) + r")\d*(?:_\d+)?$")


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def from_folders(entries: Iterable[CatalogEntry], game: str, categories: tuple[str, ...] = ("Weapons",),
                 max_files: int = 48) -> list[Configuration]:
    """Configurations guessed from how a game keeps a weapon's parts: in one folder, named after it
    (``w_rifle_ajax.mesh`` beside ``w_rifle_ajax_mag.mesh``), or named with the weapon's name before a
    double underscore (Cyberpunk's ``w_revolver__darra_nova__base1``, ``w_revolver__darra_nova__mag1``;
    these are grouped by that name, wherever the files are). The model whose name is shortest, or says
    it's the base or body (``base``, ``body1``...), is the base; the others are its parts, sorted into
    slots by words in their names (mag, scope, muzzle, stock...), each slot holding the parts that
    share a word. Parts are taken to be modelled in place."""
    folders: dict[tuple, list[CatalogEntry]] = {}
    for e in entries:
        if e.category in categories and not e.is_lod and e.config is None:
            stem = e.stem.lower()
            weapon = stem.rsplit("__", 1)[0] if "__" in stem else ""
            folders.setdefault((e.path.parent, weapon), []).append(e)
    out = []
    for items in folders.values():
        if len(items) < 2 or len(items) > max_files:
            continue
        stems = {e.stem.lower(): e for e in items}

        def is_base(s: str) -> bool:
            return _BASE_END.search(s) is not None

        bases = [s for s in stems if is_base(s)] or [min(stems, key=lambda s: (len(s), s))]
        groups: dict[str, list[str]] = {b: [] for b in bases}
        for s in stems:
            if s in groups:
                continue
            score = {b: _common_prefix(b if not is_base(b) else _strip_base(b), s) for b in bases}
            top = max(score.values())
            for best in (b for b in bases if score[b] == top):  # several bases of one weapon share its parts
                stem_b = _strip_base(best) if is_base(best) else best
                if _common_prefix(stem_b, s) < max(3, int(len(stem_b) * 0.6)):
                    continue
                groups[best].append(s)
        for b, parts in groups.items():
            if not parts:
                continue
            base = stems[b]
            prefix = _strip_base(b) if is_base(b) else b
            slots: dict[str, Slot] = {}
            for s in sorted(parts):
                rest = s[_common_prefix(prefix, s):].strip("_-. ")
                words = _TOKEN.findall(rest) or [rest or s]
                sid, label = next(((i, lab) for i, lab, ws in SLOT_WORDS if any(w in ws for w in words)),
                                  (None, None))
                if sid is None:
                    sid, label = f"part_{rest or s}", (rest or s).replace("_", " ").strip().capitalize()
                slot = slots.setdefault(sid, Slot(sid, label, [], False, None))
                slot.options.append(PartOption(s, (rest or s).replace("_", " ").strip(),
                                               ModelRef(str(stems[s].path))))
            for slot in slots.values():  # in place parts: the first of each kind shown, so the weapon looks whole
                slot.default = slot.options[0].id
            out.append(Configuration(f"auto:{b}", f"{base.label} (configurable)", game, ModelRef(str(base.path)),
                                     list(slots.values()), {}, base.category, base.group,
                                     "Guessed from the files in the weapon's folder", guessed=True))
    return out


def _strip_base(stem: str) -> str:
    return re.sub(r"[_\-. ]*(" + "|".join(_BASE_WORDS) + r")\d*(?:_\d+)?$", "", stem)


# ---------------------------------------------------------------- assembling


def _rotation(deg: list[float] | None) -> np.ndarray:
    m = np.eye(3)
    for axis, a in enumerate(deg or []):
        r = math.radians(a)
        c, s = math.cos(r), math.sin(r)
        if axis == 0:
            rot = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        elif axis == 1:
            rot = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        else:
            rot = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        m = rot @ m
    return m


def attach_matrix(attach: Attach | None, socket: np.ndarray | None) -> np.ndarray:
    """The 4x4 matrix (column vectors) that places a part."""
    m = np.eye(4) if socket is None else np.asarray(socket, float).reshape(4, 4)
    if attach is None:
        return m
    if attach.matrix:
        m = m @ np.asarray(attach.matrix, float).reshape(4, 4)
    local = np.eye(4)
    local[:3, :3] = _rotation(attach.rotate) * (attach.scale if attach.scale else 1.0)
    if attach.offset:
        local[:3, 3] = (list(attach.offset) + [0, 0, 0])[:3]
    return m @ local


def transform_mesh(mesh: TriMesh, m: np.ndarray) -> TriMesh:
    if np.allclose(m, np.eye(4)):
        return mesh
    out = mesh.copy()
    out.vertices = mesh.vertices @ m[:3, :3].T + m[:3, 3]
    lin = m[:3, :3]
    if out.corner_normals is not None:
        nm = np.linalg.inv(lin).T
        n = out.corner_normals.reshape(-1, 3) @ nm.T
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        out.corner_normals = n.reshape(out.corner_normals.shape).astype(np.float32)
    if np.linalg.det(lin) < 0:  # a mirror turns the triangles inside out: wind them the other way
        out.faces = out.faces[:, [0, 2, 1]]
        if out.corner_uvs is not None:
            out.corner_uvs = out.corner_uvs[:, [0, 2, 1]]
        if out.corner_normals is not None:
            out.corner_normals = out.corner_normals[:, [0, 2, 1]]
    return out


@dataclass
class _Component:
    slot: str | None  # None for the base
    label: str
    model: SourceModel
    keep: list[int] | None  # the model's part indices used (None: all, or a model without parts)
    matrix: np.ndarray
    part_base: int = 0  # first global part index
    n_parts: int = 1
    material_base: int = 0


Loader = Callable[[str, str | None], SourceModel]


def assemble(config: Configuration, choices: dict | None, load: Loader) -> SourceModel:
    """The model ``config`` makes with ``choices`` (slot id -> option id): the base and each chosen
    part, placed, as one :class:`SourceModel`. ``load(path, variant)`` opens a model file."""
    chosen = config.choose(choices)
    base = load(config.base.file, config.base.variant)
    comps = [_Component(None, "Base", base, config.base.parts, np.eye(4))]
    by_slot: dict[str, _Component] = {}
    notes, info = [], [f"Configuration {config.label}" + (f" ({config.source})" if config.source else "")]
    slots = {s.id: s for s in config.slots}
    placing: set[str] = set()

    def place(slot_id: str) -> _Component | None:
        if slot_id in by_slot:
            return by_slot[slot_id]
        s = slots.get(slot_id)
        opt = next((o for o in s.options if o.id == chosen.get(slot_id)), None) if s else None
        if opt is None or slot_id in placing:
            return None
        placing.add(slot_id)
        try:
            model = load(opt.model.file, opt.model.variant)
        except FormatError as exc:
            notes.append(f"{s.label}: {opt.label} couldn't be opened ({exc})")
            return None
        socket = None
        a = opt.attach
        if a and a.socket:
            holder = place(a.to) if a.to else comps[0]
            if holder is None:
                notes.append(f"{s.label}: {opt.label} goes on the {a.to} part, and none is chosen; placed at the origin")
            elif a.socket not in holder.model.sockets:
                notes.append(f"{s.label}: the {holder.label.lower()} has no point called {a.socket}; "
                             f"{opt.label} is placed at the origin")
            else:
                socket = holder.matrix @ np.asarray(holder.model.sockets[a.socket], float).reshape(4, 4)
        c = _Component(slot_id, f"{s.label}: {opt.label}", model, opt.model.parts, attach_matrix(a, socket))
        by_slot[slot_id] = c
        info.append(f"{s.label}: {opt.label} ({Path(opt.model.file).name})")
        return c

    for s in config.slots:
        if s.kind == "parts":
            place(s.id)
    comps += [by_slot[s.id] for s in config.slots if s.id in by_slot]

    parts: list[Part] = []
    materials: list[Material] = []
    textures = {}
    for i, c in enumerate(comps):
        prefix = "" if i == 0 else f"{c.slot}:"
        c.material_base = len(materials)
        def ren(k, prefix=prefix):
            return None if k is None else prefix + k

        for mat in c.model.materials:
            materials.append(Material(mat.name if i == 0 else f"{c.label} / {mat.name}", ren(mat.albedo),
                                      ren(mat.normal), ren(mat.height), mat.normal_scale, mat.guessed,
                                      mat.details, mat.normal_green, ren(mat.specular), ren(mat.detail_normal),
                                      mat.detail_scale))
        for k, t in c.model.textures.items():
            textures[prefix + k] = t
        c.part_base = len(parts)
        if c.model.parts:
            keep = list(range(len(c.model.parts))) if c.keep is None else [p for p in c.keep
                                                                          if 0 <= p < len(c.model.parts)]
            c.keep = keep
            for p in keep:
                mp = c.model.parts[p]
                parts.append(Part(mp.name if i == 0 else f"{c.label} / {mp.name}", mp.material + c.material_base,
                                  mp.vertices, mp.triangles))
            c.n_parts = len(keep)
        else:
            mesh = c.model.build_mesh(MeshOptions())
            parts.append(Part(c.label, c.material_base, mesh.n_vertices, mesh.n_faces))
            c.keep, c.n_parts = None, 1
    for s in config.slots:  # a material slot paints every component with its option's materials, a finish the base
        if s.kind not in ("material", "finish"):
            continue
        opt = next((o for o in s.options if o.id == chosen.get(s.id)), None)
        if opt is None:
            continue
        try:
            paint = load(opt.model.file, opt.model.variant)
        except FormatError as exc:
            notes.append(f"{s.label}: {opt.label} couldn't be opened ({exc})")
            continue
        if not paint.materials:
            notes.append(f"{s.label}: {opt.label} has no materials")
            continue
        if not paint.textures and paint.paint_for is None:  # it would only take the parts' own textures away
            notes.append(f"{s.label}: {opt.label} has no textures, so the parts keep their own")
            notes += [f"{s.label}: {n}" for n in paint.notes]
            continue
        prefix = f"{s.id}:"
        for k, t in paint.textures.items():
            textures[prefix + k] = t
        targets = comps[:1] if s.kind == "finish" else comps
        for c in targets:  # its first material replaces each model's first, and so on; the rest stay
            baked = None
            if paint.paint_for is not None:  # a paint mixed per model: its colour, laid out for this one
                try:
                    tex = paint.paint_for(c.model)
                except FormatError as exc:
                    tex = None
                    notes.append(f"{s.label}: {opt.label} couldn't be painted on the {c.label.lower()} ({exc})")
                if tex is not None:
                    baked = f"{prefix}{c.slot or 'base'}:colour"
                    textures[baked] = tex
            for j in range(min(len(c.model.materials), len(paint.materials))):
                old, new = materials[c.material_base + j], paint.materials[j]

                def key(k, own, j=j):  # a mixed paint keeps the part's own maps where it has none
                    if k:
                        return prefix + k
                    return own if paint.paint_for is not None else None

                materials[c.material_base + j] = Material(
                    f"{old.name} ({opt.label})", baked if baked and j == 0 else key(new.albedo, old.albedo),
                    key(new.normal, old.normal), key(new.height, old.height),
                    new.normal_scale, new.guessed, new.details, new.normal_green,
                    key(new.specular, old.specular), key(new.detail_normal, old.detail_normal), new.detail_scale)
        info.append(f"{s.label}: {opt.label}")
        notes += [f"{s.label}: {n}" for n in paint.notes]
    flag_keys = {o.key for o in base.options}

    def build(options: MeshOptions) -> TriMesh:
        meshes, group_base = [], 0
        for i, c in enumerate(comps):
            if c.model.parts:
                local = [p for j, p in enumerate(c.keep) if options.parts is None or c.part_base + j in options.parts]
                if not local:
                    continue
                sel = None if c.keep == list(range(len(c.model.parts))) and len(local) == len(c.keep) else set(local)
            else:
                if options.parts is not None and c.part_base not in options.parts:
                    continue
                sel = None
            own = {o.key for o in c.model.options}
            flags = {k: v for k, v in options.flags.items() if k in own}
            pose = options.pose if i == 0 or _poses(c.model, options.pose) else None
            # bone adjustments go by bone name: every part with those bones (a face and hair on the body's
            # skeleton) takes them
            bones = options.bones if i == 0 or _has_bones(c.model, options.bones) else None
            mesh = c.model.build_mesh(MeshOptions(sel, flags, pose, bones))
            mesh = transform_mesh(mesh, c.matrix)
            if i:
                mesh = mesh.copy()
                mesh.face_material = mesh.face_material + c.material_base
            mesh.face_group = mesh.face_group + group_base
            group_base = int(mesh.face_group.max()) + 1 if mesh.n_faces else group_base
            meshes.append(mesh)
        out = concatenate(meshes)
        if meshes and any(m.corner_uvs is None for m in meshes) and out.corner_uvs is None:
            uv = [m.corner_uvs if m.corner_uvs is not None else np.zeros((m.n_faces, 3, 2), np.float32)
                  for m in meshes if m.n_faces]
            out.corner_uvs = np.concatenate(uv) if uv else None
        return out

    if base.animations is not None and config.slots:
        posed = [c for c in comps[1:] if c.model.animations is not None and _same_animations(base, c.model)]
        if len(posed) < len(comps) - 1:
            notes.append("A pose moves the base model" + (" and the parts that share its animations" if posed else "")
                         + "; the other parts fitted to it stay where they are.")
    for c in comps[1:]:
        notes += [f"{c.label}: {n}" for n in c.model.notes]
    model = SourceModel(
        name=config.label, path=Path(config.base.file), importer=base.importer, build=build, game=config.game or base.game,
        parts=parts, materials=materials, textures=textures,
        options=[o for o in base.options if o.key in flag_keys], info=info + base.info, notes=base.notes + notes,
        animations=base.animations, sockets=dict(base.sockets), rig=base.rig, adjusts_bones=True,
    )
    model.configuration = configuration_json(config, chosen)
    return model


def _poses(model: SourceModel, pose: tuple[str, int] | None) -> bool:
    """Whether ``model`` has the animation ``pose`` names: a character's face and hair meshes share the
    body's animations, so one pose moves them all."""
    if pose is None or model.animations is None:
        return False
    try:
        return pose[0] in model.animations
    except Exception:  # noqa: BLE001 - an unreadable archive leaves the part unposed
        return False


def _has_bones(model: SourceModel, bones: dict | None) -> bool:
    """Whether ``model``'s skeleton has any of the bones ``bones`` adjusts."""
    if not bones or model.rig is None:
        return False
    return bool(set(bones) & set(model.rig.names))


def _same_animations(base: SourceModel, other: SourceModel) -> bool:
    try:
        return bool(set(base.animations.names) & set(other.animations.names))
    except Exception:  # noqa: BLE001
        return False


def configuration_json(config: Configuration, chosen: dict[str, str]) -> dict:
    """What the page shows: the slots and their options, the choice in each, the presets."""
    return {
        "id": config.id, "label": config.label, "source": config.source, "guessed": config.guessed,
        "game": config.game, "category": config.category, "group": config.group,
        "base": _drop_none(asdict(config.base)), "base_name": Path(config.base.file).name,
        "chosen": chosen, "defaults": config.defaults(),
        "presets": [{"name": k, "choices": config.choose(v)} for k, v in config.presets.items()],
        "slots": [{"id": s.id, "label": s.label, "required": s.required, "default": s.default, "kind": s.kind,
                   "options": [{"id": o.id, "label": o.label, "file": Path(o.model.file).name, "path": o.model.file,
                                "variant": o.model.variant, "parts": o.model.parts,
                                "attach": None if o.attach is None else _drop_none(asdict(o.attach))}
                               for o in s.options]} for s in config.slots],
    }


def choices_key(choices: dict[str, str]) -> str:
    return json.dumps(choices, sort_keys=True, separators=(",", ":"))
