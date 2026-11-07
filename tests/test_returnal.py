"""The Returnal importer: finding the game, listing its meshes (by name, and by reading the class of packages
whose names don't say it), grouping them, opening them with their textures, Collect, and keeping the Borderlands
importer out of the folder."""

import struct

import numpy as np
import pytest

from darkstl import library
from darkstl.importers import importer_for, open_model
from darkstl.importers.borderlands import BorderlandsImporter
from darkstl.importers.borderlands.ue4 import vfs
from darkstl.importers.returnal import GAME, ReturnalImporter, catalog
from darkstl.model import MeshOptions

import synth_ue4 as S

KEY = bytes(range(32))
RED, FLAT_NORMAL = 0xF800, 0x841F
POS = np.array([[0, 0, 0], [100, 0, 0], [0, 100, 0], [100, 100, 0], [0, 0, 50], [100, 0, 50]], float)
NRM = np.tile([0.0, 0.0, 1.0], (6, 1))
UV = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [0, 0.5], [1, 0.5]], float)
IDX = [0, 1, 2, 2, 1, 3, 0, 4, 1, 1, 4, 5]


def _packages(engine: int = 426) -> dict[str, S.Ue4Package]:
    out = {}
    tex_dir = "/Game/Characters/Selene/Textures/"
    for tex, color, normal in (("T_Selene_D", RED, False), ("T_Selene_N", FLAT_NORMAL, True)):
        b = S.Ue4Package(tex_dir + tex, engine)
        e = b.add_export("Texture2D", tex)
        b.set_data(e, S.texture_data(b, 8, 8, S.bc1_solid(8, 8, color), normal=normal))
        out[f"Characters/Selene/Textures/{tex}"] = b
    b = S.Ue4Package("/Game/Characters/Selene/Materials/MI_Selene_Suit", engine)
    d = b.import_object(tex_dir + "T_Selene_D", "Texture2D", "T_Selene_D")
    n = b.import_object(tex_dir + "T_Selene_N", "Texture2D", "T_Selene_N")
    e = b.add_export("MaterialInstanceConstant", "MI_Selene_Suit")
    b.set_data(e, S.mic_data(b, 0, [("BaseColor", d), ("Normal", n)]))
    out["Characters/Selene/Materials/MI_Selene_Suit"] = b
    mat = "/Game/Characters/Selene/Materials/MI_Selene_Suit"

    def skeletal(path: str, stem: str):
        b = S.Ue4Package(f"/Game/{path}/{stem}", engine)
        m = b.import_object(mat, "MaterialInstanceConstant", "MI_Selene_Suit")
        e = b.add_export("SkeletalMesh", stem)
        b.set_data(e, S.skeletal_data(b, [m], POS, NRM, UV, IDX, [(0, 0, 4)]))
        out[f"{path}/{stem}"] = b

    def static(path: str, stem: str):
        b = S.Ue4Package(f"/Game/{path}/{stem}", engine)
        m = b.import_object(mat, "MaterialInstanceConstant", "MI_Selene_Suit")
        e = b.add_export("StaticMesh", stem)
        b.set_data(e, S.static_data(b, [m], POS, NRM, UV, IDX, [(0, 0, 2), (0, 6, 2)]))
        out[f"{path}/{stem}"] = b

    skeletal("Characters/Selene/Meshes", "SK_Selene_Suit")
    skeletal("Characters/Selene/Meshes", "Selene_Helmet")  # no prefix: found by reading its class
    skeletal("Enemies/Severed/Meshes", "SK_Severed")
    skeletal("Weapons/Carbine/Meshes", "SK_Carbine")
    static("Environments/OvergrownRuins/Pillars", "SM_Pillar_01")
    static("Props/Xenoglyph", "Xenoglyph_Cipher")  # no prefix
    b = S.Ue4Package("/Game/Props/Xenoglyph/Xenoglyph_Data", engine)  # no prefix, not a mesh
    b.add_export("DataAsset", "Xenoglyph_Data", data=b.props())
    out["Props/Xenoglyph/Xenoglyph_Data"] = b
    return out


