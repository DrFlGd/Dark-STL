"""Which id Tech game a folder is, and its models sorted into kinds and groups by their resource paths."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from ...model import CatalogEntry
from ..base import ScanProgress
from .formats import resources as res
from .games import PROFILES, game_of_container_version
from .store import META, Store, open_store

MODEL_TYPES = ("baseModel", "model")
_GORE = ("gore", "wound", "gib", "_dmg", "damage", "destroyed", "chunk")
_TITLE_FIX = {"md6": "", "fp": "First person", "tp": "Third person"}


def detect_game(folder: Path) -> str | None:
    folder = Path(folder)
    if folder.name.lower() == "base":
        folder = folder.parent
    for p in PROFILES.values():
        if any((folder / exe).is_file() for exe in p.executables):
            return p.game
    base = folder / "base"
    if not base.is_dir():
        return None
    spec = base / "packagemapspec.json"
    probe = None
    if spec.is_file():
        probe = next((p for p in base.glob("*.resources") if p.is_file()), None)
    if probe is not None:
        try:
            with open(probe, "rb") as f:
                head = f.read(8)
            if head[:4] == res.MAGIC:
                return game_of_container_version(int.from_bytes(head[4:8], "little"))
        except OSError:
            return None
    meta = next(base.rglob("*" + META), None)  # a folder Collect wrote
    if meta is not None:
        try:
            return json.loads(meta.read_text("utf-8")).get("game")
        except (OSError, ValueError):
            return None
    return None


def _title(word: str) -> str:
    word = _TITLE_FIX.get(word, word)
    words = word.replace("_", " ").split()
    # map codes keep their capitals (e1m1_intro -> E1M1 Intro)
    return " ".join(w.upper() if re.fullmatch(r"[a-z]\d+[a-z]\d+", w) else w.capitalize() for w in words)


def classify(type_: str, name: str) -> tuple[str, str]:
    """(kind, group) of a model from its resource path."""
    parts = [p for p in name.lower().split("$", 1)[0].split("/") if p]
    folders = parts[:-1]
    gore = any(g in name.lower() for g in _GORE)

    def group(i: int, default: str) -> str:
        g = _title(folders[i]) if len(folders) > i else default
        return f"{g} (damage)" if gore and g else g or default

    top = folders[0] if folders else ""
    sub = folders[1] if len(folders) > 1 else ""
    if top == "md6" or type_ == "baseModel":
        if top != "md6":
            sub, folders = top, ["md6", *folders]
        if sub in ("monsters", "demons", "characters", "npc", "npcs", "humans", "bosses", "creatures"):
            return "Characters", group(2, _title(sub))
        if sub == "player":
            return "Characters", "Player"
        if sub in ("weapons", "weapon"):
            return "Weapons", group(2, "Weapons")
        return "Objects", group(1, "Models")
    if top in ("models", "model"):
        if sub in ("weapons", "weapon"):
            return "Weapons", group(2, "Weapons")
        if sub in ("pickups", "pickup", "items"):
            return "Objects", "Pickups"
        if sub in ("monsters", "characters", "npc", "demons"):
            return "Characters", group(2, _title(sub))
        if sub in ("architecture", "environment", "env", "arch", "terrain", "maps", "levels", "structures"):
            return "Map Pieces", group(2, _title(sub))
        return "Objects", group(2 if sub in ("props", "mapobjects", "objects") else 1, _title(sub) or "Models")
    if top in ("maps", "map", "art"):
        return "Map Pieces", group(1, "Maps")
    return "Objects", group(0, "Models")


def label_of(name: str) -> str:
    return PurePosixPath(name.split("$", 1)[0]).stem


def scan(folder: Path, game: str, progress: ScanProgress | None, cache: Path | None, importer: str) -> list[CatalogEntry]:
    from .store import find_root

    root = find_root(Path(folder)) or Path(folder)
    if progress:
        progress(-1, "Reading the game's resource tables")
    store = open_store(root, game, cache, fresh=True)
    out = []
    for type_ in MODEL_TYPES:
        for name in store.names(type_):
            if name.startswith("generated/") or "$" in name:
                continue  # map-baked copies and variants: the plain model is listed
            kind, grp = classify(type_, name)
            path = store.path_of(type_, name)
            try:
                path.relative_to(Path(folder))
            except ValueError:
                continue
            stem = label_of(name)
            out.append(CatalogEntry(store.game or game, kind, grp, stem, stem, path, importer,
                                    is_lod=bool(_lod(stem)), packed=not store.loc(type_, name).loose))
    return out


def _lod(stem: str) -> bool:
    from ...model import LOD_SUFFIX

    return bool(LOD_SUFFIX.search(stem))


def store_for(path: Path, cache: Path | None = None) -> tuple[Store, str, str]:
    """The store a model path belongs to, and the model's resource type and name."""
    from ...errors import FormatError
    from .store import split_path

    sp = split_path(Path(path))
    if sp is None:
        raise FormatError(f"{Path(path).name} isn't inside an id Tech game's base folder")
    root, _container, name = sp
    store = open_store(root, None, cache)
    for t in MODEL_TYPES + ("image", "anim", "skeleton", "rs_streamfile"):
        if store.has(t, name):
            return store, t, name
    raise FormatError(f"{name} isn't in the game's files")
