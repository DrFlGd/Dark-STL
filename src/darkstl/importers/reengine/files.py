"""Finding and opening an RE Engine model's files: the mesh, its streamed half, its materials and textures.

Files come in two shapes:

* extracted, below the game's ``natives/<platform>/`` tree (``x64`` for RE2, RE3 and RE7, ``stm``
  from Resident Evil Village on), or in any folder of loose files
* still inside a ``.pak`` archive, named through a community file list (``*.list``) kept in the
  game folder; such a file is addressed by a path made of the archive's path and the entry's
  name inside it (``re_chunk_000.pak/natives/stm/.../x.mesh.221108797``)

Materials (``.mdf2``) sit beside the mesh and name their textures by path below the platform
folder, without the version suffix (``Character/pl0000/pl0000_ALBD.tex``). Those are found in
place, in the ``streaming/`` tree (where the full-size copies of many textures live), or by name
anywhere in the game folder through the index :func:`build_index` makes as a folder is scanned.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from ...errors import FormatError
from .formats import mdf as mdffmt
from .formats import mesh as meshfmt
from .formats import pak as pakfmt
from .formats import tex as texfmt
from .games import MESH_FILE, PAK_FILE, version_of

MDF_FILE = re.compile(r"\.mdf2(\.\d+)?$", re.I)
LIST_FILE = re.compile(r"\.list$", re.I)
PLATFORMS = ("x64", "stm", "ps4", "ps5", "nsw", "msg", "mac")
INDEX_VERSION = 1

ScanProgress = Callable[[int, str], None]


def split_name(name: str) -> tuple[str, str, int | None]:
    """'pl0000.mesh.1808312334' -> ('pl0000', 'mesh', 1808312334); 'a.tex' -> ('a', 'tex', None)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    m = re.match(r"^(.*?)\.(mesh|tex|mdf2|pak)(?:\.(\d+))?$", base, re.I)
    if not m:
        stem, _, ext = base.rpartition(".")
        return stem or base, ext.lower(), None
    return m.group(1), m.group(2).lower(), int(m.group(3)) if m.group(3) else None


def file_stem(name: str) -> str:
    return split_name(name)[0]


def is_streaming(rel: str) -> bool:
    return "/streaming/" in f"/{rel.lower().replace(chr(92), '/')}/"


@dataclass
class ModelPath:
    """A file of the game: on disk, or an entry of a pak archive (then ``path`` is the archive's path
    with the entry's name appended, and ``rel`` the entry's name)."""

    path: Path
    rel: str  # '/'-separated, below the game folder (or the entry name inside its pak)
    size: int = 0
    in_pak: bool = False

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def stem(self) -> str:
        return split_name(self.name)[0]

    @property
    def kind(self) -> str:
        return split_name(self.name)[1]

    @property
    def is_mesh(self) -> bool:
        return bool(MESH_FILE.search(self.name))


# ---------------------------------------------------------------------------
# Pak archives and their names


@lru_cache(maxsize=16)
def _open_pak(path: str) -> pakfmt.Pak:
    return pakfmt.read_pak(path)


def _list_names(list_files: list[Path], cache: Path | None) -> dict[int, str]:
    """hash -> name for every community file list given (hashing is kept in the importer's cache)."""
    names: dict[int, str] = {}
    for lf in list_files:
        try:
            st = lf.stat()
        except OSError:
            continue
        key = f"{lf.name}-{st.st_size}-{int(st.st_mtime)}"
        cached = cache / "lists" / (re.sub(r"[^\w.-]", "_", key) + ".npz") if cache else None
        table: dict[int, str] | None = None
        if cached is not None and cached.is_file():
            try:
                with np.load(cached, allow_pickle=False) as z:
                    hashes = z["hashes"]
                    text = str(z["names"])
                lines = text.split("\n")
                if len(lines) == len(hashes):
                    table = {int(h): n for h, n in zip(hashes, lines)}
            except (OSError, ValueError, KeyError):
                table = None
        if table is None:
            try:
                table = pakfmt.read_name_list(lf)
            except OSError:
                continue
            if cached is not None:
                try:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    items = list(table.items())
                    np.savez(cached, hashes=np.array([h for h, _ in items], np.uint64),
                             names=np.array("\n".join(n for _, n in items)))
                except OSError:
                    pass
        names.update(table)
    return names


