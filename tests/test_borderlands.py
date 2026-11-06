"""The Borderlands (Unreal 3) importer: LZO, packages, meshes, textures, materials, the library scan and the
gun configurations built from Borderlands 2's gestalt meshes and part definitions."""

import shutil
import struct

import numpy as np
import pytest

from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.borderlands import BorderlandsImporter, files
from darkstl.importers.borderlands.formats import lzo, upk
from darkstl.importers.borderlands.formats.props import ArrayValue, object_props
from darkstl.model import MeshOptions

import synth_ue3 as S

DATA = (b"Borderlands " * 20) + bytes(range(64)) + b"claptrap" * 40 + bytes(300) + b"Pandora"
# made with the reference LZO library (lzo1x_1_compress and lzo1x_999_compress)
LZO1 = bytes.fromhex(
    "09426f726465726c616e64732020c32c000046000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f636c617074726170636c617074726170636c61"
    "70747261702000085d000020000400000b0000000000000050616e646f7261110000")
LZO999 = bytes.fromhex(
    "1d426f726465726c616e64732020c32c000036000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f636c6170747261702000181d000020000b0100"
    "5045566f0c4c110000")

RED, BLUE = 0xF800, 0x001F
FLAT_NORMAL = 0x841F  # (0.5, 0.5, 1) in 565: a flat normal map


def test_lzo_matches_the_reference_library():
    assert lzo.decompress(LZO1, len(DATA)) == DATA
    assert lzo.decompress(LZO999, len(DATA)) == DATA
    assert lzo.decompress(S.lzo_literal(b"hello world")) == b"hello world"
    with pytest.raises(FormatError):
        lzo.decompress(LZO1[:40])


def _quads(n, z=0.0, spacing=10.0):
    pos, nrm, uv, idx = [], [], [], []
    for k in range(n):
        base = len(pos)
        x = k * spacing
        pos += [(x, 0, z), (x + 10, 0, z), (x + 10, 0, z + 10), (x, 0, z + 10)]
        nrm += [(0, -1, 0)] * 4  # facing -Y in Unreal: +Y once mirrored
        uv += [(0, 0), (1, 0), (1, 1), (0, 1)]
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, nrm, uv, idx


FRAGMENTS = ["Pistol_Body_Bandit", "Pistol_Body_Bandit_Var1", "Pistol_Barrel_Bandit", "Pistol_Barrel_Jakobs",
             "Pistol_Sight_Bandit", "Pistol_Grip_Bandit"]


