"""Byte-built GTA V files for the tests: resources (drawables, dictionaries, fragments, textures, clips),
RPF7 archives (plain or NG encrypted with made-up tables), text and game lists, and a whole game folder.

Nothing here comes from the game: the NG tables are byte permutations made up here (so they can be
inverted to encrypt), and the "executable" holds made-up keys whose hashes the tests put in place of the
published ones.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import numpy as np

from darkstl.importers.rage.formats import rpf as rpffmt
from darkstl.importers.rage.formats.rsc import MAGIC as RSC7
from darkstl.importers.rage.formats.rsc import flags_for
from darkstl.importers.rage.formats.text import joaat

SYS, GFX = 0x50000000, 0x60000000


class ResWriter:
    """Builds a resource: blocks allocated in the system or graphics part, filled in afterwards."""

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
        base = ptr & 0xF0000000
        struct.pack_into("<" + fmt, self.seg[base], ptr & 0x0FFFFFFF, *vals)

    def data(self, b: bytes, gfx: bool = False) -> int:
        p = self.alloc(len(b), gfx)
        s = self.seg[p & 0xF0000000]
        off = p & 0x0FFFFFFF
        s[off:off + len(b)] = b
        return p

    def string(self, text: str) -> int:
        return self.data(text.encode("utf-8") + b"\0")

    def ptrs(self, ptrs: list[int]) -> int:
        return self.data(struct.pack(f"<{len(ptrs)}Q", *ptrs)) if ptrs else 0

    def parts(self) -> tuple[bytes, int, int]:
        sysb, gfxb = bytes(self.seg[SYS]), bytes(self.seg[GFX])
        sf, gf = flags_for(len(sysb)), flags_for(len(gfxb))
        from darkstl.importers.rage.formats.rsc import size_from_flags

        body = sysb.ljust(size_from_flags(sf), b"\0") + gfxb.ljust(size_from_flags(gf), b"\0")
        return body, sf, gf

    def rsc7(self, version: int) -> bytes:
        """The resource as a file (``RSC7`` header, version in the flags as an archive keeps it)."""
        body, sf, gf = self.parts()
        sf |= (version >> 4 & 0xF) << 28
        gf |= (version & 0xF) << 28
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        return struct.pack("<4I", RSC7, version, sf, gf) + comp.compress(body) + comp.flush()


# --- textures --------------------------------------------------------------------------------------------

def bc1_block(rgb: tuple[int, int, int]) -> bytes:
    r, g, b = rgb
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack("<HHI", c, c, 0)


def solid_bc1(size: int, rgb) -> bytes:
    blocks = max(1, size // 4) ** 2
    return bc1_block(rgb) * blocks


def texture_block(w: ResWriter, name: str, size: int = 8, rgb=(200, 40, 40), gen9: bool = False,
                  with_data: bool = True) -> int:
    """A BC1 texture (``with_data``) or a texture that only names one (what shaders point at)."""
    if gen9:
        p = w.alloc(0x80)
        w.put(p + 0x28, "Q", w.string(name))
        if with_data:
            data = w.data(solid_bc1(size, rgb), gfx=True)
            w.put(p + 0x18, "HHH", size, size, 1)
            w.put(p + 0x1F, "B", 71)  # BC1_UNORM
            w.put(p + 0x22, "B", 1)
            w.put(p + 0x38, "Q", data)
        return p
    p = w.alloc(0x90 if with_data else 0x50)
    w.put(p + 0x28, "Q", w.string(name))
    if with_data:
        data = w.data(solid_bc1(size, rgb), gfx=True)
        w.put(p + 0x50, "HHHHI", size, size, 1, size * 2, 0x31545844)  # DXT1
        w.put(p + 0x5D, "B", 1)
        w.put(p + 0x70, "Q", data)
    return p


def texture_dict(w: ResWriter, textures: list[tuple[str, tuple]], gen9: bool = False, at: int | None = None) -> int:
    p = at if at is not None else w.alloc(0x40)
    ptrs = [texture_block(w, n, 8, rgb, gen9) for n, rgb in textures]
    hashes = w.data(struct.pack(f"<{len(textures)}I", *[joaat(n) for n, _ in textures]))
    w.put(p + 0x20, "QHH", hashes, len(textures), len(textures))
    w.put(p + 0x30, "QHH", w.ptrs(ptrs), len(textures), len(textures))
    return p


def ytd(textures: list[tuple[str, tuple]], gen9: bool = False) -> bytes:
    w = ResWriter()
    root = w.alloc(0x40)
    texture_dict(w, textures, gen9, root)
    return w.rsc7(5 if gen9 else 13)


# --- drawables -------------------------------------------------------------------------------------------

def box(size=(1.0, 1.0, 1.0), centre=(0.0, 0.0, 0.0)):
    sx, sy, sz = (s / 2 for s in size)
    cx, cy, cz = centre
    v = np.array([[x, y, z] for x in (-sx, sx) for y in (-sy, sy) for z in (-sz, sz)], np.float32) + (cx, cy, cz)
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], np.int64)
    return v, f


class Geo:
    def __init__(self, positions, triangles, shader=0, weights=None, indices=None, bone_ids=None):
        self.positions = np.asarray(positions, np.float32)
        self.triangles = np.asarray(triangles, np.int64)
        self.shader = shader
        self.weights = weights  # (N, 4) uint8
        self.indices = indices  # (N, 4) uint8
        self.bone_ids = bone_ids or []


def _vertices(g: Geo, gen9: bool):
    n = len(g.positions)
    nor = g.positions / np.maximum(np.linalg.norm(g.positions, axis=1, keepdims=True), 1e-6)
    uv = np.stack([g.positions[:, 0] * 0.5 + 0.5, g.positions[:, 2] * 0.5 + 0.5], -1).astype(np.float32)
    skinned = g.weights is not None
    if gen9:
        stride = 40 if skinned else 32
        buf = bytearray(n * stride)
        for i in range(n):
            o = i * stride
            struct.pack_into("<3f3f2f", buf, o, *g.positions[i], *nor[i], *uv[i])
            if skinned:
                struct.pack_into("<4B4B", buf, o + 32, *g.weights[i], *g.indices[i])
        return bytes(buf), stride
    # original layout, by semantic order: position, (weights, indices), normal, texcoord 0
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
    return bytes(buf), stride


def _decl(w: ResWriter, skinned: bool, stride: int, gen9: bool) -> int:
    if gen9:
        p = w.alloc(320)
        offs = [0] * 52
        sizes = [0] * 52
        types = [0] * 52
        for idx, off, t in [(0, 0, 6), (4, 12, 6), (28, 24, 16)] + ([(16, 32, 28), (20, 36, 30)] if skinned else []):
            offs[idx], sizes[idx], types[idx] = off, stride, t
        w.put(p, "52I", *offs)
        w.put(p + 208, "52B", *sizes)
        w.put(p + 260, "52B", *types)
        return p
    flags = 1 | (1 << 3) | (1 << 6) | ((1 << 1) | (1 << 2) if skinned else 0)
    types = 0x7755555555996996
    p = w.alloc(16)
    w.put(p, "IHBBQ", flags, stride, 0, bin(flags).count("1"), types)
    return p


def geometry_block(w: ResWriter, g: Geo, gen9: bool) -> int:
    data, stride = _vertices(g, gen9)
    vdata = w.data(data, gfx=True)
    idx = w.data(g.triangles.astype("<u2").tobytes(), gfx=True)
    decl = _decl(w, g.weights is not None, stride, gen9)
    if gen9:
        vb = w.alloc(0x40)
        w.put(vb + 0x8, "IH", len(g.positions), stride)
        w.put(vb + 0x18, "Q", vdata)
        w.put(vb + 0x38, "Q", decl)
        ib = w.alloc(0x40)
        w.put(ib + 0x8, "IH", g.triangles.size, 2)
        w.put(ib + 0x18, "Q", idx)
    else:
        vb = w.alloc(0x80)
        w.put(vb + 0x8, "H", stride)
        w.put(vb + 0x10, "QI", vdata, len(g.positions))
        w.put(vb + 0x30, "Q", decl)
        ib = w.alloc(0x60)
        w.put(ib + 0x8, "I", g.triangles.size)
        w.put(ib + 0x10, "Q", idx)
    p = w.alloc(0x98)
    w.put(p + 0x18, "Q", vb)
    w.put(p + 0x38, "Q", ib)
    w.put(p + 0x58, "IIH", g.triangles.size, len(g.triangles), len(g.positions))
    if g.bone_ids:
        w.put(p + 0x68, "Q", w.data(struct.pack(f"<{len(g.bone_ids)}H", *g.bone_ids)))
        w.put(p + 0x72, "H", len(g.bone_ids))
    w.put(p + 0x70, "H", stride)
    return p


def model_block(w: ResWriter, geoms: list[Geo], bone: int, skinned: bool, gen9: bool) -> int:
    gps = [geometry_block(w, g, gen9) for g in geoms]
    p = w.alloc(0x30)
    w.put(p + 0x8, "QHH", w.ptrs(gps), len(gps), len(gps))
    w.put(p + 0x20, "Q", w.data(struct.pack(f"<{len(geoms)}H", *[g.shader for g in geoms])))
    w.put(p + 0x28, "I", (bone << 24) | ((1 if skinned else 0) << 8))
    return p


def shader_block(w: ResWriter, textures: list[tuple[str, str]], gen9: bool, embedded: set[str] = frozenset()) -> int:
    """A shader naming ``textures`` [(parameter, texture name)]; ``embedded`` ones carry their pixels."""
    if gen9:
        p = w.alloc(0x40)
        w.put(p, "II", joaat("default"), 0)
        tptrs = [texture_block(w, t, gen9=True, with_data=t in embedded) for _, t in textures]
        refs = w.ptrs(tptrs) if tptrs else 0
        infos = w.alloc(8 + 8 * len(textures))
        w.put(infos, "8B", 0, len(textures), 0, 0, len(textures), 0, 0, 12)
        for k, (param, _t) in enumerate(textures):
            w.put(infos + 8 + 8 * k, "II", joaat(param), (k << 2) | 0)
        w.put(p + 0x10, "Q", refs)
        w.put(p + 0x20, "Q", infos)
        return p
    p = w.alloc(0x30)
    count = len(textures)
    params = w.alloc(16 * count + 4 * count)
    for k, (param, t) in enumerate(textures):
        tp = texture_block(w, t, with_data=t in embedded)
        w.put(params + 16 * k, "BBHIQ", 0, 0, 0, 0, tp)
        w.put(params + 16 * count + 4 * k, "I", joaat(param))
    w.put(p, "QII", params, joaat("default"), 0)
    w.put(p + 0x10, "B", count)
    w.put(p + 0x18, "I", joaat("default.sps"))
    return p


def skeleton_block(w: ResWriter, bones: list[dict]) -> int:
    """``bones``: dicts of name, tag, parent, translation, rotation (x, y, z, w)."""
    head = w.alloc(16 + 80 * len(bones))
    first = head + 16
    for i, b in enumerate(bones):
        p = first + 80 * i
        w.put(p, "4f", *b.get("rotation", (0, 0, 0, 1)))
        w.put(p + 0x10, "3f", *b.get("translation", (0, 0, 0)))
        w.put(p + 0x20, "3f", 1, 1, 1)
        w.put(p + 0x32, "h", b.get("parent", -1))
        w.put(p + 0x38, "Q", w.string(b["name"]))
        w.put(p + 0x42, "hH", i, b["tag"])
    s = w.alloc(0x70)
    w.put(s + 0x20, "Q", first)
    w.put(s + 0x5E, "H", len(bones))
    return s


def drawable_block(w: ResWriter, models: list[tuple[list[Geo], int, bool]], shaders: list[list[tuple[str, str]]],
                   bones: list[dict] | None = None, name: str = "", gen9: bool = False,
                   own_textures: list[tuple[str, tuple]] | None = None, at: int | None = None,
                   frag: bool = False, embedded: set[str] = frozenset()) -> int:
    p = at if at is not None else w.alloc(0x150 if frag else 0xD0)
    sg = w.alloc(0x40)
    sh = [shader_block(w, t, gen9, embedded) for t in shaders]
    td = texture_dict(w, own_textures, gen9) if own_textures else 0
    w.put(sg + 0x8, "QQHH", td, w.ptrs(sh), len(sh), len(sh))
    w.put(p + 0x10, "Q", sg)
    if bones:
        w.put(p + 0x18, "Q", skeleton_block(w, bones))
    mps = [model_block(w, g, b, s, gen9) for g, b, s in models]
    lod = w.alloc(16)
    w.put(lod, "QHH", w.ptrs(mps), len(mps), len(mps))
    w.put(p + 0x50, "Q", lod)
    if name:
        w.put(p + (0x130 if frag else 0xA8), "Q", w.string(name))
    return p


def ydr(models, shaders, bones=None, name="", gen9=False, own_textures=None, embedded=frozenset()) -> bytes:
    w = ResWriter()
    root = w.alloc(0xD0)
    drawable_block(w, models, shaders, bones, name, gen9, own_textures, root, embedded=embedded)
    return w.rsc7(159 if gen9 else 165)


def ydd(entries: list[tuple[str, list, list]], gen9: bool = False) -> bytes:
    """``entries``: (name, models, shaders) for each drawable (no names stored: found by hash)."""
    w = ResWriter()
    root = w.alloc(0x40)
    ptrs = [drawable_block(w, models, shaders, None, "", gen9) for _, models, shaders in entries]
    hashes = w.data(struct.pack(f"<{len(entries)}I", *[joaat(n) for n, _, _ in entries]))
    w.put(root + 0x20, "QHH", hashes, len(entries), len(entries))
    w.put(root + 0x30, "QHH", w.ptrs(ptrs), len(entries), len(entries))
    return w.rsc7(159 if gen9 else 165)


def yft(name: str, models, shaders, bones, children: list[tuple[int, list | None, list | None]] = (),
        gen9: bool = False, own_textures=None) -> bytes:
    """A fragment: main drawable, and children (bone tag, models or None for an empty drawable, shaders)."""
    w = ResWriter()
    root = w.alloc(0x140)
    main = drawable_block(w, models, shaders, bones, name, gen9, own_textures, frag=True)
    w.put(root + 0x30, "Q", main)
    w.put(root + 0x58, "Q", w.string(name))
    if children:
        kids = []
        for tag, cmodels, cshaders in children:
            c = w.alloc(0x100)
            w.put(c + 0x12, "H", tag)
            if cmodels is not None:
                d = drawable_block(w, cmodels, cshaders, None, "", gen9, frag=True)
            else:
                d = drawable_block(w, [], [], None, "", gen9, frag=True)
            w.put(c + 0xA0, "Q", d)
            kids.append(c)
        lod = w.alloc(0x130)
        w.put(lod + 0xD0, "Q", w.ptrs(kids))
        w.put(lod + 0x11D, "B", len(kids))
        group = w.alloc(0x30)
        w.put(group + 0x10, "Q", lod)
        w.put(root + 0xF0, "Q", group)
    return w.rsc7(171 if gen9 else 162)


# --- clips -------------------------------------------------------------------------------------------------

def _sequence(tracks: list[list[tuple]], frames: int) -> bytes:
    """Sequence data. Each track is a list of channels: ("squat", (x, y, z)), ("svec", (x, y, z)),
    ("sfloat", v) or ("quant", [values per frame]) with the channel's index in the track."""
    by_type: dict[int, list] = {i: [] for i in range(9)}
    for s, chans in enumerate(tracks):
        for idx, ch in enumerate(chans):
            kind = {"squat": 0, "svec": 1, "sfloat": 2, "quant": 4}[ch[0]]
            by_type[kind].append((s, idx, ch))
    header = bytearray()
    frame_chans = []  # (bits, values as ints)
    for kind in range(9):
        for s, idx, ch in by_type[kind]:
            if kind == 0 or kind == 1:
                header += struct.pack("<3f", *ch[1])
            elif kind == 2:
                header += struct.pack("<f", ch[1])
            elif kind == 4:
                vals = np.asarray(ch[1], float)
                lo, hi = float(vals.min()), float(vals.max())
                bits = 12
                quant = (hi - lo) / ((1 << bits) - 1) if hi > lo else 1.0
                header += struct.pack("<iff", bits, quant, lo)
                frame_chans.append((bits, np.rint((vals - lo) / quant).astype(np.int64)))
    total_bits = sum(b for b, _ in frame_chans)
    frame_len = (total_bits + 7) // 8
    while len(header) % 4:
        header.append(0)
    frame_offset = len(header)
    frame_data = bytearray(frame_len * frames)
    for f in range(frames):
        acc, pos = 0, 0
        for bits, vals in frame_chans:
            acc |= int(vals[f]) << pos
            pos += bits
        frame_data[f * frame_len:(f + 1) * frame_len] = acc.to_bytes(frame_len, "little") if frame_len else b""
    counts = struct.pack("<9H", *[len(by_type[k]) for k in range(9)])
    dbits = bytearray()
    for kind in range(9):
        items = by_type[kind]
        for s, idx, _ch in items:
            dbits += struct.pack("<H", (s << 2) | idx)
        rem = len(items) % 4
        if rem:
            dbits += b"\0\0" * (4 - rem)
    data = bytes(header) + bytes(frame_data) + counts + bytes(dbits)
    head = struct.pack("<5IHHHHHBB", 0, len(data), 0, frame_offset, 32 + len(data), 0, frames, frame_len, 0, 0,
                       255, 0)
    return head + data


