"""Posing Borderlands 2 (Unreal 3) skeletal meshes with the game's AnimSets: skin weights, finding the AnimSets
whose bones are a mesh's (read once, from the package each belongs to), Unreal 3's key layouts and Borderlands
2's polar-encoded rotations, Unreal 3's rotation convention, model packages and Collect."""

import math
import shutil

import numpy as np
import pytest

from darkstl.importers import open_model
from darkstl.importers.borderlands import BorderlandsImporter, animsets, files
from darkstl.importers.borderlands.ue4 import anim as codecs
from darkstl.model import MeshOptions

import synth_ue3 as S

POS = np.array([[0, 0, 0], [100, 0, 0], [0, 100, 0], [100, 100, 0], [0, 0, 50], [100, 0, 50]], float)
NRM = np.tile([0.0, 0.0, 1.0], (6, 1))
UV = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [0, 0.5], [1, 0.5]], float)
IDX = [0, 1, 2, 2, 1, 3, 0, 4, 1, 1, 4, 5]
IDENTITY = (0, 0, 0, 1)
S45 = math.sin(math.pi / 4)
# the mesh has a bone its AnimSet doesn't key (it stays put on its parent)
BONES = [("Root", -1, IDENTITY, (0, 0, 0)), ("Pelvis", 0, IDENTITY, (0, 0, 25)), ("Arm", 1, IDENTITY, (0, 0, 25)),
         ("Hair", 2, IDENTITY, (0, 0, 10))]
CHUNK_BONES = (1, 2)  # the chunk's bone map: pelvis, arm
INFLUENCES = [((0, 0, 0, 0), (255, 0, 0, 0))] * 4 + [((1, 0, 0, 0), (255, 0, 0, 0))] * 2
TRACKS = ["Root", "Pelvis", "Arm"]
TURN = [IDENTITY, (0, 0, math.sin(math.pi / 8), math.cos(math.pi / 8)), (0, 0, S45, S45)]  # 0, 45, 90 degrees about Z
RISE = [(0, 0, 35)] * 3  # the pelvis 10 cm above its place on the mesh
DRIFT = [(40, 30, 0)]  # the root across the floor: left out


def conj(q):
    """How Unreal 3 keeps the rotation keys of every bone but the root."""
    return (-q[0], -q[1], -q[2], q[3])


def _material(b: S.PackageBuilder) -> int:
    dif = b.add_export("Texture2D", "Siren_Dif", S.texture_data(b, 8, 8, S.bc1_solid(8, 8, 0xF800)))
    return b.add_export("Material", "Mat_Siren", S.material_data(b, [dif]))


def mesh_package() -> bytes:
    b = S.PackageBuilder()
    mat = _material(b)
    mesh = b.add_export("SkeletalMesh", "SK_Siren", b"")
    b.set_data(mesh, S.skeletal_data(b, [mat], POS, NRM, UV, IDX, [(0, 0, 4)], bones=BONES, chunk_bones=CHUNK_BONES,
                                     influences=INFLUENCES))
    # a creature of other bones, and a mesh whose bones are turned in its bind pose
    skag = b.add_export("SkeletalMesh", "SK_Skag", b"")
    b.set_data(skag, S.skeletal_data(b, [mat], POS, NRM, UV, IDX, [(0, 0, 4)],
                                     bones=[("Root", -1), ("Jaw", 0), ("Tail", 0)], chunk_bones=(1, 2),
                                     influences=INFLUENCES))
    twisted = b.add_export("SkeletalMesh", "SK_Twisted", b"")
    b.set_data(twisted, S.skeletal_data(b, [mat], POS, NRM, UV, IDX, [(0, 0, 4)], bones=TWISTED,
                                        chunk_bones=(1, 2), influences=INFLUENCES))
    return b.build("lzo")


def _r(axis, degrees):
    a = math.radians(degrees) / 2
    v = np.asarray(axis, float) * math.sin(a)
    return (v[0], v[1], v[2], math.cos(a))


