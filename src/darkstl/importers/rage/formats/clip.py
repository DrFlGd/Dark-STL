"""Clip dictionaries (``.ycd``): GTA V's animations.

A dictionary holds clips (named, each playing an animation or part of one) and animations. An animation
lists the bone tracks it moves (bone tag and track: 0 position, 1 rotation, 2 scale, 5 and 6 the mover)
and keeps their values in blocks of frames, each value made of channels of nine kinds: static values,
raw floats, quantised floats (directly, through a table, or as packed deltas), and quaternions whose
fourth component is worked out from the other three. Layout from CodeWalker's Clip.cs (MIT).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .rsc import Reader

TRACK_POSITION, TRACK_ROTATION, TRACK_SCALE = 0, 1, 2

(STATIC_QUAT, STATIC_VEC3, STATIC_FLOAT, RAW_FLOAT, QUANT_FLOAT, INDIRECT_QUANT_FLOAT, LINEAR_FLOAT,
 CACHED_QUAT1, CACHED_QUAT2) = range(9)


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.padded = np.frombuffer(bytes(data) + b"\0" * 16, np.uint8)
        self.n = len(data) * 8

    def get(self, start: int, length: int) -> int:
        if length <= 0 or start < 0:
            return 0
        b = start >> 3
        chunk = int.from_bytes(self.padded[b:b + 9].tobytes(), "little")
        return (chunk >> (start & 7)) & ((1 << length) - 1)

    def many(self, starts: np.ndarray, length: int) -> np.ndarray:
        if length <= 0:
            return np.zeros(len(starts), np.uint64)
        starts = np.asarray(starts, np.int64)
        b = starts >> 3
        acc = np.zeros(len(starts), np.uint64)
        for k in range(8):
            acc |= self.padded[np.clip(b + k, 0, len(self.padded) - 1)].astype(np.uint64) << np.uint64(8 * k)
        acc >>= (starts & 7).astype(np.uint64)
        return acc & np.uint64((1 << length) - 1)


@dataclass
class _Channel:
    kind: int
    sequence: int = 0
    index: int = 0
    frame_bits: int = 0
    frame_offset: int = 0
    values: np.ndarray | None = None  # (frames,) float, or None for a static vector/quaternion
    static: tuple | None = None
    quant: float = 0.0
    offset: float = 0.0
    table: np.ndarray | None = None  # indirect: the values frames index
    quat_index: int = 0


@dataclass
class Track:
    bone: int  # bone tag
    track: int
    channels: list[_Channel | None] = field(default_factory=list)
    cached: bool = False

    def vector(self, f: int) -> np.ndarray:
        if self.cached:
            return self.quaternion(f)
        out = np.zeros(4)
        c = 0
        for ch in self.channels:
            if c >= 4 or ch is None:
                c += 1
                continue
            if ch.kind == STATIC_VEC3:
                for k in range(min(3, 4 - c)):
                    out[c + k] = ch.static[k]
                c += 3
            elif ch.kind == STATIC_QUAT:
                for k in range(min(4, 4 - c)):
                    out[c + k] = ch.static[k]
                c += 4
            else:
                out[c] = _value(ch, f)
                c += 1
        return out

    def quaternion(self, f: int) -> np.ndarray:
        if not self.cached:
            q = self.vector(f)
            return q
        cached = next((ch for ch in self.channels if ch is not None and ch.kind in (CACHED_QUAT1, CACHED_QUAT2)),
                      None)
        xyz = [ch for i, ch in enumerate(self.channels[:3])]
        vals = [(_value(ch, f) if ch is not None else 0.0) for ch in xyz]
        while len(vals) < 3:
            vals.append(0.0)
        w = math.sqrt(max(1.0 - sum(v * v for v in vals), 0.0))
        qi = cached.quat_index if cached is not None else 3
        comps = list(vals)
        comps.insert(qi, w)
        q = np.array(comps[:4], float)
        n = np.linalg.norm(q)
        return q / n if n > 0 else np.array([0.0, 0.0, 0.0, 1.0])


def _value(ch: _Channel, f: int) -> float:
    if ch.kind == STATIC_FLOAT:
        return ch.static[0]
    if ch.values is None or not len(ch.values):
        return ch.offset
    return float(ch.values[f % len(ch.values)])


@dataclass
class _Block:
    frames: int
    tracks: list[Track | None]


@dataclass
class Animation:
    hash: int
    frames: int
    frame_limit: int
    duration: float
    tracks: list[tuple[int, int]]  # (bone tag, track) per value, in order
    blocks: list[_Block] = field(default_factory=list, repr=False)

    def sample(self, frame: int, wanted=(TRACK_POSITION, TRACK_ROTATION)) -> dict[tuple[int, int], np.ndarray]:
        """Each (bone tag, track) value at ``frame``: a position (x, y, z) or a rotation (x, y, z, w)."""
        frame = max(0, min(int(frame), self.frames - 1)) if self.frames else 0
        limit = self.frame_limit or self.frames or 1
        b = frame // limit
        local = frame % limit
        if b >= len(self.blocks):
            b, local = len(self.blocks) - 1, self.blocks[-1].frames - 1 if self.blocks else 0
        if b < 0:
            return {}
        block = self.blocks[b]
        out = {}
        for i, key in enumerate(self.tracks):
            if key[1] not in wanted or i >= len(block.tracks) or block.tracks[i] is None:
                continue
            t = block.tracks[i]
            if key[1] == TRACK_ROTATION:
                out[key] = t.quaternion(local)
            else:
                out[key] = t.vector(local)[:3]
        return out


@dataclass
class Clip:
    name: str
    animation: Animation
    start: float = 0.0
    end: float = 0.0
    rate: float = 1.0


def _read_sequence(r: Reader, ptr: int) -> _Block:
    (_h, length, _u, frame_offset, _root_off, _u2, nframes, frame_len, _nints, _qbits, chunk,
     _roots) = r.unpack("5IHHHHHBB", ptr)
    data = r.bytes(ptr + 32, length)
    bits = _Bits(data)
    list_off = frame_offset + frame_len * nframes
    chan_off = list_off + 18
    pos = 0
    channels: list[_Channel] = []
    by_type: list[list[_Channel]] = []
    bit_cursor = 0
    for kind in range(9):
        count = struct.unpack_from("<H", data, list_off + 2 * kind)[0]
        mine = []
        for _ in range(count):
            dbits = struct.unpack_from("<H", data, chan_off)[0]
            chan_off += 2
            ch = _Channel(kind)
            pos = _read_channel(ch, data, pos, bits, nframes, chunk)
            ch.sequence, ch.index = dbits >> 2, dbits & 3
            if kind in (CACHED_QUAT1, CACHED_QUAT2):
                ch.quat_index = ch.index
                ch.index = 3 if kind == CACHED_QUAT1 else 4
            ch.frame_offset = bit_cursor
            bit_cursor += ch.frame_bits
            mine.append(ch)
            channels.append(ch)
        rem = count % 4
        if rem:
            chan_off += (4 - rem) * 2
        by_type.append(mine)
    # values stored per frame (raw, quantised, indirect)
    if nframes:
        base = (frame_offset + frame_len * np.arange(nframes, dtype=np.int64)) * 8
        for ch in channels:
            if not ch.frame_bits:
                continue
            raw = bits.many(base + ch.frame_offset, ch.frame_bits)
            if ch.kind == RAW_FLOAT:
                ch.values = raw.astype(np.uint32).view(np.float32).astype(np.float64)
            elif ch.kind == QUANT_FLOAT:
                ch.values = raw.astype(np.float64) * ch.quant + ch.offset
            elif ch.kind == INDIRECT_QUANT_FLOAT and ch.table is not None and len(ch.table):
                ch.values = ch.table[np.clip(raw.astype(np.int64), 0, len(ch.table) - 1)]
    n_tracks = max((c.sequence for c in channels), default=-1) + 1
    tracks: list[Track | None] = []
    for s in range(n_tracks):
        mine = [c for c in channels if c.sequence == s]
        if not mine:
            tracks.append(None)
            continue
        slots: list[_Channel | None] = [None] * (max(c.index for c in mine) + 1)
        for c in mine:
            if slots[c.index] is None:
                slots[c.index] = c
        t = Track(0, 0, slots, any(c is not None and c.kind == CACHED_QUAT1 for c in slots)
                  or any(c is not None and c.kind == CACHED_QUAT2 for c in slots))
        tracks.append(t)
    return _Block(nframes, tracks)


def _read_channel(ch: _Channel, data: bytes, pos: int, bits: _Bits, nframes: int, chunk: int) -> int:
    k = ch.kind
    if k == STATIC_FLOAT:
        ch.static = struct.unpack_from("<f", data, pos)
        return pos + 4
    if k == STATIC_VEC3:
        ch.static = struct.unpack_from("<3f", data, pos)
        return pos + 12
    if k == STATIC_QUAT:
        x, y, z = struct.unpack_from("<3f", data, pos)
        ch.static = (x, y, z, math.sqrt(max(1.0 - (x * x + y * y + z * z), 0.0)))
        return pos + 12
    if k == RAW_FLOAT:
        ch.frame_bits = 32
        return pos
    if k == QUANT_FLOAT:
        vbits, ch.quant, ch.offset = struct.unpack_from("<iff", data, pos)
        ch.frame_bits = max(0, vbits)
        return pos + 12
    if k == INDIRECT_QUANT_FLOAT:
        fbits, vbits, nints, ch.quant, ch.offset = struct.unpack_from("<iiiff", data, pos)
        pos += 20
        ch.frame_bits = max(0, fbits)
        n = min((nints * 32) // vbits if vbits > 0 else 0, (1 << fbits) - 1 if fbits > 0 else 0)
        start = pos * 8
        vals = bits.many(start + vbits * np.arange(n, dtype=np.int64), vbits) if n > 0 else np.zeros(0)
        ch.table = vals.astype(np.float64) * ch.quant + ch.offset
        return pos + nints * 4
    if k == LINEAR_FLOAT:
        nints, counts, ch.quant, ch.offset = struct.unpack_from("<iiff", data, pos)
        bit = (pos + 16) * 8
        c1, c2, c3 = counts & 0xFF, (counts >> 8) & 0xFF, (counts >> 16) & 0xFF
        size = chunk if chunk else 64
        nchunks = (size + nframes - 1) // size if nframes else 0
        delta_base = bit + nchunks * (c1 + c2)
        cur = bit
        offs, starts = [], []
        for _ in range(nchunks):
            offs.append(bits.get(cur, c1) if c1 else 0)
            cur += c1
        for _ in range(nchunks):
            starts.append(bits.get(cur, c2) if c2 else 0)
            cur += c2
        values = np.zeros(nframes)
        limit = bits.n
        for i in range(nchunks):
            cur = offs[i] + delta_base
            value = starts[i]
            inc = 0
            for j in range(size):
                f = i * size + j
                if f >= nframes:
                    break
                values[f] = value * ch.quant + ch.offset
                if j + 1 >= size:
                    break
                delta = bits.get(cur, c3) if c3 else 0
                cur += c3
                so = cur
                while True:  # count the zero bits before the next one
                    b = bits.get(cur, 1)
                    cur += 1
                    if b or cur >= limit:
                        break
                delta |= (cur - so - 1) << c3
                if delta:
                    sign = bits.get(cur, 1)
                    cur += 1
                    if sign:
                        delta = -delta
                inc += delta
                value += inc
        ch.values = values
        return pos + nints * 4
    return pos  # cached quaternions: nothing of their own


def read_animation(r: Reader, ptr: int) -> Animation:
    frames, limit, duration, h = r.unpack("HHfI", ptr + 0x14)
    seq_ptr, seq_count = r.unpack("QH", ptr + 0x40)
    ids_ptr, ids_count = r.unpack("QH", ptr + 0x50)
    tracks = []
    if ids_ptr and ids_count:
        raw = r.bytes(ids_ptr, 4 * ids_count)
        for i in range(ids_count):
            bone, _u, track = struct.unpack_from("<HBB", raw, 4 * i)
            tracks.append((bone, track))
    anim = Animation(h, frames, limit, duration, tracks)
    for sp in r.pointers(seq_ptr, seq_count):
        if not sp:
            continue
        block = _read_sequence(r, sp)
        for i, t in enumerate(block.tracks):
            if t is not None and i < len(tracks):
                t.bone, t.track = tracks[i]
        anim.blocks.append(block)
    return anim


def _clip_name(name: str) -> str:
    n = name.replace("\\", "/").split("/")[-1]
    if n.lower().endswith(".clip"):
        n = n[:-5]
    if n.lower().startswith("pack:"):
        n = n[5:]
    return n


def read_clips(r: Reader) -> list[Clip]:
    """The clips of a ``.ycd`` that play one animation, each with its animation read."""
    root = 0x50000000
    clips_ptr, cap = r.unpack("QH", root + 0x28)
    seen_anims: dict[int, Animation] = {}
    out = []
    names = set()
    for bucket in r.pointers(clips_ptr, cap):
        entry = bucket
        hops = 0
        while entry and hops < 10000:
            hops += 1
            _h, clip_ptr, nxt = r.unpack("IIQQ", entry)[0], r.u64(entry + 8), r.u64(entry + 16)
            entry = nxt
            if not clip_ptr:
                continue
            try:
                kind = r.u32(clip_ptr + 0x10)
                name = _clip_name(r.string(r.u64(clip_ptr + 0x18)))
                if kind != 1:
                    continue
                anim_ptr = r.u64(clip_ptr + 0x50)
                start, end, rate = r.unpack("fff", clip_ptr + 0x58)
                if not anim_ptr:
                    continue
                if anim_ptr not in seen_anims:
                    seen_anims[anim_ptr] = read_animation(r, anim_ptr)
            except FormatError:
                continue
            if name in names:
                continue
            names.add(name)
            out.append(Clip(name or f"clip {len(out) + 1}", seen_anims[anim_ptr], start, end, rate))
    out.sort(key=lambda c: c.name.lower())
    return out
