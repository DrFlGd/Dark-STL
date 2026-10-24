"""Havok skeletons and animations: the packfile and tagfile readers, spline decoding, and posing a
FLVER model at a frame (skinned and rigid parts), through the importer, the engine and the page."""

import time

import numpy as np
import pytest

import synth
import synth_havok as sh
from darkstl.importers import open_model
from darkstl.importers.fromsoft import files
from darkstl.importers.fromsoft.animation import AnimationSet, Skeleton, compose_hierarchy, qs_to_matrices, read_animation
from darkstl.importers.fromsoft.formats import dcx
from darkstl.importers.fromsoft.formats.havok import layout, read_hkx
from darkstl.model import MeshOptions

# ---------------------------------------------------------------------------
# Class layouts: the offsets the games' files use (from the Havok SDK headers)


def offsets(name, ptr, year):
    return {m: off for m, _, off in layout(name, ptr, year).members}


def test_packfile_layouts_match_the_2014_64bit_files():
    anim = offsets("hkaSplineCompressedAnimation", 8, 2014)
    assert (anim["type"], anim["duration"], anim["numberOfTransformTracks"], anim["extractedMotion"],
            anim["annotationTracks"]) == (12, 16, 20, 32, 40)
    assert (anim["numFrames"], anim["numBlocks"], anim["maxFramesPerBlock"], anim["maskAndQuantizationSize"],
            anim["blockDuration"], anim["frameDuration"]) == (56, 60, 64, 68, 72, 80)
    assert (anim["blockOffsets"], anim["floatBlockOffsets"], anim["transformOffsets"], anim["floatOffsets"],
            anim["data"], anim["endian"]) == (88, 104, 120, 136, 152, 168)
    assert layout("hkaSplineCompressedAnimation", 8, 2014).size == 176
    sk = offsets("hkaSkeleton", 8, 2014)
    assert (sk["name"], sk["parentIndices"], sk["bones"], sk["referencePose"], sk["partitions"]) == (16, 24, 40, 56, 120)
    b = offsets("hkaAnimationBinding", 8, 2014)
    assert (b["originalSkeletonName"], b["animation"], b["transformTrackToBoneIndices"], b["partitionIndices"],
            b["blendHint"]) == (16, 24, 32, 64, 80)
    c = offsets("hkaAnimationContainer", 8, 2014)
    assert (c["skeletons"], c["animations"], c["bindings"], c["attachments"], c["skins"]) == (16, 32, 48, 64, 80)
    assert layout("hkaBone", 8, 2014).size == 16
    assert layout("hkRootLevelContainerNamedVariant", 8, 2014).size == 24


def test_packfile_layouts_match_the_2010_32bit_files():
    anim = offsets("hkaSplineCompressedAnimation", 4, 2010)
    assert (anim["type"], anim["duration"], anim["extractedMotion"], anim["annotationTracks"]) == (8, 12, 24, 28)
    assert (anim["numFrames"], anim["numBlocks"], anim["frameDuration"], anim["blockOffsets"], anim["data"],
            anim["endian"]) == (40, 44, 64, 68, 116, 128)
    sk = offsets("hkaSkeleton", 4, 2010)
    assert (sk["name"], sk["parentIndices"], sk["bones"], sk["referencePose"]) == (8, 12, 24, 36)
    assert "partitions" not in sk
    b = offsets("hkaAnimationBinding", 4, 2010)
    assert (b["originalSkeletonName"], b["animation"], b["transformTrackToBoneIndices"], b["blendHint"]) == (8, 12, 16, 40)
    assert "partitionIndices" not in b
    c = offsets("hkaAnimationContainer", 4, 2010)
    assert (c["skeletons"], c["animations"], c["bindings"], c["attachments"], c["skins"]) == (8, 20, 32, 44, 56)
    assert layout("hkaBone", 4, 2010).size == 8


# ---------------------------------------------------------------------------
# A small rig: three bones, the arm slides and the hand turns


BONES = [sh.HkBone("root"), sh.HkBone("arm", 0, (0.0, 1.0, 0.0)), sh.HkBone("hand", 1, (0.0, 0.5, 0.0))]
FRAMES = 10


