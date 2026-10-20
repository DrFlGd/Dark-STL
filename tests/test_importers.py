"""The importer layer: the registry, the FromSoftware importer's SourceModel, and OBJ import."""

from pathlib import Path

import numpy as np
import pytest

from darkstl import export, library, pipeline
from darkstl.errors import FormatError
from darkstl.importers import IMPORTERS, importer_for, open_model, register
from darkstl.importers.base import Importer
from darkstl.importers.fromsoft.formats import dcx
from darkstl.importers.wavefront import looks_like_normal_map, parse_map_statement, parse_obj
from darkstl.model import MeshOptions, SourceModel

import synth
from test_heightmap import encode_normals
from test_pipeline import bump_height, weapon_dir  # noqa: F401 - fixture


def write_png(path: Path, img: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(img).save(path)


@pytest.fixture
def obj_dir(tmp_path):
    """A 1 m square (Y up, two groups) with an albedo and a normal map given as map_Bump."""
    d = tmp_path / "models"
    d.mkdir()
    (d / "tex").mkdir()
    albedo = np.zeros((64, 64, 4), np.uint8)
    albedo[..., 1] = 180
    albedo[..., 3] = 255
    write_png(d / "tex" / "panel_d.png", albedo)
    write_png(d / "tex" / "panel_nrm.png", encode_normals(bump_height(), "opengl"))
    (d / "panel.mtl").write_text(
        "newmtl Panel\nKd 1 1 1\nmap_Kd C:\\Users\\someone\\textures\\panel_d.png\n"
        "map_Bump -bm 1.0 tex/panel_nrm.png\n")
    # Square in the XZ plane (Y up), split into two groups of two triangles each.
    (d / "panel.obj").write_text(
        "# test panel\nmtllib panel.mtl\n"
        "v 0 0 0\nv 0.5 0 0\nv 1 0 0\nv 0 0 -1\nv 0.5 0 -1\nv 1 0 -1\n"
        "vt 0 0\nvt 0.5 0\nvt 1 0\nvt 0 1\nvt 0.5 1\nvt 1 1\n"
        "vn 0 1 0\n"
        "usemtl Panel\ng left\nf 1/1/1 2/2/1 5/5/1 4/4/1\n"
        "g right\nf 2/2/1 3/3/1 6/6/1\nf -5/-5/1 -1/-1/1 -2/-2/1\n")
    return d


def test_registry_picks_importer_by_file(tmp_path):
    assert importer_for(tmp_path / "wp_a_0300.partsbnd.dcx").key == "fromsoft"
    assert importer_for(tmp_path / "thing.OBJ").key == "wavefront"
    with pytest.raises(FormatError):
        importer_for(tmp_path / "notes.txt")


def test_registering_an_importer(tmp_path):
    class Dummy(Importer):
        key = "dummy"

        def can_open(self, path):
            return path.suffix == ".dum"

        def load(self, path, variant=None, texture_dirs=()):
            from darkstl.geometry.mesh import TriMesh

            mesh = TriMesh(np.eye(3), [[0, 1, 2]])
            return SourceModel("d", path, self.key, build=lambda options: mesh)

    register(Dummy())
    try:
        model = open_model(tmp_path / "x.dum")
        assert model.importer == "dummy" and model.build_mesh().n_faces == 1
    finally:
        IMPORTERS.pop(0)


def test_fromsoft_source_model(weapon_dir):  # noqa: F811
    model = open_model(weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx")
    assert model.importer == "fromsoft" and model.game == "Dark Souls III"
    assert [p.name for p in model.parts] == ["Mesh 0"]
    mat = model.materials[0]
    assert (mat.albedo, mat.normal) == ("wp_a_0300_a", "wp_a_0300_n")
    assert model.textures["wp_a_0300_n"].decode().shape == (128, 128, 4)
    assert "128 x 128" in model.textures["wp_a_0300_n"].describe()
    assert [o.key for o in model.options] == ["bones"]
    assert model.build_mesh().n_faces == 32
    assert model.build_mesh(MeshOptions(parts=set())).n_faces == 0


def test_fromsoft_file_with_two_models(tmp_path):
    pos, nrm, uv, idx = synth.grid_plane(1)
    sword = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])])
    sheath = synth.build_flver([synth.SynthMesh(pos * 2, nrm, uv, idx)], [("m", "m.mtd", [])])
    path = tmp_path / "wp_a_0402.partsbnd.dcx"
    path.write_bytes(dcx.compress_dflt(synth.build_bnd3([(200, "N:\\parts\\WP_A_0402.flver", sword),
                                                         (201, "N:\\parts\\WP_A_0402_1.flver", sheath)])))
    imp = importer_for(path)
    assert imp.variants(path) == ["WP_A_0402.flver", "WP_A_0402_1.flver"]
    first = open_model(path)
    assert first.variant == "WP_A_0402.flver" and first.variants == imp.variants(path)
    assert any("holds 2 models" in n for n in first.notes)
    second = open_model(path, "WP_A_0402_1.flver")
    assert second.variant == "WP_A_0402_1.flver" and not second.notes
    assert np.ptp(second.build_mesh().vertices[:, 0]) == pytest.approx(2 * np.ptp(first.build_mesh().vertices[:, 0]))


