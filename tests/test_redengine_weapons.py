"""Cyberpunk weapons as configurable models (from their .app files) and multilayered materials blended into
plain colour and normal maps."""

from pathlib import Path

import numpy as np
import pytest
import synth_red as S
from test_redengine import JACKET, METAL_MT, RIFLE, STEEL_MI, rifle_files, write_loose

from darkstl.errors import FormatError
from darkstl.importers import open_model
from darkstl.importers.redengine import RedEngineImporter, catalog, configurations, depot, entities
from darkstl.importers.redengine.formats import cr2w
from darkstl.importers.redengine.formats.mlmask import read_mlmask
from darkstl.importers.redengine.formats.package import HashRef, read_package
from darkstl.library import Library

RIFLE_DIR = r"base\weapons\firearms\rifle_assault\w_rifle_a"
MAG_STD = RIFLE_DIR + r"\w_rifle_a_mag_std.mesh"
MAG_DRUM = RIFLE_DIR + r"\w_rifle_a_mag_drum.mesh"
STOCK = RIFLE_DIR + r"\w_rifle_a_stock.mesh"
DECAL = RIFLE_DIR + r"\w_rifle_a_decal.mesh"
RIFLE_APP = RIFLE_DIR + r"\w_rifle_a.app"
RIFLE_ENT = RIFLE_DIR + r"\w_rifle_a.ent"
SCOPE = r"base\weapons\attachments\scope_short\w_att_scope_short.mesh"
SCOPE_APP = r"base\weapons\attachments\scope_short\w_att_scope_short.app"
MLSETUP = RIFLE_DIR + r"\w_rifle_a.mlsetup"
MLMASK = RIFLE_DIR + r"\w_rifle_a.mlmask"
RED_T = r"base\surfaces\microblends\red.mltemplate"
BLUE_T = r"base\surfaces\microblends\blue.mltemplate"
RED_D = r"base\surfaces\microblends\red_d.xbm"
BLUE_D = r"base\surfaces\microblends\blue_d.xbm"
FLAT_N = r"base\surfaces\microblends\flat_n.xbm"


def weapon_files() -> dict[str, bytes]:
    """The rifle of test_redengine with parts that vary between appearances, its .app and .ent, and a scope."""
    files = rifle_files()
    pos, nrm, uv, idx = S.grid_plane(2, 0.2)
    for path in (MAG_STD, MAG_DRUM, STOCK, DECAL, SCOPE):
        files[path] = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("steel", STEEL_MI)])
    frank = [S.mesh_component("body", RIFLE), S.mesh_component("mag", MAG_STD, "default", (0.0, 0.0, -0.1)),
             S.mesh_component("stock", STOCK, "default", (0.0, -0.3, 0.0))]
    military = [S.mesh_component("body", RIFLE, "rusty"), S.mesh_component("mag", MAG_DRUM, "default", (0.0, 0.0, -0.12)),
                S.mesh_component("stock", STOCK, "default", (0.0, -0.3, 0.0)),
                S.mesh_component("decal", DECAL, "default", (0.0, 0.1, 0.0))]
    files[RIFLE_APP] = S.build_app({"frank": frank, "military1": military}, RIFLE_ENT)
    files[RIFLE_ENT] = S.build_ent([S.slot_component("attachments", {"scope": ("", (0.0, 0.05, 0.1))})],
                                   [("frank", RIFLE_APP, "frank"), ("military1", RIFLE_APP, "military1")])
    files[SCOPE_APP] = S.build_app({"default": [S.mesh_component("scope", SCOPE)]})
    return files


def archive_game(root: Path, files: dict[str, bytes]) -> Path:
    (root / "bin" / "x64").mkdir(parents=True)
    (root / "bin" / "x64" / "Cyberpunk2077.exe").write_bytes(b"")
    content = root / "archive" / "pc" / "content"
    content.mkdir(parents=True)
    main = {p: cr2w.read_cr2w(d).objects_end for p, d in files.items()}
    textures = {k: v for k, v in files.items() if k.endswith(".xbm")}
    rest = {k: v for k, v in files.items() if not k.endswith(".xbm")}
    (content / "basegame_4_gamedata.archive").write_bytes(S.build_archive(rest, main))
    (content / "basegame_3_textures.archive").write_bytes(S.build_archive(textures, main))
    return root


# ---------------------------------------------------------------------------- packages and appearances


