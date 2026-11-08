"""Unreal Engine 4/5 ``Skeleton`` and ``AnimSequence`` assets: a skeleton's bones and an animation's keys.

A skeletal mesh names its ``Skeleton``; the skeleton holds the reference skeleton (bone names, parents,
reference pose) that animations are made for, and every ``AnimSequence`` of that skeleton maps its tracks
to the skeleton's bones by index (``CompressedTrackToSkeletonMapTable``). A cooked animation keeps only
its compressed keys, after its properties: Unreal's own key codecs ("AnimCompress_*": fixed or variable
key spacing, or per-track compression, each track's translation, rotation and scale stored as floats,
fixed-point or interval-quantised values) are decoded here. Animations compressed with the ACL plugin are
recognised and reported, not decoded.

Layouts follow UE Viewer (MIT; ``UnAnim4.cpp``, ``UnMeshTypes.h``) and CUE4Parse (Apache 2.0), which track
the format's changes from 4.12 to 5.x: the compressed data moved behind a codec in 4.23 and 4.25.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from ..formats.props import ObjectRef, Props
from ..formats.upk import Reader
from . import asset
from .asset import Export, Package
from .mesh import object_props, read_reference_skeleton

# AnimationCompressionFormat
ACF_NONE, ACF_FLOAT96, ACF_FIXED48, ACF_INTERVAL32, ACF_FIXED32, ACF_FLOAT32, ACF_IDENTITY = range(7)
# Borderlands 2's own formats (Unreal 3; numbered here past Unreal's, as they are only ever named)
ACF_DELTA40, ACF_DELTA48, ACF_POLAR32, ACF_POLAR48 = 100, 101, 102, 103
# AnimationKeyFormat
AKF_CONSTANT, AKF_VARIABLE, AKF_PER_TRACK = range(3)
_KEY_NAMES = ("ConstantKeyLerp", "VariableKeyLerp", "PerTrackCompression")

_FORMAT_NAMES = {ACF_NONE: "None", ACF_FLOAT96: "Float96NoW", ACF_FIXED48: "Fixed48NoW",
                 ACF_INTERVAL32: "IntervalFixed32NoW", ACF_FIXED32: "Fixed32NoW", ACF_FLOAT32: "Float32NoW",
                 ACF_IDENTITY: "Identity", ACF_DELTA40: "Delta40NoW", ACF_DELTA48: "Delta48NoW",
                 ACF_POLAR32: "PolarEncoded32", ACF_POLAR48: "PolarEncoded48"}

ACL_TAGS = (0xAC10AC10, 0xAC11AC11)  # compressed_tracks / compressed_database buffer tags of the ACL library


@dataclass
class Skeleton:
    name: str
    bones: list[tuple[str, int]]
    ref_pose: np.ndarray  # (B, 10): rotation x y z w, translation, scale

    @property
    def names(self) -> list[str]:
        return [b[0] for b in self.bones]


def read_skeleton(pkg: Package, e: Export) -> Skeleton:
    r = pkg.export_data(e)
    object_props(pkg, r, e)
    bones, pose = read_reference_skeleton(r, pkg.versions)
    if not bones:
        raise FormatError(f"{e.name} has no bones")
    return Skeleton(e.name, bones, pose)


@dataclass
class Track:
    """One bone's keys: values (n, 3) or (n, 4) and, for variably spaced keys, the frame of each."""

    translation: np.ndarray | None = None
    translation_frames: np.ndarray | None = None
    rotation: np.ndarray | None = None
    rotation_frames: np.ndarray | None = None
    scale: np.ndarray | None = None
    scale_frames: np.ndarray | None = None


@dataclass
class AnimSequence:
    name: str
    frames: int  # keys from first to last frame, both included
    length: float  # seconds
    tracks: dict[int, Track]  # skeleton bone index -> keys
    skeleton: ObjectRef | None = None
    additive: bool | None = None  # None: the properties couldn't be read to say
    codec: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def fps(self) -> float:
        return (self.frames - 1) / self.length if self.length > 0 and self.frames > 1 else 30.0


