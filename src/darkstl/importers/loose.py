"""Pictures kept loose in a folder of models exported by other tools (FModel, UModel, Blender).

Such a folder holds the textures as picture files (PNG, TGA, DDS...) beside the models, often many that no
model names. The importers of those models (glTF, PSK, OBJ) note the folders they find models in; the first
to note a folder lists its pictures, so the texture picker can offer them and Collect can keep every one.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ..errors import FormatError
from ..model import Texture

PICTURES = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds")

_lock = threading.Lock()
_owner: dict[Path, str] = {}  # folder -> the key of the importer that lists its pictures


def note_folder(folder: Path, key: str) -> None:
    """``folder`` holds models of the importer ``key``."""
    with _lock:
        _owner.setdefault(Path(folder), key)


def owner(folder: Path) -> str | None:
    with _lock:
        return _owner.get(Path(folder))


def picture(path: Path) -> Texture:
    """A picture file as a texture (a DDS file kept as it is)."""
    from .wavefront import _describe_image, load_image

    path = Path(path)
    raw = (lambda p=path: p.read_bytes()) if path.suffix.lower() == ".dds" else None
    return Texture(path.stem, str(path), lambda p=path: load_image(p), lambda p=path: _describe_image(p), raw=raw)


def _skipped(name: str) -> bool:
    low = name.lower()
    return low.endswith((".darkpkg", " packages")) or low == "assets"


def pictures(folder: Path) -> list[tuple[str, str]]:
    """(name, path relative to ``folder``) of every picture under it, but in model packages."""
    folder = Path(folder)
    out = []
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not _skipped(d))
        here = Path(dirpath)
        for name in sorted(files):
            if name.lower().endswith(PICTURES):
                out.append((Path(name).stem, (here / name).relative_to(folder).as_posix()))
    return out


class LoosePictures:
    """For an importer of models exported by other tools: notes the folders it scans and lists their pictures."""

    key = ""

    def note(self, folder: Path, found: list) -> None:
        if found:
            note_folder(folder, self.key)

    def scan_state(self, folder: Path):
        return True if owner(folder) == self.key else None

    def restore_state(self, folder: Path, state) -> None:
        if state:
            note_folder(folder, self.key)

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        if owner(folder) != self.key:
            return []
        return pictures(folder)

    def open_texture(self, folder: Path, ref: str) -> Texture:
        path = Path(folder) / ref
        if not path.is_file():
            raise FormatError(f"{ref} isn't in {Path(folder).name} any more")
        return picture(path)


__all__ = ["PICTURES", "LoosePictures", "note_folder", "owner", "picture", "pictures"]
