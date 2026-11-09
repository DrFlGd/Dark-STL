"""The id Tech 7 (DOOM Eternal) importer, on synthetic files built to the layouts docs/PLAN.md describes: the
container and StreamDB readers, textures, static and skinned models, skeletons, animations, material decls, the
library listing and opening models, posing and their textures. Nothing here was checked on the real game's files."""

import math
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
import synth_idtech as S
from test_app import home  # noqa: F401 - fixture

from darkstl.importers import importer_for, open_model
from darkstl.importers.idtech import IdTechImporter, catalog, games
from darkstl.importers.idtech import store as storemod
from darkstl.importers.idtech.formats import decl, image, md6anim, md6skel, resources, streamdb
from darkstl.importers.idtech.formats.farmhash import fingerprint64
from darkstl.model import MeshOptions


@pytest.fixture
def install(tmp_path) -> Path:
    root = tmp_path / "DOOMEternal"
    S.eternal_install(root)
    yield root
    storemod.forget(root)


def _entries(root: Path, imp: IdTechImporter | None = None):
    imp = imp or IdTechImporter()
    return {e.label: e for e in imp.scan(root, games.ETERNAL)}


# -- formats --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("data, value", [
    (b"", 0x9AE16A3B2F90404F), (b"a", 0xB3454265B6DF75E3), (b"hello world", 0x588FB7478BD6B01B),
    (bytes(range(40)), 0x8F45B937B49468C), (bytes(range(256)) * 3, 0x1D08637075CFEA20)])
def test_fingerprint_matches_farmhash(data, value):
    assert fingerprint64(data) == value


def test_container_lists_and_reads_entries(tmp_path):
    payload = b"hello" * 40
    blob, fp = S.container([("model", "a/b.lwo", payload, 7, 1), ("image", "a/b.tga", b"xyz", 8, 0)])
    path = tmp_path / "x.resources"
    path.write_bytes(blob)
    assert resources.is_container(path)
    c = resources.read_container(path)
    assert c.version == 12 and c.fingerprint == fp
    assert [(e.type, e.name, e.hash) for e in c.entries] == [("model", "a/b.lwo", 7), ("image", "a/b.tga", 8)]
    assert resources.read_entry(path, c.entries[0]) == payload
    assert resources.read_entry(path, c.entries[1]) == b"xyz"


def test_container_mask_switches_off_stale_copies(tmp_path):
    blob, fp = S.container([("model", "a", b"1", 1, 0), ("model", "b", b"2", 2, 0)])
    path = tmp_path / "x.resources"
    path.write_bytes(blob)
    c = resources.read_container(path)
    mask = resources.read_container_mask(S.container_mask({fp: [True, False]}), 12)
    assert list(resources.live(mask, c)) == [True, False]
    assert list(resources.live({}, c)) == [True, True]  # no mask: everything counts


def test_oodle_compressed_entries_say_what_is_needed(monkeypatch):
    from darkstl.errors import FormatError
    from darkstl.importers.fromsoft.formats import oodle

    def missing(*_a, **_k):
        raise FormatError("Oodle isn't available")

    monkeypatch.setattr(oodle, "oodle_decompress", missing)
    with pytest.raises(FormatError, match="Oodle"):
        resources.decompress(b"\0" * 8, resources.COMP_KRAKEN, 64, "x")


def test_streamdb_pieces_and_ids(tmp_path):
    path = tmp_path / "x.streamdb"
    path.write_bytes(S.streamdb({streamdb.eternal_id(0x1234, 0): b"abc", streamdb.eternal_id(0x1234, 2): b"defg"}))
    assert streamdb.is_streamdb(path)
    db = streamdb.read_streamdb(path)
    assert streamdb.read_piece(db, 0x12340, 3) == b"abc"
    assert streamdb.read_piece(db, 0x12342, 4) == b"defg"
    assert streamdb.read_piece(db, 0x12341, 4) is None
    assert streamdb.darkages_id(5, 1) != streamdb.darkages_id(5, 2)


def test_image_with_a_streamed_mip():
    full = S.bc1_solid(8, 8, 0xF800)
    data = S.image(8, 8, [full, S.bc1_solid(4, 4, 0xF800), S.bc1_solid(4, 4, 0xF800)], 33, 1, streamed=1)
    asked = []

    def fetch(i, size):
        asked.append((i, size))
        return full

    img = image.read_image(data, fetch)
    assert img.codec == "BC1" and img.srgb
    assert asked == [(0, len(full))]
    px = img.decode()
    assert px.shape == (8, 8, 4) and tuple(px[0, 0, :3]) == (255, 0, 0)
    assert image.mip_stream_id(img, 0, 0x3333, False) == streamdb.eternal_id(0x3333, 3)
    assert img.to_dds()[:4] == b"DDS "
    shown = image.read_image(data)  # without the streamed mip, the largest kept one is shown and said so
    assert shown.decode().shape == (4, 4, 4) and "not found" in shown.describe()


