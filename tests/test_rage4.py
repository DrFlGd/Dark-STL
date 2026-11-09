"""The GTA IV side of the RAGE importer: the archive key found in the executable, IMG3 archives (plain and
encrypted), RSC5 resources, a whole fake game with an episode in the library, vehicles, ped outfits, and
Collect through the app's engine."""

import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
import synth_rage4 as S4
from test_app import home, make_service, wait  # noqa: F401 - fixture

from darkstl import library
from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.rage import RageImporter, files, games
from darkstl.importers.rage.formats import drawable4 as d4
from darkstl.importers.rage.formats import ide4, keys, keys4, rsc5
from darkstl.importers.rage.formats import img as imgfmt
from darkstl.importers.rage.formats.text import joaat

GTA4 = "Grand Theft Auto IV"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    files.forget()
    keys.forget()
    keys4.forget()
    S4.install_fake_hash(monkeypatch)
    yield
    files.forget()
    keys.forget()
    keys4.forget()


@pytest.fixture
def game(tmp_path):
    return S4.gta4_game(tmp_path / "Grand Theft Auto IV")


def _colour(tex, rgb) -> bool:
    return bool(np.all(np.abs(tex.decode()[0, 0][:3].astype(int) - rgb) <= 8))


def _reader(blob: bytes):
    return rsc5.parse_file(blob).reader(False)


# ---------------------------------------------------------------------------
# archives, key, resources


def test_img_archives_plain_and_encrypted(tmp_path):
    members = {"a.txt": b"hello", "t.wtd": S4.wtd([("t", (1, 2, 3))])}
    (tmp_path / "plain.img").write_bytes(S4.img(members))
    (tmp_path / "secret.img").write_bytes(S4.img(members, S4.KEY))
    plain = imgfmt.read_img(tmp_path / "plain.img", None)
    assert [(e.name, e.resource) for e in plain.files()] == [("a.txt", False), ("t.wtd", True)]
    assert plain.read(plain.files()[0]) == b"hello"
    with pytest.raises(FormatError):
        imgfmt.read_img(tmp_path / "secret.img", None)
    with pytest.raises(FormatError):
        imgfmt.read_img(tmp_path / "secret.img", bytes(32))  # the wrong key
    secret = imgfmt.read_img(tmp_path / "secret.img", S4.KEY)
    res = secret.read(secret.files()[1])
    assert res == members["t.wtd"] and rsc5.is_resource_file(res)


def test_the_key_is_found_in_the_executable_and_kept(tmp_path):
    folder = tmp_path / "gta"
    folder.mkdir()
    assert keys4.for_game(folder)[0] is None
    (folder / "GTAIV.exe").write_bytes(b"MZ" + b"\0" * 1002 + S4.KEY + b"\0" * 64)
    seen = []
    key, why = keys4.for_game(folder, seen.append)
    assert key == S4.KEY and why is None and seen[-1] == 1.0
    assert (tmp_path / "home" / "keys" / "gta4.bin").read_bytes() == S4.KEY
    keys4.forget()
    (folder / "GTAIV.exe").unlink()
    assert keys4.cached() == S4.KEY
    keys4.forget()
    (tmp_path / "home" / "keys" / "gta4.bin").unlink()
    (folder / "GTAIV.exe").write_bytes(b"MZ" + b"\0" * 4000)
    key, why = keys4.for_game(folder)
    assert key is None and "GTAIV.exe" in why


def test_resource_sizes():
    for s, g in ((1, 0), (300, 70_000), (1 << 20, 5 << 20)):
        f = rsc5.flags_for(s, g)
        assert rsc5.system_size(f) >= s and rsc5.graphics_size(f) >= g
    with pytest.raises(FormatError):
        rsc5.parse_file(struct.pack("<3I", rsc5.MAGIC_BE, 110, 0) + b"\0" * 8)


