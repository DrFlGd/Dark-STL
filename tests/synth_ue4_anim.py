"""Byte-built Unreal 4 ``Skeleton`` and ``AnimSequence`` packages, with keys in Unreal's own codecs.

:func:`skeleton_data` writes a skeleton's reference skeleton; :func:`anim_data` an animation's properties
and compressed keys in the layout of 4.12-4.22 (``engine`` < 423), 4.23-4.24 or 4.25+, with fixed key
spacing (translations as floats, rotations as 96-bit floats or 32-bit interval-quantised) or per-track
compression.
"""

from __future__ import annotations

import struct

import numpy as np

from synth_ue4 import Ue4Package, fstring, reference_skeleton

ACF_NONE, ACF_FLOAT96, ACF_FIXED48, ACF_INTERVAL32, ACF_FIXED32, ACF_FLOAT32, ACF_IDENTITY = range(7)
AKF_CONSTANT, AKF_VARIABLE, AKF_PER_TRACK = range(3)


def p_float(b: Ue4Package, name: str, v: float) -> bytes:
    return b.tag(name, "FloatProperty", 4) + struct.pack("<f", v)


def skeleton_data(b: Ue4Package, bones) -> bytes:
    return b.props() + reference_skeleton(b, bones)


def _w_positive(q) -> np.ndarray:
    q = np.asarray(q, float).reshape(-1, 4)
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    return np.where(q[:, 3:4] < 0, -q, q)


def _interval32(q) -> bytes:
    q = _w_positive(q)[:, :3]
    lo, hi = q.min(0), q.max(0)
    mins, ranges = (lo + hi) / 2, np.maximum((hi - lo) / 2, 1e-6)
    u = (q - mins) / ranges  # -1..1
    x = np.clip(np.round((u[:, 0] + 1) * 1023), 0, 2047).astype(np.uint32)
    y = np.clip(np.round((u[:, 1] + 1) * 1023), 0, 2047).astype(np.uint32)
    z = np.clip(np.round((u[:, 2] + 1) * 511), 0, 1023).astype(np.uint32)
    packed = (x << 21) | (y << 10) | z
    return struct.pack("<3f", *mins) + struct.pack("<3f", *ranges) + packed.astype("<u4").tobytes()


def _pad(buf: bytearray) -> None:
    buf += bytes(-len(buf) % 4)


def encode_tracks(tracks, rotation_format: int = ACF_FLOAT96, per_track: bool = False):
    """``tracks``: per track (translation keys (n, 3) or None, rotation keys (n, 4) or None). Returns
    (stream, offsets) for fixed key spacing (4 per track) or per-track compression (2 per track)."""
    stream = bytearray()
    offsets = []
    for t, q in tracks:
        if per_track:
            for values, rot in ((t, False), (q, True)):
                if values is None:
                    offsets.append(-1)
                    continue
                offsets.append(len(stream))
                values = np.asarray(values, float)
                keys = len(values)
                stream += struct.pack("<I", ACF_FLOAT96 << 28 | 7 << 24 | keys)
                stream += (_w_positive(values)[:, :3] if rot else values).astype("<f4").tobytes()
                _pad(stream)
            continue
        if t is None:
            offsets += [-1, 0]
        else:
            t = np.asarray(t, float)
            offsets += [len(stream), len(t)]
            stream += t.astype("<f4").tobytes()
            _pad(stream)
        if q is None:
            offsets += [-1, 0]
        else:
            q = np.asarray(q, float)
            offsets += [len(stream), len(q)]
            if len(q) == 1 or rotation_format == ACF_FLOAT96:
                stream += _w_positive(q)[:, :3].astype("<f4").tobytes()
            elif rotation_format == ACF_INTERVAL32:
                stream += _interval32(q)
            elif rotation_format == ACF_NONE:
                stream += np.asarray(q, "<f4").tobytes()
            else:
                raise ValueError(rotation_format)
            _pad(stream)
    return bytes(stream), offsets


