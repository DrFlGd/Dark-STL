""".usmap type mappings: the property layouts of a game's classes and structs.

Unreal 5 games may cook their packages "unversioned": properties are written
without names or types, in the order the class declares them, so reading them
needs the class layouts the game was built with. A ``.usmap`` file (written by
dumping tools from the running game) lists every class and struct: its parent,
and per serialized property its schema index, array size, name and type (with
inner types for arrays, sets, maps and enums), plus the enums' value names.

The layout follows CUE4Parse (Apache 2.0) and jmap/unreal_mappings (MIT).
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ....errors import FormatError
from .pak import decompress

MAGIC = 0x30C4
TYPES = [
    "ByteProperty", "BoolProperty", "IntProperty", "FloatProperty", "ObjectProperty", "NameProperty",
    "DelegateProperty", "DoubleProperty", "ArrayProperty", "StructProperty", "StrProperty", "TextProperty",
    "InterfaceProperty", "MulticastDelegateProperty", "WeakObjectProperty", "LazyObjectProperty",
    "AssetObjectProperty", "SoftObjectProperty", "UInt64Property", "UInt32Property", "UInt16Property",
    "Int64Property", "Int16Property", "Int8Property", "MapProperty", "SetProperty", "EnumProperty",
    "FieldPathProperty", "OptionalProperty", "Utf8StrProperty", "AnsiStrProperty", "ClassProperty",
    "MulticastInlineDelegateProperty", "SoftClassProperty", "VerseStringProperty", "VerseDynamicProperty",
    "VerseFunctionProperty",
]
# usmap versions: 1 package versioning, 2 16-bit name lengths, 3 large enums, 4 explicit enum values,
# 5 engine versioning.
_COMPRESSION = {0: "None", 1: "Oodle", 2: "Brotli", 3: "Zstd"}


@dataclass
class PropType:
    kind: str
    struct: str = ""
    enum: str = ""
    inner: "PropType | None" = None
    value: "PropType | None" = None


@dataclass
class PropInfo:
    index: int  # schema index inside its own struct (super structs' properties come first)
    array_dim: int
    name: str
    type: PropType


@dataclass
class StructInfo:
    name: str
    super: str
    count: int  # schema slots, array dimensions included
    props: list[PropInfo]
    _by_index: dict[int, tuple[PropInfo, int]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for p in self.props:
            for j in range(max(1, p.array_dim)):
                self._by_index[p.index + j] = (p, j)

    def at(self, index: int) -> tuple[PropInfo, int] | None:
        return self._by_index.get(index)


@dataclass
class Mappings:
    structs: dict[str, StructInfo]
    enums: dict[str, dict[int, str]]
    source: str = ""

    def get(self, name: str) -> StructInfo | None:
        s = self.structs.get(name)
        if s is None:
            s = self._lower().get(name.lower())
        return s

    def _lower(self) -> dict[str, StructInfo]:
        low = self.__dict__.get("_low")
        if low is None:
            low = {k.lower(): v for k, v in self.structs.items()}
            self.__dict__["_low"] = low
        return low

    def chain(self, name: str) -> list[StructInfo]:
        """The struct and its parents, the root first (the order of the schema indices)."""
        out, seen = [], set()
        s = self.get(name)
        while s is not None and s.name not in seen and len(out) < 64:
            seen.add(s.name)
            out.append(s)
            s = self.get(s.super) if s.super else None
        return out[::-1]

    def resolve(self, name: str, index: int) -> tuple[PropInfo, int] | None:
        """The property (and its array element) at a schema index of ``name`` counted over the whole chain."""
        for s in self.chain(name):
            if index < s.count:
                return s.at(index)
            index -= s.count
        return None


class _R:
    __slots__ = ("d", "p")

    def __init__(self, d: bytes):
        self.d, self.p = d, 0

    def take(self, fmt: str):
        v = struct.unpack_from(fmt, self.d, self.p)
        self.p += struct.calcsize(fmt)
        return v[0] if len(v) == 1 else v

    def bytes(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise FormatError("the .usmap file is cut short")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b


def parse(data: bytes, source: str = "") -> Mappings:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if len(data) < 16:
        raise FormatError("the .usmap file is empty")
    r = _R(data)
    if r.take("<H") != MAGIC:
        raise FormatError("not a .usmap file")
    version = r.take("<B")
    if version > 5:
        raise FormatError(f"usmap version {version} isn't supported")
    if version >= 1 and r.take("<i"):  # package versioning
        if version >= 5:
            r.take("<HHHI")  # engine version: major, minor, patch, changelist
            n = r.take("<i")
            r.bytes(n if n >= 0 else -2 * n)  # branch
        r.take("<ii")
        for _ in range(r.take("<i")):
            r.bytes(20)
        r.take("<I")
    method = r.take("<B")
    csize, usize = r.take("<II")
    raw = r.bytes(csize)
    name = _COMPRESSION.get(method)
    if name is None:
        raise FormatError(f"the .usmap file uses an unknown compression ({method})")
    body = raw if name == "None" else decompress(name.lower(), raw, usize)
    return _body(body, version, source)


def _body(data: bytes, version: int, source: str) -> Mappings:
    r = _R(data)
    names = []
    for _ in range(r.take("<I")):
        n = r.take("<H") if version >= 2 else r.take("<B")
        names.append(r.bytes(n).decode("utf-8", "replace"))

    def name() -> str:
        i = r.take("<i")
        return names[i] if 0 <= i < len(names) else ""

    def ptype() -> PropType:
        k = r.take("<B")
        kind = TYPES[k] if k < len(TYPES) else f"Unknown{k}"
        t = PropType(kind)
        if kind == "EnumProperty":
            t.inner = ptype()
            t.enum = name()
        elif kind == "StructProperty":
            t.struct = name()
        elif kind in ("ArrayProperty", "SetProperty", "OptionalProperty"):
            t.inner = ptype()
        elif kind == "MapProperty":
            t.inner = ptype()
            t.value = ptype()
        return t

    enums: dict[str, dict[int, str]] = {}
    for _ in range(r.take("<I")):
        en = name()
        n = r.take("<H") if version >= 3 else r.take("<B")
        values = {}
        for j in range(n):
            if version >= 4:
                v = r.take("<Q")
                values[v] = name()
            else:
                values[j] = name()
        enums.setdefault(en, values)
    structs: dict[str, StructInfo] = {}
    for _ in range(r.take("<I")):
        sn = name()
        sup = name()
        count, ser = r.take("<HH")
        props = []
        for _ in range(ser):
            idx, dim = r.take("<HB")
            pn = name()
            props.append(PropInfo(idx, dim, pn, ptype()))
        structs[sn] = StructInfo(sn, sup, count, props)
    return Mappings(structs, enums, source)


_CACHE: dict[str, Mappings] = {}


def load(path: Path) -> Mappings:
    key = str(path)
    if key not in _CACHE:
        _CACHE[key] = parse(Path(path).read_bytes(), Path(path).name)
    return _CACHE[key]


def _game_words() -> dict[str, tuple[str, ...]]:
    """Words in a .usmap's file name that say which game it was made for, from the games' profiles
    (``unreal.usmap.words``; a game without any: its name)."""
    try:
        from ...unreal.profile import unreal_profiles
    except ImportError:
        return {}
    return {p.name: tuple(p.usmap_words) or (p.name.lower(),) for p in unreal_profiles()}


def _names_game(path: Path, game: str, words: dict | None = None) -> bool:
    low = path.stem.lower().replace("_", " ").replace("-", " ")
    words = _game_words() if words is None else words
    return any(w in low or w in low.replace(" ", "") for w in words.get(game, ()))


def _for_game(candidates: list[Path], game: str, settings_dir: Path | None) -> list[Path]:
    """The game folder's own mappings first, then the settings folder's named for this game, then the
    settings folder's named for no game this importer knows; ones named for another game are left out."""
    def in_settings(p: Path) -> bool:
        return settings_dir is not None and p.parent == Path(settings_dir)

    words = _game_words()
    own = [p for p in candidates if not in_settings(p)]
    shared = [p for p in candidates if in_settings(p)]
    named = [p for p in shared if _names_game(p, game, words)]
    others = [p for p in shared if p not in named
              and not any(_names_game(p, g, words) for g in words if g != game)]
    return own + named + others


