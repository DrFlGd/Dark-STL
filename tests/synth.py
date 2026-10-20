"""Builders for synthetic game files used by the tests.

Real game files cannot live in the repository, so these writers produce small
files that follow the documented layouts (the same layouts the readers use).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from darkstl.formats import flver as F
from darkstl.formats.binary import reverse_bits


# ---------------------------------------------------------------------------
# FLVER2


@dataclass
class SynthMesh:
    positions: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray
    indices: np.ndarray  # flat index list (triangle list or strip)
    material_index: int = 0
    strip: bool = False
    dynamic: int = 0
    bone_indices: np.ndarray | None = None  # (N, 4) per-vertex bone indices
    mesh_bones: list[int] = field(default_factory=list)
    default_bone: int = 0
    extra_face_sets: list[tuple[int, np.ndarray]] = field(default_factory=list)  # (flags, indices)


@dataclass
class SynthBone:
    name: str
    translation: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (0.0, 0.0, 0.0)
    scale: tuple = (1.0, 1.0, 1.0)
    parent: int = -1


def _utf16z(s: str, big: bool) -> bytes:
    return s.encode("utf-16-be" if big else "utf-16-le") + b"\0\0"


def build_flver(meshes: list[SynthMesh], materials: list[tuple[str, str, list[tuple[str, str]]]],
                bones: list[SynthBone] | None = None, version: int = 0x20014, big_endian: bool = False,
                uv_float: bool = False) -> bytes:
    e = ">" if big_endian else "<"
    bones = bones or [SynthBone("root")]
    textures = [t for _, _, texs in materials for t in texs]

    # Layout: position Float3, bone indices Byte4B, normal Byte4C, uv (UV short or Float2)
    layout = [(F.FLOAT3, F.SEM_POSITION), (F.BYTE4B, F.SEM_BONE_INDICES), (F.BYTE4C, F.SEM_NORMAL),
              ((F.FLOAT2, F.SEM_UV) if uv_float else (F.UV_SHORT, F.SEM_UV))]
    stride = sum(F.LAYOUT_SIZES[t] for t, _ in layout)
    uv_factor = 2048.0 if version >= 0x2000F else 1024.0

    face_sets = []  # (mesh index, flags, strip, indices)
    for mi, m in enumerate(meshes):
        face_sets.append((mi, 0, m.strip, np.asarray(m.indices)))
        for flags, idx in m.extra_face_sets:
            face_sets.append((mi, flags, m.strip, np.asarray(idx)))

    header_size = 0x80
    off_materials = header_size
    off_bones = off_materials + 0x20 * len(materials)
    off_meshes = off_bones + 0x80 * len(bones)
    off_facesets = off_meshes + 0x30 * len(meshes)
    off_vbs = off_facesets + 0x20 * len(face_sets)
    off_layouts = off_vbs + 0x20 * len(meshes)
    off_textures = off_layouts + 0x10
    off_members = off_textures + 0x20 * len(textures)
    tail = bytearray()
    cursor = off_members

    def add_tail(blob: bytes) -> int:
        nonlocal cursor
        pos = cursor
        tail.extend(blob)
        cursor += len(blob)
        return pos

    members_blob = b"".join(struct.pack(e + "iiIIi", 0, 0, t, s, 0) for t, s in layout)
    members_offset = add_tail(members_blob)
    mesh_bone_offsets, mesh_fs_offsets, mesh_vb_offsets = [], [], []
    for mi, m in enumerate(meshes):
        mesh_bone_offsets.append(add_tail(struct.pack(e + f"{len(m.mesh_bones)}i", *m.mesh_bones)))
        fs_ids = [i for i, fs in enumerate(face_sets) if fs[0] == mi]
        mesh_fs_offsets.append(add_tail(struct.pack(e + f"{len(fs_ids)}i", *fs_ids)))
        mesh_vb_offsets.append(add_tail(struct.pack(e + "i", mi)))
    string_offsets = {}
    for name, mtd, texs in materials:
        for s in (name, mtd, *[x for t in texs for x in t]):
            if s not in string_offsets:
                string_offsets[s] = add_tail(_utf16z(s, big_endian))
    for b in bones:
        if b.name not in string_offsets:
            string_offsets[b.name] = add_tail(_utf16z(b.name, big_endian))
    while cursor % 16:
        add_tail(b"\0")
    data_offset = cursor

    data = bytearray()
    index_offsets = []
    for _, _, _, idx in face_sets:
        index_offsets.append(len(data))
        data.extend(np.asarray(idx, dtype=e + "u2").tobytes())
        while len(data) % 16:
            data.append(0)
    vb_offsets = []
    for m in meshes:
        vb_offsets.append(len(data))
        n = len(m.positions)
        rec = np.zeros(n, dtype=[("p", e + "f4", 3), ("bi", "u1", 4), ("n", "u1", 4),
                                 ("uv", (e + "f4", 2) if uv_float else (e + "i2", 2))])
        rec["p"] = m.positions
        rec["bi"] = m.bone_indices if m.bone_indices is not None else 0
        rec["n"][:, :3] = np.clip(np.rint(np.asarray(m.normals) * 127 + 127), 0, 255)
        rec["n"][:, 3] = 127
        rec["uv"] = m.uvs if uv_float else np.rint(np.asarray(m.uvs) * uv_factor)
        assert rec.itemsize == stride
        data.extend(rec.tobytes())
        while len(data) % 16:
            data.append(0)

    out = bytearray()
    all_pos = np.concatenate([m.positions for m in meshes])
    out += b"FLVER\0" + (b"B\0" if big_endian else b"L\0")
    out += struct.pack(e + "iiiiiiii", version, data_offset, len(data), 0, len(materials), len(bones),
                       len(meshes), len(meshes))
    out += struct.pack(e + "3f", *all_pos.min(0)) + struct.pack(e + "3f", *all_pos.max(0))
    out += struct.pack(e + "ii", 0, 0)
    out += bytes([16, 1, 0, 0])
    out += struct.pack(e + "iiii", 0, len(face_sets), 1, len(textures))
    out += bytes([0, 0, 0, 0]) + struct.pack(e + "iii", 0, 0, 0) + b"\0" * 20
    assert len(out) == header_size, hex(len(out))

    tex_index = 0
    for name, mtd, texs in materials:
        out += struct.pack(e + "8i", string_offsets[name], string_offsets[mtd], len(texs), tex_index, 0, 0, 0, 0)
        tex_index += len(texs)
    for b in bones:
        out += struct.pack(e + "3fi3fhh3fhh", *b.translation, string_offsets[b.name], *b.rotation,
                           b.parent, -1, *b.scale, -1, -1)
        out += b"\0" * (0x80 - 48)
    for mi, m in enumerate(meshes):
        n_fs = sum(1 for fs in face_sets if fs[0] == mi)
        out += struct.pack(e + "B3xiiiiiiiiiii", m.dynamic, m.material_index, 0, 0, m.default_bone,
                           len(m.mesh_bones), 0, mesh_bone_offsets[mi], n_fs, mesh_fs_offsets[mi], 1,
                           mesh_vb_offsets[mi])
    for fi, (_, flags, strip, idx) in enumerate(face_sets):
        out += struct.pack(e + "I??hiiiiii", flags, strip, True, 0, len(idx), index_offsets[fi],
                           len(idx) * 2, 0, 16, 0)
    for mi, m in enumerate(meshes):
        out += struct.pack(e + "8i", mi, 0, stride, len(m.positions), 0, 0, stride * len(m.positions),
                           vb_offsets[mi])
    out += struct.pack(e + "iiii", len(layout), 0, 0, members_offset)
    for path, ttype in textures:
        out += struct.pack(e + "ii2fBBxxfff", string_offsets[path], string_offsets[ttype], 1.0, 1.0, 1, 0,
                           0.0, 0.0, 0.0)
    assert len(out) == off_members, (hex(len(out)), hex(off_members))
    out += tail
    assert len(out) == data_offset
    out += data
    return bytes(out)


# ---------------------------------------------------------------------------
# Binders


def build_bnd4(files: list[tuple[int, str, bytes]], unicode: bool = True) -> bytes:
    fmt = 0x02 | 0x04 | 0x08 | 0x20  # IDs | Names1 | Names2 | Compression
    raw_fmt = reverse_bits(fmt)  # stored bit-reversed when BitBigEndian is false
    header_size = 0x40
    entry_size = 0x24  # flags+pad, -1, csize, usize, offset(u32), id, name
    names_start = header_size + entry_size * len(files)
    names = bytearray()
    name_offsets = []
    for _, name, _ in files:
        name_offsets.append(names_start + len(names))
        names += name.encode("utf-16-le") + b"\0\0" if unicode else name.encode("cp932") + b"\0"
    data_start = names_start + len(names)
    data_start += (-data_start) % 16
    out = bytearray()
    out += b"BND4" + bytes([0, 0, 0, 0, 0, 0, 1, 0])  # unk04, unk05, pad, bigEndian=0, !bitBigEndian=1
    out += struct.pack("<iq8sqq", len(files), 0x40, b"07D7R6\0\0", entry_size, data_start)
    out += bytes([1 if unicode else 0, raw_fmt, 0, 0]) + struct.pack("<iq", 0, 0)
    blob = bytearray()
    for (fid, _, payload), name_off in zip(files, name_offsets):
        offset = data_start + len(blob)
        out += bytes([reverse_bits(0x40), 0, 0, 0]) + struct.pack("<iqqIiI", -1, len(payload), len(payload),
                                                                  offset, fid, name_off)
        blob += payload
        blob += b"\0" * ((-len(blob)) % 16)
    out += names
    out += b"\0" * (data_start - len(out))
    out += blob
    return bytes(out)


def build_bnd3(files: list[tuple[int, str, bytes]]) -> bytes:
    fmt = 0x02 | 0x04 | 0x08 | 0x20
    raw_fmt = reverse_bits(fmt)
    entry_size = 0x18
    names_start = 0x20 + entry_size * len(files)
    names = bytearray()
    name_offsets = []
    for _, name, _ in files:
        name_offsets.append(names_start + len(names))
        names += name.encode("cp932") + b"\0"
    data_start = names_start + len(names)
    data_start += (-data_start) % 16
    out = bytearray(b"BND3" + b"07D7R6\0\0" + bytes([raw_fmt, 0, 0, 0]))
    out += struct.pack("<iiii", len(files), names_start + len(names), 0, 0)
    blob = bytearray()
    for (fid, _, payload), name_off in zip(files, name_offsets):
        offset = data_start + len(blob)
        out += bytes([reverse_bits(0x40), 0, 0, 0]) + struct.pack("<iIiii", len(payload), offset, fid, name_off,
                                                                  len(payload))
        blob += payload
        blob += b"\0" * ((-len(blob)) % 16)
    out += names
    out += b"\0" * (data_start - len(out))
    out += blob
    return bytes(out)


def build_bxf4(files: list[tuple[int, str, bytes]]) -> tuple[bytes, bytes]:
    """Split binder: returns (bhd, bdt)."""
    bnd = build_bnd4(files)
    bhd = bytearray(bnd)
    bhd[0:4] = b"BHF4"
    # Re-point data offsets into a separate BDT that starts with a 0x30 byte header.
    bdt = bytearray(b"BDF4" + bytes([0, 0, 0, 0, 0, 0, 1, 0]) + struct.pack("<iq8sqq", 0, 0x30, b"07D7R6\0\0", 0, 0))
    entry_base = 0x40
    for i, (_, _, payload) in enumerate(files):
        ent = entry_base + i * 0x24
        new_off = len(bdt)
        struct.pack_into("<I", bhd, ent + 0x18, new_off)
        bdt += payload
        bdt += b"\0" * ((-len(bdt)) % 16)
    return bytes(bhd), bytes(bdt)


# ---------------------------------------------------------------------------
# Textures


def build_tpf(textures: list[tuple[str, bytes]], fmt: int = 0) -> bytes:
    """PC TPF holding complete DDS payloads."""
    count = len(textures)
    entries_start = 0x10
    entry_size = 0x14
    names_start = entries_start + entry_size * count
    names = bytearray()
    name_offsets = []
    for name, _ in textures:
        name_offsets.append(names_start + len(names))
        names += name.encode("utf-16-le") + b"\0\0"
    data_start = names_start + len(names)
    data_start += (-data_start) % 16
    out = bytearray(b"TPF\0")
    total = sum(len(d) for _, d in textures)
    out += struct.pack("<ii", total, count) + bytes([0, 3, 1, 0])
    blob = bytearray()
    for (name, dds_bytes), name_off in zip(textures, name_offsets):
        out += struct.pack("<IiBBBBIi", data_start + len(blob), len(dds_bytes), fmt, 0, 1, 0, name_off, 0)
        blob += dds_bytes
        blob += b"\0" * ((-len(blob)) % 16)
    out += names
    out += b"\0" * (data_start - len(out))
    out += blob
    return bytes(out)


def build_tpf_ps4(name: str, fmt: int, width: int, height: int, payload: bytes, dxgi: int) -> bytes:
    out = bytearray(b"TPF\0")
    out += struct.pack("<ii", len(payload), 1) + bytes([4, 3, 1, 0])
    entry_size = 4 + 4 + 4 + 4 + 8 + 4 + 4 + 4
    name_off = 0x10 + entry_size
    name_bytes = name.encode("utf-16-le") + b"\0\0"
    data_off = name_off + len(name_bytes)
    data_off += (-data_off) % 16
    out += struct.pack("<IiBBBBhhiiIii", data_off, len(payload), fmt, 0, 1, 0, width, height, 1, 0xD,
                       name_off, 0, dxgi)
    out += name_bytes
    out += b"\0" * (data_off - len(out))
    out += payload
    return bytes(out)


def rgba_dds(img: np.ndarray) -> bytes:
    from darkstl.formats.dds import build_dds

    h, w = img.shape[:2]
    return build_dds("R8G8B8A8", w, h, np.ascontiguousarray(img, np.uint8).tobytes())


def bc4_block(a0: int, a1: int, indices: list[int]) -> bytes:
    bits = 0
    for i, v in enumerate(indices):
        bits |= v << (3 * i)
    return bytes([a0, a1]) + bits.to_bytes(6, "little")


def bc1_block(c0: int, c1: int, indices: list[int]) -> bytes:
    bits = 0
    for i, v in enumerate(indices):
        bits |= v << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


# ---------------------------------------------------------------------------
# Geometry helpers


def grid_plane(n: int = 4, size: float = 1.0):
    """A flat n x n quad grid in the game's XZ plane (Y up), normals +Y, UVs 0..1."""
    xs = np.linspace(0, size, n + 1)
    gx, gz = np.meshgrid(xs, xs, indexing="xy")
    pos = np.stack([gx.ravel(), np.zeros(gx.size), gz.ravel()], 1).astype(np.float32)
    uv = np.stack([gx.ravel() / size, gz.ravel() / size], 1).astype(np.float32)
    nrm = np.tile([0.0, 1.0, 0.0], (len(pos), 1)).astype(np.float32)
    tris = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            b, c, d = a + 1, a + n + 1, a + n + 2
            tris += [a, c, b, b, c, d]
    return pos, nrm, uv, np.array(tris, np.int64)