def rig_poses():
    poses = np.zeros((FRAMES, 3, 12))
    poses[:, :, 7] = 1.0
    poses[:, :, 8:11] = 1.0
    for b, bone in enumerate(BONES):
        poses[:, b, 0:3] = bone.translation
    t = np.linspace(0.0, 1.0, FRAMES)
    poses[:, 1, 0] = t * 0.5  # the arm slides 0.5 m along x
    angle = t * np.pi / 2  # the hand turns a quarter turn about z
    poses[:, 2, 4:8] = np.stack([np.zeros(FRAMES), np.zeros(FRAMES), np.sin(angle / 2), np.cos(angle / 2)], axis=1)
    return poses


def check_decoded(hkx, poses, tol, pos_tol=1e-4):
    sk = Skeleton.from_hkx(hkx)
    assert sk.names == ["root", "arm", "hand"]
    assert sk.parents.tolist() == [-1, 0, 1]
    assert np.allclose(sk.reference_pose[1, 0:3], (0, 1, 0))
    anim = read_animation(hkx, "a000_000000")
    assert anim.frames == FRAMES
    assert anim.track_to_bone.tolist() == [0, 1, 2]
    for f in range(FRAMES):
        tracks = anim.tracks_at(f)
        assert np.allclose(tracks[:, 0:3], poses[f, :, 0:3], atol=pos_tol)  # 16-bit scalars
        q, want = tracks[2, 4:8], poses[f, 2, 4:8]
        assert min(np.abs(q - want).max(), np.abs(q + want).max()) < tol
        assert np.allclose(tracks[:, 8:11], 1.0)
    return sk, anim


@pytest.mark.parametrize("rotation_format", [1, 2, 5], ids=["ThreeComp40", "ThreeComp48", "Uncompressed"])
@pytest.mark.parametrize("pointer_size,version", [(8, "hk_2014.1.0-r1"), (4, "hk_2010.2.0-r1")], ids=["2014-64bit", "2010-32bit"])
def test_packfile_round_trip(rotation_format, pointer_size, version):
    poses = rig_poses()
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3, rotation_format=rotation_format))
    hkx = read_hkx(sh.build_hkx_packfile(scene, pointer_size, version))
    assert hkx.format == "packfile" and hkx.version == version
    assert [v["className"] for v in hkx.variants] == ["hkaAnimationContainer"]
    check_decoded(hkx, poses, {1: 2e-3, 2: 1e-3, 5: 1e-6}[rotation_format])


@pytest.mark.parametrize("rotation_format", [1, 2, 5], ids=["ThreeComp40", "ThreeComp48", "Uncompressed"])
def test_tagfile_round_trip_with_several_blocks(rotation_format):
    poses = rig_poses()
    # 6 frames per block means blocks share a boundary frame: frames 0-5, 5-9
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3, rotation_format=rotation_format, max_frames_per_block=6))
    assert len(scene.animation.block_offsets) == 2
    hkx = read_hkx(sh.build_hkx_tagfile(scene))
    assert hkx.format == "tagfile" and hkx.version == "20150100"
    check_decoded(hkx, poses, {1: 2e-3, 2: 1e-3, 5: 1e-6}[rotation_format])


def test_static_translation_and_8bit_scalars():
    poses = rig_poses()
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3, scalar_bits=8))
    check_decoded(read_hkx(sh.build_hkx_packfile(scene)), poses, 2e-3, pos_tol=5e-3)
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3, static_translation=True))
    anim = read_animation(read_hkx(sh.build_hkx_packfile(scene)))
    assert np.allclose(anim.tracks_at(FRAMES - 1)[1, 0:3], (0, 1, 0))  # held at the first frame's value


def test_world_matrices_follow_the_hierarchy():
    poses = rig_poses()
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3))
    data = sh.build_hkx_packfile(scene)
    aset = AnimationSet(Skeleton.from_hkx(read_hkx(data)), {"a000_000000": lambda: data})
    assert [i.name for i in aset.list()] == ["a000_000000"]
    assert aset.info("a000_000000").frames == FRAMES
    assert aset.clamp("a000_000000", 99) == FRAMES - 1
    ref = aset.reference_world()
    assert np.allclose(ref["hand"][:3, 3], (0, 1.5, 0))
    last = aset.bone_world("a000_000000", FRAMES - 1)
    assert np.allclose(last["arm"][:3, 3], (0.5, 1.0, 0), atol=1e-3)
    assert np.allclose(last["hand"][:3, 3], (0.5, 1.5, 0), atol=1e-3)
    # the hand's x axis now points along y (a quarter turn about z)
    assert np.allclose(last["hand"][:3, 0], (0, 1, 0), atol=2e-3)
    # a model bone the skeleton doesn't know keeps its own local transform under its parent
    local = qs_to_matrices([b.qs() for b in BONES] + [[0, 0, 0.2, 0, 0, 0, 0, 1, 1, 1, 1, 1]])
    world = aset.pose_bones(["root", "arm", "hand", "hand_dummy"], np.array([-1, 0, 1, 2]), local, "a000_000000", FRAMES - 1)
    assert np.allclose(world[3][:3, 3], last["hand"][:3, 3] + (0, 0, 0.2), atol=2e-3)
    assert np.allclose(compose_hierarchy(local, np.array([-1, 0, 1, 2]))[3][:3, 3], (0, 1.5, 0.2))


