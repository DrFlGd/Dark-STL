"""The texture picker and the library overlay: a texture found by name anywhere in the game, assigned to a
material, kept in the asset store and the overlay, and still used once the game is imported and gone."""

from __future__ import annotations

import json
import shutil

import numpy as np

import synth
from darkstl import overlay
from darkstl.importers.fromsoft.formats import dcx
from test_app import home, make_service, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture
from test_reengine import re4_dir  # noqa: F401 - fixture

GREEN = (20, 220, 40, 255)


def _game_with_a_wall(weapon_dir):
    """The weapon game, with a map's texture pack no model names: the texture to find and assign."""
    img = np.zeros((32, 32, 4), np.uint8)
    img[...] = GREEN
    folder = weapon_dir / "map" / "m30"
    folder.mkdir(parents=True)
    tpf = synth.build_tpf([("m30_wall_mossy_a", synth.rgba_dds(img))])
    (folder / "m30_0000.tpf.dcx").write_bytes(dcx.compress_dflt(tpf))
    return weapon_dir


def _open_weapon(s, label="Weapon 0300"):
    items = s.call("models_query", {"game": "Dark Souls III", "limit": 1000})["items"]
    item = next(i for i in items if i["label"] == label)
    return s.call("model_open", {"id": item["id"]})


def _albedo_pixel(s, m):
    from io import BytesIO

    from PIL import Image

    key = m["materials"][0]["albedo"]
    png = s.call("texture", {"sid": m["sid"], "key": key, "max": "16"})
    return tuple(np.asarray(Image.open(BytesIO(png)).convert("RGBA"))[8, 8])


def test_a_texture_is_found_by_name_and_assigned(home, weapon_dir):  # noqa: F811
    game = _game_with_a_wall(weapon_dir)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    m = _open_weapon(s)
    assert m["assigned"] == {}
    before = _albedo_pixel(s, m)
    found = s.call("textures_search", {"sid": m["sid"], "q": "wall mossy"})
    assert [t["name"] for t in found["items"]] == ["m30_wall_mossy_a"]
    assert found["listed"] >= 3  # the weapon's own two as well
    pick = found["items"][0]
    assert not pick["stored"]
    png = s.call("texture_preview", {"game": "Dark Souls III", "ref": pick["ref"], "max": "32"})
    assert png[:4] == b"\x89PNG"
    r = s.call("texture_assign", {"sid": m["sid"], "material": 0, "role": "albedo", "ref": pick["ref"]})
    assert r["texture"]["name"] == "m30_wall_mossy_a" and r["key"] == "parts/wp_a_0300.partsbnd.dcx"
    # the model opens again with it: a new session, so nothing made before is shown for it
    again = _open_weapon(s)
    assert again["sid"] != m["sid"]
    assert again["assigned"] == {"0": {"albedo": "m30_wall_mossy_a"}}
    assert _albedo_pixel(s, again) == GREEN != before
    # kept in the asset store and the overlay, which name the model by its path in the game
    store = home / "library" / "Dark Souls III packages"
    assert (store / r["texture"]["file"]).is_file()
    saved = json.loads((home / "overlays" / "Dark Souls III.json").read_text())
    assert saved["models"]["parts/wp_a_0300.partsbnd.dcx"]["textures"]["0"]["albedo"]["name"] == "m30_wall_mossy_a"
    # now stored, the picker finds it in the store
    stored = s.call("textures_search", {"sid": again["sid"], "q": "mossy"})["items"][0]
    assert stored["stored"] and stored["where"] == "asset store"
    # and back to the model's own
    s.call("texture_assign", {"sid": again["sid"], "material": 0, "role": "albedo", "ref": None})
    back = _open_weapon(s)
    assert back["assigned"] == {} and _albedo_pixel(s, back) == before


def test_the_choice_follows_the_model_into_its_package(home, weapon_dir):  # noqa: F811
    game = _game_with_a_wall(weapon_dir)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    m = _open_weapon(s)
    pick = s.call("textures_search", {"sid": m["sid"], "q": "mossy"})["items"][0]
    s.call("texture_assign", {"sid": m["sid"], "material": 0, "role": "albedo", "ref": pick["ref"]})
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "packages": True})["job"])
    s.call("folder_remove", {"path": str(game)})
    shutil.rmtree(game)
    fresh = make_service(home)
    wait(fresh, fresh.load_folders().id)
    packed = _open_weapon(fresh)
    assert packed["importer"] == "package"
    assert packed["assigned"] == {"0": {"albedo": "m30_wall_mossy_a"}}
    assert _albedo_pixel(fresh, packed) == GREEN


def test_overlay_entries_come_and_go(tmp_path):
    ov = overlay.Overlay(tmp_path / "g.json", "G")
    key = overlay.model_key("chr\\c1000.chrbnd.dcx", "c1000_1")
    assert key == "chr/c1000.chrbnd.dcx|c1000_1"
    ov.assign(key, 2, "normal", {"file": "assets/textures/x.png", "name": "x"})
    assert ov.signature_of_file("chr/c1000.chrbnd.dcx") and not ov.signature_of_file("chr/c1001.chrbnd.dcx")
    ov.save()
    again = overlay.Overlay(tmp_path / "g.json", "G")
    assert again.textures(key) == {"2": {"normal": {"file": "assets/textures/x.png", "name": "x"}}}
    again.assign(key, 2, "normal", None)
    assert again.data["models"] == {}


def test_re_engine_folders_list_their_textures(re4_dir):  # noqa: F811 - fixture
    from darkstl.importers.reengine import REEngineImporter

    imp = REEngineImporter()
    imp.scan(re4_dir, "")
    listed = imp.list_textures(re4_dir)
    assert listed, "the RE4 folder has .tex files"
    name, ref = listed[0]
    tex = imp.open_texture(re4_dir, ref)
    assert tex.name.lower() == name and tex.decode().shape[2] == 4
