"""Painting a model from its textures for multi-colour printing, and the painted 3MF it is written as."""

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import synth
from test_app import make_service, wait
from test_heightmap import encode_normals
from test_pipeline import bump_height

from darkstl import paint
from darkstl.export import ExportOptions, export_mesh, read_3mf
from darkstl.geometry.mesh import TriMesh
from darkstl.importers.fromsoft.formats import dcx
from darkstl.paint import PaintSettings, decode_string, encode_tree, leaf_colours


def square(size=1.0):
    """A 1 m square (two triangles) standing in the XZ plane, UVs over the whole texture."""
    v = np.array([[0, 0, 0], [size, 0, 0], [size, 0, size], [0, 0, size]], float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[[0, 1], [1, 1], [1, 0]], [[0, 1], [1, 0], [0, 0]]], np.float32)
    return TriMesh(v, f, corner_uvs=uv)


def halves(n=256, left=(220, 0, 0), right=(0, 0, 220)):
    """A texture whose left half is one colour and right half another."""
    img = np.zeros((n, n, 4), np.uint8)
    img[..., 3] = 255
    img[:, : n // 2, :3] = left
    img[:, n // 2:, :3] = right
    return img


# ---------------------------------------------------------------------------
# the slicers' strings


def test_strings_match_the_slicers():
    # from Bambu Studio's TriangleSelector: read from the last digit back, low 2 bits = split sides,
    # high 2 bits = state (3 escapes to the next digit + 3); children in reverse order
    assert encode_tree(1) == "4" and encode_tree(2) == "8" and encode_tree(0) == "0"
    assert encode_tree(3) == "0C" and encode_tree(7) == "4C" and encode_tree(16) == "DC"
    assert encode_tree(18) == "0FC"  # 15 + 3 escaped, then 0
    assert encode_tree([1, 1, 0, 1]) == "44043"  # the stream holds the children last to first
    assert decode_string("44043") == [1, 1, 0, 1]
    for tree in (1, 3, 16, [1, 0, 1, 1], [1, [2, 2, 3, 4], 5, [1, 1, [0, 0, 0, 1], 7]]):
        assert decode_string(encode_tree(tree)) == tree
    with pytest.raises(ValueError):
        decode_string("3")  # a split with no children
    with pytest.raises(ValueError):
        decode_string("44")  # digits left over
    assert decode_string("441") == [0, 1, 1]  # a one-sided split: special side 0, two children
    assert leaf_colours([1, 0, 1, 1], 2).tolist() == [0.25, 0.75, 0.0]


def test_colours():
    assert paint.hex_colour((255, 0, 16)) == "#FF0010"
    assert paint.parse_colour("#ff0010").tolist() == [255, 0, 16] and paint.parse_colour("f00").tolist() == [255, 0, 0]
    with pytest.raises(ValueError):
        paint.parse_colour("red")
    lab = paint.srgb_to_oklab(np.array([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0]]))
    assert lab[0, 0] == pytest.approx(1.0, abs=1e-3) and lab[1, 0] == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# palette and quantising


def test_palette_is_weighed_by_the_surface():
    # the texture is half red, half blue, but the mesh only covers the red half (and a sliver of blue)
    mesh = square()
    mesh.corner_uvs = mesh.corner_uvs * np.array([0.55, 1.0], np.float32)
    palette, coverage = paint.suggest_palette(mesh, {0: halves()}, 2)
    assert [paint.hex_colour(c) for c in palette] == ["#DE0202", "#0202DE"]  # red first: most of the surface
    assert coverage[0] > 0.85
    # a texture with four colours reduced to three keeps the three that cover most of it
    img = halves()
    img[:64, :, :3] = (0, 200, 0)
    img[64:72, :, :3] = (255, 255, 0)
    palette, coverage = paint.suggest_palette(square(), {0: img}, 3)
    assert {paint.hex_colour(c) for c in palette} == {"#DE0202", "#0202DE", "#02CA02"}
    with pytest.raises(ValueError):
        paint.suggest_palette(TriMesh(mesh.vertices, mesh.faces), {0: img}, 3)  # no UVs


def test_quantize_and_cleanup():
    img = halves()
    img[100:103, 20:23, :3] = (0, 0, 220)  # a blue speck in the red half
    img[10, 200, :3] = (220, 0, 0)  # a red dot in the blue half
    palette = np.array([[220, 0, 0], [0, 0, 220]], np.uint8)
    index = paint.quantize(img, palette)
    assert index.shape == (256, 256) and index[0, 0] == 0 and index[0, 255] == 1 and index[101, 21] == 1
    assert paint.cleanup(index, 2, "none") is index
    light = paint.cleanup(index, 2, "light")
    assert light[10, 200] == 1 and light[101, 21] == 1  # the dot goes (3 x 3 majority), the 9-texel speck stays
    strong = paint.cleanup(index, 2, "strong")
    assert strong[101, 21] == 0 and strong[:, :128].min() == 0 and strong[:, :128].max() == 0
    assert strong[:, 128:].min() == 1


# ---------------------------------------------------------------------------
# painting


def test_paints_finer_than_the_triangles():
    mesh = square()
    img = halves()
    p = paint.paint_mesh(mesh, {0: img}, PaintSettings(colours=2, print_height_mm=100, feature_mm=0.5))
    assert [paint.hex_colour(c) for c in p.palette] == ["#DE0202", "#0202DE"]
    assert len(p.strings) == 2 and p.split_faces == 2 and p.depth >= 1 and p.unpainted_faces == 0
    # the border runs down the middle of both triangles: the lower-right one is a quarter red, the
    # upper-left one three quarters, and together they are half and half
    shares = [leaf_colours(decode_string(s), 2) for s in p.strings]
    assert shares[0][1] == pytest.approx(0.25, abs=0.02) and shares[1][1] == pytest.approx(0.75, abs=0.02)
    assert (shares[0] + shares[1])[1:].tolist() == pytest.approx([1.0, 1.0], abs=0.03)
    # a border exactly on the diagonal (one triangle each colour): no split at all
    diagonal = np.zeros((256, 256, 4), np.uint8)
    diagonal[..., 3] = 255
    yy, xx = np.mgrid[0:256, 0:256]
    diagonal[..., :3] = np.where((xx + yy < 255)[..., None], (220, 0, 0), (0, 0, 220))
    p = paint.paint_mesh(mesh, {0: diagonal}, PaintSettings(palette=["#DE0202", "#0202DE"]))
    assert p.split_faces == 0 and sorted(p.strings) == ["4", "8"]
    assert p.coverage.tolist() == pytest.approx([0.5, 0.5])
    # the feature size stops the splitting: a 100 mm print with 20 mm features splits once or twice at most
    coarse = paint.paint_mesh(mesh, {0: img}, PaintSettings(colours=2, print_height_mm=100, feature_mm=20))
    assert coarse.depth <= 2 and coarse.leaves < p.leaves + 40
    # a palette given by hand is used as it is, in slot order
    given = paint.paint_mesh(mesh, {0: img}, PaintSettings(palette=["#0000FF", "#FF0000", "#00FF00"]))
    assert [paint.hex_colour(c) for c in given.palette] == ["#0000FF", "#FF0000", "#00FF00"]
    assert decode_string(given.strings[0])[0] == 2  # the corner at u=0 is red: slot 2
    assert given.coverage[2] == 0


def test_paints_materials_without_a_texture_as_unpainted():
    mesh = square()
    mesh.face_material = np.array([0, 1], np.int32)
    p = paint.paint_mesh(mesh, {0: halves()}, PaintSettings(colours=2))
    assert p.strings[1] == "" and p.unpainted_faces == 1 and p.notes


def test_tiny_triangles_take_one_colour():
    # triangles smaller than a texel are not split: each gets the colour under its middle
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], float) * 0.001
    f = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[[0.1, 0.5], [0.101, 0.5], [0.101, 0.501]], [[0.9, 0.5], [0.901, 0.5], [0.901, 0.501]]], np.float32)
    p = paint.paint_mesh(TriMesh(v, f, corner_uvs=uv), {0: halves()}, PaintSettings(palette=["#DE0202", "#0202DE"]))
    assert p.strings == ["4", "8"] and p.split_faces == 0