@dataclass
class _Compressed:
    """What a cooked animation's own data holds, before its keys are decoded."""

    track_bones: list[int]
    stream: bytes
    codec: str = ""
    frames: int = 0
    key_format: int = AKF_CONSTANT
    formats: tuple[int, int, int] = (ACF_NONE, ACF_NONE, ACF_NONE)  # translation, rotation, scale
    offsets: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    scale_offsets: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    scale_strip: int = 0
    end: int = 0  # where the data ends in its reader


def _smart_names(r: Reader, v: asset.Versions) -> None:
    for _ in range(r.count(1 << 16)):
        r.name()  # FSmartName: display name (the UID and GUID went before 4.17/4.19)


def _ints(r: Reader, n: int) -> np.ndarray:
    return np.frombuffer(r.bytes(4 * n), "<i4").astype(np.int64) if n else np.zeros(0, np.int64)


def _bool(r: Reader) -> bool:
    b = r.u32()
    if b > 1:
        raise FormatError("a flag that isn't 0 or 1")
    return bool(b)


def _stream(r: Reader, pkg: Package) -> bytes:
    n = r.i32()
    if n < 0 or n > 1 << 28:
        raise FormatError("compressed animation of an impossible size")
    if _bool(r):  # bUseBulkDataForLoad
        data = asset.read_bulk(r)
        return data[:n]
    return r.bytes(n)


def read_compressed(r: Reader, pkg: Package, bones: int | None = None) -> _Compressed:
    """An ``AnimSequence``'s data after its properties: strip flags, then (4.12+) the compressed data."""
    v = pkg.versions
    g, _c = asset.strip_flags(r)
    if g > 3:
        raise FormatError("not an animation's data")
    if not g & 1:
        raise FormatError("this animation holds editor data (an uncooked asset); only cooked games are supported")
    if not _bool(r):  # bSerializeCompressedData
        raise FormatError("this animation was cooked without its compressed keys")
    limit = bones if bones else 1 << 16
    if v.engine < 423:
        out = _compressed_old(r, pkg, limit)
    elif v.engine < 425:
        out = _compressed_423(r, pkg, limit)
    else:
        out = _compressed_425(r, pkg, limit)
    if v.cv("FortniteMain") < 183:  # AnimSequenceRawDataOnlyFlagRemoval (5.6)
        if r.remaining() >= 4:
            r.u32()  # bUseRawDataOnly
    out.end = r.tell
    return out


SKELETON_GUID = 16  # UAnimationAsset::Serialize: the skeleton's GUID, after the object's own


def read_after_properties(r: Reader, pkg: Package, bones: int | None = None) -> _Compressed:
    """The compressed data of an animation whose reader stands after its properties and object GUID: past
    the skeleton's GUID that every animation asset writes next, else (should a game leave it out) right
    there."""
    start = r.pos
    first: Exception | None = None
    for at in (start + SKELETON_GUID, start):
        if at + 6 > len(r.data):
            continue
        r.pos = at
        try:
            return read_compressed(r, pkg, bones)
        except (FormatError, struct.error, ValueError, IndexError) as exc:
            first = first or exc
    r.pos = start
    raise first if first is not None else FormatError("not an animation's data")


def _track_map(r: Reader, limit: int) -> list[int]:
    n = r.count(4096)
    bones = [int(x) for x in _ints(r, n)]
    if any(b < 0 or b >= limit for b in bones):
        raise FormatError("a track for a bone the skeleton doesn't have")
    return bones


