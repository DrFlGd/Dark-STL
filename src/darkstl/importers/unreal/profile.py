"""An Unreal 4/5 game's profile (see :mod:`darkstl.importers.profiles`), read into what the readers use.

Each game's ``unreal`` section says where its files are and how to read them (the project folder, the
engine release, AES keys, which type mappings are its own); its ``models`` section how its models are
told apart and sorted into kinds and groups; ``textures`` and ``collect`` the rest. A game without its
own sorting rules gets those of the shipped ``unreal-defaults.json``, which sorts any Unreal game by the
usual folder names (Characters, Weapons, Props, Environment...).

Sorting rules
-------------
``kinds`` is a list of rules, tried in order. A rule matches a model when one of its ``folders`` is
among the folders the model's file is in (below ``Content``), and, when given, its file name contains
one of ``name_has`` (or matches the regular expression ``name_matches``) and it is of ``class``
(``SkeletalMesh`` or ``StaticMesh``). The model is then put in the rule's ``kind`` and ``group``. The
group is a list of pieces joined with " / ", each with places filled in:

* ``{1}``, ``{2}``...: the first, second... folder after the one the rule matched (folders starting
  with ``_`` and those in ``group_skip`` are passed over);
* ``{top}``: the first folder below ``Content`` (passing over those in ``top_skip``);
* ``{stem}``: the file's name;
* ``{1:table}``: the folder looked up in the table ``tables.table`` (by its name in lower case without
  spaces), or the folder itself when the table doesn't have it;
* ``{1|top|Text}``: the first of these that isn't empty; a word other than a number, ``top`` or
  ``stem`` is written as it is.

A piece that comes out empty is left out. Models no rule matches go by ``otherwise``, per class.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import profiles

log = logging.getLogger(__name__)

MODEL_CLASSES = ("SkeletalMesh", "StaticMesh")
DEFAULTS_NAME = "Unreal game"
_PLACE = re.compile(r"\{([^{}]*)\}")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _regex(text, fallback: str = r"(?!x)x") -> re.Pattern:
    try:
        return re.compile(str(text) if text else fallback, re.I)
    except re.error:
        log.warning("Not a regular expression in a game profile: %r", text)
        return re.compile(fallback, re.I)


def label(folder: str) -> str:
    return folder.replace("_", " ").strip()


@dataclass
class Rule:
    kind: str
    group: list[str]
    folders: list[str] = field(default_factory=list)
    name_has: list[str] = field(default_factory=list)
    name_matches: re.Pattern | None = None
    cls: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> Rule:
        group = d.get("group", ["{1}"])
        return cls(str(d.get("kind") or "Objects"), [str(g) for g in (group if isinstance(group, list) else [group])],
                   [str(f).lower() for f in d.get("folders") or []], [str(w).lower() for w in d.get("name_has") or []],
                   _regex(d["name_matches"]) if d.get("name_matches") else None,
                   str(d["class"]) if d.get("class") else None)

    def match(self, folders: list[str], stem: str, cls: str) -> int | None:
        """The index (in ``folders``) of the folder the rule matched, -1 when it names no folders; None: no match."""
        if self.cls and self.cls != cls:
            return None
        s = stem.lower()
        if self.name_has and not any(w in s for w in self.name_has):
            return None
        if self.name_matches is not None and not self.name_matches.search(stem):
            return None
        if not self.folders:
            return -1
        for word in self.folders:  # the rule's first word that's among the folders
            if word in folders:
                return folders.index(word)
        return None


@dataclass
class Peek:
    """Which packages whose names don't say they're meshes have their class read from their headers."""
    names: str = "none"  # "none", "unnamed" (any not named as something else), or a regular expression
    limit: int = 60000
    not_a_mesh: re.Pattern | None = None
    skip_folders: frozenset = frozenset()
    folders: tuple[str, ...] = ()  # one of these must be among the file's folders (with a regular expression)
    containers_only: bool = False  # only packages in IoStore containers (their headers are quick to read)
    pattern: re.Pattern | None = None

    @classmethod
    def from_json(cls, d: dict) -> Peek:
        names = str(d.get("names") or "none")
        return cls(names, int(d.get("limit") or 60000), _regex(d["not_a_mesh"]) if d.get("not_a_mesh") else None,
                   frozenset(str(f).lower() for f in d.get("skip_folders") or []),
                   tuple(str(f).lower() for f in d.get("folders") or []), bool(d.get("containers_only")),
                   _regex(names) if names not in ("none", "unnamed") else None)


@dataclass
class Sorting:
    skeletal: re.Pattern
    static: re.Pattern
    skip: re.Pattern
    label_prefix: re.Pattern
    peek: Peek
    group_skip: frozenset
    top_skip: frozenset
    tables: dict[str, dict[str, str]]
    kinds: list[Rule]
    otherwise: dict[str, Rule]

    @classmethod
    def from_json(cls, d: dict) -> Sorting:
        other = d.get("otherwise") or {}
        otherwise = {k: Rule.from_json(other.get(k) or {"kind": kind, "group": ["{top}"]})
                     for k, kind in (("StaticMesh", "Objects"), ("SkeletalMesh", "Characters"))}
        tables = {str(k): {str(a).lower().replace(" ", ""): str(b) for a, b in (v or {}).items()}
                  for k, v in (d.get("tables") or {}).items() if isinstance(v, dict)}
        return cls(_regex(d.get("skeletal"), r"^(sk|skm)_"), _regex(d.get("static"), r"^(sm|stm)_"),
                   _regex(d.get("skip")), _regex(d.get("label_prefix"), r"^(SK|SKM|SM)_"),
                   Peek.from_json(d.get("peek") or {}),
                   frozenset(str(f).lower() for f in d.get("group_skip") or []),
                   frozenset(str(f).lower() for f in d.get("top_skip") or []), tables,
                   [Rule.from_json(r) for r in d.get("kinds") or [] if isinstance(r, dict)], otherwise)

    # -- what a model is and where it goes

    def class_by_name(self, stem: str) -> str | None:
        if self.skeletal.search(stem):
            return "SkeletalMesh"
        if self.static.search(stem):
            return "StaticMesh"
        return None

    def classify(self, virtual: str, cls: str | None = None) -> tuple[str, str, str] | None:
        """(class, kind, group) for a model file; None when its name doesn't say it is a mesh and ``cls``
        (the class read from the package) doesn't either, or it's a mesh the game doesn't show (physics)."""
        parts = virtual.replace("\\", "/").split("/")
        stem = parts[-1].rsplit(".", 1)[0]
        cls = cls or self.class_by_name(stem)
        if cls not in MODEL_CLASSES or self.skip.search(stem):
            return None
        low = [p.lower() for p in parts[:-1]]
        try:
            content = len(low) - 1 - low[::-1].index("content")
        except ValueError:
            content = -1
        sub = parts[content + 1:-1]
        sl = [p.lower() for p in sub]
        for rule in self.kinds:
            at = rule.match(sl, stem, cls)
            if at is not None:
                return cls, rule.kind, self._group(rule, sub, at, stem)
        rule = self.otherwise[cls]
        return cls, rule.kind, self._group(rule, sub, -1, stem)

    def _group(self, rule: Rule, sub: list[str], at: int, stem: str) -> str:
        after = [p for p in sub[at + 1:] if not p.startswith("_") and p.lower() not in self.group_skip]
        top = next((p for p in sub if p.lower() not in self.top_skip and not p.startswith("_")), "") \
            if self.top_skip else (sub[0] if sub else "")

        def fill(m: re.Match) -> str:
            for alt in m.group(1).split("|"):
                alt = alt.strip()
                key, _, table = alt.partition(":")
                key = key.strip()
                if key.isdigit():
                    i = int(key)
                    value = after[i - 1] if 0 < i <= len(after) else ""
                elif key == "top":
                    value = top
                elif key == "stem":
                    value = stem
                else:
                    return alt  # written as it is
                if value:
                    looked = self.tables.get(table.strip(), {}) if table else {}
                    return looked.get(value.lower().replace(" ", ""), label(value))
            return ""

        pieces = [_PLACE.sub(fill, piece).strip() for piece in rule.group]
        return " / ".join(p for p in pieces if p)

    def label(self, stem: str) -> str:
        return self.label_prefix.sub("", stem, count=1).replace("_", " ")

    def worth_peeking(self, virtual: str, in_container: bool) -> bool:
        """Whether to read the class of a package whose name doesn't say it is a mesh."""
        p = self.peek
        if p.names == "none" or not virtual.lower().endswith(".uasset"):
            return False
        if p.containers_only and not in_container:
            return False
        stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if self.skip.search(stem):
            return False
        folders = virtual.lower().split("/")[:-1]
        if p.pattern is not None:
            return bool(p.pattern.search(stem)) and (not p.folders or any(f in folders for f in p.folders))
        if p.not_a_mesh is not None and p.not_a_mesh.search(stem):
            return False
        return not any(f in p.skip_folders for f in folders)


