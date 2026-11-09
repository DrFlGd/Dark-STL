"""RPF7 archives (GTA V on PC): a table of entries and names, then the files, in 512-byte blocks.

The table is plain (``OPEN``, as OpenIV writes it, or 0), AES or NG encrypted, the NG key picked by the
archive's own name and size. Entries are directories, binary files (deflated when their stored size is
given, encrypted when flagged) and resources (an RSC7 page layout, always deflated, never encrypted except
scripts). Archives nest: an ``.rpf`` entry is another archive at that place in the file. Layout from
CodeWalker's RpfFile (MIT).
"""

from __future__ import annotations

import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ....errors import FormatError
from .crypto import Keys, decrypt_aes, decrypt_ng

MAGIC = 0x52504637  # "7FPR"
ENC_NONE = 0
ENC_OPEN = 0x4E45504F
ENC_AES = 0x0FFFFFF9
ENC_NG = 0x0FEFFFFF
BLOCK = 512


@dataclass
class Entry:
    name: str  # as stored (lower case in the game's archives)
    path: str  # below the archive, "/"-separated, lower case
    offset: int  # bytes from the start of the archive
    size: int  # stored bytes (0: not compressed, see ``uncompressed``)
    uncompressed: int = 0
    encrypted: bool = False
    resource: bool = False
    sys_flags: int = 0
    gfx_flags: int = 0

    @property
    def version(self) -> int:
        return ((self.sys_flags >> 28) & 0xF) << 4 | ((self.gfx_flags >> 28) & 0xF)

    @property
    def stored(self) -> int:
        return self.size if self.size else self.uncompressed

    @property
    def is_archive(self) -> bool:
        return not self.resource and self.name.lower().endswith(".rpf")


