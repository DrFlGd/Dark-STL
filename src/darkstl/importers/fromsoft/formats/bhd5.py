"""The big archives FromSoftware games ship their files in: a ``.bhd`` header and a ``.bdt`` of data.

Dark Souls (Prepare to Die Edition), Dark Souls II, Dark Souls III, Sekiro and Elden Ring keep
almost every file in a few pairs such as ``Data0.bhd`` + ``Data0.bdt``. The header (BHD5) lists
each file by a hash of its path, with where it lies in the ``.bdt``, its size and, for some, the
ranges of it encrypted with AES-128. Since Dark Souls II the header itself is encrypted with RSA:
the games ship the public key (Dark Souls II as ``.pem`` files beside the archives, the later
games inside the executable; the community has published them), and "decrypting" it is the RSA
public operation on each block.

Paths aren't stored, only their hashes, so file names come from lists the modding community made
(see :mod:`..unpack`). The hash is the game's own: the path lower-cased, with forward slashes and
a leading slash, folded as ``h = h * 37 + c`` in 32 bits (``h * 133 + c`` in 64 bits since
Elden Ring).

The format is described by SoulsFormats' BHD5 and by BinderTool and UXM; this is an independent
reading of it.
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field
from typing import BinaryIO

from .binary import FormatError

# The layouts of the header, by the game that introduced them.
DS1, DS2, DS3, ER = "DS1", "DS2", "DS3", "ER"
FORMATS = (DS1, DS2, DS3, ER)

MAGIC = b"BHD5"


# --------------------------------------------------------------------------- path hashes


def hash_path(path: str, fmt: str) -> int:
    """The hash an archive lists ``path`` (``/chr/c2010.chrbnd.dcx``) under."""
    text = path.strip().replace("\\", "/").lower()
    if not text.startswith("/"):
        text = "/" + text
    if fmt == ER:
        h, prime, mask = 0, 133, (1 << 64) - 1
    else:
        h, prime, mask = 0, 37, (1 << 32) - 1
    for c in text:
        h = (h * prime + ord(c)) & mask
    return h


def hash_bits(fmt: str) -> int:
    return 64 if fmt == ER else 32


# --------------------------------------------------------------------------- RSA


@dataclass(frozen=True)
class PublicKey:
    n: int
    e: int

    @property
    def size(self) -> int:
        """Bytes in one encrypted block."""
        return (self.n.bit_length() + 7) // 8


def _der_length(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    count = first & 0x7F
    if not 0 < count <= 4:
        raise FormatError("Bad length in an RSA key")
    return int.from_bytes(data[pos:pos + count], "big"), pos + count


def _der_take(data: bytes, pos: int, tag: int) -> tuple[bytes, int]:
    if pos >= len(data) or data[pos] != tag:
        raise FormatError("Not an RSA public key")
    length, pos = _der_length(data, pos + 1)
    if pos + length > len(data):
        raise FormatError("An RSA key is cut short")
    return data[pos:pos + length], pos + length


def parse_public_key(pem: str | bytes) -> PublicKey:
    """An RSA public key from PEM text: ``RSA PUBLIC KEY`` (PKCS #1, how the games' keys are
    published) or ``PUBLIC KEY`` (X.509 SubjectPublicKeyInfo), or the bare base64 of either."""
    text = pem.decode("ascii", "replace") if isinstance(pem, bytes) else pem
    body = re.sub(r"-----(BEGIN|END)[^-]*-----", "", text)
    try:
        der = base64.b64decode(re.sub(r"\s+", "", body), validate=False)
    except ValueError as exc:
        raise FormatError(f"Not an RSA key: {exc}") from None
    seq, _ = _der_take(der, 0, 0x30)
    if seq[:1] == b"\x30":  # SubjectPublicKeyInfo: SEQUENCE { algorithm, BIT STRING { RSAPublicKey } }
        _alg, pos = _der_take(seq, 0, 0x30)
        bits, _ = _der_take(seq, pos, 0x03)
        seq, _ = _der_take(bits[1:], 0, 0x30)
    n_bytes, pos = _der_take(seq, 0, 0x02)
    e_bytes, _ = _der_take(seq, pos, 0x02)
    n, e = int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")
    if n < (1 << 256) or e < 3:
        raise FormatError("Not a usable RSA key")
    return PublicKey(n, e)


def rsa_decrypt(data: bytes, key: PublicKey, progress=None) -> bytes:
    """Undo the games' RSA encryption of a header: each block of ``key.size`` bytes becomes
    ``key.size - 1`` bytes of plain data (``m = c ** e mod n``, written big-endian, zero-filled)."""
    size = key.size
    out_size = size - 1
    if len(data) % size:
        raise FormatError(f"The header's size ({len(data):,} bytes) isn't a whole number of {size}-byte blocks")
    blocks = len(data) // size
    out = bytearray(blocks * out_size)
    n, e = key.n, key.e
    step = max(1, blocks // 100)
    for i in range(blocks):
        c = int.from_bytes(data[i * size:(i + 1) * size], "big")
        m = pow(c, e, n)
        try:
            out[i * out_size:(i + 1) * out_size] = m.to_bytes(out_size, "big")
        except OverflowError:
            raise FormatError("This key doesn't fit the archive (wrong game or archive?)") from None
        if progress is not None and i % step == 0:
            progress(i / blocks)
    return bytes(out)


# --------------------------------------------------------------------------- the header


@dataclass
class Entry:
    hash: int
    size: int  # bytes in the .bdt (padded to the AES block size when encrypted)
    offset: int
    unpadded: int = -1  # the file's own size (Dark Souls III on), when the stored copy is padded
    aes_key: bytes | None = None
    aes_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def file_size(self) -> int:
        return self.unpadded if 0 < self.unpadded <= self.size else self.size


@dataclass
class Header:
    fmt: str
    salt: str
    entries: list[Entry]

    def by_hash(self) -> dict[int, Entry]:
        return {e.hash: e for e in self.entries}


def is_encrypted(data: bytes) -> bool:
    return data[:4] != MAGIC


def read_header(data: bytes, fmt: str) -> Header:
    """Read a (decrypted) BHD5 header laid out as ``fmt`` (one of FORMATS)."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown BHD5 layout {fmt!r}")
    if data[:4] != MAGIC:
        raise FormatError("Not a BHD5 archive header (or it didn't decrypt: wrong key?)")
    flag = struct.unpack_from("b", data, 4)[0]
    if flag not in (0, -1):
        raise FormatError("Unknown BHD5 header")
    end = ">" if flag == 0 else "<"
    version, _size, bucket_count, buckets_offset = struct.unpack_from(end + "iiii", data, 8)
    if version != 1:
        raise FormatError(f"Unknown BHD5 version {version}")
    salt = ""
    if fmt != DS1:
        (salt_len,) = struct.unpack_from(end + "i", data, 24)
        salt = data[28:28 + salt_len].decode("ascii", "replace")
    if not 0 <= bucket_count < 1 << 24 or not 0 < buckets_offset < len(data):
        raise FormatError("BHD5 header out of range (wrong key or game?)")

    def aes(offset: int) -> tuple[bytes, list[tuple[int, int]]]:
        key = data[offset:offset + 16]
        (count,) = struct.unpack_from(end + "i", data, offset + 16)
        ranges = [struct.unpack_from(end + "qq", data, offset + 20 + 16 * i) for i in range(count)]
        return key, [(a, b) for a, b in ranges if a != -1 and b != -1 and a != b]

    entries: list[Entry] = []
    try:
        for b in range(bucket_count):
            count, offset = struct.unpack_from(end + "ii", data, buckets_offset + 8 * b)
            for i in range(count):
                if fmt == ER:
                    h, size, unpadded, off, _sha, aes_off = struct.unpack_from(end + "QiiqqQ", data, offset + 40 * i)
                elif fmt == DS3:
                    h, size, off, _sha, aes_off, unpadded = struct.unpack_from(end + "Iiqqqq", data, offset + 40 * i)
                elif fmt == DS2:
                    h, size, off, _sha, aes_off = struct.unpack_from(end + "Iiqqq", data, offset + 32 * i)
                    unpadded = -1
                else:
                    h, size, off = struct.unpack_from(end + "Iiq", data, offset + 16 * i)
                    unpadded, aes_off = -1, 0
                e = Entry(h, size, off, unpadded)
                if aes_off:
                    e.aes_key, e.aes_ranges = aes(aes_off)
                entries.append(e)
    except struct.error:
        raise FormatError("BHD5 header cut short (wrong key or game?)") from None
    return Header(fmt, salt, entries)


