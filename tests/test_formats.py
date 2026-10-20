import struct

import numpy as np
import pytest

from darkstl.formats import binder, dcx, dds, flver, ps4, tpf
from darkstl.formats.binary import FormatError

import synth


# -- DCX ---------------------------------------------------------------------

def test_dcx_dflt_round_trip():
    payload = bytes(range(256)) * 50
    packed = dcx.compress_dflt(payload)
    assert dcx.is_dcx(packed)
    out, codec = dcx.decompress(packed)
    assert out == payload
    assert codec == "DFLT"


def test_dcx_zstd():
    zstandard = pytest.importorskip("zstandard")
    payload = b"elden ring " * 1000
    body = zstandard.ZstdCompressor().compress(payload)
    header = (b"DCX\0" + struct.pack(">iiiii", 0x11000, 0x18, 0x24, 0x44, 0x4C) + b"DCS\0"
              + struct.pack(">II", len(payload), len(body)) + b"DCP\0ZSTD" + struct.pack(">i", 0x20)
              + bytes([21, 0, 0, 0]) + struct.pack(">iiii", 0, 0, 0, 0x10100) + b"DCA\0" + struct.pack(">i", 8))
    assert len(header) == 0x4C
    out, codec = dcx.decompress(header + body)
    assert out == payload and codec == "ZSTD"


def test_dcx_krak_without_oodle_explains(monkeypatch):
    monkeypatch.delenv("DARKSTL_OODLE", raising=False)
    header = bytearray(dcx.compress_dflt(b"x" * 10))
    header[0x28:0x2C] = b"KRAK"
    with pytest.raises(FormatError, match="oo2core"):
        dcx.decompress(bytes(header))



def test_oodle_found_above_krak_files(tmp_path, monkeypatch):
    from darkstl import assets
    from darkstl.formats import oodle

    monkeypatch.setattr(oodle, "_hint_dirs", [])
    game = tmp_path / "ELDEN RING" / "Game"
    parts = game / "parts"
    parts.mkdir(parents=True)
    (game / "oo2core_6_win64.dll").write_bytes(b"")
    krak = bytearray(dcx.compress_dflt(b"x" * 10))
    krak[0x28:0x2C] = b"KRAK"
    (parts / "wp_a_0100.partsbnd.dcx").write_bytes(bytes(krak))
    with pytest.raises(FormatError):
        assets.read_bytes(parts / "wp_a_0100.partsbnd.dcx")  # the empty stand-in DLL cannot load
    assert (game / "oo2core_6_win64.dll").resolve() in oodle._candidates()

# -- Binders -----------------------------------------------------------------

def test_bnd4_read():
    files = [(100, "N:\\FDP\\data\\parts\\wp_a_0300.flver", b"FLVERDATA"), (200, "wp_a_0300.tpf", b"TPFDATA")]
    data = synth.build_bnd4(files)
    b = binder.read_binder(data)
    assert b.kind == "BND4"
    assert [f.id for f in b.files] == [100, 200]
    assert b.files[0].basename == "wp_a_0300.flver"
    assert b.files[0].data == b"FLVERDATA"
    assert b.by_extension(".tpf")[0].data == b"TPFDATA"


def test_bnd4_inside_dcx():
    data = dcx.compress_dflt(synth.build_bnd4([(1, "a.flver", b"abc")]))
    b = binder.read_binder(data)
    assert b.files[0].data == b"abc"


def test_bnd3_read():
    data = synth.build_bnd3([(10, "WP_A_0200.flver", b"hello"), (11, "WP_A_0200.tpf", b"world")])
    b = binder.read_binder(data)
    assert b.kind == "BND3"
    assert [(f.id, f.basename, f.data) for f in b.files] == [(10, "WP_A_0200.flver", b"hello"),
                                                              (11, "WP_A_0200.tpf", b"world")]


def test_bxf4_split():
    bhd, bdt = synth.build_bxf4([(0, "c2010.tpf", b"one"), (1, "c2010_2.tpf", b"second")])
    b = binder.read_binder(bhd, bdt)
    assert [f.data for f in b.files] == [b"one", b"second"]
    with pytest.raises(FormatError):
        binder.read_binder(bhd)