@dataclass
class Archive:
    path: Path  # the file on disk that holds it
    name: str  # its own name (for its key), e.g. "vehicles.rpf"
    base: int  # where it starts in that file
    length: int  # its size
    encryption: int
    entries: list[Entry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __getstate__(self):
        d = dict(self.__dict__)
        d.pop("_lock", None)
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self._lock = threading.Lock()

    @property
    def aes(self) -> bool:
        return self.encryption == ENC_AES

    def files(self) -> list[Entry]:
        return [e for e in self.entries if e.offset >= 0]

    def raw(self, entry: Entry, start: int = 0, length: int | None = None) -> bytes:
        n = entry.stored - start if length is None else length
        with self._lock, open(self.path, "rb") as fh:
            fh.seek(self.base + entry.offset + start)
            data = fh.read(n)
        if len(data) < n:
            raise FormatError(f"{entry.name} is cut short in {self.name}")
        return data

    def read(self, entry: Entry, keys: Keys | None) -> bytes:
        """A binary file's bytes (decrypted and inflated), or a resource's pages (inflated, without the RSC7
        header: see :func:`.rsc.from_entry`)."""
        if entry.resource:
            if entry.stored <= 16:
                return b""
            data = self.raw(entry, 16)
            if entry.encrypted:
                data = _decrypt(data, self, entry.name, entry.stored, keys)
            try:
                return zlib.decompress(data, -15)
            except zlib.error:
                return data
        if entry.stored == 0:
            return b""
        data = self.raw(entry)
        if entry.encrypted:
            data = _decrypt(data, self, entry.name, entry.uncompressed, keys)
        if entry.size:
            try:
                data = zlib.decompress(data, -15)
            except zlib.error as exc:
                raise FormatError(f"{entry.name} couldn't be inflated ({exc})") from exc
        return data


def _decrypt(data: bytes, archive: Archive, name: str, length: int, keys: Keys | None) -> bytes:
    if keys is None:
        raise FormatError("The game's archive keys aren't known yet")
    if archive.aes:
        if keys.aes is None:
            raise FormatError("No AES key")
        return decrypt_aes(data, keys.aes)
    if not keys.has_ng:
        raise FormatError("No NG keys")
    return decrypt_ng(data, keys.ng_key(name, length), keys.ng_tables)


def _decrypt_toc(data: bytes, encryption: int, name: str, length: int, keys: Keys | None) -> bytes:
    if encryption in (ENC_NONE, ENC_OPEN):
        return data
    if keys is None:
        raise FormatError("encrypted, and the game's archive keys aren't known")
    if encryption == ENC_AES:
        if keys.aes is None:
            raise FormatError("AES encrypted, and no AES key is known")
        return decrypt_aes(data, keys.aes)
    if not keys.has_ng:
        raise FormatError("NG encrypted, and no NG keys are known")
    return decrypt_ng(data, keys.ng_key(name, length), keys.ng_tables)


def read_archive(path: Path, keys: Keys | None, name: str | None = None, base: int = 0,
                 length: int | None = None, fh=None) -> Archive:
    """The table of the archive at ``base`` in ``path`` (the whole file unless a nested one is named)."""
    path = Path(path)
    own = fh is None
    if own:
        fh = open(path, "rb")
    try:
        if length is None:
            fh.seek(0, 2)
            length = fh.tell() - base
        fh.seek(base)
        head = fh.read(16)
        if len(head) < 16:
            raise FormatError(f"{name or path.name}: not an RPF archive (too short)")
        magic, count, names_len, enc = struct.unpack("<4I", head)
        if magic != MAGIC:
            raise FormatError(f"{name or path.name}: not an RPF7 archive")
        if count > 1_000_000 or names_len > 64 << 20:
            raise FormatError(f"{name or path.name}: the archive's table is damaged")
        table = fh.read(count * 16)
        names = fh.read(names_len)
        name = name or path.name
        table = _decrypt_toc(table, enc, name, length, keys)
        names = _decrypt_toc(names, enc, name, length, keys)
        raw = []
        for i in range(count):
            y, x = struct.unpack_from("<II", table, i * 16)
            raw.append((y, x, i))
        if not raw:
            return Archive(path, name, base, length, enc, [])
        out: list[Entry | None] = [None] * count
        dirs: dict[int, tuple[int, int]] = {}
        big: list[int] = []
        for y, x, i in raw:
            row = table[i * 16:(i + 1) * 16]
            if x == 0x7FFFFF00:
                name_off, _, start, n = struct.unpack("<4I", row)
                out[i] = Entry(_name(names, name_off), "", -1, 0)
                dirs[i] = (start, n)
            elif not x & 0x80000000:
                buf, unc, encflag = struct.unpack("<QII", row)
                if encflag not in (0, 1):
                    raise FormatError(f"{name}: a file entry is damaged")
                out[i] = Entry(_name(names, buf & 0xFFFF), "", ((buf >> 40) & 0xFFFFFF) * BLOCK,
                               (buf >> 16) & 0xFFFFFF, unc, encflag == 1)
            else:
                name_off = struct.unpack_from("<H", row, 0)[0]
                size = row[2] | row[3] << 8 | row[4] << 16
                off = (row[5] | row[6] << 8 | row[7] << 16) & 0x7FFFFF
                sysf, gfxf = struct.unpack_from("<II", row, 8)
                nm = _name(names, name_off)
                e = Entry(nm, "", off * BLOCK, size, 0, nm.lower().endswith(".ysc"), True, sysf, gfxf)
                out[i] = e
                if size == 0xFFFFFF:
                    big.append(i)
        for i in big:  # a size that doesn't fit is in the resource's own header
            e = out[i]
            fh.seek(base + e.offset)
            h = fh.read(16)
            if len(h) == 16:
                e.size = h[7] | h[14] << 8 | h[5] << 16 | h[2] << 24
        # paths, from the directory tree
        stack = [(0, "")]
        seen = set()
        while stack:
            d, prefix = stack.pop()
            if d in seen or d not in dirs:
                continue
            seen.add(d)
            start, n = dirs[d]
            for j in range(start, min(count, start + n)):
                e = out[j]
                if e is None:
                    continue
                e.path = (prefix + "/" + e.name.lower()) if prefix else e.name.lower()
                if j in dirs:
                    stack.append((j, e.path))
        entries = [e for i, e in enumerate(out) if e is not None and i not in dirs and e.path]
        return Archive(path, name, base, length, enc, entries)
    finally:
        if own:
            fh.close()


def _name(names: bytes, off: int) -> str:
    end = names.find(b"\0", off)
    if end < 0:
        end = len(names)
    return names[off:end].decode("latin-1")


def nested(parent: Archive, entry: Entry, keys: Keys | None, fh=None) -> Archive:
    """The archive an ``.rpf`` entry of ``parent`` holds."""
    if entry.size or entry.encrypted:
        raise FormatError(f"{entry.name} is a packed archive inside {parent.name}; it can't be read in place")
    return read_archive(parent.path, keys, entry.name, parent.base + entry.offset, entry.stored, fh)
