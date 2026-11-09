"""Game profiles as JSON: the shipped ones, one in the settings folder adding a game or replacing a shipped one,
and the generic Unreal importer reading an Unreal game no profile names (added under a name the user types)."""

from __future__ import annotations

import json
import struct

import pytest

from darkstl import library
from darkstl.app.config import Config
from darkstl.app.service import Service
from darkstl.importers import importer_for, open_model, profiles
from darkstl.importers.borderlands.ue4 import vfs
from darkstl.importers.fromsoft import games as fromsoft_games
from darkstl.importers.reengine import games as re_games
from darkstl.importers.returnal import GAME, ReturnalImporter
from darkstl.importers.unreal import UnrealImporter, catalog
from darkstl.importers.unreal.profile import UnrealProfile, get

import synth_ue4 as S
from test_app import wait
from test_returnal import _packages


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    profiles.forget()
    yield tmp_path / "home"
    profiles.forget()


def _user_profile(home, name: str, data: dict) -> None:
    folder = home / "profiles"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps({"format": 1, **data}), "utf-8")
    profiles.forget()


def _unreal_game(tmp_path, project: str, folder: str | None = None, exe: bool = True):
    """An Unreal 4 game installed in ``<folder>/``: ``<project>/Content/Paks`` with one pak of Returnal-like
    packages (characters, creatures, a weapon, map pieces, props) and its shipping executable."""
    root = tmp_path / (folder or project)
    paks = root / project / "Content" / "Paks"
    paks.mkdir(parents=True)
    if exe:
        (root / project / "Binaries" / "Win64").mkdir(parents=True)
        (root / project / "Binaries" / "Win64" / f"{project}-Win64-Shipping.exe").write_bytes(b"MZ")
    files = {}
    for path, b in _packages().items():
        uasset, uexp, ubulk = b.build()
        files[f"{project}/Content/{path}.uasset"] = uasset
        files[f"{project}/Content/{path}.uexp"] = uexp
        if ubulk:
            files[f"{project}/Content/{path}.ubulk"] = ubulk
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(files, version=8))
    vfs.forget(root)
    return root


def test_the_shipped_profiles(home):
    assert profiles.problems() == []
    names = {p.name: p for p in profiles.profiles()}
    assert {"Returnal", "Borderlands 3", "Tiny Tina's Wonderlands", "Borderlands 4"} <= set(names)
    assert names["Returnal"].engine == "unreal4" and names["Returnal"].importer == "returnal"
    assert names["Borderlands 3"].importer == "borderlands"
    assert profiles.defaults("unreal4")["name"] == "Unreal game"
    returnal = get("Returnal")
    assert returnal.project == "Returnal" and returnal.sorting.kinds
    assert returnal.names_tables is not None  # the defaults' string table rules come with every Unreal game


def test_a_profile_in_the_settings_folder_adds_a_game_and_sorts_its_models(home, tmp_path):
    _user_profile(home, "starfall.json", {
        "name": "Starfall", "engine": "unreal4",
        "recognise": {"files": ["Starfall/Binaries/Win64/Starfall-Win64-Shipping.exe"]},
        "unreal": {"project": "Starfall"},
        "models": {"kinds": [{"folders": ["enemies"], "kind": "Characters", "group": ["Foes", "{1}"]},
                             {"folders": ["weapons"], "kind": "Weapons", "group": ["Guns / {1}"]}]},
    })
    root = _unreal_game(tmp_path, "Starfall")
    assert library.detect_game(root) == "Starfall"
    assert library.detect_game(root / "Starfall" / "Content" / "Paks") == "Starfall"
    assert "Starfall" in library.known_games()
    imp = UnrealImporter()
    imp.cache = tmp_path / "cache"
    found = {e.stem: e for e in imp.scan(root, "Starfall")}
    assert (found["SK_Severed"].category, found["SK_Severed"].group) == ("Characters", "Foes / Severed")
    assert (found["SK_Carbine"].category, found["SK_Carbine"].group) == ("Weapons", "Guns / Carbine")
    # what no rule of the profile sorts falls back to the defaults' "otherwise"
    assert (found["SM_Pillar_01"].category, found["SM_Pillar_01"].group) == ("Objects", "Environments")
    assert all(e.importer == "unreal" and e.game == "Starfall" for e in found.values())
    model = open_model(found["SK_Carbine"].path)
    assert model.importer == "unreal" and model.materials[0].albedo
    assert importer_for(found["SK_Carbine"].path).key == "unreal"


