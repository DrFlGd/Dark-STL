"""The RenderWare GTA importer (III, Vice City, San Andreas): its format readers, fake game folders,
the library, vehicles, peds and posing, and Collect through the app's engine."""

import struct
from pathlib import Path

import numpy as np
import pytest
import synth_rw as S
from test_app import home, make_service, wait  # noqa: F401 - fixture

from darkstl import library
from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model, profiles
from darkstl.importers.renderware import RenderWareImporter, files, games
from darkstl.importers.renderware.formats import chunks as ck
from darkstl.importers.renderware.formats import dff as dfffmt
from darkstl.importers.renderware.formats import gxt as gxtfmt
from darkstl.importers.renderware.formats import ifp as ifpfmt
from darkstl.importers.renderware.formats import img as imgfmt
from darkstl.importers.renderware.formats import text as textfmt
from darkstl.importers.renderware.formats import txd as txdfmt
from darkstl.model import MeshOptions

SA_NAME = "Grand Theft Auto: San Andreas"
III_NAME = "Grand Theft Auto III"


@pytest.fixture(autouse=True)
def _fresh():
    files.forget()
    yield
    files.forget()


@pytest.fixture
def sa(tmp_path):
    return S.sa_game(tmp_path / "GTA San Andreas")


@pytest.fixture
def iii(tmp_path):
    return S.iii_game(tmp_path / "GTA3")


# ---------------------------------------------------------------------------
# formats


def test_library_ids_and_versions():
    for v in (0x31001, 0x33002, 0x34003, 0x36003):
        assert ck.version_of(ck.library_id(v)) == v
    assert ck.version_of(0x1803FFFF) == 0x36003
    assert ck.version_of(0x00000310) == 0x31000


def test_img_archives_both_versions(tmp_path):
    members = [("a.dff", b"x" * 10), ("B.TXD", b"y" * 3000)]
    imgfmt.write_img(tmp_path / "v2.img", members, version=2)
    imgfmt.write_img(tmp_path / "v1.img", members, version=1)
    for name, version in (("v2.img", 2), ("v1.img", 1)):
        arc = imgfmt.read_img(tmp_path / name)
        assert arc.version == version and arc.names() == ["a.dff", "B.TXD"]
        assert arc.read(arc.find("b.txd"))[:3000] == b"y" * 3000
        assert arc.read(arc.find("A.dff"))[:10] == b"x" * 10
    (tmp_path / "v1.dir").unlink()
    with pytest.raises(FormatError, match=".dir"):
        imgfmt.read_img(tmp_path / "v1.img")


def test_clump_frames_geometry_materials_and_skin():
    c = dfffmt.read_clump(S.ped())
    assert c.version == S.SA and [f.name for f in c.frames] == ["male01", "Root", "Pelvis", "Head"]
    assert c.hierarchy() == [(0, 0, 0), (1, 1, 0), (2, 2, 0)] and c.bone_frames() == [1, 2, 3]
    g = c.geometries[0]
    assert g.positions.shape == (16, 3) and len(g.triangles) == 24 and g.materials[0].texture == "male01"
    assert g.skin.bones == 3 and g.skin.indices[0, 0] == 1 and g.skin.indices[9, 0] == 2
    assert np.allclose(g.skin.inverse_bind[1][:3, 3], (0, 0, -1.0))
    world = c.frame_world()
    assert np.allclose(world[3][:3, 3], (0, 0, 1.7))
    # III's old layout: surface properties in the geometry, skins with 0xDEADDEAD before each matrix
    old = dfffmt.read_clump(S.prop(S.III))
    assert old.version == S.III and len(old.geometries[0].triangles) == 12


def test_texture_rasters_decode(tmp_path):
    pal = bytes([255, 0, 0, 255] + [0, 255, 0, 255] + [0] * 4 * 254)
    natives = [
        S.solid_bc1("red", S.RED565),
        S.native("bgra", 2, 1, "8888", [bytes([255, 0, 0, 255, 0, 0, 255, 128])], alpha=True),
        S.native("pal", 2, 1, "PAL8", [bytes([0, 1])], palette=pal),
        S.native("argb", 1, 1, "1555", [struct.pack("<H", 0x8000 | 31 << 10)], alpha=True),
        S.solid_bc1("d3d8", S.BLUE565, platform=8, version=S.III),
    ]
    d = txdfmt.read_txd(S.txd(natives))
    by = d.by_name()
    assert list(by) == ["red", "bgra", "pal", "argb", "d3d8"]
    assert tuple(by["red"].decode()[0, 0]) == (255, 0, 0, 255)
    assert tuple(by["bgra"].decode()[0, 0]) == (0, 0, 255, 255) and tuple(by["bgra"].decode()[0, 1]) == (255, 0, 0, 128)
    assert tuple(by["pal"].decode()[0, 1]) == (0, 255, 0, 255)
    assert tuple(by["argb"].decode()[0, 0]) == (255, 0, 0, 255)
    assert tuple(by["d3d8"].decode()[0, 0])[:3] == (0, 0, 255)
    from darkstl.textures.dds import decode_dds
    assert tuple(decode_dds(by["red"].to_dds())[0, 0]) == (255, 0, 0, 255)  # kept compressed
    assert tuple(decode_dds(by["pal"].to_dds())[0, 1]) == (0, 255, 0, 255)  # paletted: as RGBA


