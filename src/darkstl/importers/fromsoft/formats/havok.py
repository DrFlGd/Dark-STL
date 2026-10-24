"""Havok HKX files: the skeletons and animations of the FromSoftware games.

Havok serialises objects in two containers:

* **packfiles** (``57 E0 E0 57 10 C0 C0 10``): Dark Souls (``hk_2010.2.0-r1``, 32-bit),
  Bloodborne and Dark Souls III (``hk_2014.1.0-r1``, 64-bit). Objects are laid out as the
  game's C++ structs, with pointers patched in by fixup tables. The struct layouts are not in
  the file, so the few classes the animations need are described here (:data:`CLASSES`), for
  both pointer sizes.
* **tagfiles** (``TAG0``): Dark Souls Remastered (2015), Sekiro (2016), Elden Ring (2018).
  These carry their own type descriptions (member names, offsets and kinds), so any class is
  read from them.

Either way, :func:`read_hkx` returns the objects as plain dicts: a ``"__class__"`` key and one
key per member, with ``hkArray`` members as lists or numpy arrays, pointers as the pointed
dict (or None), and strings as ``str``. :mod:`..animation` turns ``hkaSkeleton`` and
``hkaSplineCompressedAnimation`` objects into poses.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .binary import FormatError

PACKFILE_MAGIC = bytes.fromhex("57E0E05710C0C010")
TAGFILE_MAGIC = b"TAG0"


def is_hkx(data: bytes) -> bool:
    return data[:8] == PACKFILE_MAGIC or data[4:8] == TAGFILE_MAGIC


@dataclass
class Hkx:
    format: str  # "packfile" or "tagfile"
    version: str  # "hk_2014.1.0-r1", "20150100", ...
    root: dict | None  # the hkRootLevelContainer

    @property
    def variants(self) -> list[dict]:
        """The root container's named variants (``name``, ``className``, ``variant``)."""
        return list((self.root or {}).get("namedVariants") or [])

    def find(self, class_name: str) -> list[dict]:
        """Every object of ``class_name`` reachable from the root, in the order first met."""
        out, seen = [], set()

        def walk(obj):
            if isinstance(obj, dict):
                if id(obj) in seen:
                    return
                seen.add(id(obj))
                if obj.get("__class__") == class_name:
                    out.append(obj)
                for k, v in obj.items():
                    if k != "__class__":
                        walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(self.root)
        return out


def read_hkx(data: bytes, compendium: bytes | None = None) -> Hkx:
    """Read a Havok file. Tagfiles that keep their types in a shared ``.compendium`` file (Elden Ring)
    need that file's bytes as ``compendium``."""
    from . import dcx

    data = dcx.maybe_decompress(data)
    if data[:8] == PACKFILE_MAGIC:
        return _read_packfile(data)
    if data[4:8] == TAGFILE_MAGIC:
        return _Tagfile(data, dcx.maybe_decompress(compendium) if compendium else None).read()
    if data[4:8] == b"TCM0":
        raise FormatError("Havok compendium files hold only type information, not animations")
    raise FormatError("Not a Havok file")


def is_compendium(data: bytes) -> bool:
    return data[4:8] == b"TCM0"


# ---------------------------------------------------------------------------
# Packfiles

# Member types: how big and how aligned each is, for 32-bit and 64-bit files.
_PRIMITIVES = {
    "bool": (1, 1, "?"), "i8": (1, 1, "b"), "u8": (1, 1, "B"), "i16": (2, 2, "h"), "u16": (2, 2, "H"),
    "i32": (4, 4, "i"), "u32": (4, 4, "I"), "f32": (4, 4, "f"),
}
_ARRAY_DTYPES = {"bool": "?", "i8": "i1", "u8": "u1", "i16": "i2", "u16": "u2", "i32": "i4", "u32": "u4", "f32": "f4",
                 "vec4": ("f4", 4), "qs": ("f4", 12)}