# ---------------------------------------------------------------------------
# the 3MF


def test_writes_a_painted_3mf(tmp_path):
    mesh = square()
    opts = ExportOptions(textures={0: halves()}, paint=PaintSettings(colours=2, print_height_mm=100, feature_mm=2))
    path = export_mesh(mesh, tmp_path / "square.3mf", opts)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        model = z.read("3D/3dmodel.model").decode()
    assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/Slic3r_PE_model.config"} <= set(names)
    assert 'unit="millimeter"' in model and 'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06"' in model
    assert "slic3rpe:MmPaintingVersion" in model and 'x="1000"' in model  # metres -> millimetres
    verts, faces, strings, palette = read_3mf(path)
    assert verts.shape == (4, 3) and faces.tolist() == mesh.faces.tolist()
    assert palette == ["#DE0202", "#0202DE"] and all(strings)
    assert leaf_colours(decode_string(strings[0]), 2)[1] == pytest.approx(0.25, abs=0.05)
    assert (tmp_path / "square_palette.png").stat().st_size > 100
    assert opts.painting.stats()["colours"] == palette
    with pytest.raises(ValueError, match="no colour textures"):
        export_mesh(mesh, tmp_path / "plain.3mf", ExportOptions())


# ---------------------------------------------------------------------------
# the app


