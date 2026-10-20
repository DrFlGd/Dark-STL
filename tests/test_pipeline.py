import json
import struct

import numpy as np
import pytest

from darkstl import assets, export, library, materials, pipeline
from darkstl.formats import dcx
from darkstl.geometry.remesh import acvd_available

import synth
from test_heightmap import encode_normals


def bump_height(n=128):
    y, x = np.mgrid[0:n, 0:n] / n
    return np.exp(-((x - 0.5) ** 2 + (y - 0.5) ** 2) / 0.01) * 8.0  # texels


@pytest.fixture
def weapon_dir(tmp_path):
    """A fake Dark Souls III weapon: 1 m plane with albedo + normal map in a DCX'd partsbnd."""
    game = tmp_path / "DARK SOULS III" / "Game"
    parts = game / "parts"
    parts.mkdir(parents=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    pos, nrm, uv, idx = synth.grid_plane(4, size=1.0)
    mesh = synth.SynthMesh(pos, nrm, uv, idx)
    mats = [("Blade", "N:\\mtd\\C[DSB].mtd", [("N:\\tex\\WP_A_0300_a.tga", "g_DiffuseTexture"),
                                              ("N:\\tex\\WP_A_0300_n.tga", "g_BumpmapTexture")])]
    flver_bytes = synth.build_flver([mesh], mats, uv_float=True)
    albedo = np.zeros((128, 128, 4), np.uint8)
    albedo[..., 0] = 200
    albedo[..., 3] = 255
    normal = encode_normals(bump_height(), "directx")
    tpf_bytes = synth.build_tpf([("WP_A_0300_a", synth.rgba_dds(albedo)), ("WP_A_0300_n", synth.rgba_dds(normal))])
    bnd = synth.build_bnd4([(200, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.flver", flver_bytes),
                            (100, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.tpf", tpf_bytes)])
    (parts / "wp_a_0300.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    (parts / "wp_a_0300_l.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    (parts / "hd_m_1000.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    chr_dir = game / "chr"
    chr_dir.mkdir()
    (chr_dir / "c1100.chrbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "c1100.flver", flver_bytes)])))
    (chr_dir / "c1100.texbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(0, "c1100.tpf", tpf_bytes)])))
    # Unpacked copy, as WitchyBND would leave it
    unpacked = parts / "wp_a_0400-partsbnd-dcx"
    unpacked.mkdir()
    (unpacked / "WP_A_0400.flver").write_bytes(flver_bytes)
    (unpacked / "WP_A_0400.tpf").write_bytes(tpf_bytes)
    return game


def test_library_scan_groups_models(weapon_dir):
    lib = library.Library()
    found = lib.add_root(weapon_dir)
    assert {e.game for e in found} == {"Dark Souls III"}
    by_stem = {e.stem.lower(): e for e in found}
    assert by_stem["wp_a_0300"].category == "Weapons"
    assert by_stem["wp_a_0300_l"].is_lod
    assert by_stem["hd_m_1000"].category == "Armor" and by_stem["hd_m_1000"].group == "Head"
    assert by_stem["c1100"].category == "Characters"
    assert by_stem["wp_a_0400"].packed is False
    tree = lib.grouped()
    assert "Weapons" in tree["Dark Souls III"]
    assert all(not e.is_lod for cat in tree["Dark Souls III"].values() for g in cat.values() for e in g)


def test_classify_names():
    assert library.classify("wp_a_1405")[0] == "Weapons"
    assert library.classify("BD_F_5000")[:2] == ("Armor", "Body")
    assert library.classify("c0000")[1] == "Player"
    assert library.classify("aeg099_001")[0] == "Objects"
    assert library.classify("m10_00_00_00_000100")[0] == "Map Pieces"
    assert library.classify("fc_m_0100")[0] == "Faces & Hair"


def test_load_model_and_materials(weapon_dir):
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    assert asset.name.lower() == "wp_a_0300"
    assert set(asset.textures) == {"wp_a_0300_a", "wp_a_0300_n"}
    info = materials.resolve_materials(asset)[0]
    assert info.albedo == "wp_a_0300_a" and info.normal == "wp_a_0300_n" and not info.guessed


def test_character_textures_from_sibling_texbnd(weapon_dir):
    asset = assets.load_model(weapon_dir / "chr" / "c1100.chrbnd.dcx")
    assert "wp_a_0300_n" in asset.textures


def test_unpacked_folder_model(weapon_dir):
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0400-partsbnd-dcx" / "WP_A_0400.flver")
    assert asset.name.lower() == "wp_a_0400"
    assert "wp_a_0300_n" in asset.textures


def test_material_fallback_by_name(weapon_dir):
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    for t in asset.flver.materials[0].textures:
        t.path = ""  # Elden Ring style: no paths in the FLVER
    info = materials.resolve_materials(asset)[0]
    assert info.normal == "wp_a_0300_n" and info.albedo == "wp_a_0300_a" and info.guessed


@pytest.mark.parametrize("method", ["subdivide", "isotropic"])
def test_enhance_end_to_end(weapon_dir, method):
    if method == "isotropic" and not acvd_available():
        pytest.skip("pyacvd not installed")
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    settings = pipeline.EnhanceSettings(target_vertices=40_000, method=method, detail_fraction=0)
    result = pipeline.enhance(asset, settings)
    assert not [w for w in result.warnings if "no normal map" in w]
    assert abs(result.displaced.n_vertices - 40_000) / 40_000 < 0.25
    field = result.fields[0]
    assert field.field.convention == "directx"
    assert field.texel_size == pytest.approx(1 / 128, rel=0.01)  # 1 m plane, 128 px texture
    # The bump (8 texels high, ~0.0625 m) must rise out of the plane near the UV centre, i.e. (0.5, 0.5) m.
    v = result.displaced.vertices
    centre = np.linalg.norm(v[:, :2] - [0.5, 0.5], axis=1) < 0.03
    edge = np.linalg.norm(v[:, :2] - [0.5, 0.5], axis=1) > 0.35
    rise = v[centre, 2].mean() - v[edge, 2].mean()
    assert rise == pytest.approx(8 / 128, rel=0.25)
    assert rise > 0  # outward along +Z (the plane's normal)


def test_enhance_without_textures_still_resamples(weapon_dir):
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    asset.textures.clear()
    result = pipeline.enhance(asset, pipeline.EnhanceSettings(target_vertices=5_000))
    assert result.displaced.n_vertices > 1_000
    assert any("no normal map" in w for w in result.warnings)


def test_exports(tmp_path, weapon_dir):
    asset = assets.load_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    result = pipeline.enhance(asset, pipeline.EnhanceSettings(target_vertices=3_000))
    mesh = result.displaced
    opts = export.ExportOptions(textures={0: asset.textures["wp_a_0300_a"].decode()}, material_names=["Blade"])

    stl = export.export_mesh(mesh, tmp_path / "out.stl", opts)
    tris = export.read_stl(stl)
    assert tris.shape == (mesh.n_faces, 3, 3)
    assert tris[..., :2].max() == pytest.approx(1000, rel=1e-3)  # metres -> millimetres

    export.export_mesh(mesh, tmp_path / "ascii.stl", export.ExportOptions(ascii_stl=True))
    assert (tmp_path / "ascii.stl").read_text().startswith("solid")

    obj = export.export_mesh(mesh, tmp_path / "out.obj", opts)
    text = obj.read_text()
    assert text.count("\nv ") == mesh.n_vertices
    assert text.count("\nf ") == mesh.n_faces
    assert "map_Kd out_Blade.png" in (tmp_path / "out.mtl").read_text()
    assert (tmp_path / "out_Blade.png").exists()

    ply = export.export_mesh(mesh, tmp_path / "out.ply")
    assert ply.read_bytes().startswith(b"ply\nformat binary_little_endian")

    glb = export.export_mesh(mesh, tmp_path / "out.glb", opts)
    data = glb.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data)
    assert magic == b"glTF" and version == 2 and length == len(data)
    (jlen,) = struct.unpack_from("<I", data, 12)
    gltf = json.loads(data[20:20 + jlen])
    assert gltf["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"] == 0
    assert gltf["accessors"][0]["count"] >= mesh.n_vertices


def test_textures_from_extra_folder(tmp_path):
    pos, nrm, uv, idx = synth.grid_plane(1)
    mats = [("m", "m.mtd", [("N:\\map\\tx\\m10_wall_n.tga", "g_BumpmapTexture")])]
    model = tmp_path / "map" / "m10_00_00_00"
    model.mkdir(parents=True)
    (model / "m10_00_00_00_000100.flver").write_bytes(synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], mats))
    tx = tmp_path / "tx"
    tx.mkdir()
    (tx / "m10_wall_n.tpf.dcx").write_bytes(
        dcx.compress_dflt(synth.build_tpf([("m10_wall_n", synth.rgba_dds(np.zeros((4, 4, 4), np.uint8)))])))
    asset = assets.load_model(model / "m10_00_00_00_000100.flver", extra_texture_dirs=(tx,))
    assert "m10_wall_n" in asset.textures


def test_cli_commands(tmp_path, weapon_dir, capsys):
    from darkstl.cli import main

    assert main(["scan", str(weapon_dir)]) == 0
    out = capsys.readouterr().out
    assert "Dark Souls III" in out and "Weapons" in out
    model = str(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    assert main(["info", model]) == 0
    assert "g_BumpmapTexture: WP_A_0300_n" in capsys.readouterr().out
    assert main(["textures", model, "-o", str(tmp_path / "tex")]) == 0
    assert (tmp_path / "tex" / "WP_A_0300_n.png").exists()
    assert main(["export", model, "-o", str(tmp_path / "orig.obj")]) == 0
    assert main(["enhance", model, "-o", str(tmp_path / "hi.stl"), "-n", "5000",
                 "--save-heights", str(tmp_path / "h")]) == 0
    assert (tmp_path / "hi.stl").stat().st_size > 84
    assert (tmp_path / "h" / "WP_A_0300_n_height.png").exists()
