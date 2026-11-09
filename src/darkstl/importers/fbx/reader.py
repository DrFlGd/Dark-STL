"""Reading an FBX file's node tree: binary FBX (6.1 to 7.7) and text ("ASCII") FBX.

An FBX file is a tree of nodes, each with a name, a list of values and child nodes. Binary files store
each node as its end offset, value count, value bytes and name, then the values (typed: ``Y`` int16,
``C`` bool, ``I`` int32, ``F`` float32, ``D`` float64, ``L`` int64, ``S`` string, ``R`` raw bytes, and
arrays ``f d l i b``, each optionally zlib-compressed), then the children, ended by an empty record
(25 bytes wide from version 7.5, where offsets became 64-bit). Text files write the same tree as
``Name: value, value { ... }``, with arrays as ``*count { a: v,v,v }``.

The layout follows the format as Blender's FBX importer and the open "FBX binary file format
specification" (Blender developer blog, 2013) describe it.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

from ...errors import FormatError

BINARY_MAGIC = b"Kaydara FBX Binary  \x00"
_ARRAY_TYPES = {ord("f"): "<f4", ord("d"): "<f8", ord("l"): "<i8", ord("i"): "<i4", ord("b"): "u1"}
_SCALARS = {ord("Y"): ("<h", 2), ord("C"): ("<?", 1), ord("I"): ("<i", 4), ord("F"): ("<f", 4),
            ord("D"): ("<d", 8), ord("L"): ("<q", 8)}


@dataclass
class FbxNode:
    name: str
    values: list = field(default_factory=list)
    children: list["FbxNode"] = field(default_factory=list)

    def find(self, name: str) -> "FbxNode | None":
        for c in self.children:
            if c.name == name:
                return c
        return None

    def find_all(self, name: str) -> list["FbxNode"]:
        return [c for c in self.children if c.name == name]

    def value(self, name: str, default=None):
        """The first value of the child called ``name``."""
        c = self.find(name)
        return c.values[0] if c is not None and c.values else default

    def array(self, name: str, dtype=np.float64) -> np.ndarray | None:
        """The child called ``name``'s values as an array (one array value, or a list of numbers)."""
        c = self.find(name)
        if c is None or not c.values:
            return None
        v = c.values[0] if len(c.values) == 1 and isinstance(c.values[0], np.ndarray) else c.values
        try:
            return np.asarray(v, dtype)
        except (TypeError, ValueError):
            return None


def is_fbx(data: bytes) -> bool:
    if data[:len(BINARY_MAGIC)] == BINARY_MAGIC:
        return True
    head = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b";") and b"FBX" in head[:200] or b"FBXHeaderExtension" in data[:4096]


def read(data: bytes, name: str = "") -> tuple[int, FbxNode]:
    """(the file's FBX version, e.g. 7400; its root node, whose children are the file's top-level nodes)."""
    if data[:len(BINARY_MAGIC)] == BINARY_MAGIC:
        return _read_binary(data, name)
    return _read_text(data, name)


# -- binary ---------------------------------------------------------------------------------------------------


def _read_binary(data: bytes, name: str) -> tuple[int, FbxNode]:
    if len(data) < 27:
        raise FormatError(f"{name} is cut short")
    version = struct.unpack_from("<I", data, 23)[0]
    wide = version >= 7500
    root = FbxNode("")
    pos = 27
    end = len(data)
    while pos < end:
        node, pos = _binary_node(data, pos, wide, name, 0)
        if node is None:
            break
        root.children.append(node)
    return version, root


