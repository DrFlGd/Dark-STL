"""RenderWare GTA files built byte by byte: chunks, clumps (.dff), texture dictionaries (.txd),
animation packages (.ifp), text (.gxt), and whole fake game folders (III and San Andreas)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from darkstl.importers.renderware.formats import chunks as ck
from darkstl.importers.renderware.formats.img import write_img

SA = 0x36003
III = 0x31001
VC = 0x34003


def chunk(kind: int, body: bytes, version: int = SA) -> bytes:
    return struct.pack("<3I", kind, len(body), ck.library_id(version)) + body


def string(text: str, version: int = SA) -> bytes:
    raw = text.encode("latin-1") + b"\0"
    return chunk(ck.STRING, raw + b"\0" * (-len(raw) % 4), version)


def ext(*children: bytes, version: int = SA) -> bytes:
    return chunk(ck.EXTENSION, b"".join(children), version)


# ---------------------------------------------------------------------------
# clumps


def frame_list(frames: list[dict], version: int = SA) -> bytes:
    """frames: dicts with name, parent, pos (3), rot (3x3 rows right/up/at, optional), bone_id,
    hierarchy [(id, index, flags)]."""
    body = struct.pack("<i", len(frames))
    for f in frames:
        rot = np.asarray(f.get("rot", np.eye(3)), np.float32).reshape(9)
        body += struct.pack("<9f3fiI", *rot, *f.get("pos", (0, 0, 0)), f.get("parent", -1), 0)
    out = chunk(ck.STRUCT, body, version)
    for f in frames:
        parts = []
        if f.get("name"):
            parts.append(chunk(ck.FRAME_NAME, f["name"].encode("latin-1"), version))
        if "bone_id" in f:
            h = f.get("hierarchy") or []
            hb = struct.pack("<Iii", 0x100, f["bone_id"], len(h))
            if h:
                hb += struct.pack("<II", 0, 36) + b"".join(struct.pack("<iiI", *n) for n in h)
            parts.append(chunk(ck.HANIM_PLG, hb, version))
        out += ext(*parts, version=version)
    return chunk(ck.FRAME_LIST, out, version)


def material(colour=(255, 255, 255, 255), texture: str | None = None, mask: str = "", version: int = SA) -> bytes:
    body = struct.pack("<i4Bii", 0, *colour, 0, 1 if texture else 0)
    if version > 0x30400:
        body += struct.pack("<3f", 1, 1, 1)
    out = chunk(ck.STRUCT, body, version)
    if texture:
        tex = chunk(ck.STRUCT, struct.pack("<I", 0x1106), version) + string(texture, version) + \
            string(mask, version) + ext(version=version)
        out += chunk(ck.TEXTURE, tex, version)
    out += ext(version=version)
    return chunk(ck.MATERIAL, out, version)


def geometry(positions, triangles, materials: list[bytes], tri_material=None, uvs=None, normals=None,
             skin: dict | None = None, colours=None, version: int = SA) -> bytes:
    positions = np.asarray(positions, np.float32)
    triangles = np.asarray(triangles, np.int64)
    n, t = len(positions), len(triangles)
    flags = 0x02 | 0x20
    if uvs is not None:
        flags |= 0x04 | 0x40
    if normals is not None:
        flags |= 0x10
    if colours is not None:
        flags |= 0x08
    body = struct.pack("<Iiii", flags | ((1 if uvs is not None else 0) << 16), t, n, 1)
    if version < 0x34000:
        body += struct.pack("<3f", 1, 1, 1)
    if colours is not None:
        body += np.asarray(colours, np.uint8).tobytes()
    if uvs is not None:
        body += np.asarray(uvs, np.float32).tobytes()
    tm = np.zeros(t, np.int64) if tri_material is None else np.asarray(tri_material)
    raw = np.stack([triangles[:, 1], triangles[:, 0], tm, triangles[:, 2]], 1).astype("<u2")
    body += raw.tobytes()
    centre = positions.mean(0) if n else np.zeros(3)
    body += struct.pack("<4f", *centre, 1.0) + struct.pack("<ii", 1, 1 if normals is not None else 0)
    body += positions.tobytes()
    if normals is not None:
        body += np.asarray(normals, np.float32).tobytes()
    mats = chunk(ck.STRUCT, struct.pack("<i", len(materials)) + struct.pack(f"<{len(materials)}i",
                                                                           *([-1] * len(materials))), version)
    mats += b"".join(materials)
    out = chunk(ck.STRUCT, body, version) + chunk(ck.MATERIAL_LIST, mats, version)
    plugins = []
    if skin is not None:
        bones = skin["bones"]
        sb = struct.pack("<4B", bones, bones, 4, 0) + bytes(range(bones))
        sb += np.asarray(skin["indices"], np.uint8).tobytes() + np.asarray(skin["weights"], np.float32).tobytes()
        for m in skin["inverse"]:  # 4x4 column-vector matrices, stored as RenderWare rows
            m = np.asarray(m, np.float64)
            rows = np.zeros((4, 4), np.float32)
            rows[0, :3], rows[1, :3], rows[2, :3], rows[3, :3] = m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3]
            rows[3, 3] = 1
            sb += rows.tobytes()
        sb += struct.pack("<3i", 0, 0, 0)
        plugins.append(chunk(ck.SKIN_PLG, sb, version))
    out += ext(*plugins, version=version)
    return chunk(ck.GEOMETRY, out, version)


def clump(frames: list[dict], geometries: list[bytes], atomics: list[tuple[int, int]], version: int = SA) -> bytes:
    body = struct.pack("<i", len(atomics)) + (struct.pack("<ii", 0, 0) if version > 0x33000 else b"")
    out = chunk(ck.STRUCT, body, version) + frame_list(frames, version)
    out += chunk(ck.GEOMETRY_LIST, chunk(ck.STRUCT, struct.pack("<i", len(geometries)), version)
                 + b"".join(geometries), version)
    for frame, geo in atomics:
        out += chunk(ck.ATOMIC, chunk(ck.STRUCT, struct.pack("<iiII", frame, geo, 5, 0), version)
                     + ext(version=version), version)
    out += ext(version=version)
    return chunk(ck.CLUMP, out, version)


def box(size=(1.0, 1.0, 1.0), centre=(0.0, 0.0, 0.0)):
    """A box's corners (with UVs) and outward triangles."""
    sx, sy, sz = (s / 2 for s in size)
    cx, cy, cz = centre
    p = np.array([[x, y, z] for z in (-sz, sz) for y in (-sy, sy) for x in (-sx, sx)], np.float32) + (cx, cy, cz)
    t = np.array([[0, 2, 1], [1, 2, 3], [4, 5, 6], [5, 7, 6], [0, 1, 4], [1, 5, 4],
                  [2, 6, 3], [3, 6, 7], [0, 4, 2], [2, 4, 6], [1, 3, 5], [3, 7, 5]], np.int64)
    uv = (p[:, :2] - p[:, :2].min(0)) / np.maximum(np.ptp(p[:, :2], 0), 1e-6)
    return p, t, uv.astype(np.float32)