def _compressed_old(r: Reader, pkg: Package, limit: int) -> _Compressed:
    """4.12-4.22: the codec's arrays written one after another."""
    v = pkg.versions
    key, t, q, s = r.u8(), r.u8(), r.u8(), r.u8()
    offsets = _ints(r, r.count(1 << 20))
    scale_offsets = _ints(r, r.count(1 << 20))
    strip = r.i32()
    if v.engine >= 421:
        for _ in range(r.count(4096)):  # compressed segments (gone again in 4.23)
            r.skip(16)
    bones = _track_map(r, limit)
    if v.engine < 422:
        from .props import read_props

        read_props(r)  # FRawCurveTracks, as tagged properties
    else:
        _smart_names(r, v)
    if v.engine >= 417:
        r.i32()  # compressed raw data size
    frames = r.i32() if v.engine >= 422 else 0
    codec = ""
    peek = struct.unpack_from("<i", r.data, r.pos)[0] if r.remaining() >= 8 else -1
    if 0 <= peek < len(pkg.names):  # some games (and the ACL plugin for these engines) name the format first
        named = str(pkg.names[peek])
        acl = named.upper().startswith("ACL")
        if acl or (key < len(_KEY_NAMES) and named == _KEY_NAMES[key]):
            r.skip(8)
            codec = named if acl else ""
    n = r.i32()
    stream = r.bytes(n) if n > 0 else b""
    if v.engine >= 422:
        r.fstring()  # curve codec
        r.skip(r.count(1 << 28))
    out = _Compressed(bones, stream, codec, frames, key, (t, q, s), offsets, scale_offsets, strip)
    if key == AKF_PER_TRACK and len(scale_offsets) and v.engine < 423:
        out.stream = _per_track_order(out)
    return out


def _compressed_423(r: Reader, pkg: Package, limit: int) -> _Compressed:
    """4.23-4.24: the codec's arrays share one buffer (``FUECompressedAnimData::InitViewsFromBuffer``)."""
    r.i32()  # compressed raw data size
    bones = _track_map(r, limit)
    _smart_names(r, pkg.versions)
    key, t, q, s = r.u8(), r.u8(), r.u8(), r.u8()
    frames = r.i32()
    n_offsets, n_scale, strip, n_stream = r.i32(), r.i32(), r.i32(), r.i32()
    data = _stream(r, pkg)
    r.fstring()  # curve codec
    r.skip(r.count(1 << 28))
    return _views(bones, data, "", frames, key, (t, q, s), n_offsets, n_scale, strip, n_stream)


def _compressed_425(r: Reader, pkg: Package, limit: int) -> _Compressed:
    """4.25+: the bytes, then the codec's name, then (Unreal's own codecs) the codec's header."""
    r.i32()  # compressed raw data size
    bones = _track_map(r, limit)
    _smart_names(r, pkg.versions)
    data = _stream(r, pkg)
    codec = r.fstring()
    r.fstring()  # curve codec
    r.skip(r.count(1 << 28))  # compressed curves
    if not codec:
        return _Compressed(bones, data, codec)
    frames = r.i32()  # ICompressedAnimData: number of keys
    if _is_acl(data) or "acl" in codec.lower():
        return _Compressed(bones, data, codec or "ACL", frames)
    key, t, q, s = r.u8(), r.u8(), r.u8(), r.u8()
    n_stream, n_offsets, n_scale, strip = r.i32(), r.i32(), r.i32(), r.i32()
    return _views(bones, data, codec, frames, key, (t, q, s), n_offsets, n_scale, strip, n_stream)


def _is_acl(data: bytes) -> bool:
    return len(data) >= 12 and struct.unpack_from("<I", data, 8)[0] in ACL_TAGS


def _views(bones, data, codec, frames, key, formats, n_offsets, n_scale, strip, n_stream) -> _Compressed:
    if min(n_offsets, n_scale, n_stream) < 0 or 4 * (n_offsets + n_scale) + n_stream > len(data):
        raise FormatError("the compressed animation's arrays don't fit its data")
    offsets = np.frombuffer(data, "<i4", n_offsets).astype(np.int64)
    scale = np.frombuffer(data, "<i4", n_scale, 4 * n_offsets).astype(np.int64)
    start = 4 * (n_offsets + n_scale)
    return _Compressed(bones, data[start:start + n_stream], codec, frames, key, formats, offsets, scale, strip)


_COMPONENTS = (3, 1, 1, 2, 1, 2, 2, 3)  # per-track component masks: how many components each one keeps


