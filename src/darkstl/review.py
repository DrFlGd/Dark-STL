"""Export one model's files for review: what Dark STL reads for it, in a zip, with what it made of them.

When a model doesn't open, pose or texture the way it should, these files let the problem be looked at
without the game. The zip holds:

- ``<game>/``: the model's own files and everything it reads from the game (textures, skeleton,
  animations, and what it reads from inside the game's archives, written out as loose files), as Collect
  copies them. The folder is a collected folder, so Dark STL opens it with *Add collected files*.
- with the model package option, ``<game> packages/``: the model package Dark STL imports the model as
  (glTF, materials, textures and sampled animations), opened by *Add collected files* too.
- ``report.txt`` and ``report.json``: what Dark STL made of the model. That covers its materials and the
  textures they got (and how they were found), the UV ranges each material's faces use, its animations and
  those that couldn't be read, and its notes.
- ``log.txt``: what Dark STL logged while opening it.

Game-wide libraries (a compression DLL) are left out. They aren't the game's data and the files are
written out already decompressed.
"""

from __future__ import annotations

import json
import logging
import platform
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from . import collect

LIBRARIES = (".dll", ".so", ".dylib")
REPORT_TEXT, REPORT_JSON, LOG = "report.txt", "report.json", "log.txt"
MAX_LOG_LINES = 5000


def keep(f: collect.RelatedFile) -> bool:
    """Whether a file a model needs goes into the zip: not a game-wide library."""
    return Path(f.save_as or f.path).suffix.lower() not in LIBRARIES


@contextmanager
def capture_log() -> Iterator[list[str]]:
    """Every line Dark STL logs (debug included) while the block runs."""
    lines: list[str] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if len(lines) < MAX_LOG_LINES:
                try:
                    lines.append(self.format(record))
                except Exception:  # noqa: BLE001 - a record that can't be formatted is left out
                    pass

    handler = _Keep(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(relativeCreated)8.0f ms %(levelname)-7s %(name)s: %(message)s"))
    logger = logging.getLogger("darkstl")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def _uv_ranges(mesh, materials: list) -> list[dict]:
    """For each material, the range its faces' UVs cover and how many corners fall outside 0..1 (a model
    whose UVs reach into other tiles takes a texture per tile)."""
    out = []
    uvs = mesh.corner_uvs
    if uvs is None or not len(mesh.faces):
        return out
    fm = np.asarray(mesh.face_material)
    for i in sorted(set(int(x) for x in np.unique(fm))):
        sel = np.asarray(uvs[fm == i], np.float64).reshape(-1, 2)
        if not len(sel):
            continue
        finite = sel[np.isfinite(sel).all(axis=1)]
        if not len(finite):
            continue
        tiles = np.floor(finite).astype(np.int64)
        tile_counts: dict[str, int] = {}
        for (u, v), n in zip(*np.unique(tiles, axis=0, return_counts=True)):
            tile_counts[f"{u},{v}"] = int(n)
        outside = int(((finite < 0) | (finite > 1)).any(axis=1).sum())
        out.append({"material": materials[i].name if 0 <= i < len(materials) else str(i), "faces": int((fm == i).sum()),
                    "u": [round(float(finite[:, 0].min()), 4), round(float(finite[:, 0].max()), 4)],
                    "v": [round(float(finite[:, 1].min()), 4), round(float(finite[:, 1].max()), 4)],
                    "outside_0_1": outside, "corners": int(len(finite)),
                    "tiles": dict(sorted(tile_counts.items(), key=lambda kv: -kv[1])[:12])})
    return out


def _texture(model, key: str | None) -> dict | None:
    if not key:
        return None
    t = model.textures.get(key)
    if t is None:
        return {"key": key, "missing": True}
    out = {"key": key, "name": t.name, "source": t.source}
    try:
        out["format"] = t.describe()
    except Exception as exc:  # noqa: BLE001 - the report says what went wrong
        out["format_error"] = str(exc) or type(exc).__name__
    return out


def _animations(model) -> dict | None:
    anims = model.animations
    if anims is None:
        return None
    out: dict = {}
    try:
        listed = anims.list()
        out["listed"] = [a.as_dict() if hasattr(a, "as_dict") else str(a) for a in listed]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc) or type(exc).__name__
    for name in ("note", "skeleton_package"):
        v = getattr(anims, name, None)
        if isinstance(v, str) and v:
            out[name] = v
    unreadable = getattr(anims, "unreadable", None)
    if callable(unreadable):
        try:
            out["unreadable"] = [{"name": n, "why": why} for n, why in unreadable()]
        except Exception as exc:  # noqa: BLE001
            out["unreadable_error"] = str(exc) or type(exc).__name__
    debug = getattr(anims, "debug", None)
    if callable(debug):
        try:
            out["skeleton"] = debug()
        except Exception as exc:  # noqa: BLE001
            out["skeleton_error"] = str(exc) or type(exc).__name__
    return out