# ---------------------------------------------------------------------------
# texture dictionaries


def bc1_block(c0: int, c1: int, bits: int = 0) -> bytes:
    return struct.pack("<HHI", c0, c1, bits)


def native(name: str, width: int, height: int, codec: str, levels: list[bytes], platform: int = 9,
           palette: bytes | None = None, alpha: bool = False, mask: str = "", version: int = SA) -> bytes:
    raster = {"BC1": 0x200, "BC3": 0x500, "8888": 0x500, "PAL8": 0x2500, "1555": 0x100, "565": 0x200}[codec]
    if platform == 8:
        fourcc = struct.pack("<I", 1 if alpha else 0)
        comp = {"BC1": 1, "BC3": 5}.get(codec, 0)
    else:
        fourcc = {"BC1": b"DXT1", "BC3": b"DXT5"}.get(codec, struct.pack("<I", 21))
        comp = (8 if codec in ("BC1", "BC3") else 0) | (1 if alpha else 0)
    body = struct.pack("<II", platform, 0x1106) + name.encode().ljust(32, b"\0") + mask.encode().ljust(32, b"\0")
    body += struct.pack("<I", raster) + fourcc + struct.pack("<HHBBBB", width, height, 16, len(levels), 4, comp)
    if palette is not None:
        body += palette
    for lv in levels:
        body += struct.pack("<I", len(lv)) + lv
    return chunk(ck.TEXTURE_NATIVE, chunk(ck.STRUCT, body, version) + ext(version=version), version)


def txd(natives: list[bytes], version: int = SA) -> bytes:
    return chunk(ck.TEXTURE_DICTIONARY, chunk(ck.STRUCT, struct.pack("<HH", len(natives), 2), version)
                 + b"".join(natives) + ext(version=version), version)


