"""Model packages: a model written out once reads back as the importer built it, posing included."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from darkstl import package as pkg
from darkstl.importers import importer_for, open_model
from darkstl.importers.package import PackageImporter
from darkstl.model import CatalogEntry, MeshOptions

from test_animation import FRAMES, make_char_dir
from test_reengine_motion import PL, re2_dir, _scan  # noqa: F401 - fixture


def _entry(model, path, game="Test"):
    return CatalogEntry(game, "Characters", "c2000", path.name.split(".")[0], "A character", path,
                        importer_for(path).key)


def _write(tmp_path, path, root=None):
    imp = importer_for(path)
    variants = imp.variants(path) or [None]
    models = [imp.load(path, v) for v in variants]
    root = root or tmp_path / "packages"
    store = pkg.AssetStore(root)
    index = pkg.write_package(root, _entry(models[0], path), path.name, models, store)
    store.save()
    return models, index


def _same_mesh(a, b, atol=1e-5):
    """Same surface: the same faces at the same places, UVs and normals per corner (orders may differ)."""
    assert a.n_faces == b.n_faces
    ka = np.lexsort(np.round(a.vertices[a.faces].reshape(len(a.faces), -1), 4).T[::-1])
    kb = np.lexsort(np.round(b.vertices[b.faces].reshape(len(b.faces), -1), 4).T[::-1])
    assert np.allclose(a.vertices[a.faces][ka], b.vertices[b.faces][kb], atol=atol)
    if a.corner_uvs is not None:
        assert np.allclose(a.corner_uvs[ka], b.corner_uvs[kb], atol=1e-6)
    if a.corner_normals is not None:
        assert np.allclose(a.corner_normals[ka], b.corner_normals[kb], atol=1e-4)
    assert (a.face_material[ka] == b.face_material[kb]).all()
    assert (a.face_group[ka] == b.face_group[kb]).all()


def test_a_fromsoft_character_round_trips_with_its_animations(tmp_path):
    game = make_char_dir(tmp_path / "DARK SOULS III" / "Game")
    src = game / "chr" / "c2000.chrbnd.dcx"
    (model,), index = _write(tmp_path, src)
    assert index.name == "package.json" and index.parent.name == "c2000.chrbnd.dcx.darkpkg"
    # the game can go: the package needs nothing from it
    shutil.rmtree(game)
    packed = open_model(index)
    assert packed.importer == "package"
    assert [m.name for m in packed.materials] == [m.name for m in model.materials]
    assert packed.materials[0].albedo == model.materials[0].albedo
    assert set(packed.textures) == set(model.textures)
    key = model.materials[0].albedo
    assert np.array_equal(packed.textures[key].decode(), model.textures[key].decode())
    assert [p.name for p in packed.parts] == [p.name for p in model.parts]
    _same_mesh(packed.build_mesh(), model.build_mesh())
    _same_mesh(packed.build_mesh(MeshOptions(parts={1})), model.build_mesh(MeshOptions(parts={1})))
    # posing: same names, frames, and the same posed mesh as the game's files gave
    assert [a.name for a in packed.animations.list()] == [a.name for a in model.animations.list()]
    assert packed.animations.info("a000_003000").frames == FRAMES
    for frame in (0, 4, FRAMES - 1):
        opts = MeshOptions(pose=("a000_003000", frame))
        _same_mesh(packed.build_mesh(opts), model.build_mesh(opts), atol=1e-4)
    # the two animations are the same, so they are stored once
    assert len(list((index.parents[1] / "assets" / "animations").iterdir())) == 1


def test_an_re_character_round_trips_with_its_motions(re2_dir, tmp_path):  # noqa: F811 - fixture
    _scan(re2_dir)
    src = re2_dir / PL
    (model,), index = _write(tmp_path, src)
    packed = open_model(index)
    _same_mesh(packed.build_mesh(), model.build_mesh())
    assert [a.name for a in packed.animations.list()] == [a.name for a in model.animations.list()]
    assert len(packed.animations.skeleton) == len(model.animations.skeleton)
    for frame in (0, 5, 10):
        opts = MeshOptions(pose=("pl0000_move / wave", frame))
        _same_mesh(packed.build_mesh(opts), model.build_mesh(opts), atol=1e-4)


def test_the_glb_opens_as_ordinary_gltf(tmp_path):
    game = make_char_dir(tmp_path / "DARK SOULS III" / "Game")
    _, index = _write(tmp_path, game / "chr" / "c2000.chrbnd.dcx")
    listed = json.loads(index.read_text())["models"]
    assert len(listed) == 1
    model_json = json.loads((index.parent / listed[0]["file"]).read_text())
    glb = index.parent / model_json["mesh"]
    data = glb.read_bytes()
    assert data[:4] == b"glTF"
    length = int.from_bytes(data[12:16], "little")
    gltf = json.loads(data[20:20 + length])
    assert gltf["skins"][0]["joints"] and gltf["meshes"][0]["primitives"]
    prim = gltf["meshes"][0]["primitives"][0]
    assert {"POSITION", "NORMAL", "TEXCOORD_0", "JOINTS_0", "WEIGHTS_0"} <= set(prim["attributes"])
    assert [n["name"] for n in gltf["nodes"][:3]] == ["root", "arm", "hand"]


def test_packages_are_listed_by_the_package_importer(tmp_path):
    game = make_char_dir(tmp_path / "DARK SOULS III" / "Game")
    root = tmp_path / "packages"
    _write(tmp_path, game / "chr" / "c2000.chrbnd.dcx", root)
    found = PackageImporter().scan(root, "Dark Souls III")
    assert [(e.stem, e.label, e.category, e.importer) for e in found] == [
        ("c2000", "A character", "Characters", "package")]
    assert importer_for(found[0].path).key == "package"


def test_a_texture_name_used_twice_keeps_both(tmp_path):
    from darkstl.model import Texture

    store = pkg.AssetStore(tmp_path)
    a = Texture("T_Body", "one", lambda: np.zeros((4, 4, 4), np.uint8))
    b = Texture("T_Body", "two", lambda: np.full((4, 4, 4), 9, np.uint8))
    ra, rb = store.add_texture(a), store.add_texture(b)
    assert ra == "assets/textures/T_Body.png" and rb.startswith("assets/textures/T_Body~") and ra != rb
    assert store.add_texture(Texture("T_Body", "three", lambda: np.zeros((4, 4, 4), np.uint8))) == ra
    c = Texture("/Game/Chars/T_Arm", "x", lambda: np.zeros((2, 2, 4), np.float32))
    assert store.add_texture(c) == "assets/textures/Game/Chars/T_Arm.npy"


def test_newer_packages_are_refused(tmp_path):
    folder = tmp_path / "x.darkpkg"
    folder.mkdir()
    (folder / "package.json").write_text(json.dumps({"format": pkg.FORMAT, "version": pkg.VERSION + 1}))
    with pytest.raises(Exception, match="newer Dark STL"):
        pkg.read_index(folder / "package.json")


def test_names_with_slashes_stay_inside_the_package():
    assert pkg._safe("GD_Weap:Skins.Dahl|/games/BL2/GD_Weap.upk") == "GD_Weap_Skins.Dahl__games_BL2_GD_Weap.upk"
    assert pkg.listed_variants([{"variant": "a"}, {"variant": "b", "hidden": True}]) == []
    assert pkg.listed_variants([{"variant": "a"}, {"variant": "b"}, {"variant": "c", "hidden": True}]) == ["a", "b"]
