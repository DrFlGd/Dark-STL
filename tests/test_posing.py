"""Posing freedom: bones turned on top of an animation's frame (or the bind pose), mirrored to the other side,
saved poses kept in the library overlay and offered to models with the same skeleton, models attached to a
bone that follow the pose, and the glTF export keeping the skeleton in the adjusted pose."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

import synth
from darkstl import attach, overlay, posing
from darkstl.importers import open_model
from darkstl.importers.fromsoft.formats import dcx
from darkstl.model import MeshOptions
from test_animation import FRAMES, make_char_dir
from test_app import home, make_service, wait  # noqa: F401 - fixture

GAME = "Dark Souls III"
HAND = np.array([0.0, 0.0, 1.5])  # the hand bone in the bind pose, export frame
ARM = np.array([0.0, 0.0, 1.0])


@pytest.fixture
def game(tmp_path):
    root = make_char_dir(tmp_path / "DARK SOULS III" / "Game")
    _mirror_character(root / "chr")
    return root


def _mirror_character(chr_dir):
    """c2100: a body with a left and a right arm, a quad weighted to each, mirror images of each other."""
    bones = [synth.SynthBone("Master"), synth.SynthBone("L_UpperArm", (0.3, 1.0, 0.0), parent=0),
             synth.SynthBone("R_UpperArm", (-0.3, 1.0, 0.0), parent=0)]
    meshes = []
    for bone, x0 in ((1, 0.2), (2, -0.4)):
        pos, nrm, uv, idx = synth.grid_plane(2, size=0.2)
        pos = pos + np.array([x0, 1.0, -0.1], np.float32)
        meshes.append(synth.SynthMesh(pos, nrm, uv, idx, dynamic=1, bone_indices=np.tile([bone, 0, 0, 0], (len(pos), 1))))
    mats = [("Skin", "N:\\mtd\\C[DSB].mtd", [])]
    flver = synth.build_flver(meshes, mats, bones, uv_float=True)
    (chr_dir / "c2100.chrbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "c2100.flver", flver)])))


def _sorted_rows(a: np.ndarray) -> np.ndarray:
    a = np.round(np.asarray(a, np.float64), 5)
    return a[np.lexsort(a.T[::-1])]


# ---------------------------------------------------------------------------
# the maths


def test_turns_read_back_as_three_js_makes_them():
    for deg in [(10, 20, 30), (-170, 10, 100), (0, 89.5, 5), (45, 0, 0)]:
        assert np.allclose(posing.turn_angles(posing.turn_matrix(deg)), deg, atol=1e-3)
    # X, then Y, then Z as three.js's "XYZ" Euler order: Rx @ Ry @ Rz
    rx, rz = posing.turn_matrix((90, 0, 0)), posing.turn_matrix((0, 0, 90))
    assert np.allclose(posing.turn_matrix((90, 0, 90)), rx @ rz)
    assert posing.clean_bones({"a": [0, 0, 0], "b": [370, -190, 0.00001]}) == {"b": (10.0, 170.0, 0.0)}
    q, flip = posing.quaternion(np.diag([1.0, 1.0, 1.0]))
    assert q == [0.0, 0.0, 0.0, 1.0] and not flip


def test_bones_pair_up_by_the_side_their_names_say():
    pairs = [("L_Clavicle", "R_Clavicle"), ("L Thigh", "R Thigh"), ("l_arm_humerus", "r_arm_humerus"),
             ("upperarm_l", "upperarm_r"), ("Hand.L", "Hand.R"), ("Bip01 L Hand", "Bip01 R Hand"),
             ("mixamorig:LeftHand", "mixamorig:RightHand"), ("LUpperArm", "RUpperArm"), ("Finger_L1", "Finger_R1")]
    names = [n for pair in pairs for n in pair] + ["Spine", "Lower", "Rower_x", "Neck"]
    for a, b in pairs:
        assert posing.mirror_name(a, names) == b and posing.mirror_name(b, names) == a
        assert posing.side_of(a, names) == "left" and posing.side_of(b, names) == "right"
    assert posing.mirror_name("Spine", names) is None
    assert posing.mirror_name("Lower", names) is None  # "L" + "ower" is no side
    assert posing.mirror_name("L_Foot", names) is None  # no partner there


# ---------------------------------------------------------------------------
# adjusting bones of a game model


def test_a_turned_bone_moves_its_vertices_and_its_childrens(game):
    model = open_model(game / "chr" / "c2000.chrbnd.dcx")
    quad = model.build_mesh(MeshOptions(parts={0}))  # on the arm
    cube = model.build_mesh(MeshOptions(parts={1}))  # on the hand
    # the hand turned a quarter about its Z: the box (long along X) turns about the hand, the arm stays
    hand = MeshOptions(parts={1}, bones={"hand": (0, 0, 90)})
    turned = model.build_mesh(hand)
    assert np.allclose(turned.vertices.mean(axis=0), HAND, atol=1e-6)
    assert np.allclose(np.ptp(turned.vertices, axis=0), (0.1, 0.2, 0.1), atol=1e-6)
    assert np.allclose(model.build_mesh(MeshOptions(parts={0}, bones={"hand": (0, 0, 90)})).vertices, quad.vertices)
    # the arm turned a quarter about X: the quad swings about the arm's pivot, and the hand's box with it
    arm = {"arm": (90, 0, 0)}
    swung = model.build_mesh(MeshOptions(parts={0}, bones=arm))
    rel = quad.vertices - ARM
    expect = ARM + np.stack([rel[:, 0], -rel[:, 2], rel[:, 1]], axis=1)
    assert np.allclose(swung.vertices, expect, atol=1e-6)
    hand_now = ARM + np.array([0, -0.5, 0])
    assert np.allclose(model.build_mesh(MeshOptions(parts={1}, bones=arm)).vertices.mean(axis=0), hand_now, atol=1e-6)
    # on top of an animation's frame: the frame's pose, then the turn about the bone as the frame holds it
    pose = ("a000_003000", FRAMES - 1)
    framed = model.build_mesh(MeshOptions(parts={1}, pose=pose))
    both = model.build_mesh(MeshOptions(parts={1}, pose=pose, bones={"hand": (0, 0, 90)}))
    assert np.allclose(both.vertices.mean(axis=0), framed.vertices.mean(axis=0), atol=2e-3)  # turned in place
    assert not np.allclose(both.vertices, framed.vertices, atol=1e-2)
    world = posing.bone_world(model.rig, pose, {"hand": (0, 0, 90)})
    assert np.allclose(world[2][:3, 3], framed.vertices.mean(axis=0), atol=2e-3)
    # no adjustment, or one of a bone the model doesn't have, is the pose as it was (posed from the skeleton
    # rather than by the importer, so its vertices may come in another order)
    assert np.allclose(_sorted_rows(model.build_mesh(MeshOptions(parts={1}, pose=pose, bones={"nope": (0, 0, 90)})).vertices),
                       _sorted_rows(framed.vertices), atol=2e-3)
    assert np.array_equal(cube.vertices, model.build_mesh(MeshOptions(parts={1}, bones={})).vertices)


def test_a_model_without_animations_turns_its_bind_pose(game):
    model = open_model(game / "chr" / "c2001.chrbnd.dcx")
    assert model.animations is None and model.rig is not None and model.rig.names == ["root", "arm", "hand"]
    turned = model.build_mesh(MeshOptions(parts={1}, bones={"hand": (0, 0, 90)}))
    assert np.allclose(np.ptp(turned.vertices, axis=0), (0.1, 0.2, 0.1), atol=1e-6)
    with pytest.raises(Exception, match="no animations"):
        model.build_mesh(MeshOptions(pose=("a000_000000", 0), bones={"hand": (0, 0, 1)}))


def test_mirroring_turns_the_other_side_the_same_way(game):
    model = open_model(game / "chr" / "c2100.chrbnd.dcx")
    assert posing.mirror_axis(model.rig) == 0  # the sides lie apart along X
    left = {"L_UpperArm": (20.0, 30.0, 40.0)}
    both = posing.mirror_bones(model.rig, left, ["L_UpperArm"])
    assert set(both) == {"L_UpperArm", "R_UpperArm"} and both["L_UpperArm"] == left["L_UpperArm"]
    mesh = model.build_mesh(MeshOptions(bones=both))
    lq = model.build_mesh(MeshOptions(parts={0}, bones=both)).vertices
    rq = model.build_mesh(MeshOptions(parts={1}, bones=both)).vertices
    assert not np.allclose(lq, model.build_mesh(MeshOptions(parts={0})).vertices, atol=1e-3)  # it did move
    assert np.allclose(_sorted_rows(lq * [-1, 1, 1]), _sorted_rows(rq), atol=1e-5)  # mirror images still
    assert mesh.n_faces == 16
    # mirroring a bone without an adjustment takes its partner's away
    assert posing.mirror_bones(model.rig, both, ["L_UpperArm"]) == both
    assert posing.mirror_bones(model.rig, {"R_UpperArm": (1, 2, 3)}, ["L_UpperArm"]) == {}


# ---------------------------------------------------------------------------
# the app: bones, saved poses, attached models, export


def _open(s, stem):
    items = s.call("models_query", {"game": GAME, "limit": 1000})["items"]
    item = next(i for i in items if i["stem"] == stem)
    return item, s.call("model_open", {"id": item["id"]})


def _mesh(s, sid, **args):
    from darkstl.app.meshpack import unpack_mesh

    url = {"sid": sid, "parts": "all", "flags": ""}
    for k, v in args.items():
        url[k] = v if isinstance(v, str) else json.dumps(v)
    header, arrays = unpack_mesh(s.call("model_mesh", url))
    return header, arrays["positions"].reshape(-1, 3) / 1000.0


def test_blank_url_parameters_mean_no_turns_and_nothing_attached():
    """The desktop app's engine:// requests keep blank parameters, which mean none."""
    from darkstl.app.service import _parse_lists, mesh_options

    opts = mesh_options(_parse_lists({"parts": "all", "flags": "", "pose": "", "bones": "", "attach": " "}))
    assert opts.bones is None and opts.attach is None and opts.pose is None
    opts = mesh_options(_parse_lists({"bones": '{"hand": [0, 0, 30]}', "attach": "{}"}))
    assert opts.bones == {"hand": (0.0, 0.0, 30.0)} and not opts.attach


