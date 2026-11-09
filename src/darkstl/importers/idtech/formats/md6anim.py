"""id Tech 7 animations (``anim`` resources, named ``.md6anim``).

A header (the skeleton's name, bounds), a data block (frame count and rate, frame sets, where the constant values
are), joint lists (run-length coded), constant rotations / scales / translations for joints that don't move, and
frame sets: each animated joint's first value, a bit per frame saying where it has a key, and the keys. Rotations
are 48-bit "smallest three" quaternions.

DOOM: The Dark Ages (:func:`read_darkages_anim`) adds attachments, a longer data block (how the frame sets are
streamed), joint lists that may hold 16-bit joints, and keeps the frame sets of most animations in the StreamDB;
its key bits run from the last byte of a joint's row, and its quaternions are scaled by 1/32768.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
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


def decode_quats(raw: bytes, offset: int, count: int, step: int = 0x7FFF) -> np.ndarray:
    """``count`` 48-bit quaternions; DOOM Eternal divides by ``0x7FFF``, The Dark Ages by ``0x8000``."""
    if not count:
        return np.zeros((0, 4))
    if offset < 0 or offset + count * 6 > len(raw):
        raise FormatError("the animation's rotations are cut short")
    v = np.frombuffer(raw, "<u2", count * 3, offset).reshape(count, 3).astype(np.int64)
    s2 = math.sqrt(2.0)
    abc = (v & 0x7FFF) * (s2 / step) - math.sqrt(0.5)
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
        _read_frame_set(data, start + set_offsets[s] * 16, (a_r, a_s, a_t), keys, False)
    _add_tracks(anim, keys)
    return anim


def _vectors(raw: bytes, offset: int, count: int) -> np.ndarray:
    if offset < 0 or offset + count * 12 > len(raw):
        raise FormatError("the animation's values are cut short")
    return np.frombuffer(raw, "<f4", count * 3, offset).reshape(count, 3).astype(np.float64)


def _read_frame_set(data: bytes, fs: int, animated: tuple[list[int], list[int], list[int]],
                    keys: dict[tuple[int, int], tuple[list[int], list[np.ndarray]]], darkages: bool) -> None:
    """One frame set's keys, added to ``keys`` ((kind, joint) -> frames, values)."""
    if fs < 0 or fs + 38 > len(data):
        raise FormatError("the animation's frame set is cut short")
    h = struct.unpack_from("<19H", data, fs)
    first_off, range_off, bits_off = h[0:3], h[4:7], h[8:11]
    frame_start, frame_range = h[17], h[18]
    per_joint = (frame_range + 7) >> 3
    step = 0x8000 if darkages else 0x7FFF
    for kind, joints in enumerate(animated):
        if not joints:
            continue
        rot = kind == 0
        n = len(joints)
        first = decode_quats(data, fs + first_off[kind], n, step) if rot else _vectors(data, fs + first_off[kind], n)
        at = fs + bits_off[kind]
        if at + per_joint * n > len(data):
            raise FormatError("the animation's key bits are cut short")
        rows = np.frombuffer(data, np.uint8, per_joint * n, at).reshape(n, per_joint)
        if darkages:  # the first frames are in each row's last byte
            rows = rows[:, ::-1]
        bits = np.unpackbits(np.ascontiguousarray(rows), axis=1)[:, :frame_range].astype(bool)
        total = int(bits.sum())
        rng = (decode_quats(data, fs + range_off[kind], total, step) if rot
               else _vectors(data, fs + range_off[kind], total))
        vi = 0
        for i, j in enumerate(joints):
            frames, values = keys.setdefault((kind, j), ([], []))
            frames.append(frame_start)
            values.append(first[i])
            for f in np.nonzero(bits[i])[0]:
                frames.append(frame_start + int(f))
                values.append(rng[vi])
                vi += 1


def _add_tracks(anim: Md6Anim, keys: dict[tuple[int, int], tuple[list[int], list[np.ndarray]]],
                skip: int | None = _NONE) -> None:
    for (kind, j), (frames, values) in keys.items():
        if j == skip:
            continue
        fr = np.asarray(frames)
        order = np.argsort(fr, kind="stable")
        fr, vals = fr[order], np.asarray(values)[order]
        keep = np.append(fr[1:] != fr[:-1], True)  # a later key on the same frame wins
        (anim.rotations, anim.scales, anim.translations)[kind].append(Track(j, fr[keep], vals[keep]))


def read_anim_skeleton(data: bytes) -> str:
    """Only the name of the skeleton an animation is for."""
    return Reader(data).string()


# -- DOOM: The Dark Ages ---------------------------------------------------------------------------------------------

UNSTREAMED, UNSTREAMED_FIRST_FRAMESET, STREAMED, LODS_STREAMED = 0, 1, 2, 3
_DA_DATA = struct.Struct("<i4h2i11hBBf24f")  # the data block, up to the joint map


@dataclass
class DarkAgesHeader:
    skeleton: str
    start: int  # where the data block starts
    size: int  # the header's size (where the stream info is, from start)
    frames: int
    fps: float
    sets: int
    set_offsets_at: int
    const: tuple[int, int, int]
    method: int
    map_at: int


def decode_rle16(raw: bytes, pos: int) -> list[int]:
    """A Dark Ages joint list: a 16-bit size (top bit: the joints are 16-bit), then runs of (count, first joint)."""
    if pos < 0 or pos + 2 > len(raw):
        raise FormatError("the animation's joint list is cut short")
    (size,) = struct.unpack_from("<h", raw, pos)
    wide = size < 0
    size &= 0x7FFF
    pos += 2
    out: list[int] = []
    while len(out) < size:
        if pos >= len(raw):
            raise FormatError("the animation's joint list is cut short")
        count = raw[pos]
        if count & 0x80 or count == 0:
            raise FormatError("the animation's joint list isn't one Dark STL reads")
        if wide:
            (value,) = struct.unpack_from("<H", raw, pos + 1)
            pos += 3
        else:
            value = raw[pos + 1]
            pos += 2
        out += range(value, value + count)
    return out[:size]


