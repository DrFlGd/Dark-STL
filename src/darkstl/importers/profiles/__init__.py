"""Game profiles: what differs from one game to the next, as a JSON file per game.

A profile names the game, the engine backend that reads it (``unreal4``, ``unreal5``, ``unreal3``,
``fromsoft``, ``reengine``, ``redengine``, ``renderware``...) and that game's settings: the files and
folder names that say a folder is the game, the engine release, AES keys and type mappings, how the
folders and names of its files sort its models into kinds and groups, texture name suffixes, which
way its normal maps' green points, and what Collect copies besides the models. The format is
described in ``docs/profiles.md``.

Profiles ship in this folder (``*.json``). A file of the same name in the settings folder's
``profiles/`` folder replaces a shipped one, and any other file there adds a game, so a game can be
added or tuned without a new release of Dark STL. The Unreal 4/5 games are read entirely from their
profiles; the other engines keep their per-game tables in code and take from a profile only what it
overrides (see :func:`overrides`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FORMAT = 1
SHIPPED = Path(__file__).parent
FOLDER = "profiles"  # in the settings folder
UNREAL_ENGINES = ("unreal4", "unreal5")
# the importer that reads each engine's profiles when a profile doesn't name one
DEFAULT_IMPORTER = {"unreal4": "unreal", "unreal5": "unreal", "unreal3": "borderlands", "fromsoft": "fromsoft",
                    "reengine": "reengine", "redengine": "redengine", "renderware": "renderware"}


@dataclass
class Profile:
    """One game's profile, as read from its JSON file."""

    name: str
    engine: str
    data: dict = field(default_factory=dict, repr=False)
    source: Path | None = None
    shipped: bool = True  # from Dark STL itself, not the settings folder
    defaults: bool = False  # not a game: the settings every game of its engine starts from

    @property
    def importer(self) -> str:
        return str(self.data.get("importer") or DEFAULT_IMPORTER.get(self.engine, self.engine))

    def section(self, key: str) -> dict:
        value = self.data.get(key)
        return value if isinstance(value, dict) else {}

    @property
    def recognise(self) -> dict:
        return self.section("recognise")

    @property
    def recognise_files(self) -> list[str]:
        return [str(f) for f in self.recognise.get("files") or [] if str(f).strip()]

    @property
    def folder_names(self) -> list[str]:
        return [str(f).lower() for f in self.recognise.get("folder_names") or [] if str(f).strip()]

    @property
    def folder_pattern(self) -> re.Pattern | None:
        text = self.recognise.get("folder_pattern")
        if not text:
            return None
        try:
            return re.compile(str(text), re.I)
        except re.error:
            log.warning("Profile %s: folder_pattern %r isn't a regular expression", self.name, text)
            return None

    @property
    def normal_green(self) -> str | None:
        green = str(self.section("textures").get("normal_green") or "").lower()
        return green if green in ("directx", "opengl") else None


def settings_folder() -> Path | None:
    """``<settings>/profiles``, where the user's profiles go (None without the app's settings)."""
    try:
        from ...app.config import config_dir
    except Exception:  # noqa: BLE001 - no app settings (tests, the engine on its own)
        return None
    try:
        return Path(config_dir()) / FOLDER
    except Exception:  # noqa: BLE001
        return None


_lock = threading.Lock()
_cache: dict[str, Any] = {"stamp": None, "profiles": ()}
_problems: list[str] = []


def _listing(folder: Path | None) -> list[Path]:
    if folder is None or not folder.is_dir():
        return []
    try:
        return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".json" and p.is_file())
    except OSError:
        return []


def _stamp(user: list[Path]) -> tuple:
    out = []
    for p in user:
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((str(p), st.st_size, int(st.st_mtime_ns)))
    return tuple(out)


def _read(path: Path, shipped: bool) -> Profile | None:
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        _problems.append(f"{path.name}: {exc}")
        return None
    if not isinstance(data, dict) or not data.get("name") or not data.get("engine"):
        _problems.append(f"{path.name}: a profile needs a name and an engine")
        return None
    if int(data.get("format") or FORMAT) > FORMAT:
        _problems.append(f"{path.name}: made for a newer Dark STL (format {data.get('format')})")
        return None
    return Profile(str(data["name"]).strip(), str(data["engine"]).strip().lower(), data, path, shipped,
                   bool(data.get("defaults")))


def all_profiles() -> tuple[Profile, ...]:
    """Every profile: the shipped ones, each replaced by the settings folder's file of the same name, and the
    settings folder's other files. Read again whenever the settings folder's files change."""
    user = _listing(settings_folder())
    stamp = _stamp(user)
    with _lock:
        if _cache["stamp"] == stamp and _cache["profiles"]:
            return _cache["profiles"]
    files: dict[str, tuple[Path, bool]] = {p.name.lower(): (p, True) for p in _listing(SHIPPED)}
    for p in user:
        files[p.name.lower()] = (p, False)
    problems: list[str] = []
    _problems.clear()
    out: list[Profile] = []
    seen: dict[tuple[str, bool], int] = {}
    for path, shipped in files.values():
        prof = _read(path, shipped)
        if prof is None:
            continue
        key = (prof.name.lower(), prof.defaults)
        if key in seen:  # the same game named by two files: the user's wins, else the later file
            if out[seen[key]].shipped or not shipped:
                out[seen[key]] = prof
            continue
        seen[key] = len(out)
        out.append(prof)
    out = _resolve_extends(out)
    problems = list(_problems)
    with _lock:
        _cache["stamp"] = stamp
        _cache["profiles"] = tuple(out)
        _cache["problems"] = problems
    return tuple(out)


