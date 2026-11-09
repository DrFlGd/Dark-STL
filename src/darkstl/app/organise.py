"""The library's organising commands: collections, tags and stars, related models and each game's problems.

A mixin of :class:`darkstl.app.service.Service` (it uses the service's library, overlays, index and lock), kept
apart so the service doesn't grow past reading. Collections, tags and stars are kept in one file for the whole
library (:mod:`darkstl.library_collections`); every model is referred to there by its game and its key in that
game (its path in the game, or ``config:<id>`` for a configurable model), so they survive rescans, imports as
packages and the library being moved.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .. import overlay
from ..library import Library
from ..library_collections import clean_name, clean_tag
from ..library_index import clean_stats
from ..model import CatalogEntry

Ref = tuple[str, str]

PROBLEM_ROWS = 60  # models listed per kind of problem (the rest are counted)
MISSING_CHECK_SECONDS = 3.0  # how long looking for files gone missing may take, per game


class OrganiseCommands:
    """Collections, tags, stars, related models and problems: see the module doc."""

    # ------------------------------------------------------------ references to models

    def _model_ref(self, e: CatalogEntry, lib: Library | None = None) -> Ref | None:
        """(game, key) a model is kept under in the collections file (see darkstl.library_collections)."""
        if e.config is not None:
            return e.game, f"config:{e.config.id}"
        rel = e.__dict__.get("_overlay_rel")
        if rel is None:
            rel = self._overlay_rel(e, lib) or ""
            e.__dict__["_overlay_rel"] = rel
        return (e.game, overlay.model_key(rel)) if rel else None

    def _mark(self, lib: Library, e: CatalogEntry) -> None:
        """Give a model its tags, star and collections (the library calls this for every model it lists)."""
        ref = self._model_ref(e, lib) if self.marks.has_marks(e.game) else None
        mark = self.marks.mark(ref) if ref is not None else {}
        held = self.marks.holding(ref) if ref is not None else []
        tags = tuple(mark.get("tags") or ())
        for name, value in (("_tags", tags), ("_starred", bool(mark.get("star"))), ("_collections", tuple(held))):
            if value:
                e.__dict__[name] = value
            else:
                e.__dict__.pop(name, None)

    def _ref_entries(self) -> dict[Ref, CatalogEntry]:
        """Each (game, key) the library lists, and the model listed for it (a copy collected into the app and the
        game's own file share a key: the one the lists show)."""
        lib = self.library
        with self.lock:
            kept = getattr(self, "_refs_kept", None)
            if kept is not None and kept[0] == lib.rev:
                return kept[1]
            out: dict[Ref, CatalogEntry] = {}
            for e in lib.all_entries():
                if lib.hides(e, include_lod=True):
                    continue
                ref = self._model_ref(e)
                if ref is not None:
                    out.setdefault(ref, e)
            self._refs_kept = (lib.rev, out)
            return out

    def _chosen(self, args: dict, key: str = "ids") -> list[tuple[Ref, CatalogEntry]]:
        """The library models ``args[key]`` (ids) with their references; an error when there are none."""
        out = []
        for i in args.get(key) or []:
            e = self._entry(str(i))
            ref = self._model_ref(e) if e is not None else None
            if ref is not None:
                out.append((ref, e))
        if not out:
            from .service import AppError

            raise AppError("Choose models in the library first (files opened on their own can't be kept in "
                           "collections or tagged).")
        return out

    def _marks_changed(self, entries: list[CatalogEntry]) -> None:
        """Save the collections file and give the models changed their marks (and the games' indexes)."""
        self.marks.save()
        with self.lock:
            refs = {self._model_ref(e) for e in entries}
            same = [e for e in self.library.all_entries() if self._model_ref(e) in refs]  # copies of them too
            self.library.remark(same)
            for g in {e.game for e in same}:
                self._index_dirty.setdefault(g, 0.0)
        self._flush_index(0.0)

    def _entry_marks(self, e: CatalogEntry) -> dict:
        """What the page shows of a model's marks: its tags, star and the collections it's in."""
        out = {}
        if e.__dict__.get("_tags"):
            out["tags"] = list(e.__dict__["_tags"])
        if e.__dict__.get("_starred"):
            out["starred"] = True
        if e.__dict__.get("_collections"):
            out["collections"] = list(e.__dict__["_collections"])
        return out

    # ------------------------------------------------------------ collections

    def _collection_json(self, c: dict, refs: dict | None = None) -> dict:
        refs = refs if refs is not None else self._ref_entries()
        present = [refs[(m["game"], m["key"])] for m in c["models"] if (m["game"], m["key"]) in refs]
        return {"id": c["id"], "name": c["name"], "count": len(c["models"]), "present": len(present),
                "games": list(dict.fromkeys(e.game for e in present)), "preview": [
                    {"id": e.id, "category": e.category} for e in present[:4]], "created": c.get("created") or ""}

    def cmd_collections(self, args: dict) -> dict:
        """Every collection (its name, how many models it holds, how many of them are in the library now, the
        first four for its tile), the tags in use with how many models have each, and how many are starred."""
        refs = self._ref_entries()
        starred = [r for r in self.marks.starred() if r in refs]
        return {"collections": [self._collection_json(c, refs) for c in self.marks.collections()],
                "tags": [{"name": t, "count": n} for t, n in self.marks.all_tags().items()],
                "starred": len(starred), "starred_preview": [{"id": refs[r].id, "category": refs[r].category}
                                                             for r in starred[:4]]}

    def _need_collection(self, args: dict) -> dict:
        from .service import AppError

        c = self.marks.get(str(args.get("id") or ""))
        if c is None:
            raise AppError("That collection isn't there any more.")
        return c

    def cmd_collection_create(self, args: dict) -> dict:
        """A new collection named ``name``, with the models ``ids`` in it (if any)."""
        from .service import AppError

        chosen = self._chosen(args) if args.get("ids") else []
        try:
            c = self.marks.create(str(args.get("name") or ""), [r for r, _e in chosen])
        except ValueError as exc:
            raise AppError(str(exc)) from exc
        self._marks_changed([e for _r, e in chosen])
        return {"collection": self._collection_json(c)}

    def cmd_collection_rename(self, args: dict) -> dict:
        from .service import AppError

        self._need_collection(args)
        try:
            c = self.marks.rename(str(args["id"]), str(args.get("name") or ""))
        except ValueError as exc:
            raise AppError(str(exc)) from exc
        self.marks.save()
        return {"collection": self._collection_json(c)}

    def cmd_collection_delete(self, args: dict) -> dict:
        """Delete a collection (its models stay in the library, only the list goes)."""
        c = self._need_collection(args)
        refs = self._ref_entries()
        members = [refs[(m["game"], m["key"])] for m in c["models"] if (m["game"], m["key"]) in refs]
        self.marks.delete(c["id"])
        self._marks_changed(members)
        return {"deleted": c["name"]}

    def cmd_collection_add(self, args: dict) -> dict:
        """Add the models ``ids`` to the end of the collection ``id`` (those in it already stay where they are)."""
        c = self._need_collection(args)
        chosen = self._chosen(args)
        added = self.marks.add(c["id"], [r for r, _e in chosen])
        self._marks_changed([e for _r, e in chosen])
        return {"added": added, "collection": self._collection_json(self.marks.get(c["id"]))}

    def cmd_collection_remove(self, args: dict) -> dict:
        c = self._need_collection(args)
        chosen = self._chosen(args)
        removed = self.marks.remove(c["id"], [r for r, _e in chosen])
        self._marks_changed([e for _r, e in chosen])
        return {"removed": removed, "collection": self._collection_json(self.marks.get(c["id"]))}

    def cmd_collection_move(self, args: dict) -> dict:
        """Put the models ``ids`` (in their order) just before the model ``before`` in the collection (none: at
        its end)."""
        c = self._need_collection(args)
        chosen = self._chosen(args)
        before = None
        if args.get("before"):
            e = self._entry(str(args["before"]))
            before = self._model_ref(e) if e is not None else None
        self.marks.move(c["id"], [r for r, _e in chosen], before)
        self.marks.save()
        return {"collection": self._collection_json(self.marks.get(c["id"]))}

    def cmd_models_tag(self, args: dict) -> dict:
        """Add the tags ``add`` to the models ``ids`` and take the tags ``remove`` off them."""
        chosen = self._chosen(args)
        add = [t for t in (clean_tag(x) for x in args.get("add") or []) if t]
        remove = [t for t in (clean_tag(x) for x in args.get("remove") or []) if t]
        self.marks.tag([r for r, _e in chosen], add, remove)
        self._marks_changed([e for _r, e in chosen])
        return {"items": [self._entry_json(e) for _r, e in chosen],
                "tags": [{"name": t, "count": n} for t, n in self.marks.all_tags().items()]}

    def cmd_models_star(self, args: dict) -> dict:
        """Star the models ``ids`` (``star`` false: take their star off)."""
        chosen = self._chosen(args)
        self.marks.star([r for r, _e in chosen], bool(args.get("star", True)))
        self._marks_changed([e for _r, e in chosen])
        return {"items": [self._entry_json(e) for _r, e in chosen]}

    def cmd_tag_rename(self, args: dict) -> dict:
        """Rename a tag on every model that has it (``to`` empty: take it off them all)."""
        old = clean_tag(args.get("tag"))
        changed = self.marks.rename_tag(old, str(args.get("to") or ""))
        with self.lock:
            entries = [e for e in self.library.all_entries() if any(t.lower() == old.lower() for t in e.__dict__.get("_tags") or ())]
        self._marks_changed(entries)
        return {"changed": changed, "tags": [{"name": t, "count": n} for t, n in self.marks.all_tags().items()]}

    def _collection_items(self, cid: str) -> tuple[list[CatalogEntry], int]:
        """The models of a collection that are in the library now, in its order, and how many aren't."""
        from .service import AppError

        c = self.marks.get(cid)
        if c is None:
            raise AppError("That collection isn't there any more.")
        refs = self._ref_entries()
        found = [refs.get((m["game"], m["key"])) for m in c["models"]]
        return [e for e in found if e is not None], sum(1 for e in found if e is None)

    # ------------------------------------------------------------ related models

    def _config_links(self) -> dict[str, list[CatalogEntry]]:
        """{model file (normcase): the configurable models built with it, as their base or one of their parts}."""
        lib = self.library
        with self.lock:
            kept = getattr(self, "_links_kept", None)
            if kept is not None and kept[0] == lib.rev:
                return kept[1]
            out: dict[str, list[CatalogEntry]] = {}
            for c in lib.configurations:
                files = [c.config.base.file] + [o.model.file for s in c.config.slots for o in s.options]
                for f in dict.fromkeys(os.path.normcase(str(f)) for f in files):
                    out.setdefault(f, []).append(c)
            self._links_kept = (lib.rev, out)
            return out

    def _related(self, e: CatalogEntry) -> list[tuple[str, CatalogEntry]]:
        """The models that go with ``e``: what its importer says (the rest of an armour set, a character's face and
        hair...), the parts a configurable model is made of, and the configurable models a model is part of."""
        from ..importers import IMPORTERS

        lib = self.library
        out: list[tuple[str, CatalogEntry]] = []
        with self.lock:
            if e.config is not None:
                cfg = e.config
                base = [x for x in lib.with_path(Path(cfg.base.file))]
                out += [("Its base model", x) for x in base[:1]]
                seen = {Path(cfg.base.file)}
                for slot in cfg.slots:
                    for o in slot.options:
                        p = Path(o.model.file)
                        if p in seen:
                            continue
                        seen.add(p)
                        out += [(f"A part: {slot.label}", x) for x in lib.with_path(p)[:1]]
            else:
                imp = next((i for i in IMPORTERS if i.key == e.importer), None)
                if imp is not None:
                    try:
                        out += imp.related(e, lib.game_entries(e.game))
                    except Exception:  # noqa: BLE001 - only the relations of that importer lost
                        pass
                for c in self._config_links().get(os.path.normcase(str(e.path)), []):
                    if c.game == e.game:
                        out.append(("Configurable model it's part of", c))
            seen_ids = {e.id}
            shown = []
            for rel, x in out:
                if x.id in seen_ids or lib.hides(x, include_lod=True):
                    continue
                seen_ids.add(x.id)
                shown.append((rel, x))
        return shown

    def cmd_model_related(self, args: dict) -> dict:
        """The models that go with the library model ``id`` (see _related): [{relation, ...the model}]."""
        e = self._entry(str(args.get("id") or ""))
        if e is None:
            return {"items": []}
        collected = self._collected_roots()
        return {"items": [{"relation": rel, **self._entry_json(x, collected)} for rel, x in self._related(e)]}

    # ------------------------------------------------------------ each game's problems

    def cmd_problems(self, args: dict) -> dict:
        """What needs a look in a game's library, with what fixes it: its folders' notes (still packed, gone,
        nothing found), models that couldn't be read, models without textures, textures not found, animations
        that couldn't be read and model files gone since the scan. Models are known to have a problem once
        their preview was drawn or they were opened (``unknown``: how many weren't yet)."""
        game = str(args.get("game") or "")
        with self.lock:
            entries = [e for e in self.library.entries if e.game == game and not self.library.hides(e)]
            folders = [f for g in self.library.overview() if g["name"] == game for f in g["folders"]]
            collected = self._collected_roots()
        groups = {k: [] for k in ("unreadable", "missing", "untextured", "textures", "animations")}
        unknown = 0
        started = time.monotonic()
        checked = 0
        for e in entries:
            st = self.index.stats(game, e.id) or clean_stats(e.stats) or {}
            if not st:
                unknown += 1
            if st.get("error"):
                groups["unreadable"].append((e, st["error"]))
            elif st.get("textures") == 0:
                groups["untextured"].append((e, None))
            elif st.get("missing_textures"):
                n = st["missing_textures"]
                groups["textures"].append((e, f"{n} texture{'s' if n != 1 else ''} it names weren't found"))
            if st.get("anim_error"):
                groups["animations"].append((e, st["anim_error"]))
            if time.monotonic() - started < MISSING_CHECK_SECONDS:
                checked += 1
                if not _present(e.path):
                    groups["missing"].append((e, "Its file isn't there any more"))
        titles = {
            "unreadable": ("Models that couldn't be read", "Their files are damaged or in a form Dark STL doesn't read yet. "
                           "Export model files… makes a zip of a model's files and a report, to send for a look."),
            "missing": ("Missing files", "Files that were there when the folder was scanned. Rescan the folder to "
                        "list what's there now."),
            "untextured": ("Models without textures", "They show gray. Choose texture… picks one by name from the "
                           "game's textures; Find textures… adds a folder where the game keeps them."),
            "textures": ("Textures not found", "Parts of these models name textures that weren't found in the game's "
                         "folder. Find textures… adds a folder where they are; Choose texture… picks one by name."),
            "animations": ("Animations that couldn't be read", "They can't be posed until these are read. Export "
                           "model files… sends the files for a look."),
        }
        out = []
        for key, rows in groups.items():
            if not rows:
                continue
            out.append({"id": key, "title": titles[key][0], "hint": titles[key][1], "count": len(rows),
                        "models": [{**self._entry_json(e, collected), **({"why": why} if why else {})}
                                   for e, why in rows[:PROBLEM_ROWS]]})
        notes = [{"path": f["path"], "notes": f["notes"], "missing": f["missing"], "count": f["count"]}
                 for f in folders if f["notes"] or f["missing"]]
        return {"game": game, "folders": notes, "groups": out, "unknown": unknown, "models": len(entries),
                "checked": checked, "count": len(notes) + sum(g["count"] for g in out)}

    def _note_problem(self, entry: CatalogEntry | None, **found) -> None:
        """Keep a problem found with a model (``error``: it couldn't be read; ``anim_error``: its animations
        couldn't), with what else is known of it."""
        if entry is None or entry.config is not None:
            return
        known = dict(self.index.stats(entry.game, entry.id) or {})
        if all(known.get(k) == v for k, v in found.items()):
            return
        if self.index.put(entry.game, entry.id, {**known, **found}):
            self._index_dirty.setdefault(entry.game, time.monotonic())


def _present(path: Path) -> bool:
    """Whether a model's file is still there (a model inside an archive: the archive)."""
    p = Path(path)
    for _ in range(4):
        try:
            os.stat(p)
            return True
        except OSError:
            parent = p.parent
            if parent == p:
                return False
            p = parent
            if p.is_dir():  # the folder is there but the file isn't
                return False
    return True  # (deep inside an archive: not checked)


__all__ = ["OrganiseCommands", "clean_name"]
