"""IMG archives of GTA III, Vice City and San Andreas.

Version 1 (III, VC): ``name.dir`` beside ``name.img`` holds 32-byte entries: offset and size in
2048-byte sectors, then a 24-byte NUL-padded name; the ``.img`` is the members back to back.
Version 2 (SA): the ``.img`` starts with ``VER2`` and an entry count, then the same entries with
the size as two u16 (streaming size, size in the archive; the first is used when set).
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ....errors import FormatError

SECTOR = 2048


@dataclass
class Entry:
    name: str
    offset: int  # bytes
    size: int  # bytes (whole sectors)


@dataclass
class ImgArchive:
    path: Path
    version: int  # 1 or 2
    entries: list[Entry]
    _by_name: dict[str, Entry] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        for e in self.entries:
            self._by_name.setdefault(e.name.lower(), e)

    def find(self, name: str) -> Entry | None:
        return self._by_name.get(name.lower())

    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def read(self, entry: Entry) -> bytes:
        with self._lock, open(self.path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.size)


def dir_file(img: Path) -> Path | None:
    """The ``.dir`` file of a version 1 archive, whatever its case."""
    img = Path(img)
    for cand in (img.with_suffix(".dir"), img.with_suffix(".DIR")):
        if cand.is_file():
            return cand
    try:
        for name in os.listdir(img.parent):
            if name.lower() == img.stem.lower() + ".dir":
                return img.parent / name
    except OSError:
        pass
    return None


def _name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def read_img(path: str | os.PathLike) -> ImgArchive:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] == b"VER2":
            (count,) = struct.unpack_from("<I", head, 4)
            if count > 1_000_000:
                raise FormatError(f"{path.name}: implausible entry count {count}")
            table = f.read(32 * count)
            entries = []
            for i in range(len(table) // 32):
                off, stream, archived = struct.unpack_from("<IHH", table, i * 32)
                size = stream or archived
                entries.append(Entry(_name(table[i * 32 + 8:i * 32 + 32]), off * SECTOR, size * SECTOR))
            return ImgArchive(path, 2, entries)
        if head[:4] == b"VER3" or struct.unpack_from("<I", head.ljust(4, b"\0"), 0)[0] == 0xA94E2A52:
            raise FormatError(f"{path.name} is a GTA IV archive (IMG3), not read by the RenderWare importer")
    d = dir_file(path)
    if d is None:
        raise FormatError(f"{path.name} has no .dir file beside it (and isn't a San Andreas VER2 archive)")
    table = d.read_bytes()
    entries = []
    for i in range(len(table) // 32):
        off, size = struct.unpack_from("<II", table, i * 32)
        entries.append(Entry(_name(table[i * 32 + 8:i * 32 + 32]), off * SECTOR, size * SECTOR))
    return ImgArchive(path, 1, entries)


def write_img(path: Path, members: list[tuple[str, bytes]], version: int = 2) -> None:
    """An archive holding ``members`` (for tests and for Collect's game-files copies)."""
    path = Path(path)
    blobs = [(n, b + b"\0" * (-len(b) % SECTOR)) for n, b in members]
    if version == 2:
        header = 8 + 32 * len(blobs)
        pos = (header + SECTOR - 1) // SECTOR
        table, body = bytearray(), bytearray()
        for n, b in blobs:
            table += struct.pack("<IHH", pos, len(b) // SECTOR, 0) + n.encode("latin-1")[:23].ljust(24, b"\0")
            body += b
            pos += len(b) // SECTOR
        head = b"VER2" + struct.pack("<I", len(blobs)) + table
        path.write_bytes(head.ljust(((header + SECTOR - 1) // SECTOR) * SECTOR, b"\0") + body)
        return
    table, body, pos = bytearray(), bytearray(), 0
    for n, b in blobs:
        table += struct.pack("<II", pos, len(b) // SECTOR) + n.encode("latin-1")[:23].ljust(24, b"\0")
        body += b
        pos += len(b) // SECTOR
    path.write_bytes(bytes(body))
    path.with_suffix(".dir").write_bytes(bytes(table))