def model_report(model, error: str = "") -> dict:
    """What Dark STL made of an opened model (``model`` None: it didn't open, and ``error`` says why)."""
    if model is None:
        return {"opened": False, "error": error}
    out: dict = {"opened": True, "name": model.name, "file": str(model.path), "importer": model.importer,
                 "variant": model.variant, "variants": len(model.variants), "game": model.game,
                 "info": list(model.info), "notes": list(model.notes), "pose_note": model.pose_note,
                 "parts": [{"name": p.name, "material": p.material, "vertices": p.vertices, "triangles": p.triangles}
                           for p in model.parts]}
    mats = []
    for m in model.materials:
        mats.append({"name": m.name, "guessed": m.guessed, "details": list(m.details),
                     "albedo": _texture(model, m.albedo), "normal": _texture(model, m.normal),
                     "height": _texture(model, m.height), "specular": _texture(model, m.specular),
                     "detail_normal": _texture(model, m.detail_normal), "normal_green": m.normal_green})
    out["materials"] = mats
    out["textures"] = {k: {"name": t.name, "source": t.source} for k, t in model.textures.items()}
    try:
        mesh = model.build_mesh()
        out["mesh"] = {"vertices": mesh.n_vertices, "faces": mesh.n_faces,
                       "size": [round(float(x), 4) for x in (mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0))]
                       if mesh.n_vertices else None}
        out["uv_ranges"] = _uv_ranges(mesh, model.materials)
    except Exception as exc:  # noqa: BLE001
        out["mesh_error"] = str(exc) or type(exc).__name__
    out["animations"] = _animations(model)
    return out


def report_text(report: dict) -> str:
    """The report for reading: a summary first, then everything as indented JSON."""
    head = report.get("export", {})
    model = report.get("model", {})
    lines = [f"Dark STL {head.get('app_version', '')}: model files for review", "",
             f"Model: {head.get('label', '')} ({head.get('file', '')})",
             f"Game: {head.get('game', '')} · importer {head.get('importer', '')} · variant {head.get('variant') or '-'}",
             f"Exported {head.get('time', '')} on {head.get('system', '')}", ""]
    if not model.get("opened"):
        lines += [f"It didn't open: {model.get('error', '')}", ""]
    else:
        mesh = model.get("mesh") or {}
        lines.append(f"Mesh: {mesh.get('vertices', '?')} vertices, {mesh.get('faces', '?')} faces")
        for note in model.get("notes") or []:
            lines.append(f"Note: {note}")
        lines.append("")
        lines.append("Materials:")
        uv = {r["material"]: r for r in model.get("uv_ranges") or []}
        for m in model.get("materials") or []:
            a, n = m.get("albedo") or {}, m.get("normal") or {}
            lines.append(f"  {m['name']}: colour {a.get('name', '-')}, normal {n.get('name', '-')}")
            r = uv.get(m["name"])
            if r:
                lines.append(f"    UVs u {r['u'][0]}..{r['u'][1]}, v {r['v'][0]}..{r['v'][1]}; "
                             f"{r['outside_0_1']} of {r['corners']} corners outside 0..1")
        anims = model.get("animations")
        if anims:
            lines += ["", f"Animations: {len(anims.get('listed') or [])} listed, "
                          f"{len(anims.get('unreadable') or [])} couldn't be read"]
            if anims.get("note"):
                lines.append(f"  {anims['note']}")
            for u in (anims.get("unreadable") or [])[:20]:
                lines.append(f"  {u['name']}: {u['why']}")
        elif model.get("pose_note"):
            lines += ["", f"Pose: {model['pose_note']}"]
    game = report.get("game") or {}
    if game.get("paks"):
        lines += ["", f"Game archives: {len(game['paks'])} paks, {game.get('replaced_files', 0):,} files replaced by "
                      f"later paks, {len(game.get('locked') or [])} locked"]
        for f, w in (game.get("files") or {}).items():
            if w and w.get("replaces"):
                lines.append(f"  {f}: read from {w.get('container')}, replacing the copy in "
                             f"{', '.join(str(r.get('container')) for r in w['replaces'])}")
    pkg = report.get("package")
    if pkg:
        lines += ["", f"Model package: {pkg.get('error') or Path(pkg.get('folder', '')).name}"]
    files = report.get("files") or {}
    lines += ["", f"Files: {files.get('files', 0)} ({files.get('bytes', 0):,} bytes)", "",
              "Everything:", json.dumps({k: v for k, v in report.items() if k != "files"}, indent=2, default=str),
              "", "Files:", *[f"  {x}" for x in files.get("list") or []]]
    return "\n".join(lines) + "\n"


