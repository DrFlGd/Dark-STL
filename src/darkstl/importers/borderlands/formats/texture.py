"""Unreal Engine 3 ``Texture2D``, with the mips that live in texture file caches (``.tfc``).

After the properties (size, pixel format, which cache holds the large mips)
comes the mip list; each mip is a bulk data block stored in the package
itself or in a ``.tfc`` file beside it, either raw or LZO/zlib compressed in
UE3's chunk format. The largest mip whose data can be found is decoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .... import access
from ....errors import FormatError
from ....textures import dds
from .props import Props, object_props
from .upk import COMPRESS_LZO, COMPRESS_ZLIB, Export, Package, read_compressed_chunk

FORMATS = {
    "PF_DXT1": "BC1", "PF_DXT3": "BC2", "PF_DXT5": "BC3", "PF_BC5": "BC5", "PF_ATI2": "BC5", "PF_BC4": "BC4",
    "PF_A8R8G8B8": "B8G8R8A8", "PF_G8": "R8", "PF_L8": "R8", "PF_A8": "A8", "PF_V8U8": "V8U8",
    "PF_FloatRGBA": "R16G16B16A16F", "PF_BC7": "BC7",
}


@dataclass
class Mip:
    width: int
    height: int
    flags: int
    size: int  # bytes on disk
    offset: int  # where: in the package stream, or in the cache file
    elements: int


@dataclass
class Texture2D:
    pkg: Package
    name: str
    path: str
    props: Props
    width: int
    height: int
    pixel_format: str
    codec: str
    cache_name: str | None
    mips: list[Mip] = field(default_factory=list)

    @property
    def is_normal_map(self) -> bool:
        cs = str(self.props.get("CompressionSettings", ""))
        lod = str(self.props.get("LODGroup", ""))
        return "Normal" in cs or "Normal" in lod

    def describe(self, tfc_dirs: list[Path] | None = None) -> str:
        out = f"{self.width} x {self.height}, {self.codec}, {len(self.mips)} mips"
        try:
            options = self.available(tfc_dirs)
        except OSError:
            return out
        if options:
            m = max((o[0] for o in options), key=lambda m: m.width * m.height)
            if m.width < self.width:
                missing = f"; {self.cache_name}.tfc with the larger ones wasn't found" if self.cache_name else ""
                out += f" (showing {m.width} x {m.height}{missing})"
        return out

    def available(self, tfc_dirs: list[Path] | None = None) -> list[tuple[Mip, Path | None]]:
        out = []
        cache = find_cache_file(self.pkg.path, self.cache_name, tfc_dirs) if self.cache_name else None
        if cache is not None:
            access.note_file(cache, "texture")
        for m in self.mips:
            if m.flags & 0x20 or m.size <= 0 or m.width <= 0:
                continue
            if m.flags & 0x01:
                if cache is not None:
                    out.append((m, cache))
            else:
                out.append((m, None))
        return out

    def decode(self, tfc_dirs: list[Path] | None = None) -> np.ndarray:
        options = self.available(tfc_dirs)
        if not options:
            where = f" (its large mips are in {self.cache_name}.tfc, which wasn't found)" if self.cache_name else ""
            raise FormatError(f"{self.name} has no mip data{where}")
        errors = []
        for m, cache in sorted(options, key=lambda o: -o[0].width * o[0].height):
            try:
                raw = self._mip_bytes(m, cache)
                return _decode(self.codec, raw, m.width, m.height)
            except FormatError as e:  # a broken large mip: fall back to a smaller one
                errors.append(str(e))
        raise FormatError(f"{self.name}: {errors[0]}")

    def dds_bytes(self, tfc_dirs: list[Path] | None = None) -> bytes:
        """The texture as a DDS file, still compressed as the game keeps it: the largest mip whose data is
        found (the one :meth:`decode` shows) and the smaller ones after it."""
        if self.codec == "V8U8":  # decoded into a normal map of its own
            raise FormatError(f"{self.name} ({self.codec}) can't be kept as a DDS file")
        options = sorted(self.available(tfc_dirs), key=lambda o: -o[0].width * o[0].height)
        for i, (m, cache) in enumerate(options):
            try:
                chain = [(m.width, m.height, self._mip_bytes(m, cache))]
            except (FormatError, OSError):
                continue
            for n, c in options[i + 1:]:
                try:
                    chain.append((n.width, n.height, self._mip_bytes(n, c)))
                except (FormatError, OSError):
                    break
            data = dds.wrap_mips(self.codec, chain)
            if data is None:
                break
            return data
        raise FormatError(f"{self.name} can't be kept as a DDS file")

    def _mip_bytes(self, m: Mip, cache: Path | None) -> bytes:
        if cache is not None:
            def read(offset: int, size: int) -> bytes:
                with open(cache, "rb") as f:
                    f.seek(offset)
                    return f.read(size)
        else:
            read = self.pkg.read
        method = COMPRESS_LZO if m.flags & 0x10 else COMPRESS_ZLIB if m.flags & 0x02 else 0
        if m.flags & 0x80:
            raise FormatError("LZX-compressed texture data (Xbox 360) isn't supported")
        if method:
            return read_compressed_chunk(read, m.offset, method)
        return read(m.offset, m.size)


def _decode(codec: str, raw: bytes, w: int, h: int) -> np.ndarray:
    if codec == "V8U8":
        a = np.frombuffer(raw[:w * h * 2], np.int8).reshape(h, w, 2).astype(np.float32) / 127.0
        z = np.sqrt(np.clip(1 - (a ** 2).sum(-1), 0, 1))
        rgb = np.dstack([a[..., 0], a[..., 1], z]) * 0.5 + 0.5
        return np.dstack([(rgb * 255).round().astype(np.uint8), np.full((h, w), 255, np.uint8)])
    if codec in dds.BLOCK_BYTES:
        need = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * dds.BLOCK_BYTES[codec]
        if len(raw) < need:
            raise FormatError(f"mip {w}x{h} is truncated")
    return dds.decode_surface(codec, raw, w, h)


def _guess_codec(size: int, w: int, h: int) -> str:
    texels = max(1, w * h)
    ratio = size / texels
    if abs(ratio - 0.5) < 0.05:
        return "BC1"
    if abs(ratio - 4) < 0.1:
        return "B8G8R8A8"
    if abs(ratio - 1) < 0.05:
        return "BC3"
    return "BC1"


def read_texture(pkg: Package, export: Export) -> Texture2D:
    r = pkg.export_data(export)
    s = pkg.summary
    v = s.version
    props = object_props(pkg, r)
    borderlands = s.licensee >= 46
    if not (borderlands and v >= 832):  # UTexture's source art (Borderlands 2 has none)
        flags, _count, size, _off = r.u32(), r.i32(), r.i32(), r.i32()
        if not flags & 0x01 and not flags & 0x20 and size > 0:
            r.skip(size)
    if v < 297:
        r.skip(12)
    if borderlands:
        r.skip(16)
    mips = []
    for _ in range(r.count(32)):
        flags, elements, size, offset = r.u32(), r.i32(), r.i32(), r.i32()
        if not flags & 0x01 and not flags & 0x20 and size > 0:
            offset = r.tell  # inline data follows the header
            r.skip(size)
        w, h = r.i32(), r.i32()
        mips.append(Mip(w, h, flags, size, offset, elements))
    fmt = str(props.get("Format", ""))
    width = int(props.get("SizeX", mips[0].width if mips else 0))
    height = int(props.get("SizeY", mips[0].height if mips else 0))
    codec = FORMATS.get(fmt)
    if codec is None:
        sized = [m for m in mips if m.elements > 0 and not m.flags & 0x20]
        codec = _guess_codec(sized[0].elements, sized[0].width, sized[0].height) if sized else "BC1"
    cache = props.get("TextureFileCacheName")
    cache = None if cache in (None, "None", "") else str(cache)
    if cache and access.active():  # where its large mips are, for collecting
        hit = find_cache_file(pkg.path, cache)
        if hit is not None:
            access.note_file(hit, "texture")
    return Texture2D(pkg, export.name, export.path, props, width, height, fmt or "?", codec, cache, mips)


_CACHE_INDEX: dict[str, dict[str, Path]] = {}


def find_cache_file(package_path: Path, name: str, extra_dirs: list[Path] | None = None) -> Path | None:
    """``<name>.tfc`` beside the package, in a parent folder, in the extra folders, or anywhere else in the
    package's game folder (a DLC's packages keep their large mips in the game's own caches)."""
    stem = Path(name).stem if name.lower().endswith((".tfc", ".xxx")) else name
    dirs = [package_path.parent, *list(package_path.parents)[1:4], *(extra_dirs or [])]
    for d in dirs:
        hit = _dir_index(d).get(stem.lower())
        if hit is not None:
            return hit
    root = _game_folder(package_path)
    if root is None:
        return None
    found = [p for p in _tree_index(root).get(stem.lower(), []) if p.is_file()]
    if not found:
        return None
    pkg = os.path.normcase(str(package_path.parent))
    return max(found, key=lambda p: len(os.path.commonprefix([pkg, os.path.normcase(str(p.parent))])))