def solid_bc1(name: str, rgb565: int, size: int = 8, **kw) -> bytes:
    blocks = (size // 4) * (size // 4)
    return native(name, size, size, "BC1", [bc1_block(rgb565, rgb565) * blocks], **kw)


# ---------------------------------------------------------------------------
# animations


def _q(angle: float, axis=(0, 0, 1)):
    """A quaternion (x, y, z, w) as the files store it: conjugated."""
    s = np.sin(angle / 2)
    x, y, z = (np.asarray(axis, np.float64) * s)
    return (-x, -y, -z, np.cos(angle / 2))


def anp3(name: str, anims: list[tuple[str, list[dict]]], compressed: bool = False) -> bytes:
    """anims: (name, bones) with bones dicts: name, bone_id, keys [(time, angle about z, (tx,ty,tz) or None)]."""
    body = name.encode().ljust(24, b"\0") + struct.pack("<I", len(anims))
    for aname, bones in anims:
        frames = b""
        for b in bones:
            keys = b["keys"]
            translated = any(k[2] is not None for k in keys)
            frames += b["name"].encode().ljust(24, b"\0") + struct.pack("<IIi", 2 if translated else 1, len(keys),
                                                                          b["bone_id"])
            for t, angle, tr in keys:
                q = _q(angle)
                if compressed:
                    frames += struct.pack("<5h", *(int(round(v * 4096)) for v in q), int(round(t * 60)))
                    if translated:
                        frames += struct.pack("<3h", *(int(round(v * 1024)) for v in (tr or (0, 0, 0))))
                else:
                    frames += struct.pack("<5f", *q, t)
                    if translated:
                        frames += struct.pack("<3f", *(tr or (0, 0, 0)))
        body += aname.encode().ljust(24, b"\0") + struct.pack("<III", len(bones), len(frames), 1 if compressed else 0)
        body += frames
    return b"ANP3" + struct.pack("<I", len(body)) + body


def _block(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<I", len(body)) + body + b"\0" * (-len(body) % 4)


def anpk(name: str, anims: list[tuple[str, list[dict]]]) -> bytes:
    """GTA III / Vice City animations; bones as for :func:`anp3` (matched by name)."""
    inner = _block(b"INFO", struct.pack("<i", len(anims)) + name.encode() + b"\0")
    for aname, bones in anims:
        objs = _block(b"INFO", struct.pack("<ii", len(bones), 0))
        for b in bones:
            keys = b["keys"]
            translated = any(k[2] is not None for k in keys)
            anim = b["name"].encode().ljust(28, b"\0") + struct.pack("<iiii", len(keys), 0, -1, -1)
            frames = b""
            for t, angle, tr in keys:
                frames += struct.pack("<4f", *_q(angle))
                if translated:
                    frames += struct.pack("<3f", *(tr or (0, 0, 0)))
                frames += struct.pack("<f", t)
            cpan = _block(b"ANIM", anim) + _block(b"KRT0" if translated else b"KR00", frames)
            objs += _block(b"CPAN", cpan)
        inner += _block(b"NAME", aname.encode() + b"\0") + _block(b"DGAN", objs)
    return b"ANPK" + struct.pack("<I", len(inner)) + inner


# ---------------------------------------------------------------------------
# text


def gxt_iii(entries: dict[str, str]) -> bytes:
    tdat, keys = b"", b""
    for k, v in entries.items():
        keys += struct.pack("<I", len(tdat)) + k.encode().ljust(8, b"\0")
        tdat += v.encode("utf-16-le") + b"\0\0"
    return b"TKEY" + struct.pack("<I", len(keys)) + keys + b"TDAT" + struct.pack("<I", len(tdat)) + tdat


def gxt_sa(entries: dict[str, str]) -> bytes:
    from darkstl.importers.renderware.formats.gxt import key_hash

    tdat, keys = b"", b""
    for k, v in entries.items():
        keys += struct.pack("<II", len(tdat), key_hash(k))
        tdat += v.encode("latin-1") + b"\0"
    main = b"TKEY" + struct.pack("<I", len(keys)) + keys + b"TDAT" + struct.pack("<I", len(tdat)) + tdat
    table = b"MAIN".ljust(8, b"\0") + struct.pack("<I", 4 + 8 + 12)
    return struct.pack("<HH", 4, 8) + b"TABL" + struct.pack("<I", len(table)) + table + main


# ---------------------------------------------------------------------------
# whole game folders

PRIMARY, SECONDARY = (60, 255, 0, 255), (255, 0, 175, 255)
RED565, BLUE565, GREY565 = 0xF800, 0x001F, 0x8410


def car(version: int = SA) -> bytes:
    """A car: chassis (painted body + textured glass), a door (undamaged and damaged), a low-detail
    body, and one wheel on the right front, with four wheel dummies."""
    body_p, body_t, body_uv = box((2.0, 4.0, 1.0), (0, 0, 0.8))
    door_p, door_t, door_uv = box((0.1, 1.0, 0.6), (1.05, 0.5, 0.8))
    wheel_p, wheel_t, wheel_uv = box((0.3, 0.6, 0.6))
    frames = [
        dict(name="landstal", parent=-1),
        dict(name="chassis_dummy", parent=0),
        dict(name="chassis", parent=1),
        dict(name="door_lf_dummy", parent=1),
        dict(name="door_lf_ok", parent=3),
        dict(name="door_lf_dam", parent=3),
        dict(name="chassis_vlo", parent=1),
        dict(name="wheel_rf_dummy", parent=1, pos=(1.0, 1.5, 0.3)),
        dict(name="wheel", parent=7),
        dict(name="wheel_lf_dummy", parent=1, pos=(-1.0, 1.5, 0.3)),
        dict(name="wheel_rb_dummy", parent=1, pos=(1.0, -1.5, 0.3)),
        dict(name="wheel_lb_dummy", parent=1, pos=(-1.0, -1.5, 0.3)),
        dict(name="wheel_lm_dummy", parent=1),  # a middle dummy most cars carry, with no wheel drawn at it
    ]
    tri_mat = np.array([0] * 10 + [1] * 2)
    geos = [
        geometry(body_p, body_t, [material(PRIMARY, "body", version=version), material((255, 255, 255, 255), "glass",
                                                                                      version=version)],
                 tri_mat, body_uv, version=version),
        geometry(door_p, door_t, [material(SECONDARY, "body", version=version)], None, door_uv, version=version),
        geometry(door_p + (0, 0, -0.05), door_t, [material(SECONDARY, "body", version=version)], None, door_uv,
                 version=version),
        geometry(body_p * 0.98, body_t, [material(PRIMARY, version=version)], None, None, version=version),
        geometry(wheel_p, wheel_t, [material((255, 255, 255, 255), "tyre", version=version)], None, wheel_uv,
                 version=version),
    ]
    return clump(frames, geos, [(2, 0), (4, 1), (5, 2), (6, 3), (8, 4)], version)


def ped(version: int = SA, bone_ids=(0, 1, 2)) -> bytes:
    """A skinned ped: three bones in a chain (root, pelvis at 1 m, head at 1.7 m), a box per bone."""
    names = ["Root", "Pelvis", "Head"]
    heights = [0.0, 1.0, 1.7]
    hierarchy = [(bone_ids[i], i, 0) for i in range(3)]
    frames = [dict(name="male01", parent=-1)]
    for i in range(3):
        f = dict(name=names[i], parent=i, pos=(0, 0, heights[i] - (heights[i - 1] if i else 0)),
                 bone_id=bone_ids[i])
        if i == 0:
            f["hierarchy"] = hierarchy
        frames.append(f)
    p1, t1, uv1 = box((0.4, 0.3, 0.6), (0, 0, 1.0))
    p2, t2, uv2 = box((0.3, 0.3, 0.3), (0, 0, 1.75))
    p = np.concatenate([p1, p2])
    t = np.concatenate([t1, t2 + len(p1)])
    uv = np.concatenate([uv1, uv2])
    idx = np.zeros((len(p), 4), np.uint8)
    w = np.zeros((len(p), 4), np.float32)
    idx[:8, 0], idx[8:, 0] = 1, 2
    w[:, 0] = 1
    inverse = [np.linalg.inv(_translate(0, 0, h)) for h in heights]
    g = geometry(p, t, [material((255, 255, 255, 255), "male01", version=version)], None, uv,
                 skin=dict(bones=3, indices=idx, weights=w, inverse=inverse), version=version)
    return clump(frames, [g], [(0, 0)], version)


def _translate(x, y, z):
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def prop(version: int = SA, texture: str = "crate") -> bytes:
    p, t, uv = box((1, 1, 1), (0, 0, 0.5))
    return clump([dict(name="crate01", parent=-1)], [geometry(p, t, [material(texture=texture, version=version)],
                                                              None, uv, version=version)], [(0, 0)], version)


def sa_game(root: Path) -> Path:
    """A San Andreas install: exe, level lists, IDE files, colours, text, gta3.img (VER2), a loose
    vehicle.txd and ped.ifp."""
    root = Path(root)
    (root / "data/maps/LA").mkdir(parents=True)
    (root / "models/generic").mkdir(parents=True)
    (root / "anim").mkdir()
    (root / "text").mkdir()
    (root / "gta_sa.exe").write_bytes(b"MZ")
    (root / "data/default.dat").write_text("IDE DATA\\VEHICLES.IDE\nIDE DATA\\PEDS.IDE\n")
    (root / "data/gta.dat").write_text("IDE DATA\\MAPS\\LA\\LAe.IDE\nIPL DATA\\MAPS\\LA\\LAe.IPL\n")
    (root / "data/vehicles.ide").write_text(
        "cars\n400, landstal, landstal, car, LANDSTAL, LANDSTK, null, richfamily, 10, 7, 0, -1, 0.768, 0.768, -1\nend\n")
    (root / "data/peds.ide").write_text(
        "peds\n7, male01, male01, CIVMALE, STAT_STREET_GUY, man, 1983, 1, null, VOICE_GEN_RT, VOICE_GEN_RT\nend\n")
    (root / "data/maps/LA/LAe.ide").write_text("objs\n3000, crate01, lae, 100, 0\nend\n")
    (root / "data/maps/LA/LAe.ipl").write_text("inst\n3000, crate01, 0, 1.0, 2.0, 3.0, 0, 0, 0, 1, -1\nend\n")
    (root / "data/carcols.dat").write_text(
        "col\n0,0,0  # black\n245,245,245\n200,20,20\nend\ncar\nlandstal, 2,1, 1,0\nend\ncar4\nend\n")
    (root / "text/american.gxt").write_bytes(gxt_sa({"LANDSTK": "Landstalker"}))
    (root / "models/generic/vehicle.txd").write_bytes(txd([solid_bc1("tyre", GREY565)]))
    (root / "anim/ped.ifp").write_bytes(anp3("ped", [
        ("walk", [dict(name="Pelvis", bone_id=1, keys=[(0.0, 0.0, (0, 0, 1.0)), (1.0, np.pi / 2, (0, 2.0, 1.0))])]),
        ("idle_stance", [dict(name="Head", bone_id=2, keys=[(0.0, 0.0, None), (0.5, 0.3, None)])]),
        ("car_open", [dict(name="DoorThing", bone_id=99, keys=[(0.0, 0.0, None)])]),
    ]))
    write_img(root / "models/gta3.img", [
        ("landstal.dff", car()),
        ("landstal.txd", txd([solid_bc1("body", 0xFFFF), solid_bc1("glass", BLUE565)])),
        ("male01.dff", ped()),
        ("male01.txd", txd([solid_bc1("male01", RED565)])),
        ("crate01.dff", prop()),
        ("lae.txd", txd([solid_bc1("crate", GREY565)])),
        ("unlisted.dff", prop(texture="missing")),
    ], version=2)
    return root


def iii_game(root: Path) -> Path:
    """A GTA III install: gta3.img with gta3.dir, ANPK ped.ifp, III text."""
    root = Path(root)
    (root / "data/maps").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "anim").mkdir()
    (root / "text").mkdir()
    (root / "gta3.exe").write_bytes(b"MZ")
    (root / "data/default.dat").write_text("IDE DATA\\DEFAULT.IDE\n")
    (root / "data/gta3.dat").write_text("IDE DATA\\MAPS\\INDUSTNE.IDE\n")
    (root / "data/default.ide").write_text(
        "cars\n90, landstal, landstal, car, LANDSTAL, LANDSTK, normal, 10, 7, 0\nend\n"
        "peds\n30, male01, male01, CIVMALE, STAT_STREET_GUY, man, 1983\nend\n")
    (root / "data/maps/industne.ide").write_text("objs\n1000, crate01, lae, 1, 100, 0\nend\n")
    (root / "data/carcols.dat").write_text("col\n10,10,200\n240,240,240\nend\ncar\nlandstal, 0,1\nend\n")
    (root / "text/american.gxt").write_bytes(gxt_iii({"LANDSTK": "Landstalker"}))
    (root / "anim/ped.ifp").write_bytes(anpk("ped", [
        ("walk_civi", [dict(name="Pelvis", keys=[(0.0, 0.0, (0, 0, 1.0)), (1.0, np.pi / 2, (0, 0, 1.0))])]),
    ]))
    write_img(root / "models/gta3.img", [
        ("landstal.dff", car(III)),
        ("landstal.txd", txd([solid_bc1("body", 0xFFFF, platform=8, version=III),
                              solid_bc1("glass", BLUE565, platform=8, version=III)], III)),
        ("male01.dff", ped(III, bone_ids=(0, 1, 2))),
        ("male01.txd", txd([solid_bc1("male01", RED565, platform=8, version=III)], III)),
        ("crate01.dff", prop(III)),
        ("lae.txd", txd([solid_bc1("crate", GREY565, platform=8, version=III)], III)),
        ("generic.txd", txd([solid_bc1("tyre", GREY565, platform=8, version=III)], III)),
    ], version=1)
    return root