def test_unreadable_animations_are_listed_but_say_so():
    poses = rig_poses()
    data = sh.build_hkx_packfile(sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3)))
    aset = AnimationSet(Skeleton.from_hkx(read_hkx(data)), {"a000_000000": lambda: data, "a000_000001": lambda: b"junk"})
    infos = {i.name: i for i in aset.list()}
    assert infos["a000_000000"].frames == FRAMES
    assert infos["a000_000001"].frames == 0 and "unreadable" in infos["a000_000001"].source


# ---------------------------------------------------------------------------
# A model with its anibnd beside it


@pytest.fixture
def char_dir(tmp_path):
    """A fake Dark Souls III character: a skinned strip on the arm, a rigid cube on the hand, and an anibnd."""
    return make_char_dir(tmp_path / "DARK SOULS III" / "Game")


def make_char_dir(game):
    chr_dir = game / "chr"
    chr_dir.mkdir(parents=True, exist_ok=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    bones = [synth.SynthBone(b.name, b.translation, parent=b.parent) for b in BONES]
    # mesh 0: a skinned quad, every vertex on the arm bone (index 1)
    pos, nrm, uv, idx = synth.grid_plane(2, size=0.2)
    skinned = synth.SynthMesh(pos, nrm, uv, idx, dynamic=1, bone_indices=np.tile([1, 0, 0, 0], (len(pos), 1)))
    # mesh 1: a rigid box, long along x, held by the hand bone (index 2)
    cpos, cnrm, cuv, cidx = synth.cube_mesh(0.1)
    rigid = synth.SynthMesh(cpos * np.array([2, 1, 1], np.float32), cnrm, cuv, cidx, dynamic=0,
                            bone_indices=np.tile([2, 0, 0, 0], (len(cpos), 1)))
    mats = [("Skin", "N:\\mtd\\C[DSB].mtd", [("N:\\tex\\c2000_a.tga", "g_DiffuseTexture")])]
    flver_bytes = synth.build_flver([skinned, rigid], mats, bones, uv_float=True)
    albedo = np.full((16, 16, 4), 200, np.uint8)
    tpf_bytes = synth.build_tpf([("c2000_a", synth.rgba_dds(albedo))])
    (chr_dir / "c2000.chrbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([
        (200, "N:\\chr\\c2000\\c2000.flver", flver_bytes), (100, "N:\\chr\\c2000\\c2000.tpf", tpf_bytes)])))
    poses = rig_poses()
    scene = sh.HkScene(BONES, "master", sh.spline_animation(poses, 0.3))
    skeleton = sh.build_hkx_packfile(sh.HkScene(BONES, "master"))
    anims = [(i, f"N:\\chr\\c2000\\hkx\\a000_{n:06d}.hkx", sh.build_hkx_packfile(scene)) for i, n in enumerate((0, 3000), 1)]
    (chr_dir / "c2000.anibnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4(
        [(1000000, "N:\\chr\\c2000\\hkx\\Skeleton.HKX", skeleton)] + anims)))
    # a character without animations, for contrast
    (chr_dir / "c2001.chrbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "c2001.flver", flver_bytes)])))
    return game


def test_animation_archives_are_found_beside_the_model(char_dir):
    assert [p.name for p in files.animation_archives(char_dir / "chr" / "c2000.chrbnd.dcx", "c2000")] == ["c2000.anibnd.dcx"]
    assert files.animation_archives(char_dir / "chr" / "c2001.chrbnd.dcx", "c2001") == []
    # the split archives of the later games, and unpacked folders, count too
    (char_dir / "chr" / "c2000_div00.anibnd.dcx").write_bytes(b"")
    (char_dir / "chr" / "c2000-anibnd-dcx").mkdir()
    (char_dir / "chr" / "c2000_a00.anibnd").write_bytes(b"")
    found = sorted(p.name for p in files.animation_archives(char_dir / "chr" / "c2000.chrbnd.dcx", "c2000"))
    assert found == ["c2000-anibnd-dcx", "c2000.anibnd.dcx", "c2000_a00.anibnd", "c2000_div00.anibnd.dcx"]


def test_a_model_is_posed_at_a_frame(char_dir):
    model = open_model(char_dir / "chr" / "c2000.chrbnd.dcx")
    assert model.animations is not None
    assert [a.name for a in model.animations.list()] == ["a000_000000", "a000_003000"]
    assert model.animations.info("a000_003000").frames == FRAMES
    bind = model.build_mesh(MeshOptions(parts={0}))
    posed = model.build_mesh(MeshOptions(parts={0}, pose=("a000_003000", FRAMES - 1)))
    # the skinned quad rides on the arm: 0.5 m along x, nothing else (export frame keeps x)
    shift = posed.vertices.mean(axis=0) - bind.vertices.mean(axis=0)
    assert np.allclose(shift, (0.5, 0, 0), atol=2e-3)
    assert np.allclose(posed.vertices - bind.vertices, shift, atol=2e-3)
    # the rigid box follows the hand: moved with the arm and turned a quarter turn about the game's z
    # (the export frame's y), so its long side swings from x to the game's y, the export frame's z
    cube_bind = model.build_mesh(MeshOptions(parts={1}))
    cube_posed = model.build_mesh(MeshOptions(parts={1}, pose=("a000_003000", FRAMES - 1)))
    hand_bind = np.array([0, 0, 1.5])  # hand bone, export frame (x, z, y)
    assert np.allclose(cube_bind.vertices.mean(axis=0), hand_bind, atol=1e-6)
    assert np.allclose(cube_posed.vertices.mean(axis=0), hand_bind + (0.5, 0, 0), atol=2e-3)
    extent = lambda mesh: np.ptp(mesh.vertices, axis=0)  # noqa: E731
    assert np.allclose(extent(cube_bind), (0.2, 0.1, 0.1), atol=1e-6)
    assert np.allclose(extent(cube_posed), (0.1, 0.1, 0.2), atol=2e-3)
    # frame 0 is the bind pose; frames past the end clamp to the last
    assert np.allclose(model.build_mesh(MeshOptions(parts={0}, pose=("a000_003000", 0))).vertices, bind.vertices, atol=1e-4)
    assert np.allclose(model.build_mesh(MeshOptions(parts={0}, pose=("a000_003000", 500))).vertices, posed.vertices)
    # the whole model at a pose keeps both parts and their materials
    whole = model.build_mesh(MeshOptions(pose=("a000_003000", 3)))
    assert whole.n_faces == bind.n_faces + cube_bind.n_faces
    with pytest.raises(Exception, match="No animation named"):
        model.build_mesh(MeshOptions(pose=("a999_999999", 0)))


def test_a_model_without_an_anibnd_has_no_animations(char_dir):
    model = open_model(char_dir / "chr" / "c2001.chrbnd.dcx")
    assert model.animations is None
    with pytest.raises(Exception, match="no animations"):
        model.build_mesh(MeshOptions(pose=("a000_000000", 0)))


# ---------------------------------------------------------------------------
# The engine and the page


def test_engine_poses_generates_and_exports(char_dir, tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.meshpack import unpack_mesh
    from darkstl.app.service import Service, generated_key, mesh_options, parse_pose
    from test_app import wait

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    service = Service(Config.load(), tmp_path / "home" / "cache")
    m = service.call("model_open", {"path": str(char_dir / "chr" / "c2000.chrbnd.dcx")})
    assert m["animated"] is True
    anims = service.call("model_animations", {"sid": m["sid"]})
    assert [a["name"] for a in anims["animations"]] == ["a000_000000", "a000_003000"]
    assert anims["animations"][1]["frames"] == FRAMES and anims["bones"] == 3
    assert parse_pose("a000_003000@4") == ("a000_003000", 4) and parse_pose(["a000_003000", 4]) == ("a000_003000", 4)
    assert parse_pose("") is None and parse_pose(None) is None
    _, bind = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "parts": "0", "flags": ""}))
    _, posed = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "parts": "0", "flags": "", "pose": f"a000_003000@{FRAMES - 1}"}))
    # packed positions are in millimetres, (x, z, y) as the 3D view takes them
    shift = posed["positions"].reshape(-1, 3).mean(axis=0) - bind["positions"].reshape(-1, 3).mean(axis=0)
    assert abs(shift[0] - 500) < 2 and abs(shift[1]) < 2 and abs(shift[2]) < 2
    # a pose is part of what a generated model depends on
    session = service.sessions[m["sid"]]
    plain = generated_key(session, mesh_options({"parts": [0]}), {})
    with_pose = generated_key(session, mesh_options({"parts": [0], "pose": ["a000_003000", 2]}), {})
    assert plain != with_pose
    assert generated_key(session, mesh_options({"parts": [0], "pose": ["a000_003000", 2]}), {}) == with_pose
    job = service.call("enhance", {"sid": m["sid"], "parts": [0], "pose": ["a000_003000", FRAMES - 1],
                                   "settings": {"target_vertices": 2000, "source": "none"}})
    result = wait(service, job["job"])
    assert result["stats"]["vertices"] >= 1000
    out = wait(service, service.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".stl", "folder": str(tmp_path),
                                                "name": "c2000_a000_003000_f9"})["job"])
    assert out["path"].endswith("c2000_a000_003000_f9.stl")
    data = (tmp_path / "c2000_a000_003000_f9.stl").read_bytes()
    tris = np.frombuffer(data, np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), int.from_bytes(data[80:84], "little"), 84)
    xs = tris["v"].reshape(-1, 3)[:, 0]
    assert abs(xs.min() - 500) < 5 and abs(xs.max() - 700) < 5  # the 0.2 m quad (from x = 0), 0.5 m along x, in mm
    with pytest.raises(Exception, match="animation@frame"):
        service.call("model_mesh", {"sid": m["sid"], "pose": "@3"})
    plain_model = service.call("model_open", {"path": str(char_dir / "chr" / "c2001.chrbnd.dcx")})
    assert plain_model["animated"] is False
    assert service.call("model_animations", {"sid": plain_model["sid"]}) == {"animations": [], "bones": 0}


