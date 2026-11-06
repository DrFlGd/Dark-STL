"""The Borderlands importer for the Unreal 4 games (Borderlands 3, Wonderlands): paks (plain, compressed,
encrypted, both index layouts), packages, skeletal and static meshes, textures, materials, the library scan
and the guns' configurations."""

import struct

import numpy as np
import pytest

from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.borderlands import BorderlandsImporter, files
from darkstl.importers.borderlands.ue4 import asset, pak, vfs
from darkstl.model import MeshOptions

import synth_ue4 as S

KEY = bytes(range(32))
RED, FLAT_NORMAL = 0xF800, 0x841F
POS = np.array([[0, 0, 0], [100, 0, 0], [0, 100, 0], [100, 100, 0], [0, 0, 50], [100, 0, 50]], float)
NRM = np.tile([0.0, 0.0, 1.0], (6, 1))
UV = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [0, 0.5], [1, 0.5]], float)
IDX = [0, 1, 2, 2, 1, 3, 0, 4, 1, 1, 4, 5]


def gun_packages(engine: int) -> dict[str, S.Ue4Package]:
    """A body and three parts of an assault rifle, a material instance, its two textures and a crate, by path
    below the content folder."""
    bl3 = engine < 426
    out = {}
    for tex, color, normal in (("T_Gun_Dif", RED, False), ("T_Gun_Nrm", FLAT_NORMAL, True)):
        b = S.Ue4Package(f"/Game/Gear/Weapons/AssaultRifles/Atlas/Textures/{tex}", engine)
        e = b.add_export("Texture2D", tex)
        b.set_data(e, S.texture_data(b, 8, 8, S.bc1_solid(8, 8, color), normal=normal, bl3=bl3))
        out[f"Gear/Weapons/AssaultRifles/Atlas/Textures/{tex}"] = b
    b = S.Ue4Package("/Game/Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun", engine)
    base = "/Game/Gear/Weapons/AssaultRifles/Atlas/Textures/"
    dif = b.import_object(base + "T_Gun_Dif", "Texture2D", "T_Gun_Dif")
    nrm = b.import_object(base + "T_Gun_Nrm", "Texture2D", "T_Gun_Nrm")
    e = b.add_export("MaterialInstanceConstant", "MI_Gun")
    b.set_data(e, S.mic_data(b, 0, [("Diffuse", dif), ("Normal", nrm)]))
    out["Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun"] = b
    for i, part in enumerate(("Body", "Barrel_01", "Barrel_02", "Mag_01")):
        stem = f"SK_AR_ATL_{part}"
        b = S.Ue4Package(f"/Game/Gear/Weapons/AssaultRifles/Atlas/Model/{stem}", engine)
        mat = b.import_object("/Game/Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun", "MaterialInstanceConstant",
                              "MI_Gun")
        e = b.add_export("SkeletalMesh", stem)
        b.set_data(e, S.skeletal_data(b, [mat], POS + [i * 200, 0, 0], NRM, UV, IDX, [(0, 0, 4)],
                                      in_bulk=not bl3 and i % 2 == 1, cooked_out_first=not bl3 and i == 0))
        out[f"Gear/Weapons/AssaultRifles/Atlas/Model/{stem}"] = b
    b = S.Ue4Package("/Game/InteractiveObjects/Crates/SM_Crate", engine)
    mat = b.import_object("/Game/Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun", "MaterialInstanceConstant",
                          "MI_Gun")
    e = b.add_export("StaticMesh", "SM_Crate")
    b.set_data(e, S.static_data(b, [mat], POS, NRM, UV, IDX, [(0, 0, 2), (0, 6, 2)], bl3=bl3))
    out["InteractiveObjects/Crates/SM_Crate"] = b
    return out


def _gun_files(engine: int, prefix: str = "OakGame/Content/") -> dict[str, bytes]:
    out = {}
    for path, b in gun_packages(engine).items():
        uasset, uexp, ubulk = b.build()
        out[prefix + path + ".uasset"] = uasset
        out[prefix + path + ".uexp"] = uexp
        if ubulk:
            out[prefix + path + ".ubulk"] = ubulk
    return out


def _game(tmp_path, engine: int, version: int, key=None, compress=False, exe="Borderlands3.exe"):
    root = tmp_path / ("Borderlands 3" if engine < 426 else "Tiny Tinas Wonderlands")
    paks = root / "OakGame" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "OakGame" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "OakGame" / "Binaries" / "Win64" / exe).write_bytes(b"MZ")
    data = S.build_pak(_gun_files(engine), version=version, compress=compress, key=key)
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(data)
    vfs.forget(root)
    return root


@pytest.mark.parametrize("version,compress,key,four", [(8, False, None, False), (8, True, KEY, False),
                                                       (8, True, None, True), (11, True, None, False),
                                                       (11, False, KEY, False)])
def test_pak_index_and_entries(tmp_path, version, compress, key, four):
    files_in = {"OakGame/Content/a.txt": b"hello" * 1000, "OakGame/Content/Sub/b.bin": bytes(range(256)) * 300}
    p = tmp_path / "x.pak"
    p.write_bytes(S.build_pak(files_in, version=version, compress=compress, key=key, block_size=0x4000,
                              four_methods=four))
    assert pak.is_pak_file(p)
    if key is not None:
        with pytest.raises(FormatError):
            pak.open_pak(p, [])
        with pytest.raises(FormatError):
            pak.open_pak(p, [bytes(32)])
    pk = pak.open_pak(p, [bytes(32), key] if key else [])
    assert pk.version == version and pk.mount == "../../../" and pk.method_u8 == four
    for name, data in files_in.items():
        assert pk.read(name) == data
    assert pak.parse_key("0x" + KEY.hex()) == KEY