def _dir_index(d: Path) -> dict[str, Path]:
    key = os.path.normcase(str(d))
    index = _CACHE_INDEX.get(key)
    if index is None:
        index = {}
        try:
            for entry in os.scandir(d):
                if entry.is_file() and entry.name.lower().endswith(".tfc"):
                    index[entry.name[:-4].lower()] = Path(entry.path)
        except OSError:
            pass
        _CACHE_INDEX[key] = index
    return index


_TREE_INDEX: dict[str, dict[str, list[Path]]] = {}
_GAME_MARKERS = {"binaries", "willowgame", "dlc"}


def _game_folder(package_path: Path) -> Path | None:
    """The highest folder above a package that looks like an Unreal 3 game's (holds Binaries, WillowGame or
    DLC), within a few levels."""
    best = None
    for d in list(Path(package_path).parents)[:7]:
        try:
            names = {e.name.lower() for e in os.scandir(d) if e.is_dir()}
        except OSError:
            continue
        if names & _GAME_MARKERS:
            best = d
    return best


def _tree_index(root: Path) -> dict[str, list[Path]]:
    """Every .tfc under a game folder, by name (looked for once per folder)."""
    key = os.path.normcase(str(root))
    index = _TREE_INDEX.get(key)
    if index is None:
        index = {}
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if n.lower().endswith(".tfc"):
                    index.setdefault(n[:-4].lower(), []).append(Path(dirpath) / n)
        _TREE_INDEX[key] = index
    return index


__all__ = ["FORMATS", "Mip", "Texture2D", "find_cache_file", "read_texture"]