def _per_track_order(c: _Compressed) -> bytes:
    """Before 4.23 per-track data was written translation, rotation, scale for each track although the offsets
    point at it laid out scale-first (an engine bug UE Viewer works around): put each piece where its offset
    points."""
    src = c.stream
    dst = bytearray(len(src))
    pos = 0
    tracks = len(c.offsets) // 2
    for t in range(tracks):
        for kind in range(3):
            off = int(c.offsets[2 * t + kind]) if kind < 2 else (
                int(c.scale_offsets[t * max(c.scale_strip, 1)]) if len(c.scale_offsets) else -1)
            if off < 0:
                continue
            start = pos
            info = struct.unpack_from("<I", src, pos)[0]
            pos += 4
            fmt, mask, keys = info >> 28, (info >> 24) & 0xF, info & 0xFFFFFF
            comps = _COMPONENTS[mask & 7]
            if fmt == ACF_INTERVAL32:
                pos += 8 * comps
            if fmt == ACF_FLOAT96:
                pos += 4 * comps * keys
            elif fmt == ACF_FIXED48:
                pos += 2 * comps * keys
            elif fmt in (ACF_INTERVAL32, ACF_FIXED32, ACF_FLOAT32):
                pos += 4 * keys
            if mask & 8:
                rel = off + (pos - start)
                pos += (-rel) % 4
                pos += (1 if c.frames < 256 else 2) * keys
            rel = off + (pos - start)
            pos += (-rel) % 4
            dst[off:off + pos - start] = src[start:pos]
    return bytes(dst)


# -- decoding keys ------------------------------------------------------------------------------------

def _w(xyz: np.ndarray) -> np.ndarray:
    """Quaternions from their x, y, z (w >= 0, as the codecs drop it)."""
    xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
    w = np.sqrt(np.clip(1.0 - (xyz * xyz).sum(axis=1), 0.0, None))
    return np.concatenate([xyz, w[:, None]], axis=1)


def _fixed32_quat(packed: np.ndarray, mins=None, ranges=None) -> np.ndarray:
    x = (packed >> 21).astype(np.float64) / 1023.0 - 1.0
    y = ((packed >> 10) & 0x7FF).astype(np.float64) / 1023.0 - 1.0
    z = (packed & 0x3FF).astype(np.float64) / 511.0 - 1.0
    xyz = np.stack([x, y, z], axis=1)
    if mins is not None:
        xyz = xyz * ranges + mins
    return _w(xyz)


def _float32_quat(packed: np.ndarray) -> np.ndarray:
    """``FQuatFloat32NoW``: x and y as 11-bit floats, z as a 10-bit one (sign, 3-bit exponent, mantissa)."""
    p = packed.astype(np.uint32)
    x, y, z = p >> 21, (p >> 10) & 0x7FF, p & 0x3FF

    def wide(v, mantissa_bits):
        sign = (v >> (mantissa_bits + 3)) & 1
        exponent = ((v >> mantissa_bits) & 7) + 123
        mantissa = (v & ((1 << mantissa_bits) - 1)) << (23 - mantissa_bits)
        return ((sign << 31) | (exponent << 23) | mantissa).astype(np.uint32).view(np.float32).astype(np.float64)

    return _w(np.stack([wide(x, 7), wide(y, 7), wide(z, 6)], axis=1))


_POLAR_SCALE = 1.5717963  # Borderlands 2's angle scale (UE Viewer: "not M_PI/2")


def _polar_quat(w_part: np.ndarray, w_max: float, d1: np.ndarray, signs: np.ndarray, d2_max: float) -> np.ndarray:
    """Borderlands 2's polar-encoded rotations (``FQuatPolarEncoded32``/``48``, after UE Viewer): w from its
    own bits, the axis from two angles packed in ``d1``, and the x, y, z signs from bits 31, 30, 29 of
    ``signs``."""
    d0 = w_part.astype(np.float64) / w_max
    w = 1.0 - d0 * d0
    d1 = d1.astype(np.int64)
    d2 = np.floor(np.sqrt(d1.astype(np.float64))).astype(np.int64)
    d2 = np.where((d2 + 1) * (d2 + 1) <= d1, d2 + 1, d2)
    span = (d2 + 1) * (d2 + 1) - d2 * d2 - 1
    a1 = np.where(span == 0, 0.0, _POLAR_SCALE * (d1 - d2 * d2) / np.maximum(span, 1))
    a2 = _POLAR_SCALE * d2 / d2_max
    x = np.cos(a1)
    y = np.sqrt(np.clip(1.0 - x * x, 0.0, None))
    z = np.cos(a2)
    s1 = np.sqrt(np.clip(1.0 - z * z, 0.0, None))
    x, y = x * s1, y * s1
    norm = np.sqrt(x * x + y * y + z * z)
    scale = np.sqrt(np.clip(1.0 - w * w, 0.0, None)) / np.where(norm > 0, norm, 1.0)
    out = np.stack([x * scale, y * scale, z * scale, w], axis=1)
    signs = signs.astype(np.int64)
    for axis, bit in enumerate((31, 30, 29)):
        out[:, axis] = np.where((signs >> bit) & 1, -out[:, axis], out[:, axis])
    return out