def startup_package(compress="lzo") -> bytes:
    b = S.PackageBuilder()
    grp = b.add_export("Package", "Weap_Pistol", b"", 0, export_flags=1)
    dif = b.add_export("Texture2D", "Pistol_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, RED)), grp)
    nrm = b.add_export("Texture2D", "Pistol_Nrm", S.texture_data(
        b, 16, 16, S.bc1_solid(16, 16, FLAT_NORMAL), normal=True, cache="WeaponTextures", cache_offset=64,
        extra_mips=[(8, 8, S.bc1_solid(8, 8, FLAT_NORMAL))]), grp)
    mat = b.add_export("Material", "Master_Gun", S.material_data(b, [dif, nrm]), grp)
    mic = b.add_export("MaterialInstanceConstant", "Mati_Pistol_Bandit",
                       S.mic_data(b, mat, [("p_Diffuse", dif)]), grp)
    pos, nrm_, uv, idx = _quads(len(FRAGMENTS))
    mesh = b.add_export("SkeletalMesh", "Pistol_Gestalt", b"", grp)
    b.set_data(mesh, S.skeletal_data(b, [mic], pos, nrm_, uv, idx, [(0, 0, 2 * len(FRAGMENTS))],
                                     bones=(("Root", -1), ("Barrel", 0))))
    b.add_export("GestaltSkeletalMeshDefinition", "GestaltDef_Pistol",
                 S.gestalt_data(b, mesh, [(f, 0, 6 * i, 2) for i, f in enumerate(FRAGMENTS)]), grp)
    crate = b.add_export("StaticMesh", "Crate", b"", 0)
    p4, n4, uv4, i4 = _quads(1)
    b.set_data(crate, S.static_data(b, p4, n4, uv4, i4, [(mic, 0, 2)]))
    head = b.add_export("SkeletalMesh", "Head_Siren", b"", 0)
    b.set_data(head, S.skeletal_data(b, [mic], p4, n4, uv4, i4, [(0, 0, 2)], half_uvs=False))
    return b.build(compress)


def parts_package(compress="zlib") -> bytes:
    b = S.PackageBuilder()
    top = b.add_export("Package", "GD_Weap_Pistol", b"", 0)
    groups = {g: b.add_export("Package", g, b"", top) for g in ("Body", "Barrel", "Sight", "Grip")}
    b.add_export("WeaponPartDefinition", "Pistol_Body_Bandit",
                 S.part_data(b, "Pistol_Body_Bandit", ["Pistol_Body_Bandit_Var1"]), groups["Body"])
    b.add_export("WeaponPartDefinition", "Pistol_Barrel_Bandit", S.part_data(b, "Pistol_Barrel_Bandit"),
                 groups["Barrel"])
    b.add_export("WeaponPartDefinition", "Pistol_Barrel_Jakobs", S.part_data(b, "Pistol_Barrel_Jakobs"),
                 groups["Barrel"])
    b.add_export("WeaponPartDefinition", "Pistol_Barrel_Jakobs_2", S.part_data(b, "Pistol_Barrel_Jakobs"),
                 groups["Barrel"])  # draws what the other one draws: listed once
    b.add_export("WeaponPartDefinition", "Pistol_Sight_Bandit", S.part_data(b, "Pistol_Sight_Bandit"),
                 groups["Sight"])
    b.add_export("WeaponPartDefinition", "Pistol_Sight_None", S.part_data(b, "Pistol_Sight_Bandit"),
                 groups["Sight"])  # *_None parts draw nothing
    b.add_export("WeaponPartDefinition", "Pistol_Grip_Bandit", S.part_data(b, "Pistol_Grip_Bandit"), groups["Grip"])
    return b.build(compress)


@pytest.fixture
def bl2(tmp_path):
    root = tmp_path / "Borderlands 2"
    cooked = root / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    (root / "Binaries" / "Win32").mkdir(parents=True)
    (root / "Binaries" / "Win32" / "Borderlands2.exe").write_bytes(b"MZ")
    (cooked / "Startup.upk").write_bytes(startup_package())
    (cooked / "GD_Weap_Pistol.upk").write_bytes(parts_package())
    (cooked / "WeaponTextures.tfc").write_bytes(b"\0" * 64 + S.bc1_solid(16, 16, FLAT_NORMAL))
    return root


def test_package_tables_read_the_same_compressed_or_not(tmp_path):
    for mode in (None, "lzo", "zlib"):
        p = tmp_path / f"Startup_{mode}.upk"
        p.write_bytes(startup_package(mode))
        pkg = upk.open_package(p)
        assert pkg.summary.version == 832 and pkg.summary.licensee == 46
        assert bool(pkg.summary.compression) == bool(mode)
        assert [e.path for e in pkg.exports][:3] == ["Weap_Pistol", "Weap_Pistol.Pistol_Dif", "Weap_Pistol.Pistol_Nrm"]
        g = pkg.find_export("Weap_Pistol.GestaltDef_Pistol")
        props = object_props(pkg, pkg.export_data(g))
        infos = props.get("GestaltInfos")
        assert isinstance(infos, ArrayValue)
        parts = infos.structs()[0].get("Parts").structs()
        assert [p.get("SkeletalMeshFragmentName") for p in parts] == FRAGMENTS
        assert upk.is_package_file(p)


def test_not_a_package(tmp_path):
    p = tmp_path / "x.upk"
    p.write_bytes(b"\0" * 100)
    with pytest.raises(FormatError):
        upk.open_package(p)
    assert not upk.is_package_file(p)


def test_game_detection_and_scan(bl2):
    imp = BorderlandsImporter()
    assert files.detect_game(bl2) == files.GAME_BL2
    entries = imp.scan(bl2, files.GAME_BL2)
    by_label = {e.label: e for e in entries}
    assert set(by_label) == {"Pistol_Gestalt", "Crate", "Head_Siren"}
    assert (by_label["Pistol_Gestalt"].category, by_label["Pistol_Gestalt"].group) == ("Weapons", "Pistols")
    assert by_label["Head_Siren"].category == "Faces & Hair"
    assert by_label["Crate"].category == "Objects"
    assert importer_for(by_label["Crate"].path) is not None
    assert isinstance(importer_for(by_label["Crate"].path), BorderlandsImporter)


def test_a_mesh_cooked_into_two_packages_is_listed_once(bl2, tmp_path):
    cooked = bl2 / "WillowGame" / "CookedPCConsole"
    shutil.copy(cooked / "Startup.upk", cooked / "Sanctuary_P.upk")
    imp = BorderlandsImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(bl2, files.GAME_BL2)
    gestalts = [e for e in entries if e.label == "Pistol_Gestalt"]
    assert len(gestalts) == 1
    # Crate and the head aren't forced exports: each package's own copy is its own object
    assert len([e for e in entries if e.label == "Crate"]) == 2
    # the scan was cached: a second scan gives the same
    assert [e.path for e in imp.scan(bl2, files.GAME_BL2)] == [e.path for e in entries]
    assert list((tmp_path / "cache" / "packages").glob("*.json"))


def test_gestalt_mesh_parts_textures_and_frame(bl2):
    imp = BorderlandsImporter()
    entry = next(e for e in imp.scan(bl2, files.GAME_BL2) if e.label == "Pistol_Gestalt")
    m = open_model(entry.path)
    assert m.game == files.GAME_BL2
    assert [p.name for p in m.parts] == FRAGMENTS
    assert all(p.triangles == 2 for p in m.parts)
    full = m.build_mesh()
    assert full.n_faces == 12
    two = m.build_mesh(MeshOptions(parts={0, 3}))
    assert two.n_faces == 4
    # centimetres -> metres, Y mirrored; faces wound to agree with the normals (+Y after mirroring)
    lo, hi = full.bounds()
    assert np.allclose(lo, [0, 0, 0]) and np.allclose(hi, [0.6, 0, 0.1])
    assert (np.einsum("ij,ij->i", full.face_cross(), full.corner_normals.sum(1)) > 0).all()
    assert np.allclose(full.corner_normals[:, :, 1], 1, atol=0.02)
    mat = m.materials[0]
    assert mat.normal_green == "directx"
    assert m.textures[mat.albedo].decode()[0, 0, :3].tolist() == [255, 0, 0]
    normal = m.textures[mat.normal]
    assert normal.decode().shape[:2] == (16, 16)  # the large mip from the texture cache
    assert any("gestalt" in line for line in m.info)


def test_missing_texture_cache_falls_back_to_the_package_mips(bl2):
    (bl2 / "WillowGame" / "CookedPCConsole" / "WeaponTextures.tfc").unlink()
    from darkstl.importers.borderlands.formats import texture as texfmt
    texfmt._CACHE_INDEX.clear()
    entry = next(e for e in BorderlandsImporter().scan(bl2, files.GAME_BL2) if e.label == "Pistol_Gestalt")
    m = open_model(entry.path)
    assert m.textures[m.materials[0].normal].decode().shape[:2] == (8, 8)


def test_static_mesh_and_full_float_skeletal_uvs(bl2):
    entries = {e.label: e for e in BorderlandsImporter().scan(bl2, files.GAME_BL2)}
    crate = open_model(entries["Crate"].path)
    mesh = crate.build_mesh()
    assert mesh.n_faces == 2 and crate.parts[0].triangles == 2
    assert np.allclose(mesh.bounds()[1], [0.1, 0, 0.1])
    head = open_model(entries["Head_Siren"].path)
    h = head.build_mesh()
    assert h.n_faces == 2
    assert sorted(np.unique(h.corner_uvs.reshape(-1, 2), axis=0).tolist()) == [[0, 0], [0, 1], [1, 0], [1, 1]]
    lines = BorderlandsImporter().describe(entries["Crate"].path.parent)
    assert "LZO compressed" in lines[0] and any("StaticMesh" in x for x in lines)


def test_gun_configuration_from_the_part_definitions(bl2):
    from darkstl import configs

    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    found = imp.configurations(bl2, files.GAME_BL2, entries)
    assert len(found) == 1
    c = found[0]
    assert c.id == "gestalt-pistol" and c.group == "Pistols" and not c.guessed
    slots = {s.id: s for s in c.slots}
    assert list(slots) == ["body", "grip", "barrel", "sight"]
    assert slots["body"].required and slots["barrel"].required and not slots["sight"].required
    assert [o.label for o in slots["barrel"].options] == ["Bandit", "Jakobs"]  # the duplicate is listed once
    assert slots["body"].options[0].model.parts == [0, 1]  # the body and its variant fragment
    assert len(slots["sight"].options) == 1  # Pistol_Sight_None draws nothing
    assert c.base.parts == []

    gun = configs.assemble(c, {"body": slots["body"].options[0].id, "barrel": "Pistol_Barrel_Jakobs",
                               "sight": "", "grip": ""}, open_model)
    assert gun.build_mesh().n_faces == 6  # body + variant + Jakobs barrel


def test_configuration_from_fragment_names_alone(bl2):
    (bl2 / "WillowGame" / "CookedPCConsole" / "GD_Weap_Pistol.upk").unlink()
    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    (c,) = imp.configurations(bl2, files.GAME_BL2, entries)
    assert c.guessed
    slots = {s.id: s for s in c.slots}
    assert slots["body"].options[0].model.parts == [0, 1]
    assert len(slots["barrel"].options) == 2


GREEN = 0x07E0


def skins_package() -> bytes:
    """Material parts (skins): one whose material instance is in the same package, one that imports it."""
    b = S.PackageBuilder()
    top = b.add_export("Package", "GD_Weap_Pistol", b"", 0)
    group = b.add_export("Package", "ManufacturerMaterials", b"", top)
    dif = b.add_export("Texture2D", "Bandit_Skin_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, BLUE)), group)
    master = b.add_export("Material", "Master_Gun", S.material_data(b, []), group)
    mic = b.add_export("MaterialInstanceConstant", "Mati_Bandit_1", S.mic_data(b, master, [("p_Diffuse", dif)]), group)
    gun_skins = b.add_import("Core", "Package", 0, "GunSkins")
    jakobs = b.add_import("Engine", "MaterialInstanceConstant", gun_skins, "Mati_Jakobs_1")
    for name, ref in (("Mat_Jakobs_1", jakobs), ("Mat_Bandit_1", mic)):
        b.add_export("WeaponPartDefinition", name,
                     b.props(b.p_enum("PartType", "EWeaponPartType", "WP_Material"), b.p_obj("Material", ref)), group)
    shotgun = b.add_export("Package", "GD_Weap_Shotgun", b"", 0)  # another weapon type's skin: not offered
    b.add_export("WeaponPartDefinition", "Mat_Torgue_1",
                 b.props(b.p_enum("PartType", "EWeaponPartType", "WP_Material"), b.p_obj("Material", mic)), shotgun)
    return b.build("lzo")


def gun_skins_package() -> bytes:
    b = S.PackageBuilder()
    dif = b.add_export("Texture2D", "Jakobs_Skin_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, GREEN)))
    master = b.add_export("Material", "Master_Gun", S.material_data(b, []))
    b.add_export("MaterialInstanceConstant", "Mati_Jakobs_1", S.mic_data(b, master, [("p_Diffuse", dif)]))
    return b.build("lzo")


def test_gun_skins_paint_the_whole_gun(bl2):
    from darkstl import configs

    cooked = bl2 / "WillowGame" / "CookedPCConsole"
    (cooked / "GD_Weap_Skins.upk").write_bytes(skins_package())
    (cooked / "GunSkins.upk").write_bytes(gun_skins_package())
    files._INDEXES.clear()
    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    (c,) = imp.configurations(bl2, files.GAME_BL2, entries)
    skin = next(s for s in c.slots if s.id == "skin")
    assert skin.kind == "material" and skin.required
    assert [o.label for o in skin.options] == ["Bandit 1", "Jakobs 1"]  # the shotgun's skin isn't offered
    assert skin.default == "Mat_Bandit_1"  # the default body's maker
    assert skin.options[0].model.parts == [] and skin.options[0].model.variant.startswith("skin:GD_Weap_Skins:")
    # the slot's kind survives the list format
    (again,) = configs.parse(configs.dump([c], c.game), c.game)
    assert next(s for s in again.slots if s.id == "skin").kind == "material"
    assert "kind" not in configs.dump([c], c.game)["configurations"][0]["slots"][0]

    def colour(gun):
        return {tuple(gun.textures[mat.albedo].decode()[0, 0, :3].tolist()) for mat in gun.materials}

    gun = configs.assemble(c, None, open_model)
    assert gun.build_mesh().n_faces > 0
    assert colour(gun) == {(0, 0, 255)}  # every part painted with the Bandit skin
    assert all("(Bandit 1)" in mat.name for mat in gun.materials)
    jakobs = configs.assemble(c, {"skin": "Mat_Jakobs_1"}, open_model)
    assert colour(jakobs) == {(0, 255, 0)}  # its material instance read from the package it's imported from
    assert any("Skin: Jakobs 1" in line for line in jakobs.info)


def test_skins_from_a_dlc(bl2, tmp_path):
    """A DLC's packages (``<game>/DLC/<name>/...``) belong to the game: their imports are looked up in the base
    game's packages, and a skin remembers its package file, wherever the gun's meshes are."""
    from darkstl import configs

    dlc = bl2 / "DLC" / "Aster" / "WillowGame" / "CookedPCConsole"
    dlc.mkdir(parents=True)
    (dlc / "Aster_Startup_SF.upk").write_bytes(skins_package())
    (bl2 / "WillowGame" / "CookedPCConsole" / "GunSkins.upk").write_bytes(gun_skins_package())
    assert files.game_root(dlc / "Aster_Startup_SF.upk") == bl2
    assert files.game_root(bl2 / "WillowGame" / "DLC" / "Iris" / "CookedPCConsole" / "X.upk") == bl2
    assert files.game_root(bl2 / "DLC" / "Iris" / "CookedPCConsole" / "X.upk") == bl2
    files._INDEXES.clear()
    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    (c,) = imp.configurations(bl2, files.GAME_BL2, entries)
    skin = next(s for s in c.slots if s.id == "skin")
    jakobs = next(o for o in skin.options if o.id == "Mat_Jakobs_1")
    assert jakobs.model.variant.endswith("|" + str(dlc / "Aster_Startup_SF.upk"))
    gun = configs.assemble(c, {"skin": "Mat_Jakobs_1"}, open_model)  # its material instance is in GunSkins.upk
    assert {tuple(gun.textures[m.albedo].decode()[0, 0, :3].tolist()) for m in gun.materials} == {(0, 255, 0)}
    # the gun's meshes in a folder of their own: the skin is still opened from its file
    elsewhere = tmp_path / "copy" / "WillowGame" / "CookedPCConsole"
    elsewhere.mkdir(parents=True)
    startup = elsewhere / "Startup.upk"
    shutil.copy(bl2 / "WillowGame" / "CookedPCConsole" / "Startup.upk", startup)
    paint = open_model(jakobs.model.file.replace(str(bl2 / "WillowGame" / "CookedPCConsole" / "Startup.upk"),
                                                 str(startup)), jakobs.model.variant)
    assert paint.textures and paint.materials[0].albedo


def test_a_skin_cooked_into_some_other_package_is_found(bl2):
    """The skin's material instance is imported from a package that isn't a file of its own; a copy of it was
    cooked (as a forced export) into some other package, and its texture into a third."""
    from darkstl import configs
    from darkstl.importers.borderlands import catalog

    cooked = bl2 / "WillowGame" / "CookedPCConsole"
    b = S.PackageBuilder()
    top = b.add_export("Package", "GD_Weap_Pistol", b"", 0)
    group = b.add_export("Package", "ManufacturerMaterials", b"", top)
    common = b.add_import("Core", "Package", 0, "Common_GunMaterials")
    mats = b.add_import("Core", "Package", common, "Materials")
    mic = b.add_import("Engine", "MaterialInstanceConstant", mats, "Mati_Dahl_4_Emerald")
    b.add_export("WeaponPartDefinition", "Mat_Dahl_4_Emerald",
                 b.props(b.p_enum("PartType", "EWeaponPartType", "WP_Material"), b.p_obj("Material", mic)), group)
    (cooked / "GD_Weap_Skins.upk").write_bytes(b.build("lzo"))

    b = S.PackageBuilder()  # a level that uses the skin: the instance cooked in, its texture imported
    forced = b.add_export("Package", "Common_GunMaterials", b"", 0, export_flags=0x01)
    group = b.add_export("Package", "Materials", b"", forced)
    textures = b.add_import("Core", "Package", 0, "Common_GunTextures")
    dif = b.add_import("Engine", "Texture2D", textures, "Dahl_Emerald_Dif")
    master = b.add_export("Material", "Master_Gun", S.material_data(b, []), group)
    b.add_export("MaterialInstanceConstant", "Mati_Dahl_4_Emerald", S.mic_data(b, master, [("p_Diffuse", dif)]),
                 group)
    (cooked / "Dam_P.upk").write_bytes(b.build("lzo"))

    b = S.PackageBuilder()  # another level with the texture
    forced = b.add_export("Package", "Common_GunTextures", b"", 0, export_flags=0x01)
    b.add_export("Texture2D", "Dahl_Emerald_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, GREEN)), forced)
    (cooked / "Fridge_P.upk").write_bytes(b.build("lzo"))

    files._INDEXES.clear()
    catalog.forget_object_index()
    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    (c,) = imp.configurations(bl2, files.GAME_BL2, entries)
    skin = next(s for s in c.slots if s.id == "skin")
    assert [o.label for o in skin.options] == ["Dahl 4 Emerald"]
    gun = configs.assemble(c, {"skin": "Mat_Dahl_4_Emerald"}, open_model)
    colours = {tuple(gun.textures[mat.albedo].decode()[0, 0, :3].tolist()) for mat in gun.materials}
    assert colours == {(0, 255, 0)}
    assert all(any("Dahl_Emerald_Dif" in d for d in mat.details) for mat in gun.materials)


def test_copies_without_a_forced_export_flag_are_found(bl2):
    """Real cooked packages may keep another package's objects under an outer that isn't flagged as that
    package; and a texture may be kept under other groups than its path says: found by name and class."""
    from darkstl import configs
    from darkstl.importers.borderlands import catalog

    cooked = bl2 / "WillowGame" / "CookedPCConsole"
    b = S.PackageBuilder()
    top = b.add_export("Package", "GD_Orchid_RaidWeapons", b"", 0)
    group = b.add_export("Package", "ManufacturerMaterials", b"", top)
    orchid = b.add_export("Package", "Orchid_GunMaterials", b"", 0, export_flags=0x01)
    mats = b.add_export("Package", "Pistol", b"", b.add_export("Package", "Materials", b"", orchid))
    common = b.add_import("Core", "Package", 0, "Common_GunMaterials")
    cmats = b.add_import("Core", "Package", common, "Materials")
    parent = b.add_import("Engine", "MaterialInstanceConstant", cmats, "Mati_DahlEpicAR")
    env = b.add_import("Core", "Package", common, "Env")
    glossy = b.add_import("Engine", "Texture2D", env, "GlossyC")
    mic = b.add_export("MaterialInstanceConstant", "Mati_DahlCommonAR_Seraphim",
                       S.mic_data(b, parent, [("P_SimpleReflect", glossy)]), mats)
    b.add_export("WeaponPartDefinition", "Mat_Dahl_1_Seraphim",
                 b.props(b.p_enum("PartType", "EWeaponPartType", "WP_Material"), b.p_obj("Material", mic)), group)
    dlc = bl2 / "DLC" / "Orchid" / "WillowGame" / "CookedPCConsole"
    dlc.mkdir(parents=True)
    (dlc / "Orchid_Startup_SF.upk").write_bytes(b.build("lzo"))

    b = S.PackageBuilder()  # the parent cooked into a level under an outer without the forced export flag
    outer = b.add_export("Package", "Common_GunMaterials", b"", 0)
    group = b.add_export("Package", "Materials", b"", outer)
    textures = b.add_export("Package", "Textures", b"", outer)  # not where the parent's import says
    dif = b.add_export("Texture2D", "Dahl_Epic_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, GREEN)), textures)
    b.add_export("Texture2D", "GlossyC", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, BLUE)), textures)
    master = b.add_export("Material", "Master_Gun", S.material_data(b, []), group)
    b.add_export("MaterialInstanceConstant", "Mati_DahlEpicAR", S.mic_data(b, master, [("p_Diffuse", dif)]), group)
    (cooked / "Dam_P.upk").write_bytes(b.build("lzo"))

    files._INDEXES.clear()
    catalog.forget_object_index()
    imp = BorderlandsImporter()
    entries = imp.scan(bl2, files.GAME_BL2)
    (c,) = imp.configurations(bl2, files.GAME_BL2, entries)
    gun = configs.assemble(c, {"skin": "Mat_Dahl_1_Seraphim"}, open_model)
    assert not any("not found" in d for m in gun.materials for d in m.details), gun.materials[0].details
    assert {tuple(gun.textures[m.albedo].decode()[0, 0, :3].tolist()) for m in gun.materials} == {(0, 255, 0)}


def test_texture_formats_and_bad_data(tmp_path):
    b = S.PackageBuilder()
    raw = bytes([10, 20, 30, 255]) * 16
    t = b.add_export("Texture2D", "Argb", S.texture_data(b, 4, 4, raw, fmt="PF_A8R8G8B8"))
    g = b.add_export("Texture2D", "Gray", S.texture_data(b, 4, 4, bytes(range(16)), fmt="PF_G8"))
    p = tmp_path / "T.upk"
    p.write_bytes(b.build())
    from darkstl.importers.borderlands.formats.texture import read_texture
    pkg = upk.open_package(p)
    argb = read_texture(pkg, pkg.exports[t - 1]).decode()
    assert argb[0, 0].tolist() == [30, 20, 10, 255]  # stored B, G, R, A
    gray = read_texture(pkg, pkg.exports[g - 1]).decode()
    assert gray[0, 1, 0] == 1
    assert struct.calcsize("<I") == 4


def test_the_shared_gun_mesh_points_to_its_configurable_gun(bl2, tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.service import Service
    from test_app import wait

    cooked = bl2 / "WillowGame" / "CookedPCConsole"
    (cooked / "GD_Weap_Skins.upk").write_bytes(skins_package())
    (cooked / "GunSkins.upk").write_bytes(gun_skins_package())
    files._INDEXES.clear()
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    app = Service(Config.load(), tmp_path / "home" / "cache")
    wait(app, app.call("folder_add", {"path": str(bl2), "game": files.GAME_BL2})["job"])
    items = app.call("models_query", {"game": files.GAME_BL2, "category": "Weapons"})["items"]
    gun = next(i for i in items if i.get("configurable"))
    mesh = next(i for i in items if i["label"] == "Pistol_Gestalt")
    opened = app.call("model_open", {"id": mesh["id"]})
    assert opened["configurable_as"] == [{"id": gun["id"], "label": "Pistol (any parts)", "slots": 5}]
    assert any("Pistol (any parts)" in n for n in opened["notes"])
    built = app.call("model_open", {"id": gun["id"], "choices": {"skin": "Mat_Jakobs_1"}})
    assert built["configurable_as"] == [] and built["configuration"]["chosen"]["skin"] == "Mat_Jakobs_1"
    assert next(s for s in built["configuration"]["slots"] if s["id"] == "skin")["kind"] == "material"


def ar_parts_package() -> bytes:
    """Assault rifle parts as meshes of their own, as Borderlands 2 ships them, and no gestalt definition."""
    b = S.PackageBuilder()
    grp = b.add_export("Package", "Weap_AssaultRifle", b"", 0, export_flags=1)
    dif = b.add_export("Texture2D", "AR_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, RED)), grp)
    mat = b.add_export("Material", "Master_Gun", S.material_data(b, [dif]), grp)
    for i, name in enumerate(("AR_Body_Dahl", "AR_Body_Jakobs", "AR_Barrel_Dahl", "AR_Grip_Dahl", "AR_Thing")):
        pos, nrm, uv, idx = _quads(1)
        pos = [(x + 20 * i, y, z) for x, y, z in pos]
        mesh = b.add_export("SkeletalMesh", name, b"", grp)
        b.set_data(mesh, S.skeletal_data(b, [mat], pos, nrm, uv, idx, [(0, 0, 2)]))
    return b.build("lzo")


def ar_part_definitions() -> bytes:
    b = S.PackageBuilder()
    top = b.add_export("Package", "GD_Weap_AssaultRifle", b"", 0)
    for group, names in (("Body", ("AR_Body_Dahl", "AR_Body_Jakobs")), ("Barrel", ("AR_Barrel_Dahl",)),
                         ("Grip", ("AR_Grip_Dahl",))):
        g = b.add_export("Package", group, b"", top)
        for n in names:
            b.add_export("WeaponPartDefinition", n, S.part_data(b, n), g)
    mats = b.add_export("Package", "ManufacturerMaterials", b"", top)
    skin = b.add_export("Texture2D", "Dahl_Skin_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, BLUE)), mats)
    master = b.add_export("Material", "Master_Gun", S.material_data(b, []), mats)
    mic = b.add_export("MaterialInstanceConstant", "Mati_Dahl_1", S.mic_data(b, master, [("p_Diffuse", skin)]), mats)
    b.add_export("WeaponPartDefinition", "Mat_Dahl_1",
                 b.props(b.p_enum("PartType", "EWeaponPartType", "WP_Material"), b.p_obj("Material", mic)), mats)
    return b.build("zlib")


@pytest.fixture
def bl2_parts(tmp_path):
    root = tmp_path / "Borderlands 2"
    cooked = root / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    (root / "Binaries" / "Win32").mkdir(parents=True)
    (root / "Binaries" / "Win32" / "Borderlands2.exe").write_bytes(b"MZ")
    (cooked / "Weap_AssaultRifle.upk").write_bytes(ar_parts_package())
    (cooked / "GD_Weap_AssaultRifle.upk").write_bytes(ar_part_definitions())
    files._INDEXES.clear()
    return root


def test_guns_from_part_meshes_of_their_own(bl2_parts):
    from darkstl import configs

    imp = BorderlandsImporter()
    entries = imp.scan(bl2_parts, files.GAME_BL2)
    assert {e.group for e in entries if e.stem.startswith("AR_")} == {"Assault rifles"}
    (c,) = imp.configurations(bl2_parts, files.GAME_BL2, entries)
    assert c.id == "parts-assaultrifle" and c.label == "Assault rifle (any parts)" and not c.guessed
    assert c.group == "Assault rifles" and c.base.parts == [] and c.base.file.endswith("AR_Body_Dahl.SkeletalMesh")
    slots = {s.id: s for s in c.slots}
    assert list(slots) == ["body", "grip", "barrel", "skin"]  # AR_Thing is in no slot
    assert [o.label for o in slots["body"].options] == ["Dahl", "Jakobs"] and slots["body"].required
    gun = configs.assemble(c, {"body": "AR_Body_Jakobs", "grip": "AR_Grip_Dahl"}, open_model)
    mesh = gun.build_mesh()
    assert mesh.n_faces == 6  # Jakobs body, Dahl barrel and grip; the base adds nothing
    assert {tuple(gun.textures[m.albedo].decode()[0, 0, :3].tolist()) for m in gun.materials} == {(0, 0, 255)}
    # without the part definitions the slots come from the names
    (bl2_parts / "WillowGame" / "CookedPCConsole" / "GD_Weap_AssaultRifle.upk").unlink()
    (c,) = imp.configurations(bl2_parts, files.GAME_BL2, entries)
    assert c.guessed and [s.id for s in c.slots] == ["body", "grip", "barrel"]


def test_every_part_of_a_gun_points_to_it(bl2_parts, tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.service import Service
    from test_app import wait

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    app = Service(Config.load(), tmp_path / "home" / "cache")
    wait(app, app.call("folder_add", {"path": str(bl2_parts), "game": files.GAME_BL2})["job"])
    items = app.call("models_query", {"game": files.GAME_BL2, "category": "Weapons"})["items"]
    gun = next(i for i in items if i.get("configurable"))
    barrel = next(i for i in items if i["label"] == "AR_Barrel_Dahl")
    thing = next(i for i in items if i["label"] == "AR_Thing")
    assert app.call("model_open", {"id": barrel["id"]})["configurable_as"][0]["id"] == gun["id"]
    assert app.call("model_open", {"id": thing["id"]})["configurable_as"] == []
