"""Posing id Tech skinned models with their ``.md6anim`` animations.

A model's animations are those whose header names its skeleton. They are looked for in the model's own folder
tree first (``md6/monsters/imp/...``); which skeleton each animation is for is kept in the importer's cache, so
the headers are read once. Matrices are carried into the export frame (metres) by scaling translations.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...posing import compose_hierarchy
from .formats import md6anim as animfmt
from .formats.md6skel import Skeleton, trs_matrices
from .store import Store


@dataclass
class AnimationInfo:
    name: str
    frames: int
    duration: float
    fps: float
    source: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "frames": self.frames, "duration": round(self.duration, 4),
                "fps": round(self.fps, 3), "source": self.source}


def anim_folder(skeleton: str) -> str:
    """The folder whose animations may be for a skeleton: ``md6/<kind>/<name>/``."""
    parts = skeleton.lower().split("/")
    return "/".join(parts[:3]) + "/" if len(parts) > 3 else "/".join(parts[:-1]) + "/"


class _SkeletonIndex:
    """animation name -> (skeleton, frames, fps), kept per game in the cache."""

    def __init__(self, cache: Path | None, key: str):
        self.file = cache / f"anims-{key}.json" if cache else None
        self.data: dict[str, list] = {}
        self.lock = threading.Lock()
        if self.file is not None and self.file.is_file():
            try:
                self.data = json.loads(self.file.read_text("utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def save(self) -> None:
        if self.file is None:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), "utf-8")
            tmp.replace(self.file)
        except OSError:
            pass


_indexes: dict[str, _SkeletonIndex] = {}


def _index_for(store: Store, cache: Path | None) -> _SkeletonIndex:
    key = hashlib.sha1(f"{store.root}|{store.version}|{len(store.entries)}".encode()).hexdigest()[:16]
    idx = _indexes.get(key)
    if idx is None:
        idx = _indexes[key] = _SkeletonIndex(cache, key)
    return idx


def anim_info(data: bytes, darkages: bool = False) -> tuple[str, int, float]:
    if darkages:  # the header says it all; the frame sets may be in the StreamDB
        h = animfmt.read_darkages_header(data)
        return h.skeleton, h.frames, h.fps
    a = animfmt.read_md6_anim(data)
    return a.skeleton, a.frames, a.fps


def read_anim(store: Store, name: str) -> animfmt.Md6Anim:
    """An animation of the store's game, with its streamed frame sets."""
    data = store.read("anim", name)
    if not store.profile.darkages:
        return animfmt.read_md6_anim(data)
    hash_ = store.loc("anim", name).hash
    return animfmt.read_darkages_anim(
        data, lambda layout, size: store.stream(store.anim_piece_id(hash_, layout), size))


def anim_pieces(store: Store, name: str) -> list[tuple[int, int]]:
    """The StreamDB pieces (id, size) an animation's frame sets are in (The Dark Ages)."""
    if not store.profile.darkages:
        return []
    data = store.read("anim", name)
    hash_ = store.loc("anim", name).hash
    return [(store.anim_piece_id(hash_, layout), size) for layout, size in animfmt.darkages_pieces(data)]


class IdAnimations:
    def __init__(self, store: Store, skeleton_name: str, skeleton: Skeleton, cache: Path | None = None):
        self.store = store
        self.skeleton_name = skeleton_name
        self.skeleton = skeleton
        self.unit = store.profile.unit
        self.note: str | None = None
        self._cache = cache
        self._infos: dict[str, AnimationInfo] | None = None
        self._anims: dict[str, animfmt.Md6Anim] = {}
        self._lock = threading.Lock()
        self.names_ = list(skeleton.names)
        self.parents = skeleton.parents.copy()
        s = np.diag([self.unit, self.unit, self.unit, 1.0])
        self._s, self._si = s, np.linalg.inv(s)
        self.local = self._to_export(skeleton.bind_local())
        self.bind_world = compose_hierarchy(self.local, self.parents)

    def _to_export(self, m: np.ndarray) -> np.ndarray:
        return self._s @ m @ self._si

    def candidates(self) -> list[str]:
        folder = anim_folder(self.skeleton_name)
        return [n for n in self.store.names("anim") if n.lower().startswith(folder)]

    def infos(self) -> dict[str, AnimationInfo]:
        with self._lock:
            if self._infos is not None:
                return self._infos
            idx = _index_for(self.store, self._cache)
            out: dict[str, AnimationInfo] = {}
            changed = False
            for name in self.candidates():
                got = idx.data.get(name)
                if got is None:
                    try:
                        got = list(anim_info(self.store.read("anim", name), self.store.profile.darkages))
                    except (FormatError, OSError, ValueError, IndexError, Exception):  # noqa: BLE001
                        got = ["", 0, 0.0]
                    idx.data[name] = got
                    changed = True
                skel, frames, fps = got
                if skel and skel.lower() == self.skeleton_name.lower() and frames:
                    short = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    label = short if short not in out else name
                    out[label] = AnimationInfo(label, int(frames), int(frames) / (fps or 30.0), fps or 30.0, name)
            if changed:
                idx.save()
            self._infos = out
            return out

    def files_used(self) -> list[str]:
        return [i.source for i in self.infos().values()]

    def list(self) -> list[AnimationInfo]:
        return list(self.infos().values())

    def names(self) -> list[str]:
        return list(self.infos())

    def __len__(self) -> int:
        return len(self.infos())

    def __contains__(self, name: str) -> bool:
        return name in self.infos()

    def info(self, name: str) -> AnimationInfo:
        try:
            return self.infos()[name]
        except KeyError:
            raise FormatError(f"no animation named {name}") from None

    def clamp(self, name: str, frame: int) -> int:
        return max(0, min(int(frame), self.info(name).frames - 1))

    def _anim(self, name: str) -> animfmt.Md6Anim:
        info = self.info(name)
        with self._lock:
            a = self._anims.get(info.source)
            if a is None:
                a = read_anim(self.store, info.source)
                if len(self._anims) > 8:
                    self._anims.clear()
                self._anims[info.source] = a
            return a

    def local_pose(self, name: str, frame: float) -> np.ndarray:
        sk = self.skeleton
        rot, scl, trn = self._anim(name).sample(frame, len(sk.names), sk.rotations, sk.scales, sk.translations)
        return self._to_export(trs_matrices(rot, scl, trn))

    def world_matrices(self, name: str, frame: float) -> np.ndarray:
        return compose_hierarchy(self.local_pose(name, frame), self.parents)

    def skin_matrices(self, name: str, frame: float) -> np.ndarray:
        return self.world_matrices(name, frame) @ np.linalg.inv(self.bind_world)
