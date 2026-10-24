"""The app's commands: everything the page asks the Python side to do.

Each command takes a dict of arguments and returns what JSON can carry, or
bytes for meshes and pictures. The HTTP server (server.py) only routes
requests here, so the commands can be tested without a browser. Long work
(scanning a game folder, enhancing, exporting) runs as a job the page follows.
"""

from __future__ import annotations

import hashlib
import io
import json
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
from .. import collect
from ..export import FORMATS, ExportOptions, export_mesh
from ..geometry.mesh import TriMesh
from ..library import Library, detect_game, folder_relation, known_games
from ..model import CatalogEntry, MeshOptions, SourceModel, path_id
from ..thumbnails import ThumbnailCache
from .config import Config, models_dir
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

class Stopped(BaseException):
    """Raised in a job's thread at its next progress report once the user has stopped it. Not an
    Exception, so that nothing on the way catches it as a failure."""


@dataclass
class Job:
    id: str
    label: str
    state: str = "running"  # running | done | failed | stopped
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    stoppable: bool = False
    stopping: bool = False

    def report(self, frac: float, message: str = "") -> None:
        self.check()
        self.progress = float(min(max(frac, 0.0), 1.0))
        if message:
            self.message = message

    def check(self) -> None:
        """Stop here if the user has asked to (call before keeping a result)."""
        if self.stopping:
            raise Stopped()

    def as_json(self) -> dict:
        return {"id": self.id, "label": self.label, "state": self.state, "progress": round(self.progress, 4),
                "message": self.message, "result": self.result, "error": self.error, "started": self.started,
                "stoppable": self.stoppable and self.state == "running", "stopping": self.stopping}


