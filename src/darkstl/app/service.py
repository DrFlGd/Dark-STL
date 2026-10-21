"""The app's commands: everything the page asks the Python side to do.

Each command takes a dict of arguments and returns what JSON can carry, or
bytes for meshes and pictures. The HTTP server (server.py) only routes
requests here, so the commands can be tested without a browser. Long work
(scanning a game folder, enhancing, exporting) runs as a job the page follows.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .. import __version__
from ..export import FORMATS, ExportOptions, export_mesh
from ..geometry.mesh import TriMesh
from ..library import Library, detect_game, folder_relation, known_games
from ..model import CatalogEntry, MeshOptions, SourceModel, path_id
from ..thumbnails import ThumbnailCache
from .config import Config
from .meshpack import pack_mesh

UNITS = {"mm": 1000.0, "m": 1.0, "cm": 100.0, "in": 39.3701}


class AppError(Exception):
    """A problem to show the user as it is (no traceback)."""


class Host:
    """What the window around the page can do: native dialogs, showing files. Overridden by desktop.py."""

    dialogs = False

    def pick_folder(self, title: str, start: str | None = None) -> str | None:
        return None

    def pick_file(self, title: str, patterns: list[str], start: str | None = None) -> str | None:
        return None

    def open_path(self, path: str) -> None:
        reveal(path)


def reveal(path: str) -> None:
    """Show a file in its folder (or open a folder) in the system's file manager."""
    p = Path(path)
    if sys.platform == "win32":
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))  # noqa: S606 - a folder the user asked to see
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])


# --------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    label: str
    state: str = "running"  # running | done | failed
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None
    started: float = field(default_factory=time.time)

    def report(self, frac: float, message: str = "") -> None:
        self.progress = float(min(max(frac, 0.0), 1.0))
        if message:
            self.message = message

    def as_json(self) -> dict:
        return {"id": self.id, "label": self.label, "state": self.state, "progress": round(self.progress, 4),
                "message": self.message, "result": self.result, "error": self.error, "started": self.started}