# Class layouts, as the games' C++ structs: (member name, type). Types are the primitives above,
# "vec4" (hkVector4), "qs" (hkQsTransform: translation, rotation, scale), "str" (hkStringPtr),
# "ptr:Class", "array:type" and "class:Name" (embedded). The base class comes first; members of a
# class continue from where the base's members end (Havok reuses the base's tail padding), each
# aligned to its own alignment. "since" leaves out members younger than the file.
CLASSES: dict[str, dict] = {
    "hkReferencedObject": {"members": [("vtable", "ptr:"), ("memSizeAndFlags", "u16"), ("refCount", "u16")]},
    "hkRootLevelContainer": {"members": [("namedVariants", "array:class:hkRootLevelContainerNamedVariant")]},
    "hkRootLevelContainerNamedVariant": {"members": [("name", "str"), ("className", "str"),
                                                     ("variant", "ptr:hkReferencedObject")]},
    "hkaAnimationContainer": {"base": "hkReferencedObject", "members": [
        ("skeletons", "array:ptr:hkaSkeleton"), ("animations", "array:ptr:hkaAnimation"),
        ("bindings", "array:ptr:hkaAnimationBinding"), ("attachments", "array:ptr:"), ("skins", "array:ptr:")]},
    "hkaBone": {"members": [("name", "str"), ("lockTranslation", "bool")]},
    "hkaSkeletonLocalFrameOnBone": {"members": [("localFrame", "ptr:"), ("boneIndex", "i16")]},
    "hkaSkeletonPartition": {"members": [("name", "str"), ("startBoneIndex", "i16"), ("numBones", "i16")]},
    "hkaSkeleton": {"base": "hkReferencedObject", "members": [
        ("name", "str"), ("parentIndices", "array:i16"), ("bones", "array:class:hkaBone"),
        ("referencePose", "array:qs"), ("referenceFloats", "array:f32"), ("floatSlots", "array:str"),
        ("localFrames", "array:class:hkaSkeletonLocalFrameOnBone"),
        ("partitions", "array:class:hkaSkeletonPartition", 2014)]},
    "hkaAnimatedReferenceFrame": {"base": "hkReferencedObject", "members": []},
    "hkaAnnotationTrack": {"members": [("trackName", "str"), ("annotations", "array:")]},
    "hkaAnimation": {"base": "hkReferencedObject", "members": [
        ("type", "i32"), ("duration", "f32"), ("numberOfTransformTracks", "i32"), ("numberOfFloatTracks", "i32"),
        ("extractedMotion", "ptr:hkaAnimatedReferenceFrame"), ("annotationTracks", "array:class:hkaAnnotationTrack")]},
    "hkaSplineCompressedAnimation": {"base": "hkaAnimation", "members": [
        ("numFrames", "i32"), ("numBlocks", "i32"), ("maxFramesPerBlock", "i32"), ("maskAndQuantizationSize", "i32"),
        ("blockDuration", "f32"), ("blockInverseDuration", "f32"), ("frameDuration", "f32"),
        ("blockOffsets", "array:u32"), ("floatBlockOffsets", "array:u32"), ("transformOffsets", "array:u32"),
        ("floatOffsets", "array:u32"), ("data", "array:u8"), ("endian", "i32")]},
    "hkaInterleavedUncompressedAnimation": {"base": "hkaAnimation", "members": [
        ("transforms", "array:qs"), ("floats", "array:f32")]},
    "hkaAnimationBinding": {"base": "hkReferencedObject", "members": [
        ("originalSkeletonName", "str"), ("animation", "ptr:hkaAnimation"),
        ("transformTrackToBoneIndices", "array:i16"), ("floatTrackToFloatSlotIndices", "array:i16"),
        ("partitionIndices", "array:i16", 2014), ("blendHint", "i8")]},
}


@dataclass
class Layout:
    """Where each member of a class sits, for one pointer size."""

    name: str
    members: list[tuple[str, str, int]]  # (name, type, offset), the base class's first
    size: int
    align: int


