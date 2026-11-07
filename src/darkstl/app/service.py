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
import logging
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
from .. import configs
from ..errors import FormatError
from ..export import FORMATS, ExportOptions, export_mesh, write_palette_png
from ..geometry.mesh import TriMesh
from ..library import Library, Snapshot, detect_game, folder_relation, known_games
from ..model import CatalogEntry, MeshOptions, SourceModel, path_id
from ..thumbnails import ThumbnailCache
from .config import Config, config_dir, filelists_dir, models_dir
from .meshpack import pack_mesh

log = logging.getLogger(__name__)

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
    cut: Any = None  # CutResult: the pieces last cut from this model
    cut_id: str = ""
    finished: Any = None  # finish.FinishResult: the model last made watertight (Finish tab)
    finished_id: str = ""
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
    imported = tuple(_file_id(Path(p)) for _k, p in sorted((settings.get("imported_maps") or {}).items()) if p)
    return session.sid, _file_id(session.model.path), _options_key(options), settings_key, imported


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
    out = {"id": e.id, "label": e.label, "stem": e.stem, "game": e.game, "category": e.category, "group": e.group,
           "packed": e.packed, "lod": e.is_lod, "importer": e.importer, "file": str(e.path), "name": e.path.name}
    if e.config is not None:
        out["configurable"] = {"slots": len(e.config.slots), "options": sum(len(s.options) for s in e.config.slots),
                               "guessed": e.config.guessed}
    return out


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
        "configuration": m.configuration,
    }


def mesh_stats(mesh: TriMesh) -> dict:
    lo, hi = mesh.bounds() if mesh.n_vertices else (np.zeros(3), np.zeros(3))
    return {"vertices": mesh.n_vertices, "triangles": mesh.n_faces, "area_m2": float(mesh.surface_area()),
            "size_mm": ((hi - lo) * 1000).round(2).tolist()}


# ------------------------------------------------------------------ service