def locate(path: Path) -> tuple[pakfmt.Pak, pakfmt.Entry] | None:
    """The archive and entry a pak-entry path stands for, or None for a file on disk."""
    path = Path(path)
    if path.is_file():
        return None
    for parent in path.parents:
        if PAK_FILE.search(parent.name) and parent.is_file():
            rel = path.relative_to(parent).as_posix()
            pak = _open_pak(str(parent))
            entry = pak.find(rel)
            if entry is None:
                raise FormatError(f"{parent.name} holds no file named {rel}")
            return pak, entry
    return None


def read_bytes(path: Path) -> bytes:
    found = locate(Path(path))
    if found is None:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError as exc:
            raise FormatError(f"{path} does not exist") from exc
    pak, entry = found
    return pak.read(entry)


def exists(path: Path) -> bool:
    try:
        return Path(path).is_file() or locate(Path(path)) is not None
    except FormatError:
        return False


def size_of(path: Path) -> int:
    try:
        found = locate(Path(path))
    except FormatError:
        return 0
    if found is None:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0
    return found[1].size


# ---------------------------------------------------------------------------
# Walking a game folder


def walk(root: Path, cache: Path | None = None,
         progress: ScanProgress | None = None) -> tuple[list[ModelPath], list[str]]:
    """Every file under ``root`` as a :class:`ModelPath`, pak entries included (named through the
    ``*.list`` files in the folder), and notes about archives that could not be listed."""
    root = Path(root)
    files: list[ModelPath] = []
    paks: list[Path] = []
    lists: list[Path] = []
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if PAK_FILE.search(name):
                paks.append(p)
            elif LIST_FILE.search(name):
                lists.append(p)
            else:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                files.append(ModelPath(p, rel, size))
            n += 1
            if progress and n % 500 == 0:
                progress(n, name)
    notes: list[str] = []
    if paks:
        names = _list_names(lists, cache) if lists else {}
        encrypted: list[str] = []
        unnamed: list[str] = []
        for p in paks:
            if progress:
                progress(-1, f"Reading {p.name}")
            try:
                pak = _open_pak(str(p))
            except FormatError:
                continue
            if pak.encrypted:
                encrypted.append(p.name)
                continue
            if names:
                named = pak.name_entries(names)
            else:
                named = 0
            if not named and pak.entries:
                unnamed.append(p.name)
                continue
            for e in pak.entries:
                if e.name:
                    files.append(ModelPath(p / e.name, e.name, e.size, in_pak=True))
        if encrypted:
            notes.append(f"{_join(encrypted)}: the archive's table of contents is encrypted, which Dark STL "
                         "cannot read. Extract the game's files with a pak tool (REtool, ree-pak) and add that folder.")
        if unnamed:
            what = "no file list was found" if not lists else "the file lists found don't name their entries"
            notes.append(f"{_join(unnamed)}: pak archives don't store file names and {what}. Put the game's "
                         "file list (a .list file from the RE Engine modding community) in this folder and rescan, "
                         "or extract the archives with a pak tool.")
    return files, notes


def _join(names: list[str], limit: int = 4) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


# ---------------------------------------------------------------------------
# The index: where a game's textures and materials are, by name


@dataclass
class FileIndex:
    root: Path
    by_name: dict[str, list[str]] = field(default_factory=dict)  # lower-case name without version -> rels
    paks: dict[str, str] = field(default_factory=dict)  # rel of a pak entry -> rel of its pak
    sizes: dict[str, int] = field(default_factory=dict)

    def path(self, rel: str) -> Path:
        pak = self.paks.get(rel)
        return self.root / pak / rel if pak else self.root / rel

    def lookup(self, name: str) -> list[Path]:
        """Files called ``name`` (any version), the ones in ``streaming/`` first (they are the big copies)."""
        key = _name_key(name)
        rels = self.by_name.get(key, [])
        rels = sorted(rels, key=lambda r: (not is_streaming(r), r.lower()))
        return [self.path(r) for r in rels]

    def add(self, mp: ModelPath, pak_rel: str | None) -> None:
        kind = mp.kind
        if kind not in ("tex", "mdf2", "mesh"):
            return
        self.by_name.setdefault(_name_key(mp.name), []).append(mp.rel)
        if pak_rel:
            self.paks[mp.rel] = pak_rel
        self.sizes[mp.rel] = mp.size


