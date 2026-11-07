"""Reading objects cooked without property names when the game's type mappings aren't available.

An unversioned object starts with a header saying which of its class's
properties follow, then their values, packed without names or sizes: without
the class layouts (a ``.usmap``) they can't be read, nor skipped by length.
What comes after them, the object's own binary data (a mesh's geometry and
material slots, a texture's mips), has a layout the readers know. So the
values are stepped over by finding where that data starts: the first offset
where it reads the way that class's data must begin (cooked flags, sane
bounds, material references that are the package's own imports...).

A static mesh keeps its material slots among its properties; they are picked
out of the values as the references to materials they hold, in order.
"""

from __future__ import annotations

import math
import struct

from ....errors import FormatError
from ..formats.props import ObjectRef, Props
from ..formats.upk import Reader

MATERIAL_CLASSES = ("MaterialInstanceConstant", "Material", "MaterialInstanceDynamic", "MaterialInstance")
MAX_SCAN = 1 << 20  # how far past the header to look for the object's own data


class GuessedProps(Props):
    """No property values (they couldn't be read), and what could be picked out of them."""

    def __init__(self, material_refs: list[ObjectRef] | None = None):
        super().__init__()
        self.material_refs = material_refs or []


def _ref_ok(pkg, ref: int) -> bool:
    return ref == 0 or (ref < 0 and -ref <= len(pkg.imports)) or (0 < ref <= len(pkg.exports))


def _header_end(data: bytes) -> int:
    """Where the values start, after the unversioned header and its zero mask."""
    pos, frags = 0, []
    for _ in range(4096):
        if pos + 2 > len(data):
            raise FormatError("unversioned header runs past the object")
        packed = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        frags.append(packed)
        if packed & 0x100:
            break
    zero_bits = sum(p >> 9 for p in frags if p & 0x80)
    if zero_bits:
        pos += 1 if zero_bits <= 8 else 2 if zero_bits <= 16 else 4 * ((zero_bits + 31) // 32)
    return pos


def _guid_len(data: bytes, p: int) -> int | None:
    """Length of the object GUID flag (and GUID) UE4 writes after the properties, if ``p`` can start one."""
    if p + 4 > len(data):
        return None
    flag = struct.unpack_from("<i", data, p)[0]
    if flag == 0:
        return 4
    if flag == 1:
        return 20
    return None


def _floats_ok(vals) -> bool:
    return all(math.isfinite(x) and abs(x) < 1e7 for x in vals)


def _skeletal_start(pkg, data: bytes, q: int) -> bool:
    if q + 2 + 28 + 4 > len(data):
        return False
    g, c = data[q], data[q + 1]
    if not g & 1 or g > 3 or c > 7:
        return False
    b = struct.unpack_from("<7f", data, q + 2)
    if not _floats_ok(b):
        return False
    extent, radius = b[3:6], b[6]
    if min(extent) < 0 or radius <= 0:
        return False
    length = math.sqrt(sum(x * x for x in extent))
    if radius > length * 1.05 + 1e-3 or radius < max(extent) * 0.9:
        return False
    n = struct.unpack_from("<i", data, q + 30)[0]
    if not 0 < n <= 512 or q + 34 + 4 > len(data):
        return False
    return _ref_ok(pkg, struct.unpack_from("<i", data, q + 34)[0])


def _static_start(pkg, data: bytes, q: int) -> bool:
    if q + 2 + 4 + 4 + 4 + 16 + 4 + 4 > len(data):
        return False
    g, c = data[q], data[q + 1]
    if not g & 1 or g > 3 or c > 7:
        return False
    cooked, body, nav = struct.unpack_from("<iii", data, q + 2)
    if cooked not in (0, 1) or not _ref_ok(pkg, body) or not _ref_ok(pkg, nav):
        return False
    p = q + 14 + 16
    sockets = struct.unpack_from("<i", data, p)[0]
    if not 0 <= sockets <= 256 or p + 4 + 4 * sockets + 4 > len(data):
        return False
    if not all(_ref_ok(pkg, struct.unpack_from("<i", data, p + 4 + 4 * i)[0]) for i in range(sockets)):
        return False
    lods = struct.unpack_from("<i", data, p + 4 + 4 * sockets)[0]
    return 0 < lods <= 16


def _texture_start(pkg, data: bytes, q: int) -> bool:
    if q + 4 + 4 + 8 > len(data):
        return False
    g, c, g2, c2 = data[q:q + 4]
    if not g & 1 or g > 3 or c > 7 or g2 > 3 or c2 > 7:
        return False
    if struct.unpack_from("<i", data, q + 4)[0] != 1:
        return False
    p = q + 8 + (4 if pkg.versions.engine >= 503 else 0)
    idx, num = struct.unpack_from("<ii", data, p)
    return 0 <= idx < len(pkg.names) and num == 0 and pkg.names[idx].startswith("PF_")


_STARTS = {"SkeletalMesh": _skeletal_start, "StaticMesh": _static_start, "Texture2D": _texture_start,
           "VirtualTexture2D": _texture_start}


def _material_refs(pkg, values: bytes) -> list[ObjectRef]:
    """References to materials in a static mesh's property values, in order: each material slot holds one,
    followed by the slot's name."""
    wanted = {-(i + 1) for i, imp in enumerate(pkg.imports) if imp.class_name in MATERIAL_CLASSES}
    wanted |= {e.index + 1 for e in pkg.exports if e.class_name in MATERIAL_CLASSES}
    out = []
    p = 0
    while p + 12 <= len(values):
        ref = struct.unpack_from("<i", values, p)[0]
        if ref in wanted:
            idx, num = struct.unpack_from("<ii", values, p + 4)
            if 0 <= idx < len(pkg.names) and 0 <= num < 64:
                out.append(ObjectRef(pkg, ref))
                p += 12
                continue
        p += 1
    if not out:  # no slot names after them: every reference, in order
        p = 0
        while p + 4 <= len(values):
            ref = struct.unpack_from("<i", values, p)[0]
            if ref in wanted:
                out.append(ObjectRef(pkg, ref))
                p += 4
                continue
            p += 1
    return out


def skip_properties(pkg, r: Reader, e) -> Props:
    """Step over ``e``'s unversioned properties without the type mappings, leaving ``r`` at its object GUID
    flag; raises :class:`FormatError` when its own data can't be found (or its class has none worth reading)."""
    data = r.data
    start = r.pos
    test = _STARTS.get(e.class_name)
    if test is None:
        raise FormatError(f"{e.class_name}'s properties can't be read without the game's type mappings")
    body = start + _header_end(data[start:])
    for p in range(body, min(len(data), body + MAX_SCAN)):
        g = _guid_len(data, p)
        if g is not None and test(pkg, data, p + g):
            values = data[body:p]
            r.pos = p
            return GuessedProps(_material_refs(pkg, values) if e.class_name == "StaticMesh" else None)
    raise FormatError(f"{e.name}'s own data couldn't be found after its properties (they need the game's type "
                      "mappings)")


__all__ = ["GuessedProps", "skip_properties"]
