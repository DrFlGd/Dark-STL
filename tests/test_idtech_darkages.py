"""The id Tech 8 (DOOM: The Dark Ages) side of the id Tech importer, on synthetic files built to the layouts
docs/PLAN.md describes: its skinned and static models, skeletons, animations (kept in the container and streamed),
textures and Collect. Nothing here was checked on the real game's files."""

import math
import shutil
from pathlib import Path

import numpy as np
import pytest
import synth_idtech as S

from darkstl.errors import FormatError
from darkstl.importers import open_model
from darkstl.importers.idtech import IdTechImporter, games
from darkstl.importers.idtech import store as storemod
from darkstl.importers.idtech.formats import geometry as G
from darkstl.importers.idtech.formats import md6anim, md6model, md6skel, staticmodel
from darkstl.model import MeshOptions


@pytest.fixture
def install(tmp_path) -> Path:
    root = tmp_path / "DOOMTheDarkAges"
    S.darkages_install(root)
    yield root
    storemod.forget(root)


def _entries(root: Path, imp: IdTechImporter | None = None):
    imp = imp or IdTechImporter()
    return {e.label: e for e in imp.scan(root, games.DARKAGES)}


def _angle(q) -> float:
    return 2 * math.atan2(q[2], q[3])


# -- formats --------------------------------------------------------------------------------------------------


def test_skeleton():
    sk = md6skel.read_skeleton(S.skeleton_darkages(["origin", "spine", "head"], [-1, 0, 1],
                                                   [(0, 0, 0), (0, 0, 36.0), (0, 0, 10.0)]), darkages=True)
    assert sk.names == ["origin", "spine", "head"] and list(sk.parents) == [-1, 0, 1]
    assert np.allclose(sk.bind_local()[2, :3, 3], [0, 0, 10])


def test_six_joint_skinning_keeps_the_four_heaviest():
    pos = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1]], np.float64)
    nor = np.tile([[0.0, -1.0, 0.0]], (3, 1))
    uvs = np.array([[0, 0], [1, 0], [1, 1]], np.float64)
    joints = np.array([[3, 4, 5, 6, 7, 2], [1, 0, 0, 0, 0, 0], [0, 1, 2, 3, 4, 5]])
    # the second and third weights come from the skinning stream (/255), the last three from the normal and tangent
    # (7 bits /254, then 4 bits /75 and 4 bits /89, so the last two are small)
    weights = np.array([[0.3, 0.2, 0.1, 0.25, 0.0, 0.15], [1, 0, 0, 0, 0, 0], [0.1, 0.1, 0.4, 0.4, 0.0, 0.0]])
    streams = S.vertex_streams_darkages(pos, nor, uvs, joints, weights, 6)
    mask = 0
    for m in streams:
        mask |= m
    lod = G.LodInfo(3, 1, mask, tuple(pos.min(0)), 1.0, (0.0, 0.0), 1.0, 6)
    surf = {"mask": mask, "streams": streams, "triangles": [[0, 1, 2]]}
    (got,) = G.read_sequential(S.sequential_piece([surf]), [lod], True)
    assert got.joints.shape == (3, 4) and np.allclose(got.weights.sum(1), 1)
    assert list(got.joints[0]) == [3, 6, 4, 2]  # 0.3, 0.25, 0.2, 0.15; the 0.1 is dropped
    assert got.weights[0, 0] == pytest.approx(0.3 / 0.9, abs=0.01)
    assert got.joints[1, 0] == 1 and got.weights[1, 0] == pytest.approx(1)
    assert set(got.joints[2, :2]) == {2, 3} and got.weights[2, :2] == pytest.approx([0.4, 0.4], abs=0.02)
    assert got.triangles.tolist() == [[0, 1, 2]]