def test_skeleton_and_animation():
    sk = md6skel.read_skeleton(S.skeleton(["origin", "spine"], [-1, 0], [(0, 0, 0), (0, 0, 36.0)]))
    assert sk.names == ["origin", "spine"] and list(sk.parents) == [-1, 0] and len(sk) == 2
    assert np.allclose(sk.bind_local()[1, :3, 3], [0, 0, 36])
    data = S.anim("x.md6skl", 4, 30, 1, [0.0, 0.5, 1.0, math.pi / 2], const_t={0: (0, 0, 0), 1: (0, 0, 36.0)})
    assert md6anim.read_anim_skeleton(data) == "x.md6skl"
    a = md6anim.read_md6_anim(data)
    assert a.frames == 4 and a.fps == 30
    rot, _scl, trn = a.sample(3, 2, sk.rotations, sk.scales, sk.translations)
    angle = 2 * math.atan2(rot[1, 2], rot[1, 3])
    assert angle == pytest.approx(math.pi / 2, abs=1e-3)
    assert np.allclose(trn[1], [0, 0, 36])
    mid = a.sample(1, 2, sk.rotations, sk.scales, sk.translations)[0]
    assert 2 * math.atan2(mid[1, 2], mid[1, 3]) == pytest.approx(0.5, abs=1e-3)


def test_rle_joint_lists():
    assert md6anim.decode_rle(S.rle([1, 2, 3, 7]), 0) == [1, 2, 3, 7]


def test_decl_parse_and_inherit():
    child = decl.parse('{ inherit = "base"; edit = { RenderLayers = { item[0] = { parms = { '
                       'albedo = { filePath = "a.tga"; } } } } } }')
    assert decl.get(child, "inherit") == "base"
    assert decl.get(child, "edit", "renderlayers", "item[0]", "parms", "albedo", "filepath") == "a.tga"
    parent = decl.parse('{ edit = { Parms = { smoothness = { filePath = "s.tga"; } } } }')
    merged = decl.merge(parent, child)
    assert decl.get(merged, "edit", "parms", "smoothness", "filePath") == "s.tga"
    assert decl.get(merged, "edit", "renderlayers", "item[0]", "parms", "albedo", "filePath") == "a.tga"


@pytest.mark.parametrize("type_, name, kind, group", [
    ("baseModel", "md6/monsters/imp/base/imp.md6mesh", "Characters", "Imp"),
    ("baseModel", "md6/player/doomslayer/base/doomslayer.md6mesh", "Characters", "Player"),
    ("baseModel", "md6/weapons/shotgun/base/shotgun.md6mesh", "Weapons", "Shotgun"),
    ("model", "models/pickups/medkit.lwo", "Objects", "Pickups"),
    ("model", "maps/e1m1_intro/hell/rock_01.lwo", "Map Pieces", "E1M1 Intro"),
])
def test_classify(type_, name, kind, group):
    assert catalog.classify(type_, name)[0] == kind
    assert catalog.classify(type_, name)[1].startswith(group)


# -- the importer ---------------------------------------------------------------------------------------------


def test_detects_and_lists_the_game(install):
    imp = IdTechImporter()
    assert imp.detect_game(install) == games.ETERNAL
    entries = _entries(install, imp)
    assert set(entries) == {"imp", "crate", "medkit"}
    assert entries["imp"].category == "Characters" and entries["medkit"].group == "Pickups"
    assert all(importer_for(e.path) is not None and importer_for(e.path).key == "idtech" for e in entries.values())


def test_static_models_read_inline_and_streamed_geometry(install):
    entries = _entries(install)
    for label, size in (("medkit", 10.0), ("crate", 40.0)):
        m = open_model(entries[label].path)
        mesh = m.build(MeshOptions())
        assert len(mesh.faces) == 2
        extent = mesh.vertices.max(0) - mesh.vertices.min(0)
        assert extent[0] == pytest.approx(size * 0.0254, rel=1e-3), label  # inches to metres
    crate = open_model(entries["crate"].path)
    assert crate.materials[0].albedo == "models/props/crate/crate_albedo.tga"  # not the masked-off stale copy's
    assert tuple(crate.textures["models/props/crate/crate_albedo.tga"].decode()[0, 0, :3]) == (0, 255, 0)


