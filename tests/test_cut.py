"""Cutting a model into pieces for printing (cut.py), and the app's commands for it."""

import math
import time
from pathlib import Path

import numpy as np
import pytest

manifold3d = pytest.importorskip("manifold3d")

from darkstl import cut as C  # noqa: E402
from darkstl.geometry.mesh import TriMesh, concatenate  # noqa: E402


def tm(solid) -> TriMesh:
    """A manifold built in millimetres, as the app's meshes are: metres."""
    m = solid.to_mesh()
    return TriMesh(np.asarray(m.vert_properties)[:, :3] / 1000.0, np.asarray(m.tri_verts))


def watertight(piece: C.Piece):
    solid = C._to_manifold(piece.mesh.vertices * 1000.0, piece.mesh.faces)
    assert solid is not None, f"{piece.name} isn't watertight"
    return solid


def cylinder(h=40.0, r=10.0):
    return tm(manifold3d.Manifold.cylinder(h, r, r, 96))


def across_z(at, **joint):
    return C.CutPlane([0, 0, 1], [0, 0, at], C.Joint(**joint))


def test_a_flat_cut_splits_a_solid_into_two_watertight_halves():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="flat")]))
    assert [p.name for p in r.pieces] == ["Piece 1", "Piece 2"]
    whole = math.pi * 10 ** 2 * 40
    vols = [watertight(p).volume() for p in r.pieces]
    assert sum(vols) == pytest.approx(whole, rel=0.01)
    assert vols[0] == pytest.approx(vols[1], rel=1e-3)
    assert r.pieces[0].stats()["center_mm"][2] > r.pieces[1].stats()["center_mm"][2]  # the top first
    assert r.joints == [{"cut": 0, "crossed": 1, "placed": 0, "skipped": 0, "kind": "flat"}]


def test_pegs_stand_on_one_side_and_fit_holes_wider_by_the_clearance():
    clr, d, depth = 0.25, 4.0, 5.0
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="pegs", diameter_mm=d, depth_mm=depth, clearance_mm=clr, count=2)]))
    top, bottom = (watertight(p) for p in r.pieces)
    assert r.joints[0]["placed"] == 2
    half = math.pi * 100 * 20
    peg = math.pi * (d / 2) ** 2 * depth
    hole = math.pi * (d / 2 + clr) ** 2 * (depth + clr)
    # the top piece points the way the cut's normal does, so the pegs stand on it, into the bottom piece
    assert top.volume() == pytest.approx(half + 2 * peg, rel=0.01)
    assert bottom.volume() == pytest.approx(half - 2 * hole, rel=0.01)
    # through the holes, 1 mm below the cut: two circles of the peg's radius plus the clearance
    ring = bottom.slice(19.0)
    assert ring.area() == pytest.approx(math.pi * 100 - 2 * math.pi * (d / 2 + clr) ** 2, rel=0.01)
    pegs = top.slice(19.0)
    assert pegs.area() == pytest.approx(2 * math.pi * (d / 2) ** 2, rel=0.02)


def test_flipped_pegs_stand_on_the_other_side():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="pegs", flip=True, count=1)]))
    top, bottom = (watertight(p) for p in r.pieces)
    assert bottom.volume() > top.volume()
    assert r.pieces[1].faces[0][2] and not r.pieces[0].faces[0][2]  # the raised face is the bottom piece's


def test_dowels_make_holes_both_sides_and_dowel_pieces():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="dowels", diameter_mm=3, depth_mm=6, clearance_mm=0.2)]))
    kinds = [p.kind for p in r.pieces]
    assert kinds == ["piece", "piece", "dowel", "dowel"]
    dowel = watertight(r.pieces[2])
    assert dowel.volume() == pytest.approx(math.pi * 1.5 ** 2 * 12, rel=0.02)  # 6 mm into each side
    off = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="dowels", print_dowels=False)]))
    assert [p.kind for p in off.pieces] == ["piece", "piece"]


def test_magnet_pockets_on_both_sides():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="magnets", magnet_diameter_mm=6, magnet_height_mm=2,
                                                       clearance_mm=0.1, count=1)]))
    top, bottom = (watertight(p) for p in r.pieces)
    pocket = math.pi * 3.1 ** 2 * 2.1
    assert top.volume() == pytest.approx(math.pi * 100 * 20 - pocket, rel=0.01)
    assert bottom.volume() == pytest.approx(top.volume(), rel=1e-3)


