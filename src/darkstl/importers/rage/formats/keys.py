"""Finding GTA V's archive keys in the user's own copy of the game, and keeping them.

The app ships no key. The game's executable holds the AES key, the 101 NG keys, the 272 NG tables and the
name-hash table; each is found by hashing every 8-byte-aligned run of the right length with SHA-1 and
comparing with the published hashes (:mod:`.keyhashes`), as CodeWalker does. The keys found are kept in
the app's settings folder (``keys/gta5.bin``) so the search runs once.

The NG keys and tables sit one after another in the executable, so once one is found the rest are
checked where they should be, and the long scan stops as soon as one of each kind is found.
"""

from __future__ import annotations

import hashlib
import mmap
import os
import struct
from collections.abc import Callable
from pathlib import Path

from ....errors import FormatError
from . import keyhashes
from .crypto import AES_KEY_BYTES, LUT_BYTES, NG_KEY_BYTES, NG_TABLE_BYTES, Keys

EXE_NAMES = ("GTA5.exe", "GTA5_Enhanced.exe", "gta5.exe")
CACHE_NAME = "gta5.bin"
ALIGN = 8

Progress = Callable[[float], None]


def _sections(data) -> list[tuple[int, int]]:
    """(start, end) of the executable's sections, data sections first (where keys are kept); the whole
    file when it isn't a PE file."""
    try:
        if data[:2] != b"MZ":
            raise ValueError
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError
        count = struct.unpack_from("<H", data, pe + 6)[0]
        opt = struct.unpack_from("<H", data, pe + 20)[0]
        first = pe + 24 + opt
        out = []
        for i in range(count):
            h = first + 40 * i
            size, ptr = struct.unpack_from("<II", data, h + 16)
            flags = struct.unpack_from("<I", data, h + 36)[0]
            if size and ptr < len(data):
                out.append((flags & 0x20000000 != 0, ptr, min(len(data), ptr + size)))
        if not out:
            raise ValueError
        out.sort()
        spans = [(s, e) for _, s, e in out]
        covered = sum(e - s for s, e in spans)
        if covered < len(data) // 2:  # odd layout: look everywhere
            spans.append((0, len(data)))
        return spans
    except (ValueError, struct.error):
        return [(0, len(data))]


def search(data, progress: Progress | None = None) -> Keys:
    """The keys in ``data`` (an executable's bytes); parts not found are None."""
    want = {
        AES_KEY_BYTES: {keyhashes.AES_KEY: ("aes", 0)},
        LUT_BYTES: {keyhashes.LUT: ("lut", 0)},
        NG_KEY_BYTES: {h: ("ng", i) for i, h in enumerate(keyhashes.NG_KEYS)},
        NG_TABLE_BYTES: {h: ("tab", i) for i, h in enumerate(keyhashes.NG_TABLES)},
    }
    found: dict[str, dict[int, bytes]] = {"aes": {}, "lut": {}, "ng": {}, "tab": {}}
    totals = {"aes": 1, "lut": 1, "ng": len(keyhashes.NG_KEYS), "tab": len(keyhashes.NG_TABLES)}
    sizes = {"aes": AES_KEY_BYTES, "lut": LUT_BYTES, "ng": NG_KEY_BYTES, "tab": NG_TABLE_BYTES}
    hashes = {"ng": keyhashes.NG_KEYS, "tab": keyhashes.NG_TABLES}
    view = memoryview(data)
    n = len(view)
    sha1 = hashlib.sha1

    def neighbours(kind: str, index: int, pos: int) -> None:
        """The rest of a run of keys or tables, where they would be if they follow one another."""
        size = sizes[kind]
        for j, h in enumerate(hashes[kind]):
            if j in found[kind]:
                continue
            p = pos + (j - index) * size
            if 0 <= p and p + size <= n and sha1(view[p:p + size]).digest() == h:
                found[kind][j] = bytes(view[p:p + size])

    spans = _sections(view)
    work = sum(e - s for s, e in spans) or 1
    done = 0
    lengths = [L for L in sorted(want)]
    for start, end in spans:
        pos = start - start % ALIGN
        while pos < end:
            stop = min(end, pos + (1 << 20))
            open_lengths = [L for L in lengths
                            if not any(found[k] for k, _ in want[L].values())]
            if not open_lengths:
                break
            for p in range(pos, stop, ALIGN):
                for L in open_lengths:
                    if p + L > n:
                        continue
                    hit = want[L].get(sha1(view[p:p + L]).digest())
                    if hit is None:
                        continue
                    kind, index = hit
                    found[kind][index] = bytes(view[p:p + L])
                    if kind in hashes:
                        neighbours(kind, index, p)
            done += stop - pos
            pos = stop
            if progress is not None:
                progress(min(1.0, done / work))
        if all(found[k] for k in found):
            break
    # a run that wasn't in one piece: look for what is still missing, everywhere
    missing = {k for k in ("ng", "tab") if len(found[k]) < totals[k]}
    if missing and all(found[k] for k in missing):
        rest = {sizes[k]: {h: (k, i) for i, h in enumerate(hashes[k]) if i not in found[k]} for k in missing}
        for p in range(0, n, ALIGN):
            for L, table in rest.items():
                if p + L <= n:
                    hit = table.get(sha1(view[p:p + L]).digest())
                    if hit is not None:
                        found[hit[0]][hit[1]] = bytes(view[p:p + L])
    ng = [found["ng"].get(i) for i in range(totals["ng"])]
    tabs = [found["tab"].get(i) for i in range(totals["tab"])]
    return Keys.from_parts(found["aes"].get(0), ng, tabs, found["lut"].get(0))


