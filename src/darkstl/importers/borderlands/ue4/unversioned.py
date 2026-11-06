"""Unversioned properties: values written without names or types, read through a game's type mappings.

A cooked Unreal 5 object (and some Unreal 4 games') starts with a small header
instead of property tags: fragments of (skip so many schema slots, then so
many values follow), with a bit mask marking the values left at zero. The
values follow in schema order, each in its type's plain binary form; a struct
that has no native serializer is itself a header and values. Which property a
schema slot is, and its type, comes from the ``.usmap`` mappings (see
:mod:`usmap`).

Values come out as the tagged-property reader's types (:class:`Struct4`,
:class:`Array4`, :class:`ObjectRef`...), so the mesh, texture and material code
reads both. Follows CUE4Parse (Apache 2.0).
"""

from __future__ import annotations

import struct

from ....errors import FormatError
from ..formats.props import ObjectRef, Props
from ..formats.upk import Reader
from .props import Array4, SoftRef, Struct4
from .usmap import Mappings, PropType

_FIXED = {"IntProperty": ("<i", 4), "UInt32Property": ("<I", 4), "Int64Property": ("<q", 8),
          "UInt64Property": ("<Q", 8), "Int16Property": ("<h", 2), "UInt16Property": ("<H", 2),
          "Int8Property": ("<b", 1), "FloatProperty": ("<f", 4), "DoubleProperty": ("<d", 8)}
_OBJECTS = {"ObjectProperty", "ClassProperty", "WeakObjectProperty", "InterfaceProperty"}
_SOFT = {"SoftObjectProperty", "SoftClassProperty", "AssetObjectProperty"}

# Structs with their own binary form: their size, (UE4 floats, UE5 doubles) where large world coordinates apply.
_NATIVE_LWC = {"Vector": (12, 24), "Vector2D": (8, 16), "Vector4": (16, 32), "Rotator": (12, 24),
               "Quat": (16, 32), "Plane": (16, 32), "Box": (25, 49), "Box2D": (17, 33), "Sphere": (16, 32),
               "Matrix": (64, 128), "Transform": (None, None)}
_NATIVE = {"Vector3f": 12, "Vector3d": 24, "Vector2f": 8, "Vector4f": 16, "Vector4d": 32, "Rotator3f": 12,
           "Rotator3d": 24, "Quat4f": 16, "Quat4d": 32, "Plane4f": 16, "Plane4d": 32, "Sphere3f": 16,
           "Sphere3d": 32, "Box3f": 25, "Box2f": 17, "Matrix44f": 64, "LinearColor": 16, "Color": 4, "Guid": 16,
           "GUID": 16, "IntPoint": 8, "Int32Point": 8, "IntVector": 12, "IntVector2": 8, "IntVector4": 16,
           "UintVector": 12, "UintVector2": 8, "UintVector4": 16, "Int64Point": 16, "Int64Vector": 24,
           "FrameNumber": 4, "DateTime": 8, "Timespan": 8, "PerPlatformFloat": 8, "PerPlatformInt": 8,
           "PerPlatformBool": 8, "NavAgentSelector": 4, "Vector_NetQuantize": None, "Vector_NetQuantize10": None,
           "Vector_NetQuantize100": None, "Vector_NetQuantizeNormal": None, "TwoVectors": None,
           "RichCurveKey": 29, "SimpleCurveKey": 8, "MovieSceneFrameRange": 25}
_LWC_ALIASES = {"Vector_NetQuantize": "Vector", "Vector_NetQuantize10": "Vector", "Vector_NetQuantize100": "Vector",
                "Vector_NetQuantizeNormal": "Vector"}


class UStruct4(Struct4):
    """A struct read through the mappings: its properties are already decoded."""

    def __init__(self, pkg, struct_name: str, props: Props, raw: bytes = b"", base: int = 0):
        super().__init__(pkg, struct_name, raw, base)
        self._props = props

    def props(self) -> Props:
        return self._props

    def vector(self) -> tuple[float, ...]:
        if self.raw:
            return super().vector()
        p = self._props
        return (p.get("X", 0.0), p.get("Y", 0.0), p.get("Z", 0.0))


class UArray4(Array4):
    """An array read through the mappings: its elements are already decoded."""

    def __init__(self, pkg, inner: PropType, items: list):
        super().__init__(pkg, inner.kind, len(items), b"", 0)
        self.inner_type = inner
        self.items = items

    def objects(self) -> list[ObjectRef]:
        return [x for x in self.items if isinstance(x, ObjectRef)]

    def names(self) -> list[str]:
        return [str(x) for x in self.items]

    def soft(self) -> list[SoftRef]:
        return [x for x in self.items if isinstance(x, SoftRef)]

    def structs(self) -> list[Props]:
        return [x.props() for x in self.items if isinstance(x, UStruct4)]

    def native_structs(self) -> tuple[str, list[bytes]]:
        if self.inner != "StructProperty":
            return "", []
        return self.inner_type.struct, [x.raw for x in self.items if isinstance(x, Struct4)]


