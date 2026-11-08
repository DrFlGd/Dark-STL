"""Posing Unreal 4 skeletal meshes with the game's AnimSequence assets: skin weights, the Skeleton, finding a
mesh's animations, Unreal's key codecs in each engine's layout, model packages and Collect."""

import math
import shutil

import numpy as np
import pytest

from darkstl.importers import open_model
from darkstl.importers.borderlands.ue4 import vfs
from darkstl.importers.returnal import GAME, ReturnalImporter
from darkstl.model import MeshOptions

import synth_ue4 as S
import synth_ue4_anim as A

POS = np.array([[0, 0, 0], [100, 0, 0], [0, 100, 0], [100, 100, 0], [0, 0, 50], [100, 0, 50]], float)
NRM = np.tile([0.0, 0.0, 1.0], (6, 1))
UV = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [0, 0.5], [1, 0.5]], float)
IDX = [0, 1, 2, 2, 1, 3, 0, 4, 1, 1, 4, 5]
IDENTITY = (0, 0, 0, 1)
S45 = math.sin(math.pi / 4)
# the skeleton has a bone the mesh doesn't; the mesh's bones are matched to it by name
SKELETON = [("root", -1, IDENTITY, (0, 0, 0)), ("pelvis", 0, IDENTITY, (0, 0, 25)), ("arm", 1, IDENTITY, (0, 0, 25)),
            ("tail", 1, IDENTITY, (0, -10, 0))]
MESH_BONES = SKELETON[:3]
JOINTS = [[1, 0, 0, 0]] * 4 + [[2, 0, 0, 0]] * 2  # section bone map indices: pelvis, arm
WEIGHTS = [[255, 0, 0, 0]] * 6
SKEL_PKG = "/Game/Characters/Selene/SK_Selene_Skeleton"
TURN = [IDENTITY, (0, 0, math.sin(math.pi / 8), math.cos(math.pi / 8)), (0, 0, S45, S45)]  # 0, 45, 90 degrees about Z
RISE = [(0, 0, 35)] * 3  # the pelvis 10 cm above its reference place


def _packages(engine=426, unversioned=False, rotation_format=A.ACF_FLOAT96, per_track=False, acl=False,
              folder="Characters/Selene/Animations", prefix="A_", own=True):
    out = {}
    anim_engine, engine = engine, (420 if engine == 420 else 426)  # the synthetic meshes are 4.20 or 4.26 ones
    b = S.Ue4Package(SKEL_PKG, engine, unversioned=unversioned)
    e = b.add_export("Skeleton", "SK_Selene_Skeleton")
    b.set_data(e, A.skeleton_data(b, SKELETON))
    out["Characters/Selene/SK_Selene_Skeleton"] = b

    material = 0
    if not unversioned:  # the suit's material and textures, as in the Returnal importer's tests
        from test_returnal import _packages as textured

        out.update({k: v for k, v in textured(engine).items() if "/Textures/" in k or "/Materials/" in k})
    b = S.Ue4Package("/Game/Characters/Selene/Meshes/SK_Selene_Suit", engine, unversioned=unversioned)
    if not unversioned:
        material = b.import_object("/Game/Characters/Selene/Materials/MI_Selene_Suit", "MaterialInstanceConstant",
                                   "MI_Selene_Suit")
    skel = b.import_object(SKEL_PKG, "Skeleton", "SK_Selene_Skeleton")
    e = b.add_export("SkeletalMesh", "SK_Selene_Suit")
    b.set_data(e, S.skeletal_data(b, [material], POS, NRM, UV, IDX, [(0, 0, 4, (0, 1, 2), 0, 6)], bones=MESH_BONES,
                                  joints=JOINTS, weights=WEIGHTS, skeleton=skel))
    out["Characters/Selene/Meshes/SK_Selene_Suit"] = b

    def anim(path, stem, track_bones, tracks, frames=3, length=1 / 15, **kw):
        b = S.Ue4Package(f"/Game/{path}/{stem}", anim_engine, unversioned=unversioned)
        skel = b.import_object(SKEL_PKG, "Skeleton", "SK_Selene_Skeleton")
        e = b.add_export("AnimSequence", stem)
        b.set_data(e, A.anim_data(b, skel, track_bones, tracks, frames, length, rotation_format, per_track, **kw))
        out[f"{path}/{stem}"] = b

    # tracks keyed by skeleton bone index: arm 2, pelvis 1
    if own:
        anim(folder, f"{prefix}Selene_Wave", [2, 1], [(None, TURN), (RISE, None)])
        anim(folder, f"{prefix}Selene_Idle", [1], [([(0, 0, 25)], [IDENTITY])], frames=1, length=0)
        anim(f"{folder}/Additive", f"{prefix}Selene_Lean", [2], [(None, [(0, 0, S45, S45)])], frames=1, length=0,
             additive=True)
    if acl:
        b = S.Ue4Package("/Game/Characters/Selene/Animations/A_Selene_Run", engine, unversioned=unversioned)
        skel = b.import_object(SKEL_PKG, "Skeleton", "SK_Selene_Skeleton")
        e = b.add_export("AnimSequence", "A_Selene_Run")
        b.set_data(e, A.acl_anim_data(b, skel, 10, 1.0))
        out["Characters/Selene/Animations/A_Selene_Run"] = b
    # another skeleton's animation: not offered for this mesh
    b = S.Ue4Package("/Game/Enemies/Severed/Animations/A_Severed_Walk", engine, unversioned=unversioned)
    other = b.import_object("/Game/Enemies/Severed/SK_Severed_Skeleton", "Skeleton", "SK_Severed_Skeleton")
    e = b.add_export("AnimSequence", "A_Severed_Walk")
    b.set_data(e, A.anim_data(b, other, [0], [(None, [IDENTITY])], 1, 0))
    out["Enemies/Severed/Animations/A_Severed_Walk"] = b
    return out