TWISTED = [("Root", -1, _r((0, 0, 1), 30), (5, 0, 0)), ("Spine", 0, _r((0, 1, 0), 90), (0, 0, 25)),
           ("Neck", 1, _r((1, 0, 0), 30), (10, 0, 0))]


def _sequence(b, owner, name, stream, offsets, frames=3, **kw) -> int:
    return b.add_export("AnimSequence", name, S.anim_sequence_data(b, name, frames, (frames - 1) / 30, stream, offsets,
                                                                   **kw), owner)


def add_siren_set(b: S.PackageBuilder, outer: int = 0) -> int:
    owner = b.add_export("AnimSet", "AnimSet_Siren", b"", outer)
    arm_keys = [conj(q) for q in TURN]
    wave = S.uniform_keys([(DRIFT, [IDENTITY]), (RISE, [IDENTITY]), ([(0, 0, 99)], arm_keys)],
                          rotation="ACF_PolarEncoded48")
    seqs = [_sequence(b, owner, "Wave", *wave, rotation="ACF_PolarEncoded48")]
    per_track = S.per_track_keys([(DRIFT, None), (RISE, None), (None, arm_keys)])
    seqs.append(_sequence(b, owner, "Wave_PT", *per_track, key="AKF_PerTrackCompression"))
    idle = S.uniform_keys([([(0, 0, 0)], [IDENTITY]), ([(0, 0, 25)], [IDENTITY]), ([(0, 0, 25)], [IDENTITY])])
    seqs.append(_sequence(b, owner, "Idle", *idle, frames=1))
    lean = S.uniform_keys([([(0, 0, 0)], [IDENTITY]), ([(0, 0, 0)], [IDENTITY]), ([(0, 0, 0)], [conj(TURN[2])])])
    seqs.append(_sequence(b, owner, "Lean", *lean, frames=1, additive=True))
    b.set_data(owner, S.anim_set_data(b, TRACKS, seqs, use_translation=["Pelvis"]))
    return owner


def anim_package() -> bytes:
    b = S.PackageBuilder()
    add_siren_set(b)
    # most of its bones aren't the siren's: not offered for her
    big = b.add_export("AnimSet", "AnimSet_Big", b"")
    keys = S.uniform_keys([([(0, 0, 0)], [IDENTITY])] * 5)
    b.set_data(big, S.anim_set_data(b, TRACKS + ["Wing_L", "Wing_R"], [_sequence(b, big, "Flap", *keys, frames=1)]))
    # the twisted mesh's bind pose, as an animation keys it: it must leave the mesh where it is
    hold = b.add_export("AnimSet", "AnimSet_Twisted", b"")
    keys = S.uniform_keys([([pos], [rot if i == 0 else conj(rot)]) for i, (_n, _p, rot, pos) in enumerate(TWISTED)])
    b.set_data(hold, S.anim_set_data(b, [t[0] for t in TWISTED], [_sequence(b, hold, "Hold", *keys, frames=1)],
                                     rotation_only=False))
    return b.build("lzo")


def skag_package() -> bytes:
    b = S.PackageBuilder()
    owner = b.add_export("AnimSet", "AnimSet_Skag", b"")
    keys = S.uniform_keys([([(0, 0, 0)], [IDENTITY])] * 3)
    b.set_data(owner, S.anim_set_data(b, ["Root", "Jaw", "Tail"], [_sequence(b, owner, "Bite", *keys, frames=1)]))
    return b.build("zlib")


def level_package() -> bytes:
    """A map that carries a copy of the siren's AnimSet (cooked in, as the game's maps do)."""
    b = S.PackageBuilder()
    top = b.add_export("Package", "Anim_Siren", b"", 0, export_flags=1)
    add_siren_set(b, top)
    mat = _material(b)
    rock = b.add_export("StaticMesh", "Rock", b"")
    b.set_data(rock, S.static_data(b, POS[:4], NRM[:4], UV[:4], IDX[:6], [(mat, 0, 2)]))
    return b.build("lzo")