def test_package_round_trip():
    comps = [S.mesh_component("body", RIFLE), S.mesh_component("mag", MAG_STD, "neon", (0.1, 0.2, 0.3))]
    data = S.build_package(comps, True, {1: ("body", "mag_slot")})
    pkg = read_package(data, imports_as_hash=True)
    assert pkg.version == 3 and [c.cls for c in pkg.chunks] == ["entMeshComponent", "entMeshComponent",
                                                                 "entHardTransformBinding"]
    assert isinstance(pkg.imports[0], HashRef) and pkg.imports[0].hash == depot.arc.path_hash(RIFLE)
    with pytest.raises(FormatError, match="too short"):
        read_package(data[:10])


def test_appearances_list_their_components():
    comps = [S.mesh_component("body", RIFLE), S.mesh_component("mag", MAG_STD, "neon", (0.1, 0.2, 0.3))]
    f = cr2w.read_cr2w(S.build_app({"frank": comps}, RIFLE_ENT, {"frank": {1: ("body", "mag_slot")}}))
    app = entities.read_app(f)
    assert app.base_entity == RIFLE_ENT and not app.partial
    (a,) = app.appearances
    assert a.name == "frank" and [m.name for m in a.meshes] == ["body", "mag"]
    mag = a.meshes[1]
    assert mag.mesh == depot.arc.path_hash(MAG_STD) and mag.mesh_stem == f"{mag.mesh:016x}"
    assert mag.appearance == "neon" and mag.bind_name == "body" and mag.slot_name == "mag_slot"
    assert np.allclose(mag.transform[:3, 3], (0.1, 0.2, 0.3), atol=1e-4)
    assert [i.path for i in f.imports] == [RIFLE, MAG_STD, RIFLE_ENT]  # the meshes are the file's imports too

    ent = cr2w.read_cr2w(S.build_ent([S.slot_component("attachments", {"scope": ("", (0.0, 0.05, 0.1))})],
                                     [("frank", RIFLE_APP, "frank")]))
    meshes, slots, apps = entities.read_entity(ent)
    assert meshes == [] and apps == [("frank", RIFLE_APP, "frank")]
    (slot,) = slots
    assert slot.name == "scope" and slot.component == "attachments"
    assert np.allclose(slot.matrix()[:3, 3], (0.0, 0.05, 0.1))


# ---------------------------------------------------------------------------- weapon configurations


def test_weapons_are_configurable_from_their_appearance_files(tmp_path):
    game = archive_game(tmp_path / "Cyberpunk 2077", weapon_files())
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game, "Cyberpunk 2077", None)
    assert catalog.weapon_apps(game / "archive" / "pc" / "content" / "basegame_4_gamedata.archive", imp.cache)
    (config,) = imp.configurations(game, "Cyberpunk 2077", entries)
    assert config.id == "app:w_rifle_a" and config.label == "w_rifle_a (configurable)"
    assert config.source == configurations.SOURCE and config.category == "Weapons"
    rifle = next(e for e in entries if e.stem == "w_rifle_a")
    assert config.base.file == str(rifle.path)  # the archive member
    assert config.base.variant == "default"
    slots = {s.id: s for s in config.slots}
    assert list(slots) == ["finish", "mag", "stock", "decal", "attach_scope"]
    finish = slots["finish"]
    assert finish.kind == "finish" and finish.required and finish.default == "default"
    assert [(o.id, o.model.file, o.model.variant) for o in finish.options] == [
        ("default", str(rifle.path), "default"), ("rusty", str(rifle.path), "rusty")]
    mag = slots["mag"]
    assert mag.required and mag.default == "w_rifle_a_mag_std"
    assert [(o.id, o.label) for o in mag.options] == [("w_rifle_a_mag_std", "Mag std"), ("w_rifle_a_mag_drum", "Mag drum")]
    drum = mag.options[1]
    assert str(drum.model.file).endswith(".mesh") and depot.is_archive_member(Path(drum.model.file))
    assert np.allclose(np.array(drum.attach.matrix).reshape(4, 4)[:3, 3], (0.0, 0.0, -0.12), atol=1e-4)
    assert slots["stock"].required and not slots["decal"].required
    assert config.presets == {
        "frank": {"finish": "default", "mag": "w_rifle_a_mag_std", "stock": "w_rifle_a_stock", "decal": "",
                  "attach_scope": ""},
        "military1": {"finish": "rusty", "mag": "w_rifle_a_mag_drum", "stock": "w_rifle_a_stock",
                      "decal": "w_rifle_a_decal", "attach_scope": ""},
    }
    scope = slots["attach_scope"]
    assert scope.label == "Scope (on scope)" and not scope.required
    (opt,) = scope.options
    assert opt.id == "w_att_scope_short" and opt.label == "Scope short"
    assert np.allclose(np.array(opt.attach.matrix).reshape(4, 4)[:3, 3], (0.0, 0.05, 0.1), atol=1e-4)

    lib = Library()
    lib.add_root(game, "Cyberpunk 2077")
    listed = [e for e in lib.configurations if e.config is not None and e.config.id == "app:w_rifle_a"]
    assert len(listed) == 1 and listed[0].category == "Weapons"