def _name_key(name: str) -> str:
    stem, kind, _ = split_name(name)
    return f"{stem}.{kind}".lower()


_lock = threading.Lock()
_indexes: dict[str, FileIndex] = {}


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def build_index(root: Path, files: Iterable[ModelPath], cache: Path | None = None) -> FileIndex:
    root = Path(root)
    index = FileIndex(root)
    for mp in files:
        pak_rel = None
        if mp.in_pak:
            pak_path = next((p for p in mp.path.parents if PAK_FILE.search(p.name)), None)
            pak_rel = pak_path.relative_to(root).as_posix() if pak_path is not None else None
        index.add(mp, pak_rel)
    with _lock:
        _indexes[_norm(root)] = index
    if cache is not None:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / (re.sub(r"[^\w.-]", "_", _norm(root))[-120:] + ".json")).write_text(json.dumps(
                {"version": INDEX_VERSION, "root": str(root), "by_name": index.by_name, "paks": index.paks}))
        except OSError:
            pass
    return index


def index_for(path: Path) -> FileIndex | None:
    key = _norm(path)
    with _lock:
        best = None
        for root, index in _indexes.items():
            if key.startswith(root.rstrip("\\/") + os.sep) or key == root:
                if best is None or len(root) > len(best.root.as_posix()):
                    best = index
        return best


def forget(root: Path) -> None:
    with _lock:
        _indexes.pop(_norm(root), None)


# ---------------------------------------------------------------------------
# Finding a model's companions


def natives_root(path: Path) -> tuple[Path, str] | None:
    """``(folder holding natives/, platform)`` for a path below ``natives/<platform>/``."""
    parts = [p.lower() for p in Path(path).parts]
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == "natives":
            return Path(*Path(path).parts[:i]) if i else Path(Path(path).anchor or "."), Path(path).parts[i + 1]
    return None


def _platform_path(path: Path, rel_below_platform: str) -> Path | None:
    """The path of a file given relative to the platform folder of the same tree as ``path``."""
    nr = natives_root(path)
    if nr is None:
        return None
    base, platform = nr
    return base / "natives" / platform / rel_below_platform.replace("\\", "/")


def streaming_partner(path: Path) -> Path | None:
    """The copy of ``path`` under ``natives/<platform>/streaming/``, if there is one."""
    path = Path(path)
    nr = natives_root(path)
    if nr is None or is_streaming(path.as_posix()):
        return None
    base, platform = nr
    tree = base / "natives" / platform
    try:
        rel = path.relative_to(tree)
    except ValueError:
        return None
    candidate = tree / "streaming" / rel
    return candidate if exists(candidate) else None


def _resolve_dir(base: Path, rel: str) -> Path:
    """``base/rel`` with each folder of ``rel`` matched regardless of case (the engine's paths are
    case-insensitive; extracted trees are often all lower case while the materials say ``_Chainsaw``)."""
    current = Path(base)
    for segment in [s for s in rel.replace("\\", "/").split("/") if s]:
        candidate = current / segment
        if candidate.is_dir() or not current.is_dir():
            current = candidate  # exists as written, or is inside a pak: compared lower-case there
            continue
        low = segment.lower()
        try:
            match = next((p for p in current.iterdir() if p.name.lower() == low and p.is_dir()), None)
        except OSError:
            match = None
        current = match if match is not None else candidate
    return current


