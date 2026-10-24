"""Opening game files: loose or packed models, and the textures that go with them.

A model can be given as:

* a packed container (``wp_a_0300.partsbnd.dcx``, ``c2010.chrbnd.dcx``, ...)
* a loose ``.flver`` (``.flver.dcx``), e.g. inside a folder unpacked by
  WitchyBND/Yabber (``wp_a_0300-partsbnd-dcx/``)

Textures are collected from the container itself, from sibling texture
archives (``c2010.texbnd.dcx``, ``c2010_h.texbnd.dcx``, ``*.chrtpfbdt``), from
loose ``.tpf``/``.dds`` files next to a loose FLVER, and from any extra
texture folders the user configured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from ...textures import dds
from .formats import binder as bnd
from .formats import dcx, flver, oodle, tpf
from .formats.binary import FormatError

MODEL_EXTENSIONS = (".partsbnd", ".chrbnd", ".objbnd", ".geombnd", ".mapbnd", ".flver", ".flv")
TEXTURE_CONTAINER_EXTENSIONS = (".texbnd", ".tpf", ".tpfbhd", ".chrtpfbhd")


def strip_dcx(name: str) -> str:
    return name[:-4] if name.lower().endswith(".dcx") else name


def file_stem(name: str) -> str:
    """'wp_a_0300.partsbnd.dcx' -> 'wp_a_0300'; 'wp_a_0300-partsbnd-dcx' -> 'wp_a_0300'."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = strip_dcx(base)
    base = re.sub(r"-(partsbnd|chrbnd|objbnd|geombnd|mapbnd|texbnd|tpf|flver)(-dcx)?$", "", base, flags=re.I)
    return base.split(".", 1)[0]


def read_bytes(path: Path) -> bytes:
    raw = Path(path).read_bytes()
    if dcx.is_dcx(raw) and b"KRAK" in raw[:0x40]:
        oodle.add_hint_path(Path(path).parent)  # the game's DLL is usually a few folders up
    return dcx.maybe_decompress(raw)


@dataclass(eq=False)
class TextureRef:
    name: str
    source: str
    _loader: Callable[[], bytes] = field(repr=False)  # returns DDS bytes

    def dds_bytes(self) -> bytes:
        return self._loader()

    def info(self) -> dds.DDSInfo:
        return dds.parse_dds(self.dds_bytes())

    def decode(self, mip: int = 0) -> np.ndarray:
        return _decode_cached(self, mip)


@lru_cache(maxsize=48)
def _decode_cached(ref: TextureRef, mip: int) -> np.ndarray:
    return dds.decode_dds(ref.dds_bytes(), mip)


@dataclass
class ModelAsset:
    name: str
    path: Path
    inner_name: str | None
    flver: flver.Flver
    textures: dict[str, TextureRef] = field(default_factory=dict)  # lower-case name -> ref
    notes: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)  # every model in the file, when it holds several
    binder: bnd.Binder | None = field(default=None, repr=False)  # the archive the model came from

    def texture(self, name: str) -> TextureRef | None:
        return self.textures.get(name.lower())


# ---------------------------------------------------------------------------
# Texture collection helpers


def _add_tpf(textures: dict[str, TextureRef], data: bytes, source: str) -> None:
    try:
        pack = tpf.read_tpf(data)
    except FormatError:
        return
    for tex in pack.textures:
        key = tex.name.lower()
        if key not in textures:
            textures[key] = TextureRef(tex.name, source, tex.to_dds)


def _add_binder_textures(textures: dict[str, TextureRef], binder: bnd.Binder, source: str,
                         sibling_dir: Path | None = None) -> None:
    for f in binder.files:
        low = f.basename.lower()
        if low.endswith((".tpf", ".tpf.dcx")):
            _add_tpf(textures, f.data, f"{source} > {f.basename}")
        elif low.endswith((".chrtpfbhd", ".tpfbhd")) and sibling_dir is not None:
            bdt_name = re.sub(r"bhd$", "bdt", f.basename, flags=re.I)
            bdt_path = _find_file(sibling_dir, bdt_name)
            if bdt_path is not None:
                try:
                    split = bnd.read_binder(f.data, lambda p=bdt_path: p.read_bytes())
                except FormatError:
                    continue
                _add_binder_textures(textures, split, f"{source} > {f.basename}")
        elif low.endswith((".texbnd", ".texbnd.dcx")):
            try:
                _add_binder_textures(textures, bnd.read_binder(f.data), f"{source} > {f.basename}")
            except FormatError:
                continue