def ycd(clips: list[tuple[str, int, list[tuple[int, int, list[tuple]]]]]) -> bytes:
    """``clips``: (name, frames, [(bone tag, track, channels)]) for each, one animation each."""
    w = ResWriter()
    root = w.alloc(0x40)
    entries = []
    for name, frames, tracks in clips:
        seq = w.data(_sequence([t[2] for t in tracks], frames))
        anim = w.alloc(0x60)
        w.put(anim + 0x14, "HHf", frames, frames, (frames - 1) / 30.0)
        w.put(anim + 0x40, "QHH", w.ptrs([seq]), 1, 1)
        ids = w.data(b"".join(struct.pack("<HBB", tag, 0, track) for tag, track, _ in tracks))
        w.put(anim + 0x50, "QHH", ids, len(tracks), len(tracks))
        clip = w.alloc(0x70)
        w.put(clip + 0x10, "I", 1)
        w.put(clip + 0x18, "Q", w.string(f"pack:/{name}.clip"))
        w.put(clip + 0x50, "Qfff", anim, 0.0, (frames - 1) / 30.0, 1.0)
        e = w.alloc(0x20)
        w.put(e, "IIQQ", joaat(name), 0, clip, 0)
        entries.append(e)
    w.put(root + 0x28, "QHH", w.ptrs(entries), len(entries), len(entries))
    return w.rsc7(46)


