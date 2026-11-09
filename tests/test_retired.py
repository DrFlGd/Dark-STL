"""Finish, Cut and the painted 3MF moved to Model Painter in 0.55.0: they're gone from the engine and the command
line, and what they left in a settings file, a saved variation or a bulk preset is ignored when read and dropped
when it's written again."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest
from test_app import home, make_service, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture

from darkstl import retired, variations
from darkstl.app.config import Config
from darkstl.app.service import AppError
from darkstl.export import FORMATS

ROOT = Path(__file__).resolve().parents[1]
# what 0.54 and before kept for Finish, Cut and the painted 3MF
WATERTIGHT = {"sheet_mm": 1.2, "fill_hollows": True, "close_gaps_mm": 0, "loose_percent": 0}
CUT = {"cuts": [{"normal": [0, 0, 1], "point": [0, 0, 30], "joint": {"kind": "pegs", "clearance_mm": 0.2}}], "sheet_mm": 1.2}
PAINT = {"colours": 6, "print_height_mm": 150, "feature_mm": 0.5, "cleanup": "light", "sources": {"0": "none"}}
OLD_PAGE = {"theme": "dark", "inspectorTab": {"model": "cut"}, "enhance": {"target_vertices": 3000},
            "exportOpts": {"format": ".3mf", "units": "cm", "textures": True, "folder": "", "paint": PAINT},
            "tipsOpen": {"mesh": False, "finish": False, "cut": True, "paint": False},
            "cutOpts": {"from": "enhanced", "joint": {"kind": "magnets"}, "lay_flat": True},
            "cutPlans": {"m1": [{"id": 1, "normal": [0, 0, 1], "point": [0, 0, 0]}]},
            "paintOpts": PAINT, "paintSources": {"m1": {"0": "none"}}, "finishOpts": {"fill_hollows": False}}


def _weapon(s):
    items = s.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
    return next(i for i in items if i["stem"] == "wp_a_0300")


def _queue_done(s, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        q = s.call("queue", {})
        if not q["active"]:
            return q
        time.sleep(0.05)
    raise AssertionError("the queue didn't finish")


def test_the_modules_and_the_dependency_are_gone():
    for name in ("finish", "cut", "paint"):
        assert importlib.util.find_spec(f"darkstl.{name}") is None
    assert ".3mf" not in FORMATS
    deps = (ROOT / "pyproject.toml").read_text()
    assert "manifold3d" not in deps
    assert "manifold3d" not in (ROOT / "desktop" / "engine" / "engine.spec").read_text()


def test_the_retired_keys_are_left_out_and_the_rest_kept():
    settings = {"target_vertices": 2000, "strength": 2, "watertight": WATERTIGHT, "cut": CUT, "paint": PAINT, "lay_flat": True}
    assert retired.settings(settings) == {"target_vertices": 2000, "strength": 2}
    assert retired.settings(None) == {} and retired.settings("x") == {}
    record = {"name": "Strong", "from": "20260101-000000-abcdef", "finished": {"id": "x"}, "pieces": [1, 2],
              "settings": settings, "mesh": {"parts": None, "clean": None}}
    assert retired.variation(record) == {"name": "Strong", "from": "20260101-000000-abcdef",
                                         "settings": {"target_vertices": 2000, "strength": 2},
                                         "mesh": {"parts": None, "clean": None}}
    prefs = retired.prefs({"ds-ui": OLD_PAGE, "other": 1, "cutOpts": {}})
    page = prefs["ds-ui"]
    assert prefs["other"] == 1 and "cutOpts" not in prefs
    assert not set(page) & retired.PREFS
    assert page["exportOpts"] == {"units": "cm", "textures": True, "folder": ""}  # no 3MF, no paint: STL again
    assert page["tipsOpen"] == {"mesh": False}
    assert page["theme"] == "dark" and page["enhance"] == {"target_vertices": 3000}
    # a page whose export format is still one Dark STL writes keeps it
    assert retired.prefs({"ds-ui": {"exportOpts": {"format": ".glb"}}})["ds-ui"]["exportOpts"] == {"format": ".glb"}


def test_an_old_settings_file_loads_and_drops_them_when_saved(home):  # noqa: F811
    path = home / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "game_folders": [], "export_dir": "", "prefs": {"ds-ui": OLD_PAGE},
        "last_enhance": {"Dark Souls III": {"target_vertices": 2000, "watertight": WATERTIGHT, "cut": CUT, "paint": PAINT},
                         "Broken": "not a dict"},
        "finish": {"sheet_mm": 1.2},  # never read: only the known keys are
    }), "utf-8")
    cfg = Config.load(path)
    assert cfg.last_enhance == {"Dark Souls III": {"target_vertices": 2000}}
    page = cfg.prefs["ds-ui"]
    assert not set(page) & retired.PREFS and page["exportOpts"].get("format") is None and "paint" not in page["exportOpts"]
    assert page["inspectorTab"] == {"model": "cut"}  # the page itself sends a page left on Cut to Export
    cfg.save()
    text = path.read_text("utf-8")
    for gone in ("cutOpts", "cutPlans", "paintOpts", "paintSources", "finishOpts", "watertight", '".3mf"', '"finish"', '"paint"'):
        assert gone not in text, gone
    # the app works with it: the page's preferences come back without them, and are saved without them
    s = make_service(home)
    assert not set(s.call("prefs_get", {})["ds-ui"]) & retired.PREFS
    s.call("prefs_set", {"prefs": {"ds-ui": OLD_PAGE}})
    assert not set(s.config.prefs["ds-ui"]) & retired.PREFS
    assert "cutPlans" not in path.read_text("utf-8")


def test_an_old_saved_variation_loads_and_drops_them_when_written_again(home, weapon_dir, tmp_path):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    item = _weapon(s)
    m = s.call("model_open", {"id": item["id"]})
    made = wait(s, s.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 2000}})["job"])
    row = wait(s, s.call("variation_save", {"sid": m["sid"], "name": "Old one"})["job"])["variation"]
    # as a variation saved by 0.54 that names Finish, Cut and paint settings
    folder = home / "library" / "Dark Souls III packages" / "parts" / "wp_a_0300.partsbnd.dcx.darkpkg" / "variations"
    info = folder / row["id"] / variations.INFO
    data = json.loads(info.read_text("utf-8"))
    data.update({"finished": {"id": "abc"}, "cut": CUT})
    data["settings"].update({"watertight": WATERTIGHT, "paint": PAINT})
    info.write_text(json.dumps(data), "utf-8")
    listed = s.call("variations", {"sid": m["sid"]})["items"]
    assert [v["id"] for v in listed] == [row["id"]] and listed[0]["settings"] == {"target_vertices": 2000}
    assert "cut" not in listed[0] and "finished" not in listed[0]
    shown = s.call("variation_open", {"sid": m["sid"], "id": row["id"]})
    assert shown["stats"]["vertices"] == made["stats"]["vertices"]
    out = wait(s, s.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".stl", "folder": str(tmp_path)})["job"])
    assert out["bytes"] == 84 + 50 * made["stats"]["faces"]
    # saved again from it: the new one is made with the settings without them
    again = wait(s, s.call("variation_save", {"sid": m["sid"], "name": "From the old one"})["job"])["variation"]
    assert again["settings"] == {"target_vertices": 2000}
    # renamed (its record written again): they're gone from the file
    s.call("variation_rename", {"sid": m["sid"], "id": row["id"], "name": "Renamed"})
    data = json.loads(info.read_text("utf-8"))
    assert data["name"] == "Renamed" and "cut" not in data and "finished" not in data
    assert data["settings"] == {"target_vertices": 2000}


def test_an_old_bulk_preset_still_enhances(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    s.config.last_enhance = {"Dark Souls III": {"target_vertices": 2000, "watertight": WATERTIGHT, "cut": CUT, "paint": PAINT}}
    s.call("queue_add", {"action": "enhance", "ids": [_weapon(s)["id"]]})
    (item,) = _queue_done(s)["items"]
    assert item["state"] == "done" and item["result"]["done"] == 1, item
    saved = s.call("variations", {"model": _weapon(s)["id"]})["items"]
    assert len(saved) == 1 and saved[0]["settings"] == {"target_vertices": 2000}
    # generating on the page records the settings without them for the next bulk Enhance
    m = s.call("model_open", {"id": _weapon(s)["id"]})
    wait(s, s.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 2500, "cut": CUT}})["job"])
    assert s.config.last_enhance["Dark Souls III"] == {"target_vertices": 2500}


def test_an_old_clean_list_with_their_settings_still_cleans_the_model(home, weapon_dir, tmp_path):  # noqa: F811
    """A Clean list kept by an earlier version (on the page, or in a saved variation's record) whose steps carry
    Finish, Cut or paint settings: each step keeps only what it is known by, so it cleans the model as the same list
    without them, under the same name."""
    from darkstl.clean import CleanList

    plain = [{"op": "weld", "distance_mm": 0.01}, {"op": "transform", "scale": 0.5, "floor": True}]
    old = [{**plain[0], "watertight": WATERTIGHT}, {**plain[1], "lay_flat": True, "cut": CUT, "paint": PAINT}]
    assert CleanList.of(old) == CleanList.of(plain)
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    m = s.call("model_open", {"id": _weapon(s)["id"]})
    checked = s.call("clean_check", {"sid": m["sid"], "clean": old})
    assert checked == s.call("clean_check", {"sid": m["sid"], "clean": plain})
    assert checked["size_mm"][:2] == pytest.approx([500, 500])  # the 1 m plate at half its size
    out = wait(s, s.call("export", {"sid": m["sid"], "which": "original", "format": ".stl", "clean": old,
                                    "folder": str(tmp_path)})["job"])
    assert Path(out["path"]).is_file()


def test_the_engine_has_no_finish_cut_or_painted_3mf(home, weapon_dir, tmp_path):  # noqa: F811
    out_dir = tmp_path / "exports"
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    assert "cutting" not in s.call("app_info", {})
    assert ".3mf" not in [f["ext"] for f in s.call("app_info", {})["formats"]]
    m = s.call("model_open", {"id": _weapon(s)["id"]})
    assert "cut" not in m and "finished" not in m
    for cmd in ("finish", "cut", "cut_state", "paint_palette", "paint_texture"):
        with pytest.raises(AppError, match="Unknown command"):
            s.call(cmd, {"sid": m["sid"]})
    with pytest.raises(AppError, match="Painted 3MF moved to Model Painter: export a .glb and open it there"):
        s.call("export", {"sid": m["sid"], "which": "original", "format": ".3mf", "folder": str(out_dir)})
    for which in ("finished", "pieces"):
        with pytest.raises(AppError, match="moved to Model Painter"):
            s.call("export", {"sid": m["sid"], "which": which, "format": ".stl", "folder": str(out_dir)})
        with pytest.raises(AppError, match="moved to Model Painter"):
            s.call("export", {"sid": m["sid"], "which": which, "preset": "painter", "folder": str(out_dir)})
        with pytest.raises(AppError, match="moved to Model Painter"):
            s.call("model_mesh", {"sid": m["sid"], "which": which})
    assert not out_dir.exists()  # nothing written
    # every other export stays
    for ext in (".stl", ".obj", ".ply", ".glb"):
        out = wait(s, s.call("export", {"sid": m["sid"], "which": "original", "format": ext, "folder": str(out_dir)})["job"])
        assert Path(out["path"]).suffix == ext


def test_the_command_line_refuses_a_painted_3mf_and_the_retired_options(weapon_dir, tmp_path, capsys):  # noqa: F811
    from darkstl.cli import main

    model = weapon_dir / "parts" / "wp_a_0300.partsbnd.dcx"
    for cmd in ("export", "enhance"):
        assert main([cmd, str(model), "-o", str(tmp_path / "painted.3mf")]) == 1
        err = capsys.readouterr().err
        assert "error: Painted 3MF moved to Model Painter: export a .glb and open it there" in err
    assert not (tmp_path / "painted.3mf").exists()
    for gone in (["--watertight"], ["--close-gaps", "1"], ["--drop-loose", "1"], ["--keep-hollows"], ["--sheet", "1.2"],
                 ["--cut", "z=50%"], ["--clearance", "0.2"], ["--colours", "4"], ["--palette", "#ffffff"],
                 ["--print-height", "150"], ["--feature", "0.5"], ["--cleanup", "light"], ["--tolerance", "5"],
                 ["--min-share", "1"], ["--brightness", "0"], ["--contrast", "1"], ["--saturation", "1"],
                 ["--paint-texture", "0=x"]):
        with pytest.raises(SystemExit) as stop:
            main(["export", str(model), "-o", str(tmp_path / "x.stl"), *gone])
        assert stop.value.code == 2, gone
        assert "unrecognized arguments" in capsys.readouterr().err
    # what stays: the Clean tab's print height, and the other formats
    assert main(["export", str(model), "-o", str(tmp_path / "x.glb"), "--clean", "height:150"]) == 0
    assert (tmp_path / "x.glb").read_bytes()[:4] == b"glTF"
