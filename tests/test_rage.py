"""The GTA V importer: archive keys found in the executable, RPF7 archives (plain, NG encrypted,
nested), resources in both the original and the Enhanced layouts, a whole fake game in the library,
vehicles, ped outfits and posing, and Collect through the app's engine."""

import shutil
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import synth_rage as S
from test_app import home, make_service, wait  # noqa: F401 - fixture

from darkstl import library
from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.rage import RageImporter, files, games
from darkstl.importers.rage.formats import clip as clipfmt
from darkstl.importers.rage.formats import crypto, keyhashes, keys, rpf, rsc, text
from darkstl.importers.rage.formats import drawable as dfmt
from darkstl.importers.rage.formats import texture as tfmt
from darkstl.model import MeshOptions

GTA5 = "Grand Theft Auto V"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    files.forget()
    keys.forget()
    yield
    files.forget()
    keys.forget()


@pytest.fixture
def fake():
    return S.FakeKeys()


@pytest.fixture
def game(tmp_path, fake, monkeypatch):
    S.install_fake_hashes(monkeypatch, fake)
    return S.gta5_game(tmp_path / "Grand Theft Auto V", fake)


def _colour(tex, rgb) -> bool:
    """The texture's first pixel is ``rgb``, give or take BC1's rounding."""
    return bool(np.all(np.abs(tex.decode()[0, 0][:3].astype(int) - rgb) <= 8))


def _reader(blob: bytes) -> rsc.Reader:
    res = rsc.parse_file(blob)
    return res.reader(res.version in (5, 159, 171))


# ---------------------------------------------------------------------------
# keys and archives


def test_the_published_hashes_are_complete():
    assert len(keyhashes.AES_KEY) == 20 and len(keyhashes.LUT) == 20
    assert len(keyhashes.NG_KEYS) == 101 and len(keyhashes.NG_TABLES) == 272
    assert all(len(h) == 20 for h in keyhashes.NG_KEYS + keyhashes.NG_TABLES)


def test_ng_and_aes_decrypt_and_the_keys_keep(fake):
    real = fake.real()
    data = bytes(range(256)) * 3 + b"tail"
    enc = fake.encrypt_ng(data, "x64e.rpf", 4096)
    assert enc != data and enc[-4:] == b"tail"
    assert crypto.decrypt_ng(enc, real.ng_key("x64e.rpf", 4096), real.ng_tables) == data
    assert real.name_hash("x64e.rpf") != real.name_hash("x64f.rpf")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    e = Cipher(algorithms.AES(fake.aes), modes.ECB()).encryptor()
    assert crypto.decrypt_aes(e.update(data[:768]) + e.finalize() + b"tail", fake.aes) == data
    again = crypto.Keys.from_bytes(real.to_bytes())
    assert again.aes == real.aes and again.lut == real.lut
    assert np.array_equal(again.ng_keys, real.ng_keys) and np.array_equal(again.ng_tables, real.ng_tables)
    with pytest.raises(FormatError):
        crypto.Keys.from_bytes(b"DSK5" + b"\0" * 10)


def test_keys_are_found_in_the_executable_and_kept(tmp_path, fake, monkeypatch):
    S.install_fake_hashes(monkeypatch, fake)
    found = keys.search(fake.exe())
    assert found.aes == fake.aes and found.lut == fake.lut
    assert np.array_equal(found.ng_keys, fake.ng_keys) and np.array_equal(found.ng_tables, fake.tables)
    folder = tmp_path / "game"
    folder.mkdir()
    got, why = keys.for_game(folder)
    assert got is None and "GTA5.exe" in why
    (folder / "GTA5.exe").write_bytes(fake.exe())
    seen = []
    got, why = keys.for_game(folder, seen.append)
    assert why is None and got.aes == fake.aes and seen and seen[-1] == 1.0
    assert (tmp_path / "home" / "keys" / "gta5.bin").is_file()  # kept in the settings, not beside the game
    keys.forget()
    (folder / "GTA5.exe").unlink()
    assert keys.cached().aes == fake.aes  # the search doesn't run again
    # an executable without them says what is missing
    keys.forget()
    (tmp_path / "home" / "keys" / "gta5.bin").unlink()
    (folder / "GTA5.exe").write_bytes(b"MZ" + b"\0" * 4000)
    got, why = keys.for_game(folder)
    assert got is None and "AES key" in why


