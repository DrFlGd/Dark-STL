"""DCX / DCP compression containers.

Most game files are wrapped in a DCX header that names the codec:

* ``DFLT`` - zlib (Dark Souls 1 Remastered, Bloodborne, Dark Souls III)
* ``KRAK`` - Oodle Kraken (Sekiro, Elden Ring). Oodle is proprietary, so the
  game's own ``oo2core_*.dll`` is loaded at runtime (see :mod:`darkstl.formats.oodle`).
* ``ZSTD`` - Zstandard (later Elden Ring patches)
* ``EDGE`` - PS3 chunked deflate (Demon's Souls, not supported)

Headers are always big-endian.
"""

from __future__ import annotations

import struct
import zlib

from .binary import FormatError

DCX_MAGICS = (b"DCX\0", b"DCP\0")


def is_dcx(data: bytes) -> bool:
    return data[:4] in DCX_MAGICS


def _inflate(data: bytes, expected_size: int | None = None) -> bytes:
    decomp = zlib.decompressobj()
    out = decomp.decompress(data)
    out += decomp.flush()
    if expected_size is not None and expected_size >= 0 and len(out) != expected_size:
        raise FormatError(f"Decompressed size {len(out)} does not match header size {expected_size}")
    return out


def decompress(data: bytes) -> tuple[bytes, str]:
    """Decompress a DCX/DCP (or raw zlib) blob. Returns ``(payload, codec)``."""
    magic = data[:4]
    if magic == b"DCX\0":
        codec = data[0x28:0x2C].decode("ascii", errors="replace")
        uncompressed_size, compressed_size = struct.unpack_from(">II", data, 0x1C)
        if data[0x18:0x1C] != b"DCS\0" or data[0x24:0x28] != b"DCP\0":
            raise FormatError("Malformed DCX header")
        dca = data.find(b"DCA\0", 0x2C, 0x80)
        if dca < 0:
            raise FormatError("DCX header has no DCA block")
        (dca_size,) = struct.unpack_from(">I", data, dca + 4)
        start = dca + dca_size
        payload = data[start:start + compressed_size]
        if codec == "DFLT":
            return _inflate(payload, uncompressed_size), "DFLT"
        if codec == "KRAK":
            from .oodle import oodle_decompress

            return oodle_decompress(payload, uncompressed_size), "KRAK"
        if codec == "ZSTD":
            try:
                import zstandard
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise FormatError("ZSTD-compressed file needs the 'zstandard' package") from exc
            return zstandard.ZstdDecompressor().decompress(payload, max_output_size=uncompressed_size), "ZSTD"
        raise FormatError(f"Unsupported DCX codec {codec!r}")
    if magic == b"DCP\0":
        codec = data[4:8].decode("ascii", errors="replace")
        if codec != "DFLT":
            raise FormatError(f"Unsupported DCP codec {codec!r}")
        uncompressed_size, compressed_size = struct.unpack_from(">II", data, 0x24)
        return _inflate(data[0x2C:0x2C + compressed_size], uncompressed_size), "DCP_DFLT"
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        return _inflate(data), "ZLIB"
    raise FormatError("Not a DCX/DCP container")


def decompress_head(data: bytes, size: int) -> bytes | None:
    """The first ``size`` bytes (or fewer, if that's all there is) of a DCX's payload, from ``data``
    that may be only the start of the file. zlib streams only; None for the other codecs, which
    have to be decompressed whole."""
    if data[:4] == b"DCX\0" and data[0x28:0x2C] == b"DFLT":
        dca = data.find(b"DCA\0", 0x2C, 0x80)
        if dca < 0:
            raise FormatError("DCX header has no DCA block")
        (dca_size,) = struct.unpack_from(">I", data, dca + 4)
        payload = data[dca + dca_size:]
    elif data[:4] == b"DCP\0" and data[4:8] == b"DFLT":
        payload = data[0x2C:]
    else:
        return None
    try:
        return zlib.decompressobj().decompress(payload, size)
    except zlib.error as exc:
        raise FormatError(f"Bad zlib data: {exc}") from exc


def maybe_decompress(data: bytes) -> bytes:
    """Return the payload of a DCX container, or the data unchanged."""
    if is_dcx(data):
        return decompress(data)[0]
    return data


def compress_dflt(payload: bytes) -> bytes:
    """Wrap ``payload`` in a DS3-style DCX DFLT container (used by tests and tools)."""
    body = zlib.compress(payload, 9)
    header = b"".join([
        b"DCX\0",
        struct.pack(">iiiii", 0x10000, 0x18, 0x24, 0x44, 0x4C),
        b"DCS\0",
        struct.pack(">II", len(payload), len(body)),
        b"DCP\0",
        b"DFLT",
        struct.pack(">i", 0x20),
        bytes([9, 0, 0, 0]),
        struct.pack(">i", 0),
        bytes([0, 0, 0, 0]),
        struct.pack(">ii", 0, 0x00010100),
        b"DCA\0",
        struct.pack(">i", 8),
    ])
    assert len(header) == 0x4C
    return header + body
