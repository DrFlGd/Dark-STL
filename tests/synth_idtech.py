"""Builders for synthetic id Tech 7 and 8 (DOOM Eternal, DOOM: The Dark Ages) files: containers, StreamDB,
images, models, skeletons, animations and decls, written to the layouts the readers follow (docs/PLAN.md, "Reading
id Tech 6/7/8 files")."""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path

import numpy as np

from darkstl.importers.idtech.formats import geometry as G
from darkstl.importers.idtech.formats.farmhash import fingerprint64
from darkstl.importers.idtech.formats.resources import ENTRY


def s32(text: str) -> bytes:
    b = text.encode()
    return struct.pack("<I", len(b)) + b


# ---------------------------------------------------------------------------
# containers


def container(entries: list[tuple[str, str, bytes, int, int]], version: int = 12) -> tuple[bytes, int]:
    """An IDCL file holding ``(type, name, data, hash, comp)`` entries (comp 0 stored, 1 zlib). Returns the file and
    the fingerprint the container mask keys it by."""
    strings: list[str] = []
    index: list[int] = []
    for t, n, *_ in entries:
        for s in (t, n):
            if s not in strings:
                strings.append(s)
            index.append(strings.index(s))
    head_size = 124 if version == 12 else 112
    ent_off = head_size
    str_off = ent_off + len(entries) * ENTRY.itemsize
    sbytes = b"".join(s.encode() + b"\0" for s in strings)
    offs, at = [], 0
    for s in strings:
        offs.append(at)
        at += len(s.encode()) + 1
    string_table = struct.pack("<Q", len(strings)) + struct.pack(f"<{len(offs)}Q", *offs) + sbytes
    deps_off = str_off + len(string_table)
    tail = struct.pack(f"<{len(index)}q", *index)  # no dependencies, no dependency indices
    data_off = deps_off + len(tail) + 4
    data_off = (data_off + 15) & ~15
    rows = np.zeros(len(entries), ENTRY)
    blobs = b""
    for i, (t, n, data, h, comp) in enumerate(entries):
        stored = zlib.compress(data) if comp == 1 else data
        r = rows[i]
        r["name_str"], r["desc_str"], r["strings"] = 1, -1, i * 2
        r["data_offset"] = data_off + len(blobs)
        r["data_size"], r["size"], r["hash"], r["comp"] = len(stored), len(data), h, comp
        r["num_strings"] = 2
        blobs += stored
        blobs += b"\0" * (-len(blobs) % 16)
    counts = (len(entries), 0, 0, len(index), 0, 0, len(sbytes), 0)
    offsets = (str_off, 0, ent_off, deps_off, deps_off, data_off)
    if version == 12:
        header = struct.pack("<4sIII QQ IIII II II QQQQQQ I Q", b"IDCL", 12, 0, 1, 0xFFFFFFFFFF, 0, *counts[:4],
                             *counts[4:6], *counts[6:8], *offsets, 0, 0)
    else:
        header = struct.pack("<4sIII QQ IIII II II QQQQQQ", b"IDCL", 13, 0, 1, 0xFFFFFFFFFF, 0, *counts[:4],
                             *counts[4:6], *counts[6:8], *offsets)
    body = header + rows.tobytes() + string_table + tail
    fp = fingerprint64((rows.tobytes() + string_table + tail + b"\0\0\0\0")[:len(rows.tobytes()) + len(
        string_table) + len(tail) + 4])
    body += b"\0" * (data_off - len(body))
    return body + blobs, fp


def container_mask(masks: dict[int, list[bool]], version: int = 12) -> bytes:
    out = (b"\0\0\0\0" if version == 12 else b"") + struct.pack("<I", len(masks))
    for key, bits in masks.items():
        words = (len(bits) + 63) // 64
        packed = np.packbits(np.array(bits + [False] * (words * 64 - len(bits)), bool), bitorder="little")
        out += struct.pack("<QI", key, words) + packed.tobytes()
    return out