def find_exe(root: Path) -> Path | None:
    for name in EXE_NAMES:
        p = Path(root) / name
        if p.is_file():
            return p
    return None


def search_exe(path: Path, progress: Progress | None = None) -> Keys:
    with open(path, "rb") as fh:
        try:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return search(mm, progress)
        except ValueError:  # an empty file
            return Keys(None, None, None, None)


def _cache_dir() -> Path | None:
    try:
        from ....app.config import config_dir
        return Path(config_dir()) / "keys"
    except Exception:  # noqa: BLE001 - no app settings
        return None


_memory: dict[str, Keys] = {}


def cached() -> Keys | None:
    if "gta5" in _memory:
        return _memory["gta5"]
    d = _cache_dir()
    if d is None:
        return None
    try:
        keys = Keys.from_bytes((d / CACHE_NAME).read_bytes())
    except (OSError, FormatError):
        return None
    _memory["gta5"] = keys
    return keys


def remember(keys: Keys) -> None:
    _memory["gta5"] = keys
    d = _cache_dir()
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (CACHE_NAME + ".tmp")
        tmp.write_bytes(keys.to_bytes())
        os.replace(tmp, d / CACHE_NAME)
    except OSError:
        pass


def forget() -> None:
    _memory.clear()


def for_game(root: Path, progress: Progress | None = None) -> tuple[Keys | None, str | None]:
    """The archive keys (from the settings folder, or found in the game's executable now) and, when
    there are none, why."""
    keys = cached()
    if keys is not None:
        return keys, None
    exe = find_exe(root)
    if exe is None:
        return None, ("GTA5.exe isn't in this folder, so the game's archives can't be opened. Add the folder "
                      "the game is installed in, or files exported with OpenIV or CodeWalker.")
    keys = search_exe(exe, progress)
    if keys.aes is None or not keys.has_ng:
        parts = [n for n, ok in (("AES key", keys.aes is not None), ("NG keys", keys.ng_keys is not None),
                                 ("NG tables", keys.ng_tables is not None), ("hash table", keys.lut is not None))
                 if not ok]
        return None, (f"The archive keys weren't all found in {exe.name} (missing: {', '.join(parts)}), so the "
                      "game's archives can't be opened. Files exported with OpenIV or CodeWalker still open.")
    remember(keys)
    return keys, None