def test_package_names_and_versions():
    assert vfs.package_name("OakGame/Content/Gear/X.uasset") == "/Game/Gear/X"
    assert vfs.package_name("OakGame/Plugins/DLC/Geranium/Content/A/B.uexp") == "/Geranium/A/B"
    b = S.Ue4Package("/Game/A/SM_A", 426, custom={asset.CUSTOM_VERSIONS["Rendering"][0]: 7})
    b.add_export("StaticMesh", "SM_A", data=b.props())
    uasset, uexp, _ = b.build()
    pkg = asset.read_package("/Game/A/SM_A", uasset, uexp)
    assert pkg.versions.engine == 426 and pkg.versions.ue4 == 522
    assert pkg.versions.cv("Rendering") == 7 and pkg.versions.cv("Editor") == 40
    assert pkg.main_export().class_name == "StaticMesh" and pkg.exports[0].path == "SM_A"


@pytest.mark.parametrize("engine,version,exe,game", [
    (420, 8, "Borderlands3.exe", files.GAME_BL3), (426, 11, "Wonderlands.exe", files.GAME_WL)])
def test_scan_and_open_guns(tmp_path, monkeypatch, engine, version, exe, game):
    key = KEY if engine < 426 else None
    if key is not None:
        monkeypatch.setitem(vfs.BUNDLED_KEYS, files.GAME_BL3, ["0x" + KEY.hex()])
    root = _game(tmp_path, engine, version, key=key, compress=True, exe=exe)
    imp = BorderlandsImporter()
    assert files.detect_game(root) == game
    found = imp.scan(root, game)
    stems = {e.stem: e for e in found}
    assert set(stems) == {"SK_AR_ATL_Body", "SK_AR_ATL_Barrel_01", "SK_AR_ATL_Barrel_02", "SK_AR_ATL_Mag_01",
                          "SM_Crate"}
    body = stems["SK_AR_ATL_Body"]
    assert body.category == "Weapons" and body.group == "Assault rifles / Atlas" and body.packed
    assert stems["SM_Crate"].category == "Objects"
    assert importer_for(body.path).key == "borderlands"
    for stem in ("SK_AR_ATL_Body", "SK_AR_ATL_Barrel_01"):
        m = open_model(stems[stem].path)
        mesh = m.build(MeshOptions())
        assert len(mesh.faces) == 4
        offset = 0 if stem.endswith("Body") else 2.0
        assert np.allclose(mesh.vertices[:, 0].min(), offset) and np.allclose(mesh.vertices[:, 0].max(), offset + 1)
        assert np.allclose(sorted(set(np.round(-mesh.vertices[:, 1], 6))), [0, 1])  # Y mirrored, in metres
        mat = m.materials[0]
        assert mat.name == "MI_Gun" and mat.albedo and mat.normal
        albedo = m.textures[mat.albedo].load()
        assert albedo.shape[:2] == (8, 8) and albedo[0, 0, 0] > 240 and albedo[0, 0, 2] < 10
        assert m.textures[mat.normal].load().shape[:2] == (8, 8)
    crate = open_model(stems["SM_Crate"].path)
    assert len(crate.parts) == 2 and len(crate.build(MeshOptions()).faces) == 4
    assert len(crate.build(MeshOptions(parts=[1])).faces) == 2
    configs = imp.configurations(root, game, found)
    assert len(configs) == 1
    c = configs[0]
    assert c.base.file == str(body.path) and c.guessed
    assert [s.id for s in c.slots] == ["barrel", "magazine"]
    assert [o.label for o in c.slots[0].options] == ["AR ATL Barrel 01", "AR ATL Barrel 02"]


def test_collect_extracts_the_package_and_its_textures(tmp_path):
    root = _game(tmp_path, 426, 11, exe="Wonderlands.exe")
    imp = BorderlandsImporter()
    found = {e.stem: e for e in imp.scan(root, files.GAME_WL)}
    rel = imp.related_files(found["SK_AR_ATL_Barrel_01"].path)
    names = sorted(r.save_as.rsplit("/", 1)[-1] for r in rel)
    assert "SK_AR_ATL_Barrel_01.uasset" in names and "SK_AR_ATL_Barrel_01.ubulk" in names
    assert "T_Gun_Dif.uasset" in names and "MI_Gun.uasset" in names
    out = tmp_path / "collected" / "OakGame"
    for r in rel:
        dest = tmp_path / "collected" / r.save_as
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.extract())
    loose = out / "Content/Gear/Weapons/AssaultRifles/Atlas/Model/SK_AR_ATL_Barrel_01.uasset"
    assert loose.is_file()
    vfs.forget(tmp_path / "collected")
    m = open_model(loose)
    assert len(m.build(MeshOptions()).faces) == 4
    assert m.materials[0].albedo is not None


def test_locked_paks_are_reported(tmp_path):
    root = _game(tmp_path, 426, 11, key=KEY, exe="Wonderlands.exe")
    imp = BorderlandsImporter()
    found = imp.scan(root, files.GAME_WL)
    assert found == []
    notes = imp.folder_notes(root, found)
    assert any(vfs.KEY_FILE in n for n in notes)
    (root / vfs.KEY_FILE).write_text("0x" + KEY.hex() + "\n")
    vfs.forget(root)
    assert len(imp.scan(root, files.GAME_WL)) == 5


def test_bulk_header_flags():
    r = asset.Reader(struct.pack("<IiIq", 0x40, 3, 3, 0) + b"abc")
    r.pkg = None
    h, pos = asset.skip_bulk(r)
    assert asset.is_inline(h) and pos == 20 and r.pos == 23