# UAnimationAsset::Serialize writes the skeleton's GUID after the object's own (every 4.x/5.x animation)
SKELETON_GUID = bytes.fromhex("c7a1e05b4f3d9e2a8b6c1d0e5f4a3b29")


def anim_data(b: Ue4Package, skeleton: int, track_bones: list[int], tracks, frames: int, length: float,
              rotation_format: int = ACF_FLOAT96, per_track: bool = False, additive: bool = False,
              codec: str = "/Script/Engine.AnimCompress_BitwiseCompressOnly") -> bytes:
    e = b.engine
    items = [b.p_int("NumFrames", frames), p_float(b, "SequenceLength", length), b.p_obj("Skeleton", skeleton)]
    if additive:
        items.append(b.p_enum("AdditiveAnimType", "EAdditiveAnimationType", "AAT_LocalSpaceBase"))
    out = b.props(*items) + SKELETON_GUID
    out += bytes([1, 0]) + struct.pack("<I", 1)  # strip flags, bSerializeCompressedData
    stream, offsets = encode_tracks(tracks, rotation_format, per_track)
    key = AKF_PER_TRACK if per_track else AKF_CONSTANT
    t_fmt, q_fmt = (ACF_IDENTITY, ACF_IDENTITY) if per_track else (ACF_NONE, rotation_format)
    formats = bytes([key, t_fmt, q_fmt, ACF_NONE])
    track_map = struct.pack("<i", len(track_bones)) + struct.pack(f"<{len(track_bones)}i", *track_bones)
    offs = struct.pack(f"<{len(offsets)}i", *offsets)
    if e < 423:
        out += formats + struct.pack("<i", len(offsets)) + offs + struct.pack("<i", 0) + struct.pack("<i", 0)
        if e >= 421:
            out += struct.pack("<i", 0)  # segments
        out += track_map
        out += b.end() if e < 422 else struct.pack("<i", 0)  # curves
        if e >= 417:
            out += struct.pack("<i", 0)
        if e >= 422:
            out += struct.pack("<i", frames)
        out += struct.pack("<i", len(stream)) + stream
        if e >= 422:
            out += fstring("") + struct.pack("<i", 0)
    elif e < 425:
        out += struct.pack("<i", 0) + track_map + struct.pack("<i", 0) + formats + struct.pack("<i", frames)
        out += struct.pack("<iiii", len(offsets), 0, 0, len(stream))
        data = offs + stream
        out += struct.pack("<iI", len(data), 0) + data + fstring("") + struct.pack("<i", 0)
    else:
        data = offs + stream
        out += struct.pack("<i", 0) + track_map + struct.pack("<i", 0) + struct.pack("<iI", len(data), 0) + data
        out += fstring(codec) + fstring("") + struct.pack("<i", 0)
        out += struct.pack("<i", frames) + formats + struct.pack("<iiii", len(stream), len(offsets), 0, 0)
    return out + struct.pack("<I", 0)  # bUseRawDataOnly


def acl_anim_data(b: Ue4Package, skeleton: int, frames: int, length: float) -> bytes:
    """An animation whose keys are compressed with the ACL plugin (4.25+ layout; the ACL buffer is a stub)."""
    out = b.props(b.p_int("NumFrames", frames), p_float(b, "SequenceLength", length), b.p_obj("Skeleton", skeleton))
    out += SKELETON_GUID + bytes([1, 0]) + struct.pack("<I", 1)
    data = struct.pack("<III", 64, 0, 0xAC11AC11) + bytes(52)
    out += struct.pack("<i", 0) + struct.pack("<ii", 1, 0) + struct.pack("<i", 0) + struct.pack("<iI", len(data), 0)
    out += data + fstring("AnimBoneCompressionCodec_ACL_0") + fstring("") + struct.pack("<i", 0)
    out += struct.pack("<i", frames)
    return out + struct.pack("<I", 0)