def test_rpf_archives_plain_encrypted_and_nested(tmp_path, fake):
    inner = S.archive({"deep/b.txt": b"inner file"})
    blob = S.archive({"a.txt": b"hello", "z/packed.bin": ("deflate", b"x" * 5000), "z/inner.rpf": ("rpf", inner),
                      "t.ytd": ("rsc", S.ytd([("t", (1, 2, 3))]))}, rpf.ENC_NG, fake, "outer.rpf")
    (tmp_path / "outer.rpf").write_bytes(blob)
    with pytest.raises(FormatError):
        rpf.read_archive(tmp_path / "outer.rpf", None)  # encrypted, and no keys
    arc = rpf.read_archive(tmp_path / "outer.rpf", fake.real())
    by = {e.path: e for e in arc.files()}
    assert set(by) == {"a.txt", "z/packed.bin", "z/inner.rpf", "t.ytd"}
    assert arc.read(by["a.txt"], None) == b"hello"
    assert arc.read(by["z/packed.bin"], None) == b"x" * 5000
    assert by["t.ytd"].resource and by["t.ytd"].version == 13
    sub = rpf.nested(arc, by["z/inner.rpf"], fake.real())
    assert [(e.path, sub.read(e, None)) for e in sub.files()] == [("deep/b.txt", b"inner file")]
    (tmp_path / "plain.rpf").write_bytes(S.archive({"a.txt": b"open"}))
    plain = rpf.read_archive(tmp_path / "plain.rpf", None)
    assert plain.read(plain.files()[0], None) == b"open"


def test_resource_sizes_and_flags():
    for size in (1, 4096, 8192, 100_000, 3_000_000):
        assert rsc.size_from_flags(rsc.flags_for(size)) >= size
    assert not rsc.is_resource_file(b"RIFF") and rsc.is_resource_file(struct.pack("<I", rsc.MAGIC))


# ---------------------------------------------------------------------------
# resources, both layouts


@pytest.mark.parametrize("gen9", [False, True])
def test_texture_dictionaries_decode(gen9):
    blob = S.ytd([("red", (200, 0, 0)), ("green", (0, 200, 0))], gen9)
    d = tfmt.read_dictionary(_reader(blob), rsc.SYSTEM)
    assert set(d) == {"red", "green"} and d["red"].width == 8 and d["red"].codec == "BC1"
    assert tuple(d["green"].decode()[0, 0]) == (0, 203, 0, 255)
    assert d["red"].to_dds()[:4] == b"DDS "


@pytest.mark.parametrize("gen9", [False, True])
def test_drawables_dictionaries_and_fragments_read(gen9):
    dr = dfmt.read_drawable(_reader(S.prop_ydr(gen9)))
    assert dr.name == "prop_crate_01" and list(dr.textures) == ["crate_d"]
    (g,) = dr.models()[0].geometries
    assert len(g.positions) == 8 and len(g.triangles) == 12
    assert dr.shaders[0].textures == [(text.joaat("DiffuseSampler"), "crate_d")]
    ydd = dfmt.read_dictionary(_reader(S.ped_ydd(gen9)))
    assert sorted(h for h, _ in ydd) == sorted(text.joaat(n) for n in
                                                ("head_000_r", "uppr_000_u", "uppr_001_u", "lowr_000_u"))
    skinned = ydd[0][1].models()[0]
    assert skinned.skinned and skinned.geometries[0].bone_ids
    frag = dfmt.read_fragment(_reader(S.car_yft(gen9)))
    assert frag.name == "adder" and [b.name for b in frag.drawable.skeleton.bones][:2] == ["chassis", "wheel_lf"]
    assert [(c.bone_tag, bool(c.drawable and c.drawable.models())) for c in frag.children] == [
        (27922, True), (26418, False), (27902, False), (26398, False)]


