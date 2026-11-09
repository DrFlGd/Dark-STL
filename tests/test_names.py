"""In-game names (the games' item text and tables), the library index (sizes and counts per model) and the
search across every game: byte-built FMG and PARAM files, the names hook, the index written and read, the
query with its filters and sorts."""

from __future__ import annotations

import json
import shutil
import struct
import time

import pytest

import synth
import synth_names
from darkstl import library, library_index, package
from darkstl.importers.fromsoft import FromSoftwareImporter, names, unpack
from darkstl.importers.fromsoft.formats import dcx
from darkstl.importers.fromsoft.formats.binary import FormatError
from darkstl.importers.fromsoft.formats.fmg import read_fmg, write_fmg
from darkstl.importers.fromsoft.formats.param import read_param, write_param
from test_app import home, make_service, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture

DS3 = "Dark Souls III"


# --------------------------------------------------------------------------- the formats


@pytest.mark.parametrize("version,big", [(2, False), (1, False), (1, True), (0, True)])
def test_fmg_files_read_back_every_name(version, big):
    entries = {10: "Dagger", 11: "Dagger+1", 13: "Broadsword", 2000: "Ünïcode ☆", 2001: ""}
    data = write_fmg(entries, version, big)
    assert data[2] == version and data[1] == (1 if big else 0)
    assert read_fmg(data) == entries


def test_an_fmg_file_cut_short_is_refused():
    data = write_fmg({1: "a", 5: "b"})
    with pytest.raises(FormatError):
        read_fmg(data[:0x30])
    with pytest.raises(FormatError):
        read_fmg(b"not an fmg file at all, really")


@pytest.mark.parametrize("long_offsets", [True, False])
def test_param_rows_and_fields_are_read(long_offsets):
    rows = []
    for row_id, model in ((100000, 300), (100100, 301), (200000, 400)):
        row = bytearray(272)
        struct.pack_into("<H", row, 184, model)
        rows.append((row_id, bytes(row)))
    p = read_param(write_param("EQUIP_PARAM_WEAPON_ST", rows, long_offsets=long_offsets))
    assert p.type == "EQUIP_PARAM_WEAPON_ST"
    assert p.row_size == 272
    assert [r for r, _o in p.rows] == [100000, 100100, 200000]
    assert p.column(184, "H") == [(100000, 300), (100100, 301), (200000, 400)]
    assert p.column(271, "H") == []  # past the end of a row


def test_the_regulation_files_decrypt_with_the_games_keys():
    payload = dcx.compress_dflt(synth.build_bnd4([(0, "EquipParamWeapon.param", b"x" * 100)]))
    for key in (names.DS3_REGULATION_KEY, names.ER_REGULATION_KEY):
        raw = synth_names.encrypt_regulation(payload, key)
        assert names.decrypt_regulation(raw, key)[:len(payload)] == payload


# --------------------------------------------------------------------------- the names


def _table(game, how):
    files = synth_names.params(game, synth_names.WEAPONS, synth_names.ARMOUR, synth_names.NPCS,
                               long_offsets=how == "regulation")
    texts = {"weapon": synth_names.WEAPON_NAMES, "protector": synth_names.ARMOUR_NAMES, "npc": synth_names.NPC_NAMES}
    return names.build_table(game, texts, {n.split(".")[0]: read_param(d) for n, d in files})


@pytest.mark.parametrize("game", [DS3, "Elden Ring", "Dark Souls Remastered", "Bloodborne"])
def test_each_game_names_its_weapons_armour_and_characters(game):
    t = _table(game, "regulation" if game in (DS3, "Elden Ring") else "gameparam")
    # a weapon is named by its first row: not its +1 or its infusions
    assert t.weapons == {300: "Dagger", 400: "Uchigatana"}
    # armour by its slot and number: the same number is a different piece in each slot
    assert t.armour == {("hd", 1000): "Knight Helm", ("bd", 1000): "Knight Armor", ("am", 1000): "Knight Gauntlets"}
    # a character by the name most of its rows give it; the human characters' short ids aren't a model's
    assert t.characters == {1100: "Hollow Soldier"}
    assert t.name_for("wp_a_0300") == "Dagger"
    assert t.name_for("WP_A_0300_l") == "Dagger"
    assert t.name_for("hd_f_1000") == "Knight Helm"
    assert t.name_for("lg_m_1000") is None
    assert t.name_for("c1100") == "Hollow Soldier"
    assert t.name_for("c0000") is None
    assert t.name_for("o123456") is None
    assert names.NameTable.from_json(json.loads(json.dumps(t.as_json(), ensure_ascii=False))) == t


