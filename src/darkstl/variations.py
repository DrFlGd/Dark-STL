"""Saved variations: enhanced models (Generate's results) kept with the original, under names of the user's own.

Each is a folder beside the model's package (see :mod:`darkstl.package`), whether or not the model has been
imported as one yet::

    <game> packages/<path in the game>.darkpkg/variations/<id>/
        variation.json   its name, when it was made, the model inside the file it was made from, the settings,
                         parts, options and pose it was made with, and what the app showed of it
        mesh.npz         the enhanced mesh (vertices, triangles, UVs, materials, parts, displacement)
        thumb.png        a picture of it

The package's own files are never touched, and a package written again (the game imported again) keeps them.
A variation opens in the app as the model's enhanced model: shown, exported, made watertight or cut like a
fresh one, or its settings taken up again as the starting point for new ones.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import numpy as np

from .errors import FormatError
from .geometry.mesh import TriMesh

FORMAT = "darkstl-variation"
VERSION = 1
FOLDER = "variations"
INFO = "variation.json"
MESH = "mesh.npz"
THUMB = "thumb.png"
_ID = re.compile(r"^[0-9a-z-]{6,64}$")


def folder_of(package_folder: Path) -> Path:
    """Where the variations of the model packaged (or to be packaged) at ``package_folder`` are kept."""
    return Path(package_folder) / FOLDER


def _check_id(vid: str) -> str:
    vid = str(vid or "")
    if not _ID.match(vid):
        raise FormatError("There's no such variation.")
    return vid


def write_mesh(path: Path, mesh: TriMesh) -> None:
    """The mesh, compactly: positions as 32-bit floats (a fifth of a micrometre on a two-metre model)."""
    arrays = {"vertices": mesh.vertices.astype(np.float32),
              "faces": mesh.faces.astype(np.int32 if mesh.n_vertices < 2 ** 31 else np.int64),
              "face_material": np.asarray(mesh.face_material, np.int32),
              "face_group": np.asarray(mesh.face_group, np.int32)}
    if mesh.corner_uvs is not None:
        arrays["corner_uvs"] = np.asarray(mesh.corner_uvs, np.float32)
    if mesh.corner_normals is not None:
        arrays["corner_normals"] = np.asarray(mesh.corner_normals, np.float16)
    for key, value in mesh.metadata.items():  # per-vertex numbers (the displacement): kept; the rest isn't needed
        if isinstance(value, np.ndarray) and value.dtype.kind in "fiu" and len(value) == mesh.n_vertices:
            arrays[f"meta_{key}"] = value.astype(np.float32) if value.dtype.kind == "f" else value
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, path)


def read_mesh(path: Path) -> TriMesh:
    try:
        with np.load(path, allow_pickle=False) as z:
            data = {k: z[k] for k in z.files}
    except (OSError, ValueError) as exc:
        raise FormatError(f"The variation's mesh couldn't be read: {exc}") from exc
    meta = {k[5:]: v.astype(np.float64) if v.dtype.kind == "f" else v for k, v in data.items() if k.startswith("meta_")}
    normals = data.get("corner_normals")
    return TriMesh(data["vertices"].astype(np.float64), data["faces"].astype(np.int64), data.get("corner_uvs"),
                   data["face_material"], data["face_group"], meta,
                   normals.astype(np.float32) if normals is not None else None)


def save(folder: Path, name: str, mesh: TriMesh, info: dict, thumb_png: bytes | None = None) -> dict:
    """Keep ``mesh`` as a new variation in ``folder`` (see :func:`folder_of`); ``info`` is what it was made
    with (settings, parts, pose, the variant, what the app shows of it). Its row as :func:`listed` gives it."""
    from . import __version__

    vid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    here = Path(folder) / vid
    here.mkdir(parents=True, exist_ok=False)
    try:
        write_mesh(here / MESH, mesh)
        if thumb_png:
            (here / THUMB).write_bytes(thumb_png)
        data = {**info, "format": FORMAT, "version": VERSION, "id": vid, "name": name.strip() or vid,
                "made": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": __version__}
        (here / INFO).write_text(json.dumps(data, indent=1, default=_plain), "utf-8")
    except BaseException:
        shutil.rmtree(here, ignore_errors=True)
        raise
    return _row(here, data)


def _plain(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return str(v)


def _row(here: Path, data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in ("format", "version")}
    out["thumb"] = (here / THUMB).is_file()
    try:
        out["bytes"] = sum(p.stat().st_size for p in here.iterdir() if p.is_file())
    except OSError:
        out["bytes"] = 0
    return out


def read(folder: Path, vid: str) -> dict:
    here = Path(folder) / _check_id(vid)
    try:
        data = json.loads((here / INFO).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise FormatError("There's no such variation (was it deleted?).") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FormatError("That isn't a Dark STL variation.")
    if int(data.get("version", VERSION)) > VERSION:
        raise FormatError("That variation was saved by a newer Dark STL.")
    return _row(here, data)


def listed(folder: Path, variant: str | None = None) -> list[dict]:
    """The variations kept in ``folder`` of the model ``variant`` of the file (None: its only one), newest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out = []
    for here in folder.iterdir():
        if not here.is_dir() or not _ID.match(here.name):
            continue
        try:
            row = read(folder, here.name)
        except FormatError:
            continue
        if (row.get("variant") or None) == (variant or None):
            out.append(row)
    return sorted(out, key=lambda r: (r.get("made") or "", r["id"]), reverse=True)


def mesh_of(folder: Path, vid: str) -> TriMesh:
    return read_mesh(Path(folder) / _check_id(vid) / MESH)


def thumb_of(folder: Path, vid: str) -> bytes | None:
    try:
        return (Path(folder) / _check_id(vid) / THUMB).read_bytes()
    except OSError:
        return None


def rename(folder: Path, vid: str, name: str) -> dict:
    here = Path(folder) / _check_id(vid)
    data = json.loads((here / INFO).read_text("utf-8"))
    data["name"] = name.strip() or data.get("name") or vid
    tmp = here / (INFO + ".part")
    tmp.write_text(json.dumps(data, indent=1), "utf-8")
    os.replace(tmp, here / INFO)
    return _row(here, data)


def delete(folder: Path, vid: str) -> None:
    here = Path(folder) / _check_id(vid)
    if not (here / INFO).is_file():
        raise FormatError("There's no such variation (was it deleted already?).")
    shutil.rmtree(here)
    for empty in (Path(folder), Path(folder).parent):  # the last one: no empty folders left behind (a package's
        try:                                            # own folder is only removed when nothing else is in it)
            empty.rmdir()
        except OSError:
            break


__all__ = ["FOLDER", "FORMAT", "delete", "folder_of", "listed", "mesh_of", "read", "read_mesh", "rename", "save",
           "thumb_of", "write_mesh"]
