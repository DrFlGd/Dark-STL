"""The Cyberpunk 2077 importer: CR2W files, archives, meshes, textures, materials, library and Collect."""

from pathlib import Path

import numpy as np
import pytest
import synth_red as S
from test_heightmap import encode_normals
from test_pipeline import bump_height

from darkstl import collect, library
from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.redengine import RedEngineImporter, catalog, depot
from darkstl.importers.redengine.formats import archive as arc
from darkstl.importers.redengine.formats import cr2w
from darkstl.importers.redengine.formats.mesh import read_mesh
from darkstl.importers.redengine.formats.texture import read_texture
from darkstl.model import MeshOptions

RIFLE = r"base\weapons\firearms\rifle_assault\w_rifle_a\w_rifle_a.mesh"
ALBEDO = r"base\weapons\firearms\rifle_assault\w_rifle_a\textures\w_rifle_a_d.xbm"
NORMAL = r"base\weapons\firearms\rifle_assault\w_rifle_a\textures\w_rifle_a_n.xbm"
STEEL_MI = r"base\surfaces\materials\metal\steel.mi"
METAL_MT = r"base\materials\metal_base.mt"
JACKET = r"base\characters\garment\citizen_casual\torso\t1_077_jacket\t1_077_jacket.mesh"
JACKET_SHADOW = r"base\characters\garment\citizen_casual\torso\t1_077_jacket\t1_077_jacket_shadow.mesh"


def rifle_files() -> dict[str, bytes]:
    """A rifle whose material chain is local instance -> steel.mi -> metal_base.mt, with a jacket beside it."""
    pos, nrm, uv, idx = S.grid_plane(4, 1.0)
    albedo = np.zeros((64, 64, 4), np.uint8)
    albedo[..., 2] = 200
    albedo[..., 3] = 255
    local = S.material_instance(STEEL_MI, {"Normal": NORMAL})
    mesh = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx), S.SynthChunk(pos * 0.5, nrm, uv, idx, lod_mask=2)],
                        [("steel", local)], [("default", ["steel", "steel"]), ("rusty", ["steel", "steel"])])
    jacket = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx, skinned=True)], [("cloth", r"base\surfaces\cloth.mi")])
    return {
        RIFLE: mesh,
        ALBEDO: S.rgba_xbm(albedo),
        NORMAL: S.rgba_xbm(encode_normals(bump_height(), "directx")),
        STEEL_MI: S.build_material_instance(METAL_MT, {"BaseColor": ALBEDO, "Normal": r"base\placeholder\x.xbm"}),
        METAL_MT: S.build_material_template("metal_base"),
        JACKET: jacket,
        JACKET_SHADOW: jacket,
    }


