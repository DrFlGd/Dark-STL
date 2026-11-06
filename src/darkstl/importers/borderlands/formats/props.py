"""Unreal Engine 3 tagged properties: the part of every object before its class's binary data.

Each property is a tag (name, type, size, array index, and for some types a
struct or enum name) followed by its value; the list ends with the name
``None``. Without the game's class definitions the type of an array's
elements isn't known, so arrays and structs keep their raw bytes and are read
the way the caller asks (object references, names, floats, or nested tagged
structs).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ....errors import FormatError
from .upk import Package, Reader


@dataclass
class ObjectRef:
    pkg: Package
    ref: int  # >0 export index + 1, <0 import, 0 none

    def __bool__(self) -> bool:
        return self.ref != 0

    @property
    def name(self) -> str:
        return self.pkg.object_name(self.ref)

    @property
    def path(self) -> str:
        return self.pkg.object_path(self.ref)

    @property
    def class_name(self) -> str:
        return self.pkg.object_class(self.ref)

    def __repr__(self) -> str:
        return f"ObjectRef({self.path or 'None'})"


@dataclass
class StructValue:
    pkg: Package
    struct_name: str
    raw: bytes
    base: int = 0

    def props(self) -> "Props":
        return read_props(Reader(self.raw, self.pkg, base=self.base))

    def vector(self) -> tuple[float, float, float]:
        if len(self.raw) == 12:
            return struct.unpack("<3f", self.raw)
        p = self.props()
        return (p.get("X", 0.0), p.get("Y", 0.0), p.get("Z", 0.0))

    def rotator(self) -> tuple[int, int, int]:
        """(pitch, yaw, roll) in Unreal units (65536 per turn)."""
        if len(self.raw) == 12:
            return struct.unpack("<3i", self.raw)
        p = self.props()
        return (p.get("Pitch", 0), p.get("Yaw", 0), p.get("Roll", 0))

    def linear_color(self) -> tuple[float, float, float, float]:
        if len(self.raw) == 16:
            return struct.unpack("<4f", self.raw)
        p = self.props()
        return (p.get("R", 0.0), p.get("G", 0.0), p.get("B", 0.0), p.get("A", 1.0))


@dataclass
class ArrayValue:
    pkg: Package
    count: int
    raw: bytes  # the elements, without the count
    base: int = 0

    def ints(self) -> list[int]:
        return list(struct.unpack(f"<{len(self.raw) // 4}i", self.raw[:len(self.raw) // 4 * 4]))

    def floats(self) -> list[float]:
        return list(struct.unpack(f"<{len(self.raw) // 4}f", self.raw[:len(self.raw) // 4 * 4]))

    def objects(self) -> list[ObjectRef]:
        return [ObjectRef(self.pkg, i) for i in self.ints()[:self.count]]

    def names(self) -> list[str]:
        r = Reader(self.raw, self.pkg)
        return [r.name() for _ in range(self.count)]

    def strings(self) -> list[str]:
        r = Reader(self.raw, self.pkg)
        return [r.fstring() for _ in range(self.count)]

    def structs(self) -> list["Props"]:
        """Elements that are tagged structs (most of Gearbox's data arrays)."""
        r = Reader(self.raw, self.pkg, base=self.base)
        return [read_props(r) for _ in range(self.count)]


class Props:
    """Property values by name; fixed-size arrays keep one value per array index."""

    def __init__(self) -> None:
        self.values: dict[str, dict[int, object]] = {}
        self.types: dict[str, str] = {}

    def set(self, name: str, index: int, kind: str, value) -> None:
        self.values.setdefault(name, {})[index] = value
        self.types[name] = kind

    def get(self, name: str, default=None, index: int = 0):
        v = self.values.get(name)
        if v is None:
            return default
        return v.get(index, default)

    def all(self, name: str) -> list:
        v = self.values.get(name) or {}
        return [v[i] for i in sorted(v)]

    def __contains__(self, name: str) -> bool:
        return name in self.values

    def names(self) -> list[str]:
        return list(self.values)

    def as_dict(self) -> dict:
        out = {}
        for k, v in self.values.items():
            out[k] = v[0] if list(v) == [0] else [v[i] for i in sorted(v)]
        return out

    def __repr__(self) -> str:
        return f"Props({self.as_dict()!r})"


_OBJECT_TYPES = {"ObjectProperty", "ClassProperty", "ComponentProperty", "InterfaceProperty"}


def read_props(r: Reader) -> Props:
    pkg = r.pkg
    version = pkg.summary.version
    props = Props()
    for _ in range(4096):
        name = r.name()
        if name == "None":
            return props
        kind = r.name()
        size = r.i32()
        index = r.i32()
        if size < 0 or size > r.remaining() + 8:
            raise FormatError(f"property {name} has a bad size {size}")
        if kind == "StructProperty":
            sname = r.name()
            start = r.tell
            props.set(name, index, kind, StructValue(pkg, sname, r.bytes(size), start))
        elif kind == "BoolProperty":
            v = r.u8() if version >= 673 else r.i32()
            props.set(name, index, kind, bool(v))
            if size:
                r.skip(size)
        elif kind == "ByteProperty":
            if version >= 633:
                r.name()  # enum name
            if size == 8:
                props.set(name, index, kind, r.name())
            else:
                data = r.bytes(size)
                props.set(name, index, kind, data[0] if data else 0)
        elif kind == "IntProperty":
            props.set(name, index, kind, r.i32())
        elif kind == "FloatProperty":
            props.set(name, index, kind, r.f32())
        elif kind == "NameProperty":
            props.set(name, index, kind, r.name())
        elif kind == "StrProperty":
            end = r.pos + size
            props.set(name, index, kind, r.fstring())
            r.pos = end
        elif kind in _OBJECT_TYPES:
            end = r.pos + size
            props.set(name, index, kind, ObjectRef(pkg, r.i32()))
            r.pos = end
        elif kind == "ArrayProperty":
            end = r.pos + size
            n = r.i32()
            start = r.tell
            props.set(name, index, kind, ArrayValue(pkg, n, r.bytes(size - 4), start))
            r.pos = end
        else:  # DelegateProperty, MapProperty, and anything else: keep the bytes
            props.set(name, index, kind, r.bytes(size))
    raise FormatError("too many properties")


def object_props(pkg: Package, r: Reader, has_net_index: bool = True) -> Props:
    """An object's properties, after the network index UE3 writes first."""
    if has_net_index and pkg.summary.version >= 322:
        r.i32()
    return read_props(r)


__all__ = ["ArrayValue", "ObjectRef", "Props", "StructValue", "object_props", "read_props"]