def test_textures_drawables_dictionaries_and_fragments_read():
    d = d4.read_texture_dictionary(_reader(S4.wtd([("red", (200, 0, 0)), ("grey", (90, 90, 90))])))
    assert set(d) == {"red", "grey"} and d["red"].codec == "BC1" and _colour(d["grey"], (90, 90, 90))
    assert d4.texture_key("pack:/Admiral_Badges.dds") == "admiral_badges"
    ydd = d4.read_dictionary(_reader(S4.ped_wdd()))
    assert [h for h, _ in ydd] == [joaat("head_000_r"), joaat("uppr_000_u"), joaat("lowr_000_u")]
    head = ydd[0][1]
    m = head.models()[0]
    assert m.skinned and head.shaders[0].textures == [(joaat("TextureSampler"), "head_diff_000_a_whi")]
    g = m.geometries[0]
    assert len(g.positions) == 8 and g.weights[:, 0].max() == 1.0 and set(g.indices[:, 0]) == {2}
    frag = d4.read_fragment(_reader(S4.car_wft()), "admiral")
    bones = frag.drawable.skeleton.bones
    assert [b.name for b in bones][:3] == ["chassis", "wheel_lf", "wheel_rf"] and bones[1].parent == 0
    assert np.allclose(bones[1].translation, (-0.8, 1.3, 0.3))
    assert [(c.bone_tag, len(c.drawable.models())) for c in frag.children] == [(101, 1)]


def test_item_definitions_and_text():
    cars, peds, parents = ide4.definitions(S4.ide(cars=[("admiral", "car", "ADMIRAL")], peds=[("m_y_x", "CIVMALE")],
                                                   parents=[("a", "b")]).decode())
    assert [(c.model, c.txd, c.kind, c.label) for c in cars] == [("admiral", "admiral", "CAR", "ADMIRAL")]
    assert [(p.model, p.ped_type) for p in peds] == [("m_y_x", "CIVMALE")] and parents == [("a", "b")]
    assert ide4.text(S4.gxt({"ADMIRAL": "Admiral"})) == {joaat("ADMIRAL"): "Admiral"}


# ---------------------------------------------------------------------------
# a game


def test_the_game_is_recognised(game):
    assert games.recognise(game).name == GTA4 and games.profile(GTA4).generation == 4
    assert RageImporter().detect_game(game / "pc") == GTA4


def test_the_library_sorts_gta4_models(game):
    found, notes = library.scan_folder(game)
    ours = {e.stem: e for e in found if e.importer == "rage"}
    assert notes == []
    assert set(ours) == {"admiral", "m_y_test_01", "w_glock", "cj_crate_01", "hexer"}
    assert (ours["admiral"].category, ours["admiral"].group, ours["admiral"].label) == ("Vehicles", "Cars", "Admiral")
    assert (ours["hexer"].group, ours["hexer"].label) == ("Motorcycles (The Lost and Damned)", "Hexer")
    assert (ours["m_y_test_01"].category, ours["m_y_test_01"].group) == ("Characters", "Young men")
    assert (ours["w_glock"].category, ours["cj_crate_01"].category) == ("Weapons", "Objects")
    assert all(e.game == GTA4 and e.packed for e in ours.values())
    assert (game.parent / "home" / "keys" / "gta4.bin").is_file()  # found in GTAIV.exe once, and kept
    assert importer_for(ours["admiral"].path).key == "rage"


def test_a_car_has_a_wheel_on_every_corner(game):
    library.scan_folder(game)
    m = open_model(game / "pc/models/cdimages/vehicles.img/admiral.wft")
    assert m.name == "Admiral" and m.game == GTA4
    assert [p.name for p in m.parts] == ["chassis", "door_dside_f", "wheel_lf", "wheel_rf", "wheel_lr", "wheel_rr"]
    mesh = m.build_mesh()
    assert mesh.n_faces == 72
    lo, hi = mesh.bounds()
    assert lo[0] == pytest.approx(-0.95) and hi[0] == pytest.approx(0.95)
    body = m.materials[m.parts[0].material]
    assert _colour(m.textures[body.albedo], (20, 60, 200)) and body.normal == "admiral_body_n"
    tyre = m.materials[m.parts[3].material]
    assert m.textures[tyre.albedo].source == "vehshare.wtd"  # the parent dictionary the .ide names
    assert "GTA IV fragment" in m.info[0]