def write_loose(root: Path, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        p = root.joinpath(*path.split("\\"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


@pytest.fixture
def export_dir(tmp_path):
    """Files as WolvenKit exports them: loose, under their depot paths."""
    root = tmp_path / "wolvenkit" / "exported"
    write_loose(root, rifle_files())
    return root


@pytest.fixture
def game_dir(tmp_path):
    """A game folder: two archives, the models' names only known from the files that refer to them."""
    root = tmp_path / "Cyberpunk 2077"
    (root / "bin" / "x64").mkdir(parents=True)
    (root / "bin" / "x64" / "Cyberpunk2077.exe").write_bytes(b"")
    content = root / "archive" / "pc" / "content"
    content.mkdir(parents=True)
    files = rifle_files()
    # an entity names the rifle and the jacket, as the game's .ent files do; the shadow mesh stays unnamed
    w = S.Cr2wWriter()
    ent = S.Obj("entEntityTemplate")
    ent.add("meshes", "array:rRef:CMesh", [RIFLE, JACKET])
    w.add_chunk(ent)
    files[r"base\weapons\w_rifle_a.ent"] = w.build()
    main = {p: cr2w.read_cr2w(d).objects_end for p, d in files.items()}
    textures = {k: v for k, v in files.items() if k.endswith(".xbm")}
    rest = {k: v for k, v in files.items() if not k.endswith(".xbm")}
    (content / "basegame_4_gamedata.archive").write_bytes(S.build_archive(rest, main))
    (content / "basegame_3_textures.archive").write_bytes(S.build_archive(textures, main))
    return root


def test_cr2w_round_trip_of_values():
    w = S.Cr2wWriter()
    inner = S.Obj("Vector4").add("X", "Float", 1.5).add("Z", "Float", -2.0)
    obj = S.Obj("Thing").add("name", "CName", "hello").add("text", "String", "héllo").add("flag", "Bool", True)
    obj.add("count", "Uint32", 7).add("v", "Vector4", inner).add("list", "array:Uint16", [1, 2, 3])
    obj.add("kind", "enum:ETextureCompression", "TCM_QualityRG").add("ref", "rRef:ITexture", r"base\a.xbm")
    obj.add("raw", "DataBuffer", b"abc")
    w.add_chunk(obj)
    f = cr2w.read_cr2w(w.build())
    r = f.root
    assert r.cls == "Thing" and r["name"] == "hello" and r["text"] == "héllo" and r["flag"] is True
    assert r["count"] == 7 and r["v"].vec("X", "Y", "Z") == (1.5, 0.0, -2.0) and r["list"] == [1, 2, 3]
    assert r["kind"] == "TCM_QualityRG" and r["ref"] == cr2w.Ref(r"base\a.xbm", "ITexture")
    assert f.buffer(r["raw"]) == b"abc"
    assert cr2w.parse_type("array:handle:Foo") == ("array", ("handle", ("leaf", "Foo")))
    assert cr2w.parse_type("static:4,Uint32") == ("static", 4, ("leaf", "Uint32"))
    assert cr2w.parse_type("[8]Float") == ("fixed", 8, ("leaf", "Float"))


def test_unknown_property_types_are_skipped_not_fatal():
    w = S.Cr2wWriter()
    obj = S.Obj("Thing").add("a", "Uint32", 1)
    # a property of a type the reader does not know, 6 bytes of something that is not a class
    w.add_chunk(obj)
    data = bytearray(w.build())
    f = cr2w.read_cr2w(bytes(data))
    assert f.root["a"] == 1


def test_mesh_geometry_and_lods():
    pos, nrm, uv, idx = S.grid_plane(2, 2.0)
    data = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx), S.SynthChunk(pos, nrm, uv, idx, lod_mask=4)],
                        [("m", S.material_instance(METAL_MT, {}))])
    m = read_mesh(cr2w.read_cr2w(data))
    assert [c.lod_level for c in m.chunks] == [0, 2]
    c = m.chunks[0]
    assert c.vertex_count == 9 and c.index_count == 24
    assert np.allclose(c.positions, pos, atol=2e-4)
    assert np.allclose(c.normals, nrm, atol=2e-3)
    assert np.allclose(c.uvs[0], uv, atol=1e-3)
    assert m.local_materials[0].cls == "CMaterialInstance"


def test_texture_formats():
    rgba = np.zeros((8, 4, 4), np.uint8)
    rgba[..., 1] = 77
    rgba[..., 3] = 255
    t = read_texture(cr2w.read_cr2w(S.rgba_xbm(rgba)))
    assert (t.width, t.height, t.codec) == (4, 8, "R8G8B8A8")
    assert np.array_equal(t.decode(), rgba)
    # BC1: one 4x4 block of solid colour (both endpoints the same, all indices 0)
    block = bytes([0xE0, 0x07, 0xE0, 0x07]) + b"\0" * 4  # RGB565 pure green
    t = read_texture(cr2w.read_cr2w(S.build_xbm(4, 4, block, "TCM_DXTNoAlpha")))
    assert t.codec == "BC1" and t.decode()[0, 0, 1] > 250 and t.decode()[0, 0, 0] == 0
    assert t.to_dds()[:4] == b"DDS "
    t = read_texture(cr2w.read_cr2w(S.build_xbm(4, 4, b"\0" * 16, "TCM_Normalmap")))
    assert t.codec == "BC5"


def test_archive_paths_and_hashes(tmp_path):
    assert arc.normalize_path("Base/Weapons\\\\W_Rifle.mesh") == r"base\weapons\w_rifle.mesh"
    assert arc.fnv1a64("") == arc.FNV_OFFSET
    assert arc.fnv1a64("a") == 0xAF63DC4C8601EC8C
    p = tmp_path / "x.archive"
    p.write_bytes(S.build_archive({r"base\a.bin": b"hello", r"base\b.bin": b"world!" * 10}, {r"base\b.bin": 6}))
    a = arc.open_archive(p)
    assert len(a) == 2 and a.read(arc.path_hash(r"base\a.bin")) == b"hello"
    assert a.read(arc.path_hash(r"base\b.bin")) == b"world!" * 10
    assert a.read_head(arc.path_hash(r"base\b.bin")) == b"world!"
    assert a.name_of(arc.path_hash(r"base\a.bin")) == f"{arc.path_hash(chr(98) + 'ase' + chr(92) + 'a.bin'):016x}"
    assert a.learn([r"base\a.bin", r"base\missing.bin"]) == 1 and a.name_of(arc.path_hash(r"base\a.bin")) == r"base\a.bin"
    assert arc.is_archive_file(p) and not arc.is_archive_file(tmp_path)