def write(plan: collect.Plan | None, dest: Path, report: dict, log_lines: list[str],
          progress: Callable[[float, str], None] | None = None, folders: list[tuple[Path, str]] = ()) -> dict:
    """Write the zip: the plan's files below ``<game>/`` (with the note that makes it a collected folder), each
    of ``folders`` (a folder, and its name in the zip: a model package's folder), the report and the log.
    Returns where it went and how big it is."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    extra = [(p, f"{name}/{p.relative_to(folder).as_posix()}") for folder, name in folders
             for p in sorted(Path(folder).rglob("*")) if p.is_file()]
    planned = list(plan.files) if plan is not None else []
    total = (sum(f.size for f in planned) + sum(p.stat().st_size for p, _ in extra)) or 1
    done = 0
    written = 0
    failed: list[str] = []
    listed: list[str] = []
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            if plan is not None:
                top = plan.folder.name
                for f in planned:
                    rel = f.target.relative_to(plan.folder).as_posix()
                    if progress:
                        progress(min(done / total, 0.99), f"Adding {f.source.name}")
                    try:
                        if f.extract is not None:
                            z.writestr(f"{top}/{rel}", f.extract())
                        else:
                            z.write(f.source, f"{top}/{rel}")
                        written += 1
                        listed.append(f"{top}/{rel}")
                    except Exception as exc:  # noqa: BLE001 - the report says which file couldn't be added
                        failed.append(f"{rel}: {exc}")
                    done += f.size
                from . import __version__

                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                manifest = {"kind": "collection", "app": "Dark STL", "format": 1, "game": plan.game, "packages": False,
                            "models": sorted(set(plan.models), key=str.lower), "sources": [], "created": now,
                            "updated": now, "version": __version__, "review": True}
                z.writestr(f"{top}/{collect.MANIFEST}", json.dumps(manifest, indent=2))
            for path, arc in extra:
                if progress:
                    progress(min(done / total, 0.99), f"Adding {path.name}")
                z.write(path, arc)
                listed.append(arc)
                written += 1
                done += path.stat().st_size
            report = {**report, "files": {"files": written, "bytes": done, "failed": failed, "list": listed}}
            z.writestr(REPORT_JSON, json.dumps(report, indent=2, default=str))
            z.writestr(REPORT_TEXT, report_text(report))
            z.writestr(LOG, "\n".join(log_lines) + "\n")
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    if progress:
        progress(1.0, "Done")
    return {"path": str(dest), "bytes": dest.stat().st_size, "files": written, "failed": len(failed)}


def header(entry: dict, variant: str | None) -> dict:
    """What the report says about where it came from."""
    from . import __version__

    return {"app_version": __version__, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "system": f"{platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}",
            "label": entry.get("label"), "file": entry.get("file"), "game": entry.get("game"),
            "category": entry.get("category"), "group": entry.get("group"), "importer": entry.get("importer"),
            "variant": variant}


__all__ = ["capture_log", "header", "keep", "model_report", "report_text", "write"]
