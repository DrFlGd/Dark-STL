"""Making a model watertight for printing (finish.py), the command line and the app's commands for it."""

import math
from pathlib import Path

import numpy as np
import pytest

manifold3d = pytest.importorskip("manifold3d")

from darkstl import finish as F  # noqa: E402
from darkstl.geometry.mesh import TriMesh, concatenate  # noqa: E402

from test_cut import figure_obj, tm, wait  # noqa: E402

m3 = manifold3d


def solid_of(mesh: TriMesh):
    s = F._to_manifold(mesh.vertices * 1000.0, mesh.faces)
    assert s is not None, "not watertight"
    return s


def test_crossing_parts_become_one_solid_without_the_surfaces_inside():
    body = m3.Manifold.cylinder(40, 8, 8, 64)
    arm = m3.Manifold.cylinder(40, 3, 3, 32).rotate((0, 90, 0)).translate((-20, 0, 30))
    mesh = concatenate([tm(body), tm(arm)])
    assert F.is_watertight(tm(body)) and not F.is_watertight(TriMesh(mesh.vertices, mesh.faces[:-1]))
    r = F.make_watertight(mesh)
    whole = solid_of(r.mesh)
    assert r.shells == 1 and whole.genus() == 0
    assert r.volume_mm3 == pytest.approx((body + arm).volume(), rel=1e-6)
    # no triangle is left inside the body: every vertex of the arm's surface left is outside it
    v = r.mesh.vertices * 1000
    r_xy = np.hypot(v[:, 0], v[:, 1])
    assert not ((r_xy < 7.9) & (v[:, 2] > 1) & (v[:, 2] < 39)).any()
    assert r.notes == ["2 parts joined into one; the surfaces inside one another removed."]


def test_open_surfaces_are_thickened_and_joined():
    box = tm(m3.Manifold.cube((30, 30, 30)))
    n = 10
    xs, ys = np.meshgrid(np.linspace(-10, 40, n), np.linspace(5, 25, n))
    v = np.column_stack([xs.ravel(), ys.ravel(), np.full(n * n, 15.0)])  # a sheet through the box, sticking out
    faces = [[i * n + j, i * n + j + 1, (i + 1) * n + j] for i in range(n - 1) for j in range(n - 1)]
    faces += [[i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j] for i in range(n - 1) for j in range(n - 1)]
    r = F.make_watertight(concatenate([box, TriMesh(v / 1000.0, np.array(faces))]), F.WatertightSettings(sheet_mm=1.0))
    solid_of(r.mesh)
    assert r.shells == 1
    assert r.volume_mm3 == pytest.approx(30 ** 3 + 20 * 20 * 1.0, rel=0.01)  # the 20 x 20 mm sticking out, 1 mm thick
    assert "1 open surface thickened to 1 mm." in r.notes


def hollow_pair():
    """Two half shells that only seal the space between them together: a closed coat in two parts."""
    outer = m3.Manifold.sphere(20, 64) - m3.Manifold.sphere(18, 64)
    top = outer.trim_by_plane((0, 0, 1), -1.0)  # overlapping by 2 mm at the waist
    bottom = outer.rotate((0, 0, 7)).trim_by_plane((0, 0, -1), -1.0)  # its triangles don't line up with the top's
    return concatenate([tm(top), tm(bottom)])


def test_hollows_sealed_inside_are_filled():
    mesh = hollow_pair()
    r = F.make_watertight(mesh)
    solid_of(r.mesh)
    assert r.hollows == 1 and r.shells == 1
    assert r.volume_mm3 == pytest.approx(4 / 3 * math.pi * 20 ** 3, rel=0.01)
    assert any(n.startswith("1 hollow sealed inside the model filled") for n in r.notes)
    kept = F.make_watertight(mesh, F.WatertightSettings(fill_hollows=False))
    assert kept.hollows == 0
    assert kept.volume_mm3 == pytest.approx(4 / 3 * math.pi * (20 ** 3 - 18 ** 3), rel=0.01)


def sleeve_and_arm():
    sleeve = m3.Manifold.cylinder(15, 4, 4, 48) - m3.Manifold.cylinder(17, 3.6, 3.6, 48).translate((0, 0, -1))
    arm = m3.Manifold.cylinder(25, 3, 3, 48).translate((0, 0, -5))
    return sleeve, arm


def test_narrow_gaps_are_closed_without_changing_the_outside():
    sleeve, arm = sleeve_and_arm()
    mesh = concatenate([tm(sleeve), tm(arm)])
    plain = F.make_watertight(mesh)
    assert plain.shells == 2  # the sleeve doesn't touch the arm
    r = F.make_watertight(mesh, F.WatertightSettings(close_gaps_mm=1.0))
    solid = solid_of(r.mesh)
    assert r.shells == 1
    gap = math.pi * (3.6 ** 2 - 3 ** 2) * 15
    assert r.gap_volume_mm3 == pytest.approx(gap, rel=0.15)
    # the outside is where it was: the sleeve's outer wall and the arm's ends
    lo, hi = np.asarray(solid.bounding_box()[:3]), np.asarray(solid.bounding_box()[3:])
    assert lo == pytest.approx([-4, -4, -5], abs=0.02) and hi == pytest.approx([4, 4, 20], abs=0.02)
    ring = solid.slice(7.5)
    assert ring.area() == pytest.approx(math.pi * 16, rel=0.02)  # one disc of the sleeve's radius: the gap filled
    assert any(n.startswith("Gaps up to 1 mm wide closed") for n in r.notes)


