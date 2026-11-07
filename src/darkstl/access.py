"""What a model really reads: whole files and pieces of archives, recorded while it's opened.

Collect (see :mod:`darkstl.collect`) opens each model, part and skin inside :func:`recording` and
copies or extracts everything recorded, so a collected game works without its install. Importers call
:func:`note_file` where they open a file and :func:`note_member` where they read a piece of an archive
(a ``.pak``, ``.archive``, ``.ucas``...) that a collected copy keeps as a loose file. Outside a
recording both do nothing.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Member:
    """A piece of an archive kept as a loose file ``save_as`` (a path below the game's folder)."""

    source: Path  # the archive, or a path that names the piece inside it
    save_as: str
    extract: Callable[[], bytes] = field(repr=False)
    size: int = 0
    kind: str = "other"


@dataclass
class Recorder:
    files: dict[Path, str] = field(default_factory=dict)  # file -> kind
    members: dict[str, Member] = field(default_factory=dict)  # save_as (lower case) -> member
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_file(self, path: Path, kind: str) -> None:
        with self._lock:
            self.files.setdefault(Path(path), kind)

    def add_member(self, m: Member) -> None:
        with self._lock:
            self.members.setdefault(m.save_as.replace("\\", "/").lower(), m)


_current: contextvars.ContextVar[Recorder | None] = contextvars.ContextVar("darkstl_access", default=None)


@contextmanager
def recording(rec: Recorder | None = None) -> Iterator[Recorder]:
    """Record what's read in this thread (and in what it calls) until the block ends."""
    rec = rec or Recorder()
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)


def active() -> bool:
    return _current.get() is not None


def note_file(path: str | Path, kind: str = "other") -> None:
    rec = _current.get()
    if rec is not None and path:
        rec.add_file(Path(path), kind)


def note_member(source: str | Path, save_as: str, extract: Callable[[], bytes], size: int = 0,
                kind: str = "other") -> None:
    rec = _current.get()
    if rec is not None:
        rec.add_member(Member(Path(source), save_as.replace("\\", "/").lstrip("/"), extract, size, kind))


__all__ = ["Member", "Recorder", "active", "note_file", "note_member", "recording"]