def _project_paks(root: Path) -> list[Path]:
    """``<root>/<Project>/Content/Paks`` folders (Returnal/Content/Paks...)."""
    try:
        return [d / "Content" / "Paks" for d in Path(root).iterdir() if (d / "Content" / "Paks").is_dir()]
    except OSError:
        return []


def find_mappings(root: Path, game: str, settings_dir: Path | None = None) -> Mappings | None:
    """The type mappings for a game: the newest .usmap in the game folder (or its Paks folder) or in Dark STL's
    settings folder that fits the game."""
    path = find_mappings_file(root, game, settings_dir)
    return load(path) if path is not None else None


def find_mappings_file(root: Path, game: str, settings_dir: Path | None = None) -> Path | None:
    """The .usmap :func:`find_mappings` reads."""
    candidates: list[Path] = []
    for d in [root, root / "OakGame", root / "OakGame" / "Content" / "Paks", *_project_paks(root), settings_dir]:
        if d is not None and Path(d).is_dir():
            candidates += sorted(Path(d).glob("*.usmap"), key=lambda p: -p.stat().st_mtime)
    candidates = _for_game(candidates, game, settings_dir)
    fallback = None
    for c in candidates:
        if c.is_file():
            try:
                m = load(c)
            except (FormatError, OSError, ValueError, struct.error):
                continue
            fit = _fits(m, game)
            if fit is True:
                return c
            if fit is None and fallback is None:
                fallback = c
    return fallback


def _game_types() -> dict[str, str]:
    """What the classes of a game's own code are called (the mappings of Borderlands 3, Wonderlands and 4 are full
    of Oak* types), from the games' profiles (``unreal.usmap.type_prefix``): mappings with another game's types
    are for that game."""
    try:
        from ...unreal.profile import unreal_profiles
    except ImportError:
        return {}
    return {p.name: p.type_prefix for p in unreal_profiles() if p.type_prefix}


def _fits(m: Mappings, game: str) -> bool | None:
    """True: made for ``game``; False: made for another game; None: can't tell (taken if nothing fits better)."""
    def has(prefix: str) -> bool:
        return sum(1 for n in m.structs if n.lower().startswith(prefix)) >= 20

    types = _game_types()
    mine = types.get(game)
    if mine and has(mine):
        return True
    if any(has(p) for p in set(types.values()) if p != mine):
        return False
    return None if mine else True


__all__ = ["Mappings", "PropInfo", "PropType", "StructInfo", "find_mappings", "find_mappings_file", "load", "parse"]