def test_bones_listed_with_their_places_and_partners(home, game):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    _, m = _open(s, "c2000")
    assert m["posable"] and m["bones"] == 3 and m["animated"]
    r = s.call("model_bones", {"sid": m["sid"]})
    names = [b["name"] for b in r["bones"]]
    assert names == ["root", "arm", "hand"] and r["skeleton"] == posing.skeleton_signature(names)
    hand = r["bones"][2]
    assert hand["parent"] == 1 and hand["depth"] == 2 and np.allclose(hand["pos"], HAND * 1000)
    assert all(b["used"] for b in r["bones"])  # the hand holds the box, the arm the quad, the root both
    turned = s.call("model_bones", {"sid": m["sid"], "bones": {"arm": [90, 0, 0]}})
    assert np.allclose(turned["bones"][2]["pos"], (ARM + [0, -0.5, 0]) * 1000, atol=1e-3)
    assert turned["adjusted"] == {"arm": [90.0, 0.0, 0.0]}
    # the mesh the 3D view gets follows, from the URL's JSON
    _, bind = _mesh(s, m["sid"])
    _, swung = _mesh(s, m["sid"], bones={"arm": [90, 0, 0]})
    assert not np.allclose(bind, swung)
    # a turn is part of what everything made from the model depends on
    from darkstl.app.service import generated_key, mesh_options

    session = s.sessions[m["sid"]]
    assert generated_key(session, mesh_options({}), {}) != generated_key(session, mesh_options({"bones": {"arm": [1, 0, 0]}}), {})
    # a model with bones but no animations is posable too; a mirror pair is named
    _, plain = _open(s, "c2001")
    assert plain["posable"] and not plain["animated"]
    _, sides = _open(s, "c2100")
    listed = {b["name"]: b for b in s.call("model_bones", {"sid": sides["sid"]})["bones"]}
    assert listed["L_UpperArm"]["partner"] == "R_UpperArm" and listed["L_UpperArm"]["side"] == "left"
    mirrored = s.call("pose_mirror", {"sid": sides["sid"], "bones": {"L_UpperArm": [0, 0, 30]}, "side": "left"})
    assert set(mirrored["bones"]) == {"L_UpperArm", "R_UpperArm"}
    with pytest.raises(Exception, match="partner"):
        s.call("pose_mirror", {"sid": sides["sid"], "bones": {}, "names": ["Master"]})