def cube_mesh(size: float = 1.0):
    """24-vertex cube with per-face UVs and split normals (like a game mesh)."""
    s = size / 2
    faces = [
        ((1, 0, 0), [(s, -s, -s), (s, s, -s), (s, s, s), (s, -s, s)]),
        ((-1, 0, 0), [(-s, -s, s), (-s, s, s), (-s, s, -s), (-s, -s, -s)]),
        ((0, 1, 0), [(-s, s, -s), (-s, s, s), (s, s, s), (s, s, -s)]),
        ((0, -1, 0), [(-s, -s, s), (-s, -s, -s), (s, -s, -s), (s, -s, s)]),
        ((0, 0, 1), [(s, -s, s), (s, s, s), (-s, s, s), (-s, -s, s)]),
        ((0, 0, -1), [(-s, -s, -s), (-s, s, -s), (s, s, -s), (s, -s, -s)]),
    ]
    pos, nrm, uv, idx = [], [], [], []
    quad_uv = [(0, 1), (0, 0), (1, 0), (1, 1)]
    for fi, (n, verts) in enumerate(faces):
        base = len(pos)
        for v, t in zip(verts, quad_uv):
            pos.append(v)
            nrm.append(n)
            uv.append(t)
        # Winding chosen so that it disagrees with the normals for half the faces,
        # which exercises the orientation fix-up.
        if fi % 2 == 0:
            idx += [base, base + 1, base + 2, base, base + 2, base + 3]
        else:
            idx += [base, base + 2, base + 1, base, base + 3, base + 2]
    return (np.array(pos, np.float32), np.array(nrm, np.float32), np.array(uv, np.float32),
            np.array(idx, np.int64))