@dataclass
class UnrealProfile:
    """An Unreal 4/5 game's profile."""

    name: str
    engine: str = "unreal4"
    importer: str = "unreal"
    project: str = ""  # the project folder: <install>/<project>/Content/Paks
    release: str = "auto"  # "auto": read from the game's executable; or a release such as "4.26"
    default_release: str = ""  # when the executable doesn't say
    aes_keys: list[str] = field(default_factory=list)
    usmap_words: list[str] = field(default_factory=list)  # words in a .usmap's file name that say it's this game's
    type_prefix: str = ""  # what the game's own classes start with (Oak for Borderlands 3)
    layout: str = ""  # "bl3": Borderlands 3's own changes to the package layouts
    look_through: bool = False  # look through every package for animations, when none is named like one
    sorting: Sorting | None = field(default=None, repr=False)
    normal_green: str = "directx"
    roles: dict[str, list[re.Pattern]] = field(default_factory=dict, repr=False)
    collect_engine: bool = True
    collect_usmap: bool = True
    names_tables: re.Pattern | None = field(default=None, repr=False)  # which assets are string tables
    names_keys: re.Pattern | None = field(default=None, repr=False)  # the ending of a key that holds a name
    names_skip: re.Pattern | None = field(default=None, repr=False)  # keys that don't, though they end so
    names_limit: int = 400
    recognise_files: list[str] = field(default_factory=list)
    folder_names: list[str] = field(default_factory=list)
    folder_pattern: re.Pattern | None = None
    shipped: bool = True
    source: Path | None = None
    known: bool = True  # False: made up for a game no profile names (the defaults under the user's name)

    @classmethod
    def from_profile(cls, p: profiles.Profile | None, name: str | None = None) -> UnrealProfile:
        base = profiles.defaults("unreal4")
        data = profiles.merge(base, p.data) if p is not None else dict(base)
        if p is not None and "models" in p.data and isinstance(base.get("models"), dict):
            data["models"] = {**base["models"], **p.data["models"]}
        u = data.get("unreal") if isinstance(data.get("unreal"), dict) else {}
        usmap = u.get("usmap") if isinstance(u.get("usmap"), dict) else {}
        tex = data.get("textures") if isinstance(data.get("textures"), dict) else {}
        col = data.get("collect") if isinstance(data.get("collect"), dict) else {}
        rec = data.get("recognise") if isinstance(data.get("recognise"), dict) else {}
        nm = data.get("names") if isinstance(data.get("names"), dict) else {}
        roles = {str(role).lower(): [_regex(x) for x in (pats if isinstance(pats, list) else [pats])]
                 for role, pats in (tex.get("roles") or {}).items()}
        green = str(tex.get("normal_green") or "directx").lower()
        return cls(
            name=p.name if p is not None else (name or DEFAULTS_NAME),
            engine=p.engine if p is not None else "unreal4",
            importer=p.importer if p is not None else "unreal",
            project=str(u.get("project") or ""),
            release=str(u.get("release") or ""),
            default_release=str(u.get("default_release") or ""),
            aes_keys=[str(k) for k in u.get("aes_keys") or []],
            usmap_words=[str(w).lower() for w in usmap.get("words") or []],
            type_prefix=str(usmap.get("type_prefix") or "").lower(),
            layout=str(u.get("layout") or ""),
            look_through=bool(u.get("look_through_for_animations")),
            sorting=Sorting.from_json(data.get("models") or {}),
            normal_green=green if green in ("directx", "opengl") else "directx",
            roles=roles,
            collect_engine=bool(col.get("engine_release", True)),
            collect_usmap=bool(col.get("usmap", True)),
            names_tables=_regex(nm["tables"]) if nm.get("tables") else None,
            names_keys=_regex(nm["keys"]) if nm.get("keys") else None,
            names_skip=_regex(nm["skip_keys"]) if nm.get("skip_keys") else None,
            names_limit=_int(nm.get("limit"), 400),
            recognise_files=[str(f) for f in rec.get("files") or []],
            folder_names=[str(f).lower() for f in rec.get("folder_names") or []],
            folder_pattern=_regex(rec["folder_pattern"]) if rec.get("folder_pattern") else None,
            shipped=p.shipped if p is not None else True,
            source=p.source if p is not None else None,
            known=p is not None,
        )

    # -- the engine release

    @staticmethod
    def parse_release(text: str) -> int | None:
        """``"4.26"`` -> 426."""
        m = re.match(r"^\s*(\d)\.(\d+)", text or "")
        if not m:
            return None
        major, minor = int(m.group(1)), int(m.group(2))
        return major * 100 + minor if major in (4, 5) and minor < 100 else None

    def classify(self, virtual: str, cls: str | None = None):
        return self.sorting.classify(virtual, cls)