class UnversionedReader:
    """Reads one package's unversioned objects through a game's mappings."""

    def __init__(self, mappings: Mappings, large_world: bool, soft_paths_v5: bool = True):
        self.m = mappings
        self.large_world = large_world  # UE5: vectors and rotators are doubles
        self.soft_v5 = soft_paths_v5  # UE5.1+: soft paths are (package, asset) names and a sub-path

    # -- the header ------------------------------------------------------------------

    @staticmethod
    def header(r: Reader) -> list[tuple[int, bool]]:
        """(schema index, has a value) for every property the header lists."""
        frags = []
        for _ in range(4096):
            packed = r.u16()
            frags.append((packed & 0x7F, bool(packed & 0x80), packed >> 9, bool(packed & 0x100)))
            if packed & 0x100:
                break
        else:
            raise FormatError("unversioned header without an end")
        zero_bits = sum(f[2] for f in frags if f[1])
        mask = 0
        if zero_bits:
            nbytes = 1 if zero_bits <= 8 else 2 if zero_bits <= 16 else 4 * ((zero_bits + 31) // 32)
            mask = int.from_bytes(r.bytes(nbytes), "little")
        out, index, zi = [], 0, 0
        for skip, zeroes, values, _last in frags:
            index += skip
            for _ in range(values):
                nonzero = True
                if zeroes:
                    nonzero = not (mask >> zi) & 1
                    zi += 1
                out.append((index, nonzero))
                index += 1
        return out

    def read_object(self, r: Reader, class_name: str) -> Props:
        """An object's (or struct's) properties, in schema order."""
        props = Props()
        if not self.m.chain(class_name):
            raise FormatError(f"the type mappings don't know {class_name}")
        for index, nonzero in self.header(r):
            hit = self.m.resolve(class_name, index)
            if hit is None:
                raise FormatError(f"{class_name}: schema slot {index} isn't in the type mappings")
            info, element = hit
            if not nonzero:
                props.set(info.name, element, info.type.kind, self._zero(r.pkg, info.type))
                continue
            props.set(info.name, element, info.type.kind, self.value(r, info.type, top=True))
        return props

    # -- values ----------------------------------------------------------------------

    def _zero(self, pkg, t: PropType):
        if t.kind in _FIXED:
            return 0
        if t.kind == "BoolProperty":
            return False
        if t.kind in _OBJECTS:
            return ObjectRef(pkg, 0)
        if t.kind == "ArrayProperty":
            return UArray4(pkg, t.inner or PropType("Unknown"), [])
        if t.kind == "StructProperty":
            return UStruct4(pkg, t.struct, Props())
        if t.kind in ("NameProperty", "EnumProperty", "StrProperty"):
            return ""
        return None

    def _name(self, r: Reader) -> str:
        return r.name()

    def _soft(self, r: Reader) -> SoftRef:
        if self.soft_v5:
            package = r.name()
            asset = r.name()
            sub = r.fstring()
            path = f"{package}.{asset}" if asset and asset != "None" else package
            return SoftRef(path, sub)
        return SoftRef(r.name(), r.fstring())

    def value(self, r: Reader, t: PropType, top: bool = False):
        k = t.kind
        if k in _FIXED:
            fmt, n = _FIXED[k]
            return struct.unpack(fmt, r.bytes(n))[0]
        if k == "BoolProperty":
            return bool(r.u8())
        if k == "ByteProperty":
            return r.u8()
        if k == "EnumProperty":
            if top and t.inner is not None and t.inner.kind != "Unknown":
                v = self.value(r, t.inner)
                names = self.m.enums.get(t.enum, {})
                n = names.get(v)
                return (f"{t.enum}::{n}" if n and "::" not in n else n) if n is not None else str(v)
            return r.name()
        if k == "NameProperty":
            return r.name()
        if k in ("StrProperty", "VerseStringProperty"):
            return r.fstring()
        if k in ("Utf8StrProperty", "AnsiStrProperty"):
            n = r.i32()
            return r.bytes(max(0, n)).decode("utf-8", "replace").rstrip("\0")
        if k in _OBJECTS:
            return ObjectRef(r.pkg, r.i32())
        if k == "LazyObjectProperty":
            r.skip(16)
            return None
        if k in _SOFT:
            return self._soft(r)
        if k == "StructProperty":
            return self.struct_value(r, t.struct)
        if k in ("ArrayProperty", "SetProperty"):
            inner = t.inner or PropType("Unknown")
            if k == "SetProperty":
                for _ in range(r.count(1 << 20)):
                    self.value(r, inner)
            n = r.count(1 << 24)
            if inner.kind in _FIXED:
                fmt, size = _FIXED[inner.kind]
                raw = r.bytes(n * size)
                return UArray4(r.pkg, inner, list(struct.unpack(f"<{n}{fmt[1]}", raw)) if n else [])
            if inner.kind == "ByteProperty":
                return UArray4(r.pkg, inner, list(r.bytes(n)))
            return UArray4(r.pkg, inner, [self.value(r, inner) for _ in range(n)])
        if k == "MapProperty":
            key = t.inner or PropType("Unknown")
            val = t.value or PropType("Unknown")
            for _ in range(r.count(1 << 20)):
                self.value(r, key)
            out = []
            for _ in range(r.count(1 << 22)):
                out.append((self.value(r, key), self.value(r, val)))
            return out
        if k == "TextProperty":
            return self._text(r)
        if k == "DelegateProperty":
            r.i32()
            return r.name()
        if k in ("MulticastDelegateProperty", "MulticastInlineDelegateProperty"):
            for _ in range(r.count(1 << 16)):
                r.i32()
                r.name()
            return None
        if k == "FieldPathProperty":
            path = [r.name() for _ in range(r.count(64))]
            r.i32()
            return ".".join(path)
        if k == "OptionalProperty":
            if r.u8():
                return self.value(r, t.inner or PropType("Unknown"))
            return None
        raise FormatError(f"can't read a {k} without knowing its size")

    def _text(self, r: Reader) -> str:
        r.u32()  # flags
        kind = struct.unpack("<b", r.bytes(1))[0]
        if kind == -1:
            if r.i32():
                return r.fstring()
            return ""
        if kind == 0:  # base: namespace, key, source string
            r.fstring()
            r.fstring()
            return r.fstring()
        if kind == 11:  # string table entry
            r.name()
            return r.fstring()
        raise FormatError(f"text of history type {kind} isn't supported")

    def struct_value(self, r: Reader, name: str):
        base = name
        if name in _LWC_ALIASES:
            base = _LWC_ALIASES[name]
        if base in _NATIVE_LWC:
            if base == "Transform":
                rot = self.struct_value(r, "Quat")
                pos = self.struct_value(r, "Vector")
                scale = self.struct_value(r, "Vector")
                return UStruct4(r.pkg, name, _props(Rotation=rot, Translation=pos, Scale3D=scale))
            small, large = _NATIVE_LWC[base]
            start = r.pos
            return Struct4(r.pkg, name, r.bytes(large if self.large_world else small), r.base + start)
        if name in _NATIVE and _NATIVE[name] is not None:
            start = r.pos
            return Struct4(r.pkg, name, r.bytes(_NATIVE[name]), r.base + start)
        if name == "TwoVectors":
            a = self.struct_value(r, "Vector")
            b = self.struct_value(r, "Vector")
            return UStruct4(r.pkg, name, _props(v1=a, v2=b))
        if name in ("SoftObjectPath", "SoftClassPath", "StringAssetReference", "StringClassReference"):
            return self._soft(r)
        if name in ("GameplayTag",):
            return r.name()
        if name == "GameplayTagContainer":
            return UArray4(r.pkg, PropType("NameProperty"), [r.name() for _ in range(r.count(1 << 16))])
        if name in ("PerQualityLevelInt", "PerQualityLevelFloat"):
            r.i32()  # cooked
            fmt = "<i" if name.endswith("Int") else "<f"
            default = struct.unpack(fmt, r.bytes(4))[0]
            r.skip(8 * r.count(64))
            return default
        if name == "ExpressionInput" or name.endswith("MaterialInput"):
            raise FormatError(f"{name} values aren't supported")
        return UStruct4(r.pkg, name, self.read_object(r, name))


def _props(**kw) -> Props:
    p = Props()
    for k, v in kw.items():
        p.set(k, 0, "StructProperty", v)
    return p


_MAPPINGS: dict[str, object] = {}


def _settings_dir():
    try:
        from ....app.config import config_dir
    except Exception:  # noqa: BLE001 - no app settings in tests
        return None
    return config_dir()


def forget_mappings() -> None:
    _MAPPINGS.clear()


def reader_for(pkg, root, game: str | None):
    """The function that reads ``pkg``'s unversioned objects, or one that says the type mappings are missing."""
    from pathlib import Path

    from .usmap import find_mappings

    key = str(root)
    if key not in _MAPPINGS:
        _MAPPINGS[key] = find_mappings(Path(root), game or "", _settings_dir())
    m = _MAPPINGS[key]
    if m is None:
        def missing(r, e):
            raise FormatError(f"{pkg.stem} is cooked without property names, so Dark STL needs the game's type "
                              f"mappings: put a .usmap file made for this version of the game in the game folder "
                              f"or in Dark STL's settings folder")
        return missing
    reader = UnversionedReader(m, pkg.versions.ue5 >= 1004, pkg.versions.ue5 >= 1007)
    return lambda r, e: read_unversioned(reader, r, e.class_name)


def read_unversioned(reader: UnversionedReader, r: Reader, class_name: str) -> Props:
    """An object's properties, or a :class:`FormatError` naming what the mappings couldn't explain."""
    try:
        return reader.read_object(r, class_name)
    except (FormatError, struct.error, IndexError) as e:
        raise FormatError(f"{class_name}'s properties couldn't be read with the type mappings ({e})") from e


__all__ = ["UArray4", "UStruct4", "UnversionedReader", "forget_mappings", "read_unversioned", "reader_for"]