def test_a_key_follows_the_shape_of_the_cut_face():
    box = tm(manifold3d.Manifold.cube((30, 12, 40)))
    r = C.cut_mesh(box, C.CutSettings([across_z(20, kind="key", depth_mm=4, clearance_mm=0.2)], wall_mm=2))
    top, bottom = (watertight(p) for p in r.pieces)
    tongue = top.slice(18.0)  # 2 mm below the cut: the tongue, inset 2 mm from the box's sides
    lo, hi = np.array(tongue.bounds()[:2]), np.array(tongue.bounds()[2:])
    assert hi - lo == pytest.approx([26, 8], abs=0.05)
    groove = bottom.slice(18.0)
    assert groove.area() == pytest.approx(30 * 12 - 26.4 * 8.4, rel=0.01)  # wider by the clearance (inset corners stay sharp)


def test_a_feature_with_no_room_leaves_the_cut_flat_and_says_so():
    thin = tm(manifold3d.Manifold.cube((40, 2, 40)))  # a 2 mm plate: no room for a 5 mm peg
    r = C.cut_mesh(thin, C.CutSettings([across_z(20, kind="pegs")]))
    assert len(r.pieces) == 2 and r.joints[0]["placed"] == 0
    assert any("no room for a peg" in n for n in r.notes)


def test_a_peg_is_shortened_where_the_far_piece_is_thin():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(36, kind="pegs", depth_mm=8, count=1)]))
    top, bottom = (watertight(p) for p in r.pieces)
    assert r.joints[0]["placed"] == 1
    assert bottom.volume() < math.pi * 100 * 36  # the hole went into the long part
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(4, kind="pegs", depth_mm=8, count=1)]))
    assert r.joints[0]["placed"] == 1  # 4 mm left below the cut: the peg is cut down to fit
    for p in r.pieces:
        watertight(p)


def test_cuts_in_any_direction_and_several_of_them():
    sphere = tm(manifold3d.Manifold.sphere(20, 96))
    settings = C.CutSettings([C.CutPlane([1, 0.3, 0.2], [0, 0, 0], C.Joint(kind="pegs")),
                              C.CutPlane([0, 0, 1], [0, 0, 5], C.Joint(kind="magnets"))])
    r = C.cut_mesh(sphere, settings)
    assert len([p for p in r.pieces if p.kind == "piece"]) == 4
    assert [j["crossed"] for j in r.joints] == [1, 2]
    for p in r.pieces:
        watertight(p)


def test_a_cut_that_misses_the_model_says_so():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(100, kind="flat")]))
    assert len(r.pieces) == 1 and "Cut 1 doesn't go through the model." in r.notes


def test_open_parts_are_closed_or_thickened_and_joined_into_one_solid():
    box = tm(manifold3d.Manifold.cube((30, 30, 30)))
    cup = box.subset(box.face_normals()[:, 2] < 0.9)  # a box without its lid: a small opening, closed
    n = 12
    xs, ys = np.meshgrid(np.linspace(0, 40, n), np.linspace(0, 40, n))
    v = np.column_stack([xs.ravel() + 60, ys.ravel(), np.full(n * n, 10.0)])
    faces = [[i * n + j, i * n + j + 1, (i + 1) * n + j] for i in range(n - 1) for j in range(n - 1)]
    faces += [[i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j] for i in range(n - 1) for j in range(n - 1)]
    sheet = TriMesh(v / 1000.0, np.array(faces))  # a flat cape: thickened
    ball = tm(manifold3d.Manifold.sphere(10, 48).translate((15, 15, 30)))  # overlaps the cup: joined
    solid = C.make_solid(concatenate([cup, sheet, ball]), sheet_mm=1.5)
    assert solid.notes == ["1 part had openings, closed flat.", "1 open surface thickened to 1.5 mm.", "3 parts joined into one solid."]
    expect = 30 ** 3 + 40 * 40 * 1.5 + (4 / 3 * math.pi * 1000) / 2  # half the ball sticks out of the cup
    assert solid.manifold.volume() == pytest.approx(expect, rel=0.02)