def make_two_colour_game(tmp_path):
    """A fake Dark Souls III weapon whose albedo is red on the left, blue on the right."""
    game = tmp_path / "DARK SOULS III" / "Game"
    parts = game / "parts"
    parts.mkdir(parents=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    pos, nrm, uv, idx = synth.grid_plane(3, size=1.0)  # 3 x 3: the colour border at u = 0.5 crosses the cells
    mats = [("Blade", "N:\\mtd\\C[DSB].mtd", [("N:\\tex\\WP_A_0300_a.tga", "g_DiffuseTexture"),
                                              ("N:\\tex\\WP_A_0300_n.tga", "g_BumpmapTexture")])]
    flver_bytes = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], mats, uv_float=True)
    normal = encode_normals(bump_height(), "directx")
    tpf_bytes = synth.build_tpf([("WP_A_0300_a", synth.rgba_dds(halves(128))), ("WP_A_0300_n", synth.rgba_dds(normal))])
    bnd = synth.build_bnd4([(200, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.flver", flver_bytes),
                            (100, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.tpf", tpf_bytes)])
    (parts / "wp_a_0300.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    return game


@pytest.fixture
def two_colour_game(tmp_path):
    return make_two_colour_game(tmp_path)


def test_the_app_suggests_a_palette_previews_and_exports(two_colour_game, tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    service = make_service(tmp_path / "home")
    wait(service, service.call("folder_add", {"path": str(two_colour_game)})["job"])
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})
    assert any(f["ext"] == ".3mf" for f in service.call("app_info", {})["formats"])
    pal = service.call("paint_palette", {"sid": m["sid"], "colours": 2})
    assert pal["colours"] == ["#DE0202", "#0202DE"] and pal["coverage"] == pytest.approx([0.5, 0.5], abs=0.05)
    assert service.call("paint_palette", {"sid": m["sid"], "colours": 2}) is pal  # kept
    with pytest.raises(Exception, match="between 2 and"):
        service.call("paint_palette", {"sid": m["sid"], "colours": 40})
    # the preview texture: the albedo in the palette's colours
    png = service.call("paint_texture", {"sid": m["sid"], "key": m["materials"][0]["albedo"], "max": "64",
                                         "settings": json.dumps({"colours": 2, "palette": ["#FF0000", "#0000FF", "#00FF00"]})})
    assert png[:4] == b"\x89PNG"
    import io

    from PIL import Image
    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    assert arr[32, 5].tolist() == [255, 0, 0] and arr[32, 60].tolist() == [0, 0, 255]
    out = tmp_path / "exports"
    r = wait(service, service.call("export", {"sid": m["sid"], "which": "original", "format": ".3mf", "folder": str(out),
                                              "paint": {"colours": 2, "print_height_mm": 100, "feature_mm": 1}})["job"])
    assert Path(r["path"]).name == "WP_A_0300.3mf" and r["paint"]["colours"] == ["#DE0202", "#0202DE"]
    assert Path(r["paint"]["palette_file"]).name == "WP_A_0300_palette.png" and Path(r["paint"]["palette_file"]).exists()
    assert r["paint"]["split_faces"] > 0
    _verts, faces, _strings, palette = read_3mf(r["path"])
    assert len(faces) == 18 and palette == ["#DE0202", "#0202DE"]
    # the enhanced model is painted too (its UVs are exact)
    wait(service, service.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 2000, "source": "none"}})["job"])
    r = wait(service, service.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".3mf", "folder": str(out),
                                              "paint": {"palette": ["#FF0000", "#0000FF", "#00FF00"]}})["job"])
    _verts, faces, _strings, palette = read_3mf(r["path"])
    assert len(faces) > 1000 and palette == ["#FF0000", "#0000FF", "#00FF00"] and r["paint"]["coverage"][2] == 0
    wait(service, service.call("finish", {"sid": m["sid"], "from": "original"})["job"])
    with pytest.raises(Exception, match="Finished model has no textures"):
        service.call("export", {"sid": m["sid"], "which": "finished", "format": ".3mf", "folder": str(out)})
    with pytest.raises(Exception, match="Paint settings|between 2 and"):
        service.call("export", {"sid": m["sid"], "which": "original", "format": ".3mf", "folder": str(out),
                                "paint": {"colours": 1}})


def test_cli_exports_a_painted_3mf(two_colour_game, tmp_path, capsys):
    from darkstl.cli import main

    out = tmp_path / "cli" / "blade.3mf"
    assert main(["export", str(two_colour_game / "parts" / "wp_a_0300.partsbnd.dcx"), "-o", str(out), "--colours", "2",
                 "--print-height", "100", "--feature", "1"]) == 0
    text = capsys.readouterr().out
    assert "Painted with 2 filaments: 1: #DE0202 (50%), 2: #0202DE (50%)" in text
    assert read_3mf(out)[3] == ["#DE0202", "#0202DE"] and (out.with_name("blade_palette.png")).exists()
    assert main(["export", str(two_colour_game / "parts" / "wp_a_0300.partsbnd.dcx"), "-o", str(tmp_path / "cli" / "p.3mf"),
                 "--palette", "#FF0000,#00FF00"]) == 0
    assert read_3mf(tmp_path / "cli" / "p.3mf")[3] == ["#FF0000", "#00FF00"]
