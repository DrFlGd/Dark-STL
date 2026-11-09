"""Posing GTA characters with the games' animation packages (``.ifp``).

A skinned ped's bones are the frames its HAnim hierarchy lists, in that order (the order its
skin's bone indices count in). Where each bone was when the mesh was bound comes from the skin's
inverse bind matrices; an animation gives some bones a rotation (and the root a translation) at
each key frame, and the rest keep their bind pose. San Andreas' animations name bones by HAnim
id, III's and Vice City's by frame name. The root's movement across the ground is taken out, so a
walk stays in place.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...errors import FormatError
from ...posing import compose_hierarchy
from .formats import ifp as ifpfmt
from .formats.dff import Clump

FPS = 30.0
MIN_SHARE = 0.25  # an animation is offered when it moves at least this share of the skeleton's bones


@dataclass
class AnimInfo:
    name: str
    frames: int
    duration: float
    fps: float
    source: str = ""
    moves: int | None = None

    def as_dict(self) -> dict:
        out = {"name": self.name, "frames": self.frames, "duration": round(self.duration, 4),
               "fps": round(self.fps, 3), "source": self.source}
        if self.moves is not None:
            out["moves"] = self.moves
        return out


class Skeleton:
    """A skinned clump's bones: names, ids, parents and bind pose (model space, column vectors)."""

    def __init__(self, clump: Clump, skin_frame: int, inverse_bind: np.ndarray | None):
        world = clump.frame_world()
        frames = clump.bone_frames() or []
        self.frames = frames
        self.names = [clump.frames[f].name if f >= 0 else f"bone {i}" for i, f in enumerate(frames)]
        h = clump.hierarchy() or []
        self.ids = [b[0] for b in h]
        index_of_frame = {f: i for i, f in enumerate(frames) if f >= 0}
        parents = []
        for f in frames:
            p = clump.frames[f].parent if f >= 0 else -1
            while p >= 0 and p not in index_of_frame:
                p = clump.frames[p].parent
            parents.append(index_of_frame.get(p, -1))
        self.parents = np.array(parents, np.int64)
        bind_world = np.stack([world[f] if f >= 0 else np.eye(4) for f in frames]) if frames else np.zeros((0, 4, 4))
        if inverse_bind is not None and len(inverse_bind) == len(frames):
            placed = world[skin_frame] if 0 <= skin_frame < len(world) else np.eye(4)
            try:
                bind_world = np.einsum("ij,bjk->bik", placed, np.linalg.inv(inverse_bind))
            except np.linalg.LinAlgError:
                pass
        self.bind_world = bind_world
        local = np.array(bind_world, copy=True)
        for i, p in enumerate(self.parents):
            if p >= 0:
                local[i] = np.linalg.solve(bind_world[p], bind_world[i])
        self.bind_local = local

    def __len__(self) -> int:
        return len(self.names)


class RWAnimations:
    """The animations that can pose one ped, from one or more packages, decoded when first used."""

    def __init__(self, skeleton: Skeleton, packages: list[tuple[str, callable]], match: str = "id"):
        self.skeleton = skeleton
        self.match = match
        self._packages = packages  # (source name, () -> bytes)
        self._anims: dict[str, tuple[ifpfmt.Animation, str]] | None = None
        self._infos: list[AnimInfo] | None = None
        self.note: str | None = None
        self.files: list[str] = [name for name, _ in packages]
        self._posed: dict[str, np.ndarray] = {}  # the last few animations sampled

    def _load(self) -> None:
        if self._anims is not None:
            return
        anims: dict[str, tuple[ifpfmt.Animation, str]] = {}
        infos = []
        problems = []
        for source, read in self._packages:
            try:
                pkg = ifpfmt.read_ifp(read())
            except (FormatError, OSError) as exc:
                problems.append(f"{source}: {exc}")
                continue
            for a in pkg.animations:
                moved = len(self._bones_of(a))
                if not moved or moved < MIN_SHARE * max(len(self.skeleton), 1):
                    continue
                name = a.name if a.name not in anims else f"{a.name} ({source})"
                anims[name] = (a, source)
                frames = max(1, int(round(a.duration * FPS)) + 1)
                infos.append(AnimInfo(name, frames, a.duration, FPS, source, moved))
        if problems and not infos:
            self.note = "The animations couldn't be read: " + "; ".join(problems)
        self._anims, self._infos = anims, infos

    def _bones_of(self, anim: ifpfmt.Animation) -> list[tuple[int, ifpfmt.BoneTrack]]:
        out = []
        by_name = {n.lower(): i for i, n in enumerate(self.skeleton.names)}
        by_id = {b: i for i, b in enumerate(self.skeleton.ids)}
        for t in anim.tracks:
            i = None
            if self.match == "id" and t.bone_id is not None:
                i = by_id.get(t.bone_id)
            if i is None:
                i = by_name.get(t.name.lower())
            if i is None and t.bone_id is not None:
                i = by_id.get(t.bone_id)
            if i is not None:
                out.append((i, t))
        return out

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
        if name not in self._anims:
            raise FormatError(f"No animation named {name}")
        if name in self._posed:
            return list(self.skeleton.names), self._posed[name]
        anim, _src = self._anims[name]
        info = self.info(name)
        t = np.arange(info.frames) / FPS
        local = np.repeat(self.skeleton.bind_local[None], len(t), 0)
        bones = self._bones_of(anim)
        top = min((i for i, tr in bones if tr.translations is not None),
                  key=lambda i: self._depth(i), default=None)
        for i, track in bones:
            rot, tr = ifpfmt.sample(track, t)
            local[:, i, :3, :3] = ifpfmt.quat_matrix(rot)
            if tr is not None:
                if i == top:  # the root's walk across the ground taken out (height and sway kept)
                    tr = tr.copy()
                    tr[:, :2] -= tr[:1, :2]
                    tr[:, :2] += self.skeleton.bind_local[i, :2, 3]
                local[:, i, :3, 3] = tr
        if len(self._posed) >= 4:
            self._posed.pop(next(iter(self._posed)))
        self._posed[name] = local
        return list(self.skeleton.names), local

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