def test_a_game_without_known_tables_has_no_names():
    assert len(names.build_table("Dark Souls II", {"weapon": {1: "x"}}, {})) == 0


def test_names_are_read_from_the_game_folder(tmp_path):
    root = tmp_path / "DS3"
    root.mkdir()
    synth_names.add_names(root, DS3, "regulation")
    sources = names.find_sources(root)
    assert sources.language == "engus" and [p.name for p in sources.texts] == ["item_dlc2.msgbnd.dcx"]
    assert [p.name for p, _k in sources.regulation] == ["Data0.bdt"]
    t = names.read_table(root, DS3)
    assert t.weapons[400] == "Uchigatana"
    # descriptions (WeaponInfo.fmg) aren't taken for names
    assert "A description" not in json.dumps(t.as_json())


def test_names_from_a_plain_gameparam_and_japanese_text(tmp_path):
    root = tmp_path / "DSR"
    msg = root / "msg" / "ENGLISH"
    msg.mkdir(parents=True)
    (msg / "item.msgbnd.dcx").write_bytes(synth_names.msgbnd(synth_names.WEAPON_NAMES, synth_names.ARMOUR_NAMES,
                                                              synth_names.NPC_NAMES, japanese=True, version=1))
    files = synth_names.params("Dark Souls Remastered", synth_names.WEAPONS, synth_names.ARMOUR, synth_names.NPCS, False)
    gp = root / "param" / "GameParam"
    gp.mkdir(parents=True)
    (gp / "GameParam.parambnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(i, n, d) for i, (n, d) in enumerate(files)])))
    t = names.read_table(root, "Dark Souls Remastered")
    assert t.weapons == {300: "Dagger", 400: "Uchigatana"} and t.characters == {1100: "Hollow Soldier"}


def test_elden_rings_data0_is_an_archive_not_its_tables(tmp_path):
    root = tmp_path / "ER"
    root.mkdir()
    (root / "Data0.bhd").write_bytes(b"x")
    (root / "Data0.bdt").write_bytes(b"x" * 64)
    synth_names.add_names(root, "Elden Ring", "regulation")
    sources = names.find_sources(root)
    assert [p.name for p, _k in sources.regulation] == ["regulation.bin"]
    assert names.read_table(root, "Elden Ring").armour[("bd", 1000)] == "Knight Armor"


def test_the_names_hook_names_the_models_a_scan_found(weapon_dir, tmp_path):  # noqa: F811
    synth_names.add_names(weapon_dir, DS3)
    found, _notes, kept = library.scan_folder_kept(weapon_dir, DS3)
    assert kept.names == {
        "parts/wp_a_0300.partsbnd.dcx": "Dagger", "parts/wp_a_0300_l.partsbnd.dcx": "Dagger",
        "parts/wp_a_0400-partsbnd-dcx/WP_A_0400.flver": "Uchigatana", "parts/hd_m_1000.partsbnd.dcx": "Knight Helm",
        "chr/c1100.chrbnd.dcx": "Hollow Soldier"}
    # kept in the importer's cache until the files change
    imp = FromSoftwareImporter()
    imp.cache = tmp_path / "cache"
    first = imp.names(weapon_dir, DS3, found)
    assert first == kept.names and list((tmp_path / "cache" / "names").glob("*.json"))
    (weapon_dir / "msg" / "engus" / "item_dlc2.msgbnd.dcx").write_bytes(
        synth_names.msgbnd({**synth_names.WEAPON_NAMES, 100000: "Mail Breaker"}, synth_names.ARMOUR_NAMES, synth_names.NPC_NAMES))
    assert imp.names(weapon_dir, DS3, found)["parts/wp_a_0300.partsbnd.dcx"] == "Mail Breaker"


def test_unpack_copies_the_name_files_and_the_tables_beside_the_archives(tmp_path):
    assert unpack.group_of("/msg/engus/item_dlc2.msgbnd.dcx") == "names"
    assert unpack.group_of("/msg/ENGLISH/item.msgbnd.dcx") == "names"
    assert unpack.group_of("/param/gameparam/gameparam.parambnd.dcx") == "names"
    assert unpack.group_of("/param/drawparam/default.gparam.dcx") is None
    assert "names" in unpack.DEFAULT_GROUPS


def test_a_lone_data0_bdt_isnt_taken_for_a_packed_game(tmp_path):
    root = tmp_path / "unpacked"
    (root / "parts").mkdir(parents=True)
    (root / "Data0.bdt").write_bytes(b"x" * 64)
    assert FromSoftwareImporter().folder_notes(root, []) == []
    (root / "Data1.bdt").write_bytes(b"x")
    (root / "Data1.bhd").write_bytes(b"x")
    assert "still packed" in FromSoftwareImporter().folder_notes(root, [])[0]


# --------------------------------------------------------------------------- in the app


def _items(s, **args):
    return s.call("models_query", {"limit": 1000, **args})["items"]


def test_models_are_listed_by_their_names_in_the_game(home, weapon_dir):  # noqa: F811
    synth_names.add_names(weapon_dir, DS3)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    by_stem = {i["stem"].lower(): i for i in _items(s, game=DS3, lod=True)}
    dagger = by_stem["wp_a_0300"]
    assert dagger["label"] == "Dagger" and dagger["game_name"] == "Dagger" and dagger["file_label"] == "Weapon 0300"
    assert "original" not in dagger  # not renamed by the user
    assert by_stem["c1100"]["label"] == "Hollow Soldier"
    # kept in the game's overlay, beneath the user's own names
    ov = json.loads(next((home / "overlays").glob("*.json")).read_text("utf-8"))
    assert ov["game_names"]["parts/wp_a_0300.partsbnd.dcx"] == "Dagger"
    s.call("model_arrange", {"id": dagger["id"], "name": "My knife"})
    renamed = next(i for i in _items(s, game=DS3) if i["id"] == dagger["id"])
    assert renamed["label"] == "My knife" and renamed["original"]["label"] == "Dagger"
    s.call("model_arrange", {"id": dagger["id"], "name": ""})
    assert next(i for i in _items(s, game=DS3) if i["id"] == dagger["id"])["label"] == "Dagger"
    # searched by name, name in the game or file name
    assert [i["stem"] for i in _items(s, q="dagger")] == ["wp_a_0300"]
    assert [i["stem"] for i in _items(s, q="wp_a_0300")] == ["wp_a_0300"]
    assert [i["stem"] for i in _items(s, q="weapon 0300")] == ["wp_a_0300"]
    # the names stay once the game's files are gone (as the overlay does)
    shutil.rmtree(weapon_dir / "msg")
    (weapon_dir / "Data0.bdt").unlink()
    fresh = make_service(home)
    wait(fresh, fresh.load_folders().id)
    assert {i["label"] for i in _items(fresh, game=DS3)} >= {"Dagger", "Uchigatana", "Knight Helm", "Hollow Soldier"}


def test_read_names_reads_them_again(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    assert "Dagger" not in {i["label"] for i in _items(s, game=DS3)}
    synth_names.add_names(weapon_dir, DS3)
    r = wait(s, s.call("names_read", {"game": DS3})["job"])
    assert r["named"] == 5
    assert "Dagger" in {i["label"] for i in _items(s, game=DS3)}


def _wait_for_stats(s, count, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        items = _items(s, lod=False)
        if sum(1 for i in items if i.get("stats")) >= count:
            return items
        time.sleep(0.05)
    raise AssertionError("stats weren't read")


def _other_game(tmp_path):
    folder = tmp_path / "statues"
    folder.mkdir()
    (folder / "dagger_statue.obj").write_text("v 0 0 0\nv 0.1 0 0\nv 0 0.2 0\nv 0 0 0.3\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n")
    return folder


def test_the_index_is_written_at_scan_and_the_stats_are_kept(home, weapon_dir, tmp_path):  # noqa: F811
    synth_names.add_names(weapon_dir, DS3)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    items = _wait_for_stats(s, 4)  # read from the files in the background
    dagger = next(i for i in items if i["stem"] == "wp_a_0300")
    st = dagger["stats"]
    assert st["triangles"] == 32 and st["vertices"] == 25 and st["parts"] == 1 and st["textures"] == 2
    assert st["size_mm"] == [1000.0, 1000.0, 0.0] and st["animated"] is False and st["from"] == "file"
    s._flush_index(0.0)
    index = library_index.read_index(s.index.file(DS3))
    rows = {r["stem"]: r for r in index["models"]}
    row = rows["wp_a_0300"]
    assert {k: row[k] for k in ("id", "label", "game_name", "kind", "group", "triangles", "collected")} == {
        "id": dagger["id"], "label": "Dagger", "game_name": "Dagger", "kind": "Weapons", "group": "IDs 03xx",
        "triangles": 32, "collected": False}
    # a new start reads them back, without reading the files again
    fresh = make_service(home)
    fresh._start_stats = lambda: None
    wait(fresh, fresh.load_folders().id)
    again = next(i for i in _items(fresh) if i["stem"] == "wp_a_0300")
    assert again["stats"]["triangles"] == 32
    # opening a model learns its exact stats
    m = fresh.call("model_open", {"id": dagger["id"]})
    assert fresh._entry_stats(fresh.library.find(dagger["id"]))["from"] == "model"
    assert m["entry"]["stats"]["triangles"] == 32


def test_search_every_game_with_filters_and_sorts(home, weapon_dir, tmp_path):  # noqa: F811
    synth_names.add_names(weapon_dir, DS3)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("folder_add", {"path": str(_other_game(tmp_path))})["job"])
    _wait_for_stats(s, 4)
    statue = next(i for i in _items(s) if i["stem"] == "dagger_statue")
    s.call("model_open", {"id": statue["id"]})  # OBJ files are learnt when opened (or their preview drawn)
    r = s.call("models_query", {"q": "dagger", "facets": True})
    assert {i["stem"] for i in r["items"]} == {"wp_a_0300", "dagger_statue"}
    assert r["facets"]["games"] == [{"name": DS3, "count": 1}, {"name": "statues", "count": 1}]
    assert {k["name"] for k in r["facets"]["kinds"]} == {"Weapons", "Models"}
    assert [i["stem"] for i in _items(s, q="dagger", games=["statues"])] == ["dagger_statue"]
    assert [i["stem"] for i in _items(s, q="dagger", kinds=["Weapons"])] == ["wp_a_0300"]
    assert {i["stem"] for i in _items(s, textured=True)} == {"wp_a_0300", "wp_a_0400", "hd_m_1000", "c1100"}
    assert {i["stem"] for i in _items(s, textured=False)} == {"dagger_statue"}
    assert [i["stem"] for i in _items(s, tri_max=10)] == ["dagger_statue"]
    assert len(_items(s, tri_min=10, tri_max=100)) == 4
    assert _items(s, animated=True) == []
    assert _items(s, collected=True) == []
    # sorts: by triangles, the other way, by size; each game's together when asked
    assert _items(s, sort="triangles")[0]["stem"] == "dagger_statue"
    assert _items(s, sort="triangles", desc=True)[-1]["stem"] == "dagger_statue"
    assert _items(s, sort="size")[0]["stem"] == "dagger_statue"
    assert _items(s, sort="name")[0]["label"] == "Dagger"
    assert [i["game"] for i in _items(s, sort="triangles", by_game=True)][-1] == "statues"
    # a model whose stats aren't known yet sorts last, either way
    s.index.forget(DS3, [next(i["id"] for i in _items(s) if i["stem"] == "c1100")])
    s._stats_tried.update(i["id"] for i in _items(s))
    for desc in (False, True):
        assert _items(s, sort="triangles", desc=desc)[-1]["stem"] == "c1100"
    assert s.call("models_query", {"sort": "triangles", "facets": True})["unknown"] == 1
    # Home's Recent row: the models still there, in the order asked
    got = s.call("models_get", {"ids": [statue["id"], "gone", r["items"][0]["id"]]})["items"]
    assert [g["id"] for g in got] == [statue["id"], r["items"][0]["id"]]


def test_a_packages_stats_come_from_its_index_or_its_files(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("collect_run", {"game": DS3, "packages": True})["job"])
    index_file = next((home.parent).rglob("wp_a_0300.partsbnd.dcx.darkpkg/package.json"))
    index = package.read_index(index_file)
    assert index["models"][0]["stats"]["triangles"] == 32
    # a package written before 0.44 has none in its index: read from its sidecar and its glTF's head
    for m in index["models"]:
        m.pop("stats")
    index_file.write_text(json.dumps(index), "utf-8")
    st = package.package_stats(index_file)
    assert st["triangles"] == 32 and st["parts"] == 1 and st["textures"] == 2
    assert st["size_mm"] == pytest.approx([1000.0, 1000.0, 0.0], abs=0.2)


def test_the_names_travel_with_collected_packages(home, weapon_dir, tmp_path, monkeypatch):  # noqa: F811
    """Collected as packages, the models keep the game's names: on another computer (no overlay, no game) the
    packages folder alone names them."""
    synth_names.add_names(weapon_dir, DS3)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    plan = wait(s, s.call("collect_plan", {"game": DS3, "job": 1, "packages": True})["job"])
    wait(s, s.call("collect_run", {"game": DS3, "packages": True})["job"])
    index_file = next(home.parent.rglob("wp_a_0300.partsbnd.dcx.darkpkg/package.json"))
    assert package.read_index(index_file)["game_name"] == "Dagger"
    moved = tmp_path / "elsewhere" / "Dark Souls III packages"
    shutil.copytree(plan["folder"], moved)
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "other-home"))
    other = make_service(tmp_path / "other-home")
    wait(other, other.call("folder_add", {"path": str(moved)})["job"])
    by_stem = {i["stem"].lower(): i for i in _items(other, lod=True)}
    assert by_stem["wp_a_0300"]["label"] == "Dagger" and by_stem["wp_a_0300"]["importer"] == "package"
    assert by_stem["wp_a_0300"]["game_name"] == "Dagger"  # kept in the other computer's overlay, from the package
    assert by_stem["c1100"]["label"] == "Hollow Soldier"