def _polar32_quat(packed: np.ndarray) -> np.ndarray:
    p = packed.astype(np.int64)
    return _polar_quat(p & 0x7FF, 2047.0, (p >> 11) & 0x3FFFF, p, 511.0)


def _polar48_quat(raw: np.ndarray) -> np.ndarray:
    """``raw``: (n, 6) bytes: a u16 for w, then a u32 holding the angles and signs."""
    raw = np.ascontiguousarray(np.asarray(raw, np.uint8).reshape(-1, 6))
    w = raw[:, 0:2].copy().view("<u2").reshape(-1).astype(np.int64)
    rest = raw[:, 2:6].copy().view("<u4").reshape(-1).astype(np.int64)
    return _polar_quat(w, 65535.0, rest & 0x1FFFFFFF, rest, 16383.0)


def _interval32_vector(packed: np.ndarray, mins, ranges) -> np.ndarray:
    x = (packed & 0x3FF).astype(np.float64) / 511.0 - 1.0
    y = ((packed >> 10) & 0x7FF).astype(np.float64) / 1023.0 - 1.0
    z = (packed >> 21).astype(np.float64) / 1023.0 - 1.0
    return np.stack([x, y, z], axis=1) * ranges + mins


class _Bytes:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def seek(self, pos: int) -> None:
        if not 0 <= pos <= len(self.data):
            raise FormatError("an animation track's offset lies outside its data")
        self.pos = pos

    def align(self) -> None:
        self.pos += (-self.pos) % 4

    def array(self, dtype: str, n: int) -> np.ndarray:
        size = np.dtype(dtype).itemsize * n
        if self.pos + size > len(self.data):
            raise FormatError("an animation track runs past its data")
        out = np.frombuffer(self.data, dtype, n, self.pos)
        self.pos += size
        return out

    def floats(self, n: int) -> np.ndarray:
        return self.array("<f4", n).astype(np.float64)


def _times(b: _Bytes, keys: int, frames: int) -> np.ndarray | None:
    if keys <= 1:
        return None
    t = b.array("<u1" if frames < 256 else "<u2", keys).astype(np.float64)
    b.align()
    return t


def _uniform_vector(b: _Bytes, fmt: int, keys: int) -> np.ndarray:
    if keys == 1:
        fmt = ACF_NONE  # a single key is kept as plain floats
    if fmt in (ACF_NONE, ACF_FLOAT96):
        return b.floats(3 * keys).reshape(keys, 3)
    if fmt == ACF_INTERVAL32:
        mins, ranges = b.floats(3), b.floats(3)
        return _interval32_vector(b.array("<u4", keys).astype(np.int64), mins, ranges)
    if fmt == ACF_FIXED48:
        return (b.array("<u2", 3 * keys).astype(np.float64).reshape(keys, 3) - 32767.0) * (128.0 / 32767.0)
    if fmt == ACF_IDENTITY:
        return np.zeros((keys, 3))
    if fmt == ACF_DELTA48:  # Borderlands 2: ranges and a first key, then each key's step from the one before
        mins, ranges, base = b.floats(3), b.floats(3), b.floats(3)
        steps = b.array("<u2", 3 * (keys - 1)).astype(np.float64).reshape(keys - 1, 3) / 65535.0 * ranges + mins
        return np.concatenate([base[None], base + np.cumsum(steps, axis=0)])
    raise FormatError(f"translation compression {_FORMAT_NAMES.get(fmt, fmt)} isn't supported")


