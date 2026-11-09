"""Each game's library index: a line per model with what the library's search, filters and list columns show.

``<cache>/library/index-<game>.json`` (beside the folders' snapshots) holds, per model: its id, label, its name
in the game, kind and group, file, whether it's a copy collected into the app, and once known its size (mm,
as exported: X, Y, Z up), vertex and triangle counts, parts, textures, and whether it has animations.

Names and places come from the library each time it's written (at every scan, so it follows the folders like
their snapshots). The sizes and counts are learnt as the app goes, each kept until the model's folder is
rescanned: from a quick look at the file where the importer can do that without opening the model
(:meth:`Importer.stats`: FromSoftware's FLVER files, model packages), else when its preview is drawn or it's
opened. A background pass (see the service) fills in those not known yet, a model at a time, holding back
while the user is waiting on something.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

FORMAT = "darkstl-library-index"
VERSION = 1
STAT_KEYS = ("size_mm", "vertices", "triangles", "parts", "textures", "animated")


def model_stats(model, mesh) -> dict:
    """The stats of an opened model (a SourceModel) from the mesh built from it: counts from its parts (as the
    game's files give them), size from the mesh."""
    import numpy as np

    if mesh is not None and mesh.n_vertices:
        lo, hi = mesh.bounds()
        size = [round(float(v), 1) for v in (np.asarray(hi) - np.asarray(lo)) * 1000]
    else:
        size = [0.0, 0.0, 0.0]
    parts = list(getattr(model, "parts", None) or [])
    vertices = sum(int(p.vertices) for p in parts) if parts else int(mesh.n_vertices if mesh is not None else 0)
    triangles = sum(int(p.triangles) for p in parts) if parts else int(mesh.n_faces if mesh is not None else 0)
    return {"size_mm": size, "vertices": vertices, "triangles": triangles, "parts": len(parts) or 1,
            "textures": len(model.textures or {}), "animated": model.animations is not None}


def clean_stats(stats: dict | None) -> dict | None:
    """``stats`` with only the known fields, of the right types (None if nothing is left)."""
    if not isinstance(stats, dict):
        return None
    out = {}
    size = stats.get("size_mm")
    if isinstance(size, (list, tuple)) and len(size) == 3:
        try:
            out["size_mm"] = [round(float(v), 1) for v in size]
        except (TypeError, ValueError):
            pass
    for k in ("vertices", "triangles", "parts", "textures"):
        if stats.get(k) is not None:
            try:
                out[k] = int(stats[k])
            except (TypeError, ValueError):
                pass
    if stats.get("animated") is not None:
        out["animated"] = bool(stats["animated"])
    if stats.get("from"):
        out["from"] = str(stats["from"])
    return out or None


def longest(stats: dict | None) -> float | None:
    """The model's longest side in mm, if known."""
    size = (stats or {}).get("size_mm")
    return max(size) if size else None


def _file_name(game: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", game).strip("-")[:40] or "game"
    return f"index-{safe}-{hashlib.sha1(game.encode('utf-8')).hexdigest()[:8]}.json"


class LibraryIndex:
    """The games' index files, and the stats they keep, in memory by game and model id."""

    def __init__(self, folder: str | os.PathLike):
        self.folder = Path(folder)
        self._stats: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()

    def file(self, game: str) -> Path:
        return self.folder / _file_name(game)

    def _game(self, game: str) -> dict[str, dict]:
        with self._lock:
            known = self._stats.get(game)
            if known is None:
                known = {}
                try:
                    data = json.loads(self.file(game).read_text("utf-8"))
                    if isinstance(data, dict) and data.get("format") == FORMAT and int(data.get("version", 0)) == VERSION:
                        for row in data.get("models") or []:
                            st = clean_stats(row)
                            if st and row.get("id"):
                                known[str(row["id"])] = st
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                self._stats[game] = known
            return known

    def stats(self, game: str, entry_id: str) -> dict | None:
        return self._game(game).get(entry_id)

    def all_stats(self, game: str) -> dict[str, dict]:
        return self._game(game)

    def put(self, game: str, entry_id: str, stats: dict | None) -> bool:
        """Keep a model's stats; True when they changed."""
        st = clean_stats(stats)
        if st is None:
            return False
        with self._lock:
            known = self._game(game)
            if known.get(entry_id) == st:
                return False
            known[entry_id] = st
            return True

    def forget(self, game: str, ids) -> None:
        with self._lock:
            known = self._game(game)
            for i in ids:
                known.pop(i, None)

    def write(self, game: str, rows: list[dict]) -> None:
        """Write the game's index: ``rows`` (the library's fields of each model), with the stats known."""
        known = self._game(game)
        out = []
        for row in rows:
            st = known.get(row.get("id"))
            out.append({**row, **st} if st else dict(row))
        data = {"format": FORMAT, "version": VERSION, "game": game, "models": out}
        target = self.file(game)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{threading.get_ident()}.part")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
            os.replace(tmp, target)
        except OSError:
            pass  # only the stats learnt are lost (learnt again)


def read_index(path: str | os.PathLike) -> dict:
    """An index file as written (for tools and tests)."""
    return json.loads(Path(path).read_text("utf-8"))


__all__ = ["LibraryIndex", "STAT_KEYS", "clean_stats", "longest", "model_stats", "read_index"]