def merge(base: dict, child: dict) -> dict:
    """``child``'s settings over ``base``'s: a section (a JSON object) both have is merged key by key, one
    level down; anything else ``child`` has replaces ``base``'s."""
    out = dict(base)
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def _resolve_extends(found: list[Profile]) -> list[Profile]:
    """Each profile that ``"extends"`` another game's profile, with that profile's settings under its own."""
    by_name = {p.name.lower(): p for p in found if not p.defaults}

    def resolved(p: Profile, depth: int = 0) -> dict:
        base_name = str(p.data.get("extends") or "").strip().lower()
        base = by_name.get(base_name)
        if not base_name or base is None or base is p or depth > 8:
            if base_name and base is None:
                _problems.append(f"{p.source.name if p.source else p.name}: there is no profile called "
                                 f"{p.data.get('extends')!r} to extend")
            return p.data
        data = merge(resolved(base, depth + 1), p.data)
        data.pop("defaults", None)
        if base.importer != p.importer and "importer" not in p.data:
            data["importer"] = base.importer
        return data

    out = []
    for p in found:
        if p.data.get("extends"):
            p = Profile(p.name, p.engine, resolved(p), p.source, p.shipped, p.defaults)
        out.append(p)
    return out


def problems() -> list[str]:
    """Profile files that couldn't be read, and why (for Settings)."""
    all_profiles()
    with _lock:
        return list(_cache.get("problems") or [])


def forget() -> None:
    """Read the profiles again next time (after a file was written)."""
    with _lock:
        _cache["stamp"] = None


def profiles(engine: str | tuple[str, ...] | None = None, importer: str | None = None) -> list[Profile]:
    """The games' profiles (not the defaults), of an engine (or engines) and read by an importer."""
    engines = (engine,) if isinstance(engine, str) else engine
    return [p for p in all_profiles() if not p.defaults and (engines is None or p.engine in engines)
            and (importer is None or p.importer == importer)]


def get(name: str | None, engine: str | tuple[str, ...] | None = None) -> Profile | None:
    """The profile of the game called ``name``."""
    if not name:
        return None
    low = name.strip().lower()
    return next((p for p in profiles(engine) if p.name.lower() == low), None)


def defaults(engine: str) -> dict:
    """The settings every game of ``engine`` starts from (a profile file marked ``"defaults": true``)."""
    for p in all_profiles():
        if p.defaults and p.engine == engine:
            return p.data
    return {}


def overrides(engine: str, game: str) -> dict:
    """What the settings folder's profile for ``game`` (of an engine whose games are tables in code) says;
    empty when there is none. The engine's importer applies the keys it knows."""
    p = get(game, engine)
    return p.data if p is not None and not p.shipped else {}


def find_ci(root: Path, rel: str) -> Path | None:
    """``rel`` (a path with / between folders) below ``root``, matched without regard to case; None if absent."""
    cur = Path(root)
    for part in rel.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        nxt = cur / part
        if nxt.exists():
            cur = nxt
            continue
        try:
            names = {n.lower(): n for n in os.listdir(cur)}
        except OSError:
            return None
        found = names.get(part.lower())
        if found is None:
            return None
        cur = cur / found
    return cur


def has_marker(folder: Path, profile: Profile) -> bool:
    """Whether ``folder`` holds one of the files (or folders) the profile recognises its game by."""
    return any(find_ci(folder, f) is not None for f in profile.recognise_files)


def slug(name: str) -> str:
    """A file name for a profile of the game ``name``."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "game"


def write_user_profile(data: dict) -> Path:
    """Write a profile into the settings folder (a new game, or one tuned), and read the profiles again."""
    folder = settings_folder()
    if folder is None:
        raise OSError("Dark STL's settings folder isn't known")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{slug(str(data['name']))}.json"
    n = 2
    while target.is_file():
        try:
            same = json.loads(target.read_text("utf-8-sig")).get("name") == data["name"]
        except (OSError, ValueError, AttributeError):
            same = False
        if same:
            break
        target = folder / f"{slug(str(data['name']))}-{n}.json"
        n += 1
    body = {"format": FORMAT, **data}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", "utf-8")
    tmp.replace(target)
    forget()
    return target


__all__ = ["DEFAULT_IMPORTER", "FORMAT", "Profile", "UNREAL_ENGINES", "all_profiles", "defaults", "find_ci", "forget",
           "get", "has_marker", "merge", "overrides", "problems", "profiles", "settings_folder", "slug",
           "write_user_profile"]