def _files(engine: int = 426) -> dict[str, bytes]:
    out = {}
    for path, b in _packages(engine).items():
        uasset, uexp, ubulk = b.build()
        out[f"Returnal/Content/{path}.uasset"] = uasset
        out[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            out[f"Returnal/Content/{path}.ubulk"] = ubulk
    return out


def _game(tmp_path, key=None, version=8, compress=True):
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64" / "Returnal-Win64-Shipping.exe").write_bytes(b"MZ")
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(
        S.build_pak(_files(), version=version, compress=compress, key=key))
    vfs.forget(root)
    return root


def test_detects_the_game(tmp_path):
    root = _game(tmp_path)
    assert catalog.detect_game(root) == GAME
    assert catalog.detect_game(root / "Returnal" / "Content" / "Paks") == GAME
    assert catalog.detect_game(root / "Returnal") == GAME
    assert library.detect_game(root) == GAME
    assert BorderlandsImporter().detect_game(root) is None
    assert catalog.detect_game(tmp_path) is None


@pytest.mark.parametrize("version,key", [(8, None), (11, KEY)])
def test_scan_groups_and_opens_models(tmp_path, monkeypatch, version, key):
    if key is not None:
        monkeypatch.setenv("DARKSTL_AES_KEYS", "0x" + key.hex())
    root = _game(tmp_path, key=key, version=version)
    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    found = imp.scan(root, GAME)
    by = {e.stem: e for e in found}
    assert set(by) == {"SK_Selene_Suit", "Selene_Helmet", "SK_Severed", "SK_Carbine", "SM_Pillar_01",
                       "Xenoglyph_Cipher"}
    assert (by["SK_Selene_Suit"].category, by["SK_Selene_Suit"].group) == ("Characters", "Selene")
    assert by["Selene_Helmet"].category == "Faces & Hair"
    assert (by["SK_Severed"].category, by["SK_Severed"].group) == ("Characters", "Creatures / Severed")
    assert (by["SK_Carbine"].category, by["SK_Carbine"].group) == ("Weapons", "Carbine")
    assert (by["SM_Pillar_01"].category, by["SM_Pillar_01"].group) == ("Map Pieces", "OvergrownRuins")
    assert by["Xenoglyph_Cipher"].category == "Objects"
    assert all(e.packed and e.importer == "returnal" and e.game == GAME for e in found)
    assert list((tmp_path / "cache").glob("classes-*.json"))  # what was read is kept for the next scan
    assert len(imp.scan(root, GAME)) == len(found)

    suit = by["SK_Selene_Suit"].path
    assert importer_for(suit).key == "returnal"
    m = open_model(suit)
    assert m.game == GAME and m.importer == "returnal"
    mesh = m.build(MeshOptions())
    assert len(mesh.faces) == 4 and np.allclose(mesh.vertices[:, 0].max(), 1)  # centimetres -> metres
    mat = m.materials[0]
    assert mat.name == "MI_Selene_Suit" and mat.albedo and mat.normal
    albedo = m.textures[mat.albedo].load()
    assert albedo.shape[:2] == (8, 8) and albedo[0, 0, 0] > 240
    pillar = open_model(by["SM_Pillar_01"].path)
    assert len(pillar.parts) == 2 and len(pillar.build(MeshOptions(parts=[0])).faces) == 2


def test_borderlands_importer_stays_out(tmp_path):
    root = _game(tmp_path)
    found, notes = library.scan_folder(root, GAME)
    assert found and {e.importer for e in found} == {"returnal"}
    assert BorderlandsImporter().scan(root, GAME) == []
    assert not notes


def test_locked_paks_ask_for_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKSTL_AES_KEYS", raising=False)
    root = _game(tmp_path, key=KEY)
    found, notes = library.scan_folder(root, GAME)
    assert found == []
    assert len(notes) == 1 and "Returnal" in notes[0] and vfs.KEY_FILE in notes[0]
    (root / vfs.KEY_FILE).write_text("0x" + KEY.hex() + "\n")
    vfs.forget(root)
    assert len(ReturnalImporter().scan(root, GAME)) == 6


def test_collected_files_open_loose(tmp_path):
    root = _game(tmp_path)
    imp = ReturnalImporter()
    by = {e.stem: e for e in imp.scan(root, GAME)}
    rel = imp.related_files(by["SK_Carbine"].path)
    names = sorted(r.save_as.rsplit("/", 1)[-1] for r in rel)
    assert "SK_Carbine.uasset" in names and "T_Selene_D.uasset" in names and "MI_Selene_Suit.uasset" in names
    out = tmp_path / "library" / "Returnal"
    for r in rel:
        dest = out / r.save_as
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.extract())
    import shutil
    shutil.rmtree(root)  # the game is uninstalled: the collected files are all the app has
    vfs.forget(root)
    vfs.forget(out)
    assert catalog.detect_game(out) == GAME
    loose = {e.stem: e for e in imp.scan(out, GAME)}
    assert set(loose) == {"SK_Carbine"} and not loose["SK_Carbine"].packed
    m = open_model(loose["SK_Carbine"].path)
    assert importer_for(loose["SK_Carbine"].path).key == "returnal"
    assert len(m.build(MeshOptions()).faces) == 4
    mat = m.materials[0]
    assert m.textures[mat.albedo].load()[0, 0, 0] > 240 and m.textures[mat.normal].load().shape[:2] == (8, 8)


