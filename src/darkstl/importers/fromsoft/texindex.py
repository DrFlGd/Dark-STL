"""Where a game's textures are, so that a model whose textures aren't beside it still finds them.

Most models carry their textures, inside their archive or in a ``.texbnd`` beside it. Others
use textures kept elsewhere in the game: map pieces and many objects use their map's packs
(``map/m24/m24_0000.tpfbhd``), armour shares packs such as ``parts/common_body.tpf.dcx``, and
some characters borrow another character's archive. When a game folder is scanned, every
texture file in it is listed with the names of the textures it holds, read from the files'
headers (for zlib-compressed files only their first part is unpacked), and the model archives
are listed by name. A model then looks up the textures it misses there (:meth:`TextureIndex.candidates`).

The names are kept in the importer's cache folder, so a rescan only reads the files that changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .formats import binder as bnd
from .formats import dcx, tpf
from .formats.binary import FormatError

VERSION = 1
TEXTURE_FILE = re.compile(r"\.(tpf|texbnd)(\.dcx)?$|\.(tpfbhd|chrtpfbhd|dds)$", re.I)
MODEL_FILE = re.compile(r"\.(partsbnd|chrbnd|objbnd|geombnd|mapbnd|flver|flv)(\.dcx)?$", re.I)
HEAD = 64 << 10  # bytes read first: a texture pack's names come before its pictures
PER_NAME = 3  # files tried for a texture found only by the start of its name
# "no texture here": the games' stand-ins for an unused slot (a flat normal map, a blank mask), not looked for
PLACEHOLDER = re.compile(r"^systex_dummy", re.I)

_lock = threading.Lock()
_indexes: dict[str, "TextureIndex"] = {}


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _stem(name: str) -> str:
    """'c2509_h.texbnd.dcx' -> 'c2509'; 'm24_0000.tpfbhd' -> 'm24_0000'; 'c2509-chrbnd-dcx' -> 'c2509'."""
    from .files import file_stem

    return re.sub(r"_(h|l)$", "", file_stem(name).lower())


def texture_name(entry: str) -> str:
    """A texture's name from the name of the file holding it alone ('m24_wall_a.tpf.dcx' -> 'm24_wall_a')."""
    base = entry.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"(\.tpf|\.dds)?(\.dcx)?$", "", base, flags=re.I)


@dataclass
class TextureIndex:
    root: Path
    names: dict[str, list[str]] = field(default_factory=dict)  # texture name (lower) -> files holding it
    stems: dict[str, list[str]] = field(default_factory=dict)  # file stem -> texture files and model archives
    counts: dict[str, int] = field(default_factory=dict)  # file -> how many textures it holds

    def path(self, rel: str) -> Path:
        return self.root / rel

    def candidates(self, wanted: Iterable[str], exclude: Iterable[Path] = ()) -> list[tuple[Path, set[str]]]:
        """Files that may hold the ``wanted`` textures, best first, with the names each is expected to hold:
        files known to hold a texture of that name, then a file named after it, then files named after
        the start of its name (``c2509_body_a`` is looked for in ``c2509.chrbnd.dcx``)."""
        skip = {_norm(Path(p)) for p in exclude}
        out: dict[str, set[str]] = {}

        def add(rel: str, name: str) -> None:
            if _norm(self.path(rel)) not in skip:
                out.setdefault(rel, set()).add(name)

        for name in sorted({w.lower() for w in wanted}):
            if name in self.names:
                for rel in self.names[name]:
                    add(rel, name)
                continue
            tried = 0
            parts = name.split("_")
            for n in range(len(parts), 0, -1):
                for rel in self.stems.get("_".join(parts[:n]), []):
                    if tried < PER_NAME:
                        add(rel, name)
                        tried += 1
        return [(self.path(rel), names) for rel, names in out.items()]

    def size_of(self, rel_or_path: str | Path) -> tuple[int, int]:
        """(bytes, textures) of a file in the index, for estimating what one texture taken from it weighs."""
        p = Path(rel_or_path)
        rel = p.relative_to(self.root).as_posix() if p.is_absolute() else str(rel_or_path)
        try:
            size = self.path(rel).stat().st_size
        except OSError:
            size = 0
        return size, max(self.counts.get(rel, 1), 1)


def index_for(path: str | os.PathLike) -> TextureIndex | None:
    """The index of the game folder that holds ``path`` (the innermost, when folders nest)."""
    p = _norm(Path(path))
    with _lock:
        best = None
        for root, index in _indexes.items():
            if (p == root or p.startswith(root.rstrip(os.sep) + os.sep)) and (best is None or len(root) > len(best[0])):
                best = (root, index)
    return best[1] if best else None