def _game(tmp_path, **kw):
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64" / "Returnal-Win64-Shipping.exe").write_bytes(
        b"MZ" + bytes(64) + "++UE4+Release-4.26".encode("utf-16-le"))
    files = {}
    for path, b in _packages(**kw).items():
        uasset, uexp, ubulk = b.build()
        files[f"Returnal/Content/{path}.uasset"] = uasset
        files[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            files[f"Returnal/Content/{path}.ubulk"] = ubulk
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(files, compress=True))
    vfs.forget(root)
    return root


def _suit(tmp_path, **kw):
    root = _game(tmp_path, **kw)
    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    by = {e.stem: e for e in imp.scan(root, GAME)}
    return imp, by["SK_Selene_Suit"].path


def _vertex(mesh, near) -> np.ndarray:
    d = np.linalg.norm(mesh.vertices - np.asarray(near, float), axis=1)
    return mesh.vertices[int(np.argmin(d))]


def _check_wave(m, tol=1e-4):
    names = [i.name for i in m.animations.list()]
    assert names == ["A_Selene_Idle", "A_Selene_Lean", "A_Selene_Wave"]  # not the other skeleton's
    wave = m.animations.info("A_Selene_Wave")
    assert wave.frames == 3 and abs(wave.fps - 30) < 1e-3 and wave.moves == 2
    bind = m.build(MeshOptions())
    assert np.allclose(_vertex(bind, (1, 0, 0.5)), (1, 0, 0.5))
    # frame 0: only the pelvis' rise; frame 2: the arm turned 90 degrees about Z as well
    first = m.build(MeshOptions(pose=("A_Selene_Wave", 0)))
    assert np.allclose(_vertex(first, (1, 0, 0.6)), (1, 0, 0.6), atol=tol)
    assert np.allclose(_vertex(first, (0, 0, 0.1)), (0, 0, 0.1), atol=tol)
    last = m.build(MeshOptions(pose=("A_Selene_Wave", 2)))
    # Unreal's +Y is the export frame's -Y (it is mirrored), in metres
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=tol)
    mid = m.build(MeshOptions(pose=("A_Selene_Wave", 1)))
    assert np.allclose(_vertex(mid, (S45, -S45, 0.6)), (S45, -S45, 0.6), atol=max(tol, 1e-3))
    assert len(last.faces) == len(bind.faces)


def test_poses_with_the_skeletons_animations(tmp_path):
    imp, path = _suit(tmp_path)
    m = imp.load(path)
    assert m.animations is not None and m.rig is not None
    assert m.rig.names == ["root", "pelvis", "arm"]
    _check_wave(m)
    # a held pose and an additive one (shown on the bind pose)
    idle = m.build(MeshOptions(pose=("A_Selene_Idle", 0)))
    assert np.allclose(_vertex(idle, (1, 0, 0.5)), (1, 0, 0.5), atol=1e-5)
    assert m.animations.info("A_Selene_Idle").fixed
    lean = m.animations.info("A_Selene_Lean")
    assert "additive" in lean.source
    leaned = m.build(MeshOptions(pose=("A_Selene_Lean", 0)))
    assert np.allclose(_vertex(leaned, (0, -1, 0.5)), (0, -1, 0.5), atol=1e-5)
    assert list((tmp_path / "cache").glob("unreal-animations-*.json"))  # kept for next time
    # the bind-pose mesh carries its weights for model packages
    joints, weights = m.build(MeshOptions()).metadata["skin"]
    assert joints.shape[0] == len(m.build(MeshOptions()).vertices) and weights.max() == pytest.approx(1)


