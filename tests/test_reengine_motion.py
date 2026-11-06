"""Posing RE Engine models: bone weights in meshes, .motlist/.mot motions, finding a model's
animations in an extracted folder and in pak archives, skinning, the Pose tab and Collect."""

from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np
import pytest

from darkstl import library
from darkstl.importers import open_model
from darkstl.importers.reengine import REEngineImporter, files
from darkstl.importers.reengine.animation import bone_hashes
from darkstl.importers.reengine.formats import mesh as meshfmt
from darkstl.importers.reengine.formats import motion as motfmt
from darkstl.importers.reengine.formats import pak as pakfmt
from darkstl.model import MeshOptions

import synth_re as S
from test_app import home, make_service, wait  # noqa: F401 - fixture

PL = "natives/x64/sectionroot/character/player/pl0000/pl0000.mesh.1808312334"
PL_ANIM = "natives/x64/sectionroot/animation/player/pl00/list"
CH = "natives/stm/_chainsaw/character/ch/cha0/cha000/00/cha000_00.mesh.221108797"
CH_ANIM = "natives/stm/_chainsaw/animation/ch/cha0/motlist"

S45 = math.sin(math.pi / 4)
TURN = [0.0, 0.0, S45, S45]  # 90 degrees about Z


def arm_mesh(version: int) -> bytes:
    """An upright square on two bones: the root at the origin and an arm whose joint is half way up,
    which moves the top row of vertices."""
    sub = S.quad(1.0, 0.0, 0, 0, segments=2)
    top = sub.positions[:, 1] > 0.75
    joints = np.zeros((len(sub.positions), 8), np.uint8)
    weights = np.zeros((len(sub.positions), 8), np.uint8)
    joints[top, 0] = 1
    weights[:, 0] = 255
    sub.joints, sub.weights = joints, weights
    arm = np.eye(4)
    arm[:3, 3] = [0.0, 0.5, 0.0]
    mesh, _ = S.build_mesh([sub], ["body"], version, bones=["root", "arm"], parents=[-1, 0], locals_=[np.eye(4), arm])
    return mesh


def wave(version: int, rotation: str = "10bit", translation: str = "full", name: str = "wave") -> bytes:
    """The arm turning a quarter turn about Z over ten frames (its joint staying put)."""
    return S.build_mot(name, [
        ("arm", {"r": S.SynthTrack([0, 10], np.array([[0, 0, 0, 1.0], TURN]), rotation),
                 "t": S.SynthTrack([0, 10], np.array([[0, 0.5, 0], [0, 0.5, 0]]), translation)}),
        ("root", {"r": S.SynthTrack([0], np.array([[0, 0, 0, 1.0]]), "3comp")}),
    ], 10.0, version)


def write_re2(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "re2.exe").write_bytes(b"MZ")
    (root / PL).parent.mkdir(parents=True)
    (root / PL).write_bytes(arm_mesh(S.RE2_MESH))
    anim = root / PL_ANIM
    anim.mkdir(parents=True)
    still = S.build_mot("stand", [("arm", {"r": S.SynthTrack([0], np.array([[0, 0, 0, 1.0]]), "3comp")})], 0.0)
    (anim / "pl0000_move.motlist.85").write_bytes(S.build_motlist("pl0000_move", [wave(S.RE2_MOT), still]))
    # a list for other bones only, and another character's: neither is offered
    jaw = S.build_mot("talk", [("jaw", {"r": S.SynthTrack([0], np.array([[0, 0, 0, 1.0]]), "3comp")})], 5.0)
    (anim / "pl0000_face.motlist.85").write_bytes(S.build_motlist("pl0000_face", [jaw]))
    other = root / "natives/x64/sectionroot/animation/enemy/em0000/list"
    other.mkdir(parents=True)
    (other / "em0000_walk.motlist.85").write_bytes(S.build_motlist("em0000_walk", [wave(S.RE2_MOT)]))
    return root


@pytest.fixture
def re2_dir(tmp_path):
    root = write_re2(tmp_path / "RE2")
    yield root
    files.forget(root)


def _scan(root: Path) -> None:
    imp = REEngineImporter()
    imp.scan(root, "")


# ---------------------------------------------------------------------------
# formats