def load_header(raw: bytes, fmt: str, key: PublicKey | None = None, progress=None) -> Header:
    """Read a header as it is on disk, decrypting it with ``key`` first when it is encrypted."""
    if is_encrypted(raw):
        if key is None:
            raise FormatError("This archive's header is encrypted and there's no key for it")
        raw = rsa_decrypt(raw, key, progress)
    return read_header(raw, fmt)


# --------------------------------------------------------------------------- the data


def _aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # noqa: S305 - the games' own scheme
    return dec.update(data) + dec.finalize()


def read_entry(bdt: BinaryIO, entry: Entry) -> bytes:
    """A file's bytes out of the ``.bdt``: decrypted where it's encrypted, cut to its own size."""
    bdt.seek(entry.offset)
    data = bdt.read(entry.size)
    if len(data) != entry.size:
        raise FormatError("The archive's data file is shorter than its header says")
    if entry.aes_key and entry.aes_ranges:
        buf = bytearray(data)
        for start, stop in entry.aes_ranges:
            stop = min(stop, len(buf))
            length = (stop - start) // 16 * 16
            if start < 0 or length <= 0:
                continue
            buf[start:start + length] = _aes_ecb_decrypt(entry.aes_key, bytes(buf[start:start + length]))
        data = bytes(buf)
    return data[:entry.file_size]


def encrypt_header_for_tests(plain: bytes, n: int, d: int) -> bytes:
    """The inverse of :func:`rsa_decrypt` given the private exponent (only used to build test archives)."""
    size = (n.bit_length() + 7) // 8
    out_size = size - 1
    plain = plain + b"\0" * (-len(plain) % out_size)
    return b"".join(pow(int.from_bytes(plain[i:i + out_size], "big"), d, n).to_bytes(size, "big")
                    for i in range(0, len(plain), out_size))


__all__ = ["DS1", "DS2", "DS3", "ER", "FORMATS", "Entry", "Header", "PublicKey", "hash_bits", "hash_path",
           "is_encrypted", "load_header", "parse_public_key", "read_entry", "read_header", "rsa_decrypt"]
