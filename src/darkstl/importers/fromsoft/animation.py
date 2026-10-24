"""Havok skeletons and animations, as FromSoftware ships them in ``anibnd`` archives.

A character's ``Skeleton.HKX`` holds an ``hkaSkeleton``: bone names, parents and the reference
(bind) pose as local translation/rotation/scale transforms. Every ``aXXX_XXXXXX.hkx`` holds one
animation (``hkaSplineCompressedAnimation`` almost always, ``hkaInterleavedUncompressedAnimation``
occasionally) and a binding that says which skeleton bone each transform track drives.

This module decodes those into world matrices per bone for a given frame, which
:mod:`darkstl.importers.fromsoft.mesh` then uses to skin the FLVER vertices.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ...errors import FormatError
from .formats.havok import Hkx, read_hkx

# ---------------------------------------------------------------------------
# Transforms

# A Havok hkQsTransform as 12 floats: translation xyz(w), rotation quaternion xyzw, scale xyz(w)
IDENTITY_QS = np.array([0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1], np.float32)


def qs_to_matrices(qs: np.ndarray) -> np.ndarray:
    """(n, 12) translation / quaternion xyzw / scale rows -> (n, 4, 4) affine matrices (T·R·S)."""
    qs = np.asarray(qs, np.float64).reshape(-1, 12)
    t, q, s = qs[:, 0:3], qs[:, 4:8], qs[:, 8:11]
    norm = np.linalg.norm(q, axis=1)
    norm[norm == 0] = 1.0
    x, y, z, w = (q / norm[:, None]).T
    rot = np.empty((len(qs), 3, 3))
    rot[:, 0, 0] = 1 - 2 * (y * y + z * z)
    rot[:, 0, 1] = 2 * (x * y - z * w)
    rot[:, 0, 2] = 2 * (x * z + y * w)
    rot[:, 1, 0] = 2 * (x * y + z * w)
    rot[:, 1, 1] = 1 - 2 * (x * x + z * z)
    rot[:, 1, 2] = 2 * (y * z - x * w)
    rot[:, 2, 0] = 2 * (x * z - y * w)
    rot[:, 2, 1] = 2 * (y * z + x * w)
    rot[:, 2, 2] = 1 - 2 * (x * x + y * y)
    m = np.zeros((len(qs), 4, 4))
    m[:, :3, :3] = rot * s[:, None, :]
    m[:, :3, 3] = t
    m[:, 3, 3] = 1.0
    return m


def compose_hierarchy(local: np.ndarray, parents: np.ndarray) -> np.ndarray:
    """World matrices from local ones: each bone's local matrix on top of its parent's world matrix."""
    world = np.array(local, np.float64, copy=True)
    done = np.zeros(len(local), bool)

    def visit(i: int) -> None:
        if done[i]:
            return
        p = int(parents[i])
        if 0 <= p < len(local) and p != i:
            visit(p)
            world[i] = world[p] @ local[i]
        done[i] = True

    for i in range(len(local)):
        visit(i)
    return world


# ---------------------------------------------------------------------------
# Skeleton


@dataclass
class Skeleton:
    names: list[str]
    parents: np.ndarray  # int, -1 for roots
    reference_pose: np.ndarray  # (n, 12) local hkQsTransforms

    @classmethod
    def from_hkx(cls, hkx: Hkx) -> Skeleton | None:
        """The character skeleton in a Havok file, skipping ragdoll skeletons."""
        best = None
        for sk in hkx.find("hkaSkeleton"):
            bones = sk.get("bones") or []
            if not bones:
                continue
            is_ragdoll = str(sk.get("name", "")).lower().startswith("ragdoll") or all(
                str(b.get("name", "")).lower().startswith("ragdoll") for b in bones
            )
            if best is None or (best[0] and not is_ragdoll):
                best = (is_ragdoll, sk)
        if best is None:
            return None
        sk = best[1]
        names = [str(b.get("name", "")) for b in sk["bones"]]
        parents = np.asarray(sk.get("parentIndices") if len(sk.get("parentIndices", [])) else [-1] * len(names), np.int64)
        pose = np.asarray(sk.get("referencePose"), np.float32).reshape(-1, 12)
        if len(pose) != len(names) or len(parents) != len(names):
            raise FormatError("Havok skeleton with mismatched bone, parent and pose counts")
        return cls(names, parents, pose)

    def __len__(self) -> int:
        return len(self.names)

    def world_matrices(self, local: np.ndarray | None = None) -> np.ndarray:
        """(n, 4, 4) world matrices of a local pose (the reference pose when none is given)."""
        return compose_hierarchy(qs_to_matrices(self.reference_pose if local is None else local), self.parents)


# ---------------------------------------------------------------------------
# Spline-compressed tracks

_STATIC_X, _STATIC_Y, _STATIC_Z, _STATIC_W = 0x01, 0x02, 0x04, 0x08
_SPLINE_X, _SPLINE_Y, _SPLINE_Z, _SPLINE_W = 0x10, 0x20, 0x40, 0x80
_STATIC_ANY, _SPLINE_ANY = 0x0F, 0xF0

# Rotation quantizations: (bytes per quaternion, alignment before the control points)
_ROTATION_FORMATS = {0: (4, 4), 1: (5, 1), 2: (6, 2), 3: (3, 1), 4: (2, 2), 5: (16, 4)}
_ROTATION_NAMES = {0: "Polar32", 1: "ThreeComp40", 2: "ThreeComp48", 3: "ThreeComp24", 4: "Straight16", 5: "Uncompressed"}

_THREE_COMP40_START = -math.sqrt(2) / 2
_THREE_COMP40_STEP = math.sqrt(2) / 4094


class _Cursor:
    def __init__(self, data: bytes, start: int, little_endian: bool = True):
        self.d = data
        self.p = start
        self.e = "<" if little_endian else ">"

    def align(self, n: int) -> None:
        self.p = (self.p + n - 1) // n * n

    def unpack(self, fmt: str):
        fmt = self.e + fmt
        out = struct.unpack_from(fmt, self.d, self.p)
        self.p += struct.calcsize(fmt)
        return out

    def take(self, n: int) -> bytes:
        out = self.d[self.p:self.p + n]
        self.p += n
        return out


@dataclass
class _Spline:
    degree: int
    knots: np.ndarray  # float, len = control points + degree + 1
    count: int  # control points

    @classmethod
    def read(cls, c: _Cursor) -> _Spline:
        (count_minus_one,) = c.unpack("h")
        (degree,) = c.unpack("B")
        count = count_minus_one + 1
        knots = np.frombuffer(c.take(count + degree + 1), np.uint8).astype(np.float64)
        return cls(degree, knots, count)

    def weights(self, t: float) -> tuple[int, np.ndarray]:
        """The knot span holding ``t`` and the basis weights of control points span, span-1, ... span-degree."""
        knots, degree, n = self.knots, self.degree, self.count
        t = min(max(t, float(knots[degree])), float(knots[n]))
        if t >= knots[n]:
            span = n - 1
        else:
            lo, hi = degree, n
            span = (lo + hi) // 2
            while t < knots[span] or t >= knots[span + 1]:
                if t < knots[span]:
                    hi = span
                else:
                    lo = span
                span = (lo + hi) // 2
        w = np.zeros(degree + 1)
        w[0] = 1.0
        for i in range(1, degree + 1):
            for j in range(i - 1, -1, -1):
                denom = knots[span + i - j] - knots[span - j]
                a = (t - knots[span - j]) / denom if denom else 0.0
                tmp = w[j] * a
                w[j + 1] += w[j] - tmp
                w[j] = tmp
        return span, w

    def evaluate(self, points: np.ndarray, t: float) -> np.ndarray:
        """Points (count, k) blended at parameter ``t``."""
        span, w = self.weights(t)
        idx = span - np.arange(self.degree + 1)
        return (points[idx] * w[:, None]).sum(axis=0)


@dataclass
class _VectorTrack:
    """A translation or scale track: each axis is a constant or a spline sharing one knot vector."""
    static: np.ndarray  # (3,) values of the constant axes (and the default for absent ones)
    spline: _Spline | None = None
    axes: tuple[int, ...] = ()  # which axes are splines
    points: np.ndarray | None = None  # (count, len(axes))

    @classmethod
    def read(cls, c: _Cursor, flags: int, quant: int, default: float) -> _VectorTrack:
        spline = None
        if flags & _SPLINE_ANY:
            spline = _Spline.read(c)
            c.align(4)
        static = np.full(3, default, np.float64)
        axes, bounds = [], []
        for axis, (sflag, stflag) in enumerate(((_SPLINE_X, _STATIC_X), (_SPLINE_Y, _STATIC_Y), (_SPLINE_Z, _STATIC_Z))):
            if flags & sflag:
                bounds.append(c.unpack("2f"))
                axes.append(axis)
            elif flags & stflag:
                (static[axis],) = c.unpack("f")
        points = None
        if spline is not None:
            n = spline.count * len(axes)
            if quant == 0:
                raw = np.frombuffer(c.take(n), np.uint8).astype(np.float64) / 255.0
            else:
                raw = np.frombuffer(c.take(2 * n), np.dtype(c.e + "u2")).astype(np.float64) / 65535.0
            raw = raw.reshape(spline.count, len(axes))
            b = np.array(bounds, np.float64)  # (axes, 2)
            points = b[:, 0] + raw * (b[:, 1] - b[:, 0])
        return cls(static, spline, tuple(axes), points)

    def at(self, t: float) -> np.ndarray:
        out = self.static.copy()
        if self.spline is not None:
            out[list(self.axes)] = self.spline.evaluate(self.points, t)
        return out


@dataclass
class _RotationTrack:
    static: np.ndarray  # (4,) xyzw
    spline: _Spline | None = None
    points: np.ndarray | None = None  # (count, 4)

    @classmethod
    def read(cls, c: _Cursor, flags: int, quant: int) -> _RotationTrack:
        if quant not in _ROTATION_FORMATS:
            raise FormatError(f"Havok animation with unknown rotation quantization {quant}")
        size, align = _ROTATION_FORMATS[quant]
        if flags & _SPLINE_ANY:
            spline = _Spline.read(c)
            c.align(align)
            points = _decode_quaternions(c.take(size * spline.count), quant, c.e)
            return cls(points[0], spline, points)
        if flags & _STATIC_ANY:
            return cls(_decode_quaternions(c.take(size), quant, c.e)[0])
        return cls(np.array([0, 0, 0, 1.0]))

    def at(self, t: float) -> np.ndarray:
        if self.spline is None:
            return self.static
        q = self.spline.evaluate(self.points, t)
        n = np.linalg.norm(q)
        return q / n if n > 0 else np.array([0, 0, 0, 1.0])


def _decode_quaternions(raw: bytes, quant: int, endian: str) -> np.ndarray:
    """Quantized quaternions -> (n, 4) xyzw floats."""
    if quant == 5:  # Uncompressed
        return np.frombuffer(raw, np.dtype(endian + "f4")).astype(np.float64).reshape(-1, 4)
    if quant == 1:  # ThreeComp40: three 12-bit components, 2-bit index of the implicit one, its sign
        b = np.frombuffer(raw, np.uint8).reshape(-1, 5).astype(np.uint64)
        v = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16 | b[:, 3] << 24 | b[:, 4] << 32
        comps = np.stack([(v >> s) & 0xFFF for s in (0, 12, 24)], axis=1).astype(np.float64)
        comps = _THREE_COMP40_START + comps * _THREE_COMP40_STEP
        missing = ((v >> 36) & 3).astype(np.int64)
        negative = ((v >> 38) & 1).astype(bool)
        return _with_implicit(comps, missing, negative)
    if quant == 2:  # ThreeComp48: three 15-bit signed components, the index of the implicit one in the top bits
        xyz = np.frombuffer(raw, np.dtype(endian + "i2")).reshape(-1, 3).astype(np.int64)
        x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        missing = ((y >> 14) & 2) | ((x >> 15) & 1)
        negative = (z >> 15) != 0
        comps = ((xyz & 0x7FFF) - 0x3FFF).astype(np.float64) * 0.000043161
        return _with_implicit(comps, missing, negative)
    if quant == 0:  # Polar32: spherical coordinates
        v = np.frombuffer(raw, np.dtype(endian + "u4")).astype(np.uint64)
        r = ((v >> 18) & 0x3FF).astype(np.float64) / 1023.0
        r = 1.0 - r * r
        phi_theta = (v & 0x3FFFF).astype(np.float64)
        phi = np.floor(np.sqrt(phi_theta))
        theta = np.where(phi > 0, (math.pi / 4) * (phi_theta - phi * phi) / np.maximum(phi, 1), 0.0)
        phi = phi * (math.pi / 4 / 511.0)
        mag = np.sqrt(np.maximum(0.0, 1.0 - r * r))
        q = np.stack([np.sin(phi) * np.cos(theta) * mag, np.sin(phi) * np.sin(theta) * mag, np.cos(phi) * mag, r], axis=1)
        for i, bit in enumerate((0x10000000, 0x20000000, 0x40000000, 0x80000000)):
            q[:, i] *= np.where((v & bit) != 0, -1.0, 1.0)
        return q
    raise FormatError(f"Havok animation rotation format {_ROTATION_NAMES.get(quant, quant)} is not supported")


def _with_implicit(comps: np.ndarray, missing: np.ndarray, negative: np.ndarray) -> np.ndarray:
    """Insert the fourth quaternion component, |q| = 1, at index ``missing`` with the given sign."""
    n = len(comps)
    implicit = np.sqrt(np.maximum(0.0, 1.0 - (comps * comps).sum(axis=1)))
    implicit = np.where(negative, -implicit, implicit)
    q = np.empty((n, 4))
    rows = np.arange(n)
    for k in range(4):
        # component k of the result comes from comps[k] when k < missing, comps[k-1] when k > missing
        src = np.where(k < missing, k, k - 1)
        q[:, k] = np.where(k == missing, implicit, comps[rows, np.clip(src, 0, 2)])
    return q


@dataclass
class _Track:
    translation: _VectorTrack
    rotation: _RotationTrack
    scale: _VectorTrack

    def at(self, t: float) -> np.ndarray:
        out = np.empty(12)
        out[0:3] = self.translation.at(t)
        out[3] = 0.0
        out[4:8] = self.rotation.at(t)
        out[8:11] = self.scale.at(t)
        out[11] = 1.0
        return out


# ---------------------------------------------------------------------------
# Animations


class Animation:
    """One animation: ``frames`` poses for ``track_to_bone`` skeleton bones."""

    def __init__(self, name: str, frames: int, duration: float, track_to_bone: np.ndarray):
        self.name = name
        self.frames = max(int(frames), 1)
        self.duration = float(duration)
        self.track_to_bone = np.asarray(track_to_bone, np.int64)

    @property
    def frame_duration(self) -> float:
        return self.duration / max(self.frames - 1, 1)

    @property
    def fps(self) -> float:
        fd = self.frame_duration
        return 1.0 / fd if fd > 0 else 30.0

    def tracks_at(self, frame: int) -> np.ndarray:
        """(tracks, 12) local transforms at ``frame`` (clamped into range)."""
        raise NotImplementedError

    def local_pose(self, skeleton: Skeleton, frame: int) -> np.ndarray:
        """(bones, 12) local pose: the skeleton's reference pose with the animated bones replaced."""
        pose = np.array(skeleton.reference_pose, np.float64, copy=True)
        tracks = self.tracks_at(frame)
        for t, b in enumerate(self.track_to_bone[: len(tracks)]):
            if 0 <= b < len(pose):
                pose[b] = tracks[t]
        return pose