# -- DDS ---------------------------------------------------------------------

def test_dds_rgba_round_trip():
    img = np.random.default_rng(0).integers(0, 255, (6, 10, 4), dtype=np.uint8)
    out = dds.decode_dds(synth.rgba_dds(img))
    assert out.shape == (6, 10, 4)
    assert np.array_equal(out, img)


def test_dds_bc1_solid_and_indices():
    red, blue = 0xF800, 0x001F
    block = synth.bc1_block(red, blue, [0] * 8 + [1] * 8)  # top half red, bottom half blue
    img = dds.decode_dds(dds.build_dds("BC1", 4, 4, block))
    assert img.shape == (4, 4, 4)
    assert tuple(img[0, 0]) == (255, 0, 0, 255)
    assert tuple(img[3, 3]) == (0, 0, 255, 255)


def test_dds_bc5_normal_map_channels():
    r = synth.bc4_block(255, 0, [0] * 16)
    g = synth.bc4_block(255, 0, [1] * 16)
    img = dds.decode_dds(dds.build_dds("BC5", 4, 4, r + g))
    assert (img[..., 0] == 255).all() and (img[..., 1] == 0).all()


def test_dds_bc4_interpolation():
    blk = synth.bc4_block(200, 100, [2] * 16)  # (6*200 + 100) / 7
    img = dds.decode_dds(dds.build_dds("BC4", 4, 4, blk))
    assert abs(int(img[0, 0, 0]) - round((6 * 200 + 100) / 7)) <= 1


def test_dds_bc7_via_texture2ddecoder():
    pytest.importorskip("texture2ddecoder")
    # BC7 mode 6 block encoding a constant colour is complex; just check decoding runs and crops.
    blocks = bytes(16) * 4
    img = dds.decode_dds(dds.build_dds("BC7", 6, 5, blocks))
    assert img.shape == (5, 6, 4)


def test_dds_mip_offset():
    big = np.full((8, 8, 4), 10, np.uint8)
    small = np.full((4, 4, 4), 200, np.uint8)
    data = dds.build_dds("R8G8B8A8", 8, 8, big.tobytes() + small.tobytes(), mipmaps=2)
    assert dds.decode_dds(data, mip=1)[0, 0, 0] == 200


# -- TPF ---------------------------------------------------------------------

def test_tpf_pc():
    img = np.zeros((4, 4, 4), np.uint8)
    img[..., 1] = 99
    data = synth.build_tpf([("WP_A_0300_a", synth.rgba_dds(img)), ("WP_A_0300_n", synth.rgba_dds(img))])
    pack = tpf.read_tpf(data)
    assert [t.name for t in pack.textures] == ["WP_A_0300_a", "WP_A_0300_n"]
    assert pack.get("wp_a_0300_n").decode()[0, 0, 1] == 99


def test_tpf_ps4_untile():
    # 64x32 BC1 texture = 16x8 blocks -> two 8x8-block tiles
    rng = np.random.default_rng(1)
    blocks = rng.integers(0, 255, (8, 16, 8), dtype=np.uint8)
    linear = blocks.tobytes()
    tiled = ps4.tile(linear, 16, 8, 8)
    assert tiled != linear
    data = synth.build_tpf_ps4("bb_tex", 0, 64, 32, tiled, 71)
    pack = tpf.read_tpf(data)
    tex = pack.textures[0]
    assert (tex.width, tex.height, tex.platform) == (64, 32, tpf.PLATFORM_PS4)
    rebuilt = tex.to_dds()
    info = dds.parse_dds(rebuilt)
    assert info.codec == "BC1" and (info.width, info.height) == (64, 32)
    assert rebuilt[info.data_offset:] == linear


def test_untile_padding_crops():
    rng = np.random.default_rng(2)
    linear = rng.integers(0, 255, (5 * 3 * 16,), dtype=np.uint8).tobytes()  # 5x3 blocks of 16 bytes
    tiled = ps4.tile(linear, 5, 3, 16)
    assert len(tiled) == 8 * 8 * 16
    out, used = ps4.untile(tiled, 5, 3, 16)
    assert out == linear and used == len(tiled)


