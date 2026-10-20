"""Runtime binding to the game's Oodle library for KRAK-compressed DCX files.

Oodle is proprietary and cannot be redistributed, but Sekiro and Elden Ring ship
``oo2core_6_win64.dll`` next to the game executable. Point Dark STL at it with
the ``DARKSTL_OODLE`` environment variable, the settings dialog, or by placing
the DLL next to the files being opened.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from .binary import FormatError

_LIBRARY_NAMES = (
    "oo2core_9_win64.dll",
    "oo2core_8_win64.dll",
    "oo2core_6_win64.dll",
    "liboo2corelinux64.so.9",
    "liboo2corelinux64.so",
)

_search_dirs: list[Path] = []
_lib = None


def add_search_dir(path: str | os.PathLike) -> None:
    """Remember a folder that may contain the Oodle library (e.g. the game folder)."""
    global _lib
    p = Path(path)
    if p.is_file():
        p = p.parent
    if p not in _search_dirs:
        _search_dirs.insert(0, p)
        _lib = None


def _candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("DARKSTL_OODLE")
    if env:
        envp = Path(env)
        paths.extend([envp] if envp.is_file() else [envp / n for n in _LIBRARY_NAMES])
    for d in _search_dirs:
        paths.extend(d / n for n in _LIBRARY_NAMES)
    return paths


def _load():
    global _lib
    if _lib is not None:
        return _lib
    errors = []
    for path in _candidates():
        if not path.is_file():
            continue
        try:
            lib = ctypes.WinDLL(str(path)) if sys.platform == "win32" else ctypes.CDLL(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        fn = lib.OodleLZ_Decompress
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [
            ctypes.c_char_p, ctypes.c_ssize_t,  # compBuf, compBufSize
            ctypes.c_void_p, ctypes.c_ssize_t,  # rawBuf, rawLen
            ctypes.c_int, ctypes.c_int, ctypes.c_int,  # fuzzSafe, checkCRC, verbosity
            ctypes.c_void_p, ctypes.c_ssize_t,  # decBufBase, decBufSize
            ctypes.c_void_p, ctypes.c_void_p,  # fpCallback, callbackUserData
            ctypes.c_void_p, ctypes.c_ssize_t,  # decoderMemory, decoderMemorySize
            ctypes.c_int,  # threadPhase
        ]
        _lib = lib
        return lib
    detail = ("; ".join(errors)) if errors else "no oo2core library found"
    raise FormatError(
        "This file uses Oodle (KRAK) compression, used by Sekiro and Elden Ring. "
        "Point Dark STL at the game's oo2core_6_win64.dll (set DARKSTL_OODLE or use Settings). "
        f"Details: {detail}"
    )


def oodle_decompress(payload: bytes, uncompressed_size: int) -> bytes:
    lib = _load()
    out = ctypes.create_string_buffer(uncompressed_size + 64)
    got = lib.OodleLZ_Decompress(payload, len(payload), out, uncompressed_size,
                                 1, 0, 0, None, 0, None, None, None, 0, 3)
    if got != uncompressed_size:
        raise FormatError(f"Oodle decompression failed (returned {got}, expected {uncompressed_size})")
    return out.raw[:uncompressed_size]