def test_saved_poses_round_trip_and_go_to_models_with_the_same_skeleton(home, game):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    _, m = _open(s, "c2000")
    assert s.call("poses", {"sid": m["sid"]})["own"] == []
    with pytest.raises(Exception, match="bind pose"):
        s.call("pose_save", {"sid": m["sid"], "name": "Nothing"})
    r = s.call("pose_save", {"sid": m["sid"], "name": "  Wave  hello ", "pose": ["a000_003000", 4],
                             "bones": {"hand": [0, 0, 45]}})
    assert r["saved"] == "Wave hello"
    assert [(p["name"], p["animation"], p["frame"], p["bones"]) for p in r["own"]] == [
        ("Wave hello", "a000_003000", 4, {"hand": [0.0, 0.0, 45.0]})]
    # kept in the game's overlay under the model's path in the game, with its skeleton
    saved = json.loads((home / "overlays" / f"{GAME}.json").read_text())
    kept = saved["models"]["chr/c2000.chrbnd.dcx"]["poses"]["Wave hello"]
    assert kept["skeleton"] == posing.skeleton_signature(["root", "arm", "hand"])
    # read again by a new app, renamed, and offered to another model with the same bones
    s2 = make_service(home)
    wait(s2, s2.call("folder_add", {"path": str(game)})["job"])
    _, again = _open(s2, "c2000")
    assert [p["name"] for p in s2.call("poses", {"sid": again["sid"]})["own"]] == ["Wave hello"]
    renamed = s2.call("pose_rename", {"sid": again["sid"], "name": "Wave hello", "to": "Wave"})
    assert [p["name"] for p in renamed["own"]] == ["Wave"]
    _, other = _open(s2, "c2001")
    offered = s2.call("poses", {"sid": other["sid"]})
    assert offered["own"] == [] and [(p["name"], p["model"]) for p in offered["shared"]] == [("Wave", "Character c2000")]
    pose = offered["shared"][0]
    # applied there: its bones turn the second model as they turned the first (it has no animations to play)
    _, here = _mesh(s2, other["sid"], bones=pose["bones"])
    _, there = _mesh(s2, again["sid"], bones=pose["bones"])
    assert np.allclose(here, there, atol=1e-6)
    _, c2100 = _open(s2, "c2100")
    assert s2.call("poses", {"sid": c2100["sid"]})["shared"] == []  # other bones: not offered
    assert s2.call("pose_delete", {"sid": again["sid"], "name": "Wave"})["own"] == []
    assert s2.call("poses", {"sid": other["sid"]})["shared"] == []