def test_bone_hashes_are_murmur3_of_the_utf16_name():
    assert bone_hashes(["arm", "Hip", "root"]).tolist() == [S.murmur("arm"), S.murmur("Hip"), S.murmur("root")]
    assert S.murmur("") == 0x81F16F39  # MurmurHash3_x86_32 of nothing with seed 0xFFFFFFFF


def test_mesh_weights_name_bones_through_the_skin_list():
    mesh = meshfmt.read_mesh(arm_mesh(S.RE2_MESH))
    assert [b.name for b in mesh.bones] == ["root", "arm"] and [b.parent for b in mesh.bones] == [-1, 0]
    assert mesh.skin_bones == [0, 1] and mesh.joint_count == 8
    (sub,) = meshfmt.submeshes(mesh)
    top = sub.positions[:, 1] > 0.75
    assert sub.joints.shape == (9, 8)
    assert sub.joints[top, 0].tolist() == [1, 1, 1] and sub.joints[~top, 0].tolist() == [0] * 6
    assert (sub.joints[:, 1:] == -1).all() and np.allclose(sub.weights.sum(axis=1), 1)


def test_ten_bit_rotations_decode_as_the_bits_say():
    """A key packed by hand: x, y and z in 10 bits each, scaled into the unpack ranges."""
    raw = struct.pack("<I", 1023 | (0 << 10) | (512 << 20))
    unpack = np.array([0.5, 1.0, 0.25, 0, -0.25, 0.0, 0.1, 0])
    layout = motfmt._ROTATIONS["10bit"]
    (xyz,) = motfmt._unpack(raw, 1, layout, unpack)
    assert xyz.tolist() == pytest.approx([0.25, 0.0, 0.25 * 512 / 1023 + 0.1])
    # 48-bit vectors are read big-endian with x in the low bits (as REE-Lib and RevilLib read them)
    raw = (65535 | (0 << 16) | (32768 << 32)).to_bytes(6, "big")
    (v,) = motfmt._unpack(raw, 1, motfmt._VECTORS["16bit"], np.array([2.0, 1, 1, -1, 0, 0, 0, 0]))
    assert v.tolist() == pytest.approx([1.0, 0.0, 32768 / 65535])


@pytest.mark.parametrize("version,listv,rotation,translation", [
    (S.RE2_MOT, S.RE2_MOTLIST, "10bit", "full"),
    (S.RE2_MOT, S.RE2_MOTLIST, "3comp", "full"),
    (S.RE4_MOT, S.RE4_MOTLIST, "10bit", "16bit"),
    (S.RE4_MOT, S.RE4_MOTLIST, "16bit", "full"),
    (492, 524, "3comp", "16bit"),
])
def test_motlists_and_their_tracks_round_trip(version, listv, rotation, translation):
    data = S.build_motlist("pl0000_move", [wave(version, rotation, translation), wave(version, name="again")], listv)
    ml = motfmt.read_motlist(data)
    assert ml.version == listv and ml.name == "pl0000_move"
    assert [m.name for m in ml.motions] == ["wave", "again"] and ml.motions[0].version == version
    head = ml.motions[0]
    assert head.frames == 11 and head.fps == 60 and head.duration == pytest.approx(10 / 60)
    assert motfmt.clip_hashes(data, head) == [S.murmur("arm"), S.murmur("root")]
    arm, root = motfmt.read_clips(data, head)
    assert arm.hash == S.murmur("arm") and arm.scale is None and root.translation is None
    assert arm.rotation.frames.tolist() == [0, 10]
    tol = 2e-3 if rotation != "3comp" else 1e-6
    assert np.allclose(arm.rotation.at(0), [0, 0, 0, 1], atol=tol)
    assert np.allclose(arm.rotation.at(10), TURN, atol=tol)
    half = arm.rotation.at(5)  # half way: an eighth of a turn
    assert np.allclose(half, [0, 0, math.sin(math.pi / 8), math.cos(math.pi / 8)], atol=tol)
    assert np.allclose(arm.translation.at(7), [0, 0.5, 0], atol=1e-4)
    assert np.allclose(root.rotation.at(3), [0, 0, 0, 1])