def test_clips_are_sampled():
    clips = {c.name: c for c in clipfmt.read_clips(_reader(S.walk_ycd()))}
    assert set(clips) == {"walk", "idle"}
    walk = clips["walk"].animation
    assert walk.frames == 11 and walk.tracks == [(31086, 1), (11816, 0)]
    first, last = walk.sample(0), walk.sample(10)
    assert np.allclose(first[(31086, 1)], (0, 0, 0, 1), atol=1e-3)
    assert np.allclose(last[(31086, 1)], (0, 0, np.sqrt(0.5), np.sqrt(0.5)), atol=1e-3)
    assert np.allclose(last[(11816, 0)], (0, 0, 1))


def test_text_and_game_lists():
    assert text.joaat("adder") == 0xB779A091 and text.joaat("ADDER") == 0xB779A091
    assert text.read_gxt2(S.gxt2({"ADDER": "Adder", "WT_PIST": "Pistol"})) == {
        text.joaat("ADDER"): "Adder", text.joaat("WT_PIST"): "Pistol"}
    (v,) = text.vehicles(S.vehicles_meta([{"model": "adder", "label": "ADDER", "maker": "TRUFFADE"}]))
    assert (v.model, v.label, v.maker, v.vclass) == ("adder", "ADDER", "TRUFFADE", "VC_SUPER")
    (w,) = text.weapons(S.weapons_meta([("WEAPON_PISTOL", "w_pi_pistol", "WT_PIST", "GROUP_PISTOL")]))
    assert (w.model, w.group) == ("w_pi_pistol", "GROUP_PISTOL")
    assert text.vehicles(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>') == []  # no entity tricks


# ---------------------------------------------------------------------------
# a game


def test_the_game_is_recognised(game, tmp_path):
    assert games.recognise(game).name == GTA5
    assert RageImporter().detect_game(game / "update") == GTA5
    assert games.recognise(tmp_path) is None


def test_the_library_sorts_gta5_models(game):
    found, notes = library.scan_folder(game)
    ours = {e.stem: e for e in found if e.importer == "rage"}
    assert notes == []
    assert set(ours) == {"adder", "a_m_y_test_01", "w_pi_pistol", "prop_crate_01"}
    assert (ours["adder"].category, ours["adder"].group, ours["adder"].label) == ("Vehicles", "Super",
                                                                                  "Truffade Adder")
    assert (ours["a_m_y_test_01"].category, ours["a_m_y_test_01"].group) == ("Characters", "Ambient men")
    assert (ours["w_pi_pistol"].category, ours["w_pi_pistol"].group, ours["w_pi_pistol"].label) == \
        ("Weapons", "Pistols", "Pistol")
    assert (ours["prop_crate_01"].category, ours["prop_crate_01"].group) == ("Objects", "industrial")
    assert all(e.game == GTA5 and e.packed for e in ours.values())
    assert ours["adder"].path.as_posix().endswith("x64e.rpf/levels/gta5/vehicles.rpf/adder.yft")
    assert importer_for(ours["adder"].path).key == "rage"


def test_a_folder_without_the_executable_says_why(game):
    (game / "GTA5.exe").unlink()
    found, notes = library.scan_folder(game)
    assert not [e for e in found if e.importer == "rage" and e.category != "Models"]
    assert any("GTA5.exe" in n for n in notes)


def test_a_car_has_its_wheels_and_textures(game):
    library.scan_folder(game)
    m = open_model(game / "x64e.rpf/levels/gta5/vehicles.rpf/adder.yft")
    assert m.name == "Truffade Adder" and m.game == GTA5
    assert [p.name for p in m.parts] == ["chassis", "door_dside_f", "wheel_lf", "wheel_rf", "wheel_lr", "wheel_rr"]
    mesh = m.build_mesh()
    assert mesh.n_faces == 12 * 6
    lo, hi = mesh.bounds()
    assert lo[0] == pytest.approx(-0.95) and hi[0] == pytest.approx(0.95)  # a wheel at every corner
    assert lo[1] == pytest.approx(-2.0) and hi[1] == pytest.approx(2.0)
    body = m.materials[m.parts[0].material]
    assert _colour(m.textures[body.albedo], (200, 30, 30)) and body.normal == "adder_body_n"
    wheel = m.materials[m.parts[2].material]
    assert wheel.name == "wheel_lf" and m.textures[wheel.albedo].source.endswith("vehshare.ytd")
    assert any("Paint" in n for n in m.notes)


def test_a_ped_is_an_outfit_of_components_and_is_posed(game):
    found, _notes = library.scan_folder(game)
    path = game / "x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.ydd"
    imp = RageImporter()
    assert imp.variants(path) == ["head_000_r", "uppr_000_u", "uppr_001_u", "lowr_000_u"]
    (cfg,) = imp.configurations(game, GTA5, found)
    assert cfg.base.variant == "head_000_r"
    assert [(s.id, [o.model.variant for o in s.options]) for s in cfg.slots] == [
        ("uppr", ["uppr_000_u", "uppr_001_u"]), ("lowr", ["lowr_000_u"])]
    top = open_model(path, "uppr_001_u")
    assert top.variant == "uppr_001_u"
    assert _colour(top.textures[top.materials[0].albedo], (30, 200, 90))
    infos = top.animations.list()
    assert [i.name for i in infos] == ["idle", "walk"] and infos[1].frames == 11
    head = open_model(path, "head_000_r")
    bind = head.build_mesh()
    posed = head.build_mesh(MeshOptions(pose=("walk", 10)))
    # the head bone turns 90 degrees about z: the head, wider than deep, turns on itself
    assert np.ptp(bind.vertices[:, 0]) == pytest.approx(0.3) and np.ptp(bind.vertices[:, 1]) == pytest.approx(0.2)
    assert np.ptp(posed.vertices[:, 0]) == pytest.approx(0.2, abs=1e-3)
    assert np.ptp(posed.vertices[:, 1]) == pytest.approx(0.3, abs=1e-3)
    assert posed.vertices[:, 2].min() == pytest.approx(bind.vertices[:, 2].min(), abs=1e-4)
    joints, weights = bind.metadata["skin"]
    assert set(joints[:, 0]) == {3} and np.allclose(weights[:, 0], 1)
    assert head.rig is not None and head.rig.names[:2] == ["SKEL_ROOT", "SKEL_Pelvis"]
    _names, local = head.rig.pose_all("walk")
    assert local.shape == (11, 4, 4, 4)


def test_loose_exports_open_without_the_game(tmp_path):
    d = tmp_path / "exported"
    d.mkdir()
    (d / "prop_crate_01.ydr").write_bytes(S.prop_ydr(gen9=True))
    (d / "w_pi_pistol.ydr").write_bytes(S.pistol_ydr())
    (d / "w_pi_pistol.ytd").write_bytes(S.ytd([("w_pi_pistol", (60, 60, 60))]))
    found, _notes = library.scan_folder(d, "Other models")
    ours = {e.stem: e for e in found if e.importer == "rage"}
    assert set(ours) == {"prop_crate_01", "w_pi_pistol"}
    crate = open_model(d / "prop_crate_01.ydr")
    assert _colour(crate.textures[crate.materials[0].albedo], (120, 90, 30))
    pistol = open_model(d / "w_pi_pistol.ydr")
    assert pistol.materials[0].albedo == "w_pi_pistol" and any("w_pi_pistol_s" in n for n in pistol.notes)


def test_related_and_shared_files(game):
    library.scan_folder(game)
    imp = RageImporter()
    path = game / "x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.ydd"
    rel = {(r.save_as, r.kind) for r in imp.related_files(path)}
    assert {("x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.ydd", "model"),
            ("x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.ytd", "texture"),
            ("x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.yft", "other"),
            ("x64e.rpf/anim/ingame/clip_move@.rpf/move_m@generic.ycd", "animation")} <= rel
    model = next(r for r in imp.related_files(path) if r.kind == "model")
    assert model.extract()[:4] == struct.pack("<I", rsc.MAGIC)  # a resource file OpenIV would write
    shared = {r.save_as for r in imp.shared_files(game, GTA5)}
    assert {"update/update.rpf/common/data/levels/gta5/vehicles.meta",
            "update/update.rpf/x64/data/lang/americanrel.rpf/global.gxt2",
            "x64e.rpf/levels/gta5/vehicles.rpf/vehshare.ytd"} <= shared


def test_textures_are_listed_for_the_picker(game):
    library.scan_folder(game)
    imp = RageImporter()
    deep = dict(imp.list_textures(game, deep=True))
    assert "adder/adder_body" in deep and "a_m_y_test_01/lowr_diff_000_a_uni" in deep
    assert _colour(imp.open_texture(game, deep["adder/adder_body"]), (200, 30, 30))


# ---------------------------------------------------------------------------
# through the app's engine


def test_the_engine_adds_opens_and_collects_gta5(home, game):  # noqa: F811
    s = make_service(home)
    assert s.call("folder_check", {"path": str(game)})["game"] == GTA5
    r = wait(s, s.call("folder_add", {"path": str(game)})["job"])
    assert r["count"] >= 4 and r["game"] == GTA5
    items = {i["stem"]: i for i in s.call("models_query", {"game": GTA5})["items"]}
    opened = s.call("model_open", {"id": items["adder"]["id"]})
    assert opened["game"] == GTA5 and opened["stats"]["triangles"] == 72
    assert s.call("thumb", {"id": items["w_pi_pistol"]["id"]})[:4] == b"\x89PNG"
    ids = [items["a_m_y_test_01"]["id"], items["adder"]["id"]]
    plan = s.call("collect_plan", {"ids": ids})
    done = wait(s, s.call("collect_run", {"ids": ids})["job"])
    folder = Path(plan["folder"])
    assert done["copied"] == plan["files"]
    ped = folder / "x64e.rpf/models/cdimages/streamedpeds.rpf/a_m_y_test_01.ydd"
    assert ped.is_file() and (folder / "update/update.rpf/common/data/levels/gta5/vehicles.meta").is_file()
    # the copy works with the game gone: names, textures, wheels and poses
    shutil.rmtree(game)
    files.forget()
    keys.forget()
    (home / "keys" / "gta5.bin").unlink()
    found, _notes = library.scan_folder(folder)
    labels = {e.stem: e.label for e in found if e.importer == "rage"}
    assert labels["adder"] == "Truffade Adder"
    car = open_model(folder / "x64e.rpf/levels/gta5/vehicles.rpf/adder.yft")
    assert car.build_mesh().n_faces == 72
    assert car.textures[car.materials[car.parts[2].material].albedo].source.endswith("vehshare.ytd")
    copy = open_model(ped, "uppr_000_u")
    assert _colour(copy.textures[copy.materials[0].albedo], (30, 90, 200))
    assert [a.name for a in copy.animations.list()] == ["idle", "walk"]


def test_a_whole_file_resource_and_bad_data_are_refused(tmp_path):
    bad = tmp_path / "broken.ydr"
    bad.write_bytes(struct.pack("<4I", rsc.MAGIC, 165, 0, 0) + zlib.compress(b"x")[2:])
    with pytest.raises(FormatError):
        open_model(bad)


def test_the_copy_the_game_uses_comes_first():
    order = sorted(["x64e.rpf/a.ydr", "update/x64/dlcpacks/mpbeach/dlc.rpf/a.ydr",
                    "update/x64/dlcpacks/patchday3ng/dlc.rpf/a.ydr", "update/update.rpf/a.ydr"],
                   key=files._priority)
    assert order == ["update/update.rpf/a.ydr", "update/x64/dlcpacks/patchday3ng/dlc.rpf/a.ydr",
                     "update/x64/dlcpacks/mpbeach/dlc.rpf/a.ydr", "x64e.rpf/a.ydr"]