def _dir_listing(directory: Path) -> dict[str, Path]:
    try:
        return {p.name.lower(): p for p in directory.iterdir()}
    except OSError:
        return {}


def _find_file(directory: Path, name: str) -> Path | None:
    return _dir_listing(directory).get(name.lower())


def add_texture_file(textures: dict[str, TextureRef], path: Path) -> None:
    """Add textures from a .tpf/.texbnd/.dds (optionally .dcx) file or split .tpfbhd/.tpfbdt pair."""
    low = path.name.lower()
    try:
        if low.endswith(".dds"):
            stem = path.name[:-4]
            textures.setdefault(stem.lower(), TextureRef(stem, str(path), lambda p=path: p.read_bytes()))
            return
        if low.endswith((".tpfbhd", ".chrtpfbhd")):
            bdt = _find_file(path.parent, re.sub(r"bhd$", "bdt", path.name, flags=re.I))
            if bdt is not None:
                _add_binder_textures(textures, bnd.read_binder(path.read_bytes(), lambda p=bdt: p.read_bytes()),
                                     str(path))
            return
        data = read_bytes(path)
        if tpf.is_tpf(data):
            _add_tpf(textures, data, str(path))
        elif bnd.is_binder(data):
            _add_binder_textures(textures, bnd.read_binder(data), str(path), path.parent)
    except (FormatError, OSError):
        return


def _scan_loose_texture_dir(textures: dict[str, TextureRef], directory: Path, depth: int = 1) -> None:
    for entry in sorted(_dir_listing(directory).values()):
        low = entry.name.lower()
        if entry.is_dir():
            if depth > 0 and re.search(r"-(tpf|texbnd)(-dcx)?$", low):
                _scan_loose_texture_dir(textures, entry, depth - 1)
        elif low.endswith((".dds", ".tpf", ".tpf.dcx", ".texbnd", ".texbnd.dcx")):
            add_texture_file(textures, entry)


def _sibling_texture_files(directory: Path, stem: str) -> list[Path]:
    listing = _dir_listing(directory)
    s = stem.lower()
    names = [f"{s}_h.texbnd.dcx", f"{s}_h.texbnd", f"{s}.texbnd.dcx", f"{s}.texbnd", f"{s}.tpf.dcx", f"{s}.tpf",
             f"{s}_l.texbnd.dcx", f"{s}_l.texbnd"]
    found = [listing[n] for n in names if n in listing]
    for d in (f"{s}_h-texbnd-dcx", f"{s}-texbnd-dcx", f"{s}-tpf", f"{s}-tpf-dcx"):
        if d in listing and listing[d].is_dir():
            found.append(listing[d])
    return found


# ---------------------------------------------------------------------------
# Public API


def list_models_in_file(path: str | os.PathLike) -> list[str]:
    """Names of FLVER entries inside a container (or [basename] for a loose FLVER)."""
    path = Path(path)
    data = read_bytes(path)
    if flver.is_flver(data):
        return [path.name]
    if bnd.is_binder(data):
        return [f.name for f in bnd.read_binder(data).by_extension(".flver", ".flver.dcx", ".flv")]
    return []