def test_faces_point_the_way_their_normals_do(install):
    m = open_model(_entries(install)["medkit"].path)
    mesh = m.build(MeshOptions())
    v = mesh.vertices[mesh.faces]
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    assert (n[:, 1] < 0).all()  # the synthetic quad faces -Y, as its vertex normals say


def test_skinned_model_textures_and_pose(install):
    m = open_model(_entries(install)["imp"].path)
    assert m.materials[0].albedo == "models/monsters/imp/imp_albedo.tga"
    assert m.materials[0].normal == "models/monsters/imp/imp_n.tga"
    assert any("monster_base" in d for d in m.materials[0].details)  # the parent decl was read
    albedo = m.textures["models/monsters/imp/imp_albedo.tga"]
    assert albedo.decode().shape[:2] == (8, 8)  # the full-size mip, from the StreamDB
    assert m.animations is not None and [a.name for a in m.animations.list()] == ["turn"]
    assert m.rig is not None and m.rig.names == ["origin", "spine"]
    rest = m.build(MeshOptions())
    posed = m.build(MeshOptions(pose=("turn", 3)))
    # the top two vertices are bound to the spine, which turns a quarter about Z around its joint 36" up
    moved = np.abs(posed.vertices - rest.vertices).max(1) > 1e-4
    assert moved.sum() == 2
    assert np.allclose(np.sort(posed.vertices[:, 2]), np.sort(rest.vertices[:, 2]), atol=1e-5)


def test_collected_copy_opens_without_the_game(install, tmp_path):
    imp = IdTechImporter()
    entry = _entries(install, imp)["imp"]
    files = imp.related_files(entry.path)
    names = [f.save_as for f in files]
    assert any(n.endswith("imp.md6mesh") for n in names) and any(n.endswith("turn.md6anim") for n in names)
    assert any(n.startswith("base/streamdb/") for n in names), "the streamed pieces come along"
    assert sum(n.endswith(storemod.META) for n in names) == sum(not n.endswith(storemod.META) and
                                                                 not n.startswith("base/streamdb/") for n in names)
    copy = tmp_path / "copy" / "DOOMEternal"
    for f in files:
        dest = copy / f.save_as
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.extract() if f.extract else Path(f.path).read_bytes())
    storemod.forget(install)
    shutil.rmtree(install)
    assert imp.detect_game(copy) == games.ETERNAL
    entries = _entries(copy, imp)
    assert set(entries) == {"imp"}
    m = open_model(entries["imp"].path)
    assert [a.name for a in m.animations.list()] == ["turn"]
    assert m.textures["models/monsters/imp/imp_albedo.tga"].decode().shape[:2] == (8, 8)
    storemod.forget(copy)


def test_a_profile_in_the_settings_folder_tunes_the_game(home, install):  # noqa: F811
    """What isn't confirmed on the real files yet (size, normal maps, executables) can be set by a game profile in
    the settings folder, through the shared profile loader."""
    import json

    from darkstl.importers import profiles

    medkit = _entries(install)["medkit"].path
    folder = home / "profiles"
    folder.mkdir(parents=True)
    (folder / "doom-eternal.json").write_text(json.dumps({
        "format": 1, "name": games.ETERNAL, "engine": "idtech", "idtech": {"unit": 0.01},
        "textures": {"normal_green": "directx"}, "recognise": {"files": ["DOOMEternal_renamed.exe"]}}))
    profiles.forget()
    try:
        p = games.profile(games.ETERNAL)
        assert p.unit == 0.01 and p.normal_green == "directx" and "DOOMEternal_renamed.exe" in p.executables
        mesh = open_model(medkit).build(MeshOptions())
        assert (mesh.vertices.max(0) - mesh.vertices.min(0))[0] == pytest.approx(0.1, rel=1e-3)
        renamed = home / "renamed"
        renamed.mkdir()
        (renamed / "DOOMEternal_renamed.exe").write_bytes(b"MZ")
        assert catalog.detect_game(renamed) == games.ETERNAL
    finally:
        (folder / "doom-eternal.json").unlink()
        profiles.forget()
    assert games.profile(games.ETERNAL).unit == 0.0254


def test_header_sizes_the_readers_expect():
    assert resources.ENTRY.itemsize == 144
    assert struct.calcsize("<7h") == 14