def test_gaps_wider_than_asked_stay_open():
    sleeve, arm = sleeve_and_arm()
    r = F.make_watertight(concatenate([tm(sleeve), tm(arm)]), F.WatertightSettings(close_gaps_mm=0.3))
    assert r.shells == 2 and r.gap_volume_mm3 < 1.0


def test_a_model_too_big_for_gaps_that_narrow_says_so():
    big = m3.Manifold.cube((1800, 600, 400))
    r = F.make_watertight(tm(big), F.WatertightSettings(close_gaps_mm=1.0))
    assert r.gap_volume_mm3 == 0
    assert F.narrowest_gap([1800, 600, 400]) > 1
    assert any(n.startswith("Gaps weren't closed: the model is too big (1800 mm)") for n in r.notes)
    assert F.gap_grid([1800, 600, 400], 1.0) is None and F.gap_grid([50, 50, 50], 1.0) == 0.25


def test_loose_bits_are_removed_when_asked():
    body = m3.Manifold.cube((20, 20, 20))
    speck = m3.Manifold.sphere(0.8, 16).translate((40, 0, 0))
    buckle = m3.Manifold.cube((6, 6, 6)).translate((30, 0, 0))  # 2.7% of the body
    mesh = concatenate([tm(body), tm(speck), tm(buckle)])
    assert F.make_watertight(mesh).shells == 3
    r = F.make_watertight(mesh, F.WatertightSettings(loose_percent=1.0))
    assert r.shells == 2 and r.loose_removed == 1
    r = F.make_watertight(mesh, F.WatertightSettings(loose_percent=5.0))
    assert r.shells == 1 and r.loose_removed == 2
    assert "2 loose bits smaller than 5% of the model removed." in r.notes


def test_sub_meshes_that_touch_at_a_point_keep_their_detail():
    """Two closed parts touching at one corner (a plate resting on a body) are each made solid as they are,
    not taken as one broken part and rebuilt from voxels."""
    a = m3.Manifold.cube((10, 10, 10))
    b = m3.Manifold.cube((10, 10, 10)).translate((10, 10, 10))  # touches a at the corner (10, 10, 10)
    r = F.make_watertight(concatenate([tm(a), tm(b)]))
    assert not any("voxels" in n for n in r.notes)
    assert r.volume_mm3 == pytest.approx(2000, rel=1e-6)


def test_settings_from_the_page():
    s = F.settings_from_json({"sheet_mm": 0, "fill_hollows": False, "close_gaps_mm": 999, "loose_percent": "x"})
    assert s.sheet_mm == 0.2 and not s.fill_hollows and s.close_gaps_mm == 50 and s.loose_percent == 0
    assert F.settings_from_json({}) == F.WatertightSettings()


def test_the_command_line_writes_a_watertight_model(tmp_path, capsys):
    from darkstl.cli import main

    out = tmp_path / "figure.ply"
    assert main(["export", str(figure_obj(tmp_path)), "-o", str(out), "--watertight", "--drop-loose", "1"]) == 0
    assert "3 parts joined into one" in capsys.readouterr().err
    assert out.stat().st_size > 0


def test_the_app_makes_it_watertight_shows_exports_and_cuts_it(tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.meshpack import unpack_mesh
    from darkstl.app.service import AppError, Service

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    s = Service(Config.load(), tmp_path / "home" / "cache")
    m = s.call("model_open", {"path": str(figure_obj(tmp_path))})
    assert m["finished"] is None
    sid = m["sid"]
    with pytest.raises(AppError, match="Make the enhanced model first"):
        s.call("finish", {"sid": sid, "from": "enhanced"})
    with pytest.raises(AppError, match="Make the model watertight first"):
        s.call("export", {"sid": sid, "which": "finished"})
    r = wait(s, s.call("finish", {"sid": sid, "from": "original", "watertight": {}})["job"])
    assert r["stats"]["shells"] == 1 and r["stats"]["watertight"] and not r["kept"]
    assert "3 parts joined into one; the surfaces inside one another removed." in r["notes"]
    header, _ = unpack_mesh(s.call("model_mesh", {"sid": sid, "which": "finished"}))
    assert header["which"] == "finished"
    again = wait(s, s.call("finish", {"sid": sid, "from": "original", "watertight": {}})["job"])
    assert again["kept"] and again["id"] == r["id"]
    out = wait(s, s.call("export", {"sid": sid, "which": "finished", "format": ".stl", "folder": str(tmp_path / "out"),
                                    "name": "figure_watertight"})["job"])
    assert Path(out["path"]).name == "figure_watertight.stl"
    plan = {"cuts": [{"normal": [0, 0, 1], "point": [0, 0, 30], "joint": {"kind": "flat"}}]}
    cut = wait(s, s.call("cut", {"sid": sid, "from": "finished", "cut": plan})["job"])
    assert len(cut["pieces"]) == 2 and cut["notes"] == []  # already one solid: nothing to close, thicken or join
    s.sessions.clear()
    assert s.call("model_open", {"path": str(tmp_path / "figure.obj")})["finished"]["id"] == r["id"]