def make_game(tmp_path):
    root = tmp_path / "Borderlands 2"
    cooked = root / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    (root / "Binaries" / "Win32").mkdir(parents=True)
    (root / "Binaries" / "Win32" / "Borderlands2.exe").write_bytes(b"MZ")
    (cooked / "CD_Siren.upk").write_bytes(mesh_package())
    (cooked / "Anim_Siren.upk").write_bytes(anim_package())
    (cooked / "Anim_Skag.upk").write_bytes(skag_package())
    (cooked / "Sanctuary_P.upk").write_bytes(level_package() + bytes(4096))  # the larger copy
    files._INDEXES.clear()
    animsets.forget()
    return root


def _game(tmp_path):
    root = make_game(tmp_path)
    imp = BorderlandsImporter()
    imp.cache = tmp_path / "cache"
    by = {e.label: e for e in imp.scan(root, files.GAME_BL2)}
    return imp, by


def _vertex(mesh, near) -> np.ndarray:
    d = np.linalg.norm(mesh.vertices - np.asarray(near, float), axis=1)
    return mesh.vertices[int(np.argmin(d))]


SIREN = ["AnimSet_Siren: Idle", "AnimSet_Siren: Lean", "AnimSet_Siren: Wave", "AnimSet_Siren: Wave_PT"]


def _check_wave(m, name="AnimSet_Siren: Wave", tol=2e-4):
    assert [i.name for i in m.animations.list()] == SIREN  # not the skag's, nor the one of mostly other bones
    wave = m.animations.info(name)
    assert wave.frames == 3 and abs(wave.fps - 30) < 1e-3 and wave.moves == 3
    bind = m.build(MeshOptions())
    assert np.allclose(_vertex(bind, (1, 0, 0.5)), (1, 0, 0.5))
    # frame 0: only the pelvis' rise (not the root's drift, nor the arm's own length); frame 2: the arm turned
    # 90 degrees about Z as well
    first = m.build(MeshOptions(pose=(name, 0)))
    assert np.allclose(_vertex(first, (1, 0, 0.6)), (1, 0, 0.6), atol=tol)
    assert np.allclose(_vertex(first, (0, 0, 0.1)), (0, 0, 0.1), atol=tol)
    last = m.build(MeshOptions(pose=(name, 2)))
    # Unreal's +Y is the export frame's -Y (it is mirrored), in metres
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=tol)
    mid = m.build(MeshOptions(pose=(name, 1)))
    assert np.allclose(_vertex(mid, (S45, -S45, 0.6)), (S45, -S45, 0.6), atol=max(tol, 1e-3))
    assert len(last.faces) == len(bind.faces)


def test_poses_with_the_games_animsets(tmp_path):
    imp, by = _game(tmp_path)
    m = imp.load(by["SK_Siren"].path)
    assert m.animations is not None and m.rig is not None
    assert m.rig.names == ["Root", "Pelvis", "Arm", "Hair"]
    _check_wave(m)
    _check_wave(m, "AnimSet_Siren: Wave_PT")  # the same keys, compressed per track
    idle = m.animations.info("AnimSet_Siren: Idle")
    assert idle.fixed and "Anim_Siren.AnimSet_Siren (Anim_Siren.upk)" in idle.source
    held = m.build(MeshOptions(pose=("AnimSet_Siren: Idle", 0)))
    assert np.allclose(_vertex(held, (1, 0, 0.5)), (1, 0, 0.5), atol=1e-5)
    lean = m.animations.info("AnimSet_Siren: Lean")
    assert "additive" in lean.source
    leaned = m.build(MeshOptions(pose=("AnimSet_Siren: Lean", 0)))
    assert np.allclose(_vertex(leaned, (0, -1, 0.5)), (0, -1, 0.5), atol=2e-4)
    assert list((tmp_path / "cache").glob("borderlands-animsets-*.json"))  # kept for next time
    # the bind-pose mesh carries its weights for model packages
    joints, weights = m.build(MeshOptions()).metadata["skin"]
    assert joints.shape[0] == len(m.build(MeshOptions()).vertices) and weights.max() == pytest.approx(1)
    with pytest.raises(Exception, match="No animation named"):
        m.build(MeshOptions(pose=("AnimSet_Skag: Bite", 0)))


