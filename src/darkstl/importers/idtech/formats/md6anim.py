"""id Tech 7 animations (``anim`` resources, named ``.md6anim``).

A header (the skeleton's name, bounds), a data block (frame count and rate, frame sets, where the constant values
are), joint lists (run-length coded), constant rotations / scales / translations for joints that don't move, and
frame sets: each animated joint's first value, a bit per frame saying where it has a key, and the keys. Rotations
are 48-bit "smallest three" quaternions.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .reader import Reader

_NONE = 0xFF  # a joint list's "no joint"


@dataclass
class Track:
    joint: int
    frames: np.ndarray  # (k,) int, increasing
    values: np.ndarray  # (k, 4) quaternions or (k, 3) vectors


@dataclass
class Md6Anim:
    skeleton: str
    frames: int
    fps: float
    rotations: list[Track] = field(default_factory=list)
    scales: list[Track] = field(default_factory=list)
    translations: list[Track] = field(default_factory=list)

    def sample(self, frame: float, joints: int, bind_rot: np.ndarray, bind_scale: np.ndarray,
               bind_trans: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Every joint's local rotation, scale and translation at ``frame``; joints without tracks keep the bind
        pose's."""
        rot = np.array(bind_rot[:joints], np.float64, copy=True)
        scl = np.array(bind_scale[:joints], np.float64, copy=True)
        trn = np.array(bind_trans[:joints], np.float64, copy=True)
        for tracks, out, slerp in ((self.rotations, rot, True), (self.scales, scl, False),
                                   (self.translations, trn, False)):
            for t in tracks:
                if 0 <= t.joint < joints:
                    out[t.joint] = _interpolate(t, frame, slerp)
        return rot, scl, trn


def _interpolate(t: Track, frame: float, slerp: bool) -> np.ndarray:
    k = int(np.searchsorted(t.frames, frame, side="right")) - 1
    if k < 0:
        return t.values[0]
    if k >= len(t.frames) - 1:
        return t.values[-1]
    f0, f1 = t.frames[k], t.frames[k + 1]
    a, b = t.values[k], t.values[k + 1]
    u = 0.0 if f1 == f0 else (frame - f0) / (f1 - f0)
    if not slerp:
        return a + (b - a) * u
    if np.dot(a, b) < 0:
        b = -b
    q = a + (b - a) * u  # normalised lerp: keys are a frame or so apart
    return q / max(np.linalg.norm(q), 1e-12)


def decode_quats(raw: bytes, offset: int, count: int) -> np.ndarray:
    v = np.frombuffer(raw, "<u2", count * 3, offset).reshape(count, 3).astype(np.int64)
    if not count:
        return np.zeros((0, 4))
    s2 = math.sqrt(2.0)
    abc = (v & 0x7FFF) * (s2 / 0x7FFF) - math.sqrt(0.5)
    d = np.sqrt(np.clip(1.0 - (abc ** 2).sum(axis=1), 0.0, 1.0))
    index = ((v[:, 1] >> 15) & 1) << 1 | ((v[:, 0] >> 15) & 1)
    a, b, c = abc[:, 0], abc[:, 1], abc[:, 2]
    out = np.empty((count, 4))
    for i, order in enumerate(((a, b, c, d), (b, c, d, a), (c, d, a, b), (d, a, b, c))):
        sel = index == i
        for k in range(4):
            out[sel, k] = order[k][sel]
    return out


def decode_rle(raw: bytes, pos: int) -> list[int]:
    size = raw[pos]
    pos += 1
    out: list[int] = []
    while len(out) < size:
        count = raw[pos]
        pos += 1
        if count & 0x80:
            out += [_NONE] * (count & 0x7F)
            continue
        value = raw[pos]
        pos += 1
        out += list(range(value, value + count))
        if count == 0:
            break
    return out[:size]


def read_md6_anim(data: bytes) -> Md6Anim:
    r = Reader(data)
    skeleton = r.string()
    r.vec(12)
    r.u32()
    start = r.pos
    if r.unpack("Q")[0] != 0:
        raise FormatError("not a DOOM Eternal animation")
    r.u32()
    (_size, _flags, n_frames, rate, n_sets, _set_tbl, set_off_tbl, const_r, const_s, const_t, _const_u, _next,
     _weights, _zero) = r.unpack("14h")
    r.vec(24)
    r.unpack("Q")
    maps_at = r.pos
    (n_maps,) = struct.unpack_from("<h", data, maps_at)
    if n_maps < 1:
        raise FormatError("the animation has no joint map")
    offs = struct.unpack_from("<8h", data, maps_at + 2 + 2 * n_maps)
    lists = [decode_rle(data, maps_at + o) for o in offs]
    c_r, c_s, c_t, _c_u, a_r, a_s, a_t, _a_u = lists
    anim = Md6Anim(skeleton, max(1, n_frames), float(rate) or 30.0)
    zero = np.array([0])
    for joints, offset, rot, target in ((c_r, const_r, True, anim.rotations), (c_s, const_s, False, anim.scales),
                                        (c_t, const_t, False, anim.translations)):
        vals = (decode_quats(data, start + offset, len(joints)) if rot
                else np.frombuffer(data, "<f4", len(joints) * 3, start + offset).reshape(-1, 3).astype(np.float64))
        for j, v in zip(joints, vals):
            if j != _NONE:
                target.append(Track(j, zero, v[None, :]))
    set_offsets = struct.unpack_from(f"<{n_sets + 1}i", data, start + set_off_tbl)
    keys: dict[tuple[int, int], tuple[list[int], list[np.ndarray]]] = {}
    for s in range(n_sets):
        fs = start + set_offsets[s] * 16
        h = struct.unpack_from("<19h", data, fs)
        first_off, range_off, bits_off = h[0:3], h[4:7], h[8:11]
        frame_start, frame_range = h[17], h[18]
        per_joint = (frame_range + 7) >> 3
        for kind, joints in enumerate((a_r, a_s, a_t)):
            if not joints:
                continue
            rot = kind == 0
            n = len(joints)
            first = (decode_quats(data, fs + first_off[kind], n) if rot
                     else np.frombuffer(data, "<f4", n * 3, fs + first_off[kind]).reshape(n, 3).astype(np.float64))
            bits = np.unpackbits(np.frombuffer(data, np.uint8, per_joint * n, fs + bits_off[kind])).reshape(n, -1)
            bits = bits[:, :frame_range].astype(bool)
            total = int(bits.sum())
            rng = (decode_quats(data, fs + range_off[kind], total) if rot
                   else np.frombuffer(data, "<f4", total * 3, fs + range_off[kind]).reshape(-1, 3).astype(np.float64))
            vi = 0
            for i, j in enumerate(joints):
                frames, values = keys.setdefault((kind, j), ([], []))
                frames.append(frame_start)
                values.append(first[i])
                for f in np.nonzero(bits[i])[0]:
                    frames.append(frame_start + int(f))
                    values.append(rng[vi])
                    vi += 1
    for (kind, j), (frames, values) in keys.items():
        if j == _NONE:
            continue
        fr = np.asarray(frames)
        order = np.argsort(fr, kind="stable")
        fr, vals = fr[order], np.asarray(values)[order]
        keep = np.append(fr[1:] != fr[:-1], True)  # a later key on the same frame wins
        (anim.rotations, anim.scales, anim.translations)[kind].append(Track(j, fr[keep], vals[keep]))
    return anim


def read_anim_skeleton(data: bytes) -> str:
    """Only the name of the skeleton an animation is for."""
    return Reader(data).string()
