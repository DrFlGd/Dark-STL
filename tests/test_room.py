"""Room around thin parts (geometry.room), triangles passing through each other (geometry.intersect), keeping
thin parts from being displaced through themselves, and the thickness the 3D view's Thickness shading shows."""

import numpy as np
import pytest

from darkstl import bench
from darkstl.app.meshpack import pack_mesh, unpack_mesh
from darkstl.geometry.intersect import count_crossings, crossing_pairs, crossing_pairs_among
from darkstl.geometry.mesh import TriMesh, concatenate
from darkstl.geometry.room import (Room, cap_heights, closed_parts, keep_thin_parts, measure_room, neighbours, settle,
                                   thickness)
from darkstl.geometry.subdivide import subdivide_to_vertex_count

MM = 0.001


def _sheet(size, z, step, facing_up=True):
    """A flat square sheet ``size`` wide at height ``z`` (metres), facing up or down."""
    n = max(1, int(round(size / step)))
    return bench.surface(lambda u, v: np.stack([u * size, v * size, np.full_like(u, z)], -1), n, n, flip=not facing_up)


def _cone(radius, length, vertices):
    """An open cone, base at z = 0, tip at z = ``length``, normals out, evened out to about ``vertices``."""
    def fn(u, v):
        r = radius * (1 - u)
        a = 2 * np.pi * v
        return np.stack([r * np.cos(a), r * np.sin(a), u * length], -1)
    coarse = bench.surface(fn, 40, 48, flip=True)
    return subdivide_to_vertex_count(TriMesh(coarse.vertices, coarse.faces), vertices)


def _middle(mesh, lo, hi):
    """Vertices whose x and y lie between ``lo`` and ``hi``."""
    v = mesh.vertices
    return (v[:, 0] > lo) & (v[:, 0] < hi) & (v[:, 1] > lo) & (v[:, 1] < hi)


# ---------------------------------------------------------------- room


def test_room_in_a_slab_is_half_its_thickness():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 0.5 * MM)
    room = measure_room(slab, 20 * MM)
    top = _middle(slab, 5 * MM, 35 * MM) & np.isclose(slab.vertices[:, 2], 2 * MM)
    assert top.sum() > 1000
    assert np.allclose(room.inward[top], 1 * MM, rtol=0.1)
    assert room.solid[top].all()
    assert np.isinf(room.outward[top]).all()  # nothing in front of a slab
    assert np.allclose(room.thickness()[top], 2 * MM, rtol=0.1)


def test_room_between_two_sheets_is_half_the_gap():
    lower = _sheet(30 * MM, 0.0, 0.5 * MM, facing_up=True)
    upper = _sheet(30 * MM, 3 * MM, 0.5 * MM, facing_up=False)
    pair = concatenate([lower, upper])
    room = measure_room(pair, 20 * MM)
    mid = _middle(pair, 5 * MM, 25 * MM)
    assert np.allclose(room.outward[mid], 1.5 * MM, rtol=0.1)
    # behind each sheet there is nothing: no thickness of its own
    assert np.isinf(room.inward[mid]).all()
    assert not room.solid[mid].any()
    assert (room.thickness()[mid] == 0).all()


def test_room_narrows_towards_a_cone_tip_and_never_says_more_than_there_is():
    radius, length = 8 * MM, 60 * MM
    cone = _cone(radius, length, 30_000)
    room = measure_room(cone, 40 * MM)
    v = cone.vertices
    z = v[:, 2]
    r = np.hypot(v[:, 0], v[:, 1])
    slope = np.arctan2(radius, length)
    # the ball inside, touching the surface, reaches the axis: radius r / cos(half angle)
    truth = r / np.cos(slope)
    mid = (z > 0.3 * length) & (z < 0.8 * length)
    tip = z > 0.9 * length
    assert np.median(room.inward[mid] / truth[mid]) == pytest.approx(1.0, abs=0.1)
    assert (room.inward[tip] <= truth[tip] * 1.1 + 0.05 * MM).all()
    assert room.solid[mid].all()
    near_tip = room.inward[(z > 0.85 * length) & (z < 0.95 * length)]
    assert np.median(near_tip) < 0.5 * np.median(room.inward[mid])


