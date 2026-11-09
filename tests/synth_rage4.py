"""Byte-built GTA IV files for the tests: RSC5 resources (drawables, dictionaries, fragments, textures),
IMG3 archives (plain or encrypted with a made-up key), item definitions and text, and a game folder.

Nothing here comes from the game: the archive key is made up here, and the tests put its hash in place of
the published one.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import numpy as np
from synth_rage import Geo, box, solid_bc1

from darkstl.importers.rage.formats import img as imgfmt
from darkstl.importers.rage.formats import rsc5
from darkstl.importers.rage.formats.text import joaat

SYS, GFX = 0x50000000, 0x60000000
DXT1 = 0x31545844


class Res4:
    """A GTA IV resource being built: blocks in the system or graphics part, 4-byte pointers."""

    def __init__(self):
        self.seg = {SYS: bytearray(), GFX: bytearray()}

    def alloc(self, n: int, gfx: bool = False, align: int = 16) -> int:
        base = GFX if gfx else SYS
        s = self.seg[base]
        while len(s) % align:
            s.append(0)
        off = len(s)
        s.extend(b"\0" * max(n, 1))
        return base | off

    def put(self, ptr: int, fmt: str, *vals) -> None:
        struct.pack_into("<" + fmt, self.seg[ptr & 0xF0000000], ptr & 0x0FFFFFFF, *vals)

    def data(self, b: bytes, gfx: bool = False) -> int:
        p = self.alloc(len(b), gfx)
        off = p & 0x0FFFFFFF
        self.seg[p & 0xF0000000][off:off + len(b)] = b
        return p

    def string(self, text: str) -> int:
        return self.data(text.encode("latin-1") + b"\0")

    def ptrs(self, ptrs: list[int]) -> int:
        return self.data(struct.pack(f"<{len(ptrs)}I", *ptrs)) if ptrs else 0

    def collection(self, at: int, ptrs: list[int]) -> None:
        self.put(at, "IHH", self.ptrs(ptrs), len(ptrs), len(ptrs))

    def rsc5(self, kind: int) -> bytes:
        sysb, gfxb = bytes(self.seg[SYS]), bytes(self.seg[GFX])
        flags = rsc5.flags_for(len(sysb), len(gfxb))
        body = sysb.ljust(rsc5.system_size(flags), b"\0") + gfxb.ljust(rsc5.graphics_size(flags), b"\0")
        return struct.pack("<3I", rsc5.MAGIC, kind, flags) + zlib.compress(body, 9)


# --- textures --------------------------------------------------------------------------------------------

def texture_block(w: Res4, name: str, rgb, size: int = 8) -> int:
    p = w.alloc(0x50)
    w.put(p + 0x14, "I", w.string(f"pack:/{name}.dds"))
    w.put(p + 0x1C, "HHIHBB", size, size, DXT1, size * 2, 0, 1)
    w.put(p + 0x48, "I", w.data(solid_bc1(size, rgb), gfx=True))
    return p


def texture_dict(w: Res4, textures: list[tuple[str, tuple]], at: int | None = None) -> int:
    p = at if at is not None else w.alloc(0x20)
    hashes = w.data(struct.pack(f"<{len(textures)}I", *[joaat(n) for n, _ in textures]))
    w.put(p + 0x10, "IHH", hashes, len(textures), len(textures))
    w.collection(p + 0x18, [texture_block(w, n, rgb) for n, rgb in textures])
    return p


def wtd(textures: list[tuple[str, tuple]]) -> bytes:
    w = Res4()
    root = w.alloc(0x20)
    texture_dict(w, textures, root)
    return w.rsc5(rsc5.TEXTURES)


# --- drawables -------------------------------------------------------------------------------------------

def _vertices(g: Geo) -> tuple[bytes, int, int, int]:
    """Position (float3), [weights, indices (bytes)], normal (float3), texcoord (float2)."""
    n = len(g.positions)
    nor = g.positions / np.maximum(np.linalg.norm(g.positions, axis=1, keepdims=True), 1e-6)
    uv = np.stack([g.positions[:, 0] * 0.5 + 0.5, g.positions[:, 2] * 0.5 + 0.5], -1)
    skinned = g.weights is not None
    stride = 12 + (8 if skinned else 0) + 12 + 8
    buf = bytearray(n * stride)
    for i in range(n):
        o = i * stride
        struct.pack_into("<3f", buf, o, *g.positions[i])
        o += 12
        if skinned:
            struct.pack_into("<4B4B", buf, o, *g.weights[i], *g.indices[i])
            o += 8
        struct.pack_into("<3f2f", buf, o, *nor[i], *uv[i])
    flags = 1 | (1 << 3) | (1 << 6) | ((1 << 1) | (1 << 2) if skinned else 0)
    types = 6 | (8 << 4) | (9 << 8) | (6 << 12) | (5 << 24)
    return bytes(buf), stride, flags, types


def geometry_block(w: Res4, g: Geo) -> int:
    data, stride, flags, types = _vertices(g)
    decl = w.alloc(16)
    w.put(decl, "IHBBQ", flags, stride, 0, 0, types)
    vb = w.alloc(0x20)
    w.put(vb + 0x04, "H", len(g.positions))
    w.put(vb + 0x08, "II", w.data(data, gfx=True), stride)
    w.put(vb + 0x10, "I", decl)
    ib = w.alloc(0x10)
    w.put(ib + 0x04, "II", g.triangles.size, w.data(g.triangles.astype("<u2").tobytes(), gfx=True))
    p = w.alloc(0x50)
    w.put(p + 0x0C, "I", vb)
    w.put(p + 0x1C, "I", ib)
    w.put(p + 0x2C, "IIHH", g.triangles.size, len(g.triangles), len(g.positions), 3)
    w.put(p + 0x3C, "H", stride)
    return p


def model_block(w: Res4, geoms: list[Geo], bone: int, skinned: bool) -> int:
    p = w.alloc(0x20)
    w.collection(p + 0x04, [geometry_block(w, g) for g in geoms])
    w.put(p + 0x10, "I", w.data(struct.pack(f"<{len(geoms)}H", *[g.shader for g in geoms])))
    w.put(p + 0x14, "4B", 0, 1 if skinned else 0, 0, bone)
    return p


def shader_block(w: Res4, textures: list[tuple[str, str]]) -> int:
    params = []
    for _param, tname in textures:
        tp = w.alloc(0x20)
        w.put(tp + 0x14, "I", w.string(tname))
        params.append(tp)
    p = w.alloc(0x60)
    n = len(textures)
    w.put(p + 0x14, "I", w.ptrs(params))
    w.put(p + 0x1C, "I", n)
    w.put(p + 0x24, "I", w.data(bytes(n)))  # all textures
    w.put(p + 0x28, "I", joaat("gta_default"))
    w.put(p + 0x34, "I", w.data(struct.pack(f"<{n}I", *[joaat(pn) for pn, _ in textures])))
    w.put(p + 0x44, "II", w.string("gta_default"), w.string("gta_default.sps"))
    return p


def skeleton_block(w: Res4, bones: list[dict]) -> int:
    first = w.alloc(0xE0 * len(bones))
    for i, b in enumerate(bones):
        p = first + 0xE0 * i
        parent = b.get("parent", -1)
        w.put(p, "I", w.string(b["name"]))
        w.put(p + 0x10, "I", first + 0xE0 * parent if parent >= 0 else 0)
        w.put(p + 0x14, "hH", i, b["tag"])
        w.put(p + 0x20, "4f", *b.get("translation", (0, 0, 0)), 0)
        w.put(p + 0x40, "4f", *b.get("rotation", (0, 0, 0, 1)))
    s = w.alloc(0x40)
    w.put(s, "I", first)
    w.put(s + 0x14, "H", len(bones))
    return s


def drawable_block(w: Res4, models, shaders, bones=None, own_textures=None, at=None) -> int:
    p = at if at is not None else w.alloc(0x90)
    sg = w.alloc(0x40)
    if own_textures:
        w.put(sg + 0x04, "I", texture_dict(w, own_textures))
    w.collection(sg + 0x08, [shader_block(w, t) for t in shaders])
    w.put(p + 0x08, "I", sg if shaders else 0)
    if bones:
        w.put(p + 0x0C, "I", skeleton_block(w, bones))
    if models:
        lod = w.alloc(8)
        w.collection(lod, [model_block(w, g, b, s) for g, b, s in models])
        w.put(p + 0x40, "I", lod)
    return p


def wdr(models, shaders, own_textures=None) -> bytes:
    w = Res4()
    root = w.alloc(0x90)
    drawable_block(w, models, shaders, None, own_textures, root)
    return w.rsc5(rsc5.MODEL)


def wdd(entries: list[tuple[str, list, list]]) -> bytes:
    w = Res4()
    root = w.alloc(0x20)
    hashes = w.data(struct.pack(f"<{len(entries)}I", *[joaat(n) for n, _, _ in entries]))
    w.put(root + 0x10, "IHH", hashes, len(entries), len(entries))
    w.collection(root + 0x18, [drawable_block(w, m, s) for _, m, s in entries])
    return w.rsc5(rsc5.MODEL)


def wft(models, shaders, bones, children: list[list] = ()) -> bytes:
    """A fragment: main drawable, and children (models, each placed by its model's bone)."""
    w = Res4()
    root = w.alloc(0x200)
    w.put(root + 0xB4, "I", drawable_block(w, models, shaders, bones))
    kids = []
    for cmodels in children:
        c = w.alloc(0xA0)
        w.put(c + 0x90, "I", drawable_block(w, cmodels, []))
        kids.append(c)
    if kids:
        w.put(root + 0xD4, "I", w.ptrs(kids))
        w.put(root + 0x1F3, "B", len(kids))
    return w.rsc5(rsc5.FRAGMENT)


# --- archives, lists, text ------------------------------------------------------------------------

def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    n = len(data) - len(data) % 16
    body = data[:n]
    for _ in range(16):
        e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        body = e.update(body) + e.finalize()
    return body + data[n:]


def img(files: dict[str, bytes], key: bytes | None = None) -> bytes:
    """An IMG3 archive (resources are told by their RSC5 header), its table encrypted with ``key``."""
    names = list(files)
    blob = b"".join(n.encode("latin-1") + b"\0" for n in names)
    toc_size = 16 * len(names) + len(blob)
    first = (20 + toc_size + imgfmt.BLOCK - 1) // imgfmt.BLOCK
    table, body, block = bytearray(), bytearray(), first
    for n in names:
        data = files[n]
        used = (len(data) + imgfmt.BLOCK - 1) // imgfmt.BLOCK
        pad = used * imgfmt.BLOCK - len(data)
        if rsc5.is_resource_file(data):
            kind, flags = struct.unpack_from("<II", data, 4)
            table += struct.pack("<IIIHH", flags | 0x40000000, kind, block, used, pad | 0x2000)
        else:
            table += struct.pack("<IIIHH", len(data), 0, block, used, pad)
        body += data + b"\0" * pad
        block += used
    toc = bytes(table) + blob
    head = struct.pack("<IIIIHH", imgfmt.MAGIC, 3, len(names), toc_size, 16, 0)
    if key is not None:
        head = encrypt(head[:16], key) + head[16:]
        toc = encrypt(toc, key)
    out = head + toc
    return out + b"\0" * (first * imgfmt.BLOCK - len(out)) + bytes(body)


def gxt(entries: dict[str, str]) -> bytes:
    """A version 4 text file (one ``MAIN`` table, keys by hash, UTF-16 strings)."""
    items = sorted((joaat(k), v) for k, v in entries.items())
    tdat, keys = bytearray(), bytearray()
    for h, v in items:
        keys += struct.pack("<II", len(tdat), h)
        tdat += v.encode("utf-16-le") + b"\0\0"
    main_at = 4 + 8 + 12
    tabl = b"TABL" + struct.pack("<I", 12) + b"MAIN\0\0\0\0" + struct.pack("<I", main_at)
    return (struct.pack("<HH", 4, 16) + tabl + b"TKEY" + struct.pack("<I", len(keys)) + bytes(keys)
            + b"TDAT" + struct.pack("<I", len(tdat)) + bytes(tdat))


def ide(cars=(), peds=(), parents=()) -> bytes:
    out = ["# made up"]
    if cars:
        out += ["cars"] + [f"{m}, {m}, {t}, {m.upper()}, {k}, VEH@LOW, null, 100, 999, 0.25, 0.25, 0, 0, 3, 0, 0"
                           for m, t, k in cars] + ["end"]
    if peds:
        out += ["peds"] + [f"{m}, null, {t}, STAT_GANG_MALE, null" for m, t in peds] + ["end"]
    if parents:
        out += ["txdp"] + [f"{c}, {p}" for c, p in parents] + ["end"]
    return ("\n".join(out) + "\n").encode()


# --- a game ------------------------------------------------------------------------------------------------

KEY = bytes(range(7, 39))  # made up
CAR_BONES = [
    {"name": "chassis", "tag": 0, "parent": -1},
    {"name": "wheel_lf", "tag": 101, "parent": 0, "translation": (-0.8, 1.3, 0.3)},
    {"name": "wheel_rf", "tag": 102, "parent": 0, "translation": (0.8, 1.3, 0.3)},
    {"name": "wheel_lr", "tag": 103, "parent": 0, "translation": (-0.8, -1.3, 0.3)},
    {"name": "wheel_rr", "tag": 104, "parent": 0, "translation": (0.8, -1.3, 0.3)},
    {"name": "door_dside_f", "tag": 105, "parent": 0, "translation": (-0.9, 0.3, 0.6)},
]
PED_BONES = [
    {"name": "Root", "tag": 0, "parent": -1},
    {"name": "Pelvis", "tag": 11816, "parent": 0, "translation": (0, 0, 1.0)},
    {"name": "Head", "tag": 31086, "parent": 1, "translation": (0, 0, 0.8)},
]


def _skinned(v, f, bone: int) -> Geo:
    n = len(v)
    weights = np.zeros((n, 4), np.uint8)
    weights[:, 0] = 255
    idx = np.zeros((n, 4), np.uint8)
    idx[:, 0] = bone
    return Geo(v, f, 0, weights, idx)


def car_wft() -> bytes:
    body = box((1.8, 4.2, 1.0), (0, 0, 0.8))
    door = box((0.1, 1.0, 0.6))
    wheel = box((0.3, 0.6, 0.6))
    return wft([([Geo(*body)], 0, False), ([Geo(*door)], 5, False)],
               [[("TextureSampler", "admiral_body"), ("BumpSampler", "admiral_body_n")],
                [("TextureSampler", "vehicle_tyre")]],
               CAR_BONES, [[([Geo(*wheel, shader=1)], 1, False)]])


def ped_wdd() -> bytes:
    head = box((0.3, 0.2, 0.3), (0, 0, 1.9))
    top = box((0.6, 0.4, 0.6), (0, 0, 1.4))
    legs = box((0.5, 0.3, 0.9), (0, 0, 0.6))
    return wdd([
        ("head_000_r", [([_skinned(*head, 2)], 0, True)], [[("TextureSampler", "head_diff_000_a_whi")]]),
        ("uppr_000_u", [([_skinned(*top, 1)], 0, True)], [[("TextureSampler", "uppr_diff_000_a_uni")]]),
        ("lowr_000_u", [([_skinned(*legs, 1)], 0, True)], [[("TextureSampler", "lowr_diff_000_a_uni")]]),
    ])


def ped_wft() -> bytes:
    return wft([], [], PED_BONES)


def gta4_game(root: Path, key: bytes = KEY) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    exe = bytearray(b"MZ" + b"\x90" * 1000)
    exe += b"\x12" * 2 + key + b"\x34" * 500  # at a 4-byte boundary that isn't a 32-byte one
    (root / "GTAIV.exe").write_bytes(bytes(exe))
    cd = root / "pc/models/cdimages"
    cd.mkdir(parents=True)
    (cd / "vehicles.img").write_bytes(img({
        "admiral.wft": car_wft(),
        "admiral.wtd": wtd([("admiral_body", (20, 60, 200)), ("admiral_body_n", (128, 128, 255))]),
        "vehshare.wtd": wtd([("vehicle_tyre", (30, 30, 30))]),
    }, key))
    (cd / "componentpeds.img").write_bytes(img({
        "m_y_test_01.wdd": ped_wdd(),
        "m_y_test_01.wft": ped_wft(),
        "m_y_test_01.wtd": wtd([("head_diff_000_a_whi", (220, 180, 150)), ("uppr_diff_000_a_uni", (200, 40, 40)),
                                ("lowr_diff_000_a_uni", (40, 40, 40))]),
    }, key))
    (cd / "weapons.img").write_bytes(img({
        "w_glock.wdr": wdr([([Geo(*box((0.04, 0.2, 0.12)))], 0, False)], [[("TextureSampler", "w_glock")]]),
        "w_glock.wtd": wtd([("w_glock", (60, 60, 60))]),
    }, key))
    props = root / "pc/data/maps/props/industrial"
    props.mkdir(parents=True)
    (props / "ind_props.img").write_bytes(img({
        "cj_crate_01.wdr": wdr([([Geo(*box((1, 1, 1)))], 0, False)], [[("TextureSampler", "crate")]],
                               own_textures=[("crate", (120, 90, 30))]),
    }))
    data = root / "common/data"
    data.mkdir(parents=True)
    (data / "vehicles.ide").write_bytes(ide(cars=[("admiral", "car", "ADMIRAL")]))
    (data / "peds.ide").write_bytes(ide(peds=[("m_y_test_01", "CIVMALE")]))
    (data / "gtxd.ide").write_bytes(ide(parents=[("admiral", "vehshare")]))
    text = root / "pc/text"
    text.mkdir(parents=True)
    (text / "american.gxt").write_bytes(gxt({"ADMIRAL": "Admiral", "HEXER": "Hexer"}))
    # The Lost and Damned: a bike of its own
    ep = root / "TLAD/pc/models/cdimages"
    ep.mkdir(parents=True)
    (ep / "vehicles.img").write_bytes(img({
        "hexer.wft": wft([([Geo(*box((0.5, 2.0, 1.0), (0, 0, 0.7)))], 0, False)],
                         [[("TextureSampler", "hexer_body")]], CAR_BONES[:1]),
        "hexer.wtd": wtd([("hexer_body", (10, 10, 10))]),
    }, key))
    (root / "TLAD/common/data").mkdir(parents=True)
    (root / "TLAD/common/data/vehicles.ide").write_bytes(ide(cars=[("hexer", "bike", "HEXER")]))
    return root


def install_fake_hash(monkeypatch, key: bytes = KEY) -> None:
    from darkstl.importers.rage.formats import keys4

    monkeypatch.setattr(keys4, "KEY_HASH", hashlib.sha1(key).digest())