def test_a_profile_in_the_settings_folder_replaces_the_shipped_one(home, tmp_path):
    from test_returnal import _game

    root = _game(tmp_path)
    shipped = {e.stem: e for e in ReturnalImporter().scan(root, GAME)}
    assert shipped["SK_Carbine"].group == "Carbine"
    data = json.loads(profiles.get("Returnal").source.read_text("utf-8"))
    data["models"]["kinds"].insert(0, {"folders": ["weapons"], "kind": "Weapons", "group": ["Arsenal"]})
    _user_profile(home, "returnal.json", data)
    assert not profiles.get("Returnal").shipped
    vfs.forget(root)
    ours = {e.stem: e for e in ReturnalImporter().scan(root, GAME)}
    assert ours["SK_Carbine"].group == "Arsenal"
    assert ours["SK_Selene_Suit"].group == "Selene"  # the rest as before
    # gone again: the shipped profile is read once more
    (home / "profiles" / "returnal.json").unlink()
    profiles.forget()
    vfs.forget(root)
    assert {e.stem: e for e in ReturnalImporter().scan(root, GAME)}["SK_Carbine"].group == "Carbine"


def test_extends_and_broken_files(home):
    _user_profile(home, "returnal-demo.json", {"name": "Returnal Demo", "extends": "Returnal",
                                                "unreal": {"project": "ReturnalDemo"}})
    _user_profile(home, "broken.json", {"name": "Broken"})
    (home / "profiles" / "not-json.json").write_text("{ nope", "utf-8")
    profiles.forget()
    demo = get("Returnal Demo")
    assert demo.project == "ReturnalDemo"
    assert len(demo.sorting.kinds) == len(get("Returnal").sorting.kinds)  # the rest from Returnal's
    assert demo.importer == "returnal"
    problems = " ".join(profiles.problems())
    assert "not-json.json" in problems and "broken.json" in problems


def test_other_engines_take_an_override_from_the_settings_folder(home, tmp_path):
    _user_profile(home, "dark-souls-iii.json", {"name": "Dark Souls III", "engine": "fromsoft",
                                                 "recognise": {"files": ["ds3-renamed.exe"]},
                                                 "fromsoft": {"compression": "ZSTD"}})
    _user_profile(home, "armored-core-vi.json", {"name": "Armored Core VI", "engine": "fromsoft",
                                                  "recognise": {"files": ["armoredcore6.exe"],
                                                                "folder_pattern": "armored\\s*core"},
                                                  "fromsoft": {"compression": "KRAK",
                                                               "texture_paths_in_flver": False}})
    _user_profile(home, "re-requiem.json", {"name": "Resident Evil Requiem", "engine": "reengine",
                                             "recognise": {"files": ["re9.exe"]}})
    ds3 = tmp_path / "game one"
    ds3.mkdir()
    (ds3 / "ds3-renamed.exe").write_bytes(b"MZ")
    assert fromsoft_games.detect_game(ds3) == "Dark Souls III"
    assert fromsoft_games.profile("Dark Souls III").compression == "ZSTD"
    ac = tmp_path / "games" / "Armored Core VI"
    ac.mkdir(parents=True)
    assert fromsoft_games.detect_game(ac) == "Armored Core VI"
    assert not fromsoft_games.profile("Armored Core VI").texture_paths_in_flver
    assert "Armored Core VI" in library.known_games()
    re9 = tmp_path / "re9"
    re9.mkdir()
    (re9 / "re9.exe").write_bytes(b"MZ")
    assert re_games.detect_game(re9) == "Resident Evil Requiem"
    # without the files, as shipped
    for f in (home / "profiles").iterdir():
        f.unlink()
    profiles.forget()
    assert fromsoft_games.detect_game(ds3) is None
    assert fromsoft_games.profile("Dark Souls III").compression == "DFLT"