@pytest.mark.parametrize("engine", [420, 422, 424, 425, 426])
def test_each_engines_layout(tmp_path, engine):
    _imp, path = _suit(tmp_path, engine=engine)
    _check_wave(open_model(path))


def test_interval_quantised_rotations(tmp_path):
    _imp, path = _suit(tmp_path, rotation_format=A.ACF_INTERVAL32)
    _check_wave(open_model(path), tol=2e-3)


@pytest.mark.parametrize("engine", [420, 426])
def test_per_track_compression(tmp_path, engine):
    _imp, path = _suit(tmp_path, engine=engine, per_track=True)
    _check_wave(open_model(path))


def test_acl_animations_are_named_not_offered(tmp_path):
    _imp, path = _suit(tmp_path, acl=True)
    m = open_model(path)
    assert "A_Selene_Run" not in [i.name for i in m.animations.list()]
    (name, why), = m.animations.unreadable()
    assert name == "A_Selene_Run" and "ACL" in why
    assert "1 of its skeleton's animations are compressed with the ACL plugin" in m.animations.note


def test_animations_not_named_like_animations_are_found(tmp_path):
    """Returnal's animations may go without A_/AS_ and outside an animation folder: when the names find none for
    a skeleton, every package that may be one is looked through (once: what's found is kept)."""
    imp, path = _suit(tmp_path, folder="Gameplay/Player/Selene/Moves", prefix="")
    m = imp.load(path)
    assert [i.name for i in m.animations.list()] == ["Selene_Idle", "Selene_Lean", "Selene_Wave"]
    last = m.build(MeshOptions(pose=("Selene_Wave", 2)))
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=1e-4)
    import json

    kept, = (tmp_path / "cache").glob("unreal-animations-*.json")
    data = json.loads(kept.read_text("utf-8"))
    assert data["wide"] is True
    assert any(k.endswith("selene_wave.uasset") for k in data["files"])


def test_says_why_no_animations_were_found(tmp_path):
    """None made for the model's skeleton: the Pose tab says what was looked through and what was found."""
    imp, path = _suit(tmp_path, own=False)
    m = imp.load(path)
    assert m.animations.list() == []
    note = m.animations.note
    assert "No animations made for its skeleton (SK_Selene_Skeleton) were found" in note
    assert "every package that may be one" in note
    assert "1 is an animation, made for other skeletons (SK_Severed_Skeleton: 1)" in note


def test_says_why_a_model_cant_be_posed(tmp_path):
    """A model with no animations to list still says why on the Pose tab."""
    from types import SimpleNamespace

    from darkstl.app.config import Config
    from darkstl.app.service import Service

    imp, path = _suit(tmp_path, own=False)
    service = Service(Config(), tmp_path / "service-cache")
    service.sessions["a"] = SimpleNamespace(model=imp.load(path))
    got = service.call("model_animations", {"sid": "a"})
    assert got["animations"] == [] and "SK_Severed_Skeleton: 1" in got["note"]
    unposable = SimpleNamespace(animations=None, pose_note="It's a static mesh: it has no bones to pose.")
    service.sessions["b"] = SimpleNamespace(model=unposable)
    assert service.call("model_animations", {"sid": "b"})["note"].startswith("It's a static mesh")


def test_unversioned_without_type_mappings(tmp_path, monkeypatch):
    """The real game cooks without property names and Dark STL has no .usmap for it: the skeleton, mesh and
    animations are still found by their own data, the frame rate taken as Unreal's default 30."""
    from darkstl.importers.borderlands.ue4 import unversioned

    monkeypatch.setattr(unversioned, "_settings_dir", lambda: tmp_path / "no-settings")
    unversioned.forget_mappings()
    _imp, path = _suit(tmp_path, unversioned=True)
    m = open_model(path)
    names = [i.name for i in m.animations.list()]
    assert "A_Selene_Wave" in names
    wave = m.animations.info("A_Selene_Wave")
    assert wave.frames == 3 and abs(wave.fps - 30) < 1e-6
    last = m.build(MeshOptions(pose=("A_Selene_Wave", 2)))
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=1e-4)


