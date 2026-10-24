"""Collecting models into the app's own library folder: which files go with a model, the plan and
the copy through the engine, and the dialog on the page."""

import json
import time
from pathlib import Path

import pytest

from darkstl import collect
from darkstl.app.config import Config
from darkstl.app.service import AppError, Service
from darkstl.importers.fromsoft import FromSoftwareImporter, files

from test_animation import make_char_dir
from test_app import wait
from test_pipeline import weapon_dir  # noqa: F401 - fixture


@pytest.fixture
def char_dir(tmp_path):
    """The animated character of test_animation, in a game folder of its own."""
    return make_char_dir(tmp_path / "chars" / "DARK SOULS III" / "Game")


def names(paths):
    return sorted(Path(p).name for p in paths)


def test_related_files_go_with_the_model(weapon_dir, char_dir):  # noqa: F811
    chr_model = weapon_dir / "chr" / "c1100.chrbnd.dcx"
    assert names(files.related_files(chr_model)) == ["c1100.chrbnd.dcx", "c1100.texbnd.dcx"]
    assert names(files.related_files(chr_model, textures=False)) == ["c1100.chrbnd.dcx"]
    weapon = weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx"
    assert names(files.related_files(weapon)) == ["wp_a_0300.partsbnd.dcx"]
    assert names(files.related_files(weapon, lod=True)) == ["wp_a_0300.partsbnd.dcx", "wp_a_0300_l.partsbnd.dcx"]
    # a loose FLVER in an unpacked archive folder takes the whole folder
    loose = weapon_dir / "parts" / "wp_a_0400-partsbnd-dcx" / "WP_A_0400.flver"
    assert files.related_files(loose) == [loose.parent]
    # animations come along unless asked not to
    animated = char_dir / "chr" / "c2000.chrbnd.dcx"
    assert names(files.related_files(animated)) == ["c2000.anibnd.dcx", "c2000.chrbnd.dcx"]
    assert names(files.related_files(animated, animations=False)) == ["c2000.chrbnd.dcx"]
    # what the whole game needs: the Oodle library, and the player's animations only when asked
    (weapon_dir / "oo2core_6_win64.dll").write_bytes(b"not really")
    (weapon_dir / "chr" / "c0000.anibnd.dcx").write_bytes(b"not really")
    assert names(files.shared_files(weapon_dir)) == ["oo2core_6_win64.dll"]
    assert names(files.shared_files(weapon_dir, player_animations=True)) == ["c0000.anibnd.dcx", "oo2core_6_win64.dll"]
    # the importer says what each file is for
    kinds = {f.path.name: f.kind for f in FromSoftwareImporter().related_files(animated)}
    assert kinds == {"c2000.chrbnd.dcx": "model", "c2000.anibnd.dcx": "animation"}
    assert {f.path.name: f.kind for f in FromSoftwareImporter().related_files(chr_model)} == {"c1100.chrbnd.dcx": "model", "c1100.texbnd.dcx": "texture"}
    assert [f.kind for f in FromSoftwareImporter().shared_files(weapon_dir, "Dark Souls III", True)] == ["other", "animation"]