def test_loose_appearance_files_count_too(tmp_path):
    root = tmp_path / "exported"
    write_loose(root, weapon_files())
    imp = RedEngineImporter()
    entries = imp.scan(root, "Cyberpunk 2077", None)
    found = imp.configurations(root, "Cyberpunk 2077", entries)
    assert [c.id for c in found] == ["app:w_rifle_a"]
    assert found[0].base.file == str(root.joinpath(*RIFLE.split("\\")))
    assert [s.id for s in found[0].slots] == ["finish", "mag", "stock", "decal", "attach_scope"]


def test_weapon_names_share_their_prefix():
    assert configurations._weapon_name(["w_rifle_a", "w_rifle_a_mag", "w_rifle_a_stock"]) == "w_rifle_a"
    assert configurations._weapon_name(["abc", "xyz"]) == "abc"
    assert configurations._weapon_name([]) == ""


# ---------------------------------------------------------------------------- multilayer masks and blending


def test_mlmask_layers_round_trip():
    l0 = np.zeros((20, 12), np.uint8)
    l0[:, :6] = 255
    l1 = np.zeros((20, 12), np.uint8)
    l1[10:, 6:] = 128
    l1[3, 2] = 7
    mask = read_mlmask(cr2w.read_cr2w(S.build_mlmask([l0, l1])))
    assert (mask.width, mask.height, mask.layers, mask.tile_size) == (12, 20, 2, 6)
    assert np.array_equal(mask.layer(0), l0)
    assert np.array_equal(mask.layer(1), l1)  # BC4 keeps a block's range ends; 0, 7 and 128 never share one
    with pytest.raises(FormatError, match="2 layers"):
        mask.layer(2)


def multilayer_files() -> dict[str, bytes]:
    files = rifle_files()
    pos, nrm, uv, idx = S.grid_plane(4, 1.0)
    local = S.material_instance(METAL_MT, {"MultilayerSetup": MLSETUP, "MultilayerMask": MLMASK})
    files[RIFLE] = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("steel", local)])
    del files[JACKET]
    red = np.zeros((4, 4, 4), np.uint8)
    red[..., 0] = 255
    red[..., 3] = 255
    blue = np.zeros((4, 4, 4), np.uint8)
    blue[..., 2] = 255
    blue[..., 3] = 255
    flat = np.full((4, 4, 4), 128, np.uint8)
    flat[..., 2] = 255
    files[RED_D], files[BLUE_D], files[FLAT_N] = S.rgba_xbm(red), S.rgba_xbm(blue), S.rgba_xbm(flat)
    files[RED_T] = S.build_mltemplate(RED_D, FLAT_N)
    files[BLUE_T] = S.build_mltemplate(BLUE_D, FLAT_N, color_scales={"dark": (0.25, 0.25, 0.25)})
    files[MLSETUP] = S.build_mlsetup([{"material": RED_T}, {"material": BLUE_T, "colorScale": "dark"}])
    cover = np.zeros((16, 16), np.uint8)
    cover[:, 8:] = 255  # the blue layer covers the right half
    files[MLMASK] = S.build_mlmask([np.full((16, 16), 255, np.uint8), cover])
    return files


def test_multilayered_materials_are_blended(tmp_path):
    root = tmp_path / "exported"
    write_loose(root, multilayer_files())
    model = open_model(root.joinpath(*RIFLE.split("\\")))
    (mat,) = model.materials
    assert mat.albedo and mat.albedo.endswith("#color") and mat.normal and mat.normal.endswith("#normal")
    assert any("blended here" in n for n in model.notes)
    colour = model.textures[mat.albedo]
    assert "blended, 2 layers" in colour.describe()
    rgba = colour.decode()
    assert rgba.shape == (2048, 2048, 4)
    left, right = rgba[1024, 256], rgba[1024, 1792]
    assert left[0] == 255 and left[2] == 0  # the red base layer
    assert right[0] == 0 and right[2] < 255 and right[2] > 100  # blue, darkened by its colour scale override
    normal = model.textures[mat.normal].decode()
    assert normal.shape == (2048, 2048, 4)
    assert abs(int(normal[100, 100, 0]) - 128) <= 1 and normal[100, 100, 2] >= 254