def test_each_skeleton_gets_its_own(tmp_path):
    imp, by = _game(tmp_path)
    skag = imp.load(by["SK_Skag"].path)
    assert [i.name for i in skag.animations.list()] == ["AnimSet_Skag: Bite"]
    # the bind pose of a mesh whose bones are turned, keyed as Unreal 3 keys it, leaves it where it is
    twisted = imp.load(by["SK_Twisted"].path)
    assert [i.name for i in twisted.animations.list()] == ["AnimSet_Twisted: Hold"]
    bind = twisted.build(MeshOptions()).vertices
    held = twisted.build(MeshOptions(pose=("AnimSet_Twisted: Hold", 0))).vertices
    assert np.allclose(sorted(np.round(held, 4).tolist()), sorted(np.round(bind, 4).tolist()), atol=1e-4)


def test_once_the_animsets_are_known_meshes_without_any_have_no_pose_tab(tmp_path):
    imp, by = _game(tmp_path)
    cooked = tmp_path / "Borderlands 2" / "WillowGame" / "CookedPCConsole"
    (cooked / "Anim_Skag.upk").unlink()
    animsets.forget()
    # before the game's AnimSets are listed a mesh with bones is taken to have some
    assert imp.load(by["SK_Skag"].path).animations is not None
    assert imp.load(by["SK_Siren"].path).animations.list()  # lists them (and keeps the list)
    assert imp.load(by["SK_Skag"].path).animations is None
    animsets.forget()  # a fresh start reads the kept list
    assert imp.load(by["SK_Skag"].path).animations is None
    assert imp.load(by["SK_Siren"].path).animations is not None
    assert imp.load(by["Rock"].path).animations is None


def test_each_animset_is_read_from_its_own_package_else_a_copy(tmp_path):
    imp, by = _game(tmp_path)
    m = imp.load(by["SK_Siren"].path)
    assert {p.name for p in m.animations.files_used()} == {"Anim_Siren.upk"}
    cooked = tmp_path / "Borderlands 2" / "WillowGame" / "CookedPCConsole"
    (cooked / "Anim_Siren.upk").unlink()  # only the map's copy is left: the packages changed, so it is read again
    animsets.forget()
    m = imp.load(by["SK_Siren"].path)
    assert {p.name for p in m.animations.files_used()} == {"Sanctuary_P.upk"}
    _check_wave(m)


def test_says_why_an_animation_cant_be_read(tmp_path):
    root = make_game(tmp_path)
    b = S.PackageBuilder()
    owner = b.add_export("AnimSet", "AnimSet_Odd", b"")
    keys = S.uniform_keys([([(0, 0, 0)], [IDENTITY]), ([(0, 0, 25)], [IDENTITY, IDENTITY]), ([(0, 0, 25)], [IDENTITY])])
    b.set_data(owner, S.anim_set_data(b, TRACKS, [_sequence(b, owner, "Odd", *keys, rotation="ACF_Fixed48Max")]))
    (root / "WillowGame" / "CookedPCConsole" / "Anim_Odd.upk").write_bytes(b.build())
    imp = BorderlandsImporter()
    m = imp.load(next(e for e in imp.scan(root, files.GAME_BL2) if e.label == "SK_Siren").path)
    assert "AnimSet_Odd: Odd" in m.animations.names  # listed from its properties alone
    with pytest.raises(Exception, match="ACF_Fixed48Max"):
        m.build(MeshOptions(pose=("AnimSet_Odd: Odd", 0)))