def streamdb(pieces: dict[int, bytes]) -> bytes:
    head_len = 32
    table = b""
    data = b""
    start = head_len + len(pieces) * 16
    start = (start + 15) & ~15
    for ident, blob in pieces.items():
        table += struct.pack("<QII", ident, (start + len(data)) // 16, len(blob))
        data += blob + b"\0" * (-len(blob) % 16)
    head = struct.pack("<QIIIIII", 0x61C7F32E29C2A550, head_len, 0, 0, 0, len(pieces), 0)
    out = head + table
    return out + b"\0" * (start - len(out)) + data


# ---------------------------------------------------------------------------
# images


def bc1_solid(width: int, height: int, rgb565: int) -> bytes:
    blocks = max(1, width // 4) * max(1, height // 4)
    return struct.pack("<HHI", rgb565, rgb565, 0) * blocks


def image(width: int, height: int, mips: list[bytes], fmt: int = 33, kind: int = 1, streamed: int = 0,
          version: int = 21) -> bytes:
    """A BIM image; ``mips`` largest first, the first ``streamed`` of them kept in the StreamDB (not written)."""
    out = b"BIM" + bytes([version]) + struct.pack("<7i", 0, kind, width, height, 1, len(mips), 0)
    out += struct.pack("<ff", 0.0, 1.0) + b"\0" + struct.pack("<iiih", fmt, 7 if version < 24 else 8, 0, 0)
    out += bytes([1 if streamed else 0, 0, 0, 0]) + (b"\0" if version >= 24 else b"")
    out += struct.pack("<i", streamed)
    cumulative = 0
    for i, m in enumerate(mips):
        w, h = max(1, width >> i), max(1, height >> i)
        out += struct.pack("<9i", i, 0, w, h, 1, len(m), 0, len(m), cumulative)
        cumulative += len(m)
    return out + b"".join(mips[streamed:])


# ---------------------------------------------------------------------------
# geometry


def _q16(v: np.ndarray, lo: np.ndarray, scale: float) -> np.ndarray:
    return np.clip(np.round((v - lo) / scale * 65535), 0, 65535).astype("<u2")


def _b8(v: np.ndarray) -> np.ndarray:
    return np.clip(np.round((v + 1) * 127.5), 0, 255).astype(np.uint8)


def lod_quant(positions: np.ndarray, uvs: np.ndarray):
    lo = positions.min(0)
    scale = float(max((positions.max(0) - lo).max(), 1e-6))
    uv_lo = uvs.min(0)
    uv_scale = float(max((uvs.max(0) - uv_lo).max(), 1e-6))
    return lo, scale, uv_lo, uv_scale


def vertex_streams(positions, normals, uvs, joints=None, weights=None) -> dict[int, bytes]:
    """The streams of a level of detail: POSITION_SHORT, NORMAL_TANGENT, MATERIAL_UV_SHORT (and COLOR: joints)."""
    lo, scale, uv_lo, uv_scale = lod_quant(positions, uvs)
    n = len(positions)
    p = np.zeros((n, 4), "<u2")
    p[:, :3] = _q16(positions, lo, scale)
    nt = np.zeros((n, 8), np.uint8)
    nt[:, 0:3] = _b8(normals)
    nt[:, 4:7] = _b8(np.tile([[1.0, 0.0, 0.0]], (n, 1)))
    nt[:, 7] = 0x80
    if weights is not None:
        # four weights: x is what's left; y in the tangent byte (/254), z and w in the normal byte (/45, /60)
        y = np.round(weights[:, 1] * 254).astype(int)
        z = np.round(weights[:, 2] * 45).astype(int)
        w = np.round(weights[:, 3] * 60).astype(int)
        nt[:, 7] = (nt[:, 7] & 0x80) | (y & 0x7F)
        nt[:, 3] = ((z & 0x0F) << 4) | (w & 0x0F)
    uv = _q16(uvs, uv_lo, uv_scale)
    out = {G.POSITION_SHORT: p.tobytes(), G.NORMAL_TANGENT: nt.tobytes(), G.MATERIAL_UV_SHORT: uv.tobytes()}
    if joints is not None:
        out[G.COLOR] = np.asarray(joints, np.uint8).tobytes()
    return out


def lod_record_static(n_vertices: int, n_tris: int, mask: int, positions, uvs) -> bytes:
    lo, scale, uv_lo, uv_scale = lod_quant(positions, uvs)
    return (struct.pack("<iIII", -2, n_vertices, n_tris * 3, mask) + struct.pack("<6f", *positions.min(0),
            *positions.max(0)) + struct.pack("<3ff", *lo, scale) + struct.pack("<2ff", *uv_lo, uv_scale)
            + struct.pack("<Iff", 0, 0, 0) + struct.pack("<I", 0x724C4D42))


def lod_record_md6(n_vertices: int, n_tris: int, mask: int, positions, uvs) -> bytes:
    lo, scale, uv_lo, uv_scale = lod_quant(positions, uvs)
    return (struct.pack("<II", n_vertices, n_tris) + struct.pack("<6f", *positions.min(0), *positions.max(0))
            + struct.pack("<3ff", *lo, scale) + struct.pack("<2ff", *uv_lo, uv_scale) + struct.pack("<Iff", mask, 0, 0))


def memory_layout(mask: int, masks: list[int], offsets: list[int], index_offset: int) -> bytes:
    n = len(masks)
    return struct.pack(f"<III{n}I{n}II", mask, 0, n, *masks, *offsets, index_offset)


def streamed_piece(surfaces: list[dict], masks: list[int]) -> tuple[bytes, bytes]:
    """One level of detail's piece for surfaces sharing a layout, and the memory layout record."""
    streams = {m: b"" for m in masks}
    indices = b""
    for s in surfaces:
        for m in masks:
            streams[m] += s["streams"][m]
        indices += np.asarray(s["triangles"], "<u2").tobytes()
    offsets, blob = [], b""
    for m in masks:
        offsets.append(len(blob))
        blob += streams[m]
    index_offset = len(blob)
    blob += indices
    combined = 0
    for m in masks:
        combined |= m
    return blob, memory_layout(combined, masks, offsets, index_offset)


def static_model(surfaces: list[dict], streamable: bool) -> tuple[bytes, bytes | None]:
    """A static model resource (and its LOD 0 StreamDB piece when streamable). Each surface: material, positions,
    normals, uvs, triangles."""
    masks = [G.POSITION_SHORT, G.NORMAL_TANGENT, G.MATERIAL_UV_SHORT]
    combined = G.POSITION_SHORT | G.NORMAL_TANGENT | G.MATERIAL_UV_SHORT
    out = struct.pack("<3fII", 0, 0, 0, 1, len(surfaces)) + struct.pack("<f", 0) + struct.pack("<I", int(streamable))
    for s in surfaces:
        s["streams"] = vertex_streams(s["positions"], s["normals"], s["uvs"])
        out += s32(s["material"]) + struct.pack("<III", 0, 0, 0)
        out += struct.pack("<I", 0) + lod_record_static(len(s["positions"]), len(s["triangles"]), combined,
                                                         s["positions"], s["uvs"])
        out += struct.pack("<I", 1) * 4
    out += struct.pack("<fIII", 0, 0, 0, 0)  # lightmap settings, no texture axes
    out += struct.pack("<I", 0) + s32("") + struct.pack("<I", 0)  # decals
    out += bytes(len(surfaces) * 5)
    piece = None
    if streamable:
        piece, layout = streamed_piece(surfaces, masks)
        out += struct.pack("<I", 1) + layout + struct.pack("<IIII", 0, len(piece), len(piece), 0)
        for _ in range(4):
            out += struct.pack("<I", 0) + struct.pack("<IIII", 0, 0, 0, 0)
    else:
        order = [m for m in G.INTERLEAVED if m in masks]
        for s in surfaces:
            n = len(s["positions"])
            parts = [np.frombuffer(s["streams"][m], np.uint8).reshape(n, G.SIZES[m]) for m in order]
            out += np.concatenate(parts, axis=1).tobytes() + np.asarray(s["triangles"], "<u2").tobytes()
    return out, piece


def md6_model(skeleton: str, remap: list[int], meshes: list[dict]) -> tuple[bytes, bytes]:
    """A skinned model and its LOD 0 piece. Each mesh: name, material, joint_offset, positions, normals, uvs,
    triangles, joints (raw, before remap), weights."""
    masks = [G.POSITION_SHORT, G.NORMAL_TANGENT, G.COLOR, G.MATERIAL_UV_SHORT]
    combined = 0
    for m in masks:
        combined |= m
    out = s32(skeleton) + struct.pack("<6f", 0, 0, 0, 0, 0, 0) + b"\1" + struct.pack("<I", 0)
    out += struct.pack("<H", len(remap)) + bytes(remap) + struct.pack("<6f", -1, -1, -1, 1, 1, 1)
    out += struct.pack("<I5fI", 5, 0, 0, 0, 0, 0, 0) + bytes(36)
    out += struct.pack("<I", len(meshes))
    for m in meshes:
        m["streams"] = vertex_streams(m["positions"], m["normals"], m["uvs"], m["joints"], m["weights"])
        out += s32(m["name"]) + s32(m["material"]) + b"\1" + struct.pack("<I", 1)
        out += struct.pack("<IIII", 0, m.get("joint_offset", 0), 0, 0)
        out += struct.pack("<I", 0) + lod_record_md6(len(m["positions"]), len(m["triangles"]), combined,
                                                      m["positions"], m["uvs"])
        out += struct.pack("<I", 1) * 4 + b"\0" + struct.pack("<I", 0)
    out += struct.pack("<I", 0)  # material infos
    out += s32("") + struct.pack("<I", 0)  # decals
    piece, layout = streamed_piece(meshes, masks)
    out += struct.pack("<I", 5) + layout + memory_layout(0, [], [], 0) * 4
    out += struct.pack("<IIII", 0, len(piece), len(piece), 0) + struct.pack("<IIII", 0, 0, 0, 0) * 4
    return out, piece


# ---------------------------------------------------------------------------
# skeletons and animations


def skeleton(names: list[str], parents: list[int], translations: list[tuple]) -> bytes:
    n = len(names)
    n8 = (n + 7) & ~7
    header_size = 64  # after the 4-byte prefix
    pose_off = header_size
    rot = np.zeros((n8, 4), "<f4")
    rot[:, 3] = 1
    scl = np.ones((n8, 3), "<f4")
    trn = np.zeros((n8, 3), "<f4")
    trn[:n] = translations
    pose = rot.tobytes() + scl.tobytes() + trn.tobytes()
    parent_off = pose_off + len(pose)
    par = np.full(n8, -1, "<i2")
    par[:n] = parents
    inv_off = parent_off + n8 * 2
    inv = np.zeros((n8, 3, 4), "<f4")
    inv[:, :, :3] = np.eye(3)
    body_end = inv_off + n8 * 48
    head = struct.pack("<7h", body_end, n, 0, 0, pose_off, inv_off, parent_off)
    head += struct.pack("<3h", 0, 0, 0) + bytes(32) + struct.pack("<3h", 0, 0, 0) + bytes(6)
    assert len(head) == header_size
    body = head + pose + par.tobytes() + inv.tobytes()
    return struct.pack("<I", len(body)) + body + b"".join(s32(x) for x in names)


def quat48(q) -> bytes:
    """A quaternion (x, y, z, w) in the 48-bit "smallest three" form with the largest-last ordering (index 0)."""
    x, y, z, w = q
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    vals = []
    for c in (x, y, z):
        v = round((c + math.sqrt(0.5)) * 0x7FFF / math.sqrt(2))
        vals.append(max(0, min(0x7FFF, v)))
    return struct.pack("<3H", *vals)


def rle(joints: list[int]) -> bytes:
    out = bytes([len(joints)])
    i = 0
    while i < len(joints):
        j = i
        while j + 1 < len(joints) and joints[j + 1] == joints[j] + 1 and j - i < 126:
            j += 1
        out += bytes([j - i + 1, joints[i]])
        i = j + 1
    return out


def anim(skeleton_name: str, frames: int, fps: int, rot_joint: int, angles: list[float],
         const_t: dict[int, tuple] | None = None) -> bytes:
    """An animation turning one joint about Z by ``angles`` (a key every frame) and holding translations."""
    const_t = const_t or {}
    head = s32(skeleton_name) + struct.pack("<6f6f", *([0.0] * 12)) + struct.pack("<I", 0)
    data_size = 144
    # maps
    lists = [[], [], sorted(const_t), [], [rot_joint], [], [], []]
    blobs = [rle(x) for x in lists]
    maps_head = 2 + 2 + 16
    offs, at = [], maps_head
    for b in blobs:
        offs.append(at)
        at += len(b)
    maps = struct.pack("<hh", 1, 0) + struct.pack("<8h", *offs) + b"".join(blobs)
    maps += b"\0" * (-len(maps) % 16)
    const_t_off = data_size + len(maps)
    const_block = b"".join(struct.pack("<3f", *const_t[j]) for j in sorted(const_t))
    const_block += b"\0" * (-len(const_block) % 16)
    set_tbl_off = const_t_off + len(const_block)
    set_tbl = bytes(frames) + b"\0" * (-frames % 16)
    set_off_tbl_off = set_tbl_off + len(set_tbl)
    fs_start = set_off_tbl_off + 16
    # frame set: first R, bits R, range R (keys at frames 1..n-1)
    quats = [(0.0, 0.0, math.sin(a / 2), math.cos(a / 2)) for a in angles]
    hdr = 48
    first_r = quat48(quats[0])
    per = (frames + 7) >> 3
    bits = np.zeros(per * 8, np.uint8)
    bits[1:frames] = 1
    bits_r = np.packbits(bits).tobytes()
    rng = b"".join(quat48(q) for q in quats[1:])
    first_off, bits_off = hdr, hdr + len(first_r)
    range_off = bits_off + len(bits_r)
    fs = struct.pack("<19h", first_off, 0, 0, 0, range_off, 0, 0, 0, bits_off, 0, 0, 0, 0, 0, 0, 0,
                     hdr + len(first_r) + len(bits_r) + len(rng), 0, frames) + bytes(10)
    fs += first_r + bits_r + rng
    set_offsets = struct.pack("<2i", fs_start // 16, (fs_start + len(fs) + 15) // 16)
    set_offsets += b"\0" * (16 - len(set_offsets))
    data = struct.pack("<QI", 0, 0) + struct.pack("<14h", data_size, 0, frames, fps, 1, set_tbl_off, set_off_tbl_off,
                                                     const_t_off, const_t_off, const_t_off, const_t_off, 0, 0, 0)
    data += bytes(96) + struct.pack("<Q", 0)
    assert len(data) == data_size
    return head + data + maps + const_block + set_tbl + set_offsets + fs


# ---------------------------------------------------------------------------
# a whole DOOM Eternal install


def eternal_install(root: Path, with_mask: bool = True) -> dict:
    """A small DOOM Eternal folder: an imp (skinned, skeleton, animation), a crate (static, streamed), a pickup
    (static, inline), their materials and textures. Returns what was written."""
    base = root / "base"
    base.mkdir(parents=True, exist_ok=True)
    (root / "DOOMEternalx64vk.exe").write_bytes(b"MZ")
    # geometry: a quad (two triangles) per surface
    quad = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], np.float64)
    normals = np.tile([[0.0, -1.0, 0.0]], (4, 1))
    uvs = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float64)
    tris = [[0, 2, 1], [0, 3, 2]]  # clockwise as the game keeps them
    crate, crate_piece = static_model([{"material": "models/props/crate/crate", "positions": quad * 40.0,
                                        "normals": normals, "uvs": uvs, "triangles": tris}], True)
    medkit, _ = static_model([{"material": "models/pickups/medkit", "positions": quad * 10.0, "normals": normals,
                               "uvs": uvs, "triangles": tris}], False)
    joints = np.array([[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]])
    weights = np.array([[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], np.float64)
    imp, imp_piece = md6_model("md6/monsters/imp/base/imp.md6skl", [0, 1], [{
        "name": "body", "material": "models/monsters/imp/imp", "joint_offset": 0, "positions": quad * 72.0,
        "normals": normals, "uvs": uvs, "triangles": tris, "joints": joints, "weights": weights}])
    skel = skeleton(["origin", "spine"], [-1, 0], [(0, 0, 0), (0, 0, 36.0)])
    turn = anim("md6/monsters/imp/base/imp.md6skl", 4, 30, 1, [0.0, 0.5, 1.0, math.pi / 2],
                const_t={0: (0.0, 0.0, 0.0), 1: (0.0, 0.0, 36.0)})
    albedo = image(8, 8, [bc1_solid(8, 8, 0xF800), bc1_solid(4, 4, 0xF800), bc1_solid(4, 4, 0xF800)], 33, 1,
                   streamed=1)
    normal = image(4, 4, [bytes(16 * 4)], 3, 3)
    crate_albedo = image(4, 4, [bc1_solid(4, 4, 0x07E0)], 33, 1)
    imp_decl = b'{ inherit = "monster_base"; edit = { RenderLayers = { item[0] = { parms = { ' \
        b'albedo = { filePath = "models/monsters/imp/imp_albedo.tga"; } ' \
        b'normal = { filePath = "models/monsters/imp/imp_n.tga"; } } } } } }'
    base_decl = b'{ edit = { Parms = { smoothness = { filePath = "_default"; } } } }'
    crate_decl = b'{ edit = { RenderLayers = { item[0] = { parms = { ' \
        b'albedo = { filePath = "models/props/crate/crate_albedo.tga"; } } } } } }'
    h_imp, h_crate, h_albedo = 0x1111, 0x2222, 0x3333
    entries = [
        ("baseModel", "md6/monsters/imp/base/imp.md6mesh", imp, h_imp, 1),
        ("skeleton", "md6/monsters/imp/base/imp.md6skl", skel, 0x4444, 0),
        ("anim", "md6/monsters/imp/animation/turn.md6anim", turn, 0x5555, 1),
        ("model", "models/props/crate/crate.lwo", crate, h_crate, 0),
        ("model", "models/pickups/medkit.lwo", medkit, 0x6666, 0),
        ("image", "models/monsters/imp/imp_albedo.tga$bc1srgb$mtlkind=albedo", albedo, h_albedo, 0),
        ("image", "models/monsters/imp/imp_n.tga$mtlkind=normal", normal, 0x7777, 0),
        ("image", "models/props/crate/crate_albedo.tga$mtlkind=albedo", crate_albedo, 0x8888, 0),
        ("rs_streamfile", "generated/decls/material2/models/monsters/imp/imp.decl", imp_decl, 0, 0),
        ("rs_streamfile", "generated/decls/material2/monster_base.decl", base_decl, 0, 0),
        ("rs_streamfile", "generated/decls/material2/models/props/crate/crate.decl", crate_decl, 0, 0),
    ]
    # a stale copy of the crate in a patch container, switched off by the mask
    stale, _ = static_model([{"material": "stale", "positions": quad, "normals": normals, "uvs": uvs,
                              "triangles": tris}], False)
    game, fp_game = container(entries)
    patch, fp_patch = container([("model", "models/props/crate/crate.lwo", stale, 0x9999, 0)])
    masks = {fp_game: [True] * len(entries), fp_patch: [False]} if with_mask else {}
    meta, _ = container([("file", "generated/buildgame/container.mask", container_mask(masks), 0, 1)])
    (base / "gameresources.resources").write_bytes(game)
    (base / "gameresources_patch1.resources").write_bytes(patch)
    (base / "meta.resources").write_bytes(meta)
    from darkstl.importers.idtech.formats.streamdb import eternal_id
    pieces = {eternal_id(h_imp, 0): imp_piece, eternal_id(h_crate, 0): crate_piece,
              eternal_id(h_albedo, 3 - 0): bc1_solid(8, 8, 0xF800)}
    (base / "gameresources.streamdb").write_bytes(streamdb(pieces))
    spec = {"files": [{"name": n} for n in ("meta.resources", "gameresources_patch1.resources",
                                             "gameresources.resources", "gameresources.streamdb")],
            "maps": [{"name": "common"}], "mapFileRefs": [{"file": i, "map": 0} for i in range(4)]}
    (base / "packagemapspec.json").write_text(json.dumps(spec))
    return {"root": root, "fingerprints": (fp_game, fp_patch)}


# ---------------------------------------------------------------------------
# DOOM: The Dark Ages


def vertex_streams_darkages(positions, normals, uvs, joints=None, weights=None, influence: int = 6
                            ) -> dict[int, bytes]:
    """The Dark Ages's streams: as DOOM Eternal's, but joints in a skinning stream (``influence`` per vertex) with the
    middle weights after them and the last three packed into the normal and tangent."""
    out = vertex_streams(positions, normals, uvs)
    if joints is None:
        return out
    n = len(positions)
    joints = np.asarray(joints, np.uint8).reshape(n, influence)
    weights = np.asarray(weights, np.float64).reshape(n, influence)
    mode = {4: G.SKINNING_4, 6: G.SKINNING_6, 8: G.SKINNING_8}[influence]
    extra = G._extra_weights(influence)
    skin = np.zeros((n, G.SIZES[mode]), np.uint8)
    skin[:, :influence] = joints
    skin[:, influence:influence + extra] = np.round(weights[:, 1:1 + extra] * 255).astype(np.uint8)
    s1, s2 = G._PACKED[influence]
    nt = np.frombuffer(out[G.NORMAL_TANGENT], np.uint8).reshape(n, 8).copy()
    y = np.round(weights[:, 1 + extra] * 254).astype(int)
    z = np.round(weights[:, 2 + extra] * s1).astype(int)
    w = np.round(weights[:, 3 + extra] * s2).astype(int)
    nt[:, 7] = (nt[:, 7] & 0x80) | (y & 0x7F)
    nt[:, 3] = ((z & 0x0F) << 4) | (w & 0x0F)
    out[G.NORMAL_TANGENT] = nt.tobytes()
    out[mode] = skin.tobytes()
    return out


def sequential_piece(surfaces: list[dict]) -> bytes:
    """One level of detail's piece as The Dark Ages writes it: per surface its streams (mask order, each aligned to
    its record size), its indices, then padding to 8 bytes."""
    blob = b""
    for s in surfaces:
        body = b""
        for m in G.split_mask(s["mask"]):
            size = G.SIZES[m]
            if size:
                body += b"\xee" * ((size - len(body) % size) % size)
                body += s["streams"][m]
        body += np.asarray(s["triangles"], "<u2").tobytes()
        blob += body + b"\xee" * (-len(body) % 8)
    return blob


def _mask_of(streams: dict[int, bytes]) -> int:
    combined = 0
    for m in streams:
        combined |= m
    return combined


def lod_record_static_darkages(n_vertices: int, n_tris: int, mask: int, positions, uvs) -> bytes:
    lo, scale, uv_lo, uv_scale = lod_quant(positions, uvs)
    return (struct.pack("<i6I", -5, n_vertices, n_tris * 3, 0, 0, 0, mask)
            + struct.pack("<6f", *positions.min(0), *positions.max(0)) + struct.pack("<3ff", *lo, scale)
            + struct.pack("<2ff", *uv_lo, uv_scale) + struct.pack("<Iff", 0, 0, 0) + struct.pack("<I", 0x724C4D42))


def static_model_darkages(surfaces: list[dict], streamable: bool) -> tuple[bytes, bytes | None]:
    out = struct.pack("<3fII", 0, 0, 0, 1, len(surfaces)) + struct.pack("<f", 0) + struct.pack("<I", int(streamable))
    for s in surfaces:
        s["streams"] = vertex_streams(s["positions"], s["normals"], s["uvs"])
        s["mask"] = _mask_of(s["streams"])
        out += s32(s["material"]) + struct.pack("<III", 0, 0, 0)
        out += struct.pack("<I", 0) + lod_record_static_darkages(len(s["positions"]), len(s["triangles"]),
                                                                  s["mask"], s["positions"], s["uvs"])
        out += struct.pack("<I", 1) * 4
    out += struct.pack("<fIII", 0, 0, 0, 0)
    out += struct.pack("<I", 1) + bytes(48) + s32("") + struct.pack("<I", 0)  # one decal projection
    out += bytes(len(surfaces) * 5)
    piece = None
    if streamable:
        piece = sequential_piece(surfaces)
        out += struct.pack("<IIII", 0, len(piece), len(piece), 0) + struct.pack("<IIII", 0, 0, 0, 0) * 4
    else:
        for s in surfaces:
            n = len(s["positions"])
            order = [m for m in G.INTERLEAVED if m in s["streams"]]
            parts = [np.frombuffer(s["streams"][m], np.uint8).reshape(n, G.SIZES[m]) for m in order]
            out += np.concatenate(parts, axis=1).tobytes() + np.asarray(s["triangles"], "<u2").tobytes()
    return out, piece


def lod_record_md6_darkages(n_vertices: int, n_tris: int, influence: int, mask: int, positions, uvs,
                            joint_offset: int) -> bytes:
    lo, scale, uv_lo, uv_scale = lod_quant(positions, uvs)
    return (struct.pack("<IIB", n_vertices, n_tris, influence) + struct.pack("<6f", *positions.min(0),
            *positions.max(0)) + struct.pack("<3ff", *lo, scale) + struct.pack("<2ff", *uv_lo, uv_scale)
            + struct.pack("<Iffh", mask, 0, 0, joint_offset))


def md6_model_darkages(skeleton: str, joints: int, skinned: list[int], extra: list[int], meshes: list[dict]
                       ) -> tuple[bytes, bytes]:
    """A Dark Ages skinned model for a skeleton of ``joints`` joints, and its LOD 0 piece. Each mesh: name, material,
    joint_offset, influence, positions, normals, uvs, triangles, joints and weights (``influence`` per vertex)."""
    n8 = (joints + 7) & ~7
    out = s32(skeleton) + struct.pack("<6f", 0, 0, 0, 0, 0, 0) + b"\1" + s32("")
    out += struct.pack(f"<H{len(skinned)}h", len(skinned), *skinned)
    out += struct.pack(f"<H{len(extra)}H", len(extra), *extra)
    out += struct.pack("<6f", -1, -1, -1, 1, 1, 1) + struct.pack("<i5f", 5, 0, 0, 0, 0, 0)
    out += struct.pack("<I", 0) + struct.pack("<9f", *([0.0] * 9)) + struct.pack(f"<{n8}f", *([1.0] * n8))
    out += struct.pack("<I", len(meshes))
    for m in meshes:
        inf = m.get("influence", 6)
        m["streams"] = vertex_streams_darkages(m["positions"], m["normals"], m["uvs"], m["joints"], m["weights"], inf)
        m["mask"] = _mask_of(m["streams"])
        out += s32(m["name"]) + s32(m["material"]) + struct.pack("<iih", 0, 0, 0)
        out += struct.pack("<I", 0) + lod_record_md6_darkages(len(m["positions"]), len(m["triangles"]), inf,
                                                               m["mask"], m["positions"], m["uvs"],
                                                               m.get("joint_offset", 0))
        out += struct.pack("<I", 1) * 4 + b"\0" + struct.pack("<I", 0)
    out += struct.pack("<I", 0)  # surface infos
    out += struct.pack("<iii", 0, 0, 0)  # wounds
    out += s32("") + struct.pack("<I", 0)  # decals
    piece = sequential_piece(meshes)
    out += struct.pack("<I", 5) + bytes(12 * 5)
    out += struct.pack("<IIII", 0, len(piece), len(piece), 0) + struct.pack("<IIII", 0, 0, 0, 0) * 4
    return out, piece


def skeleton_darkages(names: list[str], parents: list[int], translations: list[tuple]) -> bytes:
    n = len(names)
    n8 = (n + 7) & ~7
    header_size = 48
    rot = np.zeros((n8, 4), "<f4")
    rot[:, 3] = 1
    scl = np.ones((n8, 3), "<f4")
    trn = np.zeros((n8, 3), "<f4")
    trn[:n] = translations
    pose = rot.tobytes() + scl.tobytes() + trn.tobytes()
    pose_off = header_size
    parent_off = pose_off + len(pose)
    par = np.full(n8, -1, "<i2")
    par[:n] = parents
    inv_off = parent_off + n8 * 2
    inv = np.zeros((n8, 3, 4), "<f4")
    inv[:, :, :3] = np.eye(3)
    size = inv_off + n8 * 48
    head = struct.pack("<6i10hf", size, size, pose_off, inv_off, 0, 0, n, n, parent_off, 0, 0, 0, 0, 0, 0, 0, 1.0)
    assert len(head) == header_size
    body = head + pose + par.tobytes() + inv.tobytes()
    return struct.pack("<I", len(body)) + body + b"".join(s32(x) for x in names + [""] * (n8 - n))


def quat48_darkages(q) -> bytes:
    x, y, z, w = q
    if w < 0:
        x, y, z, w = -x, -y, -z, -w
    vals = [max(0, min(0x7FFF, round((c + math.sqrt(0.5)) * 0x8000 / math.sqrt(2)))) for c in (x, y, z)]
    return struct.pack("<3H", *vals)


def rle16(joints: list[int], wide: bool = False) -> bytes:
    out = struct.pack("<h", len(joints) | (-0x8000 if wide else 0))
    i = 0
    while i < len(joints):
        j = i
        while j + 1 < len(joints) and joints[j + 1] == joints[j] + 1 and j - i < 126:
            j += 1
        out += bytes([j - i + 1]) + (struct.pack("<H", joints[i]) if wide else bytes([joints[i]]))
        i = j + 1
    return out


def frame_set_darkages(frames: int, quats: list[tuple], lod: int = 0) -> bytes:
    """One frame set turning one joint: its first rotation, a key on every later frame (bits from the row's end)."""
    hdr = 48
    first_r = quat48_darkages(quats[0])
    per = (frames + 7) >> 3
    bits = np.zeros(per * 8, np.uint8)
    bits[1:frames] = 1
    bits_r = np.packbits(bits).tobytes()[::-1]  # one joint: its row, last byte first
    rng = b"".join(quat48_darkages(q) for q in quats[1:])
    first_off, bits_off = hdr, hdr + len(first_r)
    range_off = bits_off + len(bits_r)
    total = range_off + len(rng)
    fs = struct.pack("<19H", first_off, 0, 0, 0, range_off, 0, 0, 0, bits_off, 0, 0, 0, 0, 0, 0, 0, total, 0, frames)
    fs += bytes([lod]) + bytes(9)
    return fs + first_r + bits_r + rng


def anim_darkages(skeleton_name: str, frames: int, fps: int, rot_joint: int, angles: list[float],
                  const_t: dict[int, tuple] | None = None, streamed: bool = False, wide: bool = False
                  ) -> tuple[bytes, bytes | None]:
    """A Dark Ages animation turning one joint about Z; with ``streamed`` its frame set (and a coarser level of
    detail's, which the reader skips) is in a StreamDB piece, returned beside it."""
    const_t = const_t or {}
    head = s32(skeleton_name) + struct.pack("<i", 0) + struct.pack("<12f", *([0.0] * 12))
    data_size = 144
    lists = [[], [], sorted(const_t), [], [rot_joint], [], [], []]
    blobs = [rle16(x, wide) for x in lists]
    offs, at = [], 1 + 16
    for b in blobs:
        offs.append(at)
        at += len(b)
    maps = b"\0" + struct.pack("<8H", *offs) + b"".join(blobs)
    maps += b"\0" * (-(data_size + len(maps)) % 16)
    const_t_off = data_size + len(maps)
    const_block = b"".join(struct.pack("<3f", *const_t[j]) for j in sorted(const_t))
    const_block += b"\0" * (-len(const_block) % 16)
    set_off_tbl_off = const_t_off + len(const_block)
    quats = [(0.0, 0.0, math.sin(a / 2), math.cos(a / 2)) for a in angles]
    fs = frame_set_darkages(frames, quats)
    piece = None
    if streamed:
        coarse = frame_set_darkages(frames, [(0.0, 0.0, 0.0, 1.0)] * frames, lod=1)
        piece = fs + b"\0" * (-len(fs) % 16) + coarse
        set_offsets = b"\0" * 16
        body_end = set_off_tbl_off + len(set_offsets)
        info = struct.pack("<hh", 1, 2) + struct.pack("<3I2h", len(piece), len(piece), 0, 2, 0)
        info += struct.pack("<2H", 0, len(fs) + (-len(fs) % 16)) + struct.pack("<2h", 0, 0)
        tail = b""
        method, n_sets = 3, 2
    else:
        fs_start = set_off_tbl_off + 16
        set_offsets = struct.pack("<2i", fs_start // 16, (fs_start + len(fs) + 15) // 16) + bytes(8)
        body_end = fs_start + len(fs)
        body_end += -body_end % 16
        info = struct.pack("<h", 0)
        tail = fs + b"\0" * (-len(fs) % 16)
        method, n_sets = 0, 1
    data = struct.pack("<i4h2i11hBBf24f", 0, data_size, 0, frames, fps, 0, 0, n_sets, 0, 0, set_off_tbl_off,
                       const_t_off, const_t_off, const_t_off, const_t_off, 0, 0, n_sets if streamed else 0,
                       method, 0, 1.0, *([0.0] * 24))
    assert len(data) == data_size
    body = data + maps + const_block + set_offsets + tail
    assert len(body) == body_end
    return head + struct.pack("<i", len(body)) + body + info, piece


def darkages_install(root: Path) -> dict:
    """A small DOOM: The Dark Ages folder, as :func:`eternal_install` but in The Dark Ages's formats: a skinned
    model with six joints per vertex and an extra joint, a streamed static model, an inline one, an animation kept
    in the container and one streamed, textures (one mip streamed) and materials naming them without extension."""
    from darkstl.importers.idtech.formats.streamdb import darkages_id

    base = root / "base"
    base.mkdir(parents=True, exist_ok=True)
    (root / "DOOMTheDarkAges.exe").write_bytes(b"MZ")
    quad = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], np.float64)
    normals = np.tile([[0.0, -1.0, 0.0]], (4, 1))
    uvs = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float64)
    tris = [[0, 2, 1], [0, 3, 2]]
    shield, shield_piece = static_model_darkages([
        {"material": "models/props/shield/shield", "positions": quad * 40.0, "normals": normals, "uvs": uvs,
         "triangles": tris},
        {"material": "models/props/shield/shield", "positions": quad * 20.0 + [0, 5, 0], "normals": normals,
         "uvs": uvs, "triangles": [[0, 2, 1]]}], True)
    relic, _ = static_model_darkages([{"material": "models/pickups/relic", "positions": quad * 10.0,
                                       "normals": normals, "uvs": uvs, "triangles": tris}], False)
    # raw joint 8 is the extra joint, which repeats skinned joint 1 (spine)
    joints = np.array([[0, 1, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [8, 0, 0, 0, 0, 0], [8, 0, 0, 0, 0, 0]])
    weights = np.array([[1, 0, 0, 0, 0, 0], [0.6, 0.4, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, 0]],
                       np.float64)
    skel_name = "md6/characters/slayer/base/slayer.md6skl"
    slayer, slayer_piece = md6_model_darkages(skel_name, 2, [0, 1], [1], [{
        "name": "body", "material": "models/characters/slayer/slayer", "joint_offset": 0, "influence": 6,
        "positions": quad * 72.0, "normals": normals, "uvs": uvs, "triangles": tris, "joints": joints,
        "weights": weights}])
    skel = skeleton_darkages(["origin", "spine"], [-1, 0], [(0, 0, 0), (0, 0, 36.0)])
    angles = [0.0, 0.5, 1.0, math.pi / 2]
    turn, _ = anim_darkages(skel_name, 4, 30, 1, angles, const_t={0: (0.0, 0.0, 0.0), 1: (0.0, 0.0, 36.0)})
    swing, swing_piece = anim_darkages(skel_name, 4, 30, 1, angles[::-1], streamed=True, wide=True)
    albedo = image(8, 8, [bc1_solid(8, 8, 0xF800), bc1_solid(4, 4, 0xF800), bc1_solid(4, 4, 0xF800)], 33, 1,
                   streamed=1, version=24)
    normal = image(4, 4, [bytes(16 * 4)], 3, 3, version=24)
    shield_albedo = image(4, 4, [bc1_solid(4, 4, 0x07E0)], 33, 1, version=24)
    slayer_decl = b'{ edit = { RenderLayers = { item[0] = { parms = { ' \
        b'albedo = { filePath = "models/characters/slayer/slayer_albedo"; } ' \
        b'normal = { filePath = "models/characters/slayer/slayer_n"; } } } } } }'
    shield_decl = b'{ edit = { RenderLayers = { item[0] = { parms = { ' \
        b'albedo = { filePath = "models/props/shield/shield_albedo"; } } } } } }'
    h_slayer, h_shield, h_albedo, h_swing = 0x1111, 0x2222, 0x3333, 0x5556
    entries = [
        ("baseModel", "md6/characters/slayer/base/slayer.md6mesh", slayer, h_slayer, 1),
        ("skeleton", skel_name, skel, 0x4444, 0),
        ("anim", "md6/characters/slayer/animation/turn.md6anim", turn, 0x5555, 1),
        ("anim", "md6/characters/slayer/animation/swing.md6anim", swing, h_swing, 0),
        ("model", "models/props/shield/shield.lwo", shield, h_shield, 0),
        ("model", "models/pickups/relic.lwo", relic, 0x6666, 0),
        ("image", "models/characters/slayer/slayer_albedo.tga$bc1srgb$mtlkind=albedo", albedo, h_albedo, 0),
        ("image", "models/characters/slayer/slayer_n.tga$mtlkind=normal", normal, 0x7777, 0),
        ("image", "models/props/shield/shield_albedo.tga$mtlkind=albedo", shield_albedo, 0x8888, 0),
        ("rs_streamfile", "generated/decls/material2/models/characters/slayer/slayer.decl", slayer_decl, 0, 0),
        ("rs_streamfile", "generated/decls/material2/models/props/shield/shield.decl", shield_decl, 0, 0),
    ]
    game, fp_game = container(entries, 13)
    meta, _ = container([("file", "generated/buildgame/container.mask",
                          container_mask({fp_game: [True] * len(entries)}, 13), 0, 1)], 13)
    (base / "gameresources.resources").write_bytes(game)
    (base / "meta.resources").write_bytes(meta)
    pieces = {darkages_id(h_slayer, 4, 0): slayer_piece, darkages_id(h_shield, 4, 0): shield_piece,
              darkages_id(h_albedo, 0, 0): bc1_solid(8, 8, 0xF800), darkages_id(h_swing, 0, 0): swing_piece}
    (base / "gameresources.streamdb").write_bytes(streamdb(pieces))
    spec = {"files": [{"name": n} for n in ("meta.resources", "gameresources.resources", "gameresources.streamdb")],
            "maps": [{"name": "common"}], "mapFileRefs": [{"file": i, "map": 0} for i in range(3)]}
    (base / "packagemapspec.json").write_text(json.dumps(spec))
    return {"root": root, "angles": angles}