def test_a_ped_is_an_outfit_on_its_skeleton(game):
    found, _notes = library.scan_folder(game)
    path = game / "pc/models/cdimages/componentpeds.img/m_y_test_01.wdd"
    imp = RageImporter()
    assert imp.variants(path) == ["head_000_r", "uppr_000_u", "lowr_000_u"]
    (cfg,) = imp.configurations(game, GTA4, found)
    assert cfg.id == "gta4-m_y_test_01" and cfg.base.variant == "head_000_r"
    assert [s.id for s in cfg.slots] == ["uppr", "lowr"]
    top = open_model(path, "uppr_000_u")
    assert _colour(top.textures[top.materials[0].albedo], (200, 40, 40))
    joints, _w = top.build_mesh().metadata["skin"]
    assert set(joints[:, 0]) == {1}  # skinned to the pelvis of the ped's skeleton (its .wft)
    assert top.animations is None and ".wad" in top.pose_note


def test_loose_exports_open_without_the_game(tmp_path):
    d = tmp_path / "exported"
    d.mkdir()
    (d / "w_glock.wdr").write_bytes(S4.wdr([([S4.Geo(*S4.box((0.1, 0.2, 0.1)))], 0, False)],
                                          [[("TextureSampler", "w_glock")]]))
    (d / "w_glock.wtd").write_bytes(S4.wtd([("w_glock", (60, 60, 60))]))
    found, _notes = library.scan_folder(d, "Other models")
    assert [e.stem for e in found if e.importer == "rage"] == ["w_glock"]
    m = open_model(d / "w_glock.wdr")
    assert _colour(m.textures[m.materials[0].albedo], (60, 60, 60))


def test_textures_listed_and_archives_described(game):
    library.scan_folder(game)
    imp = RageImporter()
    deep = dict(imp.list_textures(game, deep=True))
    assert "admiral/admiral_body" in deep and _colour(imp.open_texture(game, deep["vehshare/vehicle_tyre"]), (30, 30, 30))
    assert imp.describe(game / "pc/models/cdimages/vehicles.img")[0] == "GTA IV archive (IMG3), 3 files"


# ---------------------------------------------------------------------------
# through the app's engine


def test_the_engine_adds_opens_and_collects_gta4(home, game):  # noqa: F811
    s = make_service(home)
    assert s.call("folder_check", {"path": str(game)})["game"] == GTA4
    r = wait(s, s.call("folder_add", {"path": str(game)})["job"])
    assert r["count"] >= 5 and r["game"] == GTA4
    items = {i["stem"]: i for i in s.call("models_query", {"game": GTA4})["items"]}
    assert s.call("model_open", {"id": items["admiral"]["id"]})["stats"]["triangles"] == 72
    ids = [items["admiral"]["id"], items["m_y_test_01"]["id"]]
    plan = s.call("collect_plan", {"ids": ids})
    done = wait(s, s.call("collect_run", {"ids": ids})["job"])
    folder = Path(plan["folder"])
    assert done["copied"] == plan["files"]
    car = folder / "pc/models/cdimages/vehicles.img/admiral.wft"
    assert car.is_file() and (folder / "common/data/vehicles.ide").is_file()
    # the copy works with the game and the key gone
    shutil.rmtree(game)
    files.forget()
    keys4.forget()
    (home / "keys" / "gta4.bin").unlink()
    found, _notes = library.scan_folder(folder)
    assert {e.stem: e.label for e in found if e.importer == "rage"}["admiral"] == "Admiral"
    copy = open_model(car)
    assert copy.build_mesh().n_faces == 72
    assert copy.textures[copy.materials[copy.parts[3].material].albedo].source == "vehshare.wtd"
    ped = open_model(folder / "pc/models/cdimages/componentpeds.img/m_y_test_01.wdd", "lowr_000_u")
    assert set(ped.build_mesh().metadata["skin"][0][:, 0]) == {1}