def _listing(directory: Path) -> dict[str, Path]:
    """Files in a folder (on disk, or inside a pak for a pak-entry path), by lower-case name."""
    directory = Path(directory)
    if directory.is_dir():
        try:
            return {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
        except OSError:
            return {}
    for parent in directory.parents:
        if PAK_FILE.search(parent.name) and parent.is_file():
            prefix = directory.relative_to(parent).as_posix().lower() + "/"
            out = {}
            try:
                pak = _open_pak(str(parent))
            except FormatError:
                return {}
            for e in pak.entries:
                if e.name and e.name.lower().startswith(prefix) and "/" not in e.name[len(prefix):]:
                    out[e.name[len(prefix):].lower()] = parent / e.name
            return out
    return {}


def _versions_of(listing: dict[str, Path], stem: str, kind: str) -> list[Path]:
    rx = re.compile(rf"^{re.escape(stem.lower())}\.{kind}(\.\d+)?$")
    return [p for n, p in sorted(listing.items()) if rx.match(n)]


@dataclass(eq=False)
class TexRef:
    name: str  # the texture's stem, e.g. 'pl0000_body_ALBD'
    path: Path
    source: str

    def data(self) -> bytes:
        return read_bytes(self.path)

    def tex(self) -> texfmt.TexFile:
        return _tex_cached(self)

    def decode(self, mip: int = 0) -> np.ndarray:
        return _decode_cached(self, mip)

    def describe(self) -> str:
        return self.tex().describe()


@lru_cache(maxsize=64)
def _tex_cached(ref: TexRef) -> texfmt.TexFile:
    return texfmt.read_tex(ref.data())


@lru_cache(maxsize=32)
def _decode_cached(ref: TexRef, mip: int) -> np.ndarray:
    return ref.tex().decode(mip)


@dataclass
class ModelAsset:
    name: str
    path: Path
    mesh: meshfmt.ReMesh
    mdf: mdffmt.MdfFile | None = None
    mdf_path: Path | None = None
    streaming_path: Path | None = None
    textures: dict[str, TexRef] = field(default_factory=dict)  # lower-case stem -> texture
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # texture paths the materials name that were not found


def find_mdf(path: Path, material_names: list[str]) -> Path | None:
    """The materials file of a mesh: one of the same name beside it, else another in its folder that
    knows its materials, else one of the same name anywhere in the game folder."""
    path = Path(path)
    stem = file_stem(path.name)
    listing = _listing(path.parent)
    own = _versions_of(listing, stem, "mdf2")
    if own:
        return own[-1]
    # RE4 and later name the mesh 'cha000_00.mesh' and its materials 'cha000_00.mdf2' as well, but LOD or
    # variant meshes ('cha000_00_l.mesh') borrow the base one's
    base = re.sub(r"_(l|lod\d*|\d{2})$", "", stem, flags=re.I)
    if base != stem:
        own = _versions_of(listing, base, "mdf2")
        if own:
            return own[-1]
    wanted = {m.lower() for m in material_names}
    others = [p for n, p in sorted(listing.items()) if MDF_FILE.search(n)]
    for p in others:
        try:
            names = {m.name.lower() for m in mdffmt.read_mdf(read_bytes(p)).materials}
        except FormatError:
            continue
        if wanted and wanted <= names:
            return p
    index = index_for(path)
    if index is not None:
        found = index.lookup(f"{stem}.mdf2")
        if found:
            return found[0]
    return others[0] if len(others) == 1 else None


def find_texture(path: Path, binding: str, texture_dirs: tuple[str, ...] = ()) -> Path | None:
    """The .tex file a material binding names, biggest copy first: the ``streaming/`` tree, then the
    platform tree itself, then by name beside the mesh, in the index, and in the user's texture folders."""
    binding = binding.replace("\\", "/").strip()
    if not binding:
        return None
    stem, kind, _ = split_name(binding)
    kind = kind or "tex"
    candidates: list[Path] = []
    nr = natives_root(path)
    if nr is not None:
        base, platform = nr
        tree = base / "natives" / platform
        rel = binding[:-len(Path(binding).name)].rstrip("/")
        for sub in ("streaming", ""):
            folder = _resolve_dir(tree, f"{sub}/{rel}" if sub else rel)
            candidates += _versions_of(_listing(folder), stem, kind)
            if candidates:
                return candidates[-1]
    found = _versions_of(_listing(Path(path).parent), stem, kind)
    if found:
        return found[-1]
    index = index_for(path)
    if index is not None:
        hits = index.lookup(f"{stem}.{kind}")
        if hits:
            tail = binding.lower()
            exact = [h for h in hits if h.as_posix().lower().endswith(tail) or
                     h.as_posix().lower().endswith(re.sub(r"\.tex$", "", tail) + ".tex." + h.name.rsplit(".", 1)[-1])]
            return (exact or hits)[0]
    for d in texture_dirs:
        found = _versions_of(_listing(Path(d)), stem, kind)
        if found:
            return found[-1]
    return None


def load_model(path: os.PathLike | str, texture_dirs: tuple[str, ...] = ()) -> ModelAsset:
    path = Path(path)
    data = read_bytes(path)
    if not meshfmt.is_mesh(data):
        raise FormatError(f"{path.name} is not an RE Engine mesh")
    version = version_of(path.name)
    streaming = streaming_partner(path)
    streaming_data = read_bytes(streaming) if streaming is not None else None
    mesh = meshfmt.read_mesh(data, version, streaming_data)
    asset = ModelAsset(file_stem(path.name), path, mesh, streaming_path=streaming)
    best = mesh.best_lod()
    if best is None:
        raise FormatError(f"{path.name}: none of the model's levels of detail is readable without its streamed file")
    if mesh.streamed_count and not mesh.streamed_found:
        where = "a lower level of detail is used" if best > 0 or mesh.complete(best) else \
            "the parts kept there are left out"
        asset.notes.append(f"The most detailed level of {asset.name} is streamed from a second file under "
                           f"natives/<platform>/streaming/ that was not found; {where}.")
    mdf_path = find_mdf(path, mesh.material_names)
    if mdf_path is None:
        asset.notes.append(f"No materials file (.mdf2) was found for {asset.name}, so its textures are unknown.")
    else:
        try:
            asset.mdf = mdffmt.read_mdf(read_bytes(mdf_path))
            asset.mdf_path = mdf_path
        except FormatError as exc:
            asset.notes.append(f"The materials file {mdf_path.name} could not be read: {exc}")
    if asset.mdf is not None:
        for material in asset.mdf.materials:
            if material.name not in mesh.material_names:
                continue
            for binding in material.textures:
                stem = file_stem(binding.path)
                if not stem or stem.lower() in asset.textures:
                    continue
                found = find_texture(path, binding.path, texture_dirs)
                if found is None:
                    if binding.path not in asset.missing:
                        asset.missing.append(binding.path)
                    continue
                asset.textures[stem.lower()] = TexRef(stem, found, str(found))
        if asset.missing:
            asset.notes.append(f"{len(asset.missing)} texture{'s' if len(asset.missing) != 1 else ''} named by the "
                               f"materials {'were' if len(asset.missing) != 1 else 'was'} not found: "
                               f"{_join([m.rsplit('/', 1)[-1] for m in asset.missing], 3)}.")
    return asset


def related_files(path: Path, textures: bool = True) -> list[tuple[Path, str]]:
    """The files a model needs when copied elsewhere: the mesh, its streamed half, its materials and its
    textures (both the small copy and the streamed full-size one)."""
    path = Path(path)
    out: list[tuple[Path, str]] = [(path, "model")]
    streaming = streaming_partner(path)
    if streaming is not None:
        out.append((streaming, "model"))
    if not textures:
        return out
    try:
        asset = load_model(path)
    except FormatError:
        return out
    if asset.mdf_path is not None:
        out.append((asset.mdf_path, "texture"))
    for ref in asset.textures.values():
        out.append((ref.path, "texture"))
        small = _small_copy(ref.path)
        if small is not None:
            out.append((small, "texture"))
    return out


def _small_copy(path: Path) -> Path | None:
    """For a texture in ``streaming/``, the small copy outside it (the game needs both)."""
    nr = natives_root(path)
    if nr is None or not is_streaming(path.as_posix()):
        return None
    base, platform = nr
    tree = base / "natives" / platform / "streaming"
    try:
        rel = path.relative_to(tree)
    except ValueError:
        return None
    candidate = base / "natives" / platform / rel
    return candidate if exists(candidate) else None


__all__ = ["FileIndex", "ModelAsset", "ModelPath", "TexRef", "build_index", "exists", "file_stem", "find_mdf",
           "find_texture", "forget", "index_for", "is_streaming", "load_model", "locate", "natives_root", "read_bytes",
           "related_files", "size_of", "split_name", "streaming_partner", "walk"]