def load_model(path: str | os.PathLike, inner_name: str | None = None,
               extra_texture_dirs: tuple[str | os.PathLike, ...] = ()) -> ModelAsset:
    path = Path(path)
    data = read_bytes(path)
    textures: dict[str, TextureRef] = {}
    notes: list[str] = []
    variants: list[str] = []
    stem = file_stem(path.name)
    binder = None

    if flver.is_flver(data):
        model = flver.read_flver(data)
        inner = None
        # Loose FLVER: look for textures alongside it (unpacked archive folder)
        _scan_loose_texture_dir(textures, path.parent)
    elif bnd.is_binder(data):
        binder = bnd.read_binder(data)
        candidates = binder.by_extension(".flver", ".flver.dcx", ".flv")
        if not candidates:
            raise FormatError(f"{path.name} contains no FLVER model")
        chosen = candidates[0]
        if inner_name is not None:
            match = [f for f in candidates if f.name == inner_name or f.basename.lower() == inner_name.lower()]
            if not match:
                raise FormatError(f"{inner_name} not found in {path.name}")
            chosen = match[0]
        if len(candidates) > 1:
            variants = [f.basename for f in candidates]
            if inner_name is None:
                notes.append(f"{path.name} holds {len(candidates)} models; showing {chosen.basename}")
        model = flver.read_flver(chosen.data)
        inner = chosen.name
        stem = file_stem(chosen.basename) or stem
        _add_binder_textures(textures, binder, path.name, path.parent)
    else:
        raise FormatError(f"{path.name} is not a FLVER model or model archive")

    for sib in _sibling_texture_files(path.parent, stem):
        if sib.is_dir():
            _scan_loose_texture_dir(textures, sib, depth=0)
        else:
            add_texture_file(textures, sib)
    if path.parent.name.lower().endswith(("-partsbnd-dcx", "-chrbnd-dcx", "-objbnd-dcx", "-geombnd-dcx",
                                          "-partsbnd", "-chrbnd", "-objbnd", "-geombnd")):
        for sib in _sibling_texture_files(path.parent.parent, stem):
            if sib.is_dir():
                _scan_loose_texture_dir(textures, sib, depth=0)
            else:
                add_texture_file(textures, sib)

    if extra_texture_dirs:
        wanted = {t.stem.lower() for m in model.materials for t in m.textures if t.path}
        missing = wanted - set(textures)
        if missing:
            for d in extra_texture_dirs:
                _add_from_texture_dir(textures, Path(d), missing)

    return ModelAsset(stem, path, inner, model, textures, notes, variants, binder)


def _add_from_texture_dir(textures: dict[str, TextureRef], directory: Path, wanted: set[str]) -> None:
    """Look for specific texture names in a user texture folder (loose DDS or per-texture TPFs)."""
    for root, _dirs, files in os.walk(directory):
        for name in files:
            low = name.lower()
            stem = strip_dcx(low)
            stem = stem[:-4] if stem.endswith((".tpf", ".dds")) else stem
            if stem in wanted and stem not in textures:
                add_texture_file(textures, Path(root) / name)
            elif low.endswith((".tpfbhd", ".texbnd", ".texbnd.dcx")) and wanted - set(textures):
                add_texture_file(textures, Path(root) / name)
        if not (wanted - set(textures)):
            return


# ---------------------------------------------------------------------------
# Animations

# a00_0000.hkx (Dark Souls), a000_003000.hkx (Bloodborne, Dark Souls III, Elden Ring)
ANIMATION_FILE = re.compile(r"^a\d{2,3}[_-]\d{3,6}\.hkx$", re.I)
SKELETON_FILE = re.compile(r"^skeleton\.hkx$", re.I)
ANIBND_FILE = re.compile(r"\.anibnd(\.dcx)?$", re.I)
ANIBND_FOLDER = re.compile(r"-anibnd(-dcx)?$", re.I)


@dataclass
class _HkxFile:
    name: str  # base name, e.g. a000_003000.hkx
    source: str  # the archive or folder it came from, for display
    read: Callable[[], bytes] = field(repr=False)


def _hkx_in_binder(binder: bnd.Binder, source: str) -> list[_HkxFile]:
    out = []
    for f in binder.files:
        low = f.basename.lower()
        if low.endswith((".hkx", ".compendium")):
            out.append(_HkxFile(f.basename, source, lambda f=f: f.data))
        elif ANIBND_FILE.search(low):  # an object's anibnd packed inside its objbnd
            try:
                out += _hkx_in_binder(bnd.read_binder(f.data), f"{source} > {f.basename}")
            except FormatError:
                continue
    return out