def test_the_pose_tab_steps_through_frames(char_dir, tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    from darkstl.app.config import Config
    from darkstl.app.server import AppServer
    from darkstl.app.service import Service
    from test_page import GL, launch

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    server = AppServer(Service(Config.load(), tmp_path / "home" / "cache"), 0).start()
    try:
        with sync_api.sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(server.url)
            page.wait_for_selector("#first-run")
            m = page.evaluate("(p) => import('./ui/api.js').then((a) => a.api('model_open', { path: p }))",
                              str(char_dir / "chr" / "c2000.chrbnd.dcx"))
            page.goto(f"{server.url}#/model/{m['id']}")
            page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
            page.click("#rail-pose")
            page.wait_for_selector("#pose-anim option")
            assert page.locator("#pose-anim option").count() == 2
            page.select_option("#pose-anim", "a000_003000")
            page.wait_for_function("document.querySelector('#stage-pose')?.textContent.includes('frame 0')")
            page.fill("#pose-frame-number", str(FRAMES - 1))
            page.press("#pose-frame-number", "Enter")
            page.wait_for_function(f"document.querySelector('#stage-pose')?.textContent.includes('frame {FRAMES - 1}')")
            page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
            assert f"of {FRAMES - 1}" in page.inner_text("#pose-frame-section")
            page.click("#pose-prev")
            page.wait_for_function(f"document.querySelector('#stage-pose')?.textContent.includes('frame {FRAMES - 2}')")
            page.click("#pose-play")
            deadline = time.time() + 20
            while time.time() < deadline and f"frame {FRAMES - 2}" in page.inner_text("#stage-pose"):
                time.sleep(0.1)
            page.click("#pose-play")
            assert f"frame {FRAMES - 2}" not in page.inner_text("#stage-pose")
            page.click("#rail-export")
            assert page.get_attribute("#export-name", "placeholder").startswith("c2000_a000_003000_f")
            page.click("#rail-pose")
            page.click("#pose-reset")
            page.wait_for_function("!document.querySelector('#stage-pose')")
            assert page.evaluate("window.__errors") == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
