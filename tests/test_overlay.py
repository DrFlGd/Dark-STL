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


def _listed(s, game="Dark Souls III"):
    items = s.call("models_query", {"game": game, "limit": 1000})["items"]
    return {i["stem"]: (i["category"], i["group"], i["label"]) for i in items}


def test_models_are_renamed_and_filed_in_folders_of_ones_own(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    before = _listed(s)
    assert before["wp_a_0300"] == ("Weapons", "IDs 03xx", "Weapon 0300")
    item = next(i for i in s.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
                if i["stem"] == "wp_a_0300")
    r = s.call("model_arrange", {"id": item["id"], "name": "Longsword", "folder": "Weapons / Long swords"})
    assert r["entry"]["label"] == "Longsword" and r["entry"]["original"]["label"] == "Weapon 0300"
    now = _listed(s)
    assert now["wp_a_0300"] == ("Weapons", "Long swords", "Longsword")
    assert now["wp_a_0400"] == before["wp_a_0400"]
    # searching finds it by its new name; the overview lists the new group
    found = s.call("models_query", {"game": "Dark Souls III", "q": "longsword"})["items"]
    assert [i["stem"] for i in found] == ["wp_a_0300"]
    groups = {c["name"]: [g["name"] for g in c["groups"]] for g in s.call("overview", {})["games"] for c in g["categories"]}
    assert "Long swords" in groups["Weapons"]
    # a whole kind renamed: the characters become "Bosses"
    s.call("folder_arrange", {"game": "Dark Souls III", "category": "Characters", "to": "Bosses"})
    assert _listed(s)["c1100"][0] == "Bosses"
    # kept: a fresh start (and a rescan) shows the same
    fresh = make_service(home)
    wait(fresh, fresh.load_folders().id)
    assert _listed(fresh) == _listed(s)
    wait(fresh, fresh.call("folder_rescan", {"path": str(weapon_dir)})["job"])
    assert _listed(fresh)["wp_a_0300"] == ("Weapons", "Long swords", "Longsword")
    # and undone
    fresh.call("model_arrange", {"id": item["id"], "name": "", "folder": ""})
    fresh.call("folder_arrange", {"game": "Dark Souls III", "category": "Bosses", "to": ""})
    assert _listed(fresh) == before


def test_a_moved_folder_takes_the_models_filed_there_by_hand(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    items = {i["stem"]: i for i in s.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]}
    s.call("model_arrange", {"id": items["wp_a_0300"]["id"], "folder": "Favourites"})
    s.call("model_arrange", {"id": items["c1100"]["id"], "folder": "Favourites"})
    s.call("folder_arrange", {"game": "Dark Souls III", "category": "Favourites", "to": "Best"})
    now = _listed(s)
    assert now["wp_a_0300"][0] == now["c1100"][0] == "Best"


def test_models_combined_into_one_open_without_the_game(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    items = {i["stem"]: i for i in s.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]}
    base, extra = items["c1100"], items["wp_a_0300"]
    alone = s.call("model_open", {"id": base["id"]})["stats"]["triangles"]
    other = s.call("model_open", {"id": extra["id"]})["stats"]["triangles"]
    made = s.call("config_save", {"base_id": base["id"], "configuration": {
        "label": "Knight with sword", "category": "Characters", "group": "Mine",
        "slots": [{"id": "part1", "label": extra["label"], "required": False, "default": "on",
                   "options": [{"id": "on", "label": extra["label"], "path": extra["file"]}]}]}})
    m = s.call("model_open", {"id": made["id"]})
    assert m["stats"]["triangles"] == alone + other
    assert made["entry"]["category"] == "Characters" and made["entry"]["group"] == "Mine"
    # imported as packages and the game gone: the combined model is made of the packages
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "packages": True})["job"])
    s.call("folder_remove", {"path": str(weapon_dir)})
    shutil.rmtree(weapon_dir)
    fresh = make_service(home)
    wait(fresh, fresh.load_folders().id)
    (combined,) = [i for i in fresh.call("configs", {"game": "Dark Souls III"})["items"] if i["label"] == "Knight with sword"]
    again = fresh.call("model_open", {"id": combined["id"]})
    assert again["stats"]["triangles"] == alone + other


def test_a_configuration_finds_a_models_package_by_its_path_in_the_game(tmp_path):
    from darkstl import configs
    from darkstl.model import CatalogEntry

    root = tmp_path / "Dark Souls III packages"
    index = root / "chr" / "c1100.chrbnd.dcx.darkpkg" / "package.json"
    packed = CatalogEntry("G", "Characters", "", "c1100", "C", index, "package")
    found = configs.Resolver([packed], [root])
    assert found.find(configs.ModelRef("chr/c1100.chrbnd.dcx")) == index
    assert found.find(configs.ModelRef("c1100.chrbnd.dcx")) == index
    assert found.find(configs.ModelRef(str(root / "chr" / "c1100.chrbnd.dcx"))) == index
    # the game's file, or a collected copy, is what a configuration opens while it's there
    game = tmp_path / "Game"
    real = CatalogEntry("G", "Characters", "", "c1100", "C", game / "chr" / "c1100.chrbnd.dcx", "fromsoft")
    both = configs.Resolver([packed, real], [root, game])
    assert both.find(configs.ModelRef("chr/c1100.chrbnd.dcx")) == real.path
    assert both.find(configs.ModelRef("c1100.chrbnd.dcx")) == real.path
