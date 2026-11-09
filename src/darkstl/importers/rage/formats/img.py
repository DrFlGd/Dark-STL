"""IMG3 archives: where GTA IV keeps its models and textures (``pc/models/cdimages/*.img``,
``pc/data/maps/**/*.img``).

A 20-byte header (magic ``0xA94E2A52``, version 3, entry count, table size, entry size 16), then the
table: 16-byte entries (a file's size, or a resource's RSC5 flags with the top bits set; the resource
type; the offset and the length in 2048-byte blocks, with the padding in the last block) and then the
names, null-terminated, in the same order. The header and table are usually encrypted with AES-256
(ECB, applied 16 times), with the key in the game's executable (:mod:`.keys4`); files inside are not.
A resource is stored as its whole RSC5 file. Layout as RageLib (SparkIV) documents it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ....errors import FormatError
from .crypto import decrypt_aes
from .rpf import Archive, Entry

MAGIC = 0xA94E2A52
BLOCK = 0x800


def decrypt(data: bytes, key: bytes) -> bytes:
    """GTA IV's AES: ECB over the whole 16-byte blocks, done 16 times."""
    for _ in range(16):
        data = decrypt_aes(data, key)
    return data


@dataclass
class ImgArchive(Archive):
    """An IMG3 archive; its members are read as they are stored (a resource with its RSC5 header)."""

    def read(self, entry: Entry, keys=None) -> bytes:
        return self.raw(entry) if entry.stored else b""


def is_img(head: bytes) -> bool:
    return len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] == MAGIC


def read_img(path: Path, key: bytes | None, fh=None) -> ImgArchive:
    path = Path(path)
    own = fh is None
    f = open(path, "rb") if own else fh  # noqa: SIM115 - closed below
    try:
        f.seek(0)
        head = f.read(20)
        if len(head) < 20:
            raise FormatError(f"{path.name} is too short to be an archive")
        encrypted = not is_img(head)
        if encrypted:
            if key is None:
                raise FormatError("encrypted, and the game's archive key isn't known")
            head = decrypt(head, key)
        magic, version, count, toc_size, entry_size = struct.unpack_from("<IIIIH", head, 0)
        if magic != MAGIC or version != 3:
            raise FormatError(f"{path.name} isn't a GTA IV archive" + (" (or the key is wrong)" if encrypted else ""))
        if entry_size not in (0, 16) or count > 1_000_000 or toc_size > 64 << 20 or toc_size < 16 * count:
            raise FormatError(f"{path.name} has a bad table")
        toc = f.read(toc_size)
        if len(toc) < toc_size:
            raise FormatError(f"{path.name} is cut short")
        if encrypted:
            toc = decrypt(toc, key)
        length = f.seek(0, 2)
    finally:
        if own:
            f.close()
    names = toc[16 * count:].split(b"\0")
    entries = []
    for i in range(count):
        size_or_flags, kind, block, used, flags = struct.unpack_from("<IIIHH", toc, 16 * i)
        name = names[i].decode("latin-1").lower() if i < len(names) else f"file{i}"
        resource = size_or_flags & 0xC0000000 != 0
        size = used * BLOCK - (flags & 0x7FF) if resource else size_or_flags
        offset = block * BLOCK
        if offset + size > length:
            continue
        entries.append(Entry(name, name, offset, size, size, False, resource,
                             size_or_flags if resource else 0, 0))
    return ImgArchive(path, path.name.lower(), 0, length, 0, entries)