def test_fromsoft_placeholder_model_is_explained(tmp_path):
    empty = synth.build_flver([], [], bones=[synth.SynthBone("dmy")])
    path = tmp_path / "c2509.chrbnd.dcx"
    path.write_bytes(dcx.compress_dflt(synth.build_bnd3([(200, "c2509.flver", empty)])))
    model = open_model(path)
    assert model.parts == [] and model.build_mesh().n_faces == 0
    assert any("no geometry" in n for n in model.notes)
    with pytest.raises(ValueError, match="no triangles"):
        pipeline.enhance(model, pipeline.EnhanceSettings(target_vertices=1_000))


def test_obj_parser_handles_polygons_and_negative_indices():
    obj = parse_obj("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\nf -4 -2 -1\n")
    assert len(obj.corners) == 9  # quad -> 2 triangles, plus one triangle
    assert obj.corners[6:] == [(0, -1, -1), (2, -1, -1), (3, -1, -1)]


def test_mtl_texture_options_are_skipped():
    name, opts = parse_map_statement("-bm 0.5 -s 2 2 1 -clamp on my texture_n.png")
    assert name == "my texture_n.png" and opts["-bm"] == [0.5] and opts["-s"] == [2, 2, 1]


def test_obj_import(obj_dir):
    model = open_model(obj_dir / "panel.obj")
    assert model.importer == "wavefront"
    assert [(p.name, p.triangles) for p in model.parts] == [("left", 2), ("right", 2)]
    mat = model.materials[0]
    # map_Kd's absolute path is stale but the file is found by name; the bluish map_Bump is a normal map.
    assert mat.albedo == "panel_d.png" and mat.normal == "panel_nrm.png" and mat.height is None
    assert looks_like_normal_map(model.textures["panel_nrm.png"].decode())
    mesh = model.build_mesh()
    assert mesh.n_vertices == 6 and mesh.n_faces == 4
    # Y-up -> Z-up: the square lies in the XY plane, facing +Z.
    assert np.allclose(mesh.vertices[:, 2], 0) and np.allclose(mesh.vertices.max(0), [1, 1, 0])
    assert np.allclose(mesh.corner_normals.reshape(-1, 3), [0, 0, 1], atol=1e-6)
    assert np.allclose(mesh.face_normals(), [0, 0, 1])
    # V flipped to point down the image: the corner at OBJ (x=0, z=-1) had vt (0, 1).
    corner = np.argwhere(np.all(np.isclose(mesh.vertices[mesh.faces], [0, 1, 0]), axis=-1))[0]
    assert np.allclose(mesh.corner_uvs[tuple(corner)], [0, 0])
    assert model.build_mesh(MeshOptions(parts={1})).n_vertices == 4


def test_obj_units(obj_dir):
    big = obj_dir / "big.obj"
    big.write_text("v 0 0 0\nv 100 0 0\nv 0 100 0\nf 1 2 3\n")
    model = open_model(big)
    assert {o.key: o.default for o in model.options}["mm"] is True
    assert np.ptp(model.build_mesh().vertices[:, 0]) == pytest.approx(0.1)
    assert np.ptp(model.build_mesh(MeshOptions(flags={"mm": False})).vertices[:, 0]) == pytest.approx(100)


def test_obj_enhance_and_round_trip(obj_dir, tmp_path):
    model = open_model(obj_dir / "panel.obj")
    result = pipeline.enhance(model, pipeline.EnhanceSettings(target_vertices=20_000, detail_fraction=0))
    assert result.displaced.n_vertices == 20_000
    assert result.fields[0].field.convention == "opengl"
    v = result.displaced.vertices
    centre = np.linalg.norm(v[:, :2] - [0.5, 0.5], axis=1) < 0.03
    edge = np.linalg.norm(v[:, :2] - [0.5, 0.5], axis=1) > 0.35
    assert v[centre, 2].mean() - v[edge, 2].mean() == pytest.approx(8 / 128, rel=0.3)

    out = export.export_mesh(result.displaced, tmp_path / "out" / "panel_hi.obj", export.ExportOptions())
    again = open_model(out)  # written in millimetres, Y up; read back in metres, Z up
    assert [o.key for o in again.options if o.key == "mm" and o.default] == ["mm"]
    mesh = again.build_mesh()
    assert mesh.n_vertices == result.displaced.n_vertices
    assert np.allclose(np.sort(mesh.vertices[:, 2]), np.sort(result.displaced.vertices[:, 2]), atol=1e-5)


def test_library_lists_obj_files(obj_dir, weapon_dir):  # noqa: F811
    lib = library.Library()
    found = lib.add_root(obj_dir)
    assert [(e.category, e.stem, e.importer) for e in found] == [("Models", "panel", "wavefront")]
    lib.add_root(weapon_dir)
    assert set(lib.grouped()) == {"models", "Dark Souls III"}