class _LayoutTable:
    def __init__(self, ptr: int, year: int):
        self.ptr = ptr
        self.year = year
        self._done: dict[str, Layout] = {}

    def size_align(self, t: str) -> tuple[int, int]:
        if t in _PRIMITIVES:
            return _PRIMITIVES[t][:2]
        if t == "vec4":
            return 16, 16
        if t == "qs":
            return 48, 16
        if t == "str" or t.startswith("ptr:"):
            return self.ptr, self.ptr
        if t.startswith("array:"):
            return (12, 4) if self.ptr == 4 else (16, 8)
        if t.startswith("class:"):
            lay = self.get(t[6:])
            return lay.size, lay.align
        raise FormatError(f"Unknown Havok member type {t}")

    def get(self, name: str) -> Layout:
        if name in self._done:
            return self._done[name]
        spec = CLASSES.get(name)
        if spec is None:
            raise FormatError(f"No layout for Havok class {name}")
        members: list[tuple[str, str, int]] = []
        at, align = 0, 1
        if spec.get("base"):
            base = self.get(spec["base"])
            members += base.members
            at, align = base.end, base.align
        for entry in spec["members"]:
            mname, mtype = entry[0], entry[1]
            if len(entry) > 2 and self.year < entry[2]:
                continue
            size, al = self.size_align(mtype)
            at = -(-at // al) * al
            members.append((mname, mtype, at))
            at += size
            align = max(align, al)
        lay = Layout(name, members, -(-at // align) * align, align)
        lay.end = at  # type: ignore[attr-defined]  # where a subclass's members continue
        self._done[name] = lay
        return lay


def layout(name: str, pointer_size: int = 8, year: int = 2014) -> Layout:
    """The struct layout of a class in a packfile (for tests and inspection)."""
    return _LayoutTable(pointer_size, year).get(name)


def _packfile_year(version: str) -> int:
    digits = "".join(c for c in version if c.isdigit())
    year = int(digits[:4]) if len(digits) >= 4 else 2014
    return 2014 if year >= 2013 else 2010


class _Packfile:
    def __init__(self, data: bytes):
        self.data = data
        e = "<" if data[0x11] else ">"
        self.e = e
        (self.ptr,) = struct.unpack_from("B", data, 0x10)
        (file_version,) = struct.unpack_from(e + "i", data, 0x0C)
        (self.n_sections, self.data_index, _, self.names_index, _) = struct.unpack_from(e + "5i", data, 0x14)
        self.version = data[0x28:0x38].split(b"\0", 1)[0].decode("ascii", "replace")
        if file_version >= 11:
            (predicate_size,) = struct.unpack_from(e + "h", data, 0x3E)
            at, header_size = 0x40 + predicate_size, 0x40
        else:
            at, header_size = 0x40, 0x30
        if self.ptr not in (4, 8):
            raise FormatError(f"Havok packfile with pointer size {self.ptr}")
        self.sections = []
        for _ in range(self.n_sections):
            tag = data[at:at + 19].split(b"\0", 1)[0].decode("ascii", "replace")
            start, local, glob, virt, exports, imports, end = struct.unpack_from(e + "7I", data, at + 20)
            self.sections.append({"tag": tag, "start": start, "local": local, "glob": glob, "virt": virt,
                                  "exports": exports, "imports": imports, "end": end})
            at += header_size
        self.layouts = _LayoutTable(self.ptr, _packfile_year(self.version))
        self._objects: dict[int, dict] = {}

    def _fixups(self, sec: dict, key: str, stop: str, width: int) -> list[tuple]:
        out = []
        fmt = self.e + "I" * width
        end = sec["start"] + sec[stop]
        for p in range(sec["start"] + sec[key], end, 4 * width):
            if p + 4 * width > end:
                break  # the table is padded to 16 bytes
            vals = struct.unpack_from(fmt, self.data, p)
            if vals[0] == 0xFFFFFFFF:
                break
            out.append(vals)
        return out

    def read(self) -> Hkx:
        names_sec = self.sections[self.names_index]
        names: dict[int, str] = {}
        p = names_sec["start"]
        end = names_sec["start"] + names_sec["local"]
        while p + 5 < end and self.data[p + 4] == 0x09:
            e = self.data.index(b"\0", p + 5)
            names[p + 5 - names_sec["start"]] = self.data[p + 5:e].decode("ascii", "replace")
            p = e + 1
        data_sec = self.sections[self.data_index]
        self.base = data_sec["start"]
        self.local = {src: dst for src, dst in self._fixups(data_sec, "local", "glob", 2)}
        self.glob = {src: (sec, dst) for src, sec, dst in self._fixups(data_sec, "glob", "virt", 3)}
        items = sorted((off, names.get(name_off, "?")) for off, _, name_off in self._fixups(data_sec, "virt", "exports", 3))
        self.items = dict(items)
        if not items:
            raise FormatError("Havok packfile without objects")
        root = self._object(items[0][0], items[0][1])
        return Hkx("packfile", self.version, root)

    def _object(self, offset: int, class_name: str | None = None) -> dict | None:
        if offset in self._objects:
            return self._objects[offset]
        class_name = self.items.get(offset, class_name)
        if class_name is None:
            return None
        obj: dict = {"__class__": class_name}
        self._objects[offset] = obj
        if class_name not in CLASSES:
            return obj  # a physics shape, a cloth state...: named but not read
        for name, mtype, at in self.layouts.get(class_name).members:
            if name == "vtable":
                continue
            obj[name] = self._value(mtype, offset + at)
        return obj

    def _value(self, mtype: str, at: int):
        d, e = self.data, self.e
        if mtype in _PRIMITIVES:
            return struct.unpack_from(e + _PRIMITIVES[mtype][2], d, self.base + at)[0]
        if mtype == "vec4":
            return np.array(struct.unpack_from(e + "4f", d, self.base + at), np.float32)
        if mtype == "qs":
            return np.array(struct.unpack_from(e + "12f", d, self.base + at), np.float32)
        if mtype == "str":
            dst = self.local.get(at)
            if dst is None:
                return ""
            end = d.index(b"\0", self.base + dst)
            return d[self.base + dst:end].decode("shift_jis", "replace")
        if mtype.startswith("ptr:"):
            target = self.glob.get(at)
            if target is None or target[0] != self.data_index:
                return None
            return self._object(target[1], mtype[4:] or None)
        if mtype.startswith("class:"):
            lay = self.layouts.get(mtype[6:])
            return {"__class__": lay.name, **{n: self._value(t, at + o) for n, t, o in lay.members if n != "vtable"}}
        if mtype.startswith("array:"):
            (count,) = struct.unpack_from(e + "i", d, self.base + at + self.ptr)
            dst = self.local.get(at)
            if count <= 0 or dst is None:
                return []
            return self._array(mtype[6:], dst, count)
        raise FormatError(f"Unknown Havok member type {mtype}")

    def _array(self, etype: str, at: int, count: int):
        if etype in _ARRAY_DTYPES:
            spec = _ARRAY_DTYPES[etype]
            dtype, width = (spec, 1) if isinstance(spec, str) else spec
            dt = np.dtype(dtype)
            if dt.itemsize > 1:
                dt = dt.newbyteorder(self.e)
            arr = np.frombuffer(self.data, dt, count * width, self.base + at)
            arr = arr.astype(dt.newbyteorder("=")) if width == 1 else arr.astype(dt.newbyteorder("=")).reshape(count, width)
            return arr
        if not etype:
            return [None] * count  # an array of something not described: only its length matters
        size = self.layouts.size_align(etype)[0]
        return [self._value(etype, at + i * size) for i in range(count)]


def _read_packfile(data: bytes) -> Hkx:
    return _Packfile(data).read()


# ---------------------------------------------------------------------------
# Tagfiles

# Kinds, from the low byte of a type's format flags
_K_VOID, _K_OPAQUE, _K_BOOL, _K_STRING, _K_INT, _K_FLOAT, _K_POINTER, _K_CLASS, _K_ARRAY = 0, 1, 2, 3, 4, 5, 6, 7, 8
_K_STRUCT = 0x28  # T[N], N in the second byte
_K_CSTRING = 0x83
# Type body flags
_T_FORMAT, _T_POINTER, _T_VERSION, _T_SIZE, _T_ABSTRACT, _T_MEMBERS, _T_INTERFACES = 1, 2, 4, 8, 16, 32, 64
_FLOAT32 = 0x1746 << 8 | _K_FLOAT


@dataclass
class TagType:
    name: str
    index: int
    parent: int = 0
    format: int = 0
    pointee: int = 0  # the type pointed to, or the element type
    size: int = 0
    align: int = 0
    members: list[tuple[str, int, int]] = field(default_factory=list)  # (name, offset, type index)
    templates: list[tuple[str, int]] = field(default_factory=list)

    @property
    def kind(self) -> int:
        return self.format & 0xFF


def _varint(data: bytes, p: int) -> tuple[int, int]:
    """A tagfile's packed unsigned integer: the leading one-bits of the first byte say how long it is."""
    b = data[p]
    if b & 0x80 == 0:
        return b, p + 1
    if b & 0xC0 == 0x80:
        return ((b & 0x3F) << 8) | data[p + 1], p + 2
    if b == 0xC3:  # seen in Elden Ring files: the value is the next two bytes alone
        return (data[p + 1] << 8) | data[p + 2], p + 3
    if b & 0xE0 == 0xC0:
        return ((b & 0x1F) << 16) | (data[p + 1] << 8) | data[p + 2], p + 3
    if b & 0xF8 == 0xE0:
        return int.from_bytes(data[p:p + 4], "big") & 0x07FFFFFF, p + 4
    if b & 0xF8 == 0xE8:
        return int.from_bytes(data[p:p + 5], "big") & 0x07FFFFFFFF, p + 5
    if b & 0xF8 == 0xF0:
        return int.from_bytes(data[p:p + 8], "big") & 0x07FFFFFFFFFFFFFF, p + 8
    raise FormatError(f"Bad packed integer 0x{b:02X} in Havok tagfile")


def _sections(data: bytes, start: int, end: int) -> list[tuple[str, int, int]]:
    """(tag, body start, body end) of the sections laid end to end between start and end."""
    out = []
    p = start
    while p + 8 <= end:
        size = struct.unpack_from(">I", data, p)[0] & 0x3FFFFFFF
        tag = data[p + 4:p + 8].decode("ascii", "replace")
        if size < 8:
            raise FormatError(f"Bad section size in Havok tagfile at 0x{p:X}")
        out.append((tag, p + 8, p + size))
        p += size
    return out


class _Tagfile:
    def __init__(self, data: bytes, compendium: bytes | None = None):
        self.data = data
        self.compendium = compendium
        self.types: list[TagType | None] = [None]
        self.items: list[tuple[int, int, int] | None] = []  # (type index, offset, count)
        self.values: dict[int, object] = {}

    def read(self) -> Hkx:
        d = self.data
        top = _sections(d, 0, len(d))
        if not top or top[0][0] != "TAG0":
            raise FormatError("Not a Havok tagfile")
        version = ""
        data_start = 0
        for tag, a, b in _sections(d, top[0][1], top[0][2]):
            if tag == "SDKV":
                version = d[a:b].decode("ascii", "replace").strip("\0 ")
            elif tag == "DATA":
                data_start = a
            elif tag == "TYPE":
                self._types(a, b)
            elif tag == "TCRF":
                self._compendium_types()
            elif tag == "INDX":
                for itag, ia, ib in _sections(d, a, b):
                    if itag == "ITEM":
                        for p in range(ia, ib, 12):
                            info, off, count = struct.unpack_from("<III", d, p)
                            self.items.append(None if info == 0 else (info & 0x00FFFFFF, data_start + off, count))
        if len(self.items) < 2 or self.items[1] is None:
            raise FormatError("Havok tagfile without a root object")
        root = self._item(1, single=True)
        return Hkx("tagfile", version, root if isinstance(root, dict) else None)

    # ---- types

    def _compendium_types(self) -> None:
        """Types from the shared .compendium file: TCM0 { TCID, TYPE { ... } }, the same TYPE layout."""
        if not self.compendium:
            raise FormatError("This Havok file keeps its types in a .compendium file that was not found beside it")
        comp = self.compendium
        top = _sections(comp, 0, len(comp))
        if not top or top[0][0] != "TCM0":
            raise FormatError("Not a Havok compendium file")
        own = self.data
        try:
            self.data = comp
            for tag, a, b in _sections(comp, top[0][1], top[0][2]):
                if tag == "TYPE":
                    self._types(a, b)
        finally:
            self.data = own

    def _types(self, start: int, end: int) -> None:
        d = self.data
        type_names: list[str] = []
        member_names: list[str] = []
        for tag, a, b in _sections(d, start, end):
            if tag == "TSTR":
                type_names = d[a:b].decode("utf-8", "replace").split("\0")
            elif tag in ("TNAM", "TNA1"):
                count, p = _varint(d, a)
                for i in range(1, count):
                    name_idx, p = _varint(d, p)
                    n_templates, p = _varint(d, p)
                    t = TagType(type_names[name_idx], i)
                    for _ in range(n_templates):
                        tname, p = _varint(d, p)
                        tval, p = _varint(d, p)
                        t.templates.append((type_names[tname], tval))
                    self.types.append(t)
            elif tag in ("FSTR", "FST1"):
                member_names = d[a:b].decode("utf-8", "replace").split("\0")
            elif tag in ("TBOD", "TBDY"):
                p = a
                while p < b:
                    idx, p = _varint(d, p)
                    if idx == 0:
                        continue
                    t = self.types[idx]
                    t.parent, p = _varint(d, p)
                    flags, p = _varint(d, p)
                    if flags & _T_FORMAT:
                        t.format, p = _varint(d, p)
                    if flags & _T_POINTER and (t.format & 0x0F) >= _K_POINTER:
                        t.pointee, p = _varint(d, p)
                    if flags & _T_VERSION:
                        _, p = _varint(d, p)
                    if flags & _T_SIZE:
                        t.size, p = _varint(d, p)
                        t.align, p = _varint(d, p)
                    if flags & _T_ABSTRACT:
                        _, p = _varint(d, p)
                    if flags & _T_MEMBERS:
                        n, p = _varint(d, p)
                        for _ in range(n):
                            mname, p = _varint(d, p)
                            _mflags, p = _varint(d, p)
                            off, p = _varint(d, p)
                            mtype, p = _varint(d, p)
                            t.members.append((member_names[mname], off, mtype))
                    if flags & _T_INTERFACES:
                        n, p = _varint(d, p)
                        for _ in range(n):
                            _, p = _varint(d, p)
                            _, p = _varint(d, p)
                    if flags & 0x80:
                        raise FormatError(f"Havok type {t.name} uses an unknown description flag")
        for t in self.types[1:]:
            # typedefs (hkInt16 -> short, hkReal -> float) describe nothing themselves
            p = self.types[t.parent] if t.parent else None
            while p is not None and not t.format:
                t.format = t.format or p.format
                t.pointee = t.pointee or p.pointee
                t.size, t.align = t.size or p.size, t.align or p.align
                p = self.types[p.parent] if p.parent else None
        for t in self.types[1:]:
            if t.kind == _K_ARRAY and not t.pointee:
                # hkArray<T>: the element type is its tT template
                for tname, tval in t.templates:
                    if tname == "tT":
                        t.pointee = tval

    def _all_members(self, t: TagType) -> list[tuple[str, int, int]]:
        chain = []
        while t is not None and t.index:
            chain.append(t)
            t = self.types[t.parent] if t.parent else None
        out = []
        for c in reversed(chain):
            out += c.members
        return out

    # ---- values

    def _item(self, index: int, want: TagType | None = None, single: bool = False):
        """Item ``index``: one object when a pointer refers to it, else the array it holds."""
        if index <= 0 or index >= len(self.items) or self.items[index] is None:
            return None
        if index in self.values:
            return self.values[index]
        type_idx, offset, count = self.items[index]
        t = self.types[type_idx]
        if want is not None and want.kind == _K_CLASS and t.kind != _K_CLASS:
            t = want
        if single and t.kind == _K_CLASS:
            obj: dict = {"__class__": t.name}
            self.values[index] = obj
            self._fill(obj, t, offset)
            return obj
        if single:
            value = self._value(t, offset)
        else:
            value = self._array(t, offset, count)
        self.values[index] = value
        return value

    def _fill(self, obj: dict, t: TagType, offset: int) -> None:
        for name, off, mtype in self._all_members(t):
            obj[name] = self._value(self.types[mtype], offset + off)

    def _value(self, t: TagType, at: int):
        d = self.data
        kind = t.kind
        if kind in (_K_VOID, _K_OPAQUE):
            return None
        if kind == _K_BOOL:
            return bool(d[at])
        if kind == _K_INT:
            return struct.unpack_from("<" + _int_format(t.format), d, at)[0]
        if kind == _K_FLOAT:
            if t.format == _FLOAT32:
                return struct.unpack_from("<f", d, at)[0]
            return self._class_value(t, at)  # half floats and the like keep their bits in a member
        if kind in (_K_STRING, _K_CSTRING):
            index = struct.unpack_from("<I", d, at)[0]
            v = self._item(index)
            return v if isinstance(v, str) else ""
        if kind == _K_POINTER:
            index = struct.unpack_from("<I", d, at)[0]
            return self._item(index, self.types[t.pointee] if t.pointee else None, single=True)
        if kind == _K_ARRAY:
            index = struct.unpack_from("<I", d, at)[0]
            v = self._item(index)
            return [] if v is None else v
        if kind == _K_STRUCT:
            n = (t.format >> 8) & 0xFF
            et = self.types[t.pointee]
            return self._fixed(et, at, n)
        if kind == _K_CLASS:
            return self._class_value(t, at)
        raise FormatError(f"Havok type {t.name} has an unknown kind 0x{kind:X}")

    def _class_value(self, t: TagType, at: int):
        members = self._all_members(t)
        obj: dict = {"__class__": t.name}
        for name, off, mtype in members:
            obj[name] = self._value(self.types[mtype], at + off)
        return _plain(obj)

    def _fixed(self, et: TagType, at: int, n: int):
        """A T[N] member: the N values packed one after another."""
        dt = _numpy_dtype(et)
        if dt is not None:
            return np.frombuffer(self.data, dt, n, at).astype(dt.newbyteorder("="))
        return [self._value(et, at + i * et.size) for i in range(n)]

    def _array(self, t: TagType, at: int, count: int):
        """An item that holds an array (or a string): ``count`` values of type ``t`` from ``at``."""
        kind = t.kind
        if kind == _K_INT and t.format & (0x20 << 8) and t.name in ("char", "hkChar", "hkInt8", "hkUint8", "unsigned char"):
            raw = self.data[at:at + count]
            if t.name in ("char", "hkChar", "unsigned char"):
                return raw.split(b"\0", 1)[0].decode("shift_jis", "replace")
            return np.frombuffer(raw, "u1" if t.name == "hkUint8" else "i1").copy()
        dt = _numpy_dtype(t)
        if dt is not None:
            return np.frombuffer(self.data, dt, count, at).astype(dt.newbyteorder("="))
        if kind == _K_CLASS and t.name in ("hkQsTransformf", "hkQsTransform"):
            return np.frombuffer(self.data, "<f4", count * 12, at).reshape(count, 12).astype("f4")
        if kind == _K_CLASS and t.name in ("hkVector4f", "hkVector4"):
            return np.frombuffer(self.data, "<f4", count * 4, at).reshape(count, 4).astype("f4")
        size = t.size
        if kind == _K_POINTER:
            size = size or 8
        if kind in (_K_STRING, _K_CSTRING):
            size = size or 8
        if size <= 0:
            raise FormatError(f"Havok type {t.name} has no size")
        return [self._value(t, at + i * size) for i in range(count)]


def _int_format(fmt: int) -> str:
    signed = bool(fmt & (0x02 << 8))
    if fmt & (0x20 << 8):
        c = "b"
    elif fmt & (0x40 << 8):
        c = "h"
    elif fmt & (0x80 << 8):
        c = "i"
    elif fmt & (0x100 << 8):
        c = "q"
    else:
        raise FormatError(f"Havok integer type with unknown size (format 0x{fmt:X})")
    return c if signed else c.upper()


def _numpy_dtype(t: TagType) -> np.dtype | None:
    if t.kind == _K_INT:
        return np.dtype("<" + _int_format(t.format))
    if t.kind == _K_FLOAT and t.format == _FLOAT32:
        return np.dtype("<f4")
    if t.kind == _K_BOOL:
        return np.dtype("?")
    return None


def _plain(obj: dict):
    """Small value classes read as their numbers: a vector or transform becomes an array."""
    name = obj.get("__class__", "")
    if name in ("hkVector4f", "hkVector4", "hkQuaternionf", "hkQuaternion"):
        vals = [v for k, v in obj.items() if k != "__class__"]
        if len(vals) == 1 and isinstance(vals[0], np.ndarray):
            return vals[0].astype(np.float32)
        return np.array(vals, np.float32)
    if name in ("hkQsTransformf", "hkQsTransform"):
        parts = [obj.get("translation"), obj.get("rotation"), obj.get("scale")]
        if all(isinstance(p, np.ndarray) for p in parts):
            return np.concatenate(parts).astype(np.float32)
    if name in ("hkRefPtr", "hkRefVariant") and "ptr" in obj:
        return obj["ptr"]
    if set(obj) == {"__class__", "ptr"}:
        return obj["ptr"]
    if name.startswith(("hkEnum", "hkFlags")):
        vals = [v for k, v in obj.items() if k != "__class__"]
        if len(vals) == 1:
            return vals[0]
    return obj


def _read_tagfile(data: bytes) -> Hkx:
    return _Tagfile(data).read()


__all__ = ["CLASSES", "Hkx", "Layout", "TagType", "is_compendium", "is_hkx", "layout", "read_hkx"]
