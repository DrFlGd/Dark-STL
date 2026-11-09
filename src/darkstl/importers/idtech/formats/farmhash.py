"""FarmHash's 64-bit fingerprint (``farmhashna::Hash64``), which id Tech uses to key its containers.

The container mask (``generated/buildgame/container.mask``) names each ``.resources`` file by the fingerprint of
its entry table. A plain transcription of the published algorithm (Google's FarmHash, MIT); only used on a few
megabytes per game, once, so pure Python is fast enough.
"""

from __future__ import annotations

_M = (1 << 64) - 1
K0 = 0xC3A5C85C97CB3127
K1 = 0xB492B66FBE98F273
K2 = 0x9AE16A3B2F90404F


def _f64(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 8], "little")


def _f32(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 4], "little")


def _rot(v: int, r: int) -> int:
    return ((v >> r) | (v << (64 - r))) & _M if r else v


def _smix(v: int) -> int:
    return v ^ (v >> 47)


def _len16(u: int, v: int, mul: int) -> int:
    a = ((u ^ v) * mul) & _M
    a ^= a >> 47
    b = ((v ^ a) * mul) & _M
    b ^= b >> 47
    return (b * mul) & _M


def _len0to16(s: bytes) -> int:
    n = len(s)
    if n >= 8:
        mul = K2 + n * 2
        a = (_f64(s, 0) + K2) & _M
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & _M
        d = ((_rot(a, 25) + b) * mul) & _M
        return _len16(c, d, mul)
    if n >= 4:
        mul = K2 + n * 2
        return _len16((n + (_f32(s, 0) << 3)) & _M, _f32(s, n - 4), mul)
    if n > 0:
        y = s[0] + (s[n >> 1] << 8)
        z = n + (s[n - 1] << 2)
        return (_smix(((y * K2) ^ (z * K0)) & _M) * K2) & _M
    return K2


def _len17to32(s: bytes) -> int:
    n = len(s)
    mul = K2 + n * 2
    a = (_f64(s, 0) * K1) & _M
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & _M
    d = (_f64(s, n - 16) * K2) & _M
    return _len16((_rot((a + b) & _M, 43) + _rot(c, 30) + d) & _M, (a + _rot((b + K2) & _M, 18) + c) & _M, mul)


def _len33to64(s: bytes) -> int:
    n = len(s)
    mul = K2 + n * 2
    a = (_f64(s, 0) * K2) & _M
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & _M
    d = (_f64(s, n - 16) * K2) & _M
    y = (_rot((a + b) & _M, 43) + _rot(c, 30) + d) & _M
    z = _len16(y, (a + _rot((b + K2) & _M, 18) + c) & _M, mul)
    e = (_f64(s, 16) * mul) & _M
    f = _f64(s, 24)
    g = ((y + _f64(s, n - 32)) * mul) & _M
    h = ((z + _f64(s, n - 24)) * mul) & _M
    return _len16((_rot((e + f) & _M, 43) + _rot(g, 30) + h) & _M, (e + _rot((f + a) & _M, 18) + g) & _M, mul)


def _weak(s: bytes, i: int, a: int, b: int) -> tuple[int, int]:
    w, x, y, z = _f64(s, i), _f64(s, i + 8), _f64(s, i + 16), _f64(s, i + 24)
    a = (a + w) & _M
    b = _rot((b + a + z) & _M, 21)
    c = a
    a = (a + x + y) & _M
    b = (b + _rot(a, 44)) & _M
    return (a + z) & _M, (b + c) & _M


def fingerprint64(data: bytes) -> int:
    """FarmHash Fingerprint64 of ``data`` (the same as Guava's ``farmHashFingerprint64``)."""
    s = bytes(data)
    n = len(s)
    if n <= 16:
        return _len0to16(s)
    if n <= 32:
        return _len17to32(s)
    if n <= 64:
        return _len33to64(s)
    seed = 81
    x = seed
    y = (seed * K1 + 113) & _M
    z = (_smix((y * K2 + 113) & _M) * K2) & _M
    v = (0, 0)
    w = (0, 0)
    x = (x * K2 + _f64(s, 0)) & _M
    end = ((n - 1) // 64) * 64
    last64 = end + ((n - 1) & 63) - 63
    i = 0
    while i != end:
        x = (_rot((x + y + v[0] + _f64(s, i + 8)) & _M, 37) * K1) & _M
        y = (_rot((y + v[1] + _f64(s, i + 48)) & _M, 42) * K1) & _M
        x ^= w[1]
        y = (y + v[0] + _f64(s, i + 40)) & _M
        z = (_rot((z + w[0]) & _M, 33) * K1) & _M
        v = _weak(s, i, (v[1] * K1) & _M, (x + w[0]) & _M)
        w = _weak(s, i + 32, (z + w[1]) & _M, (y + _f64(s, i + 16)) & _M)
        z, x = x, z
        i += 64
    mul = K1 + ((z & 0xFF) << 1)
    i = last64
    w0 = (w[0] + ((n - 1) & 63)) & _M
    v0 = (v[0] + w0) & _M
    w0 = (w0 + v0) & _M
    v, w = (v0, v[1]), (w0, w[1])
    x = (_rot((x + y + v[0] + _f64(s, i + 8)) & _M, 37) * mul) & _M
    y = (_rot((y + v[1] + _f64(s, i + 48)) & _M, 42) * mul) & _M
    x ^= (w[1] * 9) & _M
    y = (y + v[0] * 9 + _f64(s, i + 40)) & _M
    z = (_rot((z + w[0]) & _M, 33) * mul) & _M
    v = _weak(s, i, (v[1] * mul) & _M, (x + w[0]) & _M)
    w = _weak(s, i + 32, (z + w[1]) & _M, (y + _f64(s, i + 16)) & _M)
    z, x = x, z
    return _len16((_len16(v[0], w[0], mul) + _smix(y) * K0 + z) & _M, (_len16(v[1], w[1], mul) + x) & _M, mul)