def test_a_lone_mot_file_and_other_files():
    mot = wave(S.RE4_MOT)
    assert motfmt.is_mot(mot) and not motfmt.is_motlist(mot)
    head = motfmt.read_header(mot)
    assert head.name == "wave" and head.clip_count == 2
    with pytest.raises(motfmt.FormatError):
        motfmt.read_motlist(mot)
    with pytest.raises(motfmt.FormatError):
        motfmt.read_header(b"MESH" + b"\0" * 60)


# ---------------------------------------------------------------------------
# finding and using a model's animations


def test_motion_files_are_found_by_character_id():
    assert files.character_id("pl0000_body") == "pl0000" and files.character_id("cha000_00") == "cha000"
    assert files.character_id("ch01_1201_upperbody") == "ch01_1201" and files.character_id("_x") is None
    assert files.character_id("ch03_0200_computeskinning00") == "ch03_0200"
    score = files._id_score
    assert score("pl0000", "natives/x64/animation/player/pl0000/list/a.motlist.85") == 3
    assert score("cha000", "natives/stm/_chainsaw/animation/ch/cha0/motlist/cha0_move.motlist.663") == 2
    assert score("pl1001", "natives/stm/ch8/animation/player/pl1000/motlist/pl1000_x.motlist.524") == 1
    assert score("em0050", "natives/stm/animation/enemy/em0000/em0000.motlist.524") == 0
    assert score("ch01", "natives/stm/_ge/character/ch/animation/ch01_1000/motlist/ch01_1000_x.motlist.486") == 3
    # Village: the character's own lists before the rest of its series
    assert score("ch03_0200", "natives/stm/character/ch/prefab/gallery/ch03_0200/ch03_0200.mot.458") == 3
    assert score("ch03_0200", "natives/stm/character/ch/animation/ch03_0100/motlist/ch03_0100general.motlist.486") == 2
    assert score("ch03_0200", "natives/stm/character/ch/animation/ch05_0200/motlist/ch05_0200general.motlist.486") == 0


def test_an_extracted_character_is_posed_by_its_animations(re2_dir):
    _scan(re2_dir)
    model = open_model(re2_dir / PL)
    anims = model.animations
    assert anims is not None and len(anims.skeleton) == 2
    infos = anims.list()
    assert [(a.moves, a.fixed) for a in infos] == [(1, True), (2, False)]  # "stand" holds one pose of one bone
    listed = [(a.name, a.frames, a.source) for a in infos]
    assert listed == [("pl0000_move / stand", 1, "x64/sectionroot/animation/player/pl00/list/pl0000_move.motlist.85"),
                      ("pl0000_move / wave", 11, "x64/sectionroot/animation/player/pl00/list/pl0000_move.motlist.85")]
    rest = model.build_mesh()
    assert np.allclose(model.build_mesh(MeshOptions(pose=("pl0000_move / wave", 0))).vertices, rest.vertices, atol=1e-5)
    posed = model.build_mesh(MeshOptions(pose=("pl0000_move / wave", 10)))
    # the top row (game Y 1, export Z 1) swung about the joint half way up to point along -X
    lo, hi = posed.bounds()
    assert lo[0] == pytest.approx(-0.5, abs=2e-3) and hi[2] == pytest.approx(1.5, abs=2e-3)
    assert hi[0] == pytest.approx(1.0) and lo[2] == pytest.approx(0.0)
    assert posed.n_faces == rest.n_faces
    # frames past the end are the last one; a name that isn't there is refused
    late = model.build_mesh(MeshOptions(pose=("pl0000_move / wave", 99)))
    assert np.allclose(late.vertices, posed.vertices)
    with pytest.raises(Exception, match="No animation named"):
        model.build_mesh(MeshOptions(pose=("nothing", 0)))


def test_a_model_without_weights_or_motions_has_none(re2_dir):
    _scan(re2_dir)
    plain, _ = S.build_mesh([S.quad(1.0)], ["m"], S.RE2_MESH, bones=["root"])
    path = re2_dir / "natives/x64/sectionroot/character/player/pl0000/pl0001.mesh.1808312334"
    path.write_bytes(plain)
    assert open_model(path).animations is None
    lonely = re2_dir / "natives/x64/sectionroot/character/enemy/em9000/em9000.mesh.1808312334"
    lonely.parent.mkdir(parents=True)
    lonely.write_bytes(arm_mesh(S.RE2_MESH))
    assert open_model(lonely).animations is None


