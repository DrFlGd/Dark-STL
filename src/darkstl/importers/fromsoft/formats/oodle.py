"""Runtime binding to the game's Oodle library for Kraken-compressed data.

Oodle is proprietary and cannot be redistributed, but the games that use it
ship it: Sekiro and Elden Ring have ``oo2core_6_win64.dll`` next to the game
executable, Cyberpunk 2077 has ``oo2ext_7_win64.dll`` in ``bin\\x64``. Dark STL
looks for it in the folders above any game file it opens, so a game folder
works as is; otherwise point at it with ``--oodle``, the app's Settings page,
or the ``DARKSTL_OODLE`` environment variable. The open-source Kraken
decoder that WolvenKit ships (``kraken.dll`` / ``libkraken.so``, from ooz) is
accepted too.
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
    "oo2ext_7_win64.dll",
    "liboo2corelinux64.so.9",
    "liboo2corelinux64.so",
    "kraken.dll",
    "libkraken.so",
    "libkraken.dylib",
)
_SUBDIRS = ("", "bin/x64")  # where a game keeps the library, relative to its folder

_search_dirs: list[Path] = []  # chosen by the user, searched first
_hint_dirs: list[Path] = []  # folders above opened game files
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


def add_hint_path(path: str | os.PathLike, levels: int = 4) -> None:
    """Also search the folders above a game file, where the game's own DLL usually is."""
    p = Path(path).resolve()
    for d in [p, *p.parents][:levels + 1]:
        if d.is_dir() and d not in _search_dirs and d not in _hint_dirs:
            _hint_dirs.append(d)


def _candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("DARKSTL_OODLE")
    if env:
        envp = Path(env)
        paths.extend([envp] if envp.is_file() else [envp / n for n in _LIBRARY_NAMES])
    # this platform's libraries first (the others are listed too, so the user is told about them)
    names = sorted(_LIBRARY_NAMES, key=lambda n: n.endswith(".dll") != (sys.platform == "win32"))
    for d in _search_dirs + _hint_dirs:
        for sub in _SUBDIRS:
            paths.extend(d / sub / n for n in names)
    return paths


def located() -> Path | None:
    """The Oodle library that would be used, if one is in a folder searched."""
    return next((p for p in _candidates() if p.is_file()), None)


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
        if hasattr(lib, "Kraken_Decompress"):  # ooz: int Kraken_Decompress(src, src_len, dst, dst_len)
            fn = lib.Kraken_Decompress
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            _lib = lib
            return lib
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
    detail = ("; ".join(errors)) if errors else "no Oodle library found"
    raise FormatError(
        "This file uses Oodle (Kraken) compression, used by Sekiro, Elden Ring and Cyberpunk 2077. "
        "Point Dark STL at the game's Oodle library (oo2core_6_win64.dll beside Sekiro or Elden Ring, "
        "bin\\x64\\oo2ext_7_win64.dll in Cyberpunk 2077's folder) under Settings > Oodle, with --oodle, "
        f"or with DARKSTL_OODLE. Details: {detail}"
    )


def oodle_decompress(payload: bytes, uncompressed_size: int) -> bytes:
    lib = _load()
    out = ctypes.create_string_buffer(uncompressed_size + 64)
    if hasattr(lib, "Kraken_Decompress"):
        got = lib.Kraken_Decompress(payload, len(payload), out, uncompressed_size)
    else:
        got = lib.OodleLZ_Decompress(payload, len(payload), out, uncompressed_size,
                                     1, 0, 0, None, 0, None, None, None, 0, 3)
    if got != uncompressed_size:
        raise FormatError(f"Oodle decompression failed (returned {got}, expected {uncompressed_size})")
    return out.raw[:uncompressed_size]
