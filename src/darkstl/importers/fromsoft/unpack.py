"""Unpack a FromSoftware game's archives: the model files only, into a folder of the app's.

Dark Souls (Prepare to Die Edition), Dark Souls II, Dark Souls III, Sekiro and Elden Ring keep
their files in ``.bhd``/``.bdt`` pairs (see :mod:`.formats.bhd5`). This does what UXM Selective
Unpacker does for modders, but only for what Dark STL shows: characters, weapons and armour,
objects and assets, map pieces, with their textures and animations. It reads the game's folder
and writes nowhere in it; the files go to ``<app library>/<game>/`` by default, keeping the paths
the game gives them (``chr/c2010.chrbnd.dcx``), so they read like a folder UXM unpacked.

File names come from the community's lists (:data:`LIST_FILES`, bundled, plus newer ones the
user may download); names that follow a pattern of known ones (``c2010.chrbnd.dcx`` ->
``c2011.chrbnd.dcx``) are tried too, so models a list leaves out are still found. Files no name
is known for are skipped and counted.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from ...errors import FormatError
from .formats import bhd5
from .formats.bhd5_keys import key_for

Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class GameArchives:
    game: str
    fmt: str  # the header layout (bhd5.DS1 ... bhd5.ER)
    archives: tuple[str, ...]  # names without extension, as the game folder has them
    lists: tuple[str, ...]  # bundled file lists (filelists/<name>.txt.gz)
    downloads: tuple[str, ...]  # UXM Selective Unpacker's lists of the same game
    keys: str = "builtin"  # "builtin" (bhd5_keys), "pem" (Dark Souls II's KeyCode files) or "none"
    header_ext: str = ".bhd"


GAME_ARCHIVES: tuple[GameArchives, ...] = (
    GameArchives("Elden Ring", bhd5.ER, ("Data0", "Data1", "Data2", "Data3", "DLC"), ("eldenring",),
                 ("EldenRingDictionary.txt",)),
    GameArchives("Sekiro", bhd5.DS3, ("Data1", "Data2", "Data3", "Data4", "Data5"), ("sekiro",),
                 ("SekiroDictionary.txt",)),
    GameArchives("Dark Souls III", bhd5.DS3, ("Data1", "Data2", "Data3", "Data4", "Data5", "DLC1", "DLC2"),
                 ("darksouls3",), ("DarkSouls3Dictionary.txt",)),
    GameArchives("Dark Souls II", bhd5.DS2, ("GameDataEbl", "LqChrEbl", "LqMapEbl", "LqObjEbl", "LqPartsEbl"),
                 ("darksouls2",), ("DarkSouls2Dictionary.txt", "ScholarDictionary.txt"), keys="pem"),
    GameArchives("Dark Souls", bhd5.DS1, ("dvdbnd0", "dvdbnd1", "dvdbnd2", "dvdbnd3"), ("darksouls",),
                 ("DarkSoulsDictionary.txt",), keys="none", header_ext=".bhd5"),
)

# Where newer file lists come from (UXM Selective Unpacker by Nordgaren, the lists UXM started).
DOWNLOAD_URL = "https://raw.githubusercontent.com/Nordgaren/UXM-Selective-Unpack/master/UXM/res/{name}"
DOWNLOAD_PAGE = "https://github.com/Nordgaren/UXM-Selective-Unpack"
LIST_FILES = Path(__file__).with_name("filelists")


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    description: str
    folders: tuple[str, ...]  # path prefixes, lower case


GROUPS: tuple[Group, ...] = (
    Group("characters", "Characters", "Characters and enemies, with their textures and animations",
          ("/chr/", "/model/chr/", "/model_hq/chr/")),
    Group("parts", "Weapons and armour", "Weapons, shields, armour, faces and hair",
          ("/parts/", "/model/parts/", "/model_hq/parts/")),
    Group("objects", "Objects and assets", "Objects and, in Elden Ring, assets (the props placed in its maps)",
          ("/obj/", "/asset/", "/model/obj/", "/model_hq/obj/")),
    Group("maps", "Map pieces", "The pieces maps are built from, with their texture packs (large)",
          ("/map/", "/model/map/", "/model_hq/map/")),
    Group("names", "In-game names", "The game's item and character names and its item tables, so the library "
          "shows each model by the name it has in the game (small)", ("/msg/", "/param/gameparam/")),
)
DEFAULT_GROUPS = ("characters", "parts", "objects", "names")

# What of those folders Dark STL reads: models, texture packs, animations. Not collision, behaviour,
# scripts, effects or sound.
_WANTED = re.compile(r"\.(partsbnd|chrbnd|objbnd|geombnd|mapbnd|anibnd|texbnd|tpf|tpfbhd|tpfbdt|chrtpfbhd|chrtpfbdt"
                     r"|flver|flv|bnd)(\.dcx)?$", re.I)
_OODLE = re.compile(r"^(lib)?oo2core.*\.(dll|so|dylib)$", re.I)
# The names: the item text (msg/<language>/item*.msgbnd.dcx, and menu*) and the item tables
_NAMES = re.compile(r"^/msg/[^/]+/(item|menu)[^/]*\.msgbnd(\.dcx)?$|^/param/gameparam/gameparam[^/]*\.parambnd(\.dcx)?$",
                    re.I)
# the item tables some games keep beside their archives, encrypted (Elden Ring's, Dark Souls III's): copied as they are
_REGULATION = {"Elden Ring": ("regulation.bin",), "Dark Souls III": ("Data0.bdt",)}

MANIFEST = "darkstl-unpacked.json"


def archives_for(game: str | None) -> GameArchives | None:
    return next((g for g in GAME_ARCHIVES if g.game == game), None)


def group_of(path: str) -> str | None:
    """The group a file belongs to (``/chr/c2010.chrbnd.dcx`` -> "characters"), or None if not wanted."""
    low = path.lower()
    if _NAMES.search(low):
        return "names"
    if not _WANTED.search(low):
        return None
    if low.endswith((".bnd", ".bnd.dcx")) and not low.startswith(("/model/", "/model_hq/")):
        return None  # plain .bnd archives are models in Dark Souls II only
    return next((g.id for g in GROUPS if low.startswith(g.folders)), None)


# --------------------------------------------------------------------------- finding the archives


def archive_folder(folder: Path, spec: GameArchives | None = None) -> Path | None:
    """The folder the game keeps its archives in: ``folder`` or its ``Game`` folder (Elden Ring,
    Dark Souls II and III are installed as ``<name>/Game/``)."""
    folder = Path(folder)
    specs = [spec] if spec else list(GAME_ARCHIVES)
    for d in (folder, folder / "Game", folder / "game"):
        for s in specs:
            if any((d / f"{a}{s.header_ext}").is_file() and (d / f"{a}.bdt").is_file() for a in s.archives):
                return d
    return None


def _pem_path(folder: Path, archive: str) -> Path:
    return folder / f"{archive.replace('Ebl', 'KeyCode')}.pem"


def archive_key(spec: GameArchives, folder: Path, archive: str) -> bhd5.PublicKey | None:
    if spec.keys == "builtin":
        text = key_for(spec.game, archive)
        return bhd5.parse_public_key(text) if text else None
    if spec.keys == "pem":
        pem = _pem_path(folder, archive)
        return bhd5.parse_public_key(pem.read_text("ascii", "replace")) if pem.is_file() else None
    return None


def _opens_with(raw_first_block: bytes, key: bhd5.PublicKey | None) -> bool:
    if raw_first_block[:4] == bhd5.MAGIC:
        return True
    if key is None or len(raw_first_block) < key.size:
        return False
    try:
        return bhd5.rsa_decrypt(raw_first_block[:key.size], key)[:4] == bhd5.MAGIC
    except FormatError:
        return False


def packed_games(folder: Path) -> list[str]:
    """The games whose archives are in ``folder`` (or its Game folder), by their names alone (quick:
    Sekiro's and Dark Souls III's are named alike, so both are listed for either)."""
    return [s.game for s in GAME_ARCHIVES if archive_folder(folder, s) is not None]


def detect(folder: Path, hint: str | None = None) -> tuple[GameArchives, Path] | None:
    """Which game's archives ``folder`` holds, and where: by the archives' names, then (Sekiro and Dark
    Souls III share them) by which game's key opens their headers. ``hint`` (the game the folder was
    added as) is tried first."""
    specs = sorted(GAME_ARCHIVES, key=lambda s: s.game != hint)
    for spec in specs:
        where = archive_folder(folder, spec)
        if where is None:
            continue
        present = [a for a in spec.archives if (where / f"{a}{spec.header_ext}").is_file()]
        first = where / f"{present[0]}{spec.header_ext}"
        try:
            with open(first, "rb") as fh:
                head = fh.read(512)
            if _opens_with(head, archive_key(spec, where, present[0])):
                return spec, where
        except (OSError, FormatError):
            continue
    return None


# --------------------------------------------------------------------------- the headers


@dataclass
class Archive:
    name: str
    header: Path
    data: Path
    entries: list[bhd5.Entry] = field(default_factory=list)
    error: str = ""


def _cached_header(path: Path, cache: Path | None) -> Path | None:
    if cache is None:
        return None
    st = path.stat()
    return cache / f"{path.stem}-{st.st_size}-{int(st.st_mtime)}.bhd5"


def read_archive(spec: GameArchives, folder: Path, name: str, cache: Path | None = None,
                 progress: Progress | None = None) -> Archive:
    """An archive's file list, decrypted once and kept (decrypted) in ``cache`` for next time."""
    header = folder / f"{name}{spec.header_ext}"
    arc = Archive(name, header, folder / f"{name}.bdt")
    try:
        kept = _cached_header(header, cache)
        if kept is not None and kept.is_file():
            arc.entries = bhd5.read_header(kept.read_bytes(), spec.fmt).entries
            return arc
        raw = header.read_bytes()
        if bhd5.is_encrypted(raw):
            key = archive_key(spec, folder, name)
            if key is None:
                missing = (f"its key file {_pem_path(folder, name).name} isn't beside it" if spec.keys == "pem"
                           else "no key is known for it")
                raise FormatError(f"{header.name} is encrypted and {missing}")
            raw = bhd5.rsa_decrypt(raw, key, (lambda f: progress(f, f"Reading {header.name}")) if progress else None)
        arc.entries = bhd5.read_header(raw, spec.fmt).entries
        if kept is not None:
            kept.parent.mkdir(parents=True, exist_ok=True)
            for old in kept.parent.glob(f"{header.stem}-*.bhd5"):
                old.unlink(missing_ok=True)
            tmp = kept.with_suffix(".part")
            tmp.write_bytes(raw)
            os.replace(tmp, kept)
    except (OSError, FormatError) as exc:
        arc.error = str(exc)
    return arc