def test_a_setup_without_its_mask_still_blends(tmp_path):
    root = tmp_path / "exported"
    files = multilayer_files()
    del files[MLMASK]
    write_loose(root, files)
    model = open_model(root.joinpath(*RIFLE.split("\\")))
    (mat,) = model.materials
    rgba = model.textures[mat.albedo].decode()
    assert rgba[5, 5, 2] > 0 and rgba[5, 5, 0] == 0  # the blue layer covers everything at full opacity
    assert any("blended by their opacity" in n for n in model.notes), model.notes


# ---------------------------------------------------------------------------- without appearance files


def test_weapons_named_the_games_way_are_guessed_from_one_archive(tmp_path):
    """Cyberpunk names its meshes w_<type>__<name>__<part><n>; all of them sit in one archive."""
    from darkstl import configs

    pos, nrm, uv, idx = S.grid_plane(2, 0.2)
    mesh = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("steel", STEEL_MI)])
    d = r"base\weapons\firearms\revolver\darra_nova"
    names = ["w_revolver__darra_nova__base1_01", "w_revolver__darra_nova__base2", "w_revolver__darra_nova__mag1",
             "w_revolver__darra_nova__mag2", "w_revolver__darra_nova__scope1", "w_revolver__darra_nova__grip1",
             "w_rifle_a__midnight__base1", "w_rifle_a__midnight__stock1"]
    files = {f"{d}\\{n}.mesh": mesh for n in names}
    files[r"base\weapons\w_all.ent"] = S.build_ent([], meshes=list(files))
    game = archive_game(tmp_path / "Cyberpunk 2077", files)
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game, "Cyberpunk 2077", None)
    assert len(entries) == 8 and all(e.category == "Weapons" for e in entries)
    found = imp.configurations(game, "Cyberpunk 2077", entries)
    by_id = {c.id: c for c in found}
    assert sorted(by_id) == ["auto:w_revolver__darra_nova__base1_01", "auto:w_revolver__darra_nova__base2",
                             "auto:w_rifle_a__midnight__base1"]
    c = by_id["auto:w_revolver__darra_nova__base1_01"]  # the game's own numbering: base1_01
    assert c.guessed and c.base.file.endswith(".mesh")
    assert {s.id: [o.id for o in s.options] for s in c.slots} == {
        "magazine": ["w_revolver__darra_nova__mag1", "w_revolver__darra_nova__mag2"],
        "sight": ["w_revolver__darra_nova__scope1"], "grip": ["w_revolver__darra_nova__grip1"]}
    assert {s.id for s in by_id["auto:w_revolver__darra_nova__base2"].slots} == {"magazine", "sight", "grip"}
    assert [s.id for s in by_id["auto:w_rifle_a__midnight__base1"].slots] == ["stock"]
    assert any("No weapon appearance files" in n for n in imp.folder_notes(game, entries))
    assert configs.from_folders(entries, "Cyberpunk 2077", max_files=1) == []


def test_folder_notes_report_the_appearance_files(tmp_path):
    game = archive_game(tmp_path / "Cyberpunk 2077", weapon_files())
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game, "Cyberpunk 2077", None)
    imp.configurations(game, "Cyberpunk 2077", entries)
    (note,) = [n for n in imp.folder_notes(game, entries) if "appearance files read" in n]
    assert note.startswith("2 weapon appearance files read: 1 weapons and 1 attachments named models of this folder, "
                           "1 became configurable")


def test_collect_takes_the_weapons_appearance_files_along(tmp_path):
    game = archive_game(tmp_path / "Cyberpunk 2077", weapon_files())
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game, "Cyberpunk 2077", None)
    rifle = next(e for e in entries if e.stem == "w_rifle_a")
    related = imp.related_files(rifle.path)
    saved = {r.save_as for r in related}
    assert RIFLE_APP.replace("\\", "/") in saved and RIFLE_ENT.replace("\\", "/") in saved
    app = next(r for r in related if r.save_as.endswith(".app"))
    assert app.kind == "other" and app.extract is not None
    assert entities.read_app(cr2w.read_cr2w(app.extract())).appearances[0].name == "frank"
    mag = next(e for e in entries if e.stem == "w_rifle_a_mag_std")
    assert any(r.save_as.endswith(".app") for r in imp.related_files(mag.path))  # a part brings its weapon's too
    assert not any(r.save_as.endswith(".app") for r in imp.related_files(rifle.path, textures=False))


# ---------------------------------------------------------------------------- appearance names and layer sampling