def test_kark_buffers_need_oodle():
    data = cr2w.KARK + (123).to_bytes(4, "little") + b"\x8c\x06xyz"
    with pytest.raises(FormatError, match="Oodle"):
        cr2w.decompress_buffer(data)
    assert cr2w.decompress_buffer(b"plain") == b"plain"


def test_loose_export_is_a_source_model(export_dir):
    path = export_dir.joinpath(*RIFLE.split("\\"))
    assert importer_for(path).key == "redengine"
    model = open_model(path)
    assert model.game == "Cyberpunk 2077" and model.name == "w_rifle_a"
    assert model.variants == ["default", "rusty"] and model.variant == "default"
    assert [p.name for p in model.parts] == ["Chunk 0", "Chunk 1 (LOD 1)"]
    mat = model.materials[0]
    assert mat.name == "steel" and mat.albedo == arc.normalize_path(ALBEDO) and mat.normal == arc.normalize_path(NORMAL)
    assert mat.details[0] == "template metal_base"
    assert set(model.textures) == {arc.normalize_path(ALBEDO), arc.normalize_path(NORMAL)}
    assert model.textures[mat.albedo].decode()[0, 0, 2] == 200
    assert "64 x 64, R8G8B8A8" in model.textures[mat.albedo].describe()
    mesh = model.build_mesh()
    assert mesh.n_vertices == 25 and mesh.n_faces == 32  # the LOD 1 chunk is left out
    assert np.allclose(mesh.bounds()[1], [1, 1, 0])
    assert model.build_mesh(MeshOptions(flags={"lod0": False})).n_faces == 64
    assert model.build_mesh(MeshOptions(parts={1})).n_faces == 32
    assert not model.notes


def test_a_model_missing_its_files_says_so(tmp_path):
    root = tmp_path / "loose"
    write_loose(root, {RIFLE: rifle_files()[RIFLE]})
    model = open_model(root.joinpath(*RIFLE.split("\\")))
    assert model.materials[0].normal is None
    assert any("could not be found" in n for n in model.notes)


def test_library_scan_of_an_export_folder(export_dir):
    assert library.detect_game(export_dir) == "Cyberpunk 2077"
    entries, notes = library.scan_folder(export_dir)
    assert not notes
    by_stem = {e.stem: e for e in entries}
    assert by_stem["w_rifle_a"].category == "Weapons" and by_stem["w_rifle_a"].group == "firearms/rifle assault"
    assert by_stem["t1_077_jacket"].category == "Armor" and by_stem["t1_077_jacket"].group == "Torso"
    assert by_stem["t1_077_jacket_shadow"].is_lod
    lib = library.Library()
    lib.add_root(export_dir)
    assert [e.stem for e in lib.query("Cyberpunk 2077")] == ["w_rifle_a", "t1_077_jacket"]
    assert [e.stem for e in lib.query("Cyberpunk 2077", include_lod=True)] == ["w_rifle_a", "t1_077_jacket",
                                                                                "t1_077_jacket_shadow"]


def test_models_are_listed_from_the_games_archives(game_dir, tmp_path):
    assert catalog.detect_game(game_dir) == "Cyberpunk 2077"
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game_dir, "Cyberpunk 2077", None)
    assert sorted(e.stem for e in entries if e.group != "Unnamed") == ["t1_077_jacket", "w_rifle_a"]
    unnamed = [e for e in entries if e.group == "Unnamed"]
    assert len(unnamed) == 1 and unnamed[0].category == "Other"  # the shadow mesh nothing refers to
    rifle = next(e for e in entries if e.stem == "w_rifle_a")
    assert rifle.path.parent.name == "basegame_4_gamedata.archive" and depot.is_archive_member(rifle.path)
    assert any("listed by hash" in n for n in imp.folder_notes(game_dir, entries))
    # cached: the archives are not read through again
    assert (tmp_path / "cache" / "archives").is_dir()
    again = RedEngineImporter()
    again.cache = tmp_path / "cache"
    assert [e.id for e in again.scan(game_dir, "Cyberpunk 2077", None)] == [e.id for e in entries]

    model = open_model(rifle.path)
    assert model.name == "w_rifle_a" and model.variants == ["default", "rusty"]
    mat = model.materials[0]
    assert mat.albedo and mat.normal, model.notes  # textures found in the other archive
    assert model.textures[mat.albedo].source.startswith("basegame_3_textures.archive > ")
    assert model.build_mesh().n_faces == 32
    assert imp.describe(rifle.path)[0].startswith("CMesh")
    assert imp.describe(rifle.path.parent)[0].startswith("REDengine archive, 6 files")