class Jobs:
    def __init__(self, keep: int = 64):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def start(self, label: str, fn: Callable[[Job], Any], stoppable: bool = False) -> Job:
        """Run ``fn`` in a thread of its own. A ``stoppable`` job can be stopped (:meth:`stop`): it ends
        at its next progress report, and what it was making is dropped."""
        job = Job(uuid.uuid4().hex[:12], label, stoppable=stoppable)
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
            except Stopped:
                job.error, job.state = "Stopped", "stopped"
            except AppError as exc:
                job.error, job.state = str(exc), "failed"
            except Exception as exc:  # noqa: BLE001 - reported to the page
                traceback.print_exc()
                job.error, job.state = f"{type(exc).__name__}: {exc}", "failed"

        threading.Thread(target=run, name=f"job-{label}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None and job.stoppable and job.state == "running":
            job.stopping = True
            job.message = "Stopping…"
        return job

    def running(self) -> list[Job]:
        return [j for j in list(self._jobs.values()) if j.state == "running"]


# ------------------------------------------------------------------ models

@dataclass
class Session:
    """A model open on a page: the file, the last mesh built from it and the enhanced model shown."""

    sid: str
    model: SourceModel
    entry: CatalogEntry | None
    result: Any = None  # EnhanceResult
    result_id: str = ""  # names the result for the page, which keeps the meshes it has shown
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
    return (None if options.parts is None else tuple(sorted(options.parts)), tuple(sorted(options.flags.items())),
            options.pose)


def generated_key(session: Session, options: MeshOptions, settings: dict) -> tuple:
    """What a generated model depends on: the model file as it is now, its parts and options, and every
    setting the page sent (all of them, so settings added later count without being listed here)."""
    settings_key = json.dumps(settings, sort_keys=True, default=str)
    return session.sid, _file_id(session.model.path), _options_key(options), settings_key


def mesh_key(session: Session, options: MeshOptions, settings) -> tuple:
    """What step 1's evened-out mesh depends on: the model file, its parts, options and pose, and the
    step-1 settings (pipeline.MESH_SETTINGS) alone."""
    from ..pipeline import MESH_SETTINGS

    return (session.sid, _file_id(session.model.path), _options_key(options),
            tuple(getattr(settings, k) for k in MESH_SETTINGS))


def _file_id(path: Path) -> tuple:
    try:
        st = Path(path).stat()
    except OSError:
        return (str(path),)
    return str(path), st.st_size, st.st_mtime_ns


def mesh_options(args: dict) -> MeshOptions:
    parts = args.get("parts")
    flags = args.get("flags") or {}
    return MeshOptions(None if parts is None else {int(p) for p in parts}, {str(k): bool(v) for k, v in flags.items()},
                       parse_pose(args.get("pose")))


def parse_pose(value) -> tuple[str, int] | None:
    """A pose as the page sends it: ``["a000_003000", 12]`` in JSON or ``a000_003000@12`` in a URL."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        name, _, frame = value.rpartition("@")
        if not name:
            raise AppError("A pose is written as animation@frame.")
        value = [name, frame]
    try:
        name, frame = value
        return str(name), int(frame)
    except (TypeError, ValueError):
        raise AppError("A pose is an animation name and a frame number.") from None


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
                       "specular": x.specular, "detail_normal": x.detail_normal,
                       "guessed": x.guessed, "details": x.details} for x in m.materials],
        "textures": [{"key": k, "name": t.name, "source": t.source} for k, t in sorted(m.textures.items())],
        "info": m.info, "notes": m.notes, "stats": mesh_stats(mesh), "animated": m.animations is not None,
    }


def mesh_stats(mesh: TriMesh) -> dict:
    lo, hi = mesh.bounds() if mesh.n_vertices else (np.zeros(3), np.zeros(3))
    return {"vertices": mesh.n_vertices, "triangles": mesh.n_faces, "area_m2": float(mesh.surface_area()),
            "size_mm": ((hi - lo) * 1000).round(2).tolist()}


# ------------------------------------------------------------------ service

class Service:
    BYTES = {"model_mesh", "texture", "thumb"}
    KEEP_GENERATED = 3  # enhanced models kept, so generating one again with the same settings is instant
    KEEP_MESHES = 2  # evened-out meshes kept, so other displacement settings are tried without making them again

    def __init__(self, config: Config, cache: Path, host: Host | None = None):
        self.config = config
        self.host = host or Host()
        self.library = Library()
        self.jobs = Jobs()
        self.thumbs = ThumbnailCache(Path(cache) / "previews")
        from ..importers import set_cache_folder

        set_cache_folder(Path(cache) / "scans")  # the texture index of each game folder, so rescans are quick
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.opened: dict[str, Path] = {}  # model files opened on their own, by id
        self.lock = threading.RLock()
        self.scanning = 0
        self.textures_png: OrderedDict[tuple, bytes] = OrderedDict()
        self.generated: OrderedDict[tuple, Any] = OrderedDict()  # EnhanceResult by generated_key(), newest last
        self.meshes: OrderedDict[tuple, Any] = OrderedDict()  # step 1's evened-out meshes (Resampled) by mesh_key()
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
            "fast_solver": _importable("pyamg"),
            "ai_runtime": _importable("onnxruntime"),
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
        lib = self.config.library_folder
        files, size = collect.folder_size(lib) if lib.is_dir() else (0, 0)
        return {"texture_dirs": list(self.config.texture_dirs), "oodle": self.config.oodle,
                "oodle_found": str(found) if found else None, "export_dir": str(self.config.export_folder),
                "library_dir": str(lib), "library_files": files, "library_bytes": size,
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
                self.thumbs.clear(only_failed=True)  # models that needed it can be drawn now
        if "export_dir" in args:
            self.config.export_dir = str(args["export_dir"] or "").strip()
        if "library_dir" in args:
            self.config.library_dir = str(args["library_dir"] or "").strip()
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

    def cmd_ai_models(self, args: dict) -> dict:
        """The AI models the user has put in the models folder (made on first ask, so it can be opened)."""
        from ..textures import upscale

        folder = models_dir()
        folder.mkdir(parents=True, exist_ok=True)
        bundled = [{"name": m["name"], "description": m.get("description", ""), "codecs": m.get("codecs", [])}
                   for m in upscale.bundled_models()]
        return {"folder": str(folder), "models": upscale.list_models(folder), "bundled": bundled,
                "available": upscale.available()}

    def cmd_ai_model_add(self, args: dict) -> dict:
        """Copy a picked .onnx model into the models folder."""
        import shutil

        src = Path(str(args.get("path") or ""))
        if src.suffix.lower() != ".onnx" or not src.is_file():
            raise AppError("Choose an .onnx model file (convert .pth models with chaiNNer first).")
        folder = models_dir()
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, folder / src.name)
        return self.cmd_ai_models({})

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

    def cmd_job_stop(self, args: dict) -> dict:
        """Stop a job (generating, exporting, scanning, collecting): it ends at its next step."""
        job = self.jobs.get(str(args.get("id")))
        if job is None:
            raise AppError("That job is no longer known (the app was restarted?)")
        if not job.stoppable:
            raise AppError(f"{job.label} can't be stopped.")
        return self.jobs.stop(job.id).as_json()

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
                    if _is_under(path, self.config.library_folder) and collect.read_manifest(path) is None:
                        collect.write_manifest(path, game or path.name)  # collected before version 0.5.1
                else:
                    lib.roots[path] = game or path.name
                    lib.notes[path] = [f"This folder isn't there any more: {path}"]
            with self.lock:
                self.library.merge(lib)  # keeps a folder added while this ran
            return {"games": len(lib.games()), "models": len(lib.entries)}

        return self._scan_job("Reading game folders", run, stoppable=False)  # the library would be left half read

    def _scan_job(self, label: str, fn: Callable[[Job], Any], stoppable: bool = True) -> Job:
        with self.lock:
            self.scanning += 1

        def run(job: Job):
            try:
                return fn(job)
            finally:
                with self.lock:
                    self.scanning -= 1

        return self.jobs.start(label, run, stoppable)

    def cmd_overview(self, args: dict) -> dict:
        with self.lock:
            games = self.library.overview(bool(args.get("lod")))
        for g in games:
            for f in g["folders"]:
                f["collected"] = self._is_collection(Path(f["path"]))
        return {"scanning": self.scanning > 0, "games": games, "total": sum(g["count"] for g in games)}

    def cmd_folder_check(self, args: dict) -> dict:
        raw = str(args.get("path") or "").strip().strip('"')
        if not raw:
            raise AppError("Choose a folder first.")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        with self.lock:
            roots = dict(self.library.roots)
        outer, inner = folder_relation(path, roots)
        found = collect.collections_in(path) if path.is_dir() else []
        own = next((data for p, data in found if p == path), None)  # a game's folder of collected files
        game = own["game"] if own else detect_game(path) if path.is_dir() and not found else None
        return {"path": str(path), "exists": path.is_dir(), "game": game,
                "games": known_games(), "added": path in roots, "inside": str(outer) if outer else None,
                "inside_game": roots.get(outer) if outer else None, "contains": [str(p) for p in inner],
                "collection": _collection_json(path, own) if own else None,
                "collections": [] if own else [_collection_json(p, d) for p, d in found],  # a whole library folder
                "collection_root": str(path.parent if own else path) if found else None,  # its library folder
                "library_dir": str(self.config.library_folder),
                "library_has_files": bool(collect.collections_in(self.config.library_folder))}

    def cmd_folder_add(self, args: dict) -> dict:
        check = self.cmd_folder_check(args)
        path = Path(check["path"])
        if not check["exists"]:
            raise AppError(f"There is no folder at {path}")
        if check["inside"]:
            raise AppError(f"That folder is already in the library, as part of {check['inside_game']} "
                           f"({check['inside']}).")
        if args.get("library"):  # collect into this library folder from now on
            self.config.library_dir = str(path.parent if check["collection"] else path)
            self.config.save()
        if check["collections"]:  # a whole library folder: each game's folder in it
            with self.lock:
                added = dict(self.library.roots)
            todo = [(Path(c["path"]), c["game"]) for c in check["collections"]
                    if Path(c["path"]) not in added and not folder_relation(Path(c["path"]), added)[0]]
            if not todo:
                raise AppError("Every game in that folder is in the library already.")
            return {"job": self._scan(todo, None, f"Reading collected files in {path.name}").id}
        game = str(args.get("game") or check["game"] or path.name).strip()
        return {"job": self._scan([(path, game)], None, f"Scanning {path.name}").id}

    def cmd_folder_rescan(self, args: dict) -> dict:
        path = Path(str(args.get("path") or ""))
        with self.lock:
            game = self.library.roots.get(path)
        if game is None:
            raise AppError(f"{path} isn't one of the game folders.")
        if not path.is_dir():
            raise AppError(f"This folder isn't there any more: {path}")
        return {"job": self._scan([(path, game)], None, f"Rescanning {path.name}", redraw=True).id}

    def _scan(self, folders: list[tuple[Path, str]], replace: list[Path] | None, label: str,
              redraw: bool = False) -> Job:
        """Scan folders (each with its game) into the library; ``replace``: folders they take the place of
        (moved); ``redraw`` (a rescan) draws their models' previews again."""
        return self._scan_job(label, lambda job: self._scan_into(job, folders, replace, redraw))

    def _scan_into(self, job: Job, folders: list[tuple[Path, str]], replace: list[Path] | None = None,
                   redraw: bool = False) -> dict:
        """The work of a scan job (see _scan), in ``job``."""
        lib = Library()
        for path, game in folders:
            lib.add_root(path, game, lambda i, name: job.report(0.0, name if i < 0 else f"{i:,} files: {name}"))
        job.check()
        if redraw:
            with self.lock:
                before = [e.path for e in self.library.entries if any(_is_under(e.path, p) for p, _g in folders)]
            self.thumbs.forget(set(before) | {e.path for e in lib.entries})
        with self.lock:
            for old in replace or []:
                self.library.remove_root(old)
            for path, _game in folders:
                for inner in folder_relation(path, self.library.roots)[1]:  # now part of this folder
                    self.library.remove_root(inner)
            self.library.merge(lib)
            self._save_folders()
        count = sum(1 for e in lib.entries if not lib.hides(e))  # as the library lists them
        path, game = folders[0]
        return {"game": game, "games": list(dict.fromkeys(g for _p, g in folders)), "path": str(path),
                "count": count, "notes": lib.notes.get(path, [])}

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

    def _is_collection(self, folder: Path) -> bool:
        """Whether a folder holds copies Dark STL collected (in the library folder, or brought from elsewhere)."""
        return _is_under(folder, self.config.library_folder) or (folder / collect.MANIFEST).is_file()

    def cmd_library_move(self, args: dict) -> dict:
        """Keep collected files in another folder: move what's collected so far there (``move``, the
        default), and list the games from their new place."""
        target = Path(os.path.abspath(os.path.expanduser(str(args.get("path") or "").strip().strip('"'))))
        if not str(args.get("path") or "").strip():
            raise AppError("Choose a folder first.")
        old = self.config.library_folder
        if target == old:
            return {"job": None, "library_dir": str(old)}
        if _is_under(target, old):
            raise AppError(f"Choose a folder outside the one it's in now ({old}).")
        with self.lock:
            moving = [(r, g) for r, g in self.library.roots.items() if _is_under(r, old) and r.is_dir()]
        if not args.get("move", True) or not moving:
            self.config.library_dir = str(target)
            self.config.save()
            return {"job": None, "library_dir": str(target)}

        def run(job: Job):
            moved = []
            for i, (root, game) in enumerate(moving):
                job.report(i / len(moving), f"Moving {game}")
                dest = target / root.relative_to(old)
                if not (root / collect.MANIFEST).is_file():
                    collect.write_manifest(root, game)
                collect.move_folder(root, dest)
                moved.append((dest, game))
            self.config.library_dir = str(target)
            self.config.save()
            return self._scan_into(job, moved, [r for r, _g in moving])

        job = self._scan_job(f"Moving collected files to {target.name}", run, stoppable=False)  # half moved: no
        return {"job": job.id, "library_dir": str(target)}

    def _save_folders(self) -> None:
        self.config.game_folders = [{"path": str(p), "game": g} for p, g in self.library.roots.items()]
        self.config.save()

    # ------------------------------------------------------------ collecting into the app's library

    def _collect_plan(self, args: dict) -> collect.Plan:
        """What collecting the chosen models (``ids``, ``paths``, or a game/category/group/search) would copy."""
        from ..importers import importer_for
        from ..importers.fromsoft.files import file_stem

        with self.lock:
            entries: list[CatalogEntry] = []
            for model_id in args.get("ids") or []:
                e = self._entry(str(model_id))
                if e is None and str(model_id) in self.opened:
                    p = self.opened[str(model_id)]
                    e = CatalogEntry(str(args.get("game") or detect_game(p.parent) or "Other models"), "Models", "",
                                     file_stem(p.name), p.name, p, importer_for(p).key, packed=True)
                if e is not None:
                    entries.append(e)
            if not args.get("ids") and args.get("game"):
                entries = self.library.query(str(args["game"]), args.get("category") or None, args.get("group") or None,
                                             str(args.get("q") or ""), bool(args.get("lod")))
            roots = [r for r, g in self.library.roots.items()]
        entries = [e for e in entries if not self._is_collection(self.library.root_of(e) or e.path.parent)]
        if not entries:
            raise AppError("Nothing to collect: choose a model, a kind or a game.")
        games = sorted({e.game for e in entries})
        if len(games) > 1:
            raise AppError("Collect one game at a time.")
        options = collect.CollectOptions(textures=bool(args.get("textures", True)),
                                         animations=bool(args.get("animations", True)),
                                         player_animations=bool(args.get("player_animations", False)),
                                         lod=bool(args.get("lod_copies", False)), extras=bool(args.get("extras", True)))
        importers = {e.importer or importer_for(e.path).key for e in entries}
        from ..importers import IMPORTERS

        by_key = {imp.key: imp for imp in IMPORTERS}

        def related(e: CatalogEntry) -> list[collect.RelatedFile]:
            imp = by_key.get(e.importer) or importer_for(e.path)
            return imp.related_files(e.path, options.textures, options.animations, options.lod)

        def extras(root: Path) -> list[collect.RelatedFile]:
            return [f for key in importers for f in by_key[key].shared_files(root, games[0], options.player_animations)]

        game_roots = [r for r in roots if self.library.roots.get(r) == games[0]]
        return collect.plan(self.config.library_folder, games[0], entries, game_roots, options, related, extras)

    def cmd_collect_plan(self, args: dict) -> dict:
        return self._collect_plan(args).summary()

    def cmd_collect_run(self, args: dict) -> dict:
        """Copy the chosen models' files into the app's library and list them from there."""
        plan = self._collect_plan(args)

        def run(job: Job):
            result = collect.run(plan, job.report)
            folder = plan.folder
            if folder.is_dir():
                job.report(1.0, f"Reading {folder.name}")
                lib = Library()
                lib.add_root(folder, plan.game)
                with self.lock:
                    self.library.merge(lib)
                    self._save_folders()
                result["count"] = len(lib.entries)
            return result

        return {"job": self._scan_job(f"Collecting {len(plan.models)} model{'s' if len(plan.models) != 1 else ''} "
                                      f"from {plan.game}", run).id}

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
            self._show_kept(session)
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

    def cmd_model_animations(self, args: dict) -> dict:
        """The animations that can pose the model: name, frames, duration and frame rate of each."""
        session = self._session(args)
        anims = session.model.animations
        if anims is None:
            return {"animations": [], "bones": 0}
        try:
            infos = anims.list()
            bones = len(anims.skeleton)
        except Exception as exc:  # noqa: BLE001 - a broken archive is reported, not fatal
            raise AppError(f"The animations couldn't be read: {exc}") from exc
        return {"animations": [i.as_dict() for i in infos], "bones": bones}

    def cmd_model_mesh(self, args: dict) -> bytes:
        session = self._session(args)
        uv = str(args.get("uv", "1")) not in ("0", "false", "")
        if args.get("which") == "enhanced":
            if session.result is None:
                raise AppError("This model hasn't been generated yet.")
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
            # cached=1: only a preview drawn already (b"" when it isn't), so the page can show those at once
            return self.thumbs.get(path, None, tuple(self.config.texture_dirs),
                                   draw=str(args.get("cached") or "") not in ("1", "true"))
        except Exception:  # noqa: BLE001 - a model that can't be drawn just has no preview
            return None

    def cmd_thumbs_clear(self, args: dict) -> dict:
        return {"removed": self.thumbs.clear()}

    # ------------------------------------------------------------ enhance and export

    @staticmethod
    def _enhance_settings(s: dict, options: MeshOptions):
        from ..pipeline import EnhanceSettings

        defaults = EnhanceSettings()
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
            integration=str(s.get("integration", defaults.integration)),
            edge_keeping=min(max(float(s.get("edge_keeping", defaults.edge_keeping)), 0.0), 1.0),
            deblock=str(s.get("deblock", defaults.deblock)),
            upsample=str(s.get("upsample", defaults.upsample)),
            ai_model=str(s.get("ai_model") or ""),
            deblock_model=str(s.get("deblock_model") or ""),
            models_dir=models_dir(),
            material_strength={int(k): float(v) for k, v in (s.get("material_strength") or {}).items()
                               if v is not None and v != ""},
            edge_hints=bool(s.get("edge_hints", defaults.edge_hints)),
            albedo_detail_mm=float(s.get("albedo_detail_mm") or 0.0),
            detail_normals=bool(s.get("detail_normals", defaults.detail_normals)),
            detail_strength=float(s.get("detail_strength", defaults.detail_strength)),
        )
        if settings.target_vertices < 1000:
            raise AppError("Choose at least 1,000 vertices.")
        return settings

    def cmd_enhance(self, args: dict) -> dict:
        """Generate: step 1 (even out the vertices) then step 2 (displace them), starting from step 1's
        mesh when it's kept. ``step: "mesh"`` runs step 1 alone: the evened-out model, not displaced."""
        from dataclasses import replace

        from ..pipeline import MESH_SETTINGS, SOURCE_NONE, enhance

        session = self._session(args)
        options = mesh_options(args)
        s = args.get("settings") or {}
        settings = self._enhance_settings(s, options)
        only_mesh = args.get("step") == "mesh"
        if only_mesh:  # what step 1 depends on alone, so step-2 settings don't make it again
            s = {**{k: s[k] for k in MESH_SETTINGS if k in s}, "step": "mesh"}
            settings = replace(settings, source=SOURCE_NONE)
        key = generated_key(session, options, s)
        mkey = mesh_key(session, options, settings)
        with self.lock:
            kept = self.generated.get(key)
            mesh = self.meshes.get(mkey)

        def keep_mesh(made) -> None:  # kept as soon as it's made, so stopping step 2 doesn't lose it
            with self.lock:
                self.meshes[mkey] = made
                self.meshes.move_to_end(mkey)
                while len(self.meshes) > self.KEEP_MESHES:
                    self.meshes.popitem(last=False)

        def run(job: Job):
            result = kept or enhance(session.model, settings, job.report, mesh=mesh, on_mesh=keep_mesh)
            job.check()  # stopped in its last step: not kept
            self._keep(key, result)
            session.result, session.result_id = result, _key_id(key)
            return {**self._result_json(session), "kept": kept is not None, "mesh_reused": mesh is not None}

        name = session.model.variant or session.model.name
        label = f"Evening out {name}" if only_mesh else f"Generating {name}"
        return {"job": self.jobs.start(label, run, stoppable=True).id}

    def cmd_enhance_mesh(self, args: dict) -> dict:
        """Whether the evened-out mesh for these parts, pose and step-1 settings is kept from an earlier
        Generate (so generating with other displacement settings starts from it)."""
        session = self._session(args)
        options = mesh_options(args)
        settings = self._enhance_settings(args.get("settings") or {}, options)
        with self.lock:
            mesh = self.meshes.get(mesh_key(session, options, settings))
        return {"kept": mesh is not None, "vertices": mesh.resampled.n_vertices if mesh else 0,
                "triangles": mesh.resampled.n_faces if mesh else 0}

    def _keep(self, key: tuple, result: Any) -> None:
        with self.lock:
            self.generated[key] = result
            self.generated.move_to_end(key)
            while len(self.generated) > self.KEEP_GENERATED:
                self.generated.popitem(last=False)

    def _show_kept(self, session: Session) -> None:
        """A model opened again shows the enhanced model last generated from it, if that's still kept."""
        file = _file_id(session.model.path)
        with self.lock:
            kept = list(self.generated.items())
        for key, result in reversed(kept):
            if key[:2] == (session.sid, file):
                session.result, session.result_id = result, _key_id(key)
                return

    def _result_json(self, session: Session) -> dict:
        r = session.result
        st = r.stats()
        model = session.model
        return {"id": session.result_id, "stats": st,
                "size_mm": ((r.displaced.bounds()[1] - r.displaced.bounds()[0]) * 1000).round(2).tolist(),
                "fields": [{"material": model.materials[m].name if m < len(model.materials) else str(m),
                            "texture": model.textures[f.texture].name if f.texture in model.textures else f.texture,
                            "convention": f.field.convention, "texel_mm": f.texel_size * 1000, "steps": f.steps}
                           for m, f in r.fields.items()],
                "warnings": list(r.warnings), "displaced": r.displaced_by_textures}

    def cmd_export(self, args: dict) -> dict:
        session = self._session(args)
        which = args.get("which", "enhanced")
        if which == "enhanced":
            if session.result is None:
                raise AppError("Generate the enhanced model first, or export the original.")
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

        return {"job": self.jobs.start(f"Exporting {base}{ext}", run, stoppable=True).id}

    def _forget_models(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.textures_png.clear()
            self.generated.clear()
            self.meshes.clear()


def _collection_json(path: Path, data: dict) -> dict:
    return {"path": str(path), "game": str(data["game"]), "models": len(data.get("models") or []),
            "updated": data.get("updated"), "sources": data.get("sources") or []}


def _is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _key_id(key: tuple) -> str:
    return hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:12]


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


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