def test_an_attached_model_follows_its_bone_through_a_frame(home, game):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    _, m = _open(s, "c2000")
    copy, _ = _open(s, "c2001")
    before = m["stats"]["triangles"]
    place = {"bone": "hand", "offset_mm": [100, 0, 0], "rotate": [0, 0, 0], "scale": 1}
    r = s.call("attach_save", {"sid": m["sid"], "items": [{"file": copy["file"], "label": "Copy", "place": place}]})
    assert r["reopen"] is True and r["items"][0]["file"] == "chr/c2001.chrbnd.dcx" and r["items"][0]["id"]
    kept = overlay.Overlay(home / "overlays" / f"{GAME}.json", GAME).attachments("chr/c2000.chrbnd.dcx")
    assert kept[0]["place"]["bone"] == "hand"
    _, m2 = _open(s, "c2000")
    assert m2["sid"] != m["sid"]  # what is attached is part of what is open
    assert m2["stats"]["triangles"] == 2 * before
    assert [a["label"] for a in m2["attachments"]] == ["Copy"] and m2["attachments"][0]["parts"] == [2, 2]
    assert [p["name"] for p in m2["parts"]][2:] == ["Copy / Mesh 0", "Copy / Mesh 1"]
    assert len(m2["materials"]) == 2 and m2["materials"][1]["name"].startswith("Copy / ")
    child = open_model(game / "chr" / "c2001.chrbnd.dcx").build_mesh(MeshOptions())
    session = s.sessions[m2["sid"]]
    rig = session.model.rig

    def placed(pose=None, bones=None, offset=(0.1, 0, 0)):
        world = posing.bone_world(rig, pose, bones)
        r3 = posing.orthonormal(world[2][:3, :3])
        return child.vertices @ r3.T + (world[2][:3, 3] + r3 @ np.asarray(offset))

    # in the bind pose, on an animation's frame, and with the hand turned too: always at the hand, 100 mm out
    for pose, bones in ((None, None), (("a000_003000", FRAMES - 1), None), (("a000_003000", 3), {"hand": [0, 0, 60]})):
        opts = MeshOptions(parts={2, 3}, pose=pose, bones=bones)
        got = session.build(opts).vertices
        assert np.allclose(_sorted_rows(got), _sorted_rows(placed(pose, bones)), atol=3e-3), (pose, bones)
    # moved without saving first: the mesh options carry where it sits
    moved = MeshOptions(parts={2, 3}, attach={r["items"][0]["id"]: attach.parse_place({**place, "offset_mm": [0, 0, 200]})})
    assert np.allclose(_sorted_rows(session.build(moved).vertices), _sorted_rows(placed(offset=(0, 0, 0.2))), atol=1e-6)
    # it goes through Generate with the model
    job = s.call("enhance", {"sid": m2["sid"], "settings": {"target_vertices": 2000, "source": "none"},
                             "attach": {r["items"][0]["id"]: place}, "step": "mesh"})
    assert wait(s, job["job"])["stats"]["vertices"] >= 1000
    # taken off again
    assert s.call("attach_save", {"sid": m2["sid"], "items": []})["reopen"] is True
    assert _open(s, "c2000")[1]["stats"]["triangles"] == before