class Service:
    BYTES = {"model_mesh", "texture", "thumb", "texture_crop", "paint_texture"}
    # opening a model and what its page shows first: library previews pause while these run
    FOREGROUND = {"model_open", "model_mesh", "model_stats", "model_animations", "texture", "texture_info",
                  "texture_maps", "texture_crop"}
    KEEP_GENERATED = 3  # enhanced models kept, so generating one again with the same settings is instant
    KEEP_MESHES = 2  # evened-out meshes kept, so other displacement settings are tried without making them again
    KEEP_UPSCALED = 8  # normal maps made ready (cleaned up, upsampled) kept, within KEEP_UPSCALED_BYTES
    KEEP_UPSCALED_BYTES = 768 << 20
    KEEP_CUTS = 3  # cut models kept, so going back to a set of cuts is instant
    KEEP_SOLIDS = 1  # the model made solid (big), so moving a cut doesn't make it solid again
    KEEP_FINISHED = 2  # models made watertight, so switching between two settings is instant

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
        self.upscaled: OrderedDict[tuple, Any] = OrderedDict()  # normal maps made ready (UpscaledMap) by _upscale_key()
        self.cuts: OrderedDict[tuple, Any] = OrderedDict()  # CutResult by the model cut and the cut settings
        self.solids: OrderedDict[tuple, Any] = OrderedDict()  # cut.Solid by the model made solid and the sheet thickness
        self.finished: OrderedDict[tuple, Any] = OrderedDict()  # finish.FinishResult by the model and the settings
        self.palettes: OrderedDict[tuple, dict] = OrderedDict()  # suggested paint palettes by model, parts and count
        self.cache = Path(cache)
        self.snapshots = Path(cache) / "library"  # what each folder's last scan found (library.Snapshot)
        self.collect_plans: OrderedDict[str, tuple[float, collect.Plan]] = OrderedDict()  # by _plan_key()
        self.unpack_plans: dict[tuple, Any] = {}  # unpack.Plan by (game folder, destination), the last few
        self.part_models: OrderedDict[tuple, SourceModel] = OrderedDict()  # models opened to assemble configurations
        configs.set_user_folder((config.path.parent if config.path else config_dir()) / "configurations")
        if config.oodle:
            self._use_oodle(config.oodle)

    # ------------------------------------------------------------ routing

    def call(self, cmd: str, args: dict | None = None) -> Any:
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            raise AppError(f"Unknown command '{cmd}'")
        if cmd in self.FOREGROUND:
            with self.thumbs.foreground():
                return fn(args or {})
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
            "cutting": _importable("manifold3d"),
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
                "previews": str(self.thumbs.folder), "settings_file": str(self.config.path or ""),
                **self._unreal_settings()}

    def _unreal_settings(self) -> dict:
        """The AES keys and type mappings (.usmap) the Unreal games' importer reads from the settings folder."""
        from ..importers.borderlands.ue4 import vfs
        from .config import config_dir

        d = config_dir()
        f = d / vfs.KEY_FILE
        try:
            keys = f.read_text("utf-8", "replace") if f.is_file() else ""
        except OSError:
            keys = ""
        maps = sorted(p.name for p in d.glob("*.usmap")) if d.is_dir() else []
        return {"unreal_keys": keys, "unreal_keys_file": str(f), "usmaps": maps, "settings_dir": str(d)}

    def _forget_unreal(self) -> None:
        from ..importers.borderlands.ue4 import unversioned, vfs

        with vfs._OPEN_LOCK:
            vfs._OPEN.clear()
        unversioned.forget_mappings()
        self._forget_models()

    def _set_unreal(self, args: dict) -> None:
        import shutil

        from ..importers.borderlands.ue4 import pak, usmap, vfs
        from .config import config_dir

        d = config_dir()
        if "unreal_keys" in args:
            text = str(args["unreal_keys"] or "")
            for line in text.splitlines():
                t = line.split("#", 1)[0].strip()
                if t:
                    try:
                        pak.parse_key(t)
                    except FormatError as e:
                        raise AppError(f"{t[:20]}…: {e}") from e
            d.mkdir(parents=True, exist_ok=True)
            (d / vfs.KEY_FILE).write_text(text.strip() + "\n" if text.strip() else "", "utf-8")
            self._forget_unreal()
        if args.get("usmap_add"):
            src = Path(str(args["usmap_add"]).strip().strip('"'))
            if not src.is_file():
                raise AppError(f"There is no file at {src}")
            try:
                usmap.parse(src.read_bytes(), src.name)
            except (FormatError, ValueError) as e:
                raise AppError(f"{src.name} isn't a type mappings file Dark STL can read: {e}") from e
            d.mkdir(parents=True, exist_ok=True)
            dest, n = d / (src.stem + ".usmap"), 2
            while dest.is_file() and dest.read_bytes() != src.read_bytes():  # another game's Mappings.usmap
                dest, n = d / f"{src.stem}-{n}.usmap", n + 1
            shutil.copyfile(src, dest)
            self._forget_unreal()
        if args.get("usmap_remove"):
            target = d / Path(str(args["usmap_remove"])).name
            if target.suffix.lower() == ".usmap" and target.is_file():
                target.unlink()
            self._forget_unreal()

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
        self._set_unreal(args)
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
        if path and not Path(path).exists() and Path(path).parent.is_file():
            path = str(Path(path).parent)  # a model inside a game archive: show the archive
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
        """Read every saved game folder (at start-up). Each folder's last scan is kept on disk, so the library
        is listed at once. Then each folder is checked: one that hasn't changed since (no file added, removed
        or renamed, no archive changed: see listing.Fingerprint) gets back what its scan readied for opening
        models (such as the texture index); any other is scanned again behind the listing, replacing it
        when done. Rescan scans a folder whatever its fingerprint says."""
        folders = [(Path(f["path"]), f.get("game") or None) for f in self.config.game_folders]
        if not folders:
            return None

        def run(job: Job):
            lib = Library()
            snaps: dict[Path, Snapshot] = {}
            for path, game in folders:
                if not path.is_dir():
                    lib.roots[path] = game or path.name
                    lib.notes[path] = [f"This folder isn't there any more: {path}"]
                    continue
                snap = Snapshot.load(self.snapshots, path, game, __version__)
                if snap is not None:
                    snaps[path] = snap
                    lib.add_scanned(path, snap.game, snap.found, snap.notes, snap.configs)
                else:
                    lib.roots[path] = game or path.name  # listed in order, filled in when scanned below
            with self.lock:
                self.library.merge(lib)  # keeps a folder added while this ran
            for i, (path, game) in enumerate(folders):
                if not path.is_dir():
                    continue
                snap = snaps.get(path)
                job.report(i / len(folders), f"Checking {path.name}")
                if snap is not None and snap.unchanged():
                    job.report(i / len(folders), f"Getting {path.name} ready")
                    snap.kept.restore(path)  # listed already: nothing to scan
                else:
                    job.report(i / len(folders), f"Reading {path.name}")
                    one = Library()
                    self._scan_folder(one, path, game, job)
                    with self.lock:
                        self.library.merge(one)
                if _is_under(path, self.config.library_folder) and collect.read_manifest(path) is None:
                    collect.write_manifest(path, game or path.name)  # collected before version 0.5.1
            with self.lock:
                self.collect_plans.clear()
                return {"games": len(self.library.games()), "models": len(self.library.entries)}

        return self._scan_job("Reading game folders", run, stoppable=False)  # the library would be left half read

    def _scan_folder(self, lib: Library, path: Path, game: str | None, job: Job | None = None) -> None:
        """Scan one folder into ``lib``, and keep what was found for the next start."""
        report = (lambda i, name: job.report(job.progress, name if i < 0 else f"{i:,} files: {name}")) if job else None
        found = lib.add_root(path, game, report)
        g = lib.roots[path]
        Snapshot(path, g, found, lib.notes.get(path, []), lib.found_configs.get(path, []), __version__,
                 lib.kept.get(path)).save(self.snapshots)

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
                f["packed"] = self._packed(Path(f["path"]), g["name"]) if not f["collected"] else None
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
            self._scan_folder(lib, path, game, job)
        job.check()
        if redraw:
            with self.lock:
                before = [e.path for e in self.library.entries if any(_is_under(e.path, p) for p, _g in folders)]
            self.thumbs.forget(set(before) | {e.path for e in lib.entries})
        with self.lock:
            self.collect_plans.clear()
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
                "count": count, "notes": lib.notes.get(path, []),
                "packed": None if self._is_collection(path) else self._packed(path, game)}

    def cmd_folder_remove(self, args: dict) -> dict:
        path = Path(str(args.get("path") or ""))
        with self.lock:
            if path not in self.library.roots:
                raise AppError(f"{path} isn't one of the game folders.")
            self.library.remove_root(path)
            self._save_folders()
        from ..importers.borderlands.ue4 import vfs

        vfs.forget(path)  # the containers it kept open, so the game can be uninstalled with the app running
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

    # ------------------------------------------------------------ unpacking game archives

    @staticmethod
    def _packed(folder: Path, game: str | None) -> dict | None:
        """{"game": ...} when ``folder`` holds a game's packed archives Dark STL can unpack."""
        from ..importers.fromsoft import unpack

        try:
            games = unpack.packed_games(folder)
        except OSError:
            return None
        if not games:
            return None
        return {"game": game if game in games else games[0]}

    def _unpack_args(self, args: dict) -> tuple[Path, str | None, Path]:
        raw = str(args.get("path") or "").strip().strip('"')
        if not raw:
            raise AppError("Choose the game's folder first.")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        if not path.is_dir():
            raise AppError(f"There is no folder at {path}")
        with self.lock:
            game = self.library.roots.get(path) or str(args.get("game") or "").strip() or None
        packed = self._packed(path, game)
        if packed is None:
            raise AppError(f"There are no game archives to unpack in {path} (Data0.bhd, Data1.bhd, GameDataEbl.bhd, "
                           "dvdbnd0.bhd5 ...). Choose the game's install folder.")
        game = packed["game"]
        dest_raw = str(args.get("dest") or "").strip().strip('"')
        dest = Path(os.path.abspath(os.path.expanduser(dest_raw))) if dest_raw else \
            self.config.library_folder / collect._folder_name(game)
        if _is_under(dest, path):
            raise AppError("Choose a folder outside the game's folder: Dark STL doesn't change the game's files.")
        return path, game, dest

    def _unpack_plan(self, path: Path, game: str | None, dest: Path, job: Job):
        from ..importers.fromsoft import unpack

        try:
            plan = unpack.plan(path, dest, game, self.cache / "archives", filelists_dir(), job.report)
        except FormatError as exc:
            raise AppError(str(exc)) from None
        with self.lock:
            self.unpack_plans[(path, dest)] = plan
            while len(self.unpack_plans) > 3:
                self.unpack_plans.pop(next(iter(self.unpack_plans)))
        return plan

    def cmd_unpack_plan(self, args: dict) -> dict:
        """What unpacking a game folder's archives would write, by group (a job: the first time, reading the
        archives' encrypted headers takes a while)."""
        path, game, dest = self._unpack_args(args)

        def run(job: Job):
            out = self._unpack_plan(path, game, dest, job).summary()
            out["path"] = str(path)
            out["default_dest"] = str(self.config.library_folder / collect._folder_name(out["game"]))
            return out

        return {"job": self.jobs.start(f"Reading {game}'s archives", run, stoppable=True).id}

    def cmd_unpack_run(self, args: dict) -> dict:
        """Unpack the chosen groups of a game folder's archives into ``dest`` (the app's library by default)
        and list the models from there."""
        from ..importers.fromsoft import unpack

        path, game, dest = self._unpack_args(args)
        known = {g.id for g in unpack.GROUPS}
        groups = [str(g) for g in args.get("groups") or [] if str(g) in known]
        if not groups:
            raise AppError("Choose what to unpack: characters, weapons and armour, objects or map pieces.")
        with self.lock:
            plan = self.unpack_plans.get((path, dest))

        def run(job: Job):
            p = plan or self._unpack_plan(path, game, dest, job)
            result = unpack.run(p, groups, job.report)
            collect.write_manifest(dest, p.game, sources=[str(path)])
            job.report(1.0, f"Reading {dest.name}")
            lib = Library()
            lib.add_root(dest, p.game)
            self.thumbs.forget({e.path for e in lib.entries})
            with self.lock:
                for inner in folder_relation(dest, self.library.roots)[1]:
                    self.library.remove_root(inner)
                self.library.merge(lib)
                self._save_folders()
                self.unpack_plans.pop((path, dest), None)
            result["count"] = sum(1 for e in lib.entries if not lib.hides(e))
            return result

        return {"job": self._scan_job(f"Unpacking {game}", run).id}

    def cmd_filelists(self, args: dict) -> dict:
        """The file lists unpacking names files with: bundled ones and those downloaded from Settings."""
        from ..importers.fromsoft import unpack

        return {"folder": str(filelists_dir()), "games": unpack.lists_status(filelists_dir()),
                "source": unpack.DOWNLOAD_PAGE}

    def cmd_filelists_download(self, args: dict) -> dict:
        """Download UXM Selective Unpacker's newer file lists (all games, or ``games``) into the settings folder."""
        from ..importers.fromsoft import unpack

        games = [str(g) for g in args.get("games") or []] or None

        def run(job: Job):
            out = unpack.download_lists(filelists_dir(), games, job.report)
            if not out["downloaded"]:
                raise AppError("Couldn't download the file lists: " + "; ".join(out["failed"][:3]))
            with self.lock:
                self.unpack_plans.clear()
            return out

        return {"job": self.jobs.start("Downloading file lists", run, stoppable=False).id}

    # ------------------------------------------------------------ collecting into the app's library

    COLLECT_ARGS = ("ids", "game", "category", "group", "q", "lod", "textures", "animations", "player_animations",
                    "lod_copies", "extras")
    KEEP_PLAN_SECONDS = 600  # a plan worked out for the Collect dialog is used by its Copy button

    def _plan_key(self, args: dict) -> str:
        return json.dumps({k: args.get(k) for k in self.COLLECT_ARGS}, sort_keys=True, default=str)

    def _collect_plan(self, args: dict, job: Job | None = None) -> collect.Plan:
        """What collecting the chosen models (``ids``, ``paths``, or a game/category/group/search) would copy.
        Reading every model of a game for the textures it takes from elsewhere takes a while, so a plan is
        kept for a few minutes, and ``job`` reports how far it has got (and can stop it)."""
        key = self._plan_key(args)
        with self.lock:
            kept = self.collect_plans.get(key)
        if kept is not None and time.time() - kept[0] < self.KEEP_PLAN_SECONDS:
            return kept[1]
        with self.thumbs.foreground():  # previews wait: this is what the user is waiting for
            plan = self._make_collect_plan(args, job)
        with self.lock:
            self.collect_plans[key] = (time.time(), plan)
            while len(self.collect_plans) > 4:
                self.collect_plans.popitem(last=False)
        return plan

    def _make_collect_plan(self, args: dict, job: Job | None) -> collect.Plan:
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
        found_configs = [e.config for e in entries if e.config is not None]
        entries, variants = self._expand_configs(entries)
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
            out = list(imp.related_files(e.path, options.textures, options.animations, options.lod))
            if imp.record_reads:
                out += self._reads(imp, e.path, variants.get(e.path, {None}), options)
            return out

        def extras(root: Path) -> list[collect.RelatedFile]:
            return [f for key in importers for f in by_key[key].shared_files(root, games[0], options.player_animations)]

        game_roots = [r for r in roots if self.library.roots.get(r) == games[0]]
        progress = None
        if job is not None:
            n = len(entries)
            progress = lambda i, name: job.report(i / max(n, 1), f"Reading model {i + 1:,} of {n:,}: {name}")  # noqa: E731
        return collect.plan(self.config.library_folder, games[0], entries, game_roots, options, related, extras,
                            progress, found_configs)

    def _expand_configs(self, entries: list[CatalogEntry]) -> tuple[list[CatalogEntry], dict[Path, set]]:
        """A configurable model stands for its base and every part that fits it. Also returns, for each file,
        the variants to open it as (a gun skin is a variant of its part definitions' package)."""
        import dataclasses

        with self.lock:
            by_path = {e.path: e for e in self.library.entries}
        out: dict[Path, CatalogEntry] = {}
        variants: dict[Path, set] = {}
        for e in entries:
            if e.config is None:
                out.setdefault(e.path, e)
                variants.setdefault(e.path, set()).add(None)
                continue
            refs = [e.config.base] + [o.model for s in e.config.slots for o in s.options]
            for r in refs:
                p = Path(r.file)
                out.setdefault(p, by_path.get(p) or dataclasses.replace(e, stem=p.stem, label=p.stem, path=p, config=None))
                variants.setdefault(p, set()).add(r.variant or None)
        return list(out.values()), variants

    MAX_RECORDED_VARIANTS = 64

    def _reads(self, imp, path: Path, variants: set, options: collect.CollectOptions) -> list[collect.RelatedFile]:
        """What opening the model at ``path`` (as each of ``variants``, and each model the file holds) reads:
        every file and archive piece, so that a collected copy works without the game (see darkstl.access)."""
        from .. import access

        want = set(variants)
        if None in want:
            try:
                want |= set(imp.variants(path)[:self.MAX_RECORDED_VARIANTS])
            except Exception:  # noqa: BLE001 - a file that can't be listed is opened as it is
                pass
        rec = access.Recorder()
        with access.recording(rec):
            for v in sorted(want, key=lambda v: (v is not None, str(v))):
                try:
                    imp.load(path, v, tuple(self.config.texture_dirs))
                except Exception:  # noqa: BLE001 - a model that doesn't open still has its own files copied
                    log.debug("Collect: %s (%s) didn't open", path, v, exc_info=True)
        out = [collect.RelatedFile(p, kind) for p, kind in rec.files.items()
               if p.is_file() and (options.textures or kind != "texture")]
        out += [collect.RelatedFile(m.source, m.kind, m.save_as, m.extract, m.size) for m in rec.members.values()
                if options.textures or m.kind != "texture"]
        return out

    def cmd_collect_plan(self, args: dict) -> dict:
        """What collecting would copy. With ``job``, worked out in a job (stoppable, with progress), whose
        result is the summary: the page asks this way, as a whole game can take minutes."""
        if not args.get("job"):
            return self._collect_plan(args).summary()
        label = f"Working out what to copy from {args.get('game') or 'the game'}"
        return {"job": self.jobs.start(label, lambda job: self._collect_plan(args, job).summary(), stoppable=True).id}

    def cmd_collect_run(self, args: dict) -> dict:
        """Copy the chosen models' files into the app's library and list them from there."""
        plan = self._collect_plan(args)

        def run(job: Job):
            with self.lock:
                self.collect_plans.clear()  # what is there now has changed
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
        config = entry.config if entry is not None else None
        if config is not None:
            chosen = config.choose(args.get("choices") if isinstance(args.get("choices"), dict) else None)
            sid = f"{model_id}|config|{configs.choices_key(chosen)}"
        else:
            sid = f"{model_id}|{variant or ''}"
        with self.lock:
            session = self.sessions.get(sid)
        if session is None:
            if config is not None:
                try:
                    model = configs.assemble(config, chosen, self._part_model)
                except FormatError as exc:
                    raise AppError(f"{config.label} couldn't be put together: {exc}") from exc
            else:
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
                    "enhanced": self._result_json(session) if session.result is not None else None,
                    "cut": self._cut_json(session) if session.cut is not None else None,
                    "finished": self._finished_json(session) if session.finished is not None else None,
                    "configurable_as": self._configurable_as(path) if config is None else []})
        return out

    def _configurable_as(self, path: Path) -> list[dict]:
        """The configurable models this model is the base or a part of (a gun part -> the gun to configure)."""
        with self.lock:
            found = [e for e in self.library.configurations if e.config is not None
                     and (Path(e.path) == Path(path) or any(Path(o.model.file) == Path(path)
                                                            for s in e.config.slots if s.kind == "parts"
                                                            for o in s.options))]
        return [{"id": e.id, "label": e.label, "slots": len(e.config.slots)} for e in found]

    def _part_model(self, path: str, variant: str | None) -> SourceModel:
        """A model a configuration is made of, kept open for the next choice of parts."""
        from ..importers import open_model

        key = (path, variant, _file_id(Path(path)))
        with self.lock:
            model = self.part_models.get(key)
            if model is not None:
                self.part_models.move_to_end(key)
                return model
        model = open_model(path, variant, tuple(self.config.texture_dirs))
        with self.lock:
            self.part_models[key] = model
            while len(self.part_models) > 24:
                self.part_models.popitem(last=False)
        return model

    # ------------------------------------------------------------ configurations

    def cmd_configs(self, args: dict) -> dict:
        """The configurable models of a game (or all), and the parts of their lists that weren't found."""
        game = args.get("game") or None
        with self.lock:
            items = [e for e in self.library.configurations if not game or e.game == game]
            notes = {g: n for g, n in self.library.config_notes.items() if not game or g == game}
        return {"items": [entry_json(e) for e in items], "missing": notes,
                "folder": str(configs.USER_DIRS[0]) if configs.USER_DIRS else None}

    def _relative(self, path: str) -> str:
        """A model file as a configuration list names it: below its game folder when it's in one."""
        p = Path(path)
        with self.lock:
            root = next((r for r in self.library.roots if _is_under(p, r)), None)
        return p.relative_to(root).as_posix() if root is not None else str(p)

    def cmd_config_save(self, args: dict) -> dict:
        """Keep a configuration in the user's own list (in place of one of the same id, whichever list
        that came from). ``configuration`` is as the page shows it; ``base_id`` names the base model's
        library entry for a new one."""
        raw = args.get("configuration")
        if not isinstance(raw, dict):
            raise AppError("No configuration was sent.")
        raw = json.loads(json.dumps(raw))
        base_entry = self._entry(str(args.get("base_id") or "")) if args.get("base_id") else None
        if base_entry is not None:
            raw["base"] = {"file": str(base_entry.path)}
            raw.setdefault("game", base_entry.game)
            raw.setdefault("category", base_entry.category)
            raw.setdefault("group", base_entry.group)
        if isinstance(raw.get("base"), str):
            raw["base"] = {"file": raw["base"]}
        game = str(raw.get("game") or "")
        if not game:
            raise AppError("A configuration belongs to a game.")
        for s in raw.get("slots") or []:
            for o in s.get("options") or []:
                if not o.get("model") and o.get("path"):
                    o["model"] = {"file": o["path"], "variant": o.get("variant"), "parts": o.get("parts")}
                o.pop("path", None)
                o.pop("file", None)
                o.pop("variant", None)
                o.pop("parts", None)
                if o.get("model"):
                    o["model"]["file"] = self._relative(o["model"]["file"])
                    o["model"] = {k: v for k, v in o["model"].items() if v is not None}
                if isinstance(o.get("attach"), dict) and configs.parse_attach(o["attach"]).is_identity():
                    o.pop("attach", None)
        raw["base"]["file"] = self._relative(raw["base"]["file"])
        presets = raw.get("presets")
        if isinstance(presets, list):  # as the page shows them
            raw["presets"] = {p["name"]: p.get("choices") or {} for p in presets if p.get("name")}
        if not raw.get("id"):  # a new one: an id no configuration of the game has yet
            with self.lock:
                taken = {e.config.id for e in self.library.configurations if e.game == game}
            stem = "my:" + Path(raw["base"]["file"]).name.split(".")[0].lower()
            raw["id"], n = stem, 2
            while raw["id"] in taken:
                raw["id"], n = f"{stem}-{n}", n + 1
        for k in ("chosen", "defaults", "source", "guessed", "base_name"):
            raw.pop(k, None)
        try:
            config = configs.parse({"configurations": [raw]}, game, configs.USER_SOURCE)[0]
            path = configs.save_user(config)
        except FormatError as exc:
            raise AppError(str(exc)) from exc
        with self.lock:
            self.library.configs_changed()
            entry = next((e for e in self.library.configurations if e.game == game and e.config.id == config.id), None)
            for sid in [k for k in self.sessions if entry is not None and k.startswith(entry.id + "|")]:
                del self.sessions[sid]
        if entry is None:
            raise AppError("It was saved, but its base model isn't in the library, so it isn't listed.")
        return {"id": entry.id, "file": str(path), "entry": entry_json(entry)}

    def cmd_config_delete(self, args: dict) -> dict:
        """Remove a configuration from the user's own list (one from another list shows again)."""
        entry = self._entry(str(args.get("id") or ""))
        if entry is None or entry.config is None:
            raise AppError("That isn't a configurable model.")
        removed = configs.delete_user(entry.game, entry.config.id)
        with self.lock:
            self.library.configs_changed()
            for sid in [k for k in self.sessions if k.startswith(entry.id + "|")]:
                del self.sessions[sid]
            still = self.library.find(entry.id)
        return {"removed": removed, "still_listed": still is not None}

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
        return {"animations": [i.as_dict() for i in infos], "bones": bones, "note": getattr(anims, "note", None)}

    def cmd_model_mesh(self, args: dict) -> bytes:
        session = self._session(args)
        uv = str(args.get("uv", "1")) not in ("0", "false", "")
        if args.get("which") == "enhanced":
            if session.result is None:
                raise AppError("This model hasn't been generated yet.")
            return pack_mesh(session.result.displaced, uv, extra={"which": "enhanced"})
        if args.get("which") == "finished":
            if session.finished is None:
                raise AppError("This model hasn't been made watertight yet.")
            return pack_mesh(session.finished.mesh, False, extra={"which": "finished"})
        if args.get("which") == "pieces":
            if session.cut is None:
                raise AppError("This model hasn't been cut yet.")
            pieces = [{"name": p.name, "kind": p.kind, "center": p.stats()["center_mm"]} for p in session.cut.pieces]
            return pack_mesh(session.cut.combined(), False, extra={"which": "pieces", "pieces": pieces})
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
            edge_smoothing=min(max(float(s.get("edge_smoothing", defaults.edge_smoothing)), 0.0), 1.0),
            upsample=str(s.get("upsample", defaults.upsample)),
            ai_model=str(s.get("ai_model") or ""),
            deblock_model=str(s.get("deblock_model") or ""),
            upsample_factor=_factor(s.get("upsample_factor")),
            imported_maps={str(k): str(v) for k, v in (s.get("imported_maps") or {}).items() if v},
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
            result = kept
            if result is None:
                # the normal maps as the texture settings make them (the Textures tab's step), made now if not yet
                _maps, prepared, made = self._prepared_maps(session, options, settings, job) if not only_mesh else ([], {}, False)
                progress = (lambda f, m: job.report(0.2 + 0.8 * f, m)) if made else job.report
                result = enhance(session.model, settings, progress, mesh=mesh, on_mesh=keep_mesh, prepared=prepared)
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

    # ------------------------------------------------------------ textures made ready for displacement

    def _detail_maps(self, session: Session, options: MeshOptions, settings):
        """The normal maps displacement would read, and the vertex spacing the mesh settings give."""
        from ..geometry.subdivide import edge_length_for_vertex_count
        from ..pipeline import detail_maps

        source = session.build(options)
        maps = detail_maps(session.model, source, session.model.materials, settings)
        return maps, edge_length_for_vertex_count(source.surface_area(), max(int(settings.target_vertices), 4))

    def _upscale_key(self, session: Session, options: MeshOptions, settings, dm, spacing: float, h: int, w: int) -> tuple:
        """What a normal map made ready depends on: the model file, its parts and pose (the UVs covered),
        the texture, the texture settings, and the factor and model those give it here."""
        from ..pipeline import UPSCALE_SETTINGS, plan_upscale

        imported = settings.imported_maps.get(dm.key)
        if imported:  # a file upscaled elsewhere: the other texture settings don't come into it
            return (session.sid, _file_id(session.model.path), dm.key, _options_key(options), dm.green,
                    "imported", _file_id(Path(imported)))
        factor, model = plan_upscale(settings, dm.texel, spacing, dm.codec, h, w)
        return (session.sid, _file_id(session.model.path), dm.key, _options_key(options), dm.green,
                tuple(getattr(settings, k) for k in UPSCALE_SETTINGS), factor, str(model.path) if model else "")

    def _prepared_maps(self, session: Session, options: MeshOptions, settings, job: Job | None = None,
                       keys: list[str] | None = None, make: bool = True):
        """The normal maps displacement with these settings reads, each made ready (cleaned up and upsampled as
        the texture settings ask) and kept, so Displace and the Textures tab share them. ``make`` False only
        says which are ready. Returns (what the page shows of each, the maps ready by key, whether any was made)."""
        from ..pipeline import upscale_normal_map

        maps, spacing = self._detail_maps(session, options, settings)
        if keys is not None:
            maps = [dm for dm in maps if dm.key in keys]
        model = session.model
        shown, found, made = [], {}, False
        for i, dm in enumerate(maps):
            tex = model.textures[dm.key]
            img = tex.decode()
            h, w = img.shape[:2]
            k = self._upscale_key(session, options, settings, dm, spacing, h, w)
            with self.lock:
                up = self.upscaled.get(k)
                if up is not None:
                    self.upscaled.move_to_end(k)
            if up is None and make:
                if job is not None:
                    job.report(0.2 * i / len(maps), f"Making {tex.name} ready ({i + 1} of {len(maps)})")
                info = model.materials[dm.materials[0]] if dm.materials[0] < len(model.materials) else None
                progress = (lambda f, m, i=i, name=tex.name: job.report(0.2 * (i + f) / len(maps), f"{name}: {m}")) if job else None
                up = upscale_normal_map(model, info, img, dm.all_uv, dm.green, dm.texel, spacing, settings, dm.key,
                                        dm.codec, progress)
                if job is not None:
                    job.check()
                self._keep_upscaled(k, up)
                made = True
            if up is not None:
                found[dm.key] = up
            shown.append({"key": dm.key, "name": tex.name, "size": [w, h], "texel_mm": dm.texel * 1000,
                          "materials": [model.materials[m].name if m < len(model.materials) else str(m) for m in dm.materials],
                          "ready": up is not None, "factor": up.factor if up else None,
                          "imported": settings.imported_maps.get(dm.key) or None,
                          "up_size": [w * up.factor, h * up.factor] if up else None,
                          "steps": list(up.steps) if up else [], "warnings": list(up.warnings) if up else []})
        return shown, found, made

    def _keep_upscaled(self, key: tuple, up: Any) -> None:
        with self.lock:
            self.upscaled[key] = up
            self.upscaled.move_to_end(key)
            while len(self.upscaled) > 1 and (len(self.upscaled) > self.KEEP_UPSCALED
                                              or sum(u.nbytes for u in self.upscaled.values()) > self.KEEP_UPSCALED_BYTES):
                self.upscaled.popitem(last=False)

    def cmd_texture_maps(self, args: dict) -> dict:
        """The normal maps Displace will read with these settings, and whether each is ready (made with the
        texture settings as they are, for the vertex spacing the mesh settings give)."""
        session = self._session(args)
        options = mesh_options(args)
        settings = self._enhance_settings(args.get("settings") or {}, options)
        shown, _found, _made = self._prepared_maps(session, options, settings, make=False)
        _maps, spacing = self._detail_maps(session, options, settings)
        return {"maps": shown, "spacing_mm": spacing * 1000}

    def cmd_texture_upscale(self, args: dict) -> dict:
        """Make the normal maps ready (the Textures tab's step): ``keys`` chooses some, else all of them."""
        session = self._session(args)
        options = mesh_options(args)
        settings = self._enhance_settings(args.get("settings") or {}, options)
        keys = args.get("keys")
        if isinstance(keys, str):
            keys = [k for k in keys.split(",") if k]

        def run(job: Job):
            shown, _found, _made = self._prepared_maps(session, options, settings, job, keys)
            job.check()
            return {"maps": shown}

        name = session.model.variant or session.model.name
        return {"job": self.jobs.start(f"Upscaling {name}'s textures", run, stoppable=True).id}

    def cmd_texture_crop(self, args: dict) -> bytes:
        """A piece of a normal map made ready, before (``which=before``) or after the texture settings, drawn at
        ``w`` x ``h``: the part ``x0..x1`` by ``y0..y1`` of it (fractions; outside the texture is grey). The
        page's before/after view asks for these as it pans and zooms, so texels stay crisp however far in."""
        session = self._session(args)
        key = str(args.get("key") or "")
        options = mesh_options(_parse_lists(args))
        settings = self._enhance_settings(_json_arg(args.get("settings")) or {}, options)
        _shown, found, _made = self._prepared_maps(session, options, settings, make=False, keys=[key])
        up = found.get(key)
        if up is None:
            raise AppError("This texture hasn't been upscaled with these settings yet.")
        rect = tuple(float(args.get(k, d)) for k, d in (("x0", 0), ("y0", 0), ("x1", 1), ("y1", 1)))
        size = tuple(min(max(int(args.get(k, 512)), 16), 4096) for k in ("w", "h"))
        return crop_png(up, str(args.get("which", "after")) != "before", rect, size)

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
                break
        with self.lock:
            cuts = list(self.cuts.items())
        for key, result in reversed(cuts):
            if key[:2] == (session.sid, file):
                session.cut, session.cut_id = result, _key_id(key)
                break
        with self.lock:
            finished = list(self.finished.items())
        for key, result in reversed(finished):
            if key[:2] == (session.sid, file):
                session.finished, session.finished_id = result, _key_id(key)
                break

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
        if which == "pieces":
            return self._export_pieces(session, args)
        if which == "enhanced":
            if session.result is None:
                raise AppError("Generate the enhanced model first, or export the original.")
            mesh = session.result.displaced
        elif which == "finished":
            if session.finished is None:
                raise AppError("Make the model watertight first (Finish tab).")
            mesh = session.finished.mesh
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
        paint = paint_settings(args.get("paint") or {}) if ext == ".3mf" else None
        finished = session.finished if which == "finished" else None

        def run(job: Job):
            opts = ExportOptions(scale=scale, material_names=[m.name for m in model.materials], paint=paint,
                                 progress=job.report)
            if ext == ".3mf" and finished is not None:
                # no textures of its own: painted from the textured model it was made from
                opts.paint_from = self._surface_colours(model, finished.source, paint, job)
            elif (args.get("textures", True) and ext in (".obj", ".glb")) or ext == ".3mf":
                opts.textures = _albedo(model, mesh)
                if ext == ".3mf" and not opts.textures:
                    raise AppError("This model has no colour textures to paint from.")
            job.report(0.2, "Writing")
            path = export_mesh(mesh, _free_name(folder / f"{base}{ext}"), opts)
            out = {"path": str(path), "bytes": path.stat().st_size}
            if opts.painting is not None:
                out["paint"] = {**opts.painting.stats(), "palette_file": str(path.with_name(f"{path.stem}_palette.png"))}
            return out

        return {"job": self.jobs.start(f"Exporting {base}{ext}", run, stoppable=True).id}

    # ------------------------------------------------------------ painting (3MF)

    def _paint_mesh(self, session: Session, args: dict):
        """The mesh whose surface the palette is weighed over: the original with the chosen parts."""
        return session.build(mesh_options(_parse_lists(args)))

    def cmd_paint_palette(self, args: dict) -> dict:
        """The colours that stand best for the model's textures over its surface (paint.suggest_palette),
        for the Export tab's swatches; kept per model, parts and count."""
        from .. import paint as painting

        session = self._session(args)
        colours = int(args.get("colours") or 4)
        if not 2 <= colours <= painting.MAX_COLOURS:
            raise AppError(f"Choose between 2 and {painting.MAX_COLOURS} colours.")
        mesh = self._paint_mesh(session, args)
        key = (session.sid, session.mesh_key, colours)
        with self.lock:
            if key in self.palettes:
                return self.palettes[key]
        textures = _albedo(session.model, mesh)
        if not textures:
            raise AppError("This model has no colour textures to paint from.")
        try:
            palette, coverage = painting.suggest_palette(mesh, textures, colours)
        except ValueError as exc:
            raise AppError(str(exc)) from None
        out = {"colours": [painting.hex_colour(c) for c in palette], "coverage": [round(float(c), 4) for c in coverage]}
        with self.lock:
            self.palettes[key] = out
            while len(self.palettes) > 32:
                self.palettes.popitem(last=False)
        return out

    def cmd_paint_texture(self, args: dict) -> bytes:
        """A material's colour texture reduced to the palette, as a PNG for the 3D view (the Export tab's
        preview of the painted model). ``settings`` is the paint settings as JSON."""
        from .. import paint as painting

        session = self._session(args)
        key = str(args.get("key") or "")
        tex = session.model.textures.get(key)
        if tex is None:
            raise AppError(f"No texture '{key}' in this model.")
        settings = paint_settings(_json_arg(args.get("settings")) or {})
        size = int(args.get("max") or 2048)
        cache_key = (session.sid, "paint", key, settings.key(), size)
        with self.lock:
            if cache_key in self.textures_png:
                return self.textures_png[cache_key]
        if settings.palette:
            palette = np.array([painting.parse_colour(c) for c in settings.palette], np.uint8)
        else:
            palette = np.array([painting.parse_colour(c) for c in self.cmd_paint_palette(
                {**args, "colours": settings.colours})["colours"]], np.uint8)
        img = painting._shrink(painting._rgb(tex.decode()), size)
        index = painting.cleanup(painting.quantize(img, palette), len(palette), settings.cleanup)
        data = texture_png(palette[index], size)
        with self.lock:
            self.textures_png[cache_key] = data
            while len(self.textures_png) > 48:
                self.textures_png.popitem(last=False)
        return data

    def _forget_models(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.part_models.clear()
            self.textures_png.clear()
            self.generated.clear()
            self.meshes.clear()
            self.upscaled.clear()
            self.cuts.clear()
            self.solids.clear()
            self.finished.clear()
            self.palettes.clear()

    # ------------------------------------------------------------ cutting into pieces

    def _cut_source(self, session: Session, args: dict) -> tuple[TriMesh, tuple]:
        """The model the Cut tab cuts, and what it depends on: the enhanced model shown, or the original
        with the parts, options and pose chosen, or the model made watertight (Finish tab)."""
        if args.get("from") == "finished":
            if session.finished is None:
                raise AppError("Make the model watertight first (Finish tab), or cut another model.")
            return session.finished.mesh, ("finished", session.finished_id)
        if args.get("from", "enhanced") == "enhanced":
            if session.result is None:
                raise AppError("Make the enhanced model first (Mesh or Displace tab), or cut the original.")
            return session.result.displaced, ("enhanced", session.result_id)
        options = mesh_options(args)
        return session.build(options), ("original", _options_key(options))

    def cmd_cut(self, args: dict) -> dict:
        """Cut the model into pieces along the planes given, with each cut's joint (a job). The model made solid
        and each set of cuts are kept, so moving one cut only cuts again, and going back to one is instant."""
        from .. import cut as cutting

        if not cutting.available():
            raise AppError("Cutting needs the manifold3d package, which this copy of the app doesn't have.")
        session = self._session(args)
        data = args.get("cut") or {}
        try:
            settings = cutting.settings_from_json(data)
        except (ValueError, KeyError, TypeError) as e:
            raise AppError(f"These cuts can't be read: {e}") from None
        if not settings.cuts:
            raise AppError("Add a cut first.")
        mesh, source = self._cut_source(session, args)
        # the textured model the pieces can be painted from: the one cut, or the one the Finished model was made from
        textured = session.finished.source if source[0] == "finished" else mesh
        file = _file_id(session.model.path)
        skey = (session.sid, file, source, settings.sheet_mm)
        key = (*skey, json.dumps(data, sort_keys=True, default=str))
        with self.lock:
            kept = self.cuts.get(key)
            solid = self.solids.get(skey)

        def run(job: Job):
            result = kept
            if result is None:
                made = solid
                if made is None:
                    made = cutting.make_solid(mesh, settings.sheet_mm, lambda f, m: job.report(0.6 * f, m))
                    job.check()
                    with self.lock:
                        self.solids[skey] = made
                        while len(self.solids) > self.KEEP_SOLIDS:
                            self.solids.popitem(last=False)
                start = 0.6 if solid is None else 0.0
                result = cutting.cut_solid(made, settings, lambda f, m: job.report(start + (1 - start) * f, m))
                result.source = textured
                job.check()
                with self.lock:
                    self.cuts[key] = result
                    self.cuts.move_to_end(key)
                    while len(self.cuts) > self.KEEP_CUTS:
                        self.cuts.popitem(last=False)
            session.cut, session.cut_id = result, _key_id(key)
            return {**self._cut_json(session), "kept": kept is not None, "solid_reused": solid is not None}

        name = session.model.variant or session.model.name
        return {"job": self.jobs.start(f"Cutting {name}", run, stoppable=True).id}

    # ------------------------------------------------------------ finishing for printing

    def cmd_finish(self, args: dict) -> dict:
        """Make the model watertight (a job): every part made solid and joined, the surfaces inside one another
        removed, hollows filled, and as asked gaps closed and loose bits removed. Each result is kept."""
        from .. import finish

        if not finish.available():
            raise AppError("Making models watertight needs the manifold3d package, which this copy of the app doesn't have.")
        session = self._session(args)
        settings = finish.settings_from_json(args.get("watertight") or {})
        src = "original" if args.get("from") == "original" else "enhanced"
        if src == "enhanced" and session.result is None:
            raise AppError("Make the enhanced model first (Mesh or Displace tab), or use the original.")
        mesh, source = self._cut_source(session, {**args, "from": src})
        if mesh.n_faces == 0:
            raise AppError("There's nothing to make watertight: no parts are switched on.")
        key = (session.sid, _file_id(session.model.path), source, settings.key())
        with self.lock:
            kept = self.finished.get(key)

        def run(job: Job):
            result = kept
            if result is None:
                try:
                    result = finish.make_watertight(mesh, settings, lambda f, m: job.report(f, m))
                except ValueError as e:
                    raise AppError(str(e)) from None
                result.source = mesh
                job.check()
                with self.lock:
                    self.finished[key] = result
                    self.finished.move_to_end(key)
                    while len(self.finished) > self.KEEP_FINISHED:
                        self.finished.popitem(last=False)
            session.finished, session.finished_id = result, _key_id(key)
            return {**self._finished_json(session), "kept": kept is not None}

        name = session.model.variant or session.model.name
        return {"job": self.jobs.start(f"Making {name} watertight", run, stoppable=True).id}

    def _finished_json(self, session: Session) -> dict:
        r = session.finished
        return {"id": session.finished_id, "stats": r.stats(), "notes": list(r.notes)}

    def _cut_json(self, session: Session) -> dict:
        r = session.cut
        return {"id": session.cut_id, "pieces": [{**p.stats(), "index": i} for i, p in enumerate(r.pieces)],
                "notes": list(r.notes), "joints": list(r.joints)}

    def cmd_cut_state(self, args: dict) -> dict:
        """The pieces last cut from this model, if any are kept."""
        session = self._session(args)
        return self._cut_json(session) if session.cut is not None else {"id": "", "pieces": []}

    def _surface_colours(self, model: SourceModel, source: TriMesh | None, paint, job: Job, span: float = 0.45):
        """The colours of the textured model a Finished model or cut pieces were made from, to paint them."""
        from .. import paint as painting

        if source is None:
            raise AppError("Make the model again (Finish or Cut tab) to paint it.")
        textures = _albedo(model, source)
        if not textures:
            raise AppError("This model has no colour textures to paint from.")
        try:
            return painting.surface_colours(source, textures, paint, lambda f, m: job.report(span * f, m))
        except ValueError as exc:
            raise AppError(str(exc)) from None

    def _export_pieces(self, session: Session, args: dict) -> dict:
        from .. import cut as cutting
        from .. import paint as painting

        if session.cut is None:
            raise AppError("Cut the model first (Cut tab).")
        ext = str(args.get("format") or ".stl").lower()
        if ext not in FORMATS:
            raise AppError(f"Can't export {ext} files.")
        scale = UNITS.get(str(args.get("units") or "mm"), 1000.0)
        parent = Path(args.get("folder") or self.config.export_folder)
        base = _safe_name(str(args.get("name") or Path(session.model.variant or session.model.name).stem))
        lay_flat = bool(args.get("lay_flat", True))
        pieces = list(session.cut.pieces)
        cut = session.cut
        model = session.model
        paint = paint_settings(args.get("paint") or {}) if ext == ".3mf" else None

        def run(job: Job):
            folder = _free_name(parent / f"{base}_pieces")
            folder.mkdir(parents=True, exist_ok=True)
            paths, total = [], 0
            colours = None
            out: dict = {}
            if ext == ".3mf":
                # every piece painted from the textured model the cut model came from, with the one palette
                colours = self._surface_colours(model, cut.source, paint, job, 0.3)
                write_palette_png(colours.palette, colours.coverage, folder / f"{base}_palette.png")
            start = 0.3 if colours is not None else 0.0
            leaves = split = 0
            notes: list[str] = []
            for i, piece in enumerate(pieces):
                job.report(start + (1 - start) * i / len(pieces), f"Writing {piece.name}")
                job.check()
                mesh = cutting.print_pose(piece) if lay_flat else piece.mesh
                slug = piece.name.lower().replace(" ", "")
                opts = ExportOptions(scale=scale, paint=paint, paint_from=colours, palette_png=False)
                path = export_mesh(mesh, folder / f"{base}_{slug}{ext}", opts)
                if opts.painting is not None:
                    leaves += opts.painting.leaves
                    split += opts.painting.split_faces
                paths.append(str(path))
                total += path.stat().st_size
            if colours is not None:
                coverage = colours.coverage if colours.coverage is not None else np.full(len(colours.palette), 1 / len(colours.palette))
                if leaves > painting.MANY_LEAVES:
                    notes.append(f"The paint has {leaves / 1e6:.1f} million pieces in all, which can make the slicer slow.")
                out["paint"] = {"colours": [painting.hex_colour(c) for c in colours.palette],
                                "coverage": [round(float(c), 4) for c in coverage], "split_faces": split, "leaves": leaves,
                                "notes": notes, "palette_file": str(folder / f"{base}_palette.png")}
            return {"path": str(folder), "files": paths, "bytes": total, **out}

        return {"job": self.jobs.start(f"Exporting {len(pieces)} pieces of {base}", run, stoppable=True).id}


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


def paint_settings(s: dict):
    """paint.PaintSettings from the page's or the CLI's settings dict."""
    from ..paint import MAX_COLOURS, PaintSettings, parse_colour

    out = PaintSettings()
    try:
        out.colours = int(s.get("colours") or out.colours)
        palette = [str(c) for c in (s.get("palette") or []) if str(c).strip()]
        if palette:
            for c in palette:
                parse_colour(c)
            out.palette = palette
        out.print_height_mm = float(s.get("print_height_mm") or out.print_height_mm)
        out.feature_mm = float(s.get("feature_mm") or out.feature_mm)
        out.cleanup = str(s.get("cleanup") or out.cleanup)
        out.max_depth = int(s.get("max_depth") or out.max_depth)
    except (TypeError, ValueError) as exc:
        raise AppError(f"Paint settings: {exc}") from None
    if not 2 <= out.colours <= MAX_COLOURS or (out.palette and not 1 <= len(out.palette) <= MAX_COLOURS):
        raise AppError(f"Choose between 2 and {MAX_COLOURS} colours.")
    if out.print_height_mm <= 0 or out.feature_mm <= 0:
        raise AppError("The print height and the feature size must be above zero.")
    if out.cleanup not in ("none", "light", "strong"):
        raise AppError("Clean-up is none, light or strong.")
    out.max_depth = min(max(out.max_depth, 1), 16)
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


def _json_arg(value):
    """An argument the page sends as JSON in a URL (binary commands take their arguments as query strings)."""
    return json.loads(value) if isinstance(value, str) else value


def _factor(value) -> int:
    """The upsampling factor setting: 0 (automatic), 2 or 4."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if n in (2, 4) else 0


def crop_png(up, upscaled: bool, rect: tuple, size: tuple) -> bytes:
    """``rect`` = (x0, y0, x1, y1) of a map made ready (fractions of it), drawn at ``size`` = (w, h) px:
    texels are blocks when magnified and averaged when there are more than pixels."""
    from PIL import Image

    H, W = (up.p if upscaled else up.p0).shape
    x0, y0, x1, y1 = rect
    w, h = size
    if x1 <= x0 or y1 <= y0:
        raise AppError("Nothing to show: the view is empty.")
    canvas = Image.new("RGBA", (w, h), (52, 52, 56, 255))
    px0, px1 = max(int(np.floor(x0 * W)), 0), min(int(np.ceil(x1 * W)), W)
    py0, py1 = max(int(np.floor(y0 * H)), 0), min(int(np.ceil(y1 * H)), H)
    if px1 > px0 and py1 > py0:
        sx, sy = w / ((x1 - x0) * W), h / ((y1 - y0) * H)  # output pixels per texel
        step = max(int(1 / min(sx, sy)) // 2, 1) if min(sx, sy) < 0.5 else 1  # far out: every nth texel will do
        rgba = up.normal_map(upscaled, (py0, py1, px0, px1))[::step, ::step]
        piece = Image.fromarray(np.ascontiguousarray(rgba))
        pw, ph = max(int(round((px1 - px0) * sx)), 1), max(int(round((py1 - py0) * sy)), 1)
        piece = piece.resize((pw, ph), Image.NEAREST if min(sx, sy) >= 1 else Image.BOX)
        canvas.paste(piece, (int(round((px0 - x0 * W) * sx)), int(round((py0 - y0 * H) * sy))))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


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