def read_darkages_header(data: bytes) -> DarkAgesHeader:
    r = Reader(data)
    skeleton = r.string()
    if r.i32() != 0:
        raise FormatError("animations with attachments aren't read yet")
    r.vec(12)  # bounds
    size = r.i32()
    start = r.pos
    if start + _DA_DATA.size + 17 > len(data):
        raise FormatError("not a DOOM: The Dark Ages animation (too short)")
    d = _DA_DATA.unpack_from(data, start)
    (_total, _size, _flags, n_frames, rate, _src, _base, n_sets, _tbl0, _tbl1, set_off_tbl, const_r, const_s,
     const_t, _const_u, _next, _weights, _n_streamed, method, _pad) = d[:20]
    if method not in (UNSTREAMED, STREAMED, LODS_STREAMED):
        raise FormatError("this animation's frame sets are stored a way Dark STL doesn't read yet")
    return DarkAgesHeader(skeleton, start, size, max(1, n_frames), float(rate) or 30.0, n_sets,
                          start + (set_off_tbl & 0xFFFF), (const_r & 0xFFFF, const_s & 0xFFFF, const_t & 0xFFFF),
                          method, start + _DA_DATA.size)


@dataclass
class StreamLayout:
    size: int  # decompressed
    stored: int
    sets: int


def darkages_stream_info(data: bytes, head: DarkAgesHeader) -> tuple[list[StreamLayout], list[int], list[int]]:
    """(layouts, each streamed frame set's offset in its piece, each one's layout)."""
    at = head.start + head.size
    if at + 2 > len(data):
        return [], [], []
    (n_layouts,) = struct.unpack_from("<h", data, at)
    if n_layouts <= 0:
        return [], [], []
    (n_offsets,) = struct.unpack_from("<h", data, at + 2)
    at += 4
    layouts = []
    for _ in range(n_layouts):
        size, stored, _offset, sets, _comp = struct.unpack_from("<3I2h", data, at)
        layouts.append(StreamLayout(size, stored, sets))
        at += 16
    offsets = list(struct.unpack_from(f"<{n_offsets}H", data, at))
    which = list(struct.unpack_from(f"<{n_offsets}h", data, at + 2 * n_offsets))
    return layouts, offsets, which


def darkages_pieces(data: bytes) -> list[tuple[int, int]]:
    """The StreamDB pieces a Dark Ages animation's frame sets are in: (layout index, decompressed size)."""
    head = read_darkages_header(data)
    if head.method == UNSTREAMED:
        return []
    layouts, _offsets, which = darkages_stream_info(data, head)
    return [(i, layouts[i].size) for i in sorted(set(which)) if 0 <= i < len(layouts)]


def read_darkages_anim(data: bytes, fetch: Callable[[int, int], bytes | None] | None = None) -> Md6Anim:
    """A DOOM: The Dark Ages animation. ``fetch(layout, size)`` gives a streamed piece of its frame sets
    (see :func:`darkages_pieces`), or None."""
    head = read_darkages_header(data)
    if data[head.map_at] != 0:
        raise FormatError("not a DOOM: The Dark Ages animation (joint map)")
    offs = struct.unpack_from("<8H", data, head.map_at + 1)
    c_r, c_s, c_t, _c_u, a_r, a_s, a_t, _a_u = [decode_rle16(data, head.map_at + o) for o in offs]
    anim = Md6Anim(head.skeleton, head.frames, head.fps)
    const_r, const_s, const_t = head.const
    for joints, vals, target in (
            (c_r, decode_quats(data, head.start + const_r, len(c_r), 0x8000), anim.rotations),
            (c_s, _vectors(data, head.start + const_s, len(c_s)), anim.scales),
            (c_t, _vectors(data, head.start + const_t, len(c_t)), anim.translations)):
        for j, v in zip(joints, vals):
            target.append(Track(j, np.array([0]), v[None, :]))
    sets: list[tuple[bytes, int]] = []
    if head.method == UNSTREAMED:
        if head.set_offsets_at + 4 * (head.sets + 1) > len(data):
            raise FormatError("the animation's frame set table is cut short")
        offsets = struct.unpack_from(f"<{head.sets + 1}i", data, head.set_offsets_at)
        sets = [(data, head.start + o * 16) for o in offsets[:head.sets]]
    else:
        layouts, offsets, which = darkages_stream_info(data, head)
        pieces: dict[int, bytes] = {}
        for off, li in zip(offsets, which):
            if not 0 <= li < len(layouts):
                raise FormatError("the animation names a stream it doesn't have")
            if li not in pieces:
                got = fetch(li, layouts[li].size) if fetch is not None else None
                if got is None:
                    raise FormatError("the animation's frames are in the game's StreamDB files, which don't have them")
                pieces[li] = got
            sets.append((pieces[li], off))
        if head.method == LODS_STREAMED and sets:  # one set of frame sets per level of detail: the finest
            lod = [buf[at + 38] if at + 39 <= len(buf) else 0 for buf, at in sets]
            best = min(lod)
            sets = [s for s, lo in zip(sets, lod) if lo == best]
    keys: dict[tuple[int, int], tuple[list[int], list[np.ndarray]]] = {}
    for buf, at in sets:
        _read_frame_set(buf, at, (a_r, a_s, a_t), keys, True)
    _add_tracks(anim, keys, None)
    return anim