def test_a_plan_keeps_relative_paths_and_knows_what_is_there(tmp_path):
    root = tmp_path / "game"
    (root / "parts").mkdir(parents=True)
    (root / "parts" / "a.partsbnd.dcx").write_bytes(b"model")
    (root / "parts" / "a.tpf").write_bytes(b"tex")
    unpacked = root / "parts" / "b-partsbnd-dcx"
    unpacked.mkdir()
    (unpacked / "b.flver").write_bytes(b"flver")
    (unpacked / "b.tpf").write_bytes(b"tpf")
    from darkstl.model import CatalogEntry

    entries = [CatalogEntry("Dark Souls III", "Weapons", "", "a", "a.partsbnd.dcx", root / "parts" / "a.partsbnd.dcx", "fromsoft"),
               CatalogEntry("Dark Souls III", "Weapons", "", "b", "b.flver", unpacked / "b.flver", "fromsoft", packed=False)]

    def related(e):
        if e.stem == "a":
            return [collect.RelatedFile(e.path, "model"), collect.RelatedFile(root / "parts" / "a.tpf", "texture"),
                    collect.RelatedFile(e.path, "model")]  # repeats are dropped
        return [collect.RelatedFile(unpacked, "model")]

    library = tmp_path / "library"
    plan = collect.plan(library, "Dark Souls III", entries, [root], collect.CollectOptions(), related,
                        extras=lambda r: [collect.RelatedFile(tmp_path / "missing.dll", "other")])
    targets = sorted(str(f.target.relative_to(library)).replace("\\", "/") for f in plan.files)
    assert targets == ["Dark Souls III/parts/a.partsbnd.dcx", "Dark Souls III/parts/a.tpf",
                       "Dark Souls III/parts/b-partsbnd-dcx/b.flver", "Dark Souls III/parts/b-partsbnd-dcx/b.tpf"]
    summary = plan.summary()
    assert summary["models"] == 2 and summary["files"] == 4 and summary["bytes"] == 16 and summary["present"] == 0
    assert summary["kinds"]["model"] == {"files": 3, "bytes": 13} and summary["kinds"]["texture"] == {"files": 1, "bytes": 3}
    done = collect.run(plan)
    assert done["copied"] == 4 and (library / "Dark Souls III" / "parts" / "b-partsbnd-dcx" / "b.tpf").read_bytes() == b"tpf"
    again = collect.plan(library, "Dark Souls III", entries, [root], collect.CollectOptions(), related)
    assert again.summary()["present"] == 4 and again.summary()["to_copy"] == 0
    assert collect.run(again)["copied"] == 0
    # a changed file is copied again
    (root / "parts" / "a.tpf").write_bytes(b"newer texture")
    changed = collect.plan(library, "Dark Souls III", entries, [root], collect.CollectOptions(), related)
    assert [f.source.name for f in changed.to_copy] == ["a.tpf"]
    assert collect.folder_size(library) == (4, 16)
    assert collect._folder_name("Dark Souls: Remastered?") == "Dark Souls_ Remastered_"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_collecting_through_the_engine(home, weapon_dir, char_dir):  # noqa: F811
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("folder_add", {"path": str(char_dir)})["job"])
    chr_id = s.call("models_query", {"game": "Dark Souls III", "q": "c1100"})["items"][0]["id"]
    plan = s.call("collect_plan", {"ids": [chr_id]})
    assert plan["models"] == 1 and plan["files"] == 2 and plan["present"] == 0
    assert plan["kinds"]["model"]["files"] == 1 and plan["kinds"]["texture"]["files"] == 1
    assert Path(plan["folder"]) == home / "library" / "Dark Souls III"
    # a kind, with the low-detail copies too
    weapons = s.call("collect_plan", {"game": "Dark Souls III", "category": "Weapons", "lod": True, "lod_copies": True})
    assert weapons["models"] == 3 and weapons["files"] == 4  # wp_a_0300 (+ _l) and the unpacked wp_a_0400 (2 files)
    assert s.call("collect_plan", {"game": "Dark Souls III", "category": "Weapons"})["models"] == 2
    with pytest.raises(AppError, match="Nothing to collect"):
        s.call("collect_plan", {"ids": ["no-such-model"]})

    r = wait(s, s.call("collect_run", {"ids": [chr_id]})["job"])
    assert r["copied"] == 2 and r["count"] == 1
    copy = home / "library" / "Dark Souls III" / "chr" / "c1100.chrbnd.dcx"
    assert copy.is_file() and copy.with_name("c1100.texbnd.dcx").is_file()
    ov = s.call("overview", {})
    ds3 = next(g for g in ov["games"] if g["name"] == "Dark Souls III")
    assert [(Path(f["path"]).name, f["collected"]) for f in ds3["folders"]] == [("Game", False), ("Game", False), ("Dark Souls III", True)]
    # the copy is a second c1100, hidden while the original is listed
    assert s.call("models_query", {"game": "Dark Souls III", "q": "c1100"})["total"] == 1
    assert s.call("collect_plan", {"ids": [chr_id]})["present"] == 2
    assert wait(s, s.call("collect_run", {"ids": [chr_id]})["job"])["copied"] == 0
    settings = s.call("settings_get", {})
    assert settings["library_files"] == 2 and settings["library_bytes"] > 0 and Path(settings["library_dir"]) == home / "library"
    # the copy can't be collected again, and a run across games is refused
    s.call("folder_remove", {"path": str(weapon_dir)})
    kept = s.call("models_query", {"game": "Dark Souls III", "q": "c1100"})
    assert kept["total"] == 1 and Path(kept["items"][0]["file"]) == copy
    with pytest.raises(AppError, match="Nothing to collect"):
        s.call("collect_plan", {"ids": [kept["items"][0]["id"]]})
    m = s.call("model_open", {"id": kept["items"][0]["id"]})
    assert m["textures"], "the collected texbnd is read beside the collected chrbnd"
    # an animated model collected from another game folder poses from its copy
    c2000 = s.call("models_query", {"game": "Dark Souls III", "q": "c2000"})["items"][0]
    assert wait(s, s.call("collect_run", {"ids": [c2000["id"]]})["job"])["copied"] == 2
    s.call("folder_remove", {"path": str(char_dir)})
    c2000_copy = s.call("models_query", {"game": "Dark Souls III", "q": "c2000"})["items"][0]
    assert Path(c2000_copy["file"]).parent == home / "library" / "Dark Souls III" / "chr"
    opened = s.call("model_open", {"id": c2000_copy["id"]})
    assert opened["animated"] is True
    assert [a["name"] for a in s.call("model_animations", {"sid": opened["sid"]})["animations"]] == ["a000_000000", "a000_003000"]
    # the folders are remembered
    saved = json.loads((home / "settings.json").read_text())["game_folders"]
    assert saved == [{"path": str(home / "library" / "Dark Souls III"), "game": "Dark Souls III"}]
    again = Service(Config.load(), home / "cache")
    wait(again, again.load_folders().id)
    assert again.call("models_query", {"game": "Dark Souls III"})["total"] == 2


