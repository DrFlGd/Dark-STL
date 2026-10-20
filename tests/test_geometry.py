import numpy as np
import pytest

from darkstl.formats import flver
from darkstl.geometry.mesh import TriMesh, weld
from darkstl.geometry.model_mesh import MeshOptions, flver_to_trimesh
from darkstl.geometry.proximity import SurfaceLocator, closest_point_on_triangles
from darkstl.geometry.remesh import acvd_available, isotropic_remesh
from darkstl.geometry.subdivide import subdivide_max_edge, subdivide_to_vertex_count

import synth


def cube_trimesh() -> TriMesh:
    pos, nrm, uv, idx = synth.cube_mesh()
    f = flver.read_flver(synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])]))
    return flver_to_trimesh(f)


def signed_volume(mesh: TriMesh) -> float:
    t = mesh.triangles()
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)


def test_cube_welds_and_orients_outward():
    m = cube_trimesh()
    assert m.n_vertices == 8  # 24 game vertices -> 8 corners
    assert m.n_faces == 12
    assert signed_volume(m) == pytest.approx(1.0, rel=1e-5)  # positive: faces point outward
    edges, _ = m.edges_unique()
    assert len(edges) == 18  # closed manifold: E = 3F/2


def test_game_to_export_frame():
    pos, nrm, uv, idx = synth.grid_plane(1)
    f = flver.read_flver(synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])]))
    m = flver_to_trimesh(f)
    # game Y-up plane (y = 0) becomes z = 0 with normals +Z
    assert np.allclose(m.vertices[:, 2], 0)
    assert np.allclose(m.face_normals(), [0, 0, 1])


def test_static_mesh_bone_transform():
    pos, nrm, uv, idx = synth.grid_plane(1)
    bones = [synth.SynthBone("root"), synth.SynthBone("blade", translation=(0, 5, 0), parent=0)]
    mesh = synth.SynthMesh(pos, nrm, uv, idx, bone_indices=np.tile([1, 0, 0, 0], (len(pos), 1)), mesh_bones=[0, 1])
    f = flver.read_flver(synth.build_flver([mesh], [("m", "m.mtd", [])], bones=bones))
    moved = flver_to_trimesh(f)
    still = flver_to_trimesh(f, MeshOptions(apply_bone_transforms=False))
    assert np.allclose(moved.vertices[:, 2], 5)  # game +Y 5 -> export +Z 5
    assert np.allclose(still.vertices[:, 2], 0)


def test_include_meshes_filter():
    pos, nrm, uv, idx = synth.grid_plane(1)
    meshes = [synth.SynthMesh(pos, nrm, uv, idx), synth.SynthMesh(pos + [0, 0, 3], nrm, uv, idx)]
    f = flver.read_flver(synth.build_flver(meshes, [("m", "m.mtd", [])]))
    assert flver_to_trimesh(f).n_faces == 4
    only = flver_to_trimesh(f, MeshOptions(include_meshes={1}))
    assert only.n_faces == 2 and set(only.face_group) == {1}


def test_weld_keeps_corner_uvs():
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0.0]])
    f = np.array([[0, 1, 2], [3, 4, 5]])
    uv = np.array([[[0, 0], [1, 0], [0, 1]], [[0.5, 0.5], [1, 1], [0.2, 0.2]]], np.float32)
    w = weld(TriMesh(v, f, uv))
    assert w.n_vertices == 4
    assert np.array_equal(w.corner_uvs, uv)


def test_subdivide_limits_edges_and_keeps_surface():
    m = cube_trimesh()
    s = subdivide_max_edge(m, 0.1)
    assert s.edge_lengths().max() <= 0.1 + 1e-9
    assert signed_volume(s) == pytest.approx(1.0, rel=1e-6)
    assert np.abs(s.vertices).max() <= 0.5 + 1e-9  # midpoints stay on the cube
    edges, _ = s.edges_unique()
    assert len(edges) * 2 == 3 * s.n_faces  # still watertight (no T-junctions)


def test_subdivide_uvs_are_exact():
    pos, nrm, uv, idx = synth.grid_plane(1, size=2.0)
    f = flver.read_flver(synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])],
                                           uv_float=True))
    m = flver_to_trimesh(f)
    s = subdivide_max_edge(m, 0.05)
    # On this plane u = x / 2 and v = y / 2 (game z -> export y)
    corner_pos = s.vertices[s.faces]
    assert np.allclose(s.corner_uvs[..., 0], corner_pos[..., 0] / 2, atol=1e-6)
    assert np.allclose(s.corner_uvs[..., 1], corner_pos[..., 1] / 2, atol=1e-6)


def tube_trimesh(**kw) -> TriMesh:
    pos, nrm, uv, idx = synth.tube_mesh(**kw)
    f = flver.read_flver(synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])]))
    return flver_to_trimesh(f)


def radial(mesh: TriMesh) -> np.ndarray:
    return np.linalg.norm(mesh.vertices[:, :2], axis=1)  # tube axis is game Y = export Z