def test_animation_packages_both_formats():
    keys = [(0.0, 0.0, (0, 0, 1)), (1.0, np.pi / 2, (0, 2, 1))]
    for data in (S.anp3("ped", [("walk", [dict(name="Pelvis", bone_id=1, keys=keys)])]),
                 S.anp3("ped", [("walk", [dict(name="Pelvis", bone_id=1, keys=keys)])], compressed=True),
                 S.anpk("ped", [("walk", [dict(name="Pelvis", keys=keys)])])):
        pkg = ifpfmt.read_ifp(data)
        a = pkg.animations[0]
        assert a.name == "walk" and a.duration == pytest.approx(1.0)
        t = a.tracks[0]
        assert t.name == "Pelvis" and np.allclose(t.translations[-1], (0, 2, 1), atol=1e-3)
        rot, tr = ifpfmt.sample(t, np.array([0.5]))
        m = ifpfmt.quat_matrix(rot)[0]
        assert np.allclose(m @ (1, 0, 0), (np.cos(np.pi / 4), np.sin(np.pi / 4), 0), atol=2e-3)  # 45° about z
        assert np.allclose(tr[0], (0, 1, 1), atol=1e-3)
    with pytest.raises(FormatError):
        ifpfmt.read_ifp(b"NOPE" + bytes(8))


def test_text_files_and_names():
    assert gxtfmt.read_gxt(S.gxt_sa({"LANDSTK": "Landstalker"})).get("landstk") == "Landstalker"
    assert gxtfmt.read_gxt(S.gxt_iii({"LANDSTK": "Landstalker"})).get("LANDSTK") == "Landstalker"
    defs, parents = textfmt.definitions("# comment\nobjs\n1, a, b, 100, 0\nend\ntxdp\nb, generic\nend\n", "x.ide")
    assert [(d.id, d.model, d.txd, d.section) for d in defs] == [(1, "a", "b", "objs")] and parents == [("b", "generic")]
    cc = textfmt.car_colours("col\n1,2,3\n4 5 6\nend\ncar\nfoo, 0,1, 1,1\nend\ncar4\nbar, 0,1,0,1\nend\n")
    assert cc.combinations("FOO") == [((1, 2, 3), (4, 5, 6)), ((4, 5, 6), (4, 5, 6))]
    assert cc.combinations("bar") == [((1, 2, 3), (4, 5, 6), (1, 2, 3), (4, 5, 6))]
    pl = textfmt.binary_placements(b"bnry" + struct.pack("<6ii", 1, 0, 0, 0, 0, 0, 32) + b"\0" * 0
                                   + struct.pack("<7f3i", 1, 2, 3, 0, 0, 0, 1, 3000, 0, -1))
    assert pl[0].id == 3000 and pl[0].position == pytest.approx((1, 2, 3))


# ---------------------------------------------------------------------------
# game folders


def test_games_are_recognised(sa, iii, tmp_path):
    assert library.detect_game(sa) == SA_NAME and library.detect_game(iii) == III_NAME
    assert library.detect_game(sa / "models") == SA_NAME  # a folder inside the install
    de = tmp_path / "GTA San Andreas - Definitive Edition"
    (de / "Gameface/Content/Paks").mkdir(parents=True)
    assert RenderWareImporter().detect_game(de) is None
    assert SA_NAME in library.known_games() and "Grand Theft Auto: Vice City" in library.known_games()


def test_a_profile_in_the_settings_folder_replaces_the_shipped_one(tmp_path, monkeypatch):
    override = tmp_path / "profiles"
    override.mkdir()
    (override / "gtasa.json").write_text('{"name": "San Andreas (modded)", "engine": "renderware", '
                                         '"recognise": {"files": ["gta_sa.exe"]}}')
    monkeypatch.setattr(profiles, "settings_folder", lambda: override)
    games.forget()
    try:
        assert "San Andreas (modded)" in games.game_names() and SA_NAME not in games.game_names()
        assert games.recognise(S.sa_game(tmp_path / "sa")).name == "San Andreas (modded)"
    finally:
        games.forget()


