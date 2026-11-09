"""In-game names from the games' own text, on byte-built files: an Unreal game's string tables (Returnal,
Borderlands 3...), Borderlands 2's localised text (.INT) beside its packages, and Cyberpunk 2077's on-screen
text."""

from __future__ import annotations

import json

from darkstl.importers.borderlands import BorderlandsImporter, files
from darkstl.importers.borderlands import names as bl_names
from darkstl.importers.borderlands.ue4 import vfs
from darkstl.importers.redengine import RedEngineImporter
from darkstl.importers.redengine import names as red_names
from darkstl.importers.returnal import GAME, ReturnalImporter
from darkstl.importers.unreal import names as unreal_names
from darkstl.importers.unreal.profile import get
from darkstl.model import CatalogEntry

import synth_red as R
import synth_ue3 as S3
import synth_ue4 as S
from test_returnal import _packages


# -- Unreal string tables -------------------------------------------------------------------------------------


def _string_table(path: str, entries: dict[str, str]) -> S.Ue4Package:
    b = S.Ue4Package(path, 426)
    e = b.add_export("StringTable", path.rsplit("/", 1)[-1])
    body = b.props() + S.fstring("Weapons")
    body += len(entries).to_bytes(4, "little", signed=True)
    for k, v in entries.items():
        body += S.fstring(k) + S.fstring(v)
    b.set_data(e, body)
    return b


def test_unreal_string_tables_name_the_meshes_made_for_their_items(tmp_path):
    packages = _packages()
    packages["UI/StringTables/ST_Weapons"] = _string_table("/Game/UI/StringTables/ST_Weapons", {
        "Carbine_Name": "Hollow Seeker",
        "Carbine_Description": "Fires a hail of bolts",  # not a name
        "Severed.DisplayName": "Severed",
        "Pillar_01_Name": "Ancient Pillar",
        "Pillar_01_Title": "Another name for the same",  # two texts claim it: neither is used
    })
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    pak = {}
    for path, b in packages.items():
        uasset, uexp, ubulk = b.build()
        pak[f"Returnal/Content/{path}.uasset"] = uasset
        pak[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            pak[f"Returnal/Content/{path}.ubulk"] = ubulk
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(pak, version=8))
    vfs.forget(root)
    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    found = imp.scan(root, GAME)
    names = imp.names(root, GAME, found)
    assert names == {"Returnal/Content/Weapons/Carbine/Meshes/SK_Carbine.uasset": "Hollow Seeker",
                     "Returnal/Content/Enemies/Severed/Meshes/SK_Severed.uasset": "Severed"}
    assert list((tmp_path / "cache" / "names").glob("tables-*.json"))  # kept until the paks change
    assert unreal_names.core("SK_Carbine_LOD0") == unreal_names.core("Carbine") == "carbine"
    table = unreal_names.name_table({"Blaster_Name": "Blaster", "Blaster_FlavorName": "x"}, get("Returnal"))
    assert table == {"blaster": "Blaster"}


# -- Borderlands 2 .INT -----------------------------------------------------------------------------------------


INT_TEXT = """[Head_Zero_Default CustomizationDefinition]
CustomizationName=Zero
Description="A head of few words"

[Skin_Zero_Blue CustomizationDefinition]
CustomizationName="Blue Steel"

[Head_Zero_Other CustomizationDefinition]
CustomizationName=Another Zero
[Unlisted CustomizationDefinition]
CustomizationName=Nothing to show
"""


def _bl2_game(tmp_path):
    root = tmp_path / "Borderlands 2"
    cooked = root / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    (root / "Binaries" / "Win32").mkdir(parents=True)
    (root / "Binaries" / "Win32" / "Borderlands2.exe").write_bytes(b"MZ")
    b = S3.PackageBuilder()
    head = b.add_export("SkeletalMesh", "SK_Head_Zero", b"")
    body = b.add_export("SkeletalMesh", "SK_Body_Zero", b"")
    b.add_export("CustomizationDefinition", "Head_Zero_Default", b.props(b.p_obj("HeadMesh", head)))
    # a mesh behind a struct, as customisations keep the body they go on
    b.add_export("CustomizationDefinition", "Skin_Zero_Blue",
                 b.props(b.p_struct("BodyData", "CustomizationBody", b.p_obj("BodyMesh", body) + b.end())))
    b.add_export("CustomizationDefinition", "Unlisted", b.props(b.p_int("Nothing", 1)))
    (cooked / "GD_Zero_Items.upk").write_bytes(b.build())
    loc = root / "WillowGame" / "Localization" / "INT"
    loc.mkdir(parents=True)
    (loc / "GD_Zero_Items.int").write_bytes(b"\xff\xfe" + INT_TEXT.encode("utf-16-le"))
    return root, cooked / "GD_Zero_Items.upk"


