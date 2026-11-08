"""Export model files: one model's game files and/or its model package in a zip with a report, which Dark STL
opens again with the game gone (so a problem with a model can be looked at without the game)."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from test_app import home, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture

from darkstl.app.config import Config
from darkstl.app.service import Service
from darkstl.importers import set_cache_folder
from darkstl.importers.borderlands.ue4 import vfs


@pytest.fixture(autouse=True)
def _importer_cache(tmp_path):
    set_cache_folder(tmp_path / "importer-cache")
    yield
    set_cache_folder(None)


def _service(home, cache="cache") -> Service:  # noqa: F811
    s = Service(Config.load(), home / cache)
    s.config.export_dir = str(home / "exports")
    return s


def _item(s: Service, game: str, stem: str) -> dict:
    items = s.call("models_query", {"game": game, "limit": 1000})["items"]
    return next(i for i in items if i["stem"] == stem)


def test_returnal_character_files_and_package(home, tmp_path):  # noqa: F811
    from test_unreal_animation import _game

    root = _game(tmp_path)
    s = _service(home)
    game = wait(s, s.call("folder_add", {"path": str(root)})["job"])["game"]
    item = _item(s, game, "SK_Selene_Suit")
    shown = s.call("model_open", {"id": item["id"]})
    anims = sorted(a["name"] for a in s.call("model_animations", {"sid": shown["sid"]})["animations"])
    assert "A_Selene_Wave" in anims

    plan = wait(s, s.call("model_files_plan", {"id": item["id"], "job": 1})["job"])
    assert plan["files"] > 3 and plan["bytes"] > 0 and plan["dest"].endswith("model files.zip")
    done = wait(s, s.call("model_files_export", {"id": item["id"], "package": True})["job"])
    zpath = Path(done["path"])
    assert zpath.is_file() and zpath.parent == home / "exports" and not done["failed"]
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        report = json.loads(z.read("report.json"))
        text = z.read("report.txt").decode()
        z.extractall(tmp_path / "unzipped")
    assert {"report.txt", "report.json", "log.txt"} <= set(names)
    assert f"{game}/darkstl-collection.json" in names
    assert any(n.endswith("SK_Selene_Suit.uasset") for n in names)
    assert any("A_Selene_Wave" in n and n.startswith(f"{game}/") for n in names), "the animations' files"
    assert any(n.startswith(f"{game} packages/") and n.endswith("package.json") for n in names), "the model package"
    assert not any(n.lower().endswith(".dll") for n in names)
    model = report["model"]
    assert model["opened"] and model["materials"] and model["uv_ranges"]
    assert "A_Selene_Wave" in [a["name"] for a in model["animations"]["listed"]]
    assert model["animations"]["skeleton"]["matched"] > 0
    assert report["export"]["label"] == item["label"] and "SK_Selene_Suit" in text
    assert report["package"]["models"] == 1

    # with the game gone, the zip's game files open as they did from the game
    s.call("folder_remove", {"path": str(root)})
    vfs.forget(root)
    shutil.rmtree(root)
    for n, (folder, importer) in enumerate([(tmp_path / "unzipped" / game, "returnal"),
                                            (tmp_path / "unzipped" / f"{game} packages", "package")]):
        again = _service(home, f"cache{n + 2}")
        assert again.call("folder_check", {"path": str(folder)})["collection"], folder
        wait(again, again.call("folder_add", {"path": str(folder), "game": game})["job"])
        i = _item(again, game, "SK_Selene_Suit")
        assert i["importer"] == importer
        m = again.call("model_open", {"id": i["id"]})
        got = sorted(a["name"] for a in again.call("model_animations", {"sid": m["sid"]})["animations"])
        assert got == anims, importer
        again.call("folder_remove", {"path": str(folder)})


def test_fromsoftware_weapon_game_files_only(home, weapon_dir):  # noqa: F811
    s = _service(home)
    game = wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])["game"]
    item = s.call("models_query", {"game": game, "limit": 1000})["items"][0]
    done = wait(s, s.call("model_files_export", {"id": item["id"], "textures": False})["job"])
    with zipfile.ZipFile(done["path"]) as z:
        names = z.namelist()
        report = json.loads(z.read("report.json"))
    assert any(n.startswith(f"{game}/") and n.endswith(".dcx") for n in names)
    assert not any("packages/" in n for n in names) and "package" not in report
    assert report["model"]["opened"]


def test_needs_something_to_put_in(home, weapon_dir):  # noqa: F811
    s = _service(home)
    game = wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])["game"]
    item = s.call("models_query", {"game": game, "limit": 1000})["items"][0]
    with pytest.raises(Exception, match="game's files, the model package, or both"):
        s.call("model_files_export", {"id": item["id"], "game_files": False, "package": False})