# -- packages cooked unversioned, as the real game's are ----------------------------------

def _unversioned_files() -> dict[str, bytes]:
    from test_borderlands_ue5 import _header

    out = {}
    tex_dir = "/Game/Gameplay/Weapons/Player_KineticRifle/Textures/"
    pk = {}
    for tex, color, normal in (("T_KineticRifle_A", RED, False), ("T_KineticRifle_N", FLAT_NORMAL, True)):
        b = S.Ue4Package(tex_dir + tex, 426, unversioned=True)
        e = b.add_export("Texture2D", tex)
        b.set_data(e, S.texture_data(b, 8, 8, S.bc1_solid(8, 8, color), normal=normal))
        pk[f"Gameplay/Weapons/Player_KineticRifle/Textures/{tex}"] = b
    mi = "/Game/Gameplay/Weapons/Player_KineticRifle/Materials/MI_KineticRifle"
    b = S.Ue4Package(mi, 426, unversioned=True)
    refs = [b.import_object(tex_dir + t, "Texture2D", t) for t in ("T_KineticRifle_A", "T_KineticRifle_N")]
    elems = b""
    for name, ref in zip(("BaseColor", "Normal"), refs):
        info = _header([(0, True), (1, True)]) + b.fname(name) + b"\x02"
        elems += _header([(0, True), (1, True)]) + info + struct.pack("<i", ref)
    e = b.add_export("MaterialInstanceConstant", "MI_KineticRifle")
    b.set_data(e, _header([(4, True)]) + struct.pack("<i", 2) + elems + struct.pack("<i", 0))
    pk["Gameplay/Weapons/Player_KineticRifle/Materials/MI_KineticRifle"] = b
    b = S.Ue4Package("/Game/Gameplay/Weapons/Player_KineticRifle/SK_KineticRifle", 426, unversioned=True)
    m = b.import_object(mi, "MaterialInstanceConstant", "MI_KineticRifle")
    e = b.add_export("SkeletalMesh", "SK_KineticRifle")
    b.set_data(e, S.skeletal_data(b, [m], POS, NRM, UV, IDX, [(0, 0, 4)]))
    pk["Gameplay/Weapons/Player_KineticRifle/SK_KineticRifle"] = b
    b = S.Ue4Package("/Game/Environments/Biome1/SM_Pillar", 426, unversioned=True)
    m = b.import_object(mi, "MaterialInstanceConstant", "MI_KineticRifle")
    e = b.add_export("StaticMesh", "SM_Pillar")
    native = S.static_data(b, [m], POS, NRM, UV, IDX, [(0, 0, 2), (0, 6, 2)])[6:]  # after the empty header
    slots = struct.pack("<i", 1) + _header([(0, True), (1, True)]) + struct.pack("<i", m) + b.fname("Stone") \
        + bytes(24)  # the slot: its material, its name, its UV channel data
    b.set_data(e, _header([(3, True), (7, False), (9, True)]) + struct.pack("<f", 1.5) + slots
               + struct.pack("<i", 0) + native)
    pk["Environments/Biome1/SM_Pillar"] = b
    for path, b in pk.items():
        uasset, uexp, ubulk = b.build()
        out[f"Returnal/Content/{path}.uasset"] = uasset
        out[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            out[f"Returnal/Content/{path}.ubulk"] = ubulk
    return out


def _mappings() -> bytes:
    from test_borderlands_ue5 import _usmap

    return _usmap(
        [("Object", "", []), ("Texture", "Object", []), ("Texture2D", "Texture", []),
         ("SkeletalMesh", "Object", []),
         ("MaterialInterface", "Object", [(0, 1, "SubsurfaceProfile", ("ObjectProperty",))]),
         ("MaterialInstance", "MaterialInterface", [
             (0, 1, "Parent", ("ObjectProperty",)), (1, 1, "bHasStaticPermutationResource", ("BoolProperty",)),
             (2, 1, "ScalarParameterValues", ("ArrayProperty", ("StructProperty", "ScalarParameterValue"))),
             (3, 1, "TextureParameterValues", ("ArrayProperty", ("StructProperty", "TextureParameterValue")))]),
         ("MaterialInstanceConstant", "MaterialInstance", []),
         ("MaterialParameterInfo", "", [(0, 1, "Name", ("NameProperty",)),
                                        (1, 1, "Association",
                                         ("EnumProperty", ("ByteProperty",), "EMaterialParameterAssociation")),
                                        (2, 1, "Index", ("IntProperty",))]),
         ("TextureParameterValue", "", [(0, 1, "ParameterInfo", ("StructProperty", "MaterialParameterInfo")),
                                        (1, 1, "ParameterValue", ("ObjectProperty",)),
                                        (2, 1, "ExpressionGUID", ("StructProperty", "Guid"))])],
        {"EMaterialParameterAssociation": ["LayerParameter", "BlendParameter", "GlobalParameter"]})


def test_unversioned_packages_open_with_the_type_mappings(tmp_path, monkeypatch):
    from darkstl.errors import FormatError
    from darkstl.importers.borderlands.ue4 import unversioned

    monkeypatch.setattr(unversioned, "_settings_dir", lambda: None)
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    win64 = root / "Returnal" / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    (win64 / "Returnal-Win64-Shipping.exe").write_bytes(b"MZ" + bytes(5000) + "++UE4+Release-4.26".encode("utf-16-le"))
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(_unversioned_files(), version=8, compress=True))
    vfs.forget(root)
    unversioned.forget_mappings()
    assert catalog.engine_version(root) == 426

    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    found = {e.stem: e for e in imp.scan(root, GAME)}
    assert set(found) == {"SK_KineticRifle", "SM_Pillar"}
    assert found["SK_KineticRifle"].category == "Weapons"
    notes = imp.folder_notes(root, list(found.values()))
    assert any(".usmap" in n for n in notes)
    # without the mappings: the geometry, the material slots and the textures the material uses, by their names
    for stem, parts in (("SK_KineticRifle", 1), ("SM_Pillar", 2)):
        m = open_model(found[stem].path)
        assert len(m.build(MeshOptions()).faces) == 4 and len(m.parts) == parts
        mat = m.materials[0]
        assert mat.name == "MI_KineticRifle" and mat.albedo and mat.normal and mat.guessed, mat.details
        assert "parameter BaseColor: T_KineticRifle_A" in mat.details  # its colour map isn't named as one
        assert m.textures[mat.albedo].load()[0, 0, 0] > 240

    (paks / "Mappings.usmap").write_bytes(_mappings())
    vfs.forget(root)
    unversioned.forget_mappings()
    assert not imp.folder_notes(root, list(found.values()))
    m = open_model(found["SK_KineticRifle"].path)
    assert len(m.build(MeshOptions()).faces) == 4
    mat = m.materials[0]
    assert mat.name == "MI_KineticRifle" and mat.albedo and mat.normal
    assert m.textures[mat.albedo].load()[0, 0, 0] > 240
    with pytest.raises(FormatError):  # without the game's release, an unversioned package can't be read
        from darkstl.importers.borderlands.ue4 import asset
        asset.read_package("/Game/X", _unversioned_files()[
            "Returnal/Content/Gameplay/Weapons/Player_KineticRifle/SK_KineticRifle.uasset"])