class SplineAnimation(Animation):
    def __init__(self, name: str, obj: dict, track_to_bone: np.ndarray):
        super().__init__(name, obj["numFrames"], obj["duration"], track_to_bone)
        self.n_tracks = int(obj["numberOfTransformTracks"])
        self.n_floats = int(obj.get("numberOfFloatTracks", 0))
        self.frames_per_block = max(int(obj["maxFramesPerBlock"]) - 1, 1)
        self.block_offsets = np.asarray(obj["blockOffsets"], np.int64)
        self.mask_size = int(obj["maskAndQuantizationSize"])
        self.little_endian = int(obj.get("endian", 0)) == 0
        self.data = np.asarray(obj["data"], np.uint8).tobytes()
        self._blocks: dict[int, list[_Track]] = {}

    def _block(self, index: int) -> list[_Track]:
        if index in self._blocks:
            return self._blocks[index]
        c = _Cursor(self.data, int(self.block_offsets[index]), self.little_endian)
        headers = [c.unpack("4B") for _ in range(self.n_tracks)]
        c.p = int(self.block_offsets[index]) + self.mask_size  # the float track masks follow the transform ones
        c.align(4)
        tracks = []
        for quant, pos_flags, rot_flags, scale_flags in headers:
            translation = _VectorTrack.read(c, pos_flags, quant & 0x03, 0.0)
            c.align(4)
            rotation = _RotationTrack.read(c, rot_flags, (quant >> 2) & 0x0F)
            c.align(4)
            scale = _VectorTrack.read(c, scale_flags, (quant >> 6) & 0x03, 1.0)
            c.align(4)
            tracks.append(_Track(translation, rotation, scale))
        self._blocks[index] = tracks
        return tracks

    def tracks_at(self, frame: int) -> np.ndarray:
        frame = min(max(int(frame), 0), self.frames - 1)
        block = min(frame // self.frames_per_block, len(self.block_offsets) - 1)
        local = float(frame - block * self.frames_per_block)
        tracks = self._block(block)
        return np.array([t.at(local) for t in tracks]).reshape(-1, 12)


class InterleavedAnimation(Animation):
    def __init__(self, name: str, obj: dict, track_to_bone: np.ndarray):
        n_tracks = int(obj["numberOfTransformTracks"])
        transforms = np.asarray(obj["transforms"], np.float32).reshape(-1, 12)
        frames = len(transforms) // max(n_tracks, 1)
        super().__init__(name, frames, obj["duration"], track_to_bone)
        self.transforms = transforms[: frames * n_tracks].reshape(frames, n_tracks, 12)

    def tracks_at(self, frame: int) -> np.ndarray:
        return self.transforms[min(max(int(frame), 0), self.frames - 1)].astype(np.float64)


def read_animation(hkx: Hkx, name: str = "") -> Animation | None:
    """The first animation in a Havok file, bound to skeleton bones via its hkaAnimationBinding."""
    containers = hkx.find("hkaAnimationContainer")
    animations = [a for c in containers for a in (c.get("animations") or []) if isinstance(a, dict)]
    bindings = [b for c in containers for b in (c.get("bindings") or []) if isinstance(b, dict)]
    if not animations:
        return None
    anim = animations[0]
    binding = next((b for b in bindings if b.get("animation") is anim), bindings[0] if bindings else None)
    mapping = np.asarray(binding.get("transformTrackToBoneIndices", []) if binding else [], np.int64)
    n_tracks = int(anim.get("numberOfTransformTracks", 0))
    if len(mapping) == 0:
        mapping = np.arange(n_tracks)
    cls_name = anim.get("__class__", "")
    if cls_name == "hkaSplineCompressedAnimation":
        return SplineAnimation(name, anim, mapping)
    if cls_name == "hkaInterleavedUncompressedAnimation":
        return InterleavedAnimation(name, anim, mapping)
    raise FormatError(f"Havok animation type {cls_name} is not supported")


# ---------------------------------------------------------------------------
# Everything a model can be posed with


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


class AnimationSet:
    """The animations that can pose one model: a skeleton and lazily decoded animation files.

    ``sources`` maps an animation name (``a000_003000``) to a callable returning the HKX bytes, so
    archives are only unpacked when an animation is first used. A ``loader`` can supply the
    skeleton, sources, labels and compendium on first use instead, so opening a model stays cheap.
    """

    def __init__(self, skeleton: Skeleton | None = None, sources: dict[str, Callable[[], bytes]] | None = None,
                 labels: dict[str, str] | None = None,
                 loader: Callable[[], tuple] | None = None, compendium: bytes | None = None):
        self._skeleton = skeleton
        self._sources = dict(sources or {})
        self._labels = dict(labels or {})
        self._loader = loader  # -> (skeleton, sources, labels[, compendium bytes])
        self._compendium = compendium
        self._animations: dict[str, Animation | None] = {}
        self._infos: dict[str, AnimationInfo] = {}

    def _ensure(self) -> None:
        if self._loader is not None:
            loader, self._loader = self._loader, None
            skeleton, sources, labels, *rest = loader()
            self._skeleton = skeleton
            self._sources.update(sources)
            self._labels.update(labels)
            if rest and rest[0]:
                self._compendium = rest[0]

    @property
    def skeleton(self) -> Skeleton:
        self._ensure()
        if self._skeleton is None:
            raise FormatError("No skeleton was found for this model's animations")
        return self._skeleton

    @property
    def names(self) -> list[str]:
        self._ensure()
        return sorted(self._sources)

    def __len__(self) -> int:
        self._ensure()
        return len(self._sources)

    def __contains__(self, name: str) -> bool:
        self._ensure()
        return name in self._sources

    def animation(self, name: str) -> Animation:
        self._ensure()
        if name not in self._sources:
            raise FormatError(f"No animation named {name!r}")
        if name not in self._animations:
            hkx = read_hkx(self._sources[name](), self._compendium)
            self._animations[name] = read_animation(hkx, name)
        anim = self._animations[name]
        if anim is None:
            raise FormatError(f"{name} holds no animation")
        return anim

    def info(self, name: str) -> AnimationInfo:
        if name not in self._infos:
            anim = self.animation(name)
            self._infos[name] = AnimationInfo(name, anim.frames, anim.duration, anim.fps, self._labels.get(name, ""))
        return self._infos[name]

    def list(self) -> list[AnimationInfo]:
        out = []
        for name in self.names:
            try:
                out.append(self.info(name))
            except Exception as exc:  # noqa: BLE001 - a broken or unsupported file hides only itself
                out.append(AnimationInfo(name, 0, 0.0, 0.0, f"unreadable: {exc}"))
        return out

    def local_pose(self, name: str, frame: int) -> np.ndarray:
        return self.animation(name).local_pose(self.skeleton, frame)

    def clamp(self, name: str, frame: int) -> int:
        """``frame`` kept inside the animation."""
        return min(max(int(frame), 0), self.info(name).frames - 1)

    def world_matrices(self, name: str, frame: int) -> np.ndarray:
        """(bones, 4, 4) world matrices of the skeleton at ``frame`` of animation ``name``."""
        return self.skeleton.world_matrices(self.local_pose(name, frame))

    def bone_world(self, name: str, frame: int) -> dict[str, np.ndarray]:
        """World matrix per bone name at ``frame`` of animation ``name``."""
        return dict(zip(self.skeleton.names, self.world_matrices(name, frame)))

    def reference_world(self) -> dict[str, np.ndarray]:
        return dict(zip(self.skeleton.names, self.skeleton.world_matrices()))

    def pose_bones(self, names: list[str], parents: np.ndarray, local: np.ndarray, anim: str, frame: int) -> np.ndarray:
        """World matrices (n, 4, 4) for a model's own bones at ``frame`` of ``anim``.

        The model's bones (``names``, ``parents``, bind-pose ``local`` matrices) are matched to the
        skeleton's by name: matched bones take the animation's local transform, the rest (dummies,
        mesh bones, cloth) keep their bind-pose one, so they follow their parents.
        """
        pose = qs_to_matrices(self.local_pose(anim, frame))
        index = {n: i for i, n in enumerate(self.skeleton.names)}
        local = np.array(local, np.float64, copy=True).reshape(-1, 4, 4)
        for i, name in enumerate(names):
            j = index.get(name)
            if j is not None:
                local[i] = pose[j]
        return compose_hierarchy(local, np.asarray(parents, np.int64))


__all__ = [
    "Animation", "AnimationInfo", "AnimationSet", "InterleavedAnimation", "Skeleton", "SplineAnimation",
    "compose_hierarchy", "qs_to_matrices", "read_animation",
]