def test_streams_start_aligned_to_their_record_size():
    """Three vertices of 8-byte positions then 4-byte UVs then 8-byte normals: the normals start on 8 bytes, after
    the UVs' 12, and the second surface starts on 8 bytes after the first's indices."""
    pos = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1]], np.float64)
    nor = np.tile([[0.0, -1.0, 0.0]], (3, 1))
    uvs = np.array([[0, 0], [1, 0], [1, 1]], np.float64)
    streams = S.vertex_streams(pos, nor, uvs)
    mask = G.POSITION_SHORT | G.NORMAL_TANGENT | G.MATERIAL_UV_SHORT
    lod = G.LodInfo(3, 1, mask, tuple(pos.min(0)), 1.0, (0.0, 0.0), 1.0)
    surfs = [{"mask": mask, "streams": streams, "triangles": [[0, 1, 2]]} for _ in range(2)]
    a, b = G.read_sequential(S.sequential_piece(surfs), [lod, lod], False)
    for s in (a, b):
        assert np.allclose(s.positions, pos, atol=1e-4) and np.allclose(s.normals, nor, atol=0.01)
        assert np.allclose(s.uvs, uvs, atol=1e-4)


def test_skinned_model_names_its_joints_through_two_tables():
    data, _piece = S.md6_model_darkages("x.md6skl", 10, [4, 2, 7], [1, 8], [{
        "name": "a", "material": "m", "joint_offset": 3, "influence": 4, "positions": np.eye(3), "normals": np.eye(3),
        "uvs": np.zeros((3, 2)), "triangles": [[0, 1, 2]], "joints": np.zeros((3, 4)),
        "weights": np.tile([1.0, 0, 0, 0], (3, 1))}])
    assert md6model.model_skeleton(data) == "x.md6skl"
    m = md6model.read_md6_model_darkages(data, 10)
    assert m.meshes[0].joint_offset == 3 and m.meshes[0].lods[0].influence == 4
    lookup = m.joint_lookup()
    # skinned joints padded to 8, then the extra joints repeat entries before them (entry 8 repeats entry 1)
    assert list(lookup[:3]) == [4, 2, 7] and lookup[8] == 2 and lookup[9] == 2
    assert m.disk[0].size > 0 and not m.disk[0].layouts


def test_static_model_record_and_disk_layouts():
    quad = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], np.float64)
    data, piece = S.static_model_darkages([{"material": "m", "positions": quad, "normals": np.tile([[0, -1.0, 0]],
                                           (4, 1)), "uvs": quad[:, :2], "triangles": [[0, 2, 1], [0, 3, 2]]}], True)
    m = staticmodel.read_static_model(data, darkages=True)
    assert m.streamable and m.disk[0].size == len(piece) and m.surfaces[0].lods[0].triangles == 2
    with pytest.raises(FormatError):
        staticmodel.read_static_model(data)  # DOOM Eternal's record doesn't fit


@pytest.mark.parametrize("wide", [False, True])
def test_rle16_joint_lists(wide):
    assert md6anim.decode_rle16(S.rle16([1, 2, 3, 300 if wide else 30], wide), 0) == [1, 2, 3, 300 if wide else 30]


def test_animation_kept_in_the_container():
    angles = [0.0, 0.5, 1.0, math.pi / 2]
    data, piece = S.anim_darkages("x.md6skl", 4, 30, 1, angles, const_t={1: (0, 0, 36.0)})
    assert piece is None and md6anim.darkages_pieces(data) == []
    head = md6anim.read_darkages_header(data)
    assert (head.skeleton, head.frames, head.fps) == ("x.md6skl", 4, 30)
    a = md6anim.read_darkages_anim(data)
    (track,) = a.rotations
    assert track.joint == 1 and list(track.frames) == [0, 1, 2, 3]
    assert [_angle(q) for q in track.values] == pytest.approx(angles, abs=1e-3)
    assert np.allclose(a.translations[0].values[0], [0, 0, 36])


def test_streamed_animation_reads_its_finest_frame_sets():
    angles = [0.0, 0.5, 1.0, math.pi / 2]
    data, piece = S.anim_darkages("x.md6skl", 4, 30, 1, angles, streamed=True)
    assert md6anim.darkages_pieces(data) == [(0, len(piece))]
    with pytest.raises(FormatError, match="StreamDB"):
        md6anim.read_darkages_anim(data)
    asked = []
    a = md6anim.read_darkages_anim(data, lambda i, size: asked.append((i, size)) or piece)
    assert asked == [(0, len(piece))]
    (track,) = a.rotations
    assert [_angle(q) for q in track.values] == pytest.approx(angles, abs=1e-3)  # not the coarse set's


# -- the importer ---------------------------------------------------------------------------------------------


def test_detects_and_lists_the_game(install):
    imp = IdTechImporter()
    assert imp.detect_game(install) == games.DARKAGES
    entries = _entries(install, imp)
    assert set(entries) == {"slayer", "shield", "relic"}
    assert entries["slayer"].category == "Characters"


