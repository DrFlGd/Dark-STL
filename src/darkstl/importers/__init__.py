"""Importers turn each game's files into the common :class:`~darkstl.model.SourceModel`.

Everything after import (remeshing, displacement, preview, export) is shared,
so adding a game means adding an :class:`Importer` here.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import FormatError
from ..model import SourceModel
from .base import Importer
from .fromsoft import FromSoftwareImporter
from .wavefront import WavefrontImporter

IMPORTERS: list[Importer] = [FromSoftwareImporter(), WavefrontImporter()]


def register(importer: Importer) -> None:
    IMPORTERS.insert(0, importer)


def importer_for(path: str | os.PathLike) -> Importer:
    path = Path(path)
    for imp in IMPORTERS:
        if imp.can_open(path):
            return imp
    raise FormatError(f"No importer can open {path.name}")


def open_model(path: str | os.PathLike, variant: str | None = None,
               texture_dirs: tuple[str, ...] = ()) -> SourceModel:
    path = Path(path)
    return importer_for(path).load(path, variant, tuple(texture_dirs))


def model_variants(path: str | os.PathLike) -> list[str]:
    return importer_for(path).variants(Path(path))


def open_file_filter() -> str:
    filters = [imp.file_filter for imp in IMPORTERS if imp.file_filter]
    return ";;".join(filters + ["All files (*)"])


__all__ = ["IMPORTERS", "Importer", "importer_for", "model_variants", "open_file_filter", "open_model", "register"]