def test_room_is_only_looked_for_as_far_as_asked():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 0.5 * MM)
    reach = np.full(slab.n_vertices, 0.5 * MM)  # less than the half thickness: nothing is met
    room = measure_room(slab, reach)
    top = _middle(slab, 5 * MM, 35 * MM) & np.isclose(slab.vertices[:, 2], 2 * MM)
    assert np.isinf(room.inward[top]).all()
    still = measure_room(slab, 0.0)  # vertices that don't move need no room
    assert np.isinf(still.inward).all() and np.isinf(still.outward).all()


def test_room_measured_at_a_spread_of_vertices_matches_every_vertex():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 0.5 * MM)
    every = measure_room(slab, 20 * MM)
    few = measure_room(slab, 20 * MM, queries=2000)
    top = _middle(slab, 5 * MM, 35 * MM) & np.isclose(slab.vertices[:, 2], 2 * MM)
    assert np.allclose(few.inward[top], every.inward[top], rtol=0.15)
    assert few.solid[top].all()


def test_settling_keeps_the_tightest_room_and_evens_it_out():
    sheet = _sheet(10 * MM, 0.0, 1 * MM)
    pairs = neighbours(sheet)
    room = np.full(sheet.n_vertices, np.inf)
    one = sheet.n_vertices // 2
    room[one] = 1 * MM
    out = settle(sheet.n_vertices, room, pairs, rings=1, passes=0)
    owner, other = pairs
    ring = other[owner == one]
    assert np.allclose(out[ring], 1 * MM) and out[one] == 1 * MM
    far = np.ones(sheet.n_vertices, bool)
    far[ring] = False
    far[one] = False
    assert np.isinf(out[far]).any()


# ---------------------------------------------------------------- the cap


def _room(inward, outward, reach):
    n = len(inward)
    return Room(np.asarray(inward, float), np.asarray(outward, float), np.ones(n, bool), np.full(n, reach))


def test_cap_lowers_heights_where_the_room_is_small():
    room = _room([1 * MM, 1 * MM], [1 * MM, 1 * MM], 10 * MM)
    out, info = cap_heights(np.array([3 * MM, -3 * MM]), room, used=0.8)
    # never reaching the room's 80 %, on either side
    assert (np.abs(out) < 0.8 * MM).all() and np.abs(out).min() > 0.7 * MM
    assert out[0] > 0 > out[1]
    assert info["capped"] == 2 and info["max_reduction"] == pytest.approx(3 * MM - abs(out[0]))
    assert info["kept"] < 0.3


def test_cap_takes_the_room_on_the_side_the_height_points():
    room = _room([0.5 * MM], [np.inf], 10 * MM)
    up, _ = cap_heights(np.array([2 * MM]), room)
    down, _ = cap_heights(np.array([-2 * MM]), room)
    assert up[0] == 2 * MM and down[0] > -0.4 * MM


def test_cap_leaves_small_heights_and_open_areas_alone():
    # a height well under its room is all but unchanged (tanh is straight near 0)
    room = _room([4 * MM], [4 * MM], 40 * MM)
    out, info = cap_heights(np.array([0.1 * MM]), room)
    assert out[0] == pytest.approx(0.1 * MM, rel=1e-3) and info["capped"] == 0
    # no room limit found (inf), or room as far as was looked: not changed at all
    room = _room([np.inf, 10 * MM], [np.inf, 10 * MM], 10 * MM)
    h = np.array([5 * MM, 5 * MM])
    out, info = cap_heights(h, room)
    assert np.array_equal(out, h) and info["capped"] == 0 and info["kept"] == 1.0


def test_keeping_thin_parts_leaves_a_flat_open_panel_as_it_is():
    panel = bench.panel_mesh(64)
    rng = np.random.default_rng(3)
    h = rng.normal(0, 1 * MM, panel.n_vertices)
    out, nrm, info = keep_thin_parts(panel, h)
    assert np.array_equal(out, h) and np.array_equal(nrm, panel.vertex_normals())
    assert info["capped"] == 0 and info["crossings_found"] == 0