def register(index: TextureIndex) -> None:
    with _lock:
        _indexes[_norm(index.root)] = index


def forget(root: str | os.PathLike) -> None:
    with _lock:
        _indexes.pop(_norm(Path(root)), None)


# ---------------------------------------------------------------------------
# Building


def names_in_file(path: Path) -> list[str]:
    """The names of the textures a texture file holds, read from as little of it as will do."""
    low = path.name.lower()
    if low.endswith(".dds"):
        return [path.name[:-4]]
    if low.endswith(("tpfbhd", "chrtpfbhd")):  # the header of a split pack names its entries
        b = bnd.read_binder(dcx.maybe_decompress(path.read_bytes()), b"")
        return [texture_name(f.name) for f in b.files]
    size = path.stat().st_size
    for head in (HEAD, None):
        if head is not None and size <= head:
            continue
        with open(path, "rb") as fh:
            raw = fh.read(head) if head else fh.read()
        try:
            if dcx.is_dcx(raw):
                data = dcx.decompress_head(raw, 8 << 20) if head else dcx.maybe_decompress(raw)
                if data is None:
                    continue  # Oodle and others: only the whole file will do
            else:
                data = raw
            return _names(data)
        except FormatError:
            if head is None:
                raise
    return []


def _names(data: bytes) -> list[str]:
    if tpf.is_tpf(data):
        return tpf.texture_names(data)
    if bnd.is_binder(data) and data[:4] in (b"BND3", b"BND4"):
        out = []
        for f in bnd.read_binder(data).files:
            if f.basename.lower().endswith((".tpf", ".tpf.dcx")):
                out += tpf.texture_names(dcx.maybe_decompress(f.data))
        return out
    raise FormatError("Not a texture pack")


def walk(root: Path) -> list[Path]:
    """Every file under ``root``, folders in name order."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        out += [Path(dirpath) / n for n in sorted(filenames)]
    return out


def build(root: str | os.PathLike, paths: Iterable[Path] | None = None, cache: Path | None = None,
          progress: Callable[[int, str], None] | None = None) -> TextureIndex:
    """Index the textures under ``root`` (``paths``: its files, when they're listed already) and register
    the index, so the models under ``root`` find their textures in it. Names read before are taken from
    ``cache`` when the file's size and date haven't changed."""
    root = Path(root)
    paths = walk(root) if paths is None else list(paths)
    cache_file = cache / f"{hashlib.sha1(_norm(root).encode('utf-8')).hexdigest()[:16]}.json" if cache else None
    before = _read_cache(cache_file, root)
    index = TextureIndex(root)
    known: dict[str, list] = {}
    todo = []
    for path in paths:
        name = path.name
        if MODEL_FILE.search(name):
            rel = path.relative_to(root).as_posix()
            stem = _stem(path.parent.name) if name.lower().endswith((".flver", ".flv", ".flver.dcx")) and \
                re.search(r"-(partsbnd|chrbnd|objbnd|geombnd|mapbnd)(-dcx)?$", path.parent.name, re.I) else _stem(name)
            index.stems.setdefault(stem, []).append(rel)
            continue
        if not TEXTURE_FILE.search(name):
            continue
        rel = path.relative_to(root).as_posix()
        index.stems.setdefault(_stem(name), []).append(rel)
        try:
            st = path.stat()
        except OSError:
            continue
        old = before.get(rel)
        if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
            known[rel] = old
        else:
            todo.append((rel, path, st))
    for i, (rel, path, st) in enumerate(todo):
        if progress and i % 20 == 0:  # a negative count: the text says it all
            progress(-1, f"Reading texture names, {i + 1:,} of {len(todo):,}: {path.name}")
        try:
            known[rel] = [st.st_size, st.st_mtime_ns, names_in_file(path)]
        except (FormatError, OSError, ValueError):
            continue  # unreadable (Oodle not found yet, say): tried again on the next scan
    for rel, (_size, _mtime, names) in known.items():
        index.counts[rel] = len(names)
        for n in names:
            index.names.setdefault(n.lower(), []).append(rel)
    if cache_file is not None and (todo or len(known) != len(before)):
        _write_cache(cache_file, root, known)
    register(index)
    return index


def _read_cache(file: Path | None, root: Path) -> dict[str, list]:
    if file is None:
        return {}
    try:
        data = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("version") != VERSION or data.get("root") != _norm(root):
        return {}
    return data.get("files") or {}


def _write_cache(file: Path, root: Path, files: dict[str, list]) -> None:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"version": VERSION, "root": _norm(root), "files": files}), "utf-8")
        os.replace(tmp, file)
    except OSError:
        pass  # indexed again next time


__all__ = ["TextureIndex", "build", "forget", "index_for", "names_in_file", "register", "texture_name", "walk"]