def test_the_library_sorts_models_by_their_definitions(sa):
    found, notes = library.scan_folder(sa)
    ours = {e.stem: e for e in found if e.importer == "renderware"}
    assert set(ours) == {"landstal", "male01", "crate01", "unlisted"}
    assert (ours["landstal"].category, ours["landstal"].group, ours["landstal"].label) == \
        ("Vehicles", "Cars", "Landstalker")
    assert (ours["male01"].category, ours["male01"].group) == ("Characters", "Civilians")
    assert (ours["crate01"].category, ours["crate01"].group) == ("Map Pieces", "LA / LAe")
    assert ours["unlisted"].category == "Other"
    assert all(e.game == SA_NAME for e in ours.values())
    assert ours["landstal"].path.as_posix().endswith("gta3.img/landstal.dff") and notes == []
    assert importer_for(ours["landstal"].path).key == "renderware"


def test_a_car_has_its_parts_wheels_and_colours(sa):
    library.scan_folder(sa)
    path = sa / "models/gta3.img/landstal.dff"
    m = open_model(path)
    assert m.name == "Landstalker" and m.game == SA_NAME
    assert m.variants == ["Paint 1 (#c81414, #f5f5f5)", "Paint 2 (#f5f5f5, #000000)"] and m.variant == m.variants[0]
    names = [p.name for p in m.parts]
    assert names[:5] == ["chassis", "door_lf_ok", "door_lf_dam (damaged)", "chassis_vlo (low detail)", "wheel"]
    assert sorted(names[5:]) == ["wheel (left back)", "wheel (left front)", "wheel (right back)"]
    assert {o.key for o in m.options} == {"damaged", "low_detail", "wheels"}
    mesh = m.build_mesh()
    assert set(np.unique(mesh.face_group)) == {0, 1, 4, 5, 6, 7}  # no damaged door, no low-detail body
    lo, hi = mesh.bounds()
    assert hi[0] == pytest.approx(1.15) and lo[0] == pytest.approx(-1.15)  # wheels on both sides
    assert mesh.n_faces == 12 + 12 + 4 * 12
    every = m.build_mesh(MeshOptions(flags={"damaged": True, "low_detail": True, "wheels": False}))
    assert set(np.unique(every.face_group)) == {0, 1, 2, 3, 4}
    # paint: the body takes the car's first colour, multiplied into its texture; the door the second
    body = next(mt for mt in m.materials if mt.name == "body (paint 1)")
    assert tuple(m.textures[body.albedo].decode()[0, 0][:3]) == (200, 20, 20)
    door = next(mt for mt in m.materials if mt.name == "body (paint 2)")
    assert tuple(m.textures[door.albedo].decode()[0, 0][:3]) == (245, 245, 245)
    glass = next(mt for mt in m.materials if mt.name == "glass")
    assert glass.albedo == "glass" and tuple(m.textures["glass"].decode()[0, 0][:3]) == (0, 0, 255)
    tyre = next(mt for mt in m.materials if mt.name == "tyre")
    assert m.textures[tyre.albedo].source == "vehicle.txd"  # the shared vehicle dictionary
    other = open_model(path, m.variants[1])
    body2 = next(mt for mt in other.materials if mt.name == "body (paint 1)")
    assert tuple(other.textures[body2.albedo].decode()[0, 0][:3]) == (245, 245, 245)
    assert importer_for(path).variants(path) == m.variants


def test_a_ped_is_skinned_and_posed_by_ped_ifp(sa):
    library.scan_folder(sa)
    m = open_model(sa / "models/gta3.img/male01.dff")
    assert m.textures[m.materials[0].albedo].source == "gta3.img/male01.txd"
    infos = m.animations.list()
    assert [i.name for i in infos] == ["walk", "idle_stance"]  # car_open moves no bone of this ped
    walk = infos[0]
    assert walk.frames == 31 and walk.fps == 30
    bind = m.build_mesh()
    joints, weights = bind.metadata["skin"]
    assert joints.max() == 2 and weights.max() == 1
    posed = m.build_mesh(MeshOptions(pose=("walk", 30)))
    # the pelvis turns 90° about z; the walk's two metres forward are taken out
    _lo, hi = posed.bounds()
    _blo, bhi = bind.bounds()
    assert hi[2] == pytest.approx(bhi[2], abs=1e-4)
    torso = posed.vertices[np.isclose(posed.vertices[:, 2], 0.7, atol=1e-3)]
    assert np.ptp(torso[:, 0]) == pytest.approx(0.3, abs=1e-3) and np.ptp(torso[:, 1]) == pytest.approx(0.4, abs=1e-3)
    assert m.rig is not None and m.rig.names == ["Root", "Pelvis", "Head"]
    _names, local = m.rig.pose_all("walk")
    assert local.shape == (31, 3, 4, 4)


