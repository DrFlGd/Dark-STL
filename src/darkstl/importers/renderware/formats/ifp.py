"""GTA animation packages (``.ifp``).

GTA III and Vice City (``ANPK``): ``INFO`` (count, name), then per animation ``NAME`` and a
``DGAN`` block of ``CPAN`` objects, each an ``ANIM`` header (28-byte bone name, frame count,
and in Vice City's later files the bone id) and a key-frame block: ``KR00`` (rotation),
``KRT0`` (and translation) or ``KRTS`` (and scale), each frame ending with its time in seconds.
Every block's data is padded to four bytes.

San Andreas (``ANP3``): a 24-byte package name and count, then per animation a 24-byte name,
bone count, frame data size and a flag saying whether frames are compressed; per bone a 24-byte
name, frame type (rotation only, or with translation), frame count and HAnim bone id. Frames are
floats, or int16: quaternion / 4096, translation / 1024, time / 60 seconds.

Quaternions are stored conjugated (as re3 and librw read them); they are returned the right way
round, ``(x, y, z, w)``. Formats from the gtamods.com wiki.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError


@dataclass
class BoneTrack:
    name: str
    bone_id: int | None
    times: np.ndarray  # (n,) seconds
    rotations: np.ndarray  # (n, 4) x, y, z, w
    translations: np.ndarray | None = None  # (n, 3)


@dataclass
class Animation:
    name: str
    tracks: list[BoneTrack] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((float(t.times[-1]) for t in self.tracks if len(t.times)), default=0.0)


@dataclass
class AnimationPackage:
    name: str
    format: str  # "ANPK" or "ANP3"
    animations: list[Animation]


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1").strip()


def _pad(n: int) -> int:
    return (n + 3) & ~3


def _conjugate(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, np.float64).reshape(-1, 4).copy()
    q[:, :3] *= -1
    return q


def _read_anpk(data: bytes) -> AnimationPackage:
    pos = 8
    end = min(len(data), 8 + struct.unpack_from("<I", data, 4)[0])
    if data[pos:pos + 4] != b"INFO":
        raise FormatError("ANPK without INFO")
    size = struct.unpack_from("<I", data, pos + 4)[0]
    (count,) = struct.unpack_from("<i", data, pos + 8)
    pkg_name = _cstr(data[pos + 12:pos + 8 + size])
    pos += 8 + _pad(size)
    anims = []
    while pos + 8 <= end and len(anims) < max(count, 0):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if tag != b"NAME":
            raise FormatError(f"ANPK: expected NAME, found {tag!r}")
        anim = Animation(_cstr(data[pos + 8:pos + 8 + size]))
        pos += 8 + _pad(size)
        if data[pos:pos + 4] != b"DGAN":
            raise FormatError("ANPK: animation without DGAN")
        dgan_end = pos + 8 + struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        if data[pos:pos + 4] == b"INFO":
            pos += 8 + _pad(struct.unpack_from("<I", data, pos + 4)[0])
        while pos + 8 <= dgan_end:
            tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
            if tag != b"CPAN":
                pos += 8 + _pad(size)
                continue
            cpan_end = pos + 8 + size
            pos += 8
            name, bone_id, frames = "", None, 0
            if data[pos:pos + 4] == b"ANIM":
                asize = struct.unpack_from("<I", data, pos + 4)[0]
                body = data[pos + 8:pos + 8 + asize]
                name = _cstr(body[:28])
                frames = struct.unpack_from("<i", body, 28)[0] if len(body) >= 32 else 0
                if len(body) >= 48:
                    bone_id = struct.unpack_from("<i", body, 44)[0]
                pos += 8 + _pad(asize)
            if pos + 8 <= cpan_end and frames > 0:
                kind = data[pos:pos + 4]
                stride = {b"KR00": 5, b"KRT0": 8, b"KRTS": 11}.get(kind)
                if stride is not None:
                    vals = np.frombuffer(data, "<f4", count=frames * stride, offset=pos + 8).reshape(frames, stride)
                    track = BoneTrack(name, bone_id, vals[:, -1].astype(np.float64), _conjugate(vals[:, :4]),
                                      vals[:, 4:7].astype(np.float64) if stride >= 8 else None)
                    anim.tracks.append(track)
            pos = cpan_end
        anims.append(anim)
        pos = dgan_end
    return AnimationPackage(pkg_name, "ANPK", anims)


def _read_anp3(data: bytes) -> AnimationPackage:
    pkg_name = _cstr(data[8:32])
    (count,) = struct.unpack_from("<I", data, 32)
    pos = 36
    anims = []
    for _ in range(count):
        if pos + 36 > len(data):
            break
        name = _cstr(data[pos:pos + 24])
        bones, _data_size, flags = struct.unpack_from("<III", data, pos + 24)
        compressed = bool(flags & 1)
        pos += 36
        anim = Animation(name)
        for _ in range(bones):
            bname = _cstr(data[pos:pos + 24])
            kind, frames, bone_id = struct.unpack_from("<IIi", data, pos + 24)
            pos += 36
            translated = kind in (2, 4)
            if compressed:
                stride = 8 if translated else 5
                vals = np.frombuffer(data, "<i2", count=frames * stride, offset=pos).reshape(frames, stride)
                pos += frames * stride * 2
                rot = vals[:, :4].astype(np.float64) / 4096.0
                times = vals[:, 4].astype(np.float64) / 60.0
                trans = vals[:, 5:8].astype(np.float64) / 1024.0 if translated else None
            else:
                stride = 8 if translated else 5
                vals = np.frombuffer(data, "<f4", count=frames * stride, offset=pos).reshape(frames, stride)
                pos += frames * stride * 4
                rot = vals[:, :4].astype(np.float64)
                times = vals[:, 4].astype(np.float64)
                trans = vals[:, 5:8].astype(np.float64) if translated else None
            if frames:
                anim.tracks.append(BoneTrack(bname, bone_id, times, _conjugate(rot), trans))
        anims.append(anim)
    return AnimationPackage(pkg_name, "ANP3", anims)


def read_ifp(data: bytes) -> AnimationPackage:
    tag = data[:4]
    try:
        if tag == b"ANPK":
            return _read_anpk(data)
        if tag == b"ANP3":
            return _read_anp3(data)
    except (struct.error, ValueError) as exc:
        raise FormatError(f"Animation package is cut short: {exc}") from exc
    raise FormatError(f"Not a GTA animation package ({tag!r})")


def is_ifp(data: bytes) -> bool:
    return data[:4] in (b"ANPK", b"ANP3")


def quat_matrix(q: np.ndarray) -> np.ndarray:
    """(n, 4) quaternions (x, y, z, w) as (n, 3, 3) rotation matrices."""
    q = np.asarray(q, np.float64).reshape(-1, 4)
    n = np.linalg.norm(q, axis=1, keepdims=True)
    x, y, z, w = (q / np.where(n > 0, n, 1)).T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], 1)


def sample(track: BoneTrack, t: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """The track's rotation (n, 4) and translation (n, 3) at times ``t`` (seconds): spherical between
    key frames, held before the first and after the last."""
    t = np.asarray(t, np.float64)
    times = track.times
    if len(times) == 1:
        rot = np.repeat(track.rotations[:1], len(t), 0)
        tr = None if track.translations is None else np.repeat(track.translations[:1], len(t), 0)
        return rot, tr
    i = np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(times) - 2)
    span = np.maximum(times[i + 1] - times[i], 1e-9)
    f = np.clip((t - times[i]) / span, 0.0, 1.0)[:, None]
    a, b = track.rotations[i], track.rotations[i + 1].copy()
    dot = np.sum(a * b, axis=1)
    b[dot < 0] *= -1
    dot = np.abs(dot)
    theta = np.arccos(np.clip(dot, -1, 1))[:, None]
    sin = np.sin(theta)
    small = sin[:, 0] < 1e-6
    wa = np.where(small[:, None], 1 - f, np.sin((1 - f) * theta) / np.where(sin > 0, sin, 1))
    wb = np.where(small[:, None], f, np.sin(f * theta) / np.where(sin > 0, sin, 1))
    rot = wa * a + wb * b
    tr = None
    if track.translations is not None:
        tr = track.translations[i] * (1 - f) + track.translations[i + 1] * f
    return rot, tr
