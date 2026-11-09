"""id Tech 7/8 ``.resources`` containers (``IDCL``), their data and the container mask.

A container is a header, a table of 144-byte entries, a string table (the entries' types and names), a table of
dependencies and two index tables, then the data. Each entry says where its data is, how it is compressed and its
``defaultHash``, which also keys whatever the entry keeps in the game's StreamDB files. The layout follows Valen
(jandk/valen, MIT) and EntityAtlan (FlavorfulGecko5, MIT); see docs/PLAN.md, "Reading id Tech 6/7/8 files".
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ....errors import FormatError
from .farmhash import fingerprint64

MAGIC = b"IDCL"
VERSIONS = (12, 13)  # DOOM Eternal, DOOM: The Dark Ages

COMP_NONE, COMP_ZLIB, COMP_KRAKEN, COMP_LZNA, COMP_KRAKEN_CHUNKED, COMP_LEVIATHAN, COMP_BCPACK = range(7)
COMPRESSION_NAMES = {0: "stored", 1: "zlib", 2: "Kraken", 3: "LZNA", 4: "Kraken (chunked)", 5: "Leviathan",
                     6: "BCPack"}

ENTRY = np.dtype([
    ("type_str", "<i8"), ("name_str", "<i8"), ("desc_str", "<i8"), ("dep_indices", "<i8"), ("strings", "<i8"),
    ("special_hashes", "<i8"), ("meta_entries", "<i8"), ("data_offset", "<i8"), ("data_size", "<i8"),
    ("size", "<i8"), ("checksum", "<u8"), ("timestamp", "<u8"), ("hash", "<u8"), ("version", "<i4"),
    ("flags", "<u4"), ("comp", "u1"), ("reserved0", "u1"), ("variation", "<u2"), ("reserved2", "<u4"),
    ("reserved_var", "<u8"), ("num_strings", "<u2"), ("num_sources", "<u2"), ("num_deps", "<u2"),
    ("num_special", "<u2"), ("num_meta", "<u2"), ("pad1", "<u4"), ("pad2", "<u2"),
])
assert ENTRY.itemsize == 144

_HEADER12 = struct.Struct("<4sIII QQ IIII II II QQQQQQ I Q")
_HEADER13 = struct.Struct("<4sIII QQ IIII II II QQQQQQ")


@dataclass
class Entry:
    """One resource in a container: what and where it is."""

    type: str
    name: str
    offset: int
    stored: int  # bytes in the container
    size: int  # bytes once decompressed
    comp: int
    hash: int  # defaultHash: keys the resource's StreamDB pieces
    version: int


@dataclass
class Container:
    path: Path
    version: int
    entries: list[Entry]
    fingerprint: int  # FarmHash64 of the entry table, the key of its bits in the container mask


def is_container(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head[:4] == MAGIC and len(head) == 8 and struct.unpack_from("<I", head, 4)[0] in VERSIONS


def read_container(path: str | Path) -> Container:
    """The table of a ``.resources`` file (its data stays on disk until :func:`read_entry`)."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(_HEADER12.size)
        if len(head) < _HEADER13.size or head[:4] != MAGIC:
            raise FormatError(f"{path.name} isn't an id Tech container")
        version = struct.unpack_from("<I", head, 4)[0]
        if version not in VERSIONS:
            raise FormatError(f"{path.name}: container version {version} isn't one Dark STL reads (12, 13)")
        h = (_HEADER12 if version == 12 else _HEADER13).unpack_from(head)
        (n_res, n_deps, n_dep_idx, n_str_idx) = h[6:10]
        str_off, _meta_off, ent_off, deps_off = h[14], h[15], h[16], h[17]
        f.seek(ent_off)
        table = f.read(n_res * ENTRY.itemsize)
        f.seek(str_off)
        n_strings = struct.unpack("<Q", f.read(8))[0]
        f.seek(n_strings * 8, 1)
        string_bytes = f.read(max(0, deps_off - f.tell())) if deps_off > f.tell() else b""
        f.seek(deps_off + n_deps * 32 + n_dep_idx * 4)
        string_index = np.frombuffer(f.read(n_str_idx * 8), "<i8")
        end = f.tell()
        f.seek(ent_off)
        hashed = f.read(end - ent_off + 4)
    if len(table) < n_res * ENTRY.itemsize or len(string_index) < n_str_idx:
        raise FormatError(f"{path.name} is cut short")
    strings = string_bytes.split(b"\0")[:n_strings]
    if len(strings) < n_strings:
        raise FormatError(f"{path.name}: its string table is cut short")
    rows = np.frombuffer(table, ENTRY)
    entries = []
    for r in rows:
        s = int(r["strings"])
        try:
            t = strings[int(string_index[s])].decode("utf-8", "replace")
            n = strings[int(string_index[s + 1])].decode("utf-8", "replace")
        except IndexError:  # kept, so entries stay aligned with the container mask's bits
            t = n = ""
        entries.append(Entry(t, n, int(r["data_offset"]), int(r["data_size"]), int(r["size"]), int(r["comp"]),
                             int(r["hash"]), int(r["version"])))
    return Container(path, version, entries, fingerprint64(hashed))


def decompress(data: bytes, comp: int, size: int, where: str = "") -> bytes:
    """An entry's data as the game reads it."""
    if comp == COMP_NONE or (len(data) == size and comp not in (COMP_KRAKEN_CHUNKED,)):
        return data
    if comp == COMP_ZLIB:
        for wbits in (15, -15):
            try:
                return zlib.decompress(data, wbits)[:size]
            except zlib.error:
                continue
        raise FormatError(f"{where}: its zlib data doesn't decompress")
    if comp in (COMP_KRAKEN, COMP_KRAKEN_CHUNKED, COMP_LEVIATHAN, COMP_LZNA):
        from ...fromsoft.formats.oodle import oodle_decompress

        payload = data[12:] if comp == COMP_KRAKEN_CHUNKED else data
        return oodle_decompress(payload, size)
    raise FormatError(f"{where}: {COMPRESSION_NAMES.get(comp, comp)} compression isn't read (a console format)")


def read_entry(path: str | Path, entry: Entry) -> bytes:
    with open(path, "rb") as f:
        f.seek(entry.offset)
        data = f.read(entry.stored)
    if len(data) < entry.stored:
        raise FormatError(f"{entry.name}: {Path(path).name} is cut short")
    return decompress(data, entry.comp, entry.size, entry.name)


MASK_NAME = "generated/buildgame/container.mask"


def read_container_mask(data: bytes, version: int) -> dict[int, np.ndarray]:
    """``container.mask``: per container fingerprint, a bool per entry saying whether that copy is live."""
    pos = 4 if version == 12 else 0
    (count,) = struct.unpack_from("<I", data, pos)
    pos += 4
    out: dict[int, np.ndarray] = {}
    for _ in range(count):
        key, words = struct.unpack_from("<QI", data, pos)
        pos += 12
        raw = np.frombuffer(data, np.uint8, words * 8, pos)
        pos += words * 8
        out[key] = np.unpackbits(raw, bitorder="little").astype(bool)
    return out


def live(mask: dict[int, np.ndarray], container: Container) -> np.ndarray:
    """Which of a container's entries are live (all of them when the mask doesn't name the container)."""
    bits = mask.get(container.fingerprint)
    n = len(container.entries)
    if bits is None:
        return np.ones(n, bool)
    out = np.zeros(n, bool)
    k = min(n, len(bits))
    out[:k] = bits[:k]
    return out