def _uniform_quat(b: _Bytes, fmt: int, keys: int) -> np.ndarray:
    if keys == 1:
        fmt = ACF_FLOAT96
    if fmt == ACF_NONE:
        return b.floats(4 * keys).reshape(keys, 4)
    if fmt == ACF_FLOAT96:
        return _w(b.floats(3 * keys))
    if fmt == ACF_FIXED48:
        return _w((b.array("<u2", 3 * keys).astype(np.float64) - 32767.0) / 32767.0)
    if fmt == ACF_FIXED32:
        return _fixed32_quat(b.array("<u4", keys).astype(np.int64))
    if fmt == ACF_INTERVAL32:
        mins, ranges = b.floats(3), b.floats(3)
        return _fixed32_quat(b.array("<u4", keys).astype(np.int64), mins, ranges)
    if fmt == ACF_FLOAT32:
        return _float32_quat(b.array("<u4", keys))
    if fmt == ACF_POLAR32:
        return _polar32_quat(b.array("<u4", keys))
    if fmt == ACF_POLAR48:
        return _polar48_quat(b.array("<u1", 6 * keys).reshape(keys, 6))
    if fmt == ACF_IDENTITY:
        return np.tile([0.0, 0.0, 0.0, 1.0], (keys, 1))
    raise FormatError(f"rotation compression {_FORMAT_NAMES.get(fmt, fmt)} isn't supported")


def _decode_uniform(c: _Compressed) -> dict[int, Track]:
    """Fixed or variable key spacing: per track (translation offset, keys, rotation offset, keys)."""
    tracks = {}
    b = _Bytes(c.stream)
    variable = c.key_format == AKF_VARIABLE
    t_fmt, q_fmt, s_fmt = c.formats
    if len(c.offsets) < 4 * len(c.track_bones):
        raise FormatError("the animation's track offsets don't match its tracks")
    strip = c.scale_strip or 2
    for i, bone in enumerate(c.track_bones):
        t_off, t_keys, q_off, q_keys = (int(x) for x in c.offsets[4 * i:4 * i + 4])
        tr = Track()
        if t_keys > 0:
            b.seek(t_off)
            tr.translation = _uniform_vector(b, t_fmt, t_keys)
            b.align()
            if variable:
                tr.translation_frames = _times(b, t_keys, c.frames)
        if q_keys > 0:
            b.seek(q_off)
            tr.rotation = _uniform_quat(b, q_fmt, q_keys)
            if variable:
                b.align()
                tr.rotation_frames = _times(b, q_keys, c.frames)
        if len(c.scale_offsets) >= strip * (i + 1):
            s_off, s_keys = int(c.scale_offsets[strip * i]), int(c.scale_offsets[strip * i + 1])
            if s_keys > 0 and s_off >= 0:
                b.seek(s_off)
                tr.scale = _uniform_vector(b, s_fmt, s_keys)
                b.align()
                if variable:
                    tr.scale_frames = _times(b, s_keys, c.frames)
        tracks[bone] = tr
    return tracks


# Borderlands 2 numbers its per-track formats its own way past Float32NoW (UE Viewer); 6 stays Identity
# for translations, as UE Viewer reads them
BL2_PER_TRACK_ROTATION = {6: ACF_DELTA40, 7: ACF_DELTA48, 8: ACF_IDENTITY, 9: ACF_POLAR32, 10: ACF_POLAR48}
BL2_PER_TRACK_TRANSLATION = {7: ACF_DELTA48, 8: ACF_IDENTITY}