_cache: dict = {"found": None, "by_name": {}, "list": []}


def _profiles() -> tuple[list[UnrealProfile], dict[str, UnrealProfile]]:
    found = profiles.all_profiles()
    if _cache["found"] is not found:  # read again (kept, so the same tuple means the same files)
        items = [UnrealProfile.from_profile(p) for p in found
                 if not p.defaults and p.engine in profiles.UNREAL_ENGINES]
        _cache.update(found=found, list=items, by_name={p.name.lower(): p for p in items},
                      generic=UnrealProfile.from_profile(None))
    return _cache["list"], _cache["by_name"]


def unreal_profiles(importer: str | None = None) -> list[UnrealProfile]:
    """Every Unreal 4/5 game's profile (of the games one importer reads)."""
    items, _ = _profiles()
    return [p for p in items if importer is None or p.importer == importer]


def get(game: str | None) -> UnrealProfile | None:
    """The profile of the Unreal game called ``game``; None when no profile names it."""
    if not game:
        return None
    return _profiles()[1].get(game.strip().lower())


def for_game(game: str | None) -> UnrealProfile:
    """The game's profile, or for a game no profile names the defaults under its name."""
    found = get(game)
    if found is not None:
        return found
    _profiles()
    generic = _cache["generic"]
    if not game or game == generic.name:
        return generic
    out = UnrealProfile.from_profile(None, game)
    return out


def projects_shared(project: str) -> bool:
    """Whether several games' profiles name the same project folder (Borderlands 3, Wonderlands and 4 are
    all OakGame), so the project folder alone doesn't say which game a folder is."""
    low = project.lower()
    return sum(1 for p in unreal_profiles() if p.project.lower() == low) > 1


__all__ = ["DEFAULTS_NAME", "MODEL_CLASSES", "Peek", "Rule", "Sorting", "UnrealProfile", "for_game", "get", "label",
           "projects_shared", "unreal_profiles"]
