"""Packages: the packed component lists inside ``.app`` and ``.ent`` files.

An appearance's ``compiledData`` buffer (and an entity template's) is not
CR2W chunks but a *package*: a small header, a pool of imports (depot paths,
or their hashes in appearances), a pool of names, and the chunks, each a
field table (name, type, offset) followed by the values. Values are encoded
as in CR2W chunks except that handles are chunk indices, strings are
16-bit-length prefixed, and resource references index the package's own
import pool. The layout follows WolvenKit's RedPackageReader (GPL, used as
a reference only).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ...fromsoft.formats.binary import BinaryReader, FormatError
from .cr2w import FUNDAMENTALS, KNOWN_BITFIELDS, KNOWN_ENUMS, BufferRef, Handle, Raw, Red, Ref, parse_type


@dataclass(frozen=True)
class HashRef:
    """A resource named by the hash of its depot path (appearance packages keep imports that way)."""

    hash: int

    @property
    def path(self) -> str:
        return f"{self.hash:016x}"


@dataclass
class Package:
    version: int
    imports: list[Ref | HashRef]
    names: list[str]
    chunks: list[Red]
    cruids: list[int]

    def chunk(self, handle: Handle | None) -> Red | None:
        if handle is None or not 0 <= handle.index < len(self.chunks):
            return None
        return self.chunks[handle.index]

    def chunks_of(self, class_name: str) -> list[Red]:
        return [c for c in self.chunks if c.cls == class_name]


class _PackageReader:
    def __init__(self, data: bytes, imports_as_hash: bool):
        self.r = BinaryReader(data)
        self.imports_as_hash = imports_as_hash
        self.version = 0
        self.imports: list[Ref | HashRef] = []
        self.names: list[str] = []
        self.chunks: list[Red] = []

    def read(self) -> Package:
        r = self.r
        if len(r) < 20:
            raise FormatError("The package is too short")
        self.version = r.u8()
        if not 2 <= self.version <= 4:
            raise FormatError(f"Unsupported package version {self.version}")
        r.u8()
        sections = r.u16()
        if sections not in (6, 7):
            raise FormatError(f"Unsupported package layout ({sections} sections)")
        r.u32()  # component count
        ref_desc = ref_data = 0
        if sections == 7:
            ref_desc, ref_data = r.u32(), r.u32()
        name_desc, name_data, chunk_desc, chunk_data = r.u32(), r.u32(), r.u32(), r.u32()
        if ref_desc != 0:
            raise FormatError("Unexpected package header")
        r.i16()  # CRUID index
        cruids = [r.u64() for _ in range(r.u16())]
        base = r.pos
        for i in range((ref_data - ref_desc) // 4):
            bits = r.unpack_at(base + ref_desc + 4 * i, "I")[0]
            off, size = bits & 0x7FFFFF, (bits >> 23) & 0xFF
            raw = r.get_bytes(base + off, size)
            if size == 8 and (self.imports_as_hash or not all(0x20 <= b < 0x7F for b in raw)):
                self.imports.append(HashRef(struct.unpack("<Q", raw)[0]))
            else:
                self.imports.append(Ref(raw.decode("utf-8", "replace")))
        for i in range((name_data - name_desc) // 4):
            bits = r.unpack_at(base + name_desc + 4 * i, "I")[0]
            off = bits & 0xFFFFFF
            end = r.data.find(b"\0", base + off)
            self.names.append(bytes(r.data[base + off:end if end >= 0 else len(r)]).decode("utf-8", "replace"))
        descs = [r.unpack_at(base + chunk_desc + 8 * i, "II") for i in range((chunk_data - chunk_desc) // 8)]
        self.chunks = [Red(self._name(type_id)) for type_id, _ in descs]
        for obj, (_, off) in zip(self.chunks, descs):
            try:
                self._read_class(base + off, obj)
            except (FormatError, struct.error, IndexError, ValueError, UnicodeDecodeError):
                obj.props.setdefault("__error__", True)
        return Package(self.version, self.imports, self.names, self.chunks, cruids)

    def _name(self, index: int) -> str:
        if 0 <= index < len(self.names):
            return self.names[index]
        raise FormatError(f"Package name {index} is out of range")

    # -- values -------------------------------------------------------------

    def _read_class(self, start: int, obj: Red) -> Red:
        r = self.r
        r.seek(start)
        count = r.u16()
        fields = [r.unpack("HHI") for _ in range(count)]
        for name_id, type_id, off in fields:
            type_name = self._name(type_id)
            r.seek(start + off)
            try:
                obj.props[self._name(name_id)] = self._read_value(parse_type(type_name))
            except (FormatError, struct.error, IndexError, ValueError, UnicodeDecodeError):
                obj.props[self._name(name_id)] = Raw(type_name, b"")
        return obj

    def _read_value(self, t: tuple) -> Any:
        r = self.r
        kind = t[0]
        if kind == "array":
            count = r.i32()
            if count < 0 or count > len(r):
                raise FormatError("Bad array count")
            return [self._read_value(t[1]) for _ in range(count)]
        if kind in ("static", "fixed"):
            count = r.i32()
            if count < 0 or count > t[1]:
                raise FormatError("Bad fixed array count")
            return [self._read_value(t[2]) for _ in range(count)]
        if kind in ("handle", "whandle"):
            index = r.i16() if (self.version == 2 and kind == "handle") else r.i32()
            return Handle(index) if index >= 0 else None
        if kind in ("rRef", "raRef"):
            index = r.i16()
            if 0 <= index < len(self.imports):
                return self.imports[index]
            return None
        if kind in ("curveData", "multiChannelCurve", "ptr", "script_ref"):
            raise FormatError(f"Package type {kind} is not read")
        name = t[1]
        fmt = FUNDAMENTALS.get(name)
        if fmt is not None:
            v = r.unpack(fmt)[0]
            return bool(v) if name == "Bool" else v
        if name == "CName":
            return self._name(r.u16())
        if name in ("String", "CString"):
            return r.read_bytes(r.u16()).decode("utf-8", "replace")
        if name == "NodeRef":
            return r.read_bytes(r.i16()).decode("utf-8", "replace")
        if name == "LocalizationString":
            r.u64()
            return r.read_bytes(r.u16()).decode("utf-8", "replace")
        if name == "TweakDBID":
            if self.version == 4:
                return r.u64()
            return r.read_bytes(r.i16()).decode("utf-8", "replace")
        if name in ("CDateTime", "CRUID", "gamedataLocKeyWrapper"):
            return r.u64()
        if name == "CGUID":
            return r.read_bytes(16)
        if name == "DataBuffer":
            n = r.u32()
            if n == 0x80000000:
                return None
            if n > 0x80000000:
                return BufferRef((n ^ 0x80000000) - 1)
            return r.read_bytes(n)
        if name == "serializationDeferredDataBuffer":
            return BufferRef(r.u16() - 1)
        if name == "SharedDataBuffer":
            return r.read_bytes(r.u32())
        if name in KNOWN_BITFIELDS or name in _BITFIELDS:
            return [self._name(r.u16()) for _ in range(r.u8())]
        if name in KNOWN_ENUMS or name in _ENUMS:
            return self._name(r.u16())
        # a class (its field table) or an enum: a field table starts with a small count, an enum with a name index
        return self._read_class(r.pos, Red(name))


_ENUMS = {
    "shadowsShadowCastingMode", "ERenderingPlane", "ERenderObjectType", "entMeshComponentLODMode",
    "entForcedLodDistance", "physicsSimulationType", "physicsFilterDataSource", "NavGenNavigationSetting",
    "entRagdollBodyPartState", "animAnimGraphType", "entMeshComponentVisibilityMode", "ECensorFlags",
}
_BITFIELDS = {"RenderSceneLayerMask"}


def read_package(data: bytes, imports_as_hash: bool = False) -> Package:
    """The chunks of a package buffer. ``imports_as_hash`` for an appearance's ``compiledData``, whose
    imports are 64-bit path hashes rather than paths."""
    return _PackageReader(data, imports_as_hash).read()


__all__ = ["HashRef", "Package", "read_package"]