def test_an_unreal_game_no_profile_names_opens_under_a_typed_name(home, tmp_path):
    root = _unreal_game(tmp_path, "Moonfall", folder="Moonfall Deluxe")
    assert library.detect_game(root) is None
    info = catalog.probe(root)
    assert info["project"] == "Moonfall" and info["engine"] == "unreal4"
    assert info["executable"] == "Moonfall/Binaries/Win64/Moonfall-Win64-Shipping.exe"
    # read with the defaults' rules under any name, before a profile is written
    imp = UnrealImporter()
    found = {e.stem: e for e in imp.scan(root, "Moon Fall")}
    assert (found["SK_Carbine"].category, found["SK_Carbine"].group) == ("Weapons", "Carbine")
    assert (found["SK_Severed"].category, found["SK_Severed"].group) == ("Characters", "Creatures / Severed")
    assert (found["SM_Pillar_01"].category, found["SM_Pillar_01"].group) == ("Map Pieces", "OvergrownRuins")
    assert found["Xenoglyph_Cipher"].category == "Objects"
    model = open_model(found["SK_Selene_Suit"].path)
    assert model.parts and model.materials[0].albedo

    # added in the app: offered as an Unreal game, and a profile is written so it's recognised from now on
    s = Service(Config.load(), home / "cache")
    check = s.call("folder_check", {"path": str(root)})
    assert check["game"] is None and check["unreal"]["name"] == "Moonfall Deluxe"  # the install folder's name
    result = wait(s, s.call("folder_add", {"path": str(root), "game": "Moon Fall"})["job"])
    assert result["game"] == "Moon Fall" and result["count"] == 6
    written = json.loads((home / "profiles" / "moon-fall.json").read_text("utf-8"))
    assert written["engine"] == "unreal4" and written["unreal"]["project"] == "Moonfall"
    assert library.detect_game(root) == "Moon Fall"
    settings = s.call("settings_get", {})["profiles"]
    assert [p["name"] for p in settings["user"]] == ["Moon Fall"]
    assert any(p["name"] == "Returnal" for p in settings["shipped"])
    # Collect keeps the engine release with the models, as for Returnal
    shared = imp.shared_files(root, "Moon Fall")
    assert all(r.kind == "other" for r in shared)


def test_copying_a_shipped_profile_into_the_settings_folder(home):
    s = Service(Config.load(), home / "cache")
    r = s.call("profile_copy", {"name": "Returnal"})
    assert (home / "profiles" / "returnal.json").is_file()
    assert any(p["name"] == "Returnal" and p["replaces"] for p in r["profiles"]["user"])
    assert any(p["name"] == "Returnal" and p["replaced"] for p in r["profiles"]["shipped"])
    assert UnrealProfile.from_profile(profiles.get("Returnal")).project == "Returnal"


def test_a_locked_pak_of_an_unnamed_game_says_where_its_key_goes(home, tmp_path):
    root = tmp_path / "Lockbox"
    paks = root / "Lockbox" / "Content" / "Paks"
    paks.mkdir(parents=True)
    files = {"Lockbox/Content/Props/SM_Box.uasset": struct.pack("<I", S.TAG) + b"\0" * 60}
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(files, version=11, key=bytes(range(32))))
    vfs.forget(root)
    imp = UnrealImporter()
    found = imp.scan(root, "Lockbox")
    notes = imp.folder_notes(root, found)
    assert found == [] and any("Settings under Unreal games" in n for n in notes)


def test_every_shipped_profile_is_packaged():
    """Each shipped profile matches a [tool.setuptools.package-data] pattern, so wheels (and the Windows app,
    built from a non-editable install) carry it."""
    import re
    from pathlib import Path

    import darkstl

    pkg = Path(darkstl.__file__).parent
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8")
    block = re.search(r"\[tool\.setuptools\.package-data\]\s*darkstl\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "no package-data for darkstl in pyproject.toml"
    patterns = re.findall(r'"([^"]+)"', block.group(1))
    packaged = {p for pattern in patterns for p in pkg.glob(pattern) if p.is_file()}
    shipped = list((pkg / "importers").glob("profiles/*.json")) + list((pkg / "importers").glob("*/profiles/*.json"))
    assert shipped and profiles.SHIPPED in {p.parent for p in shipped}
    assert [p.relative_to(pkg).as_posix() for p in shipped if p not in packaged] == []
