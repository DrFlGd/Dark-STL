"""The user's collections, tags and stars: one file for the whole library, across every game.

``<settings>/library/collections.json`` (beside the games' overlays, see :mod:`darkstl.overlay`) holds:

- **collections**: named, ordered lists of models from any game ("To print", "Bosses of every game");
- **tags** per model ("cloth", "needs supports"), searched like names and offered as filter chips;
- a **star** per model, for the ones to come back to.

A model is referred to by its game and its key in that game: the same key the game's overlay files it under
(its path in the game, the same for the game's file, a collected copy and the package made from it; see
:func:`darkstl.overlay.model_key`), or ``config:<id>`` for a configurable model. So nothing here is lost when
a folder is rescanned, the library is moved, the game is imported as packages or its folder is removed and
added again: a model that isn't in the library just isn't shown until it's back.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

FORMAT = "darkstl-collections"
VERSION = 1
MAX_TAG = 40
MAX_NAME = 80


def clean_tag(tag) -> str:
    """A tag as it's kept: words separated by single spaces, at most 40 characters."""
    return " ".join(str(tag or "").replace(",", " ").split())[:MAX_TAG].strip()


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME].strip()


Ref = tuple[str, str]  # (game, model key)


class Collections:
    """The library's collections file, read once and written after each change."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = self._read()
        self._member: dict[Ref, list[str]] | None = None  # (game, key) -> ids of the collections holding it
        self._games: set[str] | None = None  # the games with a model marked or collected

    # ------------------------------------------------------------ the file

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            data = {}
        out = {"format": FORMAT, "version": VERSION, "collections": [], "marks": {}}
        for c in data.get("collections") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            models = [{"game": str(m["game"]), "key": str(m["key"])} for m in c.get("models") or []
                      if isinstance(m, dict) and m.get("game") and m.get("key")]
            out["collections"].append({"id": str(c["id"]), "name": clean_name(c.get("name")) or "Collection",
                                       "models": models, "created": c.get("created") or ""})
        for game, keys in (data.get("marks") or {}).items():
            if not isinstance(keys, dict):
                continue
            for key, mark in keys.items():
                if not isinstance(mark, dict):
                    continue
                tags = [t for t in (clean_tag(x) for x in mark.get("tags") or []) if t]
                kept = {**({"tags": tags} if tags else {}), **({"star": True} if mark.get("star") else {})}
                if kept:
                    out["marks"].setdefault(str(game), {})[str(key)] = kept
        return out

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)

    def _changed(self) -> None:
        self._member = None
        self._games = None

    # ------------------------------------------------------------ collections

    def collections(self) -> list[dict]:
        """Every collection, in the order they were made: {id, name, models: [{game, key}], created}."""
        with self._lock:
            return [dict(c, models=list(c["models"])) for c in self.data["collections"]]

    def get(self, cid: str) -> dict | None:
        with self._lock:
            c = self._find(cid)
            return dict(c, models=list(c["models"])) if c else None

    def _find(self, cid: str) -> dict | None:
        return next((c for c in self.data["collections"] if c["id"] == cid), None)

    def _need(self, cid: str) -> dict:
        c = self._find(cid)
        if c is None:
            raise KeyError(cid)
        return c

    def _free_name(self, name: str, but: str | None = None) -> str:
        taken = {c["name"].lower() for c in self.data["collections"] if c["id"] != but}
        out, n = name, 2
        while out.lower() in taken:
            out, n = f"{name} ({n})", n + 1
        return out

    def create(self, name: str, refs: list[Ref] = ()) -> dict:
        """A new collection (its name made unique: "Bosses (2)"), holding ``refs`` in that order."""
        name = clean_name(name)
        if not name:
            raise ValueError("A collection needs a name.")
        with self._lock:
            c = {"id": uuid.uuid4().hex[:10], "name": self._free_name(name), "models": [],
                 "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.data["collections"].append(c)
            self._add(c, refs)
            self._changed()
            return dict(c, models=list(c["models"]))

    def rename(self, cid: str, name: str) -> dict:
        name = clean_name(name)
        if not name:
            raise ValueError("A collection needs a name.")
        with self._lock:
            c = self._need(cid)
            c["name"] = self._free_name(name, but=cid)
            return dict(c, models=list(c["models"]))

    def delete(self, cid: str) -> bool:
        with self._lock:
            before = len(self.data["collections"])
            self.data["collections"] = [c for c in self.data["collections"] if c["id"] != cid]
            self._changed()
            return len(self.data["collections"]) != before

    @staticmethod
    def _add(c: dict, refs) -> int:
        have = {(m["game"], m["key"]) for m in c["models"]}
        n = 0
        for game, key in refs:
            if (game, key) not in have:
                c["models"].append({"game": game, "key": key})
                have.add((game, key))
                n += 1
        return n

    def add(self, cid: str, refs: list[Ref]) -> int:
        """Add models to the end of a collection (those in it already stay where they are). How many were added."""
        with self._lock:
            n = self._add(self._need(cid), refs)
            self._changed()
            return n

    def remove(self, cid: str, refs: list[Ref]) -> int:
        with self._lock:
            c = self._need(cid)
            drop = set(refs)
            before = len(c["models"])
            c["models"] = [m for m in c["models"] if (m["game"], m["key"]) not in drop]
            self._changed()
            return before - len(c["models"])

    def move(self, cid: str, refs: list[Ref], before: Ref | None) -> None:
        """Move models of a collection (in their order there) to just before the model ``before`` (None: to the
        end). Models not in the collection are added there."""
        with self._lock:
            c = self._need(cid)
            moving = list(dict.fromkeys(refs))
            rest = [m for m in c["models"] if (m["game"], m["key"]) not in set(moving)]
            at = next((i for i, m in enumerate(rest) if (m["game"], m["key"]) == before), len(rest))
            c["models"] = rest[:at] + [{"game": g, "key": k} for g, k in moving] + rest[at:]
            self._changed()

    def holding(self, ref: Ref) -> list[str]:
        """The ids of the collections a model is in."""
        with self._lock:
            if self._member is None:
                member: dict[Ref, list[str]] = {}
                for c in self.data["collections"]:
                    for m in c["models"]:
                        member.setdefault((m["game"], m["key"]), []).append(c["id"])
                self._member = member
            return list(self._member.get(ref, ()))

    # ------------------------------------------------------------ tags and stars

    def mark(self, ref: Ref) -> dict:
        """{"tags": [...], "star": bool} of a model (empty when it has none)."""
        game, key = ref
        with self._lock:
            return dict((self.data["marks"].get(game) or {}).get(key) or {})

    def has_marks(self, game: str) -> bool:
        """Whether any model of ``game`` has a tag or a star, or is in a collection (a quick test before
        looking a model up)."""
        with self._lock:
            if self._games is None:
                self._games = {g for g, keys in self.data["marks"].items() if keys}
                self._games |= {m["game"] for c in self.data["collections"] for m in c["models"]}
            return game in self._games

    def _set(self, ref: Ref, mark: dict) -> None:
        game, key = ref
        marks = self.data["marks"]
        if mark:
            marks.setdefault(game, {})[key] = mark
        else:
            (marks.get(game) or {}).pop(key, None)
            if game in marks and not marks[game]:
                del marks[game]

    def tag(self, refs: list[Ref], add=(), remove=()) -> None:
        """Add the tags ``add`` to these models and take ``remove`` off them (any case: a tag is the same tag
        whatever its case, kept as first written)."""
        add = [t for t in (clean_tag(x) for x in add) if t]
        remove = {clean_tag(x).lower() for x in remove}
        with self._lock:
            known = {t.lower(): t for t in self.all_tags()}
            for ref in refs:
                mark = self.mark(ref)
                tags = [t for t in mark.get("tags") or [] if t.lower() not in remove]
                have = {t.lower() for t in tags}
                for t in add:
                    if t.lower() not in have:
                        tags.append(known.get(t.lower(), t))
                        have.add(t.lower())
                mark.pop("tags", None)
                if tags:
                    mark["tags"] = tags
                self._set(ref, mark)
            self._changed()

    def star(self, refs: list[Ref], on: bool) -> None:
        with self._lock:
            for ref in refs:
                mark = self.mark(ref)
                mark.pop("star", None)
                if on:
                    mark["star"] = True
                self._set(ref, mark)
            self._changed()

    def all_tags(self) -> dict[str, int]:
        """Every tag in use: {tag: how many models have it}, most used first."""
        with self._lock:
            counts: dict[str, int] = {}
            first: dict[str, str] = {}
            for keys in self.data["marks"].values():
                for mark in keys.values():
                    for t in mark.get("tags") or []:
                        k = t.lower()
                        first.setdefault(k, t)
                        counts[k] = counts.get(k, 0) + 1
            return {first[k]: n for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))}

    def starred(self) -> list[Ref]:
        with self._lock:
            return [(g, k) for g, keys in self.data["marks"].items() for k, m in keys.items() if m.get("star")]

    def rename_tag(self, old: str, new: str) -> int:
        """Rename a tag on every model that has it (merged with ``new`` where a model has both); "" removes it."""
        old_l, new = clean_tag(old).lower(), clean_tag(new)
        n = 0
        with self._lock:
            for game, keys in list(self.data["marks"].items()):
                for key, mark in list(keys.items()):
                    tags = mark.get("tags") or []
                    if not any(t.lower() == old_l for t in tags):
                        continue
                    n += 1
                    rest = [t for t in tags if t.lower() != old_l]
                    if new and not any(t.lower() == new.lower() for t in rest):
                        rest.append(new)
                    mark = dict(mark)
                    mark.pop("tags", None)
                    if rest:
                        mark["tags"] = rest
                    self._set((game, key), mark)
            self._changed()
        return n


__all__ = ["Collections", "clean_name", "clean_tag"]