def test_a_glb_export_keeps_the_skeleton_in_the_adjusted_pose(home, game, tmp_path):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    _, m = _open(s, "c2000")
    args = {"sid": m["sid"], "which": "original", "format": ".glb", "units": "mm", "folder": str(tmp_path),
            "name": "posed", "pose": ["a000_003000", 5], "bones": {"hand": [0, 0, 45]}}
    out = wait(s, s.call("export", args)["job"])
    data = open(out["path"], "rb").read()
    length = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + length])
    names = [n.get("name") for n in gltf["nodes"]]
    assert names[1:4] == ["root", "arm", "hand"] and gltf["nodes"][0]["skin"] == 0
    assert gltf["skins"][0]["joints"] == [1, 2, 3]
    prim = gltf["meshes"][0]["primitives"][0]
    assert {"JOINTS_0", "WEIGHTS_0"} <= set(prim["attributes"])

    def world_of(i):
        m4 = np.array(gltf["nodes"][i]["matrix"]).reshape(4, 4).T
        parent = next((j for j, n in enumerate(gltf["nodes"]) if i in n.get("children", [])), None)
        return m4 if parent is None else world_of(parent) @ m4

    rig = s.sessions[m["sid"]].model.rig
    want = posing.bone_world(rig, ("a000_003000", 5), {"hand": (0, 0, 45)})[2]
    from darkstl.export import Z_UP_TO_Y_UP

    got = world_of(3)
    assert np.allclose(got[:3, 3], Z_UP_TO_Y_UP @ want[:3, 3], atol=1e-5)  # metres, Y up
    assert np.allclose(got[:3, :3], Z_UP_TO_Y_UP @ want[:3, :3] @ Z_UP_TO_Y_UP.T, atol=1e-5)
    # an export without a pose stands in the bind pose; a model without bones has no skin
    plain = wait(s, s.call("export", {**args, "pose": None, "bones": None, "name": "bind"})["job"])
    data = open(plain["path"], "rb").read()
    gltf = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    assert np.allclose(np.array(gltf["nodes"][3]["matrix"]).reshape(4, 4).T[:3, 3], [0, 0.5, 0], atol=1e-6)