def turn_z(frames: int, degrees: float) -> list[tuple]:
    """Channels for a rotation about z from 0 to ``degrees`` over the frames."""
    a = np.radians(np.linspace(0, degrees, frames))
    return [("sfloat", 0.0), ("sfloat", 0.0), ("quant", np.sin(a / 2)), ("quant", np.cos(a / 2))]


# --- text and lists ------------------------------------------------------------------------------------

def gxt2(entries: dict[str, str]) -> bytes:
    items = sorted((joaat(k), v) for k, v in entries.items())
    head = 8 + 8 * len(items) + 8
    strings = bytearray()
    offs = []
    for _h, v in items:
        offs.append(head + len(strings))
        strings += v.encode("utf-8") + b"\0"
    out = struct.pack("<II", 0x47585432, len(items))
    out += b"".join(struct.pack("<II", h, o) for (h, _), o in zip(items, offs))
    out += struct.pack("<II", 0x47585432, head + len(strings))
    return out + bytes(strings)


def vehicles_meta(items: list[dict]) -> bytes:
    rows = "".join(
        f"<Item><modelName>{v['model']}</modelName><txdName>{v.get('txd', v['model'])}</txdName>"
        f"<handlingId>{v['model'].upper()}</handlingId><gameName>{v['label']}</gameName>"
        f"<vehicleMakeName>{v.get('maker', '')}</vehicleMakeName><type>{v.get('type', 'VEHICLE_TYPE_CAR')}</type>"
        f"<vehicleClass>{v.get('class', 'VC_SUPER')}</vehicleClass></Item>" for v in items)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<CVehicleModelInfo__InitDataList><InitDatas>{rows}' \
           f'</InitDatas></CVehicleModelInfo__InitDataList>'.encode()