def _per_track(b: _Bytes, frames: int, rotation: bool, remap: dict | None = None) -> tuple[np.ndarray,
                                                                                           np.ndarray | None]:
    info = int(b.array("<u4", 1)[0])
    fmt, mask, keys = info >> 28, (info >> 24) & 0xF, info & 0xFFFFFF
    if remap:
        fmt = remap.get(fmt, fmt)
    mins, ranges = np.zeros(3), np.zeros(3)
    if fmt == ACF_INTERVAL32:
        for axis in range(3):
            if mask & (1 << axis):
                mins[axis], ranges[axis] = b.floats(2)
    if fmt in (ACF_NONE, ACF_FLOAT96):
        if rotation:
            values = _w(b.floats(3 * keys))
        elif mask & 7:
            axes = [a for a in range(3) if mask & (1 << a)]
            values = np.zeros((keys, 3))
            values[:, axes] = b.floats(len(axes) * keys).reshape(keys, len(axes))
        else:
            values = b.floats(3 * keys).reshape(keys, 3)
    elif fmt == ACF_FIXED48:
        axes = [a for a in range(3) if mask & (1 << a)]
        raw = b.array("<u2", len(axes) * keys).astype(np.float64).reshape(keys, len(axes))
        if rotation:
            xyz = np.zeros((keys, 3))
            xyz[:, axes] = (raw - 32767.0) / 32767.0
            values = _w(xyz)
        else:
            values = np.zeros((keys, 3))
            values[:, axes] = raw - 255.0  # DecodeFixed48_PerTrackComponent<7>
    elif fmt == ACF_INTERVAL32:
        packed = b.array("<u4", keys).astype(np.int64)
        values = _fixed32_quat(packed, mins, ranges) if rotation else _interval32_vector(packed, mins, ranges)
    elif fmt == ACF_FIXED32 and rotation:
        values = _fixed32_quat(b.array("<u4", keys).astype(np.int64))
    elif fmt == ACF_FLOAT32 and rotation:
        values = _float32_quat(b.array("<u4", keys))
    elif fmt == ACF_POLAR32 and rotation:
        values = _polar32_quat(b.array("<u4", keys))
    elif fmt == ACF_POLAR48 and rotation:
        values = _polar48_quat(b.array("<u1", 6 * keys).reshape(keys, 6))
    elif fmt == ACF_IDENTITY:
        values = np.tile([0.0, 0.0, 0.0, 1.0], (keys, 1)) if rotation else np.zeros((keys, 3))
    else:
        raise FormatError(f"per-track compression {_FORMAT_NAMES.get(fmt, fmt)} isn't supported")
    b.align()
    times = _times(b, keys, frames) if mask & 8 else None
    return values, times


def _decode_per_track(c: _Compressed) -> dict[int, Track]:
    tracks = {}
    b = _Bytes(c.stream)
    if len(c.offsets) < 2 * len(c.track_bones):
        raise FormatError("the animation's track offsets don't match its tracks")
    strip = c.scale_strip or 1
    for i, bone in enumerate(c.track_bones):
        t_off, q_off = int(c.offsets[2 * i]), int(c.offsets[2 * i + 1])
        tr = Track()
        if t_off >= 0:
            b.seek(t_off)
            tr.translation, tr.translation_frames = _per_track(b, c.frames, False)
        if q_off >= 0:
            b.seek(q_off)
            tr.rotation, tr.rotation_frames = _per_track(b, c.frames, True)
        if len(c.scale_offsets) >= strip * (i + 1):
            s_off = int(c.scale_offsets[strip * i])
            if s_off >= 0:
                b.seek(s_off)
                tr.scale, tr.scale_frames = _per_track(b, c.frames, False)
        tracks[bone] = tr
    return tracks


def decode(c: _Compressed) -> dict[int, Track]:
    if c.codec and (c.codec.upper().startswith("ACL") or "acl" in c.codec.lower() or _is_acl(c.stream)):
        raise FormatError("it is compressed with the ACL plugin, which Dark STL can't read yet")
    if not c.stream and not c.track_bones:
        return {}
    if c.key_format == AKF_PER_TRACK:
        return _decode_per_track(c)
    if c.key_format in (AKF_CONSTANT, AKF_VARIABLE):
        return _decode_uniform(c)
    raise FormatError(f"key format {c.key_format} isn't supported")


# -- the asset ------------------------------------------------------------------------------------------