def test_mappings_named_for_another_game_are_left_alone(tmp_path):
    from darkstl.importers.borderlands.ue4 import usmap

    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "BL4.usmap").write_bytes(_mappings())
    assert usmap.find_mappings(tmp_path, GAME, settings) is None
    (settings / "Returnal.usmap").write_bytes(_mappings())
    assert usmap.find_mappings(tmp_path, GAME, settings) is not None


def test_generic_textures_of_master_materials_are_not_taken_for_a_models_own():
    from darkstl.importers.borderlands.materials import _NAME_RULES
    from darkstl.importers.borderlands.ue4.materials import _shared_texture

    assert _shared_texture("/Game/Materials/_TextureLibrary/Noise/T_GrungeNoise_C")
    assert _shared_texture("/Engine/EngineMaterials/DefaultDiffuse")
    assert not _shared_texture("/Game/Gameplay/Weapons/Player_Pinpointer/Textures/T_Pinpointer_BaseColor")
    roles = {n: next((r for rx, r in _NAME_RULES if rx.search(n)), None)
             for n in ("T_Pinpointer_BaseColor", "T_Pinpointer_BC", "T_Pinpointer_ORM", "T_Pinpointer_Normal")}
    assert roles == {"T_Pinpointer_BaseColor": "albedo", "T_Pinpointer_BC": "albedo",
                     "T_Pinpointer_ORM": "specular", "T_Pinpointer_Normal": "normal"}