def test_the_library_folder_can_be_moved(home, weapon_dir, tmp_path):  # noqa: F811
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    elsewhere = tmp_path / "elsewhere"
    assert s.call("settings_set", {"library_dir": str(elsewhere)})["library_dir"] == str(elsewhere)
    assert Service(Config.load(), home / "cache").call("settings_get", {})["library_dir"] == str(elsewhere)
    plan = s.call("collect_plan", {"game": "Dark Souls III", "category": "Armor"})
    assert Path(plan["folder"]) == elsewhere / "Dark Souls III"
    r = wait(s, s.call("collect_run", {"game": "Dark Souls III", "category": "Armor"})["job"])
    assert r["copied"] == 1 and (elsewhere / "Dark Souls III" / "parts" / "hd_m_1000.partsbnd.dcx").is_file()
    assert s.call("settings_set", {"library_dir": ""})["library_dir"] == str(home / "library")


def test_the_collect_dialog_on_the_page(home, weapon_dir, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from darkstl.app.server import AppServer
    from test_page import GL, launch

    server = AppServer(Service(Config.load(), home / "cache"), 0).start()
    try:
        with sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(server.url)
            page.wait_for_selector("#first-run")
            page.click("#first-add-folder")
            page.fill("#folder-path", str(weapon_dir))
            page.wait_for_function("document.querySelector('#folder-game')?.value === 'Dark Souls III'")
            page.click("#add-folder-dialog-ok")
            page.wait_for_selector("#kind-tiles", timeout=60000)
            page.wait_for_function("!document.querySelector('.dialog')", timeout=60000)
            page.click("#place-collect")
            page.wait_for_selector("#collect-plan-text", timeout=30000)
            assert "4 models" in page.inner_text("#collect-plan-text")
            page.check("[data-collect=lod]")
            page.wait_for_function("document.querySelector('#collect-plan-text')?.textContent.includes('5 models')", timeout=30000)
            page.click("#collect-dialog-ok")
            page.wait_for_function("!document.querySelector('.dialog')", timeout=120000)
            assert page.inner_text("#toast").startswith("Copied 7 files")
            assert (home / "library" / "Dark Souls III" / "parts" / "wp_a_0300_l.partsbnd.dcx").is_file()
            page.goto(server.url + "#/settings")
            page.wait_for_function("document.querySelector('#library-status')?.textContent.includes('7 files')", timeout=30000)
            assert page.locator("#folder-list li", has_text="kept by Dark STL").count() == 1
            assert page.evaluate("window.__errors") == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_collections_carry_a_note_move_and_come_back_on_a_fresh_install(home, weapon_dir, tmp_path, monkeypatch):  # noqa: F811
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "category": "Weapons"})["job"])
    folder = home / "library" / "Dark Souls III"
    note = collect.read_manifest(folder)
    assert note["game"] == "Dark Souls III" and note["models"] == ["wp_a_0300", "wp_a_0400"]
    assert note["sources"] == [str(weapon_dir)]
    assert s.call("settings_get", {})["library_files"] == 3  # the note isn't counted

    # moved: the files go, the game is listed from the new place, and new collections go there
    elsewhere = tmp_path / "Prints" / "Dark STL"
    r = s.call("library_move", {"path": str(elsewhere)})
    moved = wait(s, r["job"])
    assert moved["games"] == ["Dark Souls III"] and not folder.exists()
    assert (elsewhere / "Dark Souls III" / "parts" / "wp_a_0300.partsbnd.dcx").is_file()
    assert collect.read_manifest(elsewhere / "Dark Souls III")["models"] == ["wp_a_0300", "wp_a_0400"]
    ds3 = next(g for g in s.call("overview", {})["games"] if g["name"] == "Dark Souls III")
    assert [(f["path"], f["collected"]) for f in ds3["folders"]] == [(str(weapon_dir), False), (str(elsewhere / "Dark Souls III"), True)]
    assert s.call("settings_get", {})["library_dir"] == str(elsewhere)
    assert Path(s.call("collect_plan", {"game": "Dark Souls III", "category": "Armor"})["folder"]) == elsewhere / "Dark Souls III"
    with pytest.raises(AppError, match="outside the one"):
        s.call("library_move", {"path": str(elsewhere / "inner")})
    # without moving: only where new ones go
    assert s.call("library_move", {"path": str(tmp_path / "new"), "move": False})["job"] is None
    assert s.call("settings_get", {})["library_dir"] == str(tmp_path / "new")
    assert (elsewhere / "Dark Souls III").is_dir()

    # a fresh install: no settings, no game; the collected folder (or one game's folder in it) is enough
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "fresh"))
    fresh = Service(Config.load(), tmp_path / "fresh" / "cache")
    one = fresh.call("folder_check", {"path": str(elsewhere / "Dark Souls III")})
    assert one["game"] == "Dark Souls III" and one["collection"]["models"] == 2 and one["collections"] == []
    whole = fresh.call("folder_check", {"path": str(elsewhere)})
    assert whole["game"] is None and [c["game"] for c in whole["collections"]] == ["Dark Souls III"]
    assert whole["collection_root"] == str(elsewhere) and not whole["library_has_files"]
    added = wait(fresh, fresh.call("folder_add", {"path": str(elsewhere), "library": True})["job"])
    assert added["games"] == ["Dark Souls III"] and added["count"] == 2
    assert fresh.call("settings_get", {})["library_dir"] == str(elsewhere)
    (game,) = fresh.call("overview", {})["games"]
    assert game["folders"][0]["collected"] and game["count"] == 2
    with pytest.raises(AppError, match="in the library already"):
        fresh.call("folder_add", {"path": str(elsewhere)})


