"""Lists and opens model packages (see :mod:`darkstl.package`), which need nothing from the game."""

from __future__ import annotations

import os
from pathlib import Path

from ..collect import RelatedFile, read_manifest
from ..errors import FormatError
from ..model import CatalogEntry, SourceModel
from .. import package as pkg
from .base import Importer, ScanProgress


class PackageImporter(Importer):
    key = "package"
    name = "Dark STL model packages"
    file_filter = "Model packages (package.json)"
    record_reads = False

    def detect_game(self, folder: Path) -> str | None:
        data = read_manifest(Path(folder))
        return data.get("game") if data and data.get("packages") else None

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        out: list[CatalogEntry] = []
        seen = 0
        for dirpath, dirs, files in os.walk(folder):
            if Path(dirpath) == Path(folder):
                dirs[:] = [d for d in dirs if d != pkg.ASSETS]
            here = Path(dirpath)
            if here.name.endswith(pkg.SUFFIX):
                dirs[:] = []
                if pkg.INDEX not in files:
                    continue
                seen += 1
                if progress is not None and seen % 50 == 0:
                    progress(seen, here.name)
                try:
                    index = pkg.read_index(here / pkg.INDEX)
                except FormatError:
                    continue
                stem = str(index.get("stem") or here.name[:-len(pkg.SUFFIX)])
                models = index.get("models") or []
                stats = models[0].get("stats") if models and isinstance(models[0], dict) else None
                entry = CatalogEntry(game or str(index.get("game") or ""), str(index.get("category") or "Models"),
                                     str(index.get("group") or ""), stem, str(index.get("label") or stem),
                                     here / pkg.INDEX, self.key, is_lod=bool(index.get("is_lod")), packed=True,
                                     stats=stats if isinstance(stats, dict) else None)
                if index.get("game_name") and index.get("source"):
                    entry.__dict__["_package_name"] = (str(index["source"]), str(index["game_name"]))
                out.append(entry)
        return out

    def names(self, folder: Path, game: str, entries: list[CatalogEntry], fresh: bool = False) -> dict[str, str]:
        """The names the game gave the models when they were collected (kept in their packages), so they're
        named without the game: {source path in the game: name}."""
        return dict(e.__dict__["_package_name"] for e in entries if "_package_name" in e.__dict__)

    def can_open(self, path: Path) -> bool:
        return pkg.is_package_index(Path(path)) or Path(path).name.endswith(pkg.SUFFIX) and Path(path).is_dir()

    def stats(self, path: Path) -> dict | None:
        try:
            return pkg.package_stats(self._index(path))
        except FormatError:
            return None

    def variants(self, path: Path) -> list[str]:
        return pkg.listed_variants(pkg.read_index(self._index(path)).get("models") or [])

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        return pkg.load_package(self._index(path), variant, self.cache)

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        return [RelatedFile(self._index(path).parent, "model")]

    @staticmethod
    def _index(path: Path) -> Path:
        path = Path(path)
        return path / pkg.INDEX if path.is_dir() else path


__all__ = ["PackageImporter"]