def test_scanning_a_large_folder_isnt_slowed_by_names_and_the_index(tmp_path, monkeypatch):
    """The names are read once per scan (then kept until their files change) and the index is one write per game."""
    root = tmp_path / "big"
    parts = root / "parts"
    parts.mkdir(parents=True)
    stub = dcx.compress_dflt(synth.build_bnd4([(0, "x.flver", b"FLVER\0")]))
    for i in range(3000):
        (parts / f"wp_a_{i:04d}.partsbnd.dcx").write_bytes(stub)
    def timed(with_names):
        if not with_names:
            monkeypatch.setattr(library, "folder_names", lambda *a, **k: {})
        else:
            monkeypatch.undo()
        best = 1e9
        for _ in range(3):
            t = time.perf_counter()
            library.scan_folder_kept(root, DS3)
            best = min(best, time.perf_counter() - t)
        return best

    synth_names.add_names(root, DS3)
    plain = timed(False)
    named = timed(True)
    assert named - plain < max(0.5, plain * 0.5), (plain, named)
    found = library.scan(root, DS3)
    idx = library_index.LibraryIndex(tmp_path / "index")
    t = time.perf_counter()
    idx.write(DS3, [{"id": e.id, "label": e.label, "kind": e.category} for e in found])
    assert time.perf_counter() - t < 1.0