def _binary_node(data: bytes, pos: int, wide: bool, name: str, depth: int) -> tuple[FbxNode | None, int]:
    if depth > 64:
        raise FormatError(f"{name}: nodes nested too deeply")
    head = "<QQQB" if wide else "<IIIB"
    size = struct.calcsize(head)
    if pos + size > len(data):
        raise FormatError(f"{name} is cut short")
    end, count, length, nlen = struct.unpack_from(head, data, pos)
    if end == 0:  # the empty record ending a list of nodes
        return None, pos + size
    if end > len(data) or end < pos:
        raise FormatError(f"{name}: a node runs past the end of the file")
    pos += size
    node_name = data[pos:pos + nlen].decode("utf-8", "replace")
    pos += nlen
    values = []
    vend = pos + length
    for _ in range(count):
        if pos >= vend:
            raise FormatError(f"{name}: {node_name}'s values are cut short")
        t = data[pos]
        pos += 1
        if t in _SCALARS:
            fmt, n = _SCALARS[t]
            values.append(struct.unpack_from(fmt, data, pos)[0])
            pos += n
        elif t in _ARRAY_TYPES:
            n, enc, clen = struct.unpack_from("<III", data, pos)
            pos += 12
            raw = data[pos:pos + clen]
            pos += clen
            if enc == 1:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error as exc:
                    raise FormatError(f"{name}: {node_name}'s array can't be decompressed ({exc})") from exc
            arr = np.frombuffer(raw, _ARRAY_TYPES[t], count=n) if n else np.zeros(0, _ARRAY_TYPES[t])
            values.append(arr.astype(bool) if t == ord("b") else arr)
        elif t in (ord("S"), ord("R")):
            n = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            raw = data[pos:pos + n]
            pos += n
            values.append(_string(raw) if t == ord("S") else bytes(raw))
        else:
            raise FormatError(f"{name}: {node_name} has a value of an unknown type ({chr(t)!r})")
    pos = vend
    node = FbxNode(node_name, values)
    while pos < end:
        child, pos = _binary_node(data, pos, wide, name, depth + 1)
        if child is None:
            break
        node.children.append(child)
    return node, end


def _string(raw: bytes) -> str:
    """A string value. Object names are written ``Name\\x00\\x01Class`` in binary files; ``Class::Name`` in text."""
    text = raw.decode("utf-8", "replace")
    if "\x00\x01" in text:
        obj, _, cls = text.partition("\x00\x01")
        return f"{cls}::{obj}"
    return text


# -- text -----------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]|,|\*?-?[0-9][0-9.eE+-]*|[A-Za-z_|][A-Za-z0-9_|.\-]*:|[A-Za-z_][A-Za-z0-9_.\-|]*|;[^\n]*'
                    r"|\S")


def _read_text(data: bytes, name: str) -> tuple[int, FbxNode]:
    text = data.decode("utf-8-sig", "replace")
    tokens = [t for t in _TOKEN.findall(text) if not t.startswith(";")]
    root = FbxNode("")
    stack = [root]
    i, n = 0, len(tokens)
    current: FbxNode | None = None
    while i < n:
        t = tokens[i]
        i += 1
        if t.endswith(":") and not t.startswith('"'):
            current = FbxNode(t[:-1])
            stack[-1].children.append(current)
            # values until a line ends: a "{" opens its children; another "Name:" starts the next node
            while i < n:
                t = tokens[i]
                if t == ",":
                    i += 1
                    continue
                if t == "{":
                    i += 1
                    stack.append(current)
                    break
                if t == "}" or (t.endswith(":") and not t.startswith('"')):
                    break
                i += 1
                if t.startswith("*"):  # an array: *count { a: v, v, ... }
                    current.values.append(None)
                    continue
                current.values.append(_text_value(t))
        elif t == "}":
            if len(stack) > 1:
                done = stack.pop()
                _fold_array(done)
        elif t == "{":
            stack.append(current or FbxNode(""))
    version = 7400
    header = root.find("FBXHeaderExtension")
    if header is not None:
        version = int(header.value("FBXVersion", version) or version)
    return version, root


def _text_value(t: str):
    if t.startswith('"'):
        s = t[1:-1].replace('\\"', '"')
        return s.replace("&quot;", '"')
    try:
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        return float(t)
    except ValueError:
        return t


def _fold_array(node: FbxNode) -> None:
    """A text array (``*n { a: ... }``): its numbers become one array value, as in a binary file."""
    if node.values == [None] and len(node.children) == 1 and node.children[0].name == "a":
        nums = node.children[0].values
        node.values = [np.asarray(nums, np.float64) if any(isinstance(x, float) for x in nums)
                       else np.asarray(nums, np.int64)]
        node.children = []


__all__ = ["FbxNode", "is_fbx", "read"]
