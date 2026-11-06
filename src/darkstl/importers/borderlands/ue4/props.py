"""Unreal Engine 4 tagged properties (versioned packages, as Borderlands 3 and Wonderlands cook them).

Like UE3 each property is a tag and a value and the list ends with ``None``,
but the tag carries more: a struct's GUID, an array's element type, a map's
key and value types, and an optional property GUID. Arrays of structs repeat
the element's tag once before the elements.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ....errors import FormatError
from ..formats.props import ObjectRef, Props
from ..formats.upk import Reader

_OBJECT_TYPES = {"ObjectProperty", "ClassProperty", "WeakObjectProperty", "InterfaceProperty"}
_SOFT_TYPES = {"SoftObjectProperty", "SoftClassProperty", "AssetObjectProperty", "AssetClassProperty"}
_FIXED = {"IntProperty": ("<i", 4), "UInt32Property": ("<I", 4), "Int64Property": ("<q", 8),
          "UInt64Property": ("<Q", 8), "Int16Property": ("<h", 2), "UInt16Property": ("<H", 2),
          "Int8Property": ("<b", 1), "FloatProperty": ("<f", 4), "DoubleProperty": ("<d", 8)}
# Structs written as raw bytes rather than as tagged properties.
NATIVE_STRUCTS = {
    "Vector": 12, "Vector2D": 8, "Vector4": 16, "Rotator": 12, "Quat": 16, "LinearColor": 16, "Color": 4,
    "Guid": 16, "IntPoint": 8, "IntVector": 12, "Box": 25, "Box2D": 17, "Plane": 16, "Matrix": 64,
    "SoftObjectPath": None, "SoftClassPath": None, "PerPlatformFloat": 5, "PerPlatformInt": 8,
    "PerPlatformBool": 5, "FrameNumber": 4, "DateTime": 8, "Timespan": 8,
}


@dataclass
class SoftRef:
    """A soft object path: ``/Game/Path/Asset.Asset`` (and a sub-path)."""

    path: str
    sub: str = ""

    def __bool__(self) -> bool:
        return bool(self.path) and self.path != "None"

    @property
    def package(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


@dataclass
class Struct4:
    pkg: object
    struct_name: str
    raw: bytes
    base: int = 0

    def props(self) -> Props:
        return read_props(Reader(self.raw, self.pkg, base=self.base))

    def vector(self) -> tuple[float, ...]:
        if len(self.raw) == 12:
            return struct.unpack("<3f", self.raw)
        if len(self.raw) == 24:
            return struct.unpack("<3d", self.raw)
        p = self.props()
        return (p.get("X", 0.0), p.get("Y", 0.0), p.get("Z", 0.0))

    def rotator(self) -> tuple[float, float, float]:
        """(pitch, yaw, roll) in degrees."""
        if len(self.raw) == 12:
            return struct.unpack("<3f", self.raw)
        if len(self.raw) == 24:
            return struct.unpack("<3d", self.raw)
        p = self.props()
        return (p.get("Pitch", 0.0), p.get("Yaw", 0.0), p.get("Roll", 0.0))


@dataclass
class Array4:
    pkg: object
    inner: str
    count: int
    raw: bytes
    base: int = 0

    def __len__(self) -> int:
        return self.count

    def objects(self) -> list[ObjectRef]:
        if self.inner not in _OBJECT_TYPES:
            return []
        n = min(self.count, len(self.raw) // 4)
        return [ObjectRef(self.pkg, v) for v in struct.unpack(f"<{n}i", self.raw[:4 * n])]

    def names(self) -> list[str]:
        r = Reader(self.raw, self.pkg)
        return [r.name() for _ in range(self.count)]

    def soft(self) -> list[SoftRef]:
        r = Reader(self.raw, self.pkg)
        return [_soft(r) for _ in range(self.count)]

    def structs(self) -> list[Props]:
        """Elements that are tagged structs; the element tag comes first."""
        if self.inner != "StructProperty" or not self.count:
            return []
        r = Reader(self.raw, self.pkg, base=self.base)
        _tag_name, sname, _size = _inner_tag(r)
        if sname in NATIVE_STRUCTS:
            return []
        return [read_props(r) for _ in range(self.count)]

    def native_structs(self) -> tuple[str, list[bytes]]:
        """(struct name, raw bytes per element) for arrays of fixed-size native structs."""
        if self.inner != "StructProperty" or not self.count:
            return "", []
        r = Reader(self.raw, self.pkg, base=self.base)
        _tag_name, sname, size = _inner_tag(r)
        each = size // self.count if self.count else 0
        return sname, [r.bytes(each) for _ in range(self.count)]


def _soft(r: Reader) -> SoftRef:
    path = r.name()
    return SoftRef(path, r.fstring())


def _inner_tag(r: Reader) -> tuple[str, str, int]:
    name = r.name()
    kind = r.name()
    size = r.i32()
    r.i32()  # array index
    sname = ""
    if kind == "StructProperty":
        sname = r.name()
        r.skip(16)
    if r.u8():
        r.skip(16)
    del name
    return kind, sname, size


def read_props(r: Reader) -> Props:
    pkg = r.pkg
    v = pkg.versions
    props = Props()
    for _ in range(8192):
        name = r.name()
        if name == "None":
            return props
        kind = r.name()
        size = r.i32()
        index = r.i32()
        extra = ""
        bool_value = False
        if kind == "StructProperty":
            extra = r.name()
            if v.ue4 >= 441:
                r.skip(16)
        elif kind == "BoolProperty":
            bool_value = bool(r.u8())
        elif kind in ("ByteProperty", "EnumProperty"):
            extra = r.name()
        elif kind in ("ArrayProperty", "SetProperty", "OptionalProperty"):
            extra = r.name()
        elif kind == "MapProperty":
            extra = r.name()
            r.name()
        if v.ue4 >= 503 and r.u8():
            r.skip(16)
        if v.ue5 >= 1011:
            if r.u8() & 2:
                r.skip(5)
        if size < 0 or size > r.remaining():
            raise FormatError(f"property {name} has a bad size {size}")
        start = r.pos
        end = start + size
        try:
            if kind == "BoolProperty":
                props.set(name, index, kind, bool_value)
            elif kind == "StructProperty":
                props.set(name, index, kind, Struct4(pkg, extra, r.bytes(size), r.base + start))
            elif kind in _FIXED:
                fmt, n = _FIXED[kind]
                props.set(name, index, kind, struct.unpack(fmt, r.bytes(n))[0])
            elif kind == "ByteProperty":
                props.set(name, index, kind, r.name() if size == 8 else (r.u8() if size else 0))
            elif kind in ("EnumProperty", "NameProperty"):
                props.set(name, index, kind, r.name())
            elif kind == "StrProperty":
                props.set(name, index, kind, r.fstring())
            elif kind in _OBJECT_TYPES:
                props.set(name, index, kind, ObjectRef(pkg, r.i32()))
            elif kind in _SOFT_TYPES:
                props.set(name, index, kind, _soft(r))
            elif kind == "ArrayProperty":
                n = r.i32()
                props.set(name, index, kind, Array4(pkg, extra, n, r.bytes(size - 4), r.base + start + 4))
            else:  # text, maps, sets, delegates...: keep the bytes
                props.set(name, index, kind, r.bytes(size))
        except FormatError:
            props.set(name, index, kind, None)
        r.pos = end
    raise FormatError("too many properties")


__all__ = ["Array4", "NATIVE_STRUCTS", "SoftRef", "Struct4", "read_props"]