def test_smooth_subdivision_rounds_a_low_poly_tube():
    tube = tube_trimesh(radius=0.03, segments=16)
    flat = subdivide_max_edge(tube, 0.002)
    assert radial(flat).min() == pytest.approx(0.03 * np.cos(np.pi / 16), rel=1e-3)  # facets kept
    round_ = subdivide_max_edge(tube, 0.002, smooth=0.5)
    r = radial(round_)
    assert np.abs(r / 0.03 - 1).max() < 0.002  # within 0.2 % of a true circle
    assert round_.corner_normals.shape == (round_.n_faces, 3, 3)
    assert np.allclose(np.linalg.norm(round_.corner_normals, axis=2), 1, atol=1e-5)


def test_smooth_subdivision_keeps_flat_shaded_parts_flat():
    cube = subdivide_max_edge(cube_trimesh(), 0.1, smooth=0.5)
    assert np.allclose(np.abs(cube.vertices).max(axis=1), 0.5, atol=1e-9)  # every vertex still on a face


def test_smooth_subdivision_curves_hard_rims():
    tube = tube_trimesh(radius=0.03, segments=12, caps=True)
    m = subdivide_max_edge(tube, 0.002, smooth=0.5)
    axial = np.abs(m.face_normals()[:, 2]) > 0.99
    cap = np.unique(m.faces[axial])
    side = np.unique(m.faces[~axial])
    rim = np.intersect1d(cap, side)
    assert len(rim) > 50
    assert np.abs(radial(m)[rim] / 0.03 - 1).max() < 0.005  # cap edge follows the round side
    z = m.vertices[cap, 2]
    assert np.all((np.abs(z) < 1e-9) | (np.abs(z - 0.25) < 1e-9))  # caps themselves stay flat
    assert len(m.edges_unique()[0]) * 2 == m.n_faces * 3  # still closed: no cracks along the rim


def sphere_trimesh(n=400, seed=0) -> TriMesh:
    from scipy.spatial import ConvexHull

    pts = np.random.default_rng(seed).normal(size=(n, 3))
    pts /= np.linalg.norm(pts, axis=1, keepdims=True)
    return TriMesh(pts, ConvexHull(pts).simplices)


def test_subdivide_to_vertex_count_hits_target():
    for m in (sphere_trimesh(), cube_trimesh(), tube_trimesh()):
        for target in (5_000, 50_000):
            s = subdivide_to_vertex_count(m, target, smooth=0.5)
            assert s.n_vertices == target
            lengths = s.edge_lengths()
            assert lengths.max() < 2.5 * np.median(lengths)  # evenly spaced, not dense in patches


def test_subdivision_evens_out_density():
    # One huge triangle next to a tiny one: after subdivision both have similar edge lengths.
    v = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [10.1, 0, 0], [10.1, 0.1, 0]], float)
    f = np.array([[0, 1, 2], [1, 3, 4]])
    s = subdivide_max_edge(TriMesh(v, f), 0.2)
    assert s.edge_lengths().max() <= 0.2
    assert s.n_faces > 4000


def test_closest_point_regions():
    a = np.array([[0.0, 0, 0]] * 4)
    b = np.array([[1.0, 0, 0]] * 4)
    c = np.array([[0.0, 1, 0]] * 4)
    p = np.array([[0.2, 0.2, 1.0], [-1, -1, 0], [2, -0.5, 0], [1, 1, 0]])
    bary = closest_point_on_triangles(p, a, b, c)
    pts = bary[:, :1] * a + bary[:, 1:2] * b + bary[:, 2:] * c
    assert np.allclose(pts, [[0.2, 0.2, 0], [0, 0, 0], [1, 0, 0], [0.5, 0.5, 0]])


def test_locator_on_sphere():
    from scipy.spatial import ConvexHull

    rng = np.random.default_rng(0)
    pts = rng.normal(size=(2000, 3))
    pts /= np.linalg.norm(pts, axis=1, keepdims=True)
    hull = ConvexHull(pts)
    mesh = TriMesh(pts, hull.simplices)
    q = rng.normal(size=(500, 3))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    q *= 1.05
    hits = SurfaceLocator(mesh).query(q)
    assert np.all(np.abs(np.linalg.norm(hits.points, axis=1) - 1) < 0.01)
    assert np.allclose(hits.distance, 0.05, atol=0.012)


@pytest.mark.skipif(not acvd_available(), reason="pyacvd not installed")
def test_isotropic_remesh_cube():
    m = cube_trimesh()
    r = isotropic_remesh(m, 3000)
    assert abs(r.n_vertices - 3000) < 300
    assert signed_volume(r) == pytest.approx(1.0, rel=0.03)
    lengths = r.edge_lengths()
    assert lengths.std() / lengths.mean() < 0.35  # evenly spaced
    assert r.corner_uvs is not None and r.corner_uvs.shape == (r.n_faces, 3, 2)