def test_model_package_keeps_the_animations(tmp_path):
    from darkstl import package
    from darkstl.model import CatalogEntry

    imp, path = _suit(tmp_path)
    m = open_model(path)
    root = tmp_path / "library" / "Returnal packages"
    store = package.AssetStore(root)
    entry = CatalogEntry(GAME, "Characters", "Selene", "SK_Selene_Suit", "Selene Suit", path, imp.key)
    index = package.write_package(root, entry, "Characters/SK_Selene_Suit.uasset", [m], store)
    store.save()
    shutil.rmtree(tmp_path / "Returnal")
    vfs.forget(tmp_path / "Returnal")
    p = open_model(index)
    assert "A_Selene_Wave" in [i.name for i in p.animations.list()]
    last = p.build(MeshOptions(pose=("A_Selene_Wave", 2)))
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=1e-4)


def test_collect_copies_the_animations(tmp_path):
    imp, path = _suit(tmp_path)
    rel = imp.related_files(path)
    names = {r.save_as.rsplit("/", 1)[-1] for r in rel}
    assert {"SK_Selene_Skeleton.uasset", "A_Selene_Wave.uasset", "A_Selene_Wave.uexp"} <= names
    assert "A_Severed_Walk.uasset" not in names
    assert {r.kind for r in rel if "A_Selene" in r.save_as} == {"animation"}
    assert all("A_Selene" not in r.save_as for r in imp.related_files(path, animations=False))
    out = tmp_path / "library" / "Returnal"
    for r in rel:
        dest = out / r.save_as
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.extract())
    shutil.rmtree(tmp_path / "Returnal")  # the game is uninstalled: the collected files are all the app has
    vfs.forget(tmp_path / "Returnal")
    vfs.forget(out)
    loose = {e.stem: e for e in imp.scan(out, GAME)}
    _check_wave(open_model(loose["SK_Selene_Suit"].path))


def test_a_game_that_leaves_out_the_skeleton_guid(tmp_path, monkeypatch):
    """Animations are read past the skeleton's GUID every animation asset writes, and without it too."""
    monkeypatch.setattr(A, "SKELETON_GUID", b"")
    imp, path = _suit(tmp_path)
    _check_wave(imp.load(path))


def test_animations_kept_as_unreadable_before_are_read_again(tmp_path):
    import json

    imp, path = _suit(tmp_path)
    imp.load(path).animations.list()
    kept = next((tmp_path / "cache").glob("unreal-animations-*.json"))
    data = json.loads(kept.read_text())
    assert data["info"]
    for row in data["info"].values():  # as 0.36.0-0.38.0 kept them
        row[:] = [0, 0.0, None, "", "not an animation's data", []]
    del data["info_version"]
    kept.write_text(json.dumps(data))
    vfs.forget(tmp_path / "Returnal")
    _check_wave(imp.load(path))


def test_says_which_bones_a_track_names_that_the_skeleton_doesnt_have(tmp_path):
    from darkstl.errors import FormatError
    from darkstl.importers.borderlands.ue4 import anim
    from darkstl.importers.borderlands.ue4 import vfs as V
    from darkstl.importers.borderlands.ue4.animset import UEAnimations

    imp, path = _suit(tmp_path)
    m = imp.load(path)
    gf = V.game_files(tmp_path / "Returnal")
    wave = next(v for v in gf.assets() if v.endswith("A_Selene_Wave.uasset"))
    pkg = gf.open_package(wave)
    e = pkg.main_export(("AnimSequence",))
    with pytest.raises(FormatError, match=r"1 of its 2 tracks names bone 2, and the skeleton has 2"):
        anim.read_anim(pkg, e, 2)
    assert isinstance(m.animations, UEAnimations)
    m.animations.entries()["A_Selene_Idle"].error = "a track for a bone the skeleton doesn't have: x"
    m.animations.entries()["A_Selene_Lean"].error = "a track for a bone the skeleton doesn't have: x"
    m.animations.entries()["A_Selene_Wave"].error = "it is broken"
    assert "3 couldn't be read (a track for a bone the skeleton doesn't have: x; 2 of them, and 1 for 1 other " \
           "reason)." in m.animations.note
