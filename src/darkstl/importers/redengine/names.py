"""The names Cyberpunk 2077 gives its weapons, from its on-screen text (``onscreens.json``).

The game's English text is ``base\\localization\\en-us\\onscreens\\onscreens.json`` (in
``lang_en_text.archive``, or extracted by WolvenKit, as the game's file or as WolvenKit's JSON): entries of
a key and the text shown, for a woman's and a man's voice. Item names are joined to items in the game's
TweakDB, which Dark STL doesn't read; a weapon's meshes are named after its model instead
(``base\\weapons\\handguns\\militech_lexington\\...\\w_handgun__militech_lexington__body.mesh``), and the model
is named when a text is exactly that model's name, with or without its maker (``Lexington``). The mesh's own
part ends its name (``Lexington (body)``).

What's read is kept in the importer's cache until the text's file changes.
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

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
ONSCREENS = "base\\localization\\en-us\\onscreens\\onscreens.json"
_NOT_A_NAME = re.compile(r"desc|flavo|lore|tooltip|hint|subtitle|message|quest|dialog", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def read_onscreens(data: bytes) -> list[tuple[str, str]]:
    """(key, text) of every entry of an onscreens file: the game's own (CR2W) or WolvenKit's JSON."""
    out: list[tuple[str, str]] = []
    head = data[:64].lstrip(b"\xef\xbb\xbf").lstrip()
    if head[:1] in (b"{", b"["):
        def walk(obj):
            if isinstance(obj, dict):
                if "secondaryKey" in obj and ("femaleVariant" in obj or "maleVariant" in obj):
                    text = obj.get("femaleVariant") or obj.get("maleVariant") or ""
                    key = obj.get("secondaryKey") or ""
                    if isinstance(key, dict):  # WolvenKit writes some strings as {"$value": ...}
                        key = key.get("$value", "")
                    if isinstance(text, dict):
                        text = text.get("$value", "")
                    out.append((str(key), str(text)))
                    return
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        try:
            walk(json.loads(data.decode("utf-8-sig")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FormatError(f"onscreens.json isn't JSON Dark STL can read: {exc}") from exc
        return out
    from .formats import cr2w

    f = cr2w.read_cr2w(data)
    for chunk in f.chunks_of("localizationPersistenceOnScreenEntries"):
        for e in chunk.get("entries") or []:
            get = e.get if hasattr(e, "get") else (lambda k, d=None: d)
            text = get("femaleVariant") or get("maleVariant") or ""
            out.append((str(get("secondaryKey") or ""), str(text)))
    return out


def find_onscreens(folder: Path) -> tuple[Path, bytes] | None:
    """Where the game's on-screen text is (a file, or an archive's), and its bytes."""
    from . import depot
    from .formats import archive as arc

    folder = Path(folder)
    rel = Path(*ONSCREENS.split("\\"))
    for base in [folder, *folder.parents][:4]:
        for candidate in (base / rel, base / rel.with_name("onscreens.json.json")):
            if candidate.is_file():
                return candidate, candidate.read_bytes()
    game = depot.game_root_of(folder)
    archives = depot.game_archives(game) if game is not None else []
    archives += sorted(p for p in folder.glob("*.archive") if p not in archives)
    h = arc.path_hash(ONSCREENS)
    # the language archives first: that's where the game keeps it
    for a in sorted(archives, key=lambda p: (not p.name.lower().startswith("lang_en"), p.name.lower())):
        try:
            ar = arc.open_archive(a)
            if h in ar.entries:
                return a, ar.read(h)
        except (FormatError, OSError) as exc:
            log.debug("Couldn't look in %s for the on-screen text: %s", a, exc)
    return None


def texts(folder: Path, cache: Path | None = None, fresh: bool = False) -> list[tuple[str, str]]:
    """The on-screen text's (key, text) pairs (kept in ``cache``)."""
    from . import depot

    folder = Path(folder)
    game = depot.game_root_of(folder) or folder
    kept = None
    if cache is not None:
        tag = hashlib.sha1(os.path.normcase(str(game)).encode()).hexdigest()[:16]
        kept = Path(cache) / "names" / f"onscreens-{tag}.json"
    found = find_onscreens(folder)
    if found is None:
        return []
    where, data = found
    try:
        st = where.stat()
        signature = [FORMAT_VERSION, str(where), st.st_size, int(st.st_mtime)]
    except OSError:
        signature = None
    if kept is not None and signature and not fresh:
        try:
            saved = json.loads(kept.read_text("utf-8"))
            if saved.get("signature") == signature:
                return [tuple(x) for x in saved.get("texts") or []]
        except (OSError, ValueError, AttributeError):
            pass
    try:
        out = read_onscreens(data)
    except (FormatError, ValueError, IndexError, KeyError) as exc:
        log.warning("The on-screen text in %s couldn't be read: %s", where, exc)
        return []
    if kept is not None and signature:
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            tmp = kept.with_suffix(".part")
            tmp.write_text(json.dumps({"signature": signature, "texts": out}, ensure_ascii=False), "utf-8")
            os.replace(tmp, kept)
        except OSError:
            pass
    return out


def name_texts(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """{a name's letters and digits, lower case: the name as shown}, for short texts that aren't descriptions."""
    out: dict[str, str] = {}
    for key, text in pairs:
        text = (text or "").strip()
        if not text or len(text) > 40 or len(text.split()) > 4 or _NOT_A_NAME.search(key or ""):
            continue
        n = _norm(text)
        if len(n) >= 3:
            out.setdefault(n, text)
    return out


def weapon_model(depot_path: str) -> tuple[list[str], str] | None:
    """(the ways a weapon's model may be named, the mesh's part) for a mesh of a weapon, else None."""
    parts = depot_path.replace("/", "\\").lower().split("\\")
    if len(parts) < 4 or parts[1] != "weapons":
        return None
    model = parts[3]  # base\weapons\<kind>\<maker_model>\...
    words = [w for w in model.split("_") if w]
    if not words:
        return None
    ways = [_norm(model)] + ([_norm("_".join(words[1:]))] if len(words) > 1 else [])
    stem = parts[-1].rsplit(".", 1)[0]
    part = ""
    if model in stem:
        part = stem.split(model, 1)[1].strip("_- ")
    elif "__" in stem:
        part = stem.rsplit("__", 1)[1]
    part = re.sub(r"_+", " ", part).strip()
    return [w for w in ways if len(w) >= 3], part


def names(folder: Path, entries: list[CatalogEntry], cache: Path | None = None,
          fresh: bool = False) -> dict[str, str]:
    """{overlay key (the model's path in ``folder``): its name in the game} for the weapons the text names."""
    from ...overlay import model_key
    from . import depot

    folder = Path(folder)
    weapons = []
    for e in entries:
        if e.config is not None or e.category != "Weapons":
            continue
        try:
            path = depot.open_file(e.path).depot_path
        except (FormatError, OSError):
            path = None
        found = weapon_model(path) if path else None
        if found:
            weapons.append((e, found))
    if not weapons:
        return {}
    table = name_texts(texts(folder, cache, fresh))
    if not table:
        return {}
    out = {}
    for e, (ways, part) in weapons:
        name = next((table[w] for w in ways if w in table), None)
        if not name:
            continue
        try:
            rel = Path(e.path).relative_to(folder).as_posix()
        except ValueError:
            continue
        out[model_key(rel)] = f"{name} ({part})" if part else name
    return out


__all__ = ["find_onscreens", "name_texts", "names", "read_onscreens", "texts", "weapon_model"]