def test_two_sided_duplicates_and_broken_parts_still_make_a_solid():
    box = tm(manifold3d.Manifold.cube((10, 10, 10)))
    doubled = concatenate([box, box])  # the same triangles twice: kept once
    fin = TriMesh(np.array([[0, 0, 0], [0, 0, 10], [-8, -4, 5.0]]) / 1000, np.array([[0, 1, 2]]))
    broken = concatenate([box, fin, TriMesh(fin.vertices, np.array([[0, 2, 1]]))])  # three faces on one edge
    assert C.make_solid(doubled).notes == []
    solid = C.make_solid(broken)
    assert "1 broken part rebuilt from voxels, with less detail." in solid.notes
    assert solid.manifold.volume() > 900


def test_pieces_are_laid_on_a_cut_face_without_pegs_on_it():
    r = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="pegs"), C.CutPlane([1, 0, 0], [0, 0, 0], C.Joint(kind="flat"))]))
    for p in r.pieces:
        laid = C.print_pose(p)
        lo, hi = laid.bounds()
        assert lo[2] == pytest.approx(0, abs=1e-9)
        assert (lo[:2] + hi[:2]) == pytest.approx([0, 0], abs=1e-9)
        # a quarter of a cylinder lies on the flat face of the vertical cut, its biggest (20 x 20 mm)
        fn = laid.face_normals()
        down = laid.face_areas()[fn[:, 2] < -0.999].sum() * 1e6
        assert 400 * 0.9 < down < 400 * 1.1  # less the holes, or more by the pegs, cut by it
    # with only the pegged face to lie on, a piece stays upright
    top = C.cut_mesh(cylinder(), C.CutSettings([across_z(20, kind="pegs")])).pieces[0]
    assert np.allclose(C.print_pose(top).vertices, C._on_bed(top.mesh).vertices)
    bottom = C.cut_mesh(cylinder(), C.CutSettings([across_z(30, kind="pegs")])).pieces[1]  # the holed face is the bigger one
    turned = C.print_pose(bottom)
    assert turned.bounds()[1][2] == pytest.approx(0.030, abs=1e-5)  # turned over: 30 mm tall on its cut face


def test_settings_from_the_page_and_the_command_line():
    s = C.settings_from_json({"cuts": [{"normal": [0, 0, 2], "point": [1, 2, 3], "joint": {"kind": "magnets", "clearance_mm": 9, "count": 99}}],
                              "sheet_mm": 0.01})
    cut = s.cuts[0]
    assert cut.normal.tolist() == [0, 0, 1] and cut.offset == 3
    assert cut.joint.kind == "magnets" and cut.joint.clearance_mm == 5 and cut.joint.count == C.MAX_FEATURES
    assert s.sheet_mm == 0.2
    with pytest.raises(ValueError):
        C.settings_from_json({"cuts": [{"normal": [0, 0, 1], "point": [0, 0, 0], "joint": {"kind": "glue"}}]})
    with pytest.raises(ValueError):
        C.CutPlane([0, 0, 0], [0, 0, 0])
    mesh = cylinder()
    assert C.parse_cut("z=12:dowels").offset == 12 and C.parse_cut("z=12:dowels").joint.kind == "dowels"
    assert C.parse_cut("x=-5").normal.tolist() == [1, 0, 0] and C.parse_cut("x=-5").joint.kind == "pegs"
    assert C.parse_cut("z=25%", mesh).offset == pytest.approx(10)
    with pytest.raises(ValueError):
        C.parse_cut("w=3")


def figure_obj(folder: Path) -> Path:
    """A small figure (body, head and arms, overlapping as game parts do) as an OBJ in millimetres."""
    from darkstl.export import ExportOptions, export_mesh

    m3 = manifold3d
    body = m3.Manifold.cylinder(50, 12, 9, 64)
    head = m3.Manifold.sphere(9, 48).translate((0, 0, 57))
    arms = m3.Manifold.cylinder(56, 4, 4, 32).rotate((0, 90, 0)).translate((-28, 0, 42))
    path = folder / "figure.obj"
    export_mesh(concatenate([tm(body), tm(head), tm(arms)]), path, ExportOptions(scale=1000.0))
    return path



