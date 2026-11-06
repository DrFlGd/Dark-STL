"""The files in a game folder, listed once per scan, and a quick check of whether a folder has changed.

Every importer looks through each folder it's given, so a scan used to walk a game folder once per
importer (five times), several of them asking each file's size as well. Within :func:`one_listing`, the
first importer to ask lists the folder and the others reuse that list.

A folder's :class:`Fingerprint` (the date of each of its folders, and the size and date of its big files)
is kept with what its scan found. At start-up, a folder whose fingerprint still matches isn't scanned
again: adding, removing or renaming a file changes the date of the folder holding it, and archives (the
big files) are checked one by one. A file overwritten in place by one of the same name is only seen by
Rescan (or when it's a big file).
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

BIG_FILE = 16 << 20  # files this big (archives, packages) are checked one by one


@dataclass
class Listing:
    root: Path
    files: list[Path] = field(default_factory=list)  # folders in name order, each folder's files in name order
    sizes: list[int] = field(default_factory=list)
    dirs: list[tuple[str, int]] = field(default_factory=list)  # every folder (the root first) and its date
    big: list[tuple[str, int, int]] = field(default_factory=list)  # (file, size, date) of the big files

    @classmethod
    def read(cls, root: str | os.PathLike) -> "Listing":
        """List ``root`` as ``os.walk`` would (folders sorted, links to folders not followed)."""
        root = Path(root)
        out = cls(root)
        try:
            st = os.stat(root)
        except OSError:
            return out
        out.dirs.append((str(root), st.st_mtime_ns))
        out._read(str(root))
        return out

    def _read(self, folder: str) -> None:
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            return
        files, subdirs = [], []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            (subdirs if is_dir else files).append(e)
        for e in sorted(files, key=lambda e: e.name):
            try:
                size = e.stat().st_size  # free on Windows: the folder listing carries it
            except OSError:
                size = 0
            self.files.append(Path(e.path))
            self.sizes.append(size)
            if size >= BIG_FILE:  # dated as Fingerprint.unchanged will see it (see below)
                try:
                    st = os.stat(e.path)
                    self.big.append((e.path, st.st_size, st.st_mtime_ns))
                except OSError:
                    pass
        for e in sorted(subdirs, key=lambda e: e.name):
            try:
                if e.is_symlink():
                    continue
                # asked of the folder itself: on Windows the date a folder listing gives for a folder in it is
                # a copy NTFS doesn't always keep up to date, so it wouldn't match a later os.stat
                mtime = os.stat(e.path).st_mtime_ns
            except OSError:
                continue
            self.dirs.append((e.path, mtime))
            self._read(e.path)

    def fingerprint(self) -> "Fingerprint":
        return Fingerprint(tuple(self.dirs), tuple(self.big))


@dataclass(frozen=True)
class Fingerprint:
    dirs: tuple[tuple[str, int], ...] = ()
    big: tuple[tuple[str, int, int], ...] = ()

    def unchanged(self) -> bool:
        """Whether every folder and big file is as it was when this was taken."""
        if not self.dirs:
            return False
        try:
            for path, mtime in self.dirs:
                if os.stat(path).st_mtime_ns != mtime:
                    return False
            for path, size, mtime in self.big:
                st = os.stat(path)
                if st.st_size != size or st.st_mtime_ns != mtime:
                    return False
        except OSError:
            return False
        return True


_local = threading.local()


@contextmanager
def one_listing():
    """Within this, each folder is listed once (see :func:`listing`), on this thread."""
    outer = getattr(_local, "cache", None)
    if outer is None:
        _local.cache = {}
    try:
        yield
    finally:
        if outer is None:
            _local.cache = None


def listing(root: str | os.PathLike) -> Listing:
    """The files under ``root``: listed now, or the listing made already within :func:`one_listing`."""
    cache = getattr(_local, "cache", None)
    key = os.path.normcase(os.path.abspath(root))
    if cache is not None and key in cache:
        return cache[key]
    out = Listing.read(root)
    if cache is not None:
        cache[key] = out
    return out


def shared(root: str | os.PathLike) -> Listing | None:
    """Within :func:`one_listing`, the listing of ``root`` (made now if it's the first ask); else None, and
    the caller walks the folder itself."""
    return listing(root) if getattr(_local, "cache", None) is not None else None


def last_listing(root: str | os.PathLike) -> Listing | None:
    """The listing of ``root`` made within the current :func:`one_listing`, if any."""
    cache = getattr(_local, "cache", None)
    return cache.get(os.path.normcase(os.path.abspath(root))) if cache else None


def files_under(root: str | os.PathLike):
    """Every file under ``root``, folders in name order: from the shared listing within
    :func:`one_listing`, else walked now."""
    listed = shared(root)
    if listed is not None:
        yield from listed.files
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name
