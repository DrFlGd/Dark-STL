"""Posing GTA V characters with the game's clip dictionaries (``.ycd``).

A character's skeleton is the one in its ``.yft`` (or its drawable's own). An animation moves bones by
tag: track 0 sets a bone's position, track 1 its rotation; bones it doesn't move keep their bind pose.
The mover (tracks 5 and 6) isn't applied, and the root's drift across the ground is taken out, so a walk
stays in place. The same skeleton poses every component (head, top, legs, shoes) of the character.
"""

from __future__ import annotations

import numpy as np

from ...errors import FormatError
from ...model import Rig
from ...posing import compose_hierarchy
from ..renderware.animation import AnimInfo
from .formats import clip as clipfmt
from .formats.drawable import Skeleton, quat_matrix

FPS = 30.0
MIN_SHARE = 0.25  # an animation is offered when it moves at least this share of the skeleton's bones


class RageSkeleton:
    def __init__(self, skel: Skeleton):
        self.skel = skel
        self.names = [b.name for b in skel.bones]
        self.parents = np.array([b.parent if 0 <= b.parent < len(skel.bones) else -1 for b in skel.bones],
                                np.int64)
        self.bind_local = np.stack([b.local() for b in skel.bones]) if skel.bones else np.zeros((0, 4, 4))
        self.bind_world = compose_hierarchy(self.bind_local, self.parents) if skel.bones else np.zeros((0, 4, 4))
        self.tags = {b.tag: i for i, b in enumerate(skel.bones)}

    def __len__(self) -> int:
        return len(self.names)


class RageAnimations:
    """The clips that can pose one character, from one or more dictionaries, read when first used."""

    def __init__(self, skeleton: RageSkeleton, dictionaries: list[tuple[str, callable]]):
        self.skeleton = skeleton
        self._dicts = dictionaries  # (source name, () -> list[Clip])
        self._clips: dict[str, tuple[clipfmt.Clip, str]] | None = None
        self._infos: list[AnimInfo] | None = None
        self.note: str | None = None
        self.files = [n for n, _ in dictionaries]
        self._posed: dict[str, np.ndarray] = {}

    @property
    def names(self) -> list[str]:
        return [i.name for i in self.list()]

    def __contains__(self, name: str) -> bool:
        self._load()
        return name in self._clips

    def _load(self) -> None:
        if self._clips is not None:
            return
        clips: dict[str, tuple[clipfmt.Clip, str]] = {}
        infos, problems = [], []
        for source, read in self._dicts:
            try:
                found = read()
            except (FormatError, OSError) as exc:
                problems.append(f"{source}: {exc}")
                continue
            for c in sorted(found, key=lambda c: c.name.lower()):
                moved = len({t for t, k in c.animation.tracks if k in (0, 1) and t in self.skeleton.tags})
                if not moved or moved < MIN_SHARE * max(len(self.skeleton), 1) and moved < 20:
                    continue
                name = c.name if c.name not in clips else f"{c.name} ({source})"
                clips[name] = (c, source)
                frames = max(1, c.animation.frames)
                dur = c.animation.duration or (frames - 1) / FPS
                fps = (frames - 1) / dur if dur > 0 and frames > 1 else FPS
                infos.append(AnimInfo(name, frames, dur, fps, source, moved))
        if problems and not infos:
            self.note = "The animations couldn't be read: " + "; ".join(problems)
        elif not infos:
            self.note = "None of the animations found move this skeleton's bones."
        self._clips, self._infos = clips, infos

    def list(self) -> list[AnimInfo]:
        self._load()
        return list(self._infos)

    def info(self, name: str) -> AnimInfo:
        for i in self.list():
            if i.name == name:
                return i
        raise FormatError(f"No animation named {name}")

    def clamp(self, name: str, frame: int) -> int:
        return int(min(max(int(frame), 0), self.info(name).frames - 1))

    def pose_all(self, name: str) -> tuple[list[str], np.ndarray]:
        """Every frame's local bone matrices, (frames, bones, 4, 4)."""
        self._load()
        if name not in self._clips:
            raise FormatError(f"No animation named {name}")
        if name in self._posed:
            return list(self.skeleton.names), self._posed[name]
        c, _src = self._clips[name]
        anim = c.animation
        sk = self.skeleton
        n = max(1, anim.frames)
        local = np.repeat(sk.bind_local[None], n, 0)
        root = None
        for tag, track in anim.tracks:
            if track == 0 and tag in sk.tags:
                i = sk.tags[tag]
                if root is None or self._depth(i) < self._depth(root):
                    root = i
        first_root = None
        for f in range(n):
            values = anim.sample(f)
            for (tag, track), v in values.items():
                i = sk.tags.get(tag)
                if i is None:
                    continue
                scale = np.linalg.norm(sk.bind_local[i, :3, :3], axis=0)
                if track == 1:
                    q = np.asarray(v, float)
                    nq = np.linalg.norm(q)
                    if nq > 0 and np.all(np.isfinite(q)):
                        local[f, i, :3, :3] = quat_matrix(q / nq) * scale[None, :]
                elif track == 0 and np.all(np.isfinite(v)):
                    t = np.asarray(v, float)
                    if i == root:  # the walk across the ground taken out (height kept)
                        if first_root is None:
                            first_root = t.copy()
                        t = t.copy()
                        t[:2] += sk.bind_local[i, :2, 3] - first_root[:2]
                    local[f, i, :3, 3] = t
        if len(self._posed) >= 4:
            self._posed.pop(next(iter(self._posed)))
        self._posed[name] = local
        return list(sk.names), local

    def _depth(self, i: int) -> int:
        d, p = 0, int(self.skeleton.parents[i])
        while p >= 0 and d < 512:
            d += 1
            p = int(self.skeleton.parents[p])
        return d

    def local_pose(self, name: str, frame: int) -> np.ndarray:
        _names, local = self.pose_all(name)
        return local[self.clamp(name, frame)]

    def skin_matrices(self, name: str, frame: int) -> np.ndarray:
        """Per bone, the matrix that carries a bind-pose vertex into the pose."""
        world = compose_hierarchy(self.local_pose(name, frame), self.skeleton.parents)
        return np.einsum("bij,bjk->bik", world, np.linalg.inv(self.skeleton.bind_world))


def rig(skeleton: RageSkeleton, anims: RageAnimations) -> Rig:
    def pose_locals(name: str, frame: int):
        return list(skeleton.names), anims.local_pose(name, frame)

    return Rig(list(skeleton.names), skeleton.parents.copy(), skeleton.bind_local.copy(),
               skeleton.bind_world.copy(), pose_locals, anims.pose_all)