def peds_meta(items: list[tuple[str, str]]) -> bytes:
    rows = "".join(f"<Item><Name>{n}</Name><Pedtype>{t}</Pedtype></Item>" for n, t in items)
    return f"<CPedModelInfo__InitDataList><InitDatas>{rows}</InitDatas></CPedModelInfo__InitDataList>".encode()


def weapons_meta(items: list[tuple[str, str, str, str]]) -> bytes:
    rows = "".join(f'<Item type="CWeaponInfo"><Name>{n}</Name><Model>{m}</Model><HumanNameHash>{l}</HumanNameHash>'
                   f"<Group>{g}</Group></Item>" for n, m, l, g in items)
    return f"<CWeaponInfoBlob><Infos><Item><Infos>{rows}</Infos></Item></Infos></CWeaponInfoBlob>".encode()


# --- archives and keys -----------------------------------------------------------------------------------

class FakeKeys:
    """Made-up archive keys: NG tables that are byte permutations (so encryption is their inverse)."""

    def __init__(self, seed: int = 5):
        rng = np.random.default_rng(seed)
        self.aes = bytes(rng.integers(0, 256, 32, dtype=np.uint8))
        self.lut = bytes(rng.permutation(256).astype(np.uint8))
        self.ng_keys = rng.integers(0, 2 ** 32, (101, 17, 4), dtype=np.uint64).astype(np.uint32)
        self.perms = np.stack([rng.permutation(256) for _ in range(17 * 16)]).reshape(17, 16, 256)
        self.inv = np.argsort(self.perms, axis=2)
        shifts = (np.arange(16) % 4 * 8).astype(np.uint32)
        self.tables = (self.perms.astype(np.uint32) << shifts[None, :, None]).astype(np.uint32)

    def exe(self) -> bytes:
        """An "executable" holding the keys at 8-byte-aligned places, the NG keys and tables in runs."""
        parts = [b"MZ" + b"\0" * 62, b"\x11" * 200, self.aes, b"\x22" * 56,
                 self.ng_keys.astype("<u4").tobytes(), b"\x33" * 8,
                 self.tables.astype("<u4").tobytes(), b"\x44" * 16, self.lut, b"\x55" * 64]
        out = bytearray()
        for p in parts:
            while len(out) % 8:
                out.append(0)
            out += p
        return bytes(out)

    def hashes(self) -> dict:
        return {"AES_KEY": hashlib.sha1(self.aes).digest(), "LUT": hashlib.sha1(self.lut).digest(),
                "NG_KEYS": tuple(hashlib.sha1(k.astype("<u4").tobytes()).digest() for k in self.ng_keys),
                "NG_TABLES": tuple(hashlib.sha1(t.astype("<u4").tobytes()).digest()
                                   for t in self.tables.reshape(272, 256))}

    def real(self):
        from darkstl.importers.rage.formats.crypto import Keys

        return Keys(self.aes, self.ng_keys.copy(), self.tables.copy(), self.lut)

    def encrypt_ng(self, data: bytes, name: str, length: int) -> bytes:
        keys = self.real()
        key = keys.ng_key(name, length)
        n = len(data) - len(data) % 16
        blocks = np.frombuffer(data[:n], np.uint8).reshape(-1, 16).copy()
        mix_a = ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9, 10, 11), (12, 13, 14, 15))
        mix_b = ((0, 7, 10, 13), (1, 4, 11, 14), (2, 5, 8, 15), (3, 6, 9, 12))
        for r in reversed(range(17)):
            mix = mix_b if 2 <= r <= 15 else mix_a
            kb = np.frombuffer(key[r].astype("<u4").tobytes(), np.uint8)
            prev = np.empty_like(blocks)
            for wd, idx in enumerate(mix):
                for j in idx:
                    out_byte = 4 * wd + j % 4
                    prev[:, j] = self.inv[r, j][blocks[:, out_byte] ^ kb[out_byte]]
            blocks = prev
        return blocks.tobytes() + data[n:]


