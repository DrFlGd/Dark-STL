"""LZO1X decompression, which Borderlands 1, 2 and The Pre-Sequel use for their packages on PC.

A plain Python port of the reference decoder's logic (``lzo1x_decompress_safe``): no
LZO library is needed. Packages are read a block (usually 128 KB) at a time, so the
speed of this loop only matters while a folder is first scanned.
"""

from __future__ import annotations

from ....errors import FormatError

_M2_MAX_OFFSET = 0x0800


def _run_length(src: bytes, ip: int, base: int) -> tuple[int, int]:
    """A long length: zero bytes each add 255, then one byte; ``base`` is added."""
    zeros = 0
    while src[ip] == 0:
        zeros += 1
        ip += 1
    return base + zeros * 255 + src[ip], ip + 1


def decompress(src: bytes, out_size: int | None = None) -> bytes:
    """Decompress one LZO1X stream (any of the LZO1X-1/-999 levels; they share the format)."""
    out = bytearray()
    ip = 0
    n = len(src)
    try:
        state = 0
        if src[0] > 17:
            t = src[0] - 17
            ip = 1
            out += src[ip:ip + t]
            ip += t
            state = 4 if t >= 4 else t
        while True:
            t = src[ip]
            ip += 1
            if t < 16:
                if state == 0:
                    if t == 0:
                        t, ip = _run_length(src, ip, 15)
                    t += 3
                    out += src[ip:ip + t]
                    ip += t
                    state = 4
                    continue
                if state != 4:
                    nxt = t & 3
                    m = len(out) - 1 - (t >> 2) - (src[ip] << 2)
                    ip += 1
                    length = 2
                else:
                    nxt = t & 3
                    m = len(out) - 1 - _M2_MAX_OFFSET - (t >> 2) - (src[ip] << 2)
                    ip += 1
                    length = 3
            elif t >= 64:
                nxt = t & 3
                m = len(out) - 1 - ((t >> 2) & 7) - (src[ip] << 3)
                ip += 1
                length = (t >> 5) + 1
            elif t >= 32:
                length = (t & 31) + 2
                if length == 2:
                    length, ip = _run_length(src, ip, 31 + 2)
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                m = len(out) - 1 - (v >> 2)
                nxt = v & 3
            else:
                m = len(out) - ((t & 8) << 11)
                length = (t & 7) + 2
                if length == 2:
                    length, ip = _run_length(src, ip, 7 + 2)
                v = src[ip] | (src[ip + 1] << 8)
                ip += 2
                m -= v >> 2
                nxt = v & 3
                if m == len(out):
                    break  # end of stream
                m -= 0x4000
            if m < 0:
                raise FormatError("LZO data refers back before its start")
            dist = len(out) - m
            if dist >= length:
                out += out[m:m + length]
            else:  # overlapping copy: the pattern repeats
                pattern = out[m:]
                reps, rest = divmod(length, dist)
                out += pattern * reps + pattern[:rest]
            state = nxt
            if nxt:
                out += src[ip:ip + nxt]
                ip += nxt
            if ip > n:
                raise IndexError
    except IndexError:
        raise FormatError("LZO data ends early") from None
    if out_size is not None and len(out) != out_size:
        raise FormatError(f"LZO block gave {len(out)} bytes, expected {out_size}")
    return bytes(out)


__all__ = ["decompress"]