def test_borderlands_2_text_names_the_meshes_its_objects_show(tmp_path):
    root, package = _bl2_game(tmp_path)
    sections = bl_names.read_int((root / "WillowGame" / "Localization" / "INT" / "GD_Zero_Items.int").read_bytes())
    assert sections["Head_Zero_Default"] == ("CustomizationDefinition",
                                             {"CustomizationName": "Zero", "Description": "A head of few words"})
    assert bl_names.name_of({"Description": "x", "CustomizationName": "Zero"}) == "Zero"
    entries = [CatalogEntry("Borderlands 2", "Characters", "Zero", stem, stem,
                            files.member_path(package, stem, "SkeletalMesh"), "borderlands")
               for stem in ("SK_Head_Zero", "SK_Body_Zero")]
    imp = BorderlandsImporter()
    imp.cache = tmp_path / "cache"
    names = imp.names(root, "Borderlands 2", entries)
    assert names == {"WillowGame/CookedPCConsole/GD_Zero_Items.upk/SK_Head_Zero.SkeletalMesh": "Zero",
                     "WillowGame/CookedPCConsole/GD_Zero_Items.upk/SK_Body_Zero.SkeletalMesh": "Blue Steel"}
    assert list((tmp_path / "cache" / "names").glob("int-*.json"))
    # a second object naming the head otherwise: two names claim it, so it keeps its file name
    text = INT_TEXT.replace("[Head_Zero_Other", "[Head_Zero_Default2")
    (root / "WillowGame" / "Localization" / "INT" / "GD_Zero_Items.int").write_text(text, "utf-8")
    b = S3.PackageBuilder()
    head = b.add_export("SkeletalMesh", "SK_Head_Zero", b"")
    b.add_export("CustomizationDefinition", "Head_Zero_Default", b.props(b.p_obj("HeadMesh", head)))
    b.add_export("CustomizationDefinition", "Head_Zero_Default2", b.props(b.p_obj("HeadMesh", head)))
    package.write_bytes(b.build())
    assert imp.names(root, "Borderlands 2", entries[:1], fresh=True) == {}


# -- Cyberpunk 2077 on-screen text ------------------------------------------------------------------------------


def _onscreens(entries: list[tuple[str, str]]) -> bytes:
    w = R.Cr2wWriter()
    rows = [R.Obj("localizationPersistenceOnScreenEntry").add("primaryKey", "Uint64", i + 1)
            .add("secondaryKey", "String", key).add("femaleVariant", "String", text).add("maleVariant", "String", "")
            for i, (key, text) in enumerate(entries)]
    w.add_chunk(R.Obj("JsonResource").add("root", "handle:ISerializable", 2))
    w.add_chunk(R.Obj("localizationPersistenceOnScreenEntries")
                .add("entries", "array:localizationPersistenceOnScreenEntry", rows))
    return w.build()


def test_cyberpunk_on_screen_text_names_its_weapons(tmp_path):
    folder = tmp_path / "Cyberpunk export"
    text = folder / "base" / "localization" / "en-us" / "onscreens"
    text.mkdir(parents=True)
    (text / "onscreens.json").write_bytes(_onscreens([
        ("Gameplay-Items-Item Name-Preset_Lexington", "Lexington"),
        ("Gameplay-Items-Item Desc-Preset_Lexington", "A reliable sidearm"),
        ("Gameplay-Items-Item Name-Preset_Sor22", "SOR-22"),
    ]))
    entries = []
    for depot, category in (("base/weapons/handguns/militech_lexington/meshes/w_handgun__militech_lexington__body.mesh",
                             "Weapons"),
                            ("base/weapons/rifles/midnight_sor22/meshes/w_rifle__midnight_sor22.mesh", "Weapons"),
                            ("base/weapons/rifles/unknown_gun/meshes/w_rifle__unknown_gun__mag.mesh", "Weapons"),
                            ("base/characters/garment/torso/t1_jacket.mesh", "Armor")):
        p = folder / depot
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"CR2W")
        entries.append(CatalogEntry("Cyberpunk 2077", category, "", p.stem, p.stem, p, "redengine", packed=False))
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    names = imp.names(folder, "Cyberpunk 2077", entries)
    assert names == {
        "base/weapons/handguns/militech_lexington/meshes/w_handgun__militech_lexington__body.mesh": "Lexington (body)",
        "base/weapons/rifles/midnight_sor22/meshes/w_rifle__midnight_sor22.mesh": "SOR-22",
    }
    assert list((tmp_path / "cache" / "names").glob("onscreens-*.json"))
    # WolvenKit's JSON of the same file reads alike
    wkit = {"Header": {}, "Data": {"RootChunk": {"root": {"Data": {"entries": [
        {"$type": "localizationPersistenceOnScreenEntry", "primaryKey": "1",
         "secondaryKey": "Gameplay-Items-Item Name-Preset_Lexington", "femaleVariant": "Lexington",
         "maleVariant": ""}]}}}}}
    assert red_names.read_onscreens(json.dumps(wkit).encode()) == [("Gameplay-Items-Item Name-Preset_Lexington",
                                                                     "Lexington")]
    assert red_names.weapon_model("base\\weapons\\rifles\\midnight_sor22\\w_rifle__midnight_sor22__scope.mesh") \
        == (["midnightsor22", "sor22"], "scope")