def test_iii_reads_dir_archives_old_files_and_names_bones(iii):
    found, _notes = library.scan_folder(iii)
    ours = {e.stem: e for e in found if e.importer == "renderware"}
    assert ours["landstal"].label == "Landstalker" and ours["landstal"].game == III_NAME
    car = open_model(ours["landstal"].path)
    assert car.variants == ["Paint 1 (#0a0ac8, #f0f0f0)"]
    tyre = next(mt for mt in car.materials if mt.name == "tyre")
    assert car.textures[tyre.albedo].source == "gta3.img/generic.txd"
    ped = open_model(ours["male01"].path)
    assert [a.name for a in ped.animations.list()] == ["walk_civi"]  # matched by bone name
    assert ped.build_mesh(MeshOptions(pose=("walk_civi", 15))).n_faces == 24


def test_loose_files_without_the_game_open_too(tmp_path):
    d = tmp_path / "exported"
    d.mkdir()
    (d / "crate01.dff").write_bytes(S.prop())
    (d / "crate01.txd").write_bytes(S.txd([S.solid_bc1("crate", S.RED565)]))
    found, _notes = library.scan_folder(d, "Other models")
    ours = [e for e in found if e.importer == "renderware"]
    assert [(e.category, e.stem) for e in ours] == [("Models", "crate01")]
    m = open_model(d / "crate01.dff")
    assert tuple(m.textures[m.materials[0].albedo].decode()[0, 0]) == (255, 0, 0, 255)


def test_missing_textures_are_named(sa):
    library.scan_folder(sa)
    m = open_model(sa / "models/gta3.img/unlisted.dff")
    assert any("missing" in n for n in m.notes)


def test_related_files_and_shared_files(sa):
    library.scan_folder(sa)
    imp = RenderWareImporter()
    rel = imp.related_files(sa / "models/gta3.img/male01.dff")
    assert [(r.save_as or r.path.name, r.kind) for r in rel] == [
        ("models/gta3/male01.dff", "model"), ("models/gta3/male01.txd", "texture"), ("ped.ifp", "animation")]
    assert rel[0].extract()[:4] == struct.pack("<I", ck.CLUMP)
    shared = {r.path.name.lower() for r in imp.shared_files(sa, SA_NAME)}
    assert {"gta.dat", "default.dat", "vehicles.ide", "peds.ide", "lae.ide", "carcols.dat", "american.gxt",
            "vehicle.txd", "ped.ifp"} <= shared


def test_textures_are_listed_for_the_picker(sa):
    library.scan_folder(sa)
    imp = RenderWareImporter()
    assert [n for n, _ in imp.list_textures(sa)] == ["vehicle/tyre"]  # loose only, unless asked for all
    deep = dict(imp.list_textures(sa, deep=True))
    assert "landstal/body" in deep and "lae/crate" in deep
    assert tuple(imp.open_texture(sa, deep["male01/male01"]).decode()[0, 0]) == (255, 0, 0, 255)


# ---------------------------------------------------------------------------
# through the app's engine


def test_the_engine_adds_opens_and_collects_a_gta_game(home, sa):  # noqa: F811
    s = make_service(home)
    assert s.call("folder_check", {"path": str(sa)})["game"] == SA_NAME
    r = wait(s, s.call("folder_add", {"path": str(sa)})["job"])
    assert r["count"] == 4 and r["game"] == SA_NAME
    items = {i["stem"]: i for i in s.call("models_query", {"game": SA_NAME})["items"]}
    opened = s.call("model_open", {"id": items["landstal"]["id"]})
    assert opened["game"] == SA_NAME and opened["stats"]["triangles"] == 72
    assert s.call("thumb", {"id": items["male01"]["id"]})[:4] == b"\x89PNG"
    plan = s.call("collect_plan", {"ids": [items["male01"]["id"]]})
    done = wait(s, s.call("collect_run", {"ids": [items["male01"]["id"]]})["job"])
    folder = Path(plan["folder"])
    assert done["copied"] == plan["files"]
    assert (folder / "models/gta3/male01.dff").is_file() and (folder / "data/peds.ide").is_file()
    files.forget()
    copy = open_model(folder / "models/gta3/male01.dff")  # the copy works on its own: textures and poses
    assert copy.game == SA_NAME and copy.textures[copy.materials[0].albedo].source == "male01.txd"
    assert [a.name for a in copy.animations.list()] == ["walk", "idle_stance"]
    # and as a package
    done = wait(s, s.call("collect_run", {"ids": [items["landstal"]["id"]], "packages": True})["job"])
    assert done.get("written", done.get("copied", 1))