def wait(service, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        job = service.call("job", {"id": job_id})
        if job["state"] != "running":
            assert job["state"] == "done", job["error"]
            return job["result"]
        time.sleep(0.02)
    raise AssertionError("job didn't finish")


def test_the_app_cuts_shows_keeps_and_exports_the_pieces(tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.meshpack import unpack_mesh
    from darkstl.app.service import AppError, Service

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    s = Service(Config.load(), tmp_path / "home" / "cache")
    assert s.call("app_info", {})["cutting"]
    m = s.call("model_open", {"path": str(figure_obj(tmp_path))})
    assert m["cut"] is None
    sid = m["sid"]
    plan = {"cuts": [{"normal": [0, 0, 1], "point": [0, 0, 30], "joint": {"kind": "pegs", "clearance_mm": 0.15}}]}
    with pytest.raises(AppError, match="Make the enhanced model first"):
        s.call("cut", {"sid": sid, "from": "enhanced", "cut": plan})
    with pytest.raises(AppError, match="Add a cut first"):
        s.call("cut", {"sid": sid, "from": "original", "cut": {"cuts": []}})
    with pytest.raises(AppError, match="can't be read"):
        s.call("cut", {"sid": sid, "from": "original", "cut": {"cuts": [{"normal": [0, 0, 1]}]}})
    r = wait(s, s.call("cut", {"sid": sid, "from": "original", "cut": plan})["job"])
    assert [p["name"] for p in r["pieces"]] == ["Piece 1", "Piece 2"] and not r["kept"] and not r["solid_reused"]
    assert r["joints"][0]["placed"] == 2
    header, arrays = unpack_mesh(s.call("model_mesh", {"sid": sid, "which": "pieces"}))
    assert [p["name"] for p in header["pieces"]] == ["Piece 1", "Piece 2"]
    assert sorted({g["material"] for g in header["groups"]}) == [0, 1]
    # the same cuts again: kept; another position: the solid is reused
    again = wait(s, s.call("cut", {"sid": sid, "from": "original", "cut": plan})["job"])
    assert again["kept"] and again["id"] == r["id"]
    plan["cuts"][0]["point"] = [0, 0, 20]
    moved = wait(s, s.call("cut", {"sid": sid, "from": "original", "cut": plan})["job"])
    assert moved["solid_reused"] and not moved["kept"]
    assert s.call("cut_state", {"sid": sid})["id"] == moved["id"]
    out = wait(s, s.call("export", {"sid": sid, "which": "pieces", "format": ".stl", "folder": str(tmp_path / "out")})["job"])
    assert Path(out["path"]).name == "figure_pieces"
    assert [Path(f).name for f in out["files"]] == ["figure_piece1.stl", "figure_piece2.stl"]
    again = wait(s, s.call("export", {"sid": sid, "which": "pieces", "format": ".stl", "folder": str(tmp_path / "out")})["job"])
    assert Path(again["path"]).name == "figure_pieces (2)"  # a second export doesn't overwrite the first
    # the enhanced model (here evened out only) is cut the same way
    wait(s, s.call("enhance", {"sid": sid, "step": "mesh", "settings": {"target_vertices": 4000}})["job"])
    enhanced = wait(s, s.call("cut", {"sid": sid, "from": "enhanced", "cut": plan})["job"])
    assert len(enhanced["pieces"]) == 2 and enhanced["id"] != moved["id"] and not enhanced["solid_reused"]
    # opened again, the model shows its pieces
    s.sessions.clear()
    assert s.call("model_open", {"path": str(tmp_path / "figure.obj")})["cut"]["id"] == enhanced["id"]


def test_a_cut_on_a_face_of_the_model_leaves_no_sheet_behind():
    base = manifold3d.Manifold.cube((50, 50, 12), True).translate((0, 0, 6))
    torso = manifold3d.Manifold.cylinder(40, 16, 12, 96).translate((0, 0, 12))
    r = C.cut_mesh(tm(base + torso), C.CutSettings([across_z(12, kind="key")]))  # exactly on the base's top
    sizes = [p.stats()["size_mm"] for p in r.pieces]
    assert sizes[0][:2] == pytest.approx([32, 32], abs=0.05)  # the torso alone, not the base's top face with it
    for p in r.pieces:
        watertight(p)