def test_keeping_thin_parts_leaves_thick_parts_alone():
    block = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 40 * MM], 1 * MM)
    rng = np.random.default_rng(4)
    h = rng.normal(0, 0.3 * MM, block.n_vertices)
    out, _, info = keep_thin_parts(block, h)
    face = _middle(block, 5 * MM, 35 * MM) & np.isclose(block.vertices[:, 2], 40 * MM)
    assert np.allclose(out[face], h[face], rtol=0.02, atol=1e-9)
    assert info["kept"] > 0.98


def test_keeping_thin_parts_lowers_relief_on_a_thin_slab():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 0.5 * MM)
    h = np.full(slab.n_vertices, -3 * MM)  # every side pushed 3 mm in: through a 2 mm slab
    out, _, info = keep_thin_parts(slab, h)
    top = _middle(slab, 5 * MM, 35 * MM) & np.isclose(slab.vertices[:, 2], 2 * MM)
    assert (np.abs(out[top]) < 0.8 * MM).all()
    assert info["capped"] > top.sum()


@pytest.mark.parametrize("scene", sorted(bench.THIN_SCENES))
def test_thin_scenes_no_longer_pass_through_themselves(scene):
    r = bench.run_thin(scene, vertices=20_000)
    assert r["before"] == 0  # the scenes themselves are clean
    assert r["without"] > 10  # displacement alone pushes them through themselves
    assert r["with"] == 0
    assert r["kept"] > 75 and r["capped"] > 0


def test_thin_parts_are_reported():
    model = bench.scene_claws()
    from darkstl.pipeline import EnhanceSettings, enhance
    on = enhance(model, EnhanceSettings(target_vertices=8000, smooth=0.0))
    st = on.stats()
    assert st["thin_capped"] > 0 and 0 < st["thin_max_reduction_mm"] and 0 < st["thin_kept_percent"] <= 100
    assert any(w.startswith("Thin parts:") for w in on.warnings)
    off = enhance(model, EnhanceSettings(target_vertices=8000, smooth=0.0, thin_parts=False))
    assert off.thin is None and "thin_capped" not in off.stats()


@pytest.mark.parametrize("scene", ["plates", "cloth"])
def test_bench_panels_are_the_same_with_thin_parts_kept(scene):
    # nothing is thin on a panel: keeping thin parts changes nothing
    on = bench.run(scene, "8bit", bench.Method("on", {}), 20_000)
    off = bench.run(scene, "8bit", bench.Method("off", {"thin_parts": False}), 20_000)
    # (the same to rounding: the pipeline's threaded sums differ by about 1e-12 from run to run)
    assert on["error"] == pytest.approx(off["error"], rel=1e-6) and on["detail"] == pytest.approx(off["detail"], rel=1e-6)


# ---------------------------------------------------------------- triangles passing through each other


def _tri(*pts):
    return np.array(pts, float)


def test_crossing_triangles_are_found():
    a = _tri((0, 0, 0), (1, 0, 0), (0, 1, 0))
    b = _tri((0.2, 0.2, -0.5), (0.3, 0.2, 0.5), (0.2, 0.3, 0.5))  # through a
    c = _tri((5, 5, 0), (6, 5, 0), (5, 6, 0))  # far away
    mesh = TriMesh(np.concatenate([a, b, c]), np.arange(9).reshape(3, 3))
    assert crossing_pairs(mesh).tolist() == [[0, 1]]
    assert count_crossings(mesh) == 1
    assert crossing_pairs_among(mesh, np.array([0, 2])).shape == (0, 2)
    assert crossing_pairs_among(mesh, np.array([1, 0, 2])).tolist() == [[1, 0]]


def test_touching_and_neighbouring_triangles_do_not_cross():
    # sharing a corner, lying side by side, or only touching along an edge
    a = _tri((0, 0, 0), (1, 0, 0), (0, 1, 0))
    b = _tri((1, 0, 0), (1, 1, 0), (0, 1, 0))
    c = _tri((0, 0, 0), (1, 0, 0), (0.5, 0, 1))
    mesh = TriMesh(np.concatenate([a, b, c]), np.arange(9).reshape(3, 3))
    assert count_crossings(mesh) == 0
    assert count_crossings(bench.box_mesh([0, 0, 0], [1, 1, 1], 0.1)) == 0
    assert count_crossings(TriMesh(a, np.array([[0, 1, 2]]))) == 0