def _hkx_in_folder(folder: Path) -> list[_HkxFile]:
    out = []
    for entry in sorted(_dir_listing(folder).values()):
        low = entry.name.lower()
        if entry.is_file() and low.endswith((".hkx", ".hkx.dcx", ".compendium")):
            name = strip_dcx(entry.name)
            out.append(_HkxFile(name, str(folder), lambda p=entry: read_bytes(p)))
    return out


def animation_archives(path: Path, stem: str) -> list[Path]:
    """The anibnd files and unpacked anibnd folders that belong to the model at ``path``.

    ``c1120.anibnd.dcx`` beside ``c1120.chrbnd.dcx``, plus the split ``c1120_div00.anibnd.dcx`` /
    ``c0000_a0x.anibnd.dcx`` archives of the later games, and ``c1120-anibnd-dcx/`` folders.
    """
    found = []
    folders = [path.parent]
    if ANIBND_FOLDER.search(path.parent.name) or re.search(r"-(chrbnd|objbnd|partsbnd)(-dcx)?$", path.parent.name, re.I):
        folders.append(path.parent.parent)  # a loose FLVER inside an unpacked archive folder
    s = stem.lower()
    for folder in folders:
        for low, entry in sorted(_dir_listing(folder).items()):
            base = strip_dcx(low)
            if entry.is_dir():
                if ANIBND_FOLDER.search(low) and re.match(rf"^{re.escape(s)}(_div\d+|_a\d+)?-anibnd", low):
                    found.append(entry)
            elif base.endswith(".anibnd") and re.match(rf"^{re.escape(s)}(_div\d+|_a\d+)?\.anibnd$", base):
                found.append(entry)
    return found


def find_animations(path: Path, stem: str, binder: bnd.Binder | None):
    """An :class:`~darkstl.importers.fromsoft.animation.AnimationSet` for the model, or None when no
    animation files are there. Archives are read when the animations are first listed or used."""
    from ..fromsoft.animation import AnimationSet, Skeleton
    from .formats.havok import is_compendium, is_hkx, read_hkx

    path = Path(path)
    archives = animation_archives(path, stem)
    own: list[_HkxFile] = _hkx_in_binder(binder, path.name) if binder is not None else []
    has_anims = bool(archives) or any(ANIMATION_FILE.match(f.name) for f in own)
    if not has_anims:
        return None

    def load():
        hkx_files = list(own)
        for arc in archives:
            if arc.is_dir():
                hkx_files += _hkx_in_folder(arc)
            else:
                try:
                    hkx_files += _hkx_in_binder(bnd.read_binder(read_bytes(arc)), arc.name)
                except (FormatError, OSError):
                    continue
        compendium = next((f for f in hkx_files if f.name.lower().endswith(".compendium")), None)
        comp_bytes = compendium.read() if compendium else None
        skeleton = None
        for f in hkx_files:  # the anibnd's Skeleton.HKX first, else the model's own HKX (ragdoll + skeleton)
            if SKELETON_FILE.match(f.name):
                skeleton = Skeleton.from_hkx(read_hkx(f.read(), comp_bytes))
                break
        if skeleton is None:
            for f in hkx_files:
                if f.name.lower() == f"{stem.lower()}.hkx" or (not ANIMATION_FILE.match(f.name) and f.name.lower().endswith(".hkx")):
                    try:
                        data = f.read()
                        if is_hkx(data) and not is_compendium(data):
                            skeleton = Skeleton.from_hkx(read_hkx(data, comp_bytes))
                    except FormatError:
                        skeleton = None
                    if skeleton is not None:
                        break
        if skeleton is None:
            raise FormatError("No skeleton (Skeleton.HKX) was found with this model's animations")
        sources: dict[str, Callable[[], bytes]] = {}
        labels: dict[str, str] = {}
        for f in hkx_files:
            if ANIMATION_FILE.match(f.name) and f.name[:-4] not in sources:
                sources[f.name[:-4]] = f.read
                labels[f.name[:-4]] = f.source
        return skeleton, sources, labels, comp_bytes

    return AnimationSet(loader=load)