# --------------------------------------------------------------------------- file names


def _read_list(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("/") and len(line) < 400:
            out.append(line)
    return out


def bundled_lists(spec: GameArchives) -> list[Path]:
    return [p for p in (LIST_FILES / f"{n}.txt.gz" for n in spec.lists) if p.is_file()]


def downloaded_lists(spec: GameArchives, folder: Path | None) -> list[Path]:
    if folder is None:
        return []
    return [p for p in (Path(folder) / n for n in spec.downloads) if p.is_file()]


@lru_cache(maxsize=4)
def _names_from(files: tuple[tuple[Path, float], ...], fmt: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for p, _mtime in files:
        raw = p.read_bytes()
        text = (gzip.decompress(raw) if p.suffix == ".gz" else raw).decode("utf-8", "replace")
        for line in _read_list(text):
            names.setdefault(bhd5.hash_path(line, fmt), line)
    return names


def known_names(spec: GameArchives, lists_dir: Path | None = None) -> dict[int, str]:
    """Hash -> path of the game's files, from its downloaded and bundled lists."""
    paths = downloaded_lists(spec, lists_dir) + bundled_lists(spec)
    return _names_from(tuple((p, p.stat().st_mtime) for p in paths), spec.fmt) if paths else {}


_LAST_NUMBER = re.compile(r"^(.*?)(\d{1,4})(\D*)$")


def _hash_scalar(text: str, prime: int, mask: int, start: int = 0) -> int:
    h = start
    for c in text:
        h = (h * prime + ord(c)) & mask
    return h


def guess_names(known: dict[int, str], wanted: set[int], fmt: str, limit: int = 20_000_000) -> dict[int, str]:
    """Names for hashes no list has, made by trying every number in place of the last one in each known
    name (``/chr/c2010.chrbnd.dcx`` -> ``/chr/c0000`` ... ``/chr/c9999.chrbnd.dcx``)."""
    if not wanted:
        return {}
    prime = 133 if fmt == bhd5.ER else 37
    bits = bhd5.hash_bits(fmt)
    mask = (1 << bits) - 1
    templates: set[tuple[str, int, str]] = set()
    for path in known.values():
        m = _LAST_NUMBER.match(path.lower()) if group_of(path) else None
        if m:
            templates.add((m.group(1), len(m.group(2)), m.group(3)))
            if m.group(3).startswith("."):  # and its low-detail copy, which most models have
                templates.add((m.group(1), len(m.group(2)), "_l" + m.group(3)))
    targets = np.array(sorted(wanted), dtype=np.uint64)
    digits_hash: dict[int, np.ndarray] = {}
    found: dict[int, str] = {}
    tried = 0
    p64 = np.uint64(prime)
    order = {g.id: i for i, g in enumerate(GROUPS)}  # maps (many, and big) last, should the limit be reached
    for prefix, k, suffix in sorted(templates, key=lambda t: (order.get(group_of(t[0] + "0" * t[1] + t[2]), 9), t)):
        if tried > limit:
            break
        if k not in digits_hash:
            i = np.arange(10 ** k, dtype=np.uint64)
            h = np.zeros(10 ** k, dtype=np.uint64)
            for j in range(k):
                h = h * p64 + (np.uint64(48) + (i // np.uint64(10 ** (k - 1 - j))) % np.uint64(10))
            digits_hash[k] = h
        tail = len(suffix)
        hp = _hash_scalar(prefix, prime, (1 << 64) - 1)
        lead = (hp * pow(prime, k + tail, 1 << 64)) & ((1 << 64) - 1)
        hs = _hash_scalar(suffix, prime, (1 << 64) - 1)
        scale = pow(prime, tail, 1 << 64)
        full = np.uint64(lead) + digits_hash[k] * np.uint64(scale) + np.uint64(hs)
        if bits == 32:
            full = full & np.uint64(mask)
        tried += full.size
        at = np.searchsorted(targets, full).clip(0, targets.size - 1)
        hits = np.nonzero(targets[at] == full)[0]
        for n in hits:
            h = int(full[n])
            if h not in found and h not in known:
                found[h] = f"{prefix}{int(n):0{k}d}{suffix}"
    return found


# --------------------------------------------------------------------------- planning


@dataclass
class PlannedFile:
    archive: str
    path: str  # '/'-separated, from the game's root ("/chr/c2010.chrbnd.dcx")
    group: str
    entry: bhd5.Entry
    present: bool = False


@dataclass
class Plan:
    game: str
    source: Path  # the folder the archives are in
    dest: Path
    archives: list[Archive]
    files: list[PlannedFile]
    unknown: int  # files no name is known for (skipped)
    guessed: int  # names found by pattern, not from a list
    lists: list[str]

    def chosen(self, groups: tuple[str, ...] | list[str]) -> list[PlannedFile]:
        return [f for f in self.files if f.group in groups]

    def summary(self) -> dict:
        groups = []
        for g in GROUPS:
            fs = [f for f in self.files if f.group == g.id]
            todo = [f for f in fs if not f.present]
            groups.append({"id": g.id, "label": g.label, "description": g.description, "files": len(fs),
                           "bytes": sum(f.entry.file_size for f in fs), "to_unpack": len(todo),
                           "bytes_to_unpack": sum(f.entry.file_size for f in todo),
                           "default": g.id in DEFAULT_GROUPS})
        return {"game": self.game, "source": str(self.source), "dest": str(self.dest), "groups": groups,
                "archives": [{"name": a.name, "files": len(a.entries), "error": a.error} for a in self.archives],
                "unknown": self.unknown, "guessed": self.guessed, "lists": self.lists,
                "free_bytes": _free_bytes(self.dest)}


def _free_bytes(path: Path) -> int | None:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def plan(folder: Path, dest: Path, game: str | None = None, cache: Path | None = None,
         lists_dir: Path | None = None, progress: Progress | None = None) -> Plan:
    """What unpacking the game in ``folder`` into ``dest`` would write (every group; :meth:`Plan.chosen`
    narrows it), reading each archive's header (decrypted once, then kept in ``cache``)."""
    found = detect(Path(folder), game)
    if found is None:
        raise FormatError(f"There are no game archives (Data0.bhd, Data1.bhd, GameDataEbl.bhd, dvdbnd0.bhd5 ...) "
                          f"in {folder} or its Game folder.")
    spec, where = found
    present = [a for a in spec.archives if (where / f"{a}{spec.header_ext}").is_file()
               and (where / f"{a}.bdt").is_file()]
    archives = []
    for i, name in enumerate(present):
        def step(f: float, msg: str, i=i) -> None:
            if progress:
                progress(0.8 * (i + f) / len(present), msg)

        step(0.0, f"Reading {name}{spec.header_ext}")
        archives.append(read_archive(spec, where, name, cache / spec.game if cache else None, step))
    if all(a.error for a in archives):
        raise FormatError("; ".join(a.error for a in archives))
    if progress:
        progress(0.82, "Matching file names")
    names = known_names(spec, lists_dir)
    all_hashes = {e.hash for a in archives for e in a.entries}
    if progress:
        progress(0.88, "Trying names that follow the known ones")
    guessed = guess_names(names, all_hashes - names.keys(), spec.fmt)
    files: list[PlannedFile] = []
    unknown = 0
    seen: set[str] = set()
    dest = Path(dest)
    check_guesses = bhd5.hash_bits(spec.fmt) < 64  # 32-bit hashes: a guess can hit another file's hash
    for a in reversed(archives):  # a later archive (the DLC's) has the newer copy of a file in both
        for e in a.entries:
            path = names.get(e.hash)
            if path is None and e.hash in guessed:
                path = guessed[e.hash]
                if check_guesses and not _looks_like(a.data, e, path):
                    path = None
            if path is None:
                unknown += 1
                continue
            group = group_of(path)
            if group is None or path.lower() in seen:
                continue
            seen.add(path.lower())
            target = dest / path.lstrip("/")
            pf = PlannedFile(a.name, path, group, e)
            try:
                pf.present = target.stat().st_size == e.file_size
            except OSError:
                pass
            files.append(pf)
    files.sort(key=lambda f: (f.archive, f.entry.offset))
    lists = [p.name for p in downloaded_lists(spec, lists_dir) + bundled_lists(spec)]
    return Plan(spec.game, where, dest, archives, files, unknown,
                sum(1 for f in files if f.entry.hash in guessed), lists)


_MAGICS = {".tpf": (b"TPF\0",), ".flver": (b"FLVER\0",), ".flv": (b"FLVER\0",), ".tpfbdt": (b"BDF3", b"BDF4"),
           ".chrtpfbdt": (b"BDF3", b"BDF4")}


def _looks_like(bdt_path: Path, entry: bhd5.Entry, path: str) -> bool:
    """Whether a file found by a guessed name is that file: the right kind of file, and (archives name
    the files they hold) its name inside it. Only Dark Souls I-III and Sekiro need this: with 32-bit
    hashes, trying millions of names hits a few other files' hashes."""
    try:
        with open(bdt_path, "rb") as fh:
            data = bhd5.read_entry(fh, entry)
    except (OSError, FormatError):
        return False
    name = path.rsplit("/", 1)[-1].lower()
    compressed = name.endswith(".dcx")
    if compressed:
        if data[:4] not in (b"DCX\0", b"DCP\0"):
            return False
        try:
            from .formats import dcx

            data = dcx.decompress(data)[0]
        except Exception:  # noqa: BLE001 - e.g. no Oodle library to look inside Sekiro's: the kind must do
            return True
        name = name[:-4]
    magics = _MAGICS.get("." + name.rsplit(".", 1)[-1], (b"BND3", b"BND4"))
    if not data.startswith(magics):
        return False
    if magics[0].startswith(b"BND"):  # a binder names its files after the model: c2010.chrbnd holds c2010.flver
        stem = name.split(".", 1)[0]
        stem = re.sub(r"_l$", "", stem)
        head = data[:1 << 16].lower()
        return stem.encode() in head or stem.encode("utf-16-le") in head
    return True


def run(p: Plan, groups: tuple[str, ...] | list[str], progress: Progress | None = None) -> dict:
    """Write the chosen groups' files (those not there already) into ``p.dest``, with the game's Oodle
    library beside them, and a note of what this folder is."""
    todo = [f for f in p.chosen(groups) if not f.present]
    total = sum(f.entry.file_size for f in todo) or 1
    done = written = 0
    by_archive: dict[str, list[PlannedFile]] = {}
    for f in todo:
        by_archive.setdefault(f.archive, []).append(f)
    archives = {a.name: a for a in p.archives}
    failed: list[str] = []
    for name, files in by_archive.items():
        with open(archives[name].data, "rb") as bdt:
            for f in files:
                if progress:
                    progress(done / total, f.path.lstrip("/"))
                target = p.dest / f.path.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                try:
                    tmp.write_bytes(bhd5.read_entry(bdt, f.entry))
                    os.replace(tmp, target)
                    f.present = True
                    written += 1
                except (OSError, FormatError) as exc:
                    tmp.unlink(missing_ok=True)
                    failed.append(f"{f.path}: {exc}")
                done += f.entry.file_size
    if "names" in groups:  # the item tables the game keeps beside its archives, for the models' names
        for name in _REGULATION.get(p.game, ()):
            src = p.source / name
            if src.is_file() and not (p.dest / name).is_file():
                p.dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, p.dest / name)
    oodle = []
    for lib in sorted(p.source.iterdir()):
        if _OODLE.match(lib.name) and lib.is_file() and not (p.dest / lib.name).is_file():
            p.dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib, p.dest / lib.name)
            oodle.append(lib.name)
    write_note(p, groups)
    if progress:
        progress(1.0, "Done")
    return {"game": p.game, "dest": str(p.dest), "written": written, "bytes": sum(f.entry.file_size for f in todo),
            "present": len(p.chosen(groups)) - len(todo), "failed": failed[:20], "failed_count": len(failed),
            "oodle": oodle}


def write_note(p: Plan, groups) -> None:
    old = read_note(p.dest) or {}
    data = {"app": "Dark STL", "game": p.game, "source": str(p.source),
            "groups": sorted(set(old.get("groups") or []) | set(groups)),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    p.dest.mkdir(parents=True, exist_ok=True)
    (p.dest / MANIFEST).write_text(json.dumps(data, indent=2), "utf-8")


def read_note(folder: Path) -> dict | None:
    try:
        data = json.loads((Path(folder) / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- newer file lists


def lists_status(lists_dir: Path | None) -> list[dict]:
    out = []
    for spec in GAME_ARCHIVES:
        got = downloaded_lists(spec, lists_dir)
        out.append({"game": spec.game, "bundled": [p.name for p in bundled_lists(spec)],
                    "downloaded": [p.name for p in got],
                    "downloaded_at": max((time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))
                                          for p in got), default=None)})
    return out


def download_lists(lists_dir: Path, games: list[str] | None = None, progress: Progress | None = None,
                   opener=None) -> dict:
    """Fetch UXM Selective Unpacker's file lists (``games``, or all) into ``lists_dir``."""
    lists_dir = Path(lists_dir)
    lists_dir.mkdir(parents=True, exist_ok=True)
    names = [n for s in GAME_ARCHIVES if not games or s.game in games for n in s.downloads]
    fetch = opener or (lambda url: urllib.request.urlopen(url, timeout=60).read())  # noqa: S310 - https only
    got, failed = [], []
    for i, name in enumerate(names):
        if progress:
            progress(i / len(names), f"Downloading {name}")
        try:
            raw = fetch(DOWNLOAD_URL.format(name=name))
            text = raw.decode("utf-8-sig", "replace")
            if len(_read_list(text)) < 100:
                raise FormatError("that isn't a file list")
            tmp = lists_dir / f"{name}.part"
            tmp.write_text(text.replace("\r\n", "\n"), "utf-8")
            os.replace(tmp, lists_dir / name)
            got.append(name)
        except Exception as exc:  # noqa: BLE001 - reported per list, the others still come
            failed.append(f"{name}: {exc}")
    _names_from.cache_clear()
    if progress:
        progress(1.0, "Done")
    return {"downloaded": got, "failed": failed, "folder": str(lists_dir)}


__all__ = ["DEFAULT_GROUPS", "GAME_ARCHIVES", "GROUPS", "GameArchives", "Plan", "archive_folder", "archives_for",
           "detect", "download_lists", "packed_games", "group_of", "guess_names", "known_names", "lists_status", "plan", "read_note",
           "run"]