def test_static_models(install):
    entries = _entries(install)
    relic = open_model(entries["relic"].path).build(MeshOptions())
    assert len(relic.faces) == 2
    assert (relic.vertices.max(0) - relic.vertices.min(0))[0] == pytest.approx(10 * 0.0254, rel=1e-3)
    shield = open_model(entries["shield"].path)
    assert [p.triangles for p in shield.parts] == [2, 1]
    mesh = shield.build(MeshOptions())
    assert (mesh.vertices.max(0) - mesh.vertices.min(0))[0] == pytest.approx(40 * 0.0254, rel=1e-3)
    assert shield.materials[0].albedo == "models/props/shield/shield_albedo.tga"  # named without extension
    assert tuple(shield.textures[shield.materials[0].albedo].decode()[0, 0, :3]) == (0, 255, 0)


def test_skinned_model_textures_and_poses(install):
    m = open_model(_entries(install)["slayer"].path)
    assert m.materials[0].albedo == "models/characters/slayer/slayer_albedo.tga"
    assert m.textures[m.materials[0].albedo].decode().shape[:2] == (8, 8)  # the streamed mip
    assert sorted(a.name for a in m.animations.list()) == ["swing", "turn"]
    rest = m.build(MeshOptions())
    joints, weights = rest.metadata["skin"]
    assert set(joints[weights[:, 0] == 1, 0]) == {0, 1}  # raw joint 8 (the extra joint) is the spine
    size = 72 * 0.0254
    # the spine turns a quarter about Z around its joint 36" up: the top corners go with it, the bottom right one
    # (0.6 origin, 0.4 spine) part of the way
    want = sorted(map(tuple, np.round([[0, 0, 0], [0, 0, size], [0, size, size], [0.6 * size, 0.4 * size, 0]], 3)))
    for anim, frame in (("turn", 3), ("swing", 0)):  # the last frame of one, the first of the other
        posed = m.build(MeshOptions(pose=(anim, frame)))
        assert sorted(map(tuple, np.round(posed.vertices, 3))) == want, anim
    still = m.build(MeshOptions(pose=("swing", 3)))
    assert sorted(map(tuple, np.round(still.vertices, 3))) == sorted(map(tuple, np.round(rest.vertices, 3)))


def test_collected_copy_opens_without_the_game(install, tmp_path):
    imp = IdTechImporter()
    entry = _entries(install, imp)["slayer"]
    files = imp.related_files(entry.path)
    names = [f.save_as for f in files]
    assert any(n.endswith("swing.md6anim") for n in names)
    assert sum(n.startswith("base/streamdb/") for n in names) == 3  # geometry, a mip, the streamed animation
    copy = tmp_path / "copy" / "DOOMTheDarkAges"
    for f in files:
        dest = copy / f.save_as
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.extract() if f.extract else Path(f.path).read_bytes())
    storemod.forget(install)
    shutil.rmtree(install)
    assert imp.detect_game(copy) == games.DARKAGES
    m = open_model(_entries(copy, imp)["slayer"].path)
    assert sorted(a.name for a in m.animations.list()) == ["swing", "turn"]
    posed = m.build(MeshOptions(pose=("swing", 0)))
    assert not np.allclose(posed.vertices, m.build(MeshOptions()).vertices, atol=1e-3)
    assert m.textures[m.materials[0].albedo].decode().shape[:2] == (8, 8)
    storemod.forget(copy)


def test_oodle_from_doom_eternal_beside_it(tmp_path):
    """The Dark Ages ships no Oodle library; DOOM Eternal's, in the same Steam library, is searched, and the folder's
    note says what to do when there's none."""
    from darkstl.importers.fromsoft.formats import oodle

    common = tmp_path / "common"
    root = common / "DOOMTheDarkAges"
    S.darkages_install(root)
    imp = IdTechImporter()
    found = imp.scan(root, games.DARKAGES)
    if oodle.located() is None:
        assert any("doesn't ship" in n for n in imp.folder_notes(root, found))
    (common / "DOOMEternal").mkdir()
    storemod.open_store(root, fresh=True)
    assert (common / "DOOMEternal").resolve() in oodle._hint_dirs
    storemod.forget(root)
