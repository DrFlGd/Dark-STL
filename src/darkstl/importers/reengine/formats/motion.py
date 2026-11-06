"""RE Engine animations: ``.motlist`` files (magic ``mlst``) holding ``.mot`` motions (magic ``mot ``).

A motion lists, per bone it moves, a "bone clip": the bone's name hash (MurmurHash3 of its UTF-16
name, as the pak names are hashed) and up to three tracks, translation, rotation and scale. Each
track has key frames (an index per key, or one key) and values packed in one of many ways: plain
floats, or each component quantised to a few bits and scaled into a range kept beside the track
("unpack data": eight floats, scales then offsets). Rotations store x, y and z of a unit
quaternion, w being positive and implied. A motlist is a header, a table of offsets to its
motions (each read from its own start, so every offset inside a motion counts from there) and
an index of motion numbers. Translations are in metres, like the mesh.

The layouts differ by the motion version, which the motlist version decides (motlist 85 holds
mot 65 in Resident Evil 2 and Devil May Cry 5, motlist 524 holds mot 492 in the ray-tracing
re-releases, motlist 663 holds mot 613 in Resident Evil 4, ...). The format is documented by
shadowcookie's REE-Lib (MIT licence; ``RszFile/MotFile.cs``, ``MotlistFile.cs``), which the
decoders here follow; PredatorCZ's RevilLib agrees with it on the packed bit layouts.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .binary import FormatError

MOT_MAGIC = 0x20746F6D  # "mot "
MOTLIST_MAGIC = 0x74736C6D  # "mlst"

# Motion versions, by the game that introduced them.
MOT_RE7, MOT_RE2, MOT_RE3, MOT_MHR_DEMO, MOT_RE8, MOT_RERT, MOT_MHR, MOT_SF6, MOT_RE4, MOT_DD2, MOT_WILDS = (
    43, 65, 78, 456, 458, 492, 495, 603, 613, 698, 932)
MOTLIST_RE7 = 60

GAMES_BY_MOT = {43: "Resident Evil 7", 65: "Resident Evil 2 / Devil May Cry 5", 78: "Resident Evil 3",
                458: "Resident Evil Village", 492: "the ray-tracing re-releases", 613: "Resident Evil 4"}

TRANSLATION, ROTATION, SCALE = 1, 2, 4


def is_motlist(data: bytes) -> bool:
    return len(data) >= 8 and struct.unpack_from("<I", data, 4)[0] == MOTLIST_MAGIC


def is_mot(data: bytes, offset: int = 0) -> bool:
    return len(data) >= offset + 8 and struct.unpack_from("<I", data, offset + 4)[0] == MOT_MAGIC


# ---------------------------------------------------------------------------
# Packed values
#
# A field is (kind, ...): ("q", shift, bits, scale, offset) is unpack[scale] * (v >> shift & mask) / mask
# + unpack[offset] for the packed integer v of each key; ("c", i) is unpack[i] for every key; ("f", n)
# is the n-th float of the key. A layout is (bytes per key, byte order of the packed integer, fields).

_LE, _BE, _FLOATS = "le", "be", "floats"


def _q(shift, bits, scale, offset):
    return ("q", shift, bits, scale, offset)


def _c(i):
    return ("c", i)


def _f(n):
    return ("f", n)


def _xyz(bits, offsets=(3, 4, 5)):
    return (_q(0, bits, 0, offsets[0]), _q(bits, bits, 1, offsets[1]), _q(2 * bits, bits, 2, offsets[2]))


# Translation and scale layouts by name
_VECTORS = {
    "full": (12, _FLOATS, (_f(0), _f(1), _f(2))),
    "5bit": (2, _LE, _xyz(5)),
    "8bit": (3, _BE, (_q(16, 8, 0, 3), _q(8, 8, 1, 4), _q(0, 8, 2, 5))),  # three bytes in order
    "10bit": (4, _LE, _xyz(10)),
    "13bit": (5, _BE, _xyz(13)),
    "16bit": (6, _BE, _xyz(16)),
    "18bit": (7, _BE, _xyz(18)),
    "21bit": (8, _LE, _xyz(21)),
    "scale": (4, _FLOATS, (_f(0), _f(0), _f(0))),
    "x": (4, _FLOATS, (_f(0), _c(1), _c(2))),
    "y": (4, _FLOATS, (_c(0), _f(0), _c(2))),
    "z": (4, _FLOATS, (_c(0), _c(1), _f(0))),
    "xy": (8, _FLOATS, (_f(0), _f(1), _c(2))),
    "yz": (8, _FLOATS, (_c(0), _f(0), _f(1))),
    "zx": (8, _FLOATS, (_f(1), _c(1), _f(0))),
    "x16": (2, _LE, (_q(0, 16, 0, 1), _c(2), _c(3))),
    "y16": (2, _LE, (_c(1), _q(0, 16, 0, 2), _c(3))),
    "z16": (2, _LE, (_c(1), _c(2), _q(0, 16, 0, 3))),
    "x24": (3, _BE, (_q(0, 24, 0, 1), _c(2), _c(3))),
    "y24": (3, _BE, (_c(1), _q(0, 24, 0, 2), _c(3))),
    "z24": (3, _BE, (_c(1), _c(2), _q(0, 24, 0, 3))),
    "xyz16": (2, _LE, (_q(0, 16, 0, 1),) * 3),
}
for _bits, _size, _order in ((8, 2, _LE), (12, 3, _BE), (16, 4, _LE), (20, 5, _BE), (24, 6, _BE), (28, 7, _BE)):
    _VECTORS[f"xy{_bits}"] = (_size, _order, (_q(0, _bits, 0, 2), _q(_bits, _bits, 1, 3), _c(4)))
    _VECTORS[f"yz{_bits}"] = (_size, _order, (_c(2), _q(0, _bits, 0, 3), _q(_bits, _bits, 1, 4)))
    if _bits <= 16:
        _VECTORS[f"xz{_bits}"] = (_size, _order, (_q(0, _bits, 0, 2), _c(3), _q(_bits, _bits, 1, 4)))
    else:  # stored z first
        _VECTORS[f"zx{_bits}"] = (_size, _order, (_q(_bits, _bits, 1, 2), _c(3), _q(0, _bits, 0, 4)))
_VECTORS["xyz"] = _VECTORS["scale"]

# Rotation layouts (x, y and z of the quaternion; w follows from them, except for "full")
_ROTATIONS = {
    "full": (16, _FLOATS, (_f(0), _f(1), _f(2), _f(3))),
    "3comp": (12, _FLOATS, (_f(0), _f(1), _f(2))),
    "5bit": (2, _LE, _xyz(5, (4, 5, 6))),
    "8bit": (3, _BE, (_q(16, 8, 0, 4), _q(8, 8, 1, 5), _q(0, 8, 2, 6))),
    "10bit": (4, _LE, _xyz(10, (4, 5, 6))),
    "13bit": (5, _BE, _xyz(13, (4, 5, 6))),
    "16bit": (6, _LE, _xyz(16, (4, 5, 6))),  # three little-endian shorts (big-endian since Wilds)
    "18bit": (7, _BE, _xyz(18, (4, 5, 6))),
    "21bit": (8, _LE, _xyz(21, (4, 5, 6))),
    "x16": (2, _LE, (_q(0, 16, 0, 1), ("z",), ("z",))),
    "y16": (2, _LE, (("z",), _q(0, 16, 0, 1), ("z",))),
    "z16": (2, _LE, (("z",), ("z",), _q(0, 16, 0, 1))),
    "x24": (3, _BE, (_q(0, 24, 0, 1), ("z",), ("z",))),
    "y24": (3, _BE, (("z",), _q(0, 24, 0, 1), ("z",))),
    "z24": (3, _BE, (("z",), ("z",), _q(0, 24, 0, 1))),
    "x": (4, _FLOATS, (_f(0), ("z",), ("z",))),
    "y": (4, _FLOATS, (("z",), _f(0), ("z",))),
    "z": (4, _FLOATS, (("z",), ("z",), _f(0))),
}

# The compression field (flags & 0xFF000) -> layout: one table up to Resident Evil 2 / DMC5, another after.
_VECTOR_CODES_OLD = {0x00000: "full", 0x21000: "x16", 0x22000: "y16", 0x23000: "z16", 0x24000: "xyz16",
                     0x31000: "x", 0x32000: "y", 0x33000: "z", 0x34000: "scale"}
_VECTOR_CODES = {
    0x00000: "full", 0x20000: "5bit", 0x21000: "x16", 0x22000: "y16", 0x23000: "z16", 0x24000: "xyz16",
    0x25000: "xy8", 0x26000: "xz8", 0x27000: "yz8", 0x30000: "8bit", 0x31000: "x24", 0x32000: "y24",
    0x33000: "z24", 0x35000: "yz12", 0x36000: "xz12", 0x37000: "xy12", 0x40000: "10bit", 0x41000: "x",
    0x42000: "y", 0x43000: "z", 0x44000: "xyz", 0x45000: "xy16", 0x46000: "xz16", 0x47000: "yz16",
    0x50000: "13bit", 0x55000: "xy20", 0x56000: "yz20", 0x57000: "zx20", 0x60000: "16bit", 0x65000: "xy24",
    0x66000: "yz24", 0x67000: "zx24", 0x70000: "18bit", 0x75000: "xy28", 0x76000: "yz28", 0x77000: "zx28",
    0x80000: "21bit", 0x85000: "xy", 0x86000: "yz", 0x87000: "zx",
}
_ROTATION_CODES_OLD = {0x00000: "full", 0x21000: "x16", 0x22000: "y16", 0x23000: "z16", 0x30000: "10bit",
                       0x31000: "x", 0x32000: "y", 0x33000: "z", 0x70000: "21bit", 0xB0000: "3comp"}
_ROTATION_CODES = {
    0x00000: "full", 0x20000: "5bit", 0x21000: "x16", 0x22000: "y16", 0x23000: "z16", 0x30000: "8bit",
    0x31000: "x24", 0x32000: "y24", 0x33000: "z24", 0x40000: "10bit", 0x41000: "x", 0x42000: "y", 0x43000: "z",
    0x50000: "13bit", 0x60000: "16bit", 0x70000: "18bit", 0x80000: "21bit", 0xC0000: "3comp",
}


def _packed_ints(raw: np.ndarray, order: str) -> np.ndarray:
    """(keys, bytes) uint8 -> one uint64 per key, the bytes read in ``order``."""
    out = np.zeros(len(raw), np.uint64)
    cols = range(raw.shape[1]) if order == _BE else range(raw.shape[1] - 1, -1, -1)
    for c in cols:
        out = (out << np.uint64(8)) | raw[:, c].astype(np.uint64)
    return out


def _unpack(raw: bytes, count: int, layout: tuple, unpack: np.ndarray) -> np.ndarray:
    size, order, fields = layout
    if len(raw) < size * count:
        raise FormatError("An animation track runs past the end of its file")
    block = np.frombuffer(raw, np.uint8, count=size * count).reshape(count, size)
    floats = block.copy().view("<f4").astype(np.float64) if order == _FLOATS else None
    ints = _packed_ints(block, order) if order != _FLOATS else None
    out = np.zeros((count, len(fields)), np.float64)
    for i, fld in enumerate(fields):
        kind = fld[0]
        if kind == "f":
            out[:, i] = floats[:, fld[1]]
        elif kind == "c":
            out[:, i] = unpack[fld[1]]
        elif kind == "q":
            _, shift, bits, scale, offset = fld
            mask = (1 << bits) - 1
            value = ((ints >> np.uint64(shift)) & np.uint64(mask)).astype(np.float64)
            out[:, i] = unpack[scale] * (value / mask) + unpack[offset]
        # "z": zero
    return out


# ---------------------------------------------------------------------------
# Tracks


@dataclass
class Track:
    frames: np.ndarray  # (k,) frame of each key, ascending
    values: np.ndarray  # (k, 3) translations/scales or (k, 4) quaternions xyzw

    def at(self, frame: float) -> np.ndarray:
        frames, values = self.frames, self.values
        if len(frames) == 1 or frame <= frames[0]:
            return values[0]
        if frame >= frames[-1]:
            return values[-1]
        i = int(np.searchsorted(frames, frame, side="right")) - 1
        a, b = values[i], values[i + 1]
        span = frames[i + 1] - frames[i]
        t = (frame - frames[i]) / span if span > 0 else 0.0
        if values.shape[1] == 4:  # nlerp along the shorter way round
            if float(np.dot(a, b)) < 0:
                b = -b
            q = a * (1 - t) + b * t
            n = np.linalg.norm(q)
            return q / n if n > 0 else a
        return a * (1 - t) + b * t


def _read_track(data: bytes, base: int, pos: int, version: int, kind: int) -> tuple[Track, int]:
    """One track header at ``base + pos`` and the keys it points to; returns the track and the next position."""
    new = version >= MOT_RE3
    if new:
        flags, count, index_off, data_off, unpack_off = struct.unpack_from("<Ii3I", data, base + pos)
        pos += 20
    else:
        flags, count, _rate, _max, index_off, data_off, unpack_off = struct.unpack_from("<IiIf3Q", data, base + pos)
        pos += 40
    if not 0 < count < 1 << 20:
        raise FormatError("An animation track has no keys")
    code = flags & 0xFF000
    old = version <= MOT_RE2
    if kind == ROTATION:
        name = (_ROTATION_CODES_OLD if old else _ROTATION_CODES).get(code)
        layout = _ROTATIONS.get(name) if name else None
        if name == "16bit" and version >= MOT_WILDS:
            layout = (6, _BE, layout[2])
    else:
        name = (_VECTOR_CODES_OLD if old else _VECTOR_CODES).get(code)
        layout = _VECTORS.get(name) if name else None
    if layout is None:
        raise FormatError(f"Unknown animation track packing 0x{code:05X}")
    unpack = np.zeros(8, np.float64)
    if unpack_off:
        unpack[:] = struct.unpack_from("<8f", data, base + unpack_off)
    # key frames
    if index_off and count > 1:
        size = flags >> 20
        dtype = {2: "<u1", 4: "<i2", 5: "<i4"}.get(size)
        if dtype is None:
            raise FormatError(f"Unknown key frame size {size} in an animation track")
        frames = np.frombuffer(data, dtype, count=count, offset=base + index_off).astype(np.float64)
    else:
        frames = np.arange(count, dtype=np.float64)
    raw = data[base + data_off:base + data_off + layout[0] * count]
    values = _unpack(raw, count, layout, unpack)
    if kind == ROTATION and values.shape[1] == 3:
        w = np.sqrt(np.clip(1.0 - (values ** 2).sum(axis=1), 0.0, None))
        values = np.concatenate([values, w[:, None]], axis=1)
    if kind == ROTATION:
        n = np.linalg.norm(values, axis=1, keepdims=True)
        values = values / np.where(n > 0, n, 1)
    order = np.argsort(frames, kind="stable")
    return Track(frames[order], values[order]), pos


@dataclass
class BoneClip:
    hash: int  # MurmurHash3 of the bone's name (UTF-16LE)
    index: int
    translation: Track | None = None
    rotation: Track | None = None
    scale: Track | None = None


# ---------------------------------------------------------------------------
# Motions


@dataclass
class MotionHeader:
    version: int
    name: str
    frame_count: float
    fps: float
    bone_count: int
    clip_count: int
    clips_offset: int
    bones_offset: int
    size: int
    offset: int = 0  # where the motion starts in its file

    @property
    def frames(self) -> int:
        """Frames 0 to the last one, both included."""
        return max(1, int(np.floor(self.frame_count + 1e-4)) + 1)

    @property
    def duration(self) -> float:
        return self.frame_count / self.fps if self.fps > 0 else 0.0


def read_header(data: bytes, offset: int = 0) -> MotionHeader:
    if not is_mot(data, offset):
        raise FormatError("Not an RE Engine motion")
    try:
        version, _magic, _zero, size = struct.unpack_from("<4I", data, offset)
        bones_offset, clips_offset = struct.unpack_from("<2Q", data, offset + 16)
        pos = offset + 72 + (8 if version >= MOT_MHR_DEMO else 0)
        _tree, name_offset = struct.unpack_from("<2Q", data, pos)
        pos += 16
        frame_count, _blend, _start, _end = struct.unpack_from("<4f", data, pos)
        pos += 16
        bone_count, clip_count, _clips, _end_clips = struct.unpack_from("<2H2B", data, pos)
        pos += 6
        if version >= MOT_RE8:
            pos += 2
        (fps,) = struct.unpack_from("<H", data, pos)
    except struct.error:
        raise FormatError("A motion is cut short") from None
    name = ""
    if name_offset:
        start = offset + name_offset
        end = start
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        name = data[start:end].decode("utf-16-le", errors="replace")
    return MotionHeader(version, name, float(frame_count), float(fps or 60), bone_count, clip_count,
                        clips_offset, bones_offset, size, offset)


def clip_hashes(data: bytes, header: MotionHeader) -> list[int]:
    """The bone hashes a motion has clips for, read from the clip headers only."""
    out = []
    pos = header.offset + header.clips_offset
    step, at = _clip_layout(header.version)
    for i in range(header.clip_count if header.clips_offset else 0):
        out.append(struct.unpack_from("<I", data, pos + i * step + at)[0])
    return out


def clip_key_counts(data: bytes, header: MotionHeader) -> list[int]:
    """Per bone clip, the most keys any of its tracks has (1 for a pose the motion holds still), read from
    the track headers only."""
    out = []
    base, version = header.offset, header.version
    step, _ = _clip_layout(version)
    size = 20 if version >= MOT_RE3 else 40
    for i in range(header.clip_count if header.clips_offset else 0):
        pos = base + header.clips_offset + i * step
        try:
            (flags,) = struct.unpack_from("<B", data, pos + 2)
            if version >= MOT_RE3:
                (tracks,) = struct.unpack_from("<I", data, pos + 8)
            elif version >= MOT_RE2:
                (tracks,) = struct.unpack_from("<Q", data, pos + 16)
            else:
                (tracks,) = struct.unpack_from("<Q", data, pos + 8)
            most = 0
            at = tracks
            for kind in (TRANSLATION, ROTATION, SCALE):
                if flags & kind:
                    (count,) = struct.unpack_from("<i", data, base + at + 4)
                    most = max(most, count)
                    at += size
        except struct.error:
            most = 0
        out.append(most)
    return out


def _clip_layout(version: int) -> tuple[int, int]:
    """(bytes per clip header, where its bone hash is)."""
    if version >= MOT_RE3:
        return 12, 4
    if version >= MOT_RE2:
        return 24, 4
    return 16, 4


def read_clips(data: bytes, header: MotionHeader) -> list[BoneClip]:
    """Every bone clip of a motion, its tracks decoded."""
    base, version = header.offset, header.version
    step, _ = _clip_layout(version)
    clips = []
    try:
        for i in range(header.clip_count if header.clips_offset else 0):
            pos = base + header.clips_offset + i * step
            index, flags = struct.unpack_from("<HB", data, pos)
            (bone_hash,) = struct.unpack_from("<I", data, pos + 4)
            if version >= MOT_RE3:
                (tracks,) = struct.unpack_from("<I", data, pos + 8)
            elif version >= MOT_RE2:
                (tracks,) = struct.unpack_from("<Q", data, pos + 16)
            else:
                (tracks,) = struct.unpack_from("<Q", data, pos + 8)
            clip = BoneClip(bone_hash, index)
            at = tracks
            for kind in (TRANSLATION, ROTATION, SCALE):
                if flags & kind:
                    track, at = _read_track(data, base, at, version, kind)
                    if kind == TRANSLATION:
                        clip.translation = track
                    elif kind == ROTATION:
                        clip.rotation = track
                    else:
                        clip.scale = track
            clips.append(clip)
    except struct.error:
        raise FormatError("A motion is cut short") from None
    return clips


@dataclass
class MotBone:
    name: str
    hash: int
    translation: np.ndarray  # (3,)
    rotation: np.ndarray  # (4,) xyzw


def read_bones(data: bytes, header: MotionHeader) -> list[MotBone]:
    """The bind pose a motion carries (often only the first motion of a motlist has one)."""
    if not header.bones_offset or (header.size and header.bones_offset >= header.size and header.offset):
        return []
    base = header.offset
    try:
        first, count = struct.unpack_from("<2Q", data, base + header.bones_offset)
        if not 0 < count < 4096:
            return []
        out = []
        for i in range(count):
            pos = base + first + 80 * i
            name_off, _parent, _child, _sibling = struct.unpack_from("<4Q", data, pos)
            t = struct.unpack_from("<3f", data, pos + 32)
            q = struct.unpack_from("<4f", data, pos + 48)
            _index, bone_hash = struct.unpack_from("<iI", data, pos + 64)
            start = base + name_off
            end = start
            while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            out.append(MotBone(data[start:end].decode("utf-16-le", errors="replace"), bone_hash,
                               np.array(t, np.float64), np.array(q, np.float64)))
        return out
    except struct.error:
        return []


# ---------------------------------------------------------------------------
# Motion lists


@dataclass
class Motlist:
    version: int
    name: str
    motions: list[MotionHeader] = field(default_factory=list)


def read_motlist(data: bytes) -> Motlist:
    """A motlist's motions (headers only; :func:`read_clips` decodes one)."""
    if not is_motlist(data):
        raise FormatError("Not an RE Engine motion list")
    try:
        version, _magic, _ukn, _zero = struct.unpack_from("<4I", data, 0)
        pointers, _indices, name_offset = struct.unpack_from("<3Q", data, 16)
        pos = 40
        if version > MOTLIST_RE7:
            pos += 8
        if version >= 1036:  # Onimusha: a motpack path
            pos += 8
        (count,) = struct.unpack_from("<i", data, pos)
        if not 0 <= count < 1 << 16:
            raise FormatError("A motion list with an impossible number of motions")
        offsets = struct.unpack_from(f"<{count}Q", data, pointers)
    except struct.error:
        raise FormatError("A motion list is cut short") from None
    name = ""
    if name_offset:
        end = name_offset
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        name = data[name_offset:end].decode("utf-16-le", errors="replace")
    out = Motlist(version, name)
    seen: set[int] = set()
    for off in offsets:
        if not off or off in seen or off + 8 > len(data):
            continue
        seen.add(off)
        if not is_mot(data, off):  # a link to another file, or a motion tree
            continue
        try:
            out.motions.append(read_header(data, off))
        except FormatError:
            continue
    return out


__all__ = ["BoneClip", "MotBone", "MotionHeader", "Motlist", "Track", "clip_hashes", "clip_key_counts", "is_mot",
           "is_motlist", "read_bones", "read_clips", "read_header", "read_motlist"]