class Jobs:
    def __init__(self, keep: int = 64):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def start(self, label: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(uuid.uuid4().hex[:12], label)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._keep:
                oldest = next(iter(self._jobs.values()))
                if oldest.state == "running":
                    break
                self._jobs.popitem(last=False)

        def run():
            try:
                job.result = fn(job)
                job.progress = 1.0
                job.state = "done"
            except AppError as exc:
                job.error, job.state = str(exc), "failed"
            except Exception as exc:  # noqa: BLE001 - reported to the page
                traceback.print_exc()
                job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"

        threading.Thread(target=run, name=f"job-{label}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self) -> list[Job]:
        return [j for j in list(self._jobs.values()) if j.state == "running"]


# ------------------------------------------------------------------ models

@dataclass
class Session:
    """A model open on a page: the file, the last mesh built from it and the last enhanced result."""

    sid: str
    model: SourceModel
    entry: CatalogEntry | None
    result: Any = None  # EnhanceResult
    result_key: tuple | None = None
    mesh_key: tuple | None = None
    mesh: TriMesh | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def build(self, options: MeshOptions) -> TriMesh:
        key = _options_key(options)
        with self.lock:
            if self.mesh is None or self.mesh_key != key:
                self.mesh, self.mesh_key = self.model.build_mesh(options), key
            return self.mesh


def _options_key(options: MeshOptions) -> tuple:
    return (None if options.parts is None else tuple(sorted(options.parts)), tuple(sorted(options.flags.items())))


def mesh_options(args: dict) -> MeshOptions:
    parts = args.get("parts")
    flags = args.get("flags") or {}
    return MeshOptions(None if parts is None else {int(p) for p in parts}, {str(k): bool(v) for k, v in flags.items()})


def entry_json(e: CatalogEntry) -> dict:
    return {"id": e.id, "label": e.label, "stem": e.stem, "game": e.game, "category": e.category, "group": e.group,
            "packed": e.packed, "lod": e.is_lod, "importer": e.importer, "file": str(e.path), "name": e.path.name}


def model_json(m: SourceModel, mesh: TriMesh) -> dict:
    return {
        "name": m.name, "file": str(m.path), "game": m.game, "importer": m.importer,
        "variant": m.variant, "variants": m.variants,
        "parts": [{"name": p.name, "material": p.material, "vertices": p.vertices, "triangles": p.triangles}
                  for p in m.parts],
        "options": [{"key": o.key, "label": o.label, "default": o.default, "tooltip": o.tooltip} for o in m.options],
        "materials": [{"name": x.name, "albedo": x.albedo, "normal": x.normal, "height": x.height,
                       "guessed": x.guessed, "details": x.details} for x in m.materials],
        "textures": [{"key": k, "name": t.name, "source": t.source} for k, t in sorted(m.textures.items())],
        "info": m.info, "notes": m.notes, "stats": mesh_stats(mesh),
    }


def mesh_stats(mesh: TriMesh) -> dict:
    lo, hi = mesh.bounds() if mesh.n_vertices else (np.zeros(3), np.zeros(3))
    return {"vertices": mesh.n_vertices, "triangles": mesh.n_faces, "area_m2": float(mesh.surface_area()),
            "size_mm": ((hi - lo) * 1000).round(2).tolist()}


# ------------------------------------------------------------------ service

class Service:
    BYTES = {"model_mesh", "texture", "thumb"}

    def __init__(self, config: Config, cache: Path, host: Host | None = None):
        self.config = config
        self.host = host or Host()
        self.library = Library()
        self.jobs = Jobs()
        self.thumbs = ThumbnailCache(Path(cache) / "previews")
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.opened: dict[str, Path] = {}  # model files opened on their own, by id
        self.lock = threading.RLock()
        self.scanning = 0
        self.textures_png: OrderedDict[tuple, bytes] = OrderedDict()
        if config.oodle:
            self._use_oodle(config.oodle)

    # ------------------------------------------------------------ routing

    def call(self, cmd: str, args: dict | None = None) -> Any:
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            raise AppError(f"Unknown command '{cmd}'")
        return fn(args or {})

    # ------------------------------------------------------------ the app

    def cmd_app_info(self, args: dict) -> dict:
        from ..geometry.remesh import acvd_available

        return {
            "version": __version__,
            "games": known_games(),
            "formats": [{"ext": ext, "label": label} for ext, label in FORMATS.items()],
            "methods": [{"id": "subdivide", "label": "Adaptive subdivision (exact UVs, keeps sharp edges)",
                         "available": True},
                        {"id": "isotropic", "label": "Isotropic remesh (exact count, uniform triangles)",
                         "available": acvd_available(), "missing": "needs pyacvd"}],
            "open_patterns": _dialog_patterns(),
            "first_run": not self.config.game_folders,
            "dialogs": bool(self.host.dialogs),
            "platform": sys.platform,
        }

    def cmd_prefs_get(self, args: dict) -> dict:
        return dict(self.config.prefs)

    def cmd_prefs_set(self, args: dict) -> dict:
        self.config.prefs = dict(args.get("prefs") or {})
        self.config.save()
        return {}

    def cmd_settings_get(self, args: dict) -> dict:
        from ..importers.fromsoft.formats import oodle

        found = oodle.located()
        return {"texture_dirs": list(self.config.texture_dirs), "oodle": self.config.oodle,
                "oodle_found": str(found) if found else None, "export_dir": str(self.config.export_folder),
                "previews": str(self.thumbs.folder), "settings_file": str(self.config.path or "")}

    def cmd_settings_set(self, args: dict) -> dict:
        if "texture_dirs" in args:
            self.config.texture_dirs = [str(Path(d)) for d in args["texture_dirs"] if str(d).strip()]
            self._forget_models()
        if "oodle" in args:
            path = str(args["oodle"] or "").strip()
            if path and not Path(path).exists():
                raise AppError(f"There is no file or folder at {path}")
            self.config.oodle = path
            if path:
                self._use_oodle(path)
        if "export_dir" in args:
            self.config.export_dir = str(args["export_dir"] or "").strip()
        self.config.save()
        return self.cmd_settings_get({})

    def _use_oodle(self, path: str) -> None:
        from ..importers.fromsoft.formats import oodle

        oodle.add_search_dir(path)

    def cmd_pick_folder(self, args: dict) -> dict:
        return {"path": self.host.pick_folder(args.get("title") or "Choose a folder", args.get("start"))}

    def cmd_pick_file(self, args: dict) -> dict:
        return {"path": self.host.pick_file(args.get("title") or "Choose a file", list(args.get("patterns") or []),
                                            args.get("start"))}

    def cmd_open_path(self, args: dict) -> dict:
        path = str(args.get("path") or "")
        if not path or not Path(path).exists():
            raise AppError(f"{path or 'That file'} isn't there any more.")
        self.host.open_path(path)
        return {}

    def cmd_job(self, args: dict) -> dict:
        job = self.jobs.get(str(args.get("id")))
        if job is None:
            raise AppError("That job is no longer known (the app was restarted?)")
        return job.as_json()

    def cmd_jobs(self, args: dict) -> list:
        return [j.as_json() for j in self.jobs.running()]

    # ------------------------------------------------------------ library

    def load_folders(self) -> Job | None:
        """Scan every saved game folder (at start-up)."""
        folders = [(Path(f["path"]), f.get("game") or None) for f in self.config.game_folders]
        if not folders:
            return None

        def run(job: Job):
            lib = Library()
            for i, (path, game) in enumerate(folders):
                job.report(i / len(folders), f"Reading {path.name}")
                if path.is_dir():
                    lib.add_root(path, game)
                else:
                    lib.roots[path] = game or path.name
                    lib.notes[path] = [f"This folder isn't there any more: {path}"]
            with self.lock:
                self.library.merge(lib)  # keeps a folder added while this ran
            return {"games": len(lib.games()), "models": len(lib.entries)}

        return self._scan_job("Reading game folders", run)

    def _scan_job(self, label: str, fn: Callable[[Job], Any]) -> Job:
        with self.lock:
            self.scanning += 1

        def run(job: Job):
            try:
                return fn(job)
            finally:
                with self.lock:
                    self.scanning -= 1

        return self.jobs.start(label, run)

    def cmd_overview(self, args: dict) -> dict:
        with self.lock:
            games = self.library.overview(bool(args.get("lod")))
            return {"scanning": self.scanning > 0, "games": games, "total": sum(g["count"] for g in games)}

    def cmd_folder_check(self, args: dict) -> dict:
        raw = str(args.get("path") or "").strip().strip('"')
        if not raw:
            raise AppError("Choose a folder first.")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        with self.lock:
            roots = dict(self.library.roots)
        outer, inner = folder_relation(path, roots)
        return {"path": str(path), "exists": path.is_dir(), "game": detect_game(path) if path.is_dir() else None,
                "games": known_games(), "added": path in roots, "inside": str(outer) if outer else None,
                "inside_game": roots.get(outer) if outer else None, "contains": [str(p) for p in inner]}

    def cmd_folder_add(self, args: dict) -> dict:
        check = self.cmd_folder_check(args)
        path = Path(check["path"])
        if not check["exists"]:
            raise AppError(f"There is no folder at {path}")
        if check["inside"]:
            raise AppError(f"That folder is already in the library, as part of {check['inside_game']} "
                           f"({check['inside']}).")
        game = str(args.get("game") or check["game"] or path.name).strip()
        return {"job": self._scan(path, game, f"Scanning {path.name}").id}

    def cmd_folder_rescan(self, args: dict) -> dict:
        path = Path(str(args.get("path") or ""))
        with self.lock:
            game = self.library.roots.get(path)
        if game is None:
            raise AppError(f"{path} isn't one of the game folders.")
        if not path.is_dir():
            raise AppError(f"This folder isn't there any more: {path}")
        return {"job": self._scan(path, game, f"Rescanning {path.name}").id}

    def _scan(self, path: Path, game: str, label: str) -> Job:
        def run(job: Job):
            lib = Library()
            lib.add_root(path, game, lambda i, name: job.report(0.0, f"{i:,} files: {name}"))
            with self.lock:
                for inner in folder_relation(path, self.library.roots)[1]:  # now part of this folder
                    self.library.remove_root(inner)
                self.library.merge(lib)
                self._save_folders()
            count = sum(1 for e in lib.entries if not lib.hides(e))  # as the library lists them
            return {"game": game, "path": str(path), "count": count, "notes": lib.notes.get(path, [])}

        return self._scan_job(label, run)

    def cmd_folder_remove(self, args: dict) -> dict:
        path = Path(str(args.get("path") or ""))
        with self.lock:
            if path not in self.library.roots:
                raise AppError(f"{path} isn't one of the game folders.")
            self.library.remove_root(path)
            self._save_folders()
        return {}

    def cmd_folder_set_game(self, args: dict) -> dict:
        path = Path(str(args.get("path") or ""))
        game = str(args.get("game") or "").strip()
        if not game:
            raise AppError("Choose a game.")
        with self.lock:
            if path not in self.library.roots:
                raise AppError(f"{path} isn't one of the game folders.")
            self.library.set_game(path, game)
            self._save_folders()
        return {}

    def _save_folders(self) -> None:
        self.config.game_folders = [{"path": str(p), "game": g} for p, g in self.library.roots.items()]
        self.config.save()

    def cmd_models_query(self, args: dict) -> dict:
        with self.lock:
            items = self.library.query(args.get("game") or None, args.get("category") or None,
                                       args.get("group") if args.get("group") is not None else None,
                                       str(args.get("q") or ""), bool(args.get("lod")))
        if args.get("sort") == "file":
            items = sorted(items, key=lambda e: e.path.name.lower())
        offset = max(int(args.get("offset") or 0), 0)
        limit = int(args.get("limit") or 500)
        return {"total": len(items), "items": [entry_json(e) for e in items[offset:offset + limit]]}

    def cmd_model_entry(self, args: dict) -> dict:
        entry = self._entry(str(args.get("id") or ""))
        if entry is None:
            path = self.opened.get(str(args.get("id") or ""))
            if path is None:
                raise AppError("That model isn't in the library any more.")
            return {"id": path_id(path), "label": path.name, "stem": path.stem, "game": None, "category": None,
                    "group": None, "packed": True, "lod": False, "importer": "", "file": str(path), "name": path.name}
        return entry_json(entry)

    def _entry(self, entry_id: str) -> CatalogEntry | None:
        with self.lock:
            return self.library.find(entry_id)

    # ------------------------------------------------------------ models

    def cmd_model_open(self, args: dict) -> dict:
        """Open a library model (``id``) or any model file (``path``); ``variant`` picks a model inside it."""
        from ..importers import open_model

        entry = None
        if args.get("path"):
            path = Path(str(args["path"]))
            if not path.is_file():
                raise AppError(f"There is no file at {path}")
            model_id = path_id(path)
            self.opened[model_id] = path
        else:
            model_id = str(args.get("id") or "")
            entry = self._entry(model_id)
            path = entry.path if entry else self.opened.get(model_id)
            if path is None:
                raise AppError("That model isn't in the library any more (was its folder removed?)")
        variant = args.get("variant") or None
        sid = f"{model_id}|{variant or ''}"
        with self.lock:
            session = self.sessions.get(sid)
        if session is None:
            model = open_model(path, variant, tuple(self.config.texture_dirs))
            session = Session(sid, model, entry)
            with self.lock:
                self.sessions[sid] = session
                while len(self.sessions) > 6:
                    self.sessions.popitem(last=False)
        else:
            with self.lock:
                self.sessions.move_to_end(sid)
        mesh = session.build(MeshOptions())
        out = model_json(session.model, mesh)
        out.update({"sid": sid, "id": model_id, "entry": entry_json(entry) if entry else None,
                    "enhanced": self._result_json(session) if session.result is not None else None})
        return out

    def _session(self, args: dict) -> Session:
        sid = str(args.get("sid") or "")
        with self.lock:
            session = self.sessions.get(sid)
        if session is None:
            raise AppError("This model was closed; open it again.")
        return session

    def cmd_model_stats(self, args: dict) -> dict:
        session = self._session(args)
        return mesh_stats(session.build(mesh_options(args)))

    def cmd_model_mesh(self, args: dict) -> bytes:
        session = self._session(args)
        uv = str(args.get("uv", "1")) not in ("0", "false", "")
        if args.get("which") == "enhanced":
            if session.result is None:
                raise AppError("This model hasn't been enhanced yet.")
            return pack_mesh(session.result.displaced, uv, extra={"which": "enhanced"})
        return pack_mesh(session.build(mesh_options(_parse_lists(args))), uv, extra={"which": "original"})

    def cmd_texture(self, args: dict) -> bytes:
        session = self._session(args)
        key = str(args.get("key") or "")
        tex = session.model.textures.get(key)
        if tex is None:
            raise AppError(f"No texture '{key}' in this model.")
        size = int(args.get("max") or 2048)
        cache_key = (session.sid, key, size)
        with self.lock:
            if cache_key in self.textures_png:
                return self.textures_png[cache_key]
        data = texture_png(tex.decode(), size)
        with self.lock:
            self.textures_png[cache_key] = data
            while len(self.textures_png) > 48:
                self.textures_png.popitem(last=False)
        return data

    def cmd_texture_info(self, args: dict) -> dict:
        session = self._session(args)
        tex = session.model.textures.get(str(args.get("key") or ""))
        if tex is None:
            raise AppError("No such texture in this model.")
        try:
            return {"name": tex.name, "source": tex.source, "info": tex.describe()}
        except Exception as exc:  # noqa: BLE001 - shown next to the texture
            return {"name": tex.name, "source": tex.source, "info": f"can't be read: {exc}"}

    def cmd_texture_save(self, args: dict) -> dict:
        from ..export import write_png

        session = self._session(args)
        tex = session.model.textures.get(str(args.get("key") or ""))
        if tex is None:
            raise AppError("No such texture in this model.")
        folder = Path(args.get("folder") or self.config.export_folder)
        path = _free_name(folder / f"{Path(tex.name).stem}.png")
        folder.mkdir(parents=True, exist_ok=True)
        write_png(tex.decode(), path)
        return {"path": str(path)}

    def cmd_thumb(self, args: dict) -> bytes | None:
        model_id = str(args.get("id") or "")
        entry = self._entry(model_id)
        path = entry.path if entry else self.opened.get(model_id)
        if path is None:
            return None
        try:
            return self.thumbs.get(path, None, tuple(self.config.texture_dirs))
        except Exception:  # noqa: BLE001 - a model that can't be drawn just has no preview
            return None

    def cmd_thumbs_clear(self, args: dict) -> dict:
        return {"removed": self.thumbs.clear()}

    # ------------------------------------------------------------ enhance and export

    def cmd_enhance(self, args: dict) -> dict:
        from ..pipeline import EnhanceSettings, enhance

        session = self._session(args)
        options = mesh_options(args)
        s = args.get("settings") or {}
        settings = EnhanceSettings(
            target_vertices=int(s.get("target_vertices", 1_000_000)),
            method=str(s.get("method", "subdivide")),
            smooth=0.5 if s.get("smooth", True) else 0.0,
            source=str(s.get("source", "normal")),
            strength=float(s.get("strength", 1.0)),
            height_range_mm=float(s.get("height_range_mm", 1.0)),
            green=str(s.get("green", "auto")),
            detail_fraction=float(s.get("detail_fraction", 1 / 16)),
            max_displacement_mm=float(s["max_displacement_mm"]) if s.get("max_displacement_mm") else None,
            texture_overrides={int(k): str(v or "") for k, v in (s.get("texture_overrides") or {}).items()},
            mesh_options=options,
        )
        if settings.target_vertices < 1000:
            raise AppError("Choose at least 1,000 vertices.")

        def run(job: Job):
            result = enhance(session.model, settings, job.report)
            session.result, session.result_key = result, _options_key(options)
            return self._result_json(session)

        name = session.model.variant or session.model.name
        return {"job": self.jobs.start(f"Enhancing {name}", run).id}

    def _result_json(self, session: Session) -> dict:
        r = session.result
        st = r.stats()
        model = session.model
        return {"stats": st, "size_mm": ((r.displaced.bounds()[1] - r.displaced.bounds()[0]) * 1000).round(2).tolist(),
                "fields": [{"material": model.materials[m].name if m < len(model.materials) else str(m),
                            "texture": model.textures[f.texture].name if f.texture in model.textures else f.texture,
                            "convention": f.field.convention, "texel_mm": f.texel_size * 1000}
                           for m, f in r.fields.items()],
                "warnings": list(r.warnings)}

    def cmd_export(self, args: dict) -> dict:
        session = self._session(args)
        which = args.get("which", "enhanced")
        if which == "enhanced":
            if session.result is None:
                raise AppError("Enhance the model first, or export the original.")
            mesh = session.result.displaced
        else:
            mesh = session.build(mesh_options(args))
        if mesh.n_faces == 0:
            raise AppError("There's nothing to export: no parts are switched on.")
        ext = str(args.get("format") or ".stl").lower()
        if ext not in FORMATS:
            raise AppError(f"Can't export {ext} files.")
        scale = UNITS.get(str(args.get("units") or "mm"), 1000.0)
        folder = Path(args.get("folder") or self.config.export_folder)
        base = _safe_name(str(args.get("name") or Path(session.model.variant or session.model.name).stem))
        model = session.model

        def run(job: Job):
            opts = ExportOptions(scale=scale, material_names=[m.name for m in model.materials])
            if args.get("textures", True) and ext in (".obj", ".glb"):
                opts.textures = _albedo(model, mesh)
            job.report(0.2, "Writing")
            path = export_mesh(mesh, _free_name(folder / f"{base}{ext}"), opts)
            return {"path": str(path), "bytes": path.stat().st_size}

        return {"job": self.jobs.start(f"Exporting {base}{ext}", run).id}

    def _forget_models(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.textures_png.clear()


def _dialog_patterns() -> list[str]:
    """The importers' file filters as the window's open dialog takes them: "Name (*.a;*.b)"."""
    from ..importers import IMPORTERS

    out = []
    for imp in IMPORTERS:
        name, _, globs = imp.file_filter.partition("(")
        if globs:
            out.append(f"{name.strip()} ({';'.join(globs.rstrip(')').split())})")
    return out + ["All files (*.*)"]


def _parse_lists(args: dict) -> dict:
    """Mesh options as they come in a URL: parts=0,2 and flags=bones:1,mm:0."""
    out = dict(args)
    if isinstance(args.get("parts"), str):
        out["parts"] = [int(p) for p in args["parts"].split(",") if p != ""] if args["parts"] != "all" else None
    if isinstance(args.get("flags"), str):
        out["flags"] = {k: v == "1" for k, _, v in (f.partition(":") for f in args["flags"].split(",") if f)}
    return out


def _albedo(model: SourceModel, mesh: TriMesh) -> dict[int, np.ndarray]:
    out = {}
    for m in np.unique(mesh.face_material):
        m = int(m)
        if m < len(model.materials) and model.materials[m].albedo in model.textures:
            try:
                out[m] = model.textures[model.materials[m].albedo].decode()
            except Exception:  # noqa: BLE001 - exported without that texture
                pass
    return out


def texture_png(img: np.ndarray, max_size: int) -> bytes:
    from PIL import Image

    arr = img if img.dtype == np.uint8 else np.clip(img * 255, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.dstack([arr, arr, arr])
    pil = Image.fromarray(np.ascontiguousarray(arr[..., :4] if arr.shape[2] >= 4 else arr[..., :3]))
    if max(pil.size) > max_size:
        pil.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    pil.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def _safe_name(name: str) -> str:
    cleaned = "".join("-" if c in '<>:"/\\|?*' else c for c in name).strip().rstrip(". ")
    return cleaned or "model"


def _free_name(path: Path) -> Path:
    """``path``, or ``name (2).ext`` and so on if that file exists already."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path
