"""A collected game works without the game: for each kind of game, collect everything, take the game's folder
away (as if it were uninstalled), and open every model, texture, pose and configuration again from the copy
alone. What each shows must be what it showed from the game. Each game is collected both ways: as copies of
its files, and imported as model packages (configurable models too, Borderlands 2 skins mixed for each part
included)."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import synth_ue3
from darkstl.app.config import Config
from darkstl.app.service import Service
from darkstl.importers import set_cache_folder
from darkstl.importers.borderlands import files as bl_files
from darkstl.importers.borderlands.ue4 import vfs
from test_animation import make_char_dir
from test_app import home, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture
from test_reengine import re2_pak_dir, re4_dir  # noqa: F401 - fixtures


def _service(home, cache: str = "cache") -> Service:  # noqa: F811
    return Service(Config.load(), home / cache)


def _model(s: Service, item: dict, choices: dict | None = None) -> dict:
    args = {"id": item["id"]}
    if choices is not None:
        args["choices"] = choices
    m = s.call("model_open", args)
    textures = {}
    for t in m["textures"]:
        png = s.call("texture", {"sid": m["sid"], "key": t["key"], "max": "64"})
        textures[t["key"]] = hashlib.sha1(png).hexdigest()
    anims = []
    if m["animated"]:
        anims = sorted(a["name"] for a in s.call("model_animations", {"sid": m["sid"]})["animations"])
    return {"name": m["name"], "variant": m["variant"], "variants": m["variants"],
            "parts": [(p["name"], p["triangles"]) for p in m["parts"]],
            "materials": [(x["name"], x["albedo"], x["normal"], x["height"]) for x in m["materials"]],
            "textures": textures, "triangles": m["stats"]["triangles"], "size": m["stats"]["size_mm"],
            "animations": anims}


def _everything(s: Service, game: str) -> dict:
    """What every model of ``game`` shows, each configurable one with each option of each slot chosen."""
    out: dict = {}
    items = s.call("models_query", {"game": game, "limit": 100000})["items"]
    for item in items:
        key = (item["category"], item["label"], bool(item.get("configurable")))
        shown = [_model(s, item)]
        if item.get("configurable"):
            config = s.call("model_open", {"id": item["id"]})["configuration"]
            for slot in config["slots"]:
                for option in slot["options"]:
                    shown.append(_model(s, item, {slot["id"]: option["id"]}))
        out.setdefault(key, []).append(shown)
    return {k: sorted(v, key=repr) for k, v in out.items()}


def collect_and_uninstall(home, game_dir: Path, game: str | None = None,  # noqa: F811
                          packages: bool = False) -> tuple[dict, dict, dict]:
    """(what the game showed, what its collected copy shows once the game is gone, the plan's summary)."""
    s = _service(home)
    args = {"path": str(game_dir)}
    if game:
        args["game"] = game
    game = wait(s, s.call("folder_add", args)["job"])["game"]
    before = _everything(s, game)
    assert before, "the game lists models"
    plan = wait(s, s.call("collect_plan", {"game": game, "job": 1, "packages": packages})["job"])
    done = wait(s, s.call("collect_run", {"game": game, "packages": packages})["job"])
    if packages:
        assert not done["failed"], done["failed"]
        assert done["written"] + done["configurable"] > 0
    s.call("folder_remove", {"path": str(game_dir)})
    gone = game_dir.with_name(game_dir.name + " (uninstalled)")
    bl_files._INDEXES.clear()
    vfs.forget(game_dir)  # lets go of the files it holds open (Windows won't move them)
    shutil.move(str(game_dir), str(gone))
    try:
        again = _service(home, "cache2")  # a fresh start, with nothing kept from the game
        wait(again, again.load_folders().id)
        after = _everything(again, game)
        if packages:  # every model opens from its package, configurable ones too (but any whose paint couldn't
            # be kept, which are copied as game files)
            items = again.call("models_query", {"game": game, "limit": 100000})["items"]
            plain = [i for i in items if not i.get("configurable")]
            copied = bool(done.get("collected"))
            assert plain and all(i["importer"] == "package" for i in plain if not (copied and _is_part(i, items))), \
                [(i["label"], i["importer"]) for i in plain if i["importer"] != "package"]
            configurable = [i for i in items if i.get("configurable")]
            if not copied:
                assert done["configurable_packed"] == done["configurable"]
                assert all(i["importer"] == "package" for i in configurable)
    finally:
        shutil.move(str(gone), str(game_dir))
    return before, after, plan


def _is_part(item: dict, items: list[dict]) -> bool:
    """A part file of a configurable model, copied as a game file with it."""
    return any(i.get("configurable") for i in items) and item["importer"] != "package"


def assert_same(before: dict, after: dict) -> None:
    for key, shown in before.items():
        assert key in after, f"{key} isn't listed in the collected copy"
        assert after[key] == shown, f"{key} differs in the collected copy"


@pytest.fixture(params=[False, True], ids=["files", "packages"])
def packages(request):
    return request.param


@pytest.fixture(autouse=True)
def _importer_cache(tmp_path):
    set_cache_folder(tmp_path / "importer-cache")
    yield
    set_cache_folder(None)


def test_fromsoftware(home, packages, weapon_dir, tmp_path):  # noqa: F811
    make_char_dir(weapon_dir)
    before, after, _plan = collect_and_uninstall(home, weapon_dir, packages=packages)
    assert any(shown[0][0]["animations"] for shown in before.values()), "an animated model is in the test"
    assert_same(before, after)


def test_borderlands_2_guns_parts_and_skins(home, packages, tmp_path):  # noqa: F811
    import test_borderlands as T

    root = tmp_path / "Borderlands 2"
    cooked = root / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    (root / "Binaries" / "Win32").mkdir(parents=True)
    (root / "Binaries" / "Win32" / "Borderlands2.exe").write_bytes(b"MZ")
    # the core package compressed as a whole, as the game's are
    (cooked / "Startup.upk").write_bytes(synth_ue3.fully_compressed(T.startup_package(None)))
    (cooked / "GD_Weap_Pistol.upk").write_bytes(T.parts_package())
    (cooked / "WeaponTextures.tfc").write_bytes(b"\0" * 64 + synth_ue3.bc1_solid(16, 16, T.FLAT_NORMAL))
    (cooked / "Unused.tfc").write_bytes(b"\0" * 4096)
    # a skin whose material is in another package, and one whose colour is mixed from masks
    (cooked / "GD_Weap_Skins.upk").write_bytes(T.skins_package())
    (cooked / "GunSkins.upk").write_bytes(T.gun_skins_package())
    (cooked / "GD_Weap_Paint.upk").write_bytes(T.paint_skins_package())
    # a gun of parts of its own
    (cooked / "Weap_AssaultRifle.upk").write_bytes(T.ar_parts_package())
    (cooked / "GD_Weap_AssaultRifle.upk").write_bytes(T.ar_part_definitions())
    bl_files._INDEXES.clear()
    before, after, plan = collect_and_uninstall(home, root, bl_files.GAME_BL2, packages=packages)
    gun = before[("Weapons", "Pistol (any parts)", True)][0]
    assert len(gun) > 3 and any(":colour" in (m[1] or "") for o in gun for m in o["materials"]), \
        "a painted skin is in the test"
    assert_same(before, after)
    if packages:  # the skins mixed for each part are packages too: how each mixes, and what from on each part
        folder = Path(plan["folder"])
        assert not folder.with_name(bl_files.GAME_BL2).exists(), "nothing was copied as game files"
        kept = [json.loads(p.read_text()) for p in folder.rglob("*.json") if p.name != "package.json"]
        assert any(k.get("paint", {}).get("kind") == "borderlands-gun-paint" for k in kept)
        assert any(k.get("paint_uvs") for k in kept)
        return
    folder = Path(plan["folder"])
    assert (folder / "WillowGame" / "CookedPCConsole" / "WeaponTextures.tfc").is_file()
    assert not (folder / "WillowGame" / "CookedPCConsole" / "Unused.tfc").exists(), "only the caches read"
    assert (folder / "collected.darkstl-configs.json").is_file() and plan["configurations"] >= 2


def test_cyberpunk_from_archives(home, packages, tmp_path):  # noqa: F811
    from test_redengine_weapons import archive_game, weapon_files

    root = archive_game(tmp_path / "Cyberpunk 2077", weapon_files())
    before, after, plan = collect_and_uninstall(home, root, packages=packages)
    assert any(k[2] for k in before), "a configurable weapon is in the test"
    assert_same(before, after)
    if not packages:
        assert not list(Path(plan["folder"]).rglob("*.archive")), "pieces of archives are kept as loose files"


def test_resident_evil_from_paks(home, packages, re2_pak_dir):  # noqa: F811
    before, after, _plan = collect_and_uninstall(home, re2_pak_dir, packages=packages)
    assert_same(before, after)


def test_resident_evil_unpacked(home, packages, re4_dir):  # noqa: F811
    before, after, _plan = collect_and_uninstall(home, re4_dir, packages=packages)
    assert_same(before, after)


def test_wonderlands_from_paks(home, packages, tmp_path):  # noqa: F811
    from test_borderlands4 import _game

    root = _game(tmp_path, 426, 11, compress=True, exe="Wonderlands.exe")
    before, after, plan = collect_and_uninstall(home, root, packages=packages)
    assert_same(before, after)
    if not packages:
        assert not list(Path(plan["folder"]).rglob("*.pak")), "pieces of paks are kept as loose files"


def test_borderlands_4_from_zen_containers(home, packages, tmp_path):  # noqa: F811
    from test_borderlands_ue5 import _bl4

    root = _bl4(tmp_path)
    before, after, _plan = collect_and_uninstall(home, root, packages=packages)
    assert_same(before, after)


def test_returnal_from_paks(home, packages, tmp_path):  # noqa: F811
    from test_returnal import _game

    root = _game(tmp_path)
    before, after, _plan = collect_and_uninstall(home, root, packages=packages)
    assert_same(before, after)


def test_returnal_unversioned_keeps_its_mappings_and_engine(home, packages, tmp_path):  # noqa: F811
    import synth_ue4 as S
    from darkstl.importers.borderlands.ue4 import unversioned
    from test_returnal import _mappings, _unversioned_files

    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    win64 = root / "Returnal" / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    (win64 / "Returnal-Win64-Shipping.exe").write_bytes(b"MZ" + "++UE4+Release-4.26".encode("utf-16-le"))
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(_unversioned_files(), compress=True))
    (paks / "Mappings.usmap").write_bytes(_mappings())
    vfs.forget(root)
    unversioned.forget_mappings()
    before, after, _plan = collect_and_uninstall(home, root, packages=packages)
    assert before and any(m["materials"][0][1] for shown in before.values() for v in shown for m in v)
    assert_same(before, after)