def test_collect_takes_the_motion_files_that_fit(re2_dir):
    _scan(re2_dir)
    related = REEngineImporter().related_files(re2_dir / PL)
    anims = [f.path.relative_to(re2_dir).as_posix() for f in related if f.kind == "animation"]
    assert anims == [f"{PL_ANIM}/pl0000_move.motlist.85"]
    assert not [f for f in REEngineImporter().related_files(re2_dir / PL, animations=False) if f.kind == "animation"]


def test_listings_are_kept_in_the_cache(re2_dir, tmp_path):
    imp = REEngineImporter()
    imp.cache = tmp_path / "cache"
    imp.scan(re2_dir, "")
    assert len(imp.load(re2_dir / PL).animations.list()) == 2
    saved = list((tmp_path / "cache" / "motions").glob("*.json"))
    assert len(saved) == 1 and "pl0000_move" in saved[0].read_text()


def test_a_packed_re4_character_is_posed_from_the_paks(tmp_path):
    """Resident Evil 4 still in its paks, named by a file list in the folder: the character's motlist is
    found in the family folder (cha0) and poses it."""
    root = tmp_path / "RE4"
    root.mkdir()
    (root / "re4.exe").write_bytes(b"MZ")
    motlist = S.build_motlist("cha0_move", [wave(S.RE4_MOT, "16bit", "16bit")], S.RE4_MOTLIST)
    names = [CH, f"{CH_ANIM}/cha0_move.motlist.663"]
    (root / "re_chunk_000.pak").write_bytes(S.build_pak(list(zip(names, [arm_mesh(S.RE4_MESH), motlist])),
                                                       pakfmt.COMPRESSION_DEFLATE))
    (root / "re4.list").write_text("\n".join(names))
    try:
        entries, _ = library.scan_folder(root)
        (entry,) = [e for e in entries if e.stem == "cha000_00"]
        model = open_model(entry.path)
        assert [a.name for a in model.animations.list()] == ["cha0_move / wave"]
        lo, hi = model.build_mesh(MeshOptions(pose=("cha0_move / wave", 10))).bounds()
        assert lo[0] == pytest.approx(-0.5, abs=2e-3) and hi[2] == pytest.approx(1.5, abs=2e-3)
        related = REEngineImporter().related_files(entry.path)
        assert [f.save_as for f in related if f.kind == "animation"] == [f"{CH_ANIM}/cha0_move.motlist.663"]
    finally:
        files.forget(root)


def test_the_pose_tab_lists_re_animations(home, re2_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(re2_dir)})["job"])
    (item,) = [i for i in s.call("models_query", {"game": "Resident Evil 2"})["items"] if i["stem"] == "pl0000"]
    opened = s.call("model_open", {"id": item["id"]})
    assert opened["animated"] is True
    r = s.call("model_animations", {"sid": opened["sid"]})
    assert r["bones"] == 2 and [a["name"] for a in r["animations"]] == ["pl0000_move / stand", "pl0000_move / wave"]
    assert ".motlist" in r["note"]


def test_a_mesh_bound_whole_to_a_few_bones_is_called_a_figure():
    from darkstl.importers.reengine.animation import REAnimations
    from darkstl.importers.reengine.formats.mesh import Bone

    names = ["root", "Hip", "Spine", "Neck", "Head", "L_Thigh", "L_Shin", "R_Thigh", "R_Shin", "Pocket"]
    parents = [-1, 0, 1, 2, 3, 1, 5, 1, 7, 1]
    bones = [Bone(n, p, np.eye(4, dtype=np.float32)) for n, p in zip(names, parents)]
    figure = REAnimations(bones, [], None, used_bones={1, 9})
    assert figure.moving_bones == [0, 1, 9]  # the bones used and their ancestors
    assert "bound whole to 2 of its 10 bones (Hip, Pocket)" in figure.rigid_description()
    assert figure.note == REAnimations.note  # the importer decides whether the note replaces the usual one
    skinned = REAnimations(bones, [], None, used_bones=set(range(1, 10)))
    assert skinned.rigid_description() is None and skinned.moving_bones == list(range(10))
    unknown = REAnimations(bones, [], None)
    assert unknown.rigid_description() is None and unknown.moving_bones == list(range(10))
