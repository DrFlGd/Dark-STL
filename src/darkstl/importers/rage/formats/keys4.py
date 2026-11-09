"""Finding GTA IV's archive key in the user's own copy of the game, and keeping it.

The app ships no key. ``GTAIV.exe`` (or ``EFLC.exe``) holds the 32-byte AES key the archive tables are
encrypted with; it is found by hashing every 4-byte-aligned run of 32 bytes with SHA-1 and comparing with
the hash SparkIV publishes (:data:`KEY_HASH`), then kept in the app's settings folder (``keys/gta4.bin``)
so the search runs once.
"""

from __future__ import annotations

import hashlib
import mmap
import os
from collections.abc import Callable
from pathlib import Path

KEY_HASH = bytes.fromhex("DEA375EF1E6EF2223A1221C2C575C47BF17EFA5E")  # SHA-1 of the key (SparkIV)
EXE_NAMES = ("GTAIV.exe", "EFLC.exe", "gtaiv.exe", "eflc.exe")
CACHE_NAME = "gta4.bin"
KEY_BYTES = 32
ALIGN = 4

Progress = Callable[[float], None]
_memory: dict[str, bytes] = {}


def search(data, progress: Progress | None = None) -> bytes | None:
    view = memoryview(data)
    n = len(view)
    sha1 = hashlib.sha1
    step = 1 << 20
    for start in range(0, n, step):
        stop = min(n - KEY_BYTES + 1, start + step)
        for p in range(start, stop, ALIGN):
            if sha1(view[p:p + KEY_BYTES]).digest() == KEY_HASH:
                if progress is not None:
                    progress(1.0)
                return bytes(view[p:p + KEY_BYTES])
        if progress is not None:
            progress(min(1.0, stop / max(n, 1)))
    return None


def find_exe(root: Path) -> Path | None:
    for name in EXE_NAMES:
        p = Path(root) / name
        if p.is_file():
            return p
    return None


def _cache_dir() -> Path | None:
    try:
        from ....app.config import config_dir
        return Path(config_dir()) / "keys"
    except Exception:  # noqa: BLE001 - no app settings
        return None


def cached() -> bytes | None:
    if "gta4" in _memory:
        return _memory["gta4"]
    d = _cache_dir()
    if d is None:
        return None
    try:
        key = (d / CACHE_NAME).read_bytes()
    except OSError:
        return None
    if len(key) != KEY_BYTES or hashlib.sha1(key).digest() != KEY_HASH:
        return None
    _memory["gta4"] = key
    return key


def remember(key: bytes) -> None:
    _memory["gta4"] = key
    d = _cache_dir()
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (CACHE_NAME + ".tmp")
        tmp.write_bytes(key)
        os.replace(tmp, d / CACHE_NAME)
    except OSError:
        pass


def forget() -> None:
    _memory.clear()


def for_game(root: Path, progress: Progress | None = None) -> tuple[bytes | None, str | None]:
    """The archive key (from the settings folder, or found in the game's executable now) and, when there
    is none, why."""
    key = cached()
    if key is not None:
        return key, None
    exe = find_exe(root)
    if exe is None:
        return None, ("GTAIV.exe isn't in this folder, so the game's encrypted archives can't be opened. Add "
                      "the folder the game is installed in, or files exported with OpenIV or SparkIV.")
    with open(exe, "rb") as fh:
        try:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                key = search(mm, progress)
        except ValueError:
            key = None
    if key is None:
        return None, (f"The archive key wasn't found in {exe.name}, so the game's encrypted archives can't be "
                      "opened. Files exported with OpenIV or SparkIV still open.")
    remember(key)
    return key, None