# -- FLVER -------------------------------------------------------------------

def _simple_flver(**kw):
    pos, nrm, uv, idx = synth.grid_plane(2)
    mesh = synth.SynthMesh(pos, nrm, uv, idx)
    mats = [("Blade", "N:\\mtd\\P[ARSN].mtd", [("N:\\tex\\wp_a_0300_a.tif", "g_DiffuseTexture"),
                                               ("N:\\tex\\wp_a_0300_n.tif", "g_BumpmapTexture")])]
    return synth.build_flver([mesh], mats, **kw), (pos, nrm, uv, idx)


@pytest.mark.parametrize("version", [0x2000C, 0x20014, 0x2001A])
def test_flver_read_basic(version):
    data, (pos, nrm, uv, idx) = _simple_flver(version=version)
    f = flver.read_flver(data)
    assert f.version == version
    assert len(f.meshes) == 1 and len(f.materials) == 1 and len(f.bones) == 1
    m = f.meshes[0]
    assert np.allclose(m.vertices.positions, pos)
    assert np.allclose(m.vertices.normals, nrm, atol=0.01)
    factor = 2048 if version >= 0x2000F else 1024
    assert np.allclose(m.vertices.uvs[0], np.rint(uv * factor) / factor)
    assert np.array_equal(m.triangles(), idx.reshape(-1, 3))
    mat = f.materials[0]
    assert mat.name == "Blade"
    assert [t.stem for t in mat.textures] == ["wp_a_0300_a", "wp_a_0300_n"]
    assert mat.textures[1].type == "g_BumpmapTexture"


def test_flver_big_endian_float_uv():
    data, (pos, _, uv, _) = _simple_flver(big_endian=True, uv_float=True)
    f = flver.read_flver(data)
    assert f.big_endian
    assert np.allclose(f.meshes[0].vertices.positions, pos)
    assert np.allclose(f.meshes[0].vertices.uvs[0], uv)
    assert f.materials[0].name == "Blade"


def test_flver_inside_dcx():
    data, _ = _simple_flver()
    assert len(flver.read_flver(dcx.compress_dflt(data)).meshes) == 1


def test_triangle_strip_with_restart():
    fs = flver.FaceSet(0, True, True, np.array([0, 1, 2, 3, 0xFFFF, 4, 5, 6, 6, 7], np.uint16))
    tris = fs.triangles(allow_restart=True)
    # strip 0..3 -> (0,1,2), (3,2,1) flipped; restart; 4,5,6 -> (4,5,6); (5,6,6) degenerate; (6,6,7) degenerate
    assert tris.tolist() == [[0, 1, 2], [3, 2, 1], [4, 5, 6]]


def test_lod_face_sets_are_skipped():
    pos, nrm, uv, idx = synth.grid_plane(2)
    lod = np.array([0, 1, 2])
    mesh = synth.SynthMesh(pos, nrm, uv, idx, extra_face_sets=[(flver.FS_LOD1, lod)])
    f = flver.read_flver(synth.build_flver([mesh], [("m", "m.mtd", [])]))
    assert len(f.meshes[0].face_sets) == 2
    assert len(f.meshes[0].triangles()) == len(idx) // 3


def test_bone_transforms_compose():
    bones = [flver.Bone("root", np.array([1, 0, 0], np.float32), np.zeros(3, np.float32), np.ones(3, np.float32),
                        -1, 1, -1, -1),
             flver.Bone("child", np.array([0, 2, 0], np.float32), np.array([0, np.pi / 2, 0], np.float32),
                        np.ones(3, np.float32), 0, -1, -1, -1)]
    f = flver.Flver(0x20014, False, True, np.zeros(3), np.zeros(3), bones, [], [])
    world = f.bone_world_matrices()
    p = world[1] @ np.array([1.0, 0, 0, 1])
    # rotate (1,0,0) by +90deg about Y -> (0,0,-1); then +(0,2,0) then +(1,0,0)
    assert np.allclose(p[:3], [1, 2, -1], atol=1e-6)