def test_collections_made_before_the_note_get_one(home, weapon_dir):  # noqa: F811
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "category": "Armor"})["job"])
    note = home / "library" / "Dark Souls III" / collect.MANIFEST
    note.unlink()
    again = Service(Config.load(), home / "cache")
    wait(again, again.load_folders().id)
    assert collect.read_manifest(note.parent)["game"] == "Dark Souls III"


def test_moving_and_adding_collected_files_on_the_page(home, weapon_dir, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from darkstl.app.server import AppServer
    from test_page import launch

    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "category": "Weapons"})["job"])
    elsewhere = tmp_path / "Prints"
    servers = [AppServer(s, 0).start()]
    try:
        with sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            # Settings: a new place for the collected files, and they move there
            page.goto(servers[0].url + "#/settings")
            page.wait_for_selector("#library-move")
            assert page.is_checked("#library-move")
            page.fill("#library-dir", str(elsewhere))
            page.press("#library-dir", "Enter")
            page.wait_for_selector("text=Moved the collected files", timeout=60000)
            assert (elsewhere / "Dark Souls III" / collect.MANIFEST).is_file()
            assert page.locator("#folder-list li", has_text=str(elsewhere)).count() == 1

            # a fresh install: add the collected files instead of the game
            monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "fresh"))
            servers.append(AppServer(Service(Config.load(), tmp_path / "fresh" / "cache"), 0).start())
            page.goto(servers[1].url)
            page.wait_for_selector("#first-run")
            page.click("#first-add-collected")
            page.wait_for_selector("text=Add collected files")
            page.fill("#folder-path", str(elsewhere))
            page.wait_for_selector("#folder-collections li", timeout=30000)
            assert "Dark Souls III" in page.inner_text("#folder-collections")
            assert page.is_checked("#folder-use-library")
            assert page.text_content("#add-folder-dialog-ok") == "Add 1 game"
            page.click("#add-folder-dialog-ok")
            page.wait_for_selector('#kind-tiles [data-sub="Weapons"]', timeout=60000)
            assert page.inner_text("#browse-count") == "2 models"
            assert page.evaluate("window.__errors") == []
            browser.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert json.loads((tmp_path / "fresh" / "settings.json").read_text())["library_dir"] == str(elsewhere)