def archive(files: dict[str, object], encryption: int = rpffmt.ENC_OPEN, keys: FakeKeys | None = None,
            name: str = "a.rpf") -> bytes:
    """An RPF7 archive. ``files`` maps "dir/name" to bytes (stored as is), ``("deflate", bytes)``,
    ``("rsc", rsc7_bytes)`` for a resource or ``("rpf", archive_bytes)`` for a nested archive."""
    tree: dict = {}
    for p in files:
        node = tree
        parts = p.lower().split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = p
    names = bytearray(b"\0")
    offs: dict[str, int] = {"": 0}

    def name_of(s: str) -> int:
        if s not in offs:
            offs[s] = len(names)
            names.extend(s.encode("latin-1") + b"\0")
        return offs[s]

    rows: list[list] = [["dir", "", tree, 0, 0]]
    i = 0
    while i < len(rows):
        row = rows[i]
        if row[0] == "dir":
            items = sorted(row[2])
            row[3], row[4] = len(rows), len(items)
            for k in items:
                v = row[2][k]
                rows.append(["dir", k.rstrip("/"), v, 0, 0] if isinstance(v, dict) else ["file", k, v])
        i += 1
    for row in rows:
        name_of(row[1])
    while len(names) % 16:
        names.append(0)
    count = len(rows)
    pos = (16 + 16 * count + len(names) + 511) // 512 * 512
    body = bytearray()
    table = bytearray()
    for row in rows:
        if row[0] == "dir":
            table += struct.pack("<4I", name_of(row[1]), 0x7FFFFF00, row[3], row[4])
            continue
        data = files[row[2]]
        off = pos + len(body)
        if isinstance(data, tuple) and data[0] == "rsc":
            blob = data[1]
            _m, _v, sf, gf = struct.unpack_from("<4I", blob, 0)
            table += struct.pack("<H", name_of(row[1])) + len(blob).to_bytes(3, "little") + \
                (off // 512 | 0x800000).to_bytes(3, "little") + struct.pack("<II", sf, gf)
            body += blob
        else:
            if isinstance(data, tuple) and data[0] == "deflate":
                c = zlib.compressobj(9, zlib.DEFLATED, -15)
                stored = c.compress(data[1]) + c.flush()
                size, unc = len(stored), len(data[1])
            elif isinstance(data, tuple) and data[0] == "rpf":
                stored, size, unc = data[1], 0, len(data[1])
            else:
                stored, size, unc = bytes(data), 0, len(data)
            table += struct.pack("<QII", name_of(row[1]) | size << 16 | (off // 512) << 40, unc, 0)
            body += stored
        while len(body) % 512:
            body.append(0)
    total = pos + len(body)
    tbl, nms = bytes(table), bytes(names)
    if encryption == rpffmt.ENC_NG:
        tbl = keys.encrypt_ng(tbl, name, total)
        nms = keys.encrypt_ng(nms, name, total)
    head = struct.pack("<4I", rpffmt.MAGIC, count, len(names), encryption) + tbl + nms
    return head + b"\0" * (pos - len(head)) + bytes(body)


# --- a game ------------------------------------------------------------------------------------------------

PED_BONES = [
    {"name": "SKEL_ROOT", "tag": 0, "parent": -1},
    {"name": "SKEL_Pelvis", "tag": 11816, "parent": 0, "translation": (0, 0, 1.0)},
    {"name": "SKEL_Spine0", "tag": 23553, "parent": 1, "translation": (0, 0, 0.2)},
    {"name": "SKEL_Head", "tag": 31086, "parent": 2, "translation": (0, 0, 0.6)},
]
CAR_BONES = [
    {"name": "chassis", "tag": 0, "parent": -1},
    {"name": "wheel_lf", "tag": 27922, "parent": 0, "translation": (-0.8, 1.2, 0.3)},
    {"name": "wheel_rf", "tag": 26418, "parent": 0, "translation": (0.8, 1.2, 0.3)},
    {"name": "wheel_lr", "tag": 27902, "parent": 0, "translation": (-0.8, -1.2, 0.3)},
    {"name": "wheel_rr", "tag": 26398, "parent": 0, "translation": (0.8, -1.2, 0.3)},
    {"name": "door_dside_f", "tag": 7, "parent": 0, "translation": (-0.9, 0.3, 0.6)},
]


def _skinned(v, f, bone: int, shader: int = 0) -> Geo:
    n = len(v)
    weights = np.zeros((n, 4), np.uint8)
    weights[:, 0] = 255
    idx = np.zeros((n, 4), np.uint8)
    idx[:, 0] = 0  # through bone_ids
    return Geo(v, f, shader, weights, idx, [bone])


def car_yft(gen9: bool = False) -> bytes:
    body_v, body_f = box((1.8, 4.0, 1.0), (0, 0, 0.8))
    door_v, door_f = box((0.1, 1.0, 0.6))
    wheel_v, wheel_f = box((0.3, 0.7, 0.7))
    return yft("adder", [([Geo(body_v, body_f, 0)], 0, False), ([Geo(door_v, door_f, 0)], 5, False)],
               [[("DiffuseSampler", "adder_body"), ("BumpSampler", "adder_body_n")]], CAR_BONES,
               [(27922, [([Geo(wheel_v, wheel_f, 0)], 0, False)], [[("DiffuseSampler", "adder_wheel")]]),
                (26418, None, None), (27902, None, None), (26398, None, None)], gen9)


def ped_yft() -> bytes:
    return yft("a_m_y_test_01", [], [], PED_BONES)


def ped_ydd(gen9: bool = False) -> bytes:
    hv, hf = box((0.3, 0.2, 0.3), (0, 0, 1.9))
    uv, uf = box((0.6, 0.4, 0.6), (0, 0, 1.4))
    u2v, u2f = box((0.7, 0.5, 0.6), (0, 0, 1.4))
    lv, lf = box((0.5, 0.3, 0.9), (0, 0, 0.6))
    return ydd([
        ("head_000_r", [([_skinned(hv, hf, 3)], 0, True)], [[("DiffuseSampler", "head_diff_000_a_whi")]]),
        ("uppr_000_u", [([_skinned(uv, uf, 2)], 0, True)], [[("DiffuseSampler", "uppr_diff_000_a_uni")]]),
        ("uppr_001_u", [([_skinned(u2v, u2f, 2)], 0, True)], [[("DiffuseSampler", "uppr_diff_001_a_uni")]]),
        ("lowr_000_u", [([_skinned(lv, lf, 1)], 0, True)], [[("DiffuseSampler", "lowr_diff_000_a_uni")]]),
    ], gen9)


def walk_ycd() -> bytes:
    return ycd([("walk", 11, [(31086, 1, turn_z(11, 90)), (11816, 0, [("svec", (0.0, 0.0, 1.0))])]),
                ("idle", 2, [(23553, 1, [("squat", (0.0, 0.0, 0.0))]), (31086, 1, [("squat", (0.0, 0.0, 0.0))])])])


def pistol_ydr() -> bytes:
    v, f = box((0.05, 0.2, 0.12))
    return ydr([([Geo(v, f, 0)], 0, False)], [[("DiffuseSampler", "w_pi_pistol"), ("SpecSampler", "w_pi_pistol_s")]],
               name="w_pi_pistol")


def prop_ydr(gen9: bool = False) -> bytes:
    v, f = box((1, 1, 1))
    return ydr([([Geo(v, f, 0)], 0, False)], [[("DiffuseSampler", "crate_d")]], name="prop_crate_01", gen9=gen9,
               own_textures=[("crate_d", (120, 90, 30))])


def gta5_game(root: Path, keys: FakeKeys) -> Path:
    """A GTA V folder: GTA5.exe, an NG-encrypted x64e.rpf (vehicles and a ped and weapons nested inside),
    a plain update.rpf (the lists and text) and a loose prop export."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "GTA5.exe").write_bytes(keys.exe())
    vehicles = archive({
        "adder.yft": ("rsc", car_yft()),
        "adder.ytd": ("rsc", ytd([("adder_body", (200, 30, 30)), ("adder_body_n", (128, 128, 255))])),
        "vehshare.ytd": ("rsc", ytd([("adder_wheel", (30, 30, 30))])),
    })
    peds = archive({
        "a_m_y_test_01.yft": ("rsc", ped_yft()),
        "a_m_y_test_01.ydd": ("rsc", ped_ydd()),
        "a_m_y_test_01.ytd": ("rsc", ytd([("head_diff_000_a_whi", (220, 180, 150)),
                                          ("uppr_diff_000_a_uni", (30, 90, 200)),
                                          ("uppr_diff_001_a_uni", (30, 200, 90)),
                                          ("lowr_diff_000_a_uni", (40, 40, 40))])),
    })
    weapons = archive({
        "w_pi_pistol.ydr": ("rsc", pistol_ydr()),
        "w_pi_pistol.ytd": ("rsc", ytd([("w_pi_pistol", (60, 60, 60)), ("w_pi_pistol_s", (10, 10, 10))])),
    })
    anims = archive({"move_m@generic.ycd": ("rsc", walk_ycd())})
    x64e = archive({
        "levels/gta5/vehicles.rpf": ("rpf", vehicles),
        "models/cdimages/streamedpeds.rpf": ("rpf", peds),
        "models/cdimages/weapons.rpf": ("rpf", weapons),
        "anim/ingame/clip_move@.rpf": ("rpf", anims),
        "levels/gta5/props/industrial.rpf": ("rpf", archive({"prop_crate_01.ydr": ("rsc", prop_ydr())})),
        "readme.txt": b"not a model",
    }, rpffmt.ENC_NG, keys, "x64e.rpf")
    (root / "x64e.rpf").write_bytes(x64e)
    lang = archive({"global.gxt2": gxt2({"ADDER": "Adder", "TRUFFADE": "Truffade", "WT_PIST": "Pistol"})})
    update = archive({
        "common/data/levels/gta5/vehicles.meta": ("deflate", vehicles_meta(
            [{"model": "adder", "label": "ADDER", "maker": "TRUFFADE", "class": "VC_SUPER"}])),
        "common/data/peds.meta": peds_meta([("a_m_y_test_01", "CIVMALE")]),
        "common/data/ai/weapons.meta": weapons_meta([("WEAPON_PISTOL", "w_pi_pistol", "WT_PIST", "GROUP_PISTOL")]),
        "x64/data/lang/americanrel.rpf": ("rpf", lang),
    })
    (root / "update").mkdir()
    (root / "update" / "update.rpf").write_bytes(update)
    return root


def install_fake_hashes(monkeypatch, keys: FakeKeys) -> None:
    from darkstl.importers.rage.formats import keyhashes

    for k, v in keys.hashes().items():
        monkeypatch.setattr(keyhashes, k, v)


__all__ = ["FakeKeys", "ResWriter", "archive", "gta5_game", "install_fake_hashes"]