def test_polar_encoded_rotations_round_trip():
    rng = np.random.default_rng(3)
    qs = rng.normal(size=(200, 4))
    qs /= np.linalg.norm(qs, axis=1, keepdims=True)
    qs = np.vstack([qs, [IDENTITY, (0, 0, -S45, S45), (1, 0, 0, 0), (0, -1, 0, 0)]])
    raw = np.frombuffer(b"".join(S.polar48(q) for q in qs), np.uint8).reshape(-1, 6)
    got = codecs._polar48_quat(raw)
    want = qs * np.where(qs[:, 3:] < 0, -1, 1)
    # the same rotation (q or -q): the angle between them is small
    dots = np.abs((got * want).sum(axis=1))
    assert (dots > 1 - 1e-6).all(), dots.min()
    # 32 bits: coarser, the same rotations
    got32 = codecs._polar32_quat(np.array([S.polar32(q) for q in qs], np.int64))
    assert (np.abs((got32 * want).sum(axis=1)) > 0.999).all(), np.abs((got32 * want).sum(axis=1)).min()


def test_model_package_keeps_the_animations(tmp_path):
    from darkstl import package
    from darkstl.model import CatalogEntry

    imp, by = _game(tmp_path)
    path = by["SK_Siren"].path
    m = open_model(path)
    root = tmp_path / "library" / "Borderlands 2 packages"
    store = package.AssetStore(root)
    entry = CatalogEntry(files.GAME_BL2, "Characters", "Siren", "SK_Siren", "Siren", path, imp.key)
    index = package.write_package(root, entry, "Characters/SK_Siren.upk", [m], store)
    store.save()
    shutil.rmtree(tmp_path / "Borderlands 2")
    animsets.forget()
    p = open_model(index)
    assert "AnimSet_Siren: Wave" in [i.name for i in p.animations.list()]
    last = p.build(MeshOptions(pose=("AnimSet_Siren: Wave", 2)))
    assert np.allclose(_vertex(last, (0, -1, 0.6)), (0, -1, 0.6), atol=2e-4)


def test_collect_copies_the_animsets(tmp_path):
    imp, by = _game(tmp_path)
    path = by["SK_Siren"].path
    rel = imp.related_files(path)
    assert {(r.path.name, r.kind) for r in rel} == {("CD_Siren.upk", "model"), ("Anim_Siren.upk", "animation")}
    assert {r.path.name for r in imp.related_files(path, animations=False)} == {"CD_Siren.upk"}
    out = tmp_path / "library" / "Borderlands 2"
    cooked = out / "WillowGame" / "CookedPCConsole"
    cooked.mkdir(parents=True)
    for r in rel:
        shutil.copy(r.path, cooked / r.path.name)
    shutil.rmtree(tmp_path / "Borderlands 2")  # the game is uninstalled: the collected files are all the app has
    files._INDEXES.clear()
    animsets.forget()
    copy = BorderlandsImporter()
    loose = {e.label: e for e in copy.scan(out, files.GAME_BL2)}
    _check_wave(copy.load(loose["SK_Siren"].path))


def test_package_lists_kept_before_animsets_were_are_read_again_for_them(tmp_path):
    import json

    imp, by = _game(tmp_path)
    kept = list((tmp_path / "cache" / "packages").glob("*.json"))
    assert kept and all("anim_sets" in json.loads(f.read_text()) for f in kept)  # the scan lists them too
    for f in kept:  # as an earlier version kept them
        data = json.loads(f.read_text())
        del data["anim_sets"]
        f.write_text(json.dumps(data))
    animsets.forget()
    assert [i.name for i in imp.load(by["SK_Siren"].path).animations.list()] == SIREN
    assert by["SK_Siren"].label in {e.label for e in imp.scan(tmp_path / "Borderlands 2", files.GAME_BL2)}
