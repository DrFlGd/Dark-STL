"""The names an Unreal 4/5 game's string tables give its models.

A ``StringTable`` asset holds a namespace and its entries, key to text (after the object's properties:
the namespace as a string, then a count of key and text strings). Games keep item names there under keys
made from the item (``WPN_Blaster_Name``); a mesh made for that item is named like it (``SK_WPN_Blaster``).
A model is named when its name, without its kind's prefix and its level of detail, is the key without its
"name" ending, and no other text claims it. Which assets are string tables and which keys hold names come
from the game's profile (``names`` section, see docs/profiles.md).

What's read is kept in the importer's cache until the game's paks change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from ...errors import FormatError
from ...model import CatalogEntry
from .profile import UnrealProfile

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
_MESH_PREFIX = re.compile(r"^(sk|skm|skel|sm|stm|mesh|m|bp|wpn_mesh)_", re.I)
_MESH_SUFFIX = re.compile(r"(_(lod\d*|mesh|skel|skeletal|static|1p|3p|fp|tp|low|high|hi|lo|geo|base|main|body))+$",
                          re.I)


def core(name: str) -> str:
    """A model's or key's name reduced to what both share: no kind prefix or level of detail, letters and digits."""
    name = _MESH_PREFIX.sub("", name.strip())
    name = _MESH_SUFFIX.sub("", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def read_string_table(pkg) -> dict[str, str]:
    """{key: text} of a package's StringTable exports."""
    from ..borderlands.ue4.mesh import object_props

    out: dict[str, str] = {}
    for e in pkg.exports:
        if e.class_name != "StringTable":
            continue
        r = pkg.export_data(e)
        object_props(pkg, r, e)
        r.fstring()  # the namespace
        count = r.i32()
        if not 0 <= count <= 1_000_000:
            raise FormatError(f"{pkg.name}: a string table of {count} entries")
        for _ in range(count):
            key = r.fstring()
            out[key] = r.fstring()
    return out


def _fingerprint(gf) -> str:
    from .catalog import _fingerprint as paks

    return paks(gf) if (gf.paks or gf.zen is not None) else ""


def _cache_file(cache: Path | None, root: Path) -> Path | None:
    if cache is None:
        return None
    tag = hashlib.sha1(os.path.normcase(str(root)).encode()).hexdigest()[:16]
    return Path(cache) / "names" / f"tables-{tag}.json"


def tables(root: Path, prof: UnrealProfile, cache: Path | None = None, fresh: bool = False) -> dict[str, str]:
    """{key: text} of every string table the profile's rules pick in the game's files (kept in ``cache``)."""
    from ..borderlands.ue4 import vfs

    if prof.names_tables is None:
        return {}
    gf = vfs.game_files(Path(root), prof.name)
    virtuals = gf.assets() + (gf.zen.assets() if gf.zen is not None else [])
    picked = [v for v in virtuals if v.lower().endswith(".uasset") and prof.names_tables.search(v.lower())]
    picked = sorted(set(picked))[:prof.names_limit]
    if not picked:
        return {}
    signature = [FORMAT_VERSION, _fingerprint(gf), len(picked)]
    file = _cache_file(cache, gf.root)
    if file is not None and not fresh and signature[1]:
        try:
            data = json.loads(file.read_text("utf-8"))
            if data.get("signature") == signature:
                return dict(data.get("entries") or {})
        except (OSError, ValueError, AttributeError):
            pass
    out: dict[str, str] = {}
    for v in picked:
        try:
            out.update(read_string_table(gf.open_package(v)))
        except (FormatError, OSError, ValueError, IndexError, KeyError, EOFError) as exc:
            log.debug("Couldn't read the string table %s: %s", v, exc)
    if file is not None and signature[1]:
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            tmp = file.with_suffix(".part")
            tmp.write_text(json.dumps({"signature": signature, "entries": out}, ensure_ascii=False), "utf-8")
            os.replace(tmp, file)
        except OSError:
            pass
    return out


def name_table(entries: dict[str, str], prof: UnrealProfile) -> dict[str, str]:
    """{core of a model's name: the text naming it}, from the keys that hold names (a core two texts claim is
    left out)."""
    found: dict[str, set[str]] = {}
    for key, text in entries.items():
        text = (text or "").strip()
        if not text or len(text) > 64 or "\n" in text or len(text.split()) > 6 or "{" in text:
            continue
        if prof.names_skip is not None and prof.names_skip.search(key):
            continue
        m = prof.names_keys.search(key) if prof.names_keys is not None else None
        if m is None:
            continue
        c = core(key[:m.start()])
        if len(c) >= 3:
            found.setdefault(c, set()).add(text)
    return {c: next(iter(t)) for c, t in found.items() if len(t) == 1}


def names(root: Path, folder: Path, prof: UnrealProfile, entries: list[CatalogEntry], cache: Path | None = None,
          fresh: bool = False) -> dict[str, str]:
    """{overlay key (the model's path in ``folder``): its name in the game} for the models a string table names."""
    from ...overlay import model_key

    try:
        table = name_table(tables(root, prof, cache, fresh), prof)
    except (FormatError, OSError) as exc:
        log.debug("No names from the string tables of %s: %s", root, exc)
        return {}
    if not table:
        return {}
    out = {}
    for e in entries:
        if e.config is not None:
            continue
        name = table.get(core(e.stem))
        if not name:
            continue
        try:
            rel = Path(e.path).relative_to(folder).as_posix()
        except ValueError:
            continue
        out[model_key(rel)] = name
    return out


__all__ = ["core", "name_table", "names", "read_string_table", "tables"]