def test_boxes_through_each_other_cross_and_a_big_triangle_is_not_missed():
    one = bench.box_mesh([0, 0, 0], [1, 1, 1], 0.1)
    two = bench.box_mesh([0.537, 0.521, 0.513], [1.537, 1.521, 1.513], 0.1)  # off the grid: no edge meets an edge
    assert count_crossings(concatenate([one, two])) > 0
    # one huge triangle through a field of small ones: found though it is far bigger than the rest
    big = TriMesh(_tri((-5, -5, 0.55), (5, -5, 0.55), (0, 5, 0.55)), np.array([[0, 1, 2]]))
    assert count_crossings(concatenate([one, big])) > 0


# ---------------------------------------------------------------- the Thickness shading


def test_thickness_of_a_slab_and_of_a_single_sheet():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 0.5 * MM)
    t = thickness(slab)
    top = _middle(slab, 5 * MM, 35 * MM) & np.isclose(slab.vertices[:, 2], 2 * MM)
    assert np.allclose(t[top], 2 * MM, rtol=0.1)
    sheet = _sheet(30 * MM, 0.0, 0.5 * MM)
    assert (thickness(sheet) == 0).all()
    assert len(thickness(TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64)))) == 0


def test_overlapping_parts_are_measured_as_one_solid():
    # a square rod pushed into a block, as game parts overlap: its end inside the block is hidden and stops nothing
    block = bench.box_mesh([0, 0, 0], [20 * MM, 20 * MM, 20 * MM], 1 * MM)
    rod = bench.box_mesh([5.37 * MM, 5.21 * MM, 10.13 * MM], [14.37 * MM, 14.21 * MM, 30.13 * MM], 1 * MM)
    both = concatenate([block, rod])
    assert closed_parts(both).all() and not closed_parts(_sheet(10 * MM, 0.0, 1 * MM)).any()
    t = thickness(both)
    z = both.vertices[:, 2]
    on_rod = np.arange(both.n_vertices) >= block.n_vertices
    assert np.isinf(t[on_rod & (z < 19 * MM)]).all()
    sides = on_rod & (z > 23 * MM) & (z < 28 * MM)  # the rod's walls above the block: 9 mm across
    assert np.median(t[sides]) == pytest.approx(9 * MM, rel=0.15)
    # the block's top beside the rod: as thick as the block, not stopped by the rod's hidden end
    v = both.vertices
    beside = ~on_rod & np.isclose(z, 20 * MM) & (v[:, 0] > 2 * MM) & (v[:, 0] < 4 * MM) & (v[:, 1] > 6 * MM) & (v[:, 1] < 14 * MM)
    assert beside.any() and (t[beside] > 15 * MM).all()


def test_thickness_of_a_coarse_model_is_measured_on_a_finer_stand_in():
    # a slab of a few big triangles, as game meshes often are: its walls still measure
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 20 * MM)
    t = thickness(slab, most=200_000)
    top = np.isclose(slab.vertices[:, 2], 2 * MM)
    assert np.allclose(t[top], 2 * MM, rtol=0.25)


def test_the_view_gets_a_value_per_vertex_also_where_uv_seams_split_them():
    slab = bench.box_mesh([0, 0, 0], [40 * MM, 40 * MM, 2 * MM], 2 * MM)
    values = np.arange(slab.n_vertices, dtype=float)
    header, arrays = unpack_mesh(pack_mesh(slab, True, scalar=("thickness", values)))
    assert header["scalar"] == "thickness" and header["uv"]
    assert header["vertices"] > slab.n_vertices  # split along the box's seams
    # each sent vertex carries its own source vertex's value
    from scipy.spatial import cKDTree
    _, src = cKDTree(slab.vertices * 1000).query(arrays["positions"].reshape(-1, 3))
    assert np.allclose(arrays["scalar"], values[src])
    header, arrays = unpack_mesh(pack_mesh(slab, False, scalar=("thickness", values)))
    assert np.array_equal(arrays["scalar"], values.astype(np.float32))
    header, arrays = unpack_mesh(pack_mesh(slab, False))
    assert "scalar" not in header and len(arrays["scalar"]) == 0