def _prop_number(props: Props, *names: str) -> float | None:
    for n in names:
        v = props.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _additive(props: Props) -> bool | None:
    if not props.names():
        return None
    v = props.get("AdditiveAnimType")
    if v is None:
        return False
    return str(v).split("::")[-1] not in ("AAT_None", "0", "None")


def read_anim(pkg: Package, e: Export, bones: int | None = None) -> AnimSequence:
    """An animation's tracks (by skeleton bone index), frame count and length. ``bones``: how many bones its
    skeleton has, to check the track map against."""
    from .mesh import skeleton_of

    r = pkg.export_data(e)
    props = object_props(pkg, r, e)
    c = read_after_properties(r, pkg, bones)
    tracks = decode(c)
    keys = _prop_number(props, "NumberOfKeys", "NumFrames")
    frames = int(keys) if keys else (c.frames or 0)
    if frames <= 0:
        frames = max((len(x) for t in tracks.values() for x in (t.translation, t.rotation, t.scale)
                      if x is not None), default=1)
    length = _prop_number(props, "SequenceLength") or 0.0
    if length <= 0 and frames > 1:
        length = (frames - 1) / 30.0  # Unreal's default sampling rate, when the properties can't say
    rate = _prop_number(props, "RateScale")
    if rate and rate > 0 and length > 0:
        length = length / rate
    return AnimSequence(e.name, frames, length, tracks, skeleton_of(pkg, props), _additive(props), c.codec)


def _lerp_keys(values: np.ndarray, at: np.ndarray, frames_of: np.ndarray | None, frames: int,
               quat: bool) -> np.ndarray:
    """``values`` (n, d) sampled at frame positions ``at`` (m,)."""
    n = len(values)
    if n == 1:
        return np.repeat(values[:1], len(at), axis=0)
    if frames_of is not None and len(frames_of) == n:
        pos = np.interp(at, frames_of, np.arange(n, dtype=np.float64))
    else:
        pos = at * (n - 1) / max(frames - 1, 1)
    pos = np.clip(pos, 0, n - 1)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, n - 1)
    f = (pos - lo)[:, None]
    a, b = values[lo], values[hi]
    if quat:
        flip = (a * b).sum(axis=1) < 0
        b = np.where(flip[:, None], -b, b)
        out = a * (1 - f) + b * f
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
    return a * (1 - f) + b * f


def sample(anim: AnimSequence, bone: int, at: np.ndarray) -> tuple[np.ndarray | None, ...]:
    """A track's (translation, rotation, scale) at frame positions ``at``; None for a part it doesn't key."""
    t = anim.tracks.get(bone)
    if t is None:
        return None, None, None
    out = []
    for values, frames_of, quat in ((t.translation, t.translation_frames, False),
                                    (t.rotation, t.rotation_frames, True), (t.scale, t.scale_frames, False)):
        out.append(None if values is None or not len(values) else
                   _lerp_keys(np.asarray(values, np.float64), at, frames_of, anim.frames, quat))
    return tuple(out)


def quat_matrices(q: np.ndarray) -> np.ndarray:
    """(n, 3, 3) rotation matrices (column vectors) from quaternions (x, y, z, w)."""
    q = np.asarray(q, np.float64).reshape(-1, 4)
    q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-12)
    x, y, z, w = q.T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], 1)


def transforms_to_matrices(rot: np.ndarray, trans: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """(..., 4, 4) matrices ``T @ R @ S`` (column vectors), as ``FTransform::ToMatrixWithScale``."""
    shape = np.asarray(rot).shape[:-1]
    r = quat_matrices(np.asarray(rot).reshape(-1, 4)) * np.asarray(scale, np.float64).reshape(-1, 1, 3)
    out = np.zeros((len(r), 4, 4))
    out[:, :3, :3] = r
    out[:, :3, 3] = np.asarray(trans, np.float64).reshape(-1, 3)
    out[:, 3, 3] = 1.0
    return out.reshape(*shape, 4, 4)


__all__ = ["AnimSequence", "Skeleton", "Track", "decode", "quat_matrices", "read_after_properties", "read_anim",
           "read_compressed",
           "read_skeleton", "sample", "transforms_to_matrices"]