def test_collecting_from_archives_writes_a_loose_tree(game_dir, tmp_path):
    imp = RedEngineImporter()
    imp.cache = tmp_path / "cache"
    entries = imp.scan(game_dir, "Cyberpunk 2077", None)
    rifle = next(e for e in entries if e.stem == "w_rifle_a")
    related = imp.related_files(rifle.path)
    kinds = sorted((f.kind, f.save_as) for f in related)
    assert ("model", RIFLE.replace("\\", "/")) in kinds
    assert ("texture", ALBEDO.replace("\\", "/")) in kinds and ("texture", NORMAL.replace("\\", "/")) in kinds
    assert ("other", STEEL_MI.replace("\\", "/")) in kinds and ("other", METAL_MT.replace("\\", "/")) in kinds
    assert all(f.extract is not None and f.size > 0 for f in related)
    lib_dir = tmp_path / "library"
    plan = collect.plan(lib_dir, "Cyberpunk 2077", [rifle], [game_dir], collect.CollectOptions(),
                        lambda e: imp.related_files(e.path), lambda root: [])
    collect.run(plan)
    copied = lib_dir / "Cyberpunk 2077"
    mesh_path = copied.joinpath(*RIFLE.split("\\"))
    assert mesh_path.is_file() and copied.joinpath(*ALBEDO.split("\\")).is_file()
    # the copies are read like a WolvenKit export, textures and all
    model = open_model(mesh_path)
    assert model.materials[0].albedo and model.materials[0].normal and model.build_mesh().n_faces == 32
    assert library.scan(copied)[0].stem == "w_rifle_a"


def test_loose_export_related_files_keep_their_paths(export_dir):
    imp = RedEngineImporter()
    path = export_dir.joinpath(*RIFLE.split("\\"))
    related = imp.related_files(path)
    assert related[0].kind == "model" and related[0].save_as == ""
    assert sorted(f.path.name for f in related) == ["metal_base.mt", "steel.mi", "w_rifle_a.mesh", "w_rifle_a_d.xbm",
                                                     "w_rifle_a_n.xbm"]
    assert all(f.save_as == "" for f in related)  # same root: relative paths are kept by Collect


def test_textures_from_extra_folders(tmp_path):
    files = rifle_files()
    root = tmp_path / "models"
    write_loose(root, {k: v for k, v in files.items() if k.endswith((".mesh", ".mi", ".mt"))})
    flat = tmp_path / "textures"
    flat.mkdir()
    for k, v in files.items():
        if k.endswith(".xbm"):
            (flat / k.rsplit("\\", 1)[-1]).write_bytes(v)
    model = open_model(root.joinpath(*RIFLE.split("\\")), texture_dirs=(str(flat),))
    assert model.materials[0].albedo and model.materials[0].normal


def test_classify_depot_paths():
    assert catalog.classify(r"base\vehicles\sport\v_sport2_quadra_type66\quadra_body.mesh")[:2] == ("Vehicles", "Sport")
    assert catalog.classify(r"base\environment\decoration\furniture\chair_a.mesh")[:2] == ("Objects", "Decoration")
    assert catalog.classify(r"base\characters\head\pma\h0_000_pma_c__basehead.mesh")[:2] == ("Characters", "Head")
    assert catalog.classify(r"ep1\weapons\melee\katana\w_katana.mesh")[:2] == ("Weapons", "melee/katana")
    assert catalog.classify(None) == ("Other", "Unnamed", "", False)
    assert catalog.classify(r"base\fx\thing_proxy.mesh")[3] is True


def test_the_app_lists_opens_and_previews_archive_models(game_dir, tmp_path, monkeypatch):
    from test_app import wait

    from darkstl.app.config import Config
    from darkstl.app.service import Service

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    s = Service(Config.load(), tmp_path / "home" / "cache")
    assert s.call("folder_check", {"path": str(game_dir)})["game"] == "Cyberpunk 2077"
    result = wait(s, s.call("folder_add", {"path": str(game_dir)})["job"])
    assert result["game"] == "Cyberpunk 2077" and result["count"] == 3  # the unnamed shadow mesh counts
    overview = s.call("overview", {})
    game = next(g for g in overview["games"] if g["name"] == "Cyberpunk 2077")
    assert [c["name"] for c in game["categories"]] == ["Weapons", "Armor", "Other"]
    items = s.call("models_query", {"game": "Cyberpunk 2077", "category": "Weapons"})["items"]
    assert [i["stem"] for i in items] == ["w_rifle_a"]
    opened = s.call("model_open", {"id": items[0]["id"]})
    assert opened["name"] == "w_rifle_a" and opened["game"] == "Cyberpunk 2077"
    assert len(opened["materials"]) == 1 and opened["materials"][0]["albedo"]
    assert s.call("thumb", {"id": items[0]["id"]})[:4] == b"\x89PNG"
