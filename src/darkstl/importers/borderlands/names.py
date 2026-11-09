"""The names Borderlands 1, 2 and The Pre-Sequel give their models, from their localised text (``.INT`` files).

Each package of game data has a text file of the same name in ``Localization/INT`` (``GD_Assassin_Items.int``
beside ``GD_Assassin_Items.upk``). Its sections are objects of that package (``[Head_Zero_Default
CustomizationDefinition]``) and their keys the texts the object shows (``CustomizationName=Zero``).
An object whose name text is set and that refers to a mesh (a head or skin customisation, an item) names
that mesh: its properties are read for references to skeletal or static meshes, following them into other
packages. Description and flavour texts aren't names.

What's read is kept in the importer's cache until the text files change.
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
MESH_CLASSES = ("SkeletalMesh", "StaticMesh")
_NAME_KEY = re.compile(r"(^|[a-z])(name|title)$", re.I)
_NOT_A_NAME = re.compile(r"desc|flavo|lore|hint|tooltip|message|prefix|suffix|stat|text|plural", re.I)
_SECTION = re.compile(r"^\[(?P<object>[^\s\]]+)(\s+(?P<cls>[^\]]+))?\]\s*$")
MAX_FILES = 4000


def read_int(data: bytes) -> dict[str, tuple[str, dict[str, str]]]:
    """{object path: (class, {key: text})} of an ``.INT`` file (UTF-16 with a byte order mark, or 8-bit)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", "replace")
    out: dict[str, tuple[str, dict[str, str]]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m = _SECTION.match(line)
        if m:
            current = {}
            out[m.group("object")] = ((m.group("cls") or "").strip(), current)
            continue
        if current is None:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        current.setdefault(key.strip(), value)
    return out


def name_of(keys: dict[str, str]) -> str | None:
    """The text a section's keys name its object by, if any."""
    for key, value in keys.items():
        bare = re.sub(r"\[\d+\]$", "", key)
        if not _NAME_KEY.search(bare) or _NOT_A_NAME.search(bare):
            continue
        value = value.strip()
        if value and len(value) <= 64 and "\\n" not in value and not value.startswith(("$", "%")):
            return value
    return None


def text_files(root: Path) -> list[Path]:
    """The English text files of a game folder (``.../Localization/INT/*.int``, the game's and its DLCs')."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        here = Path(dirpath)
        if here.name.lower() == "int" and here.parent.name.lower() == "localization":
            out += [here / f for f in sorted(files) if f.lower().endswith(".int")]
            dirs[:] = []
            if len(out) >= MAX_FILES:
                break
        else:
            dirs[:] = [d for d in dirs if not d.lower().startswith(("movies", "splash", "binaries", "logs"))]
    return out[:MAX_FILES]


def _refs(value, depth: int = 0) -> list:
    """The object references in a property's value (in arrays and structs too)."""
    from .formats.props import ArrayValue, ObjectRef, StructValue

    if isinstance(value, ObjectRef):
        return [value] if value else []
    if depth > 2:
        return []
    out = []
    if isinstance(value, StructValue):
        try:
            for name in value.props.names():
                out += [r for v in value.props.all(name) for r in _refs(v, depth + 1)]
        except (FormatError, ValueError, IndexError, KeyError):
            return out
    elif isinstance(value, ArrayValue):
        for getter in (value.objects, value.structs):
            try:
                items = getter()
            except (FormatError, ValueError, IndexError, KeyError, TypeError):
                continue
            for item in items:
                if hasattr(item, "names") and not isinstance(item, ObjectRef):  # a struct's Props
                    out += [r for n in item.names() for v in item.all(n) for r in _refs(v, depth + 1)]
                else:
                    out += _refs(item, depth + 1)
            break
    return out


def meshes_named(root: Path, cache: Path | None = None, fresh: bool = False) -> dict[str, str]:
    """{the mesh's path, ``<package>.<object path>`` lower case: the name the game's text gives it}."""
    from . import files
    from .formats import upk
    from .formats.props import object_props

    texts = text_files(Path(root))
    if not texts:
        return {}
    h = hashlib.sha1(str(FORMAT_VERSION).encode())
    for p in texts:
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    signature = h.hexdigest()
    kept = None
    if cache is not None:
        tag = hashlib.sha1(os.path.normcase(str(root)).encode()).hexdigest()[:16]
        kept = Path(cache) / "names" / f"int-{tag}.json"
        if not fresh:
            try:
                data = json.loads(kept.read_text("utf-8"))
                if data.get("signature") == signature:
                    return dict(data.get("meshes") or {})
            except (OSError, ValueError, AttributeError):
                pass
    index = files.PackageIndex(Path(root))
    found: dict[str, set[str]] = {}
    for text_file in texts:
        try:
            sections = read_int(text_file.read_bytes())
        except OSError:
            continue
        named = {obj: n for obj, (_cls, keys) in sections.items() if (n := name_of(keys))}
        if not named:
            continue
        pkg_file = index.find(text_file.stem)
        if pkg_file is None:
            continue
        try:
            pkg = upk.open_package(pkg_file)
        except (FormatError, OSError) as exc:
            log.debug("Couldn't open %s for its names: %s", pkg_file, exc)
            continue
        for obj, name in named.items():
            e = pkg.find_export(obj)
            if e is None or e.class_name in MESH_CLASSES:
                continue
            try:
                props = object_props(pkg, pkg.export_data(e))
            except (FormatError, ValueError, IndexError, KeyError, EOFError):
                continue
            for prop in props.names():
                for value in props.all(prop):
                    for ref in _refs(value):
                        if ref.class_name not in MESH_CLASSES:
                            continue
                        try:
                            hit = files.resolve(ref, ref.class_name)
                        except (FormatError, OSError, ValueError, IndexError):
                            hit = None
                        if hit is None:
                            key = ref.path.lower()
                        else:
                            mpkg, me = hit
                            key = f"{mpkg.stem}.{me.path}".lower()
                        found.setdefault(key, set()).add(name)
    out = {k: next(iter(v)) for k, v in found.items() if len(v) == 1}
    if kept is not None:
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            tmp = kept.with_suffix(".part")
            tmp.write_text(json.dumps({"signature": signature, "meshes": out}, ensure_ascii=False), "utf-8")
            os.replace(tmp, kept)
        except OSError:
            pass
    return out


def names(folder: Path, entries: list[CatalogEntry], cache: Path | None = None,
          fresh: bool = False) -> dict[str, str]:
    """{overlay key (the model's path in ``folder``): its name in the game} for the models the text names."""
    from ...overlay import model_key
    from . import files

    folder = Path(folder)
    try:
        meshes = meshes_named(files.game_root(folder), cache, fresh)
    except OSError:
        return {}
    if not meshes:
        return {}
    out = {}
    for e in entries:
        if e.config is not None or not files.is_member(e.path):
            continue
        package, object_path, _cls = files.split_member(e.path)
        low = object_path.lower()
        # its path with its package first, or as cooked under the package it came from (Pkg.Group.Name)
        name = meshes.get(f"{package.stem}.{object_path}".lower()) or meshes.get(low)
        if not name:
            continue
        try:
            rel = Path(e.path).relative_to(folder).as_posix()
        except ValueError:
            continue
        out[model_key(rel)] = name
    return out


__all__ = ["meshes_named", "name_of", "names", "read_int", "text_files"]