def test_the_clean_tab_works_on_the_posed_model_and_moves_its_bones(home, game, tmp_path):  # noqa: F811
    """Pose, then Clean, then Mesh: the clean-up is done on the model with its bones turned, and the Clean tab's
    transform (twice the size, on the floor) moves the bones the Pose tab shows and the GLB's skeleton with it."""
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    _, m = _open(s, "c2000")
    steps = [{"op": "transform", "scale": 2, "floor": True}]
    turned = {"hand": [0, 0, 45]}
    session = s.sessions[m["sid"]]
    from darkstl import clean as cleanup
    from darkstl.app.service import mesh_options

    posed = session.build(mesh_options({"bones": turned}))
    cleaned = session.cleaned(mesh_options({"bones": turned, "clean": steps}))
    affine = cleaned.affine
    assert affine is not None and np.allclose(np.linalg.det(affine[:3, :3]), 8)
    moved = posed.vertices @ affine[:3, :3].T + affine[:3, 3]
    assert np.allclose(_sorted_rows(cleaned.mesh.vertices), _sorted_rows(moved), atol=1e-6)
    assert abs(cleaned.mesh.vertices[:, 2].min()) < 1e-9  # on the floor
    assert np.allclose(cleanup.apply(posed, cleanup.CleanList.of(steps)).affine, affine)
    # the bones shown are where the cleaned-up model has them
    bones = s.call("model_bones", {"sid": m["sid"], "bones": turned, "clean": steps})["bones"]
    world = posing.bone_world(session.model.rig, None, {"hand": (0, 0, 45)})
    assert np.allclose(bones[2]["pos"], (affine @ world[2])[:3, 3] * 1000, atol=1e-3)
    # and the GLB's skeleton too, the cleaned-up mesh bound to it
    out = wait(s, s.call("export", {"sid": m["sid"], "which": "original", "format": ".glb", "units": "mm",
                                    "folder": str(tmp_path), "name": "cleaned", "bones": turned, "clean": steps})["job"])
    data = open(out["path"], "rb").read()
    gltf = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    assert gltf["nodes"][0]["skin"] == 0
    from darkstl.export import Z_UP_TO_Y_UP

    root = np.array(gltf["nodes"][1]["matrix"]).reshape(4, 4).T
    assert np.allclose(root[:3, 3], Z_UP_TO_Y_UP @ (affine @ world[0])[:3, 3], atol=1e-5)


def test_a_package_of_a_model_without_animations_keeps_its_bones(tmp_path, game):
    from darkstl import package

    model = open_model(game / "chr" / "c2001.chrbnd.dcx")
    folder = tmp_path / "pkg" / "c2001.chrbnd.dcx.darkpkg"
    folder.mkdir(parents=True)
    store = package.AssetStore(tmp_path / "pkg" / "assets")
    package._write_model(folder, "c2001", model, store, package.WriteOptions())
    data = json.loads((folder / "c2001.json").read_text())
    assert data["skeleton"] == 3 and data["animations"] == []
    index = {"format": package.FORMAT, "version": package.VERSION, "models": [{"file": "c2001.json"}], "source": "chr/c2001.chrbnd.dcx"}
    (folder / package.INDEX).write_text(json.dumps(index))
    packed = package.load_package(folder / package.INDEX)
    assert packed.animations is None and packed.rig is not None and packed.rig.names == ["root", "arm", "hand"]
    opts = MeshOptions(parts={1}, bones={"hand": (0, 0, 90)})
    assert np.allclose(packed.build_mesh(opts).vertices, model.build_mesh(opts).vertices, atol=1e-5)
