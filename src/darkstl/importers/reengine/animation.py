"""Posing RE Engine models with the game's animations.

A model's bones are matched to a motion's bone clips by name hash; a bone a motion has no clip for
(or no track of a kind for) keeps its bind-pose transform from the mesh, so it follows its parent.
The root bones' translation (the walk across the room) is left out, as for FromSoftware models:
the model stays where its bind pose puts it.
The motions that may fit a model are found by its character id (:func:`files.find_motion_lists`);
listing them reads each file's motion headers and the hashes of the bones they move, and only the
motions that move at least one of the model's bones are offered. What a listing finds is kept in
the importer's cache, so a model's list comes back quickly the next time.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from typing import Iterable

import numpy as np

from ...errors import FormatError
from ..fromsoft.animation import AnimationInfo, compose_hierarchy
from . import files
from .formats import motion as motfmt
from .formats import pak as pakfmt
from .formats.mesh import Bone

CACHE_VERSION = 2


def bone_hashes(names: list[str]) -> np.ndarray:
    """MurmurHash3 of each bone name as UTF-16LE, case kept: what motions name bones by."""
    if not names:
        return np.zeros(0, np.uint32)
    units = [np.frombuffer(n.encode("utf-16-le"), "<u2") for n in names]
    lengths = np.array([len(u) for u in units], np.int64)
    width = int(lengths.max())
    width += width % 2
    arr = np.zeros((len(units), max(width, 2)), np.uint32)
    for i, u in enumerate(units):
        arr[i, :len(u)] = u
    return pakfmt._murmur3_rows(arr, lengths)


def _quat_matrices(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, np.float64).reshape(-1, 4)
    n = np.linalg.norm(q, axis=1)
    n[n == 0] = 1
    x, y, z, w = (q / n[:, None]).T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], 1)


def bind_locals(bones: list[Bone]) -> np.ndarray:
    """(n, 4, 4) local bind matrices for column vectors (the file keeps the translation in the last row)."""
    local = np.array([b.local for b in bones], np.float64).reshape(-1, 4, 4)
    if len(local) and np.abs(local[:, 3, :3]).sum() > np.abs(local[:, :3, 3]).sum():
        local = np.transpose(local, (0, 2, 1))
    return local


# ---------------------------------------------------------------------------
# What a motion file holds (cached)


@dataclass
class _Entry:
    name: str
    path: Path
    offset: int
    frames: int
    duration: float
    fps: float
    label: str
    moves: int = 0  # bones of the model it has clips for
    fixed: bool = False  # every track one key: a held pose


_lock = threading.Lock()
_bytes: OrderedDict[str, bytes] = OrderedDict()  # the last few motion files read


def _read(path: Path) -> bytes:
    key = str(path)
    with _lock:
        if key in _bytes:
            _bytes.move_to_end(key)
            return _bytes[key]
    data = files.read_bytes(path)
    with _lock:
        _bytes[key] = data
        while len(_bytes) > 6:
            _bytes.popitem(last=False)
    return data


def _summary(path: Path) -> dict:
    """{'hashes': bone hashes moved anywhere in the file,
    'motions': [[name, offset, frame count, fps, [its bone hashes], most keys in a track], ...]}"""
    data = _read(path)
    if motfmt.is_motlist(data):
        headers = motfmt.read_motlist(data).motions
    elif motfmt.is_mot(data):
        headers = [motfmt.read_header(data, 0)]
    else:
        raise FormatError(f"{path.name} is not an RE Engine motion file")
    hashes: set[int] = set()
    motions = []
    for h in headers:
        try:
            own = motfmt.clip_hashes(data, h)
            keys = max(motfmt.clip_key_counts(data, h), default=0)
        except Exception:  # noqa: BLE001 - a broken header hides only that motion
            continue
        hashes.update(own)
        motions.append([h.name, h.offset, h.frame_count, h.fps, sorted(set(own)), keys])
    return {"hashes": sorted(hashes), "motions": motions}


class _SummaryCache:
    """Motion file summaries on disk, one JSON file per game folder, keyed by path and size."""

    def __init__(self, folder: Path | None):
        self.folder = folder
        self._loaded: dict[str, dict] = {}

    def _file(self, root: str) -> Path | None:
        if self.folder is None:
            return None
        return self.folder / (re.sub(r"[^\w.-]", "_", root)[-120:] + ".json")

    def get(self, root: str, paths: list[Path]) -> dict[str, dict]:
        table = self._loaded.get(root)
        if table is None:
            table = {}
            f = self._file(root)
            if f is not None and f.is_file():
                try:
                    raw = json.loads(f.read_text())
                    if raw.get("version") == CACHE_VERSION:
                        table = raw.get("files", {})
                except (OSError, ValueError):
                    table = {}
            self._loaded[root] = table
        out, changed = {}, False
        for p in paths:
            key = str(p)
            size = files.size_of(p)
            got = table.get(key)
            if got is None or got.get("size") != size:
                try:
                    got = dict(_summary(p), size=size)
                except FormatError:
                    got = {"size": size, "hashes": [], "motions": [], "unreadable": True}
                table[key] = got
                changed = True
            out[key] = got
        if changed:
            f = self._file(root)
            if f is not None:
                try:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps({"version": CACHE_VERSION, "files": table}))
                except OSError:
                    pass
        return out


_caches: dict[str, _SummaryCache] = {}


def _cache_for(folder: Path | None) -> _SummaryCache:
    key = str(folder)
    with _lock:
        if key not in _caches:
            _caches[key] = _SummaryCache(folder)
        return _caches[key]


# ---------------------------------------------------------------------------
# The animations of one model


class REAnimations:
    """The motions that can pose one RE Engine model, with the interface the Pose tab uses
    (``list``, ``info``, ``clamp``, ``skeleton``) and :meth:`skin_matrices` for the importer."""

    note = ("Animations come from the game's .motlist files filed under this model's character id, "
            "read from the game folder (or its pak archives).")

    def __init__(self, bones: list[Bone], candidates: list[Path], cache: Path | None = None,
                 used_bones: Iterable[int] | None = None):
        """``used_bones``: the bones the mesh's vertices are weighted to; the count of bones an animation
        moves is then taken over them and their ancestors, the bones whose motion shows on the mesh."""
        self.bones = bones
        self.names_ = [b.name for b in bones]
        self.parents = np.array([b.parent for b in bones], np.int64)
        self.local = bind_locals(bones)
        self.bind_world = compose_hierarchy(self.local, self.parents)
        self.hashes = bone_hashes(self.names_)
        self._index = {int(h): i for i, h in enumerate(self.hashes)}
        self.used_bones = sorted(int(b) for b in used_bones) if used_bones is not None else None
        self.moving_bones = self._with_ancestors(self.used_bones) if self.used_bones is not None \
            else list(range(len(bones)))
        self.note = type(self).note
        self.candidates = list(candidates)
        self._cache = _cache_for(cache)
        self._entries: dict[str, _Entry] | None = None
        self._clips: OrderedDict[str, tuple[list[motfmt.BoneClip], float]] = OrderedDict()

    @property
    def skeleton(self) -> list[str]:
        return self.names_

    def _with_ancestors(self, bones: list[int]) -> list[int]:
        out: set[int] = set()
        for b in bones:
            while 0 <= b < len(self.parents) and b not in out:
                out.add(b)
                b = int(self.parents[b])
        return sorted(out)

    def rigid_description(self) -> str | None:
        """Why posing can't bend this mesh, when its vertices are bound whole to a few bones (a display
        figure baked into one piece, or an object fixed to a hand bone); None for an ordinary skinned mesh."""
        used = self.used_bones
        if used is None or not used or len(used) > 8 or len(used) > len(self.bones) // 3:
            return None
        names = ", ".join(self.names_[b] for b in used[:4]) + (", …" if len(used) > 4 else "")
        count = "one bone" if len(used) == 1 else f"{len(used)} of its {len(self.bones)} bones"
        return (f"This mesh is bound whole to {count} ({names}), so animations move it as one piece "
                "but can't bend its joints. For a figure that poses, use the game's playable copy of the "
                "character (the meshes under its own character folder), not the gallery display model.")

    def _root(self) -> str:
        index = files.index_for(self.candidates[0]) if self.candidates else None
        return str(index.root) if index is not None else "loose"

    def entries(self) -> dict[str, _Entry]:
        if self._entries is None:
            summaries = self._cache.get(self._root(), self.candidates) if self.candidates else {}
            out: dict[str, _Entry] = {}
            own = set(self._index)
            shows = {int(self.hashes[b]) for b in self.moving_bones}
            for p in self.candidates:
                s = summaries.get(str(p))
                if not s or not own.intersection(s["hashes"]):
                    continue
                stem = files.file_stem(p.name)
                where = p.as_posix().split("/natives/", 1)[-1]
                for i, (name, offset, frame_count, fps, moved, keys) in enumerate(s["motions"]):
                    label = f"{stem} / {name or f'motion {i}'}"
                    key, n = label, 2
                    while key in out:
                        key, n = f"{label} ({n})", n + 1
                    fc = float(frame_count)
                    frames = max(1, int(np.floor(fc + 1e-4)) + 1)
                    rate = float(fps) or 60.0
                    out[key] = _Entry(key, p, int(offset), frames, fc / rate, rate, where,
                                      len(shows.intersection(moved)), int(keys) <= 1)
            self._entries = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
        return self._entries

    @property
    def names(self) -> list[str]:
        return list(self.entries())

    def __len__(self) -> int:
        return len(self.entries())

    def __contains__(self, name: str) -> bool:
        return name in self.entries()

    def files_used(self) -> list[Path]:
        """The motion files that hold at least one of the listed animations."""
        seen: dict[str, Path] = {}
        for e in self.entries().values():
            seen.setdefault(str(e.path), e.path)
        return list(seen.values())

    def _entry(self, name: str) -> _Entry:
        e = self.entries().get(name)
        if e is None:
            raise FormatError(f"No animation named {name!r}")
        return e

    def info(self, name: str) -> AnimationInfo:
        e = self._entry(name)
        return AnimationInfo(e.name, e.frames, e.duration, e.fps, e.label, e.moves, e.fixed)

    def list(self) -> list[AnimationInfo]:
        return [AnimationInfo(e.name, e.frames, e.duration, e.fps, e.label, e.moves, e.fixed)
                for e in self.entries().values()]

    def clamp(self, name: str, frame: int) -> int:
        return min(max(int(frame), 0), self.info(name).frames - 1)

    def _clips_of(self, name: str) -> tuple[list[motfmt.BoneClip], float]:
        """A motion's clips and the factor its translations need to be in the mesh's units."""
        if name in self._clips:
            self._clips.move_to_end(name)
            return self._clips[name]
        e = self._entry(name)
        data = _read(e.path)
        header = motfmt.read_header(data, e.offset)
        clips = motfmt.read_clips(data, header)
        found = (clips, self._unit_factor(clips))
        self._clips[name] = found
        while len(self._clips) > 4:
            self._clips.popitem(last=False)
        return found

    def _unit_factor(self, clips: list[motfmt.BoneClip]) -> float:
        """1, or 0.01 / 100 when a motion's bone offsets come out a hundred times the mesh's (centimetres
        against metres): judged on the median length ratio of the bones both place."""
        ratios = []
        for c in clips:
            i = self._index.get(c.hash)
            if i is None or c.translation is None or self.parents[i] < 0:
                continue
            bind = np.linalg.norm(self.local[i, :3, 3])
            moved = np.linalg.norm(c.translation.values[0])
            if bind > 1e-4 and moved > 1e-6:
                ratios.append(moved / bind)
        if len(ratios) < 3:
            return 1.0
        r = float(np.median(ratios))
        if 30 < r < 300:
            return 0.01
        if 1 / 300 < r < 1 / 30:
            return 100.0
        return 1.0

    def local_pose(self, name: str, frame: float) -> np.ndarray:
        """(n, 4, 4) local matrices of the model's bones at ``frame`` of motion ``name``."""
        clips, factor = self._clips_of(name)
        local = self.local.copy()
        for c in clips:
            i = self._index.get(c.hash)
            if i is None:
                continue
            m = local[i]
            scale = np.linalg.norm(m[:3, :3], axis=0)
            scale[scale == 0] = 1
            rot = m[:3, :3] / scale
            t = m[:3, 3].copy()
            if c.translation is not None and self.parents[i] >= 0:  # root motion is left out
                t = c.translation.at(frame) * factor
            if c.rotation is not None:
                rot = _quat_matrices(c.rotation.at(frame))[0]
            if c.scale is not None:
                scale = c.scale.at(frame)
            out = np.eye(4)
            out[:3, :3] = rot * scale[None, :]
            out[:3, 3] = t
            local[i] = out
        return local

    def world_matrices(self, name: str, frame: float) -> np.ndarray:
        return compose_hierarchy(self.local_pose(name, frame), self.parents)

    def skin_matrices(self, name: str, frame: float) -> np.ndarray:
        """(n, 4, 4) matrices that carry a vertex from the bind pose into the pose, per bone."""
        return self.world_matrices(name, frame) @ np.linalg.pinv(self.bind_world)


def skin(positions: np.ndarray, normals: np.ndarray | None, joints: np.ndarray, weights: np.ndarray,
         matrices: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Linear blend skinning: each vertex moved by its bones' matrices, weighted. Vertices without weights
    stay where they are."""
    valid = (joints >= 0) & (joints < len(matrices)) & (weights > 0)
    w = np.where(valid, weights, 0.0)
    total = w.sum(axis=1)
    j = np.clip(joints, 0, len(matrices) - 1)
    blended = np.einsum("nk,nkij->nij", w, matrices[j])
    keep = total <= 0
    blended[keep] = np.eye(4)
    p = np.einsum("nij,nj->ni", blended[:, :3, :3], positions) + blended[:, :3, 3]
    n = np.einsum("nij,nj->ni", blended[:, :3, :3], normals) if normals is not None else None
    return p, n


__all__ = ["REAnimations", "bind_locals", "bone_hashes", "skin"]