def test_an_unnamed_first_appearance_is_shown_as_default(tmp_path):
    """Many game meshes leave their first appearance unnamed; the app lists it as "default"."""
    pos, nrm, uv, idx = S.grid_plane(2, 0.2)
    mesh = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("steel", STEEL_MI), ("rust", STEEL_MI)],
                        appearances=[("", ["steel"]), ("military1", ["rust"])])
    files = rifle_files()
    files[RIFLE] = mesh
    root = tmp_path / "exported"
    write_loose(root, files)
    path = root.joinpath(*RIFLE.split("\\"))
    imp = RedEngineImporter()
    assert imp.variants(path) == ["default", "military1"]
    model = open_model(path)
    assert model.variant == "default" and model.variants == ["default", "military1"]
    assert model.materials[model.parts[0].material].name == "steel"
    model = open_model(path, variant="military1")
    assert model.variant == "military1"
    assert model.materials[model.parts[0].material].name == "rust"
    assert open_model(path, variant="default").variant == "default"


def test_finely_tiled_layers_are_averaged_not_sparkled():
    from darkstl.importers.redengine import multilayer

    # a 64x64 checkerboard tiled 32 times across a 64-texel output: each output texel covers 32 texture texels
    tex = np.zeros((64, 64, 1), np.uint8)
    tex[::2, ::2] = 255
    tex[1::2, 1::2] = 255
    rows = np.arange(64)
    out = multilayer._sample(tex, rows, 64, 64, 32.0, (0.0, 0.0))
    assert out.shape == (64, 64, 1)
    assert abs(int(out.mean()) - 128) <= 2 and out.max() - out.min() <= 2  # the checkerboard averages to grey
    # untiled, the same texture samples texel for texel
    out = multilayer._sample(tex, rows, 64, 64, 1.0, (0.0, 0.0))
    assert out[0, 0, 0] == 255 and out[0, 1, 0] == 0


def test_each_mask_of_a_setup_gets_its_own_blend(tmp_path):
    """A weapon draws one setup through a first-person mask and a third-person one (masksset / maskssettpp):
    the chunks using each must get their own blend, not the last material's."""
    files = multilayer_files()
    pos, nrm, uv, idx = S.grid_plane(4, 1.0)
    tpp_mask = RIFLE_DIR + r"\w_rifle_a_tpp.mlmask"
    cover = np.zeros((16, 16), np.uint8)
    cover[:8, :] = 255  # the blue layer covers the top half for the third-person mask (right half for the other)
    files[tpp_mask] = S.build_mlmask([np.full((16, 16), 255, np.uint8), cover])
    fpp = S.material_instance(METAL_MT, {"MultilayerSetup": MLSETUP, "MultilayerMask": MLMASK})
    tpp = S.material_instance(METAL_MT, {"MultilayerSetup": MLSETUP, "MultilayerMask": tpp_mask})
    files[RIFLE] = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx), S.SynthChunk(pos, nrm, uv, idx)],
                                [("steel", fpp), ("steel_tpp", tpp)])
    root = tmp_path / "exported"
    write_loose(root, files)
    model = open_model(root.joinpath(*RIFLE.split("\\")))
    a, b = model.materials
    assert a.albedo != b.albedo and a.albedo.endswith("#color") and b.albedo.endswith("#color")
    first, second = model.textures[a.albedo].decode(), model.textures[b.albedo].decode()
    assert first[1024, 256, 0] == 255 and first[1024, 1792, 2] > 0  # blue on the right
    assert second[256, 1024, 2] > 0 and second[1792, 1024, 0] == 255  # blue at the top


def test_mesh_uvs_run_down_the_texture(tmp_path):
    """The game's V runs up the image; Dark STL's runs down, so the mesh's V is flipped (the masks and
    textures land where the game puts them, checked on a real revolver)."""
    from darkstl.importers.redengine.formats.mesh import read_mesh
    from darkstl.importers.redengine.mesh import RedMeshOptions, chunks_to_trimesh

    pos, nrm, uv, idx = S.grid_plane(2, 2.0)
    data = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("m", S.material_instance(METAL_MT, {}))])
    tm = chunks_to_trimesh(read_mesh(cr2w.read_cr2w(data)), RedMeshOptions())
    corners = tm.vertices[tm.faces]  # the plane's UV is its position over its size, so V = y / 2 in the file
    assert np.allclose(tm.corner_uvs[..., 0], corners[..., 0] / 2.0, atol=1e-3)
    assert np.allclose(tm.corner_uvs[..., 1], 1.0 - corners[..., 1] / 2.0, atol=1e-3)
