"""Builders for synthetic RE Engine files (mesh, tex, mdf2, pak) used by the tests.

They write the layouts the readers in ``darkstl.importers.reengine.formats`` expect, for two
generations of the format: the Resident Evil 2 one (version 1808312334) and the Resident Evil 4
one (221108797, with streamed buffers).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

from darkstl.importers.reengine.formats import mesh as M
from darkstl.importers.reengine.formats import pak as P

RE2_MESH, RE4_MESH, RE8_MESH = 1808312334, 221108797, 2101050001
RE2_TEX, RE4_TEX, RE8_TEX = 10, 143221013, 30
RE2_MDF, RE4_MDF = 10, 32


@dataclass(eq=False)
class SynthSub:
    positions: np.ndarray  # (n, 3)
    normals: np.ndarray  # (n, 3)
    uvs: np.ndarray  # (n, 2), V down
    triangles: np.ndarray  # (m, 3)
    material: int = 0
    group: int = 0
    streamed: bool = False  # in the streamed buffer (RE4 era), not the file's own


@dataclass
class _Buffer:
    subs: list[SynthSub] = field(default_factory=list)
    data: bytes = b""
    faces: bytes = b""
    elements: list[tuple[int, int, int]] = field(default_factory=list)  # (type, stride, offset)
    starts: list[tuple[int, int]] = field(default_factory=list)  # (vertex_start, face_start) per sub


def _pad16(b: bytearray) -> None:
    while len(b) % 16:
        b.append(0)


def _f16(a: np.ndarray) -> bytes:
    return np.asarray(a, np.float32).astype("<f2").tobytes()


def _pack_buffer(subs: list[SynthSub]) -> _Buffer:
    buf = _Buffer(subs)
    pos, nt, uv, faces = bytearray(), bytearray(), bytearray(), bytearray()
    v = f = 0
    for s in subs:
        n = len(s.positions)
        pos += np.asarray(s.positions, "<f4").tobytes()
        nortan = np.zeros((n, 2, 4), np.int8)
        nortan[:, 0, :3] = np.clip(np.round(np.asarray(s.normals) * 127), -127, 127)
        nortan[:, 1, 0] = 127
        nortan[:, 1, 3] = 127
        nt += nortan.tobytes()
        uv += _f16(s.uvs)
        tris = np.asarray(s.triangles, np.int64)
        faces += tris.astype("<u2").tobytes()
        if (len(tris) * 3) % 2:  # each sub-mesh's indices padded to 4 bytes
            faces += b"\0\0"
        buf.starts.append((v, f))
        v += n
        f += len(tris) * 3 + ((len(tris) * 3) % 2)
    data = bytearray()
    buf.elements.append((M.EL_POSITION, 12, 0))
    data += pos
    _pad16(data)
    buf.elements.append((M.EL_NORTAN, 8, len(data)))
    data += nt
    _pad16(data)
    buf.elements.append((M.EL_UV, 4, len(data)))
    data += uv
    _pad16(data)
    buf.data = bytes(data)
    buf.faces = bytes(faces)
    return buf


def build_mesh(subs: list[SynthSub], materials: list[str], version: int = RE2_MESH,
               bones: list[str] = ()) -> tuple[bytes, bytes | None]:
    """A mesh file with one level of detail; returns ``(mesh bytes, streaming bytes or None)``.

    Sub-meshes marked ``streamed`` go in the streaming file (RE4-era versions only)."""
    era = M.era_of(version)
    own = _pack_buffer([s for s in subs if not s.streamed or era < M.ERA_SF6])
    streamed = None
    if era >= M.ERA_SF6 and any(s.streamed for s in subs):
        streamed = _pack_buffer([s for s in subs if s.streamed])
    names = list(materials) + list(bones)
    out = bytearray(b"\0" * 0x100)  # header written last

    # main mesh header
    lod_offset_pos = len(out)
    main = bytearray()
    main += struct.pack("<4bHbB", 1, len(materials), 1, 0, len(subs), 0, 0)
    if era < M.ERA_RE8:
        main += b"\0" * 8
    main += struct.pack("<4f", 0, 0, 0, 1)
    allpos = np.concatenate([np.asarray(s.positions) for s in subs]) if subs else np.zeros((1, 3))
    main += struct.pack("<4f", *allpos.min(0), 0) + struct.pack("<4f", *allpos.max(0), 0)
    main += struct.pack("<Q", 0)
    lod_ptr = len(main)
    main += struct.pack("<Q", 0)  # LOD 0 offset, patched
    while len(main) % 16:
        main.append(0)
    lod_pos = lod_offset_pos + len(main)
    struct.pack_into("<Q", main, lod_ptr, lod_pos)
    # LOD header: groups by id
    groups: dict[int, list[int]] = {}
    for i, s in enumerate(subs):
        groups.setdefault(s.group, []).append(i)
    lod = bytearray(struct.pack("<2BHfQ", len(groups), 0, 0, 0.0, 0))
    group_ptr = len(lod)
    lod += b"\0" * (8 * len(groups))
    while len(lod) % 16:
        lod.append(0)
    group_offsets = []
    for gid, members in groups.items():
        group_offsets.append(lod_pos + len(lod))
        g = struct.pack("<2B3HII", gid, len(members), 0, 0, 0, sum(len(subs[i].positions) for i in members),
                        sum(len(subs[i].triangles) * 3 for i in members))
        lod += g
        for i in members:
            s = subs[i]
            buffer_index = 1 if (s.streamed and streamed is not None) else 0
            src = streamed if buffer_index else own
            v_start, f_start = src.starts[next(k for k, x in enumerate(src.subs) if x is s)]
            sub = struct.pack("<4B", s.material, 0, buffer_index, 0)
            if era >= M.ERA_DR:
                sub += struct.pack("<I", 0)
            sub += struct.pack("<3I", len(s.triangles) * 3, f_start, v_start)
            if era >= M.ERA_RE8:
                sub += struct.pack("<2I", 0, 0)
            if era >= M.ERA_DD2NEW:
                sub += struct.pack("<I", 0)
            lod += sub
    for i, off in enumerate(group_offsets):
        struct.pack_into("<Q", lod, group_ptr + 8 * i, off)
    while len(lod) % 16:
        lod.append(0)
    out += main + lod
    mesh_group_offset = lod_offset_pos

    # bones (names only matter here)
    skeleton_offset = 0
    if bones:
        skeleton_offset = len(out)
        n = len(bones)
        sk = bytearray(struct.pack("<2IQ4Q", n, n, 0, 0, 0, 0, 0))
        sk += b"".join(struct.pack("<H", i) for i in range(n))
        while len(sk) % 16:
            sk.append(0)
        bone_hdr = skeleton_offset + len(sk)
        for i in range(n):
            sk += struct.pack("<H7h", i, i - 1, -1, -1, -1, 0, 0, 0)
        local = skeleton_offset + len(sk)
        ident = np.eye(4, dtype="<f4").tobytes()
        sk += ident * n * 3
        struct.pack_into("<4Q", sk, 16, bone_hdr, local, local + 64 * n, local + 128 * n)
        out += sk

    # name tables
    material_names_offset = len(out)
    out += b"".join(struct.pack("<H", i) for i in range(len(materials)))
    _pad16(out)
    bone_names_offset = len(out)
    out += b"".join(struct.pack("<H", len(materials) + i) for i in range(len(bones)))
    _pad16(out)
    name_offsets_offset = len(out)
    out += b"\0" * (8 * len(names))
    _pad16(out)
    for i, nm in enumerate(names):
        struct.pack_into("<Q", out, name_offsets_offset + 8 * i, len(out))
        out += nm.encode("utf-8") + b"\0"
    _pad16(out)

    # streaming info (RE4 era)
    streaming_info_offset = 0
    stream_ranges: list[tuple[int, int]] = []
    if streamed is not None:
        streaming_info_offset = len(out)
        entry_offset = streaming_info_offset + 16
        out += struct.pack("<2IQ", 1, 0, entry_offset)
        stream_ranges = [(0, len(streamed.data) + len(streamed.faces))]
        out += struct.pack("<2I", *stream_ranges[0])
        _pad16(out)

    # mesh buffer header
    mesh_offset = len(out)
    mb = bytearray()
    if era < M.ERA_SF6:
        hdr_size = 48 + (16 if era > M.ERA_RE8 else 0)
        element_offset = mesh_offset + hdr_size
        elements = b"".join(struct.pack("<2HI", *e) for e in own.elements)
        vertex_offset = (element_offset + len(elements) + 15) & ~15
        face_offset = (vertex_offset + len(own.data) + 15) & ~15
        mb += struct.pack("<3Q", element_offset, vertex_offset, face_offset)
        if era > M.ERA_RE8:
            mb += struct.pack("<Q", 0)
        mb += struct.pack("<2I2HIIhh", len(own.data), len(own.faces), len(own.elements), len(own.elements),
                          face_offset - vertex_offset + len(own.faces), 0, 0, -1)
        if era > M.ERA_RE8:
            mb += struct.pack("<Q", 0)
        assert len(mb) == hdr_size
        mb += elements
        while mesh_offset + len(mb) < vertex_offset:
            mb.append(0)
        mb += own.data
        while mesh_offset + len(mb) < face_offset:
            mb.append(0)
        mb += own.faces
    else:
        n_el = len(own.elements)
        hdr_size = 8 + 8 + 8 + 4 + 4 + 2 + 2 + 4 + 4 + 2 + 2 + 8 * 4
        stream_entries = 60 * len(stream_ranges)
        element_offset = mesh_offset + hdr_size + stream_entries
        stream_el_offset = element_offset + 8 * n_el
        stream_el_offset = (stream_el_offset + 15) & ~15
        after_elements = stream_el_offset + (16 * len(stream_ranges) if stream_ranges else 0) * ((8 * n_el + 15) // 16)
        vertex_offset = (after_elements + 15) & ~15
        face_size = len(own.faces)
        mb += struct.pack("<3Q2I2H", element_offset, vertex_offset, 0, len(own.data) + face_size, len(own.data),
                          n_el, n_el)
        mb += struct.pack("<IIhh", len(own.data) + face_size, 0, 0, -1)
        mb += struct.pack("<4Q", 0, 0, stream_el_offset, 0)
        assert len(mb) == hdr_size
        for start, length in stream_ranges:
            mb += struct.pack("<Q2I2H", 0, length, len(streamed.data), n_el, n_el)
            mb += struct.pack("<9I", length, length, 0, 0, 0, 0, 0, 0, 0)
            mb += struct.pack("<I", 0)
        mb += b"".join(struct.pack("<2HI", *e) for e in own.elements)
        while mesh_offset + len(mb) < stream_el_offset:
            mb.append(0)
        if streamed is not None:
            mb += b"".join(struct.pack("<2HI", *e) for e in streamed.elements)
        while mesh_offset + len(mb) < vertex_offset:
            mb.append(0)
        mb += own.data
        mb += own.faces
    out += mb
    _pad16(out)

    # header
    h = bytearray(struct.pack("<4I", M.MAGIC, version, len(out), 0))
    if era < M.ERA_SF6:
        h += struct.pack("<HhI", 2 if bones else 0, len(names), 0)
        h += struct.pack("<13Q", mesh_group_offset, 0, 0, skeleton_offset, 0, 0, 0, mesh_offset, 0,
                         material_names_offset, bone_names_offset, 0, name_offsets_offset)
    elif era < M.ERA_ONI2:
        h += struct.pack("<HhhhII", 2 if bones else 0, 0, len(names), 0, 0, 0)
        h += struct.pack("<13Q", mesh_group_offset, 0, 0, 0, 0, mesh_offset, 0, 0, 0, skeleton_offset,
                         material_names_offset, bone_names_offset, 0)
        if era < M.ERA_DD2:
            h += struct.pack("<2Q", streaming_info_offset, name_offsets_offset)
        else:
            h += struct.pack("<3Q", name_offsets_offset, 0, streaming_info_offset)
        h += struct.pack("<2Q", 0, 0)
    else:
        raise NotImplementedError("synthetic meshes of the Wilds era")
    assert len(h) <= 0x100
    out[:len(h)] = h
    return bytes(out), (streamed.data + streamed.faces) if streamed is not None else None


def build_tex(width: int, height: int, dxgi: int, surfaces: list[bytes], version: int = RE2_TEX,
              pad_rows: int = 0) -> bytes:
    """A tex file with one image and ``len(surfaces)`` mips. ``pad_rows`` adds that many bytes of padding
    to every row of the first mip (RE textures pad rows to a pitch)."""
    from darkstl.textures import dds

    codec = dds.DXGI_CODECS[dxgi]
    h = bytearray(struct.pack("<2I3H", 0x00584554, version, width, height, 1))
    mips = len(surfaces)
    if version > 11 and version != 190820018:
        h += struct.pack("<2B", 1, mips * 16)
    else:
        h += struct.pack("<2B", mips, 1)
    h += struct.pack("<IiI2BH", dxgi, -1, 0, 0, 0, 0)
    if version > 27 and version != 190820018:
        h += struct.pack("<2B3H", 0, 0, 0, 7, 1)
    table_pos = len(h)
    h += b"\0" * (16 * mips)
    payloads = []
    for level, raw in enumerate(surfaces):
        w = max(1, width >> level)
        hgt = max(1, height >> level)
        if codec in dds.BLOCK_BYTES:
            rows, row_bytes = max(1, (hgt + 3) // 4), max(1, (w + 3) // 4) * dds.BLOCK_BYTES[codec]
        else:
            rows, row_bytes = hgt, w * dds.PIXEL_BYTES[codec]
        pad = pad_rows if level == 0 else 0
        scanline = row_bytes + pad
        if pad:
            arr = np.frombuffer(raw, np.uint8).reshape(rows, row_bytes)
            raw = np.concatenate([arr, np.zeros((rows, pad), np.uint8)], axis=1).tobytes()
        payloads.append((scanline, raw))
    offset = len(h)
    for i, (scanline, raw) in enumerate(payloads):
        struct.pack_into("<QII", h, table_pos + 16 * i, offset, scanline, len(raw))
        offset += len(raw)
    for _, raw in payloads:
        h += raw
    return bytes(h)


def build_mdf(materials: list[tuple[str, str, list[tuple[str, str]]]], version: int = RE2_MDF) -> bytes:
    """An mdf2 file: ``[(material name, shader path, [(slot type, texture path), ...]), ...]``."""
    def w(s: str) -> bytes:
        return s.encode("utf-16-le") + b"\0\0"

    entry_size = 64 if version < 19 else 80
    if version >= 31:
        entry_size = 100
    if version >= 51:
        entry_size = 108
    tex_entry = 24 if version < 13 else 32
    out = bytearray(struct.pack("<IHHQ", 0x0046444D, version, len(materials), 0))
    entries_pos = len(out)
    out += b"\0" * (entry_size * len(materials))
    tex_table_pos = len(out)
    out += b"\0" * (tex_entry * sum(len(t) for _, _, t in materials))
    while len(out) % 16:
        out.append(0)
    strings: dict[str, int] = {}

    def string(s: str) -> int:
        if s not in strings:
            strings[s] = len(out)
            out.extend(w(s))
        return strings[s]

    tex_cursor = tex_table_pos
    for i, (name, shader, textures) in enumerate(materials):
        e = bytearray()
        e += struct.pack("<QIiii", string(name), 0, 0, 0, len(textures))
        if version >= 19:
            e += struct.pack("<2i", 0, 0)
        if version >= 31:
            e += struct.pack("<i", 0)
        e += struct.pack("<ii", 0, 0)
        if version >= 31:
            e += struct.pack("<2i", 0, 0)
        if version >= 51:
            e += struct.pack("<Q", 0)
        e += struct.pack("<QQ", 0, tex_cursor)
        if version >= 19:
            e += struct.pack("<Q", 0)
        e += struct.pack("<QQ", 0, string(shader))
        if version >= 31:
            e += struct.pack("<Q", 0)
        assert len(e) == entry_size, (len(e), entry_size)
        out[entries_pos + entry_size * i:entries_pos + entry_size * (i + 1)] = e
        for slot, path in textures:
            t = struct.pack("<QIIQ", string(slot), 0, 0, string(path))
            if version >= 13:
                t += b"\0" * 8
            out[tex_cursor:tex_cursor + tex_entry] = t
            tex_cursor += tex_entry
    return bytes(out)


def build_pak(files: list[tuple[str, bytes]], compression: int = P.COMPRESSION_NONE, version: int = 4) -> bytes:
    """A pak archive of ``[(path, data), ...]``, every entry stored with ``compression``."""
    entry_size = 48 if version == 4 else 24
    header = struct.pack("<4sBBHII", b"KPKA", version, 0 if version == 2 else 2, 0, len(files), 0)
    table = bytearray()
    body = bytearray()
    offset = len(header) + entry_size * len(files)
    hashes = P.hash_names([name for name, _ in files])
    for (name, data), h in zip(files, hashes):
        h = int(h)
        if compression == P.COMPRESSION_DEFLATE:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            stored = c.compress(data) + c.flush()
        elif compression == P.COMPRESSION_ZSTD:
            import zstandard

            stored = zstandard.ZstdCompressor().compress(data)
        else:
            stored = data
        if version == 4:
            table += struct.pack("<IIQQQqQ", h & 0xFFFFFFFF, h >> 32, offset + len(body), len(stored), len(data),
                                 compression, 0)
        else:
            assert compression == P.COMPRESSION_NONE
            table += struct.pack("<IIQQ", h & 0xFFFFFFFF, h >> 32, offset + len(body), len(data))
        body += stored
        while len(body) % 16:
            body.append(0)
    return header + bytes(table) + bytes(body)


def quad(size: float = 1.0, z: float = 0.0, material: int = 0, group: int = 0, streamed: bool = False,
         segments: int = 1) -> SynthSub:
    """A square in the XY plane (RE Engine frame: Y up, so it stands upright), facing +Z."""
    n = segments + 1
    xs, ys = np.meshgrid(np.linspace(0, size, n), np.linspace(0, size, n))
    positions = np.stack([xs.ravel(), ys.ravel(), np.full(n * n, z)], axis=1)
    normals = np.tile([0.0, 0.0, 1.0], (n * n, 1))
    uvs = np.stack([xs.ravel() / size, 1 - ys.ravel() / size], axis=1)
    tris = []
    for j in range(segments):
        for i in range(segments):
            a = j * n + i
            tris += [[a, a + 1, a + n], [a + 1, a + n + 1, a + n]]
    return SynthSub(positions.astype(np.float32), normals.astype(np.float32), uvs.astype(np.float32),
                    np.array(tris, np.int64), material, group, streamed)


def bc1_flat(width: int, height: int, color565: int) -> bytes:
    """A BC1 surface of one colour."""
    block = struct.pack("<HHI", color565, color565, 0)
    return block * (max(1, (width + 3) // 4) * max(1, (height + 3) // 4))


def rgba_surface(img: np.ndarray) -> bytes:
    return np.ascontiguousarray(img, np.uint8).tobytes()
