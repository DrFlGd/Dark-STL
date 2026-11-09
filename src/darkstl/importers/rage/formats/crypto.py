"""The two ciphers GTA V's PC archives use: AES-256 (ECB) and Rockstar's own "NG" cipher.

NG is 17 rounds of table look-ups over 16-byte blocks, with a key chosen per file from 101 by a hash of
the file's name and size. The keys, tables and hash look-up table are in the game's executable; they are
never shipped with the app (see :mod:`.keys`). Layout from CodeWalker's GTACrypto (MIT, (c) 2015
Neodymium); the rounds are worked on all blocks at once with numpy.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ....errors import FormatError

AES_KEY_BYTES = 0x20
NG_KEY_BYTES = 0x110  # 17 round keys of four u32
NG_TABLE_BYTES = 0x400  # 256 u32
LUT_BYTES = 0x100
NG_KEY_COUNT = 101
NG_TABLE_COUNT = 17 * 16

# which input bytes each output word of a round mixes (round A for rounds 0, 1 and 16; B for 2-15)
_ROUND_A = ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10, 11), (12, 13, 14, 15))
_ROUND_B = ((0, 7, 10, 13), (1, 4, 11, 14), (2, 5, 8, 15), (3, 6, 9, 12))


@dataclass
class Keys:
    """Everything the archives need, as found in one executable."""

    aes: bytes | None  # 32 bytes
    ng_keys: np.ndarray | None  # (101, 17, 4) uint32
    ng_tables: np.ndarray | None  # (17, 16, 256) uint32
    lut: bytes | None  # 256 bytes

    @property
    def has_ng(self) -> bool:
        return self.ng_keys is not None and self.ng_tables is not None and self.lut is not None

    def to_bytes(self) -> bytes:
        """All of it in one blob (what the app keeps in its settings folder)."""
        if self.aes is None or not self.has_ng:
            raise FormatError("The archive keys are incomplete")
        return (b"DSK5" + self.aes + self.ng_keys.astype("<u4").tobytes()
                + self.ng_tables.astype("<u4").tobytes() + self.lut)

    @classmethod
    def from_bytes(cls, data: bytes) -> Keys:
        want = 4 + AES_KEY_BYTES + NG_KEY_COUNT * NG_KEY_BYTES + NG_TABLE_COUNT * NG_TABLE_BYTES + LUT_BYTES
        if len(data) != want or data[:4] != b"DSK5":
            raise FormatError("Not a saved set of GTA V archive keys")
        p = 4
        aes = data[p:p + AES_KEY_BYTES]
        p += AES_KEY_BYTES
        keys = np.frombuffer(data, "<u4", NG_KEY_COUNT * NG_KEY_BYTES // 4, p).reshape(NG_KEY_COUNT, 17, 4)
        p += NG_KEY_COUNT * NG_KEY_BYTES
        tables = np.frombuffer(data, "<u4", NG_TABLE_COUNT * 256, p).reshape(17, 16, 256)
        p += NG_TABLE_COUNT * NG_TABLE_BYTES
        return cls(bytes(aes), keys.astype(np.uint32), tables.astype(np.uint32), bytes(data[p:p + LUT_BYTES]))

    @classmethod
    def from_parts(cls, aes: bytes | None, ng_keys: list[bytes] | None, tables: list[bytes] | None,
                   lut: bytes | None) -> Keys:
        k = t = None
        if ng_keys is not None and all(x is not None for x in ng_keys):
            k = np.frombuffer(b"".join(ng_keys), "<u4").reshape(NG_KEY_COUNT, 17, 4).astype(np.uint32)
        if tables is not None and all(x is not None for x in tables):
            # the executable holds them round by round, sixteen tables a round
            t = np.frombuffer(b"".join(tables), "<u4").reshape(17, 16, 256).astype(np.uint32)
        return cls(aes, k, t, lut)

    def name_hash(self, name: str) -> int:
        """The hash that picks a file's NG key (case as stored, which is lower case)."""
        if self.lut is None:
            raise FormatError("No hash table for the archive keys")
        lut = self.lut
        r = 0
        for ch in name.encode("latin-1", "replace"):
            temp = (1025 * (lut[ch] + r)) & 0xFFFFFFFF
            r = ((temp >> 6) ^ temp) & 0xFFFFFFFF
        r9 = (9 * r) & 0xFFFFFFFF
        return (32769 * ((r9 >> 11) ^ r9)) & 0xFFFFFFFF

    def ng_key(self, name: str, length: int) -> np.ndarray:
        if not self.has_ng:
            raise FormatError("No NG keys")
        return self.ng_keys[(self.name_hash(name) + length + 61) % NG_KEY_COUNT]


def decrypt_aes(data: bytes, key: bytes) -> bytes:
    """AES-256 ECB over the whole 16-byte blocks; a short tail stays as it is."""
    n = len(data) - len(data) % 16
    if n <= 0:
        return bytes(data)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(bytes(data[:n])) + dec.finalize() + bytes(data[n:])


def decrypt_ng(data: bytes, key: np.ndarray, tables: np.ndarray) -> bytes:
    """NG decryption with one file's ``key`` ((17, 4) uint32) and the 17 x 16 ``tables``."""
    n = len(data) - len(data) % 16
    if n <= 0:
        return bytes(data)
    blocks = np.frombuffer(bytes(data[:n]), np.uint8).reshape(-1, 16)
    for r in range(17):
        mix = _ROUND_B if 2 <= r <= 15 else _ROUND_A
        tab = tables[r]
        words = np.empty((len(blocks), 4), np.uint32)
        for w, idx in enumerate(mix):
            x = np.full(len(blocks), key[r][w], np.uint32)
            for b in idx:
                x ^= tab[b][blocks[:, b]]
            words[:, w] = x
        blocks = words.astype("<u4").view(np.uint8).reshape(-1, 16)
    return blocks.tobytes() + bytes(data[n:])
