"""Cleaning a model before Mesh (clean.py): what each operation finds and removes, fixes or moves, on models
built here (a body inside a cuirass, a cape, two-sided cloth, holes, flipped faces), and the list replaying."""

import math

import numpy as np
import pytest

from darkstl import clean as C
from darkstl.geometry.mesh import TriMesh, concatenate


def sphere(centre=(0, 0, 0), r=0.1, nu=32, nv=16, group=0, material=0) -> TriMesh:
    """A closed UV sphere facing outwards, in metres, with shading normals along its radius."""
    c = np.asarray(centre, np.float64)
    verts = [c + (0, 0, r), c + (0, 0, -r)]
    for j in range(1, nv):
        th = math.pi * j / nv
        for i in range(nu):
            ph = 2 * math.pi * i / nu
            verts.append(c + r * np.array([math.sin(th) * math.cos(ph), math.sin(th) * math.sin(ph), math.cos(th)]))
    ring = lambda j, i: 2 + (j - 1) * nu + (i % nu)  # noqa: E731
    faces = []
    for i in range(nu):
        faces.append((0, ring(1, i), ring(1, i + 1)))
        faces.append((1, ring(nv - 1, i + 1), ring(nv - 1, i)))
    for j in range(1, nv - 1):
        for i in range(nu):
            a, b, cc, d = ring(j, i), ring(j, i + 1), ring(j + 1, i + 1), ring(j + 1, i)
            faces += [(a, d, cc), (a, cc, b)]
    return _mesh(np.array(verts), np.array(faces), group, material, outward_from=c)


def box(lo, hi, n=4, group=0, material=0, open_ends=False) -> TriMesh:
    """A closed box facing outwards, each side a grid of n x n squares; ``open_ends``: no top or bottom (a tube)."""
    lo, hi = np.asarray(lo, np.float64), np.asarray(hi, np.float64)
    meshes = []
    for axis in range(3):
        if open_ends and axis == 2:
            continue
        for side in (0, 1):
            u, w = [a for a in range(3) if a != axis]
            g = np.linspace(0, 1, n + 1)
            uu, ww = np.meshgrid(g, g, indexing="ij")
            p = np.zeros((n + 1, n + 1, 3))
            p[..., axis] = hi[axis] if side else lo[axis]
            p[..., u] = lo[u] + uu * (hi[u] - lo[u])
            p[..., w] = lo[w] + ww * (hi[w] - lo[w])
            idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
            a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
            f = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3), np.stack([a, c, d], -1).reshape(-1, 3)])
            m = TriMesh(p.reshape(-1, 3), f)
            out = np.zeros(3)
            out[axis] = 1 if side else -1
            if (m.face_normals() @ out).mean() < 0:
                m.faces = m.faces[:, ::-1]
            meshes.append(m)
    from darkstl.geometry.mesh import weld

    m = weld(concatenate(meshes), 1e-9)
    return _mesh(m.vertices, m.faces, group, material, outward_from=(lo + hi) / 2)


def sheet(origin, du, dv, n=8, group=0, material=0) -> TriMesh:
    """An open square of cloth, n x n squares, facing du x dv."""
    o, du, dv = (np.asarray(x, np.float64) for x in (origin, du, dv))
    g = np.linspace(0, 1, n + 1)
    uu, vv = np.meshgrid(g, g, indexing="ij")
    p = o + uu[..., None] * du + vv[..., None] * dv
    idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    f = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3), np.stack([a, c, d], -1).reshape(-1, 3)])
    m = TriMesh(p.reshape(-1, 3), f)
    return _mesh(m.vertices, m.faces, group, material)


def _mesh(v, f, group, material, outward_from=None) -> TriMesh:
    m = TriMesh(v, f)
    m.face_group[:] = group
    m.face_material[:] = material
    nrm = m.face_normals()
    if outward_from is not None:
        vn = v - np.asarray(outward_from)
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
        m.corner_normals = vn[f].astype(np.float32)
    else:
        m.corner_normals = np.repeat(nrm[:, None, :], 3, axis=1).astype(np.float32)
    m.corner_uvs = (v[f][..., :2] * 3).astype(np.float32)
    return m


def run(mesh: TriMesh, *ops) -> C.Cleaned:
    return C.apply(mesh, C.CleanList.of(list(ops)))


def watertight(mesh: TriMesh) -> bool:
    topo = C.Topology.of(mesh)
    he, same, reverse = C.edge_counts(topo.faces)
    return bool(((same == 1) & (reverse == 1)).all())


# ------------------------------------------------------------------ the shared helpers and ids


def test_the_inside_test_winds_once_inside_a_closed_surface_either_way_round():
    s = sphere(r=0.1)
    solid = C.Solid(s.vertices, s.faces)
    pts = np.array([[0, 0, 0], [0.05, 0.02, -0.03], [0.2, 0, 0], [0, 0, 0.11], [0.099, 0, 0]])
    assert solid.contains(pts).tolist() == [True, True, False, False, True]
    inside_out = C.Solid(s.vertices, s.faces[:, ::-1])
    assert inside_out.contains(pts).tolist() == [True, True, False, False, True]
    assert solid.distance(np.array([[0, 0, 0]]))[0] == pytest.approx(0.1, rel=0.03)


def test_face_ids_name_each_face_by_its_part_and_index_and_replay_with_parts_left_out():
    m = concatenate([sphere(group=0), sphere((0.3, 0, 0), group=1), sphere((0.6, 0, 0), group=2)])
    ids = C.face_ids(m)
    n = sphere().n_faces
    assert (ids[:n] >> C.PART_SHIFT == 0).all() and (ids[n:2 * n] >> C.PART_SHIFT == 1).all()
    assert (ids[n:2 * n] & 0xFFFFFFFF).tolist() == list(range(n))
    pick = ids[[3, 4, 5, 9, n + 7, 2 * n + 1]]
    enc = C.encode_ids(pick)
    assert enc == {"0": [3, 3, 9, 1], "1": [7, 1], "2": [1, 1]}
    assert C.decode_ids(enc).tolist() == sorted(pick.tolist())
    # the same faces are removed with part 1 switched off: the other parts' faces keep their ids
    op = {"op": "remove", "kind": "selection", "faces": enc}
    without_1 = concatenate([sphere(group=0), sphere((0.6, 0, 0), group=2)])
    got = run(without_1, op)
    assert got.mesh.n_faces == 2 * n - 5
    assert got.rows[0]["faces_removed"] == 5 and got.rows[0]["wanted"] == 6


def test_a_clean_list_is_checked_and_keyed_by_what_it_holds():
    a = C.CleanList.of([{"op": "weld", "distance_mm": 0.02}, {"op": "fix_normals", "extra": 1}])
    b = C.CleanList.of([{"op": "weld", "distance_mm": "0.02"}, {"op": "fix_normals"}])
    assert a.key == b.key and a.ops[1] == {"op": "fix_normals"}
    assert C.CleanList.of([]).key != a.key and not C.CleanList.of([])
    with pytest.raises(ValueError):
        C.CleanList.of([{"op": "explode"}])
    with pytest.raises(ValueError):
        C.CleanList.of([{"op": "remove", "kind": "selection"}])  # a selection names its faces


# ------------------------------------------------------------------ 1. parts hidden inside others


def test_a_part_wholly_inside_another_is_found_and_one_half_outside_isnt():
    outer = sphere(r=0.2, group=0)
    lining = sphere(r=0.15, group=1)  # a lining inside it
    bead = sphere((0.19, 0, 0), r=0.03, group=2)  # half out
    far = sphere((1, 0, 0), r=0.05, group=3)
    m = concatenate([outer, lining, bead, far])
    found = C.find_hidden_parts(m)
    assert len(found) == 1
    assert set(m.face_group[found[0]].tolist()) == {1}
    got = run(m, {"op": "remove", "kind": "hidden_parts"})
    assert got.mesh.n_faces == m.n_faces - lining.n_faces
    assert got.rows[0]["text"] == f"removed 1 part, {lining.n_faces:,} faces"


# ------------------------------------------------------------------ 2. faces hidden inside other parts


def body_and_cuirass():
    """A body (a tall closed box, 1.8 m) and a cuirass round its chest (a tube open top and bottom, thick)."""
    body = box((-0.15, -0.1, 0), (0.15, 0.1, 1.8), n=36, group=0, material=0)
    outer = box((-0.2, -0.15, 0.9), (0.2, 0.15, 1.4), n=6, group=1, material=1, open_ends=True)
    return body, outer, concatenate([body, outer])


def test_the_body_under_a_cuirass_is_removed_deeper_than_the_depth_and_kept_near_its_openings():
    body, cuirass, m = body_and_cuirass()
    hidden = C.find_hidden_faces(m, 0.0)
    assert len(hidden) and (m.face_group[hidden] == 0).all()  # only the body's faces
    c = m.vertices[m.faces[hidden]].mean(1)
    assert c[:, 2].min() > 0.9 - 1e-6 and c[:, 2].max() < 1.4 + 1e-6  # under the cuirass, not the legs or head
    deep = C.find_hidden_faces(m, 0.03)
    cd = m.vertices[m.faces[deep]].mean(1)
    assert 0 < len(deep) < len(hidden)
    assert cd[:, 2].min() > 0.93 and cd[:, 2].max() < 1.37  # 30 mm in from the openings, top and bottom
    got = run(m, {"op": "remove", "kind": "hidden_faces", "depth_mm": 30})
    assert got.mesh.n_faces == m.n_faces - len(deep)
    assert got.rows[0]["label"] == "Faces hidden inside other parts, deeper than 30 mm"


# ------------------------------------------------------------------ 3. thin sheets


def test_a_cape_two_sided_cloth_and_a_thin_flap_are_found_as_sheets_and_the_body_isnt():
    body = sphere(r=0.15, group=0)
    cape = sheet((-0.2, 0.2, -0.3), (0.4, 0, 0), (0, 0, 0.5), n=10, group=0)  # an open sheet in the body's part
    cloth = sheet((-0.2, -0.6, -0.1), (0.3, 0, 0), (0, 0, 0.3), n=6, group=1)
    mirrored = TriMesh(cloth.vertices, cloth.faces[:, ::-1], cloth.corner_uvs, cloth.face_material, cloth.face_group,
                       corner_normals=-cloth.corner_normals)
    flap = box((0.5, -0.1, 0), (0.7, -0.0997, 0.2), n=4, group=2)  # 0.3 mm thick
    m = concatenate([body, cape, cloth, mirrored, flap])
    regions, more = C.find_sheets(m, 0.0005)
    kinds = sorted((r.kind, len(r.faces)) for r in regions)
    assert ("open", cape.n_faces) in kinds
    assert ("two-sided", 2 * cloth.n_faces) in kinds  # both sides, as one region
    thin = [r for r in regions if r.kind == "thin"]
    assert len(thin) == 1 and set(m.face_group[thin[0].faces].tolist()) == {2}
    assert thin[0].thickness == pytest.approx(0.0003, rel=0.05)
    body_faces = np.arange(body.n_faces)
    assert not any(np.isin(r.faces, body_faces).any() for r in regions)
    assert regions[0].area >= regions[-1].area  # biggest first
    # thinner than the flap: the flap isn't one
    regions, _ = C.find_sheets(m, 0.0002)
    assert not [r for r in regions if r.kind == "thin"]
    # removed by the threshold, as the command line does it
    got = run(m, {"op": "remove", "kind": "sheets", "thickness_mm": 0.5})
    assert got.mesh.n_faces == body.n_faces
    assert got.rows[0]["text"].startswith("removed 3 sheets")


def test_the_thickness_behind_a_face_is_the_distance_to_the_other_side():
    slab = box((0, 0, 0), (0.2, 0.2, 0.004), n=8)
    t = C.face_thickness(slab, 0.01)
    top = slab.face_normals()[:, 2] > 0.9
    assert np.allclose(t[top], 0.004, rtol=0.02)
    assert np.isinf(C.face_thickness(slab, 0.002)[top]).all()  # thicker than the limit


# ------------------------------------------------------------------ 4. select and remove


def test_a_click_floods_to_the_creases():
    b = box((0, 0, 0), (1, 1, 1), n=3)
    g = C.FaceGraph(b)
    side = g.flood(0, 30)
    assert len(side) == 18  # one side of the box: 3 x 3 squares, two triangles each
    assert np.allclose(b.face_normals()[side], b.face_normals()[0])
    assert len(g.flood(0, 91)) == b.n_faces  # the box's edges are 90 degrees
    pick = C.encode_ids(C.face_ids(b)[side])
    got = run(b, {"op": "remove", "kind": "selection", "faces": pick})
    assert got.mesh.n_faces == b.n_faces - 18 and got.rows[0]["text"] == "removed 18 faces"


# ------------------------------------------------------------------ 5. repairs


def test_small_holes_are_filled_and_big_openings_left():
    s = sphere(r=0.1)
    gone = np.zeros(s.n_faces, bool)
    gone[[200, 201]] = True  # a small hole (two triangles' worth)
    holed = s.subset(~gone)
    holed = concatenate([holed, sheet((0.3, 0, 0), (0.2, 0, 0), (0, 0.2, 0), n=4)])  # a sheet's edge is no hole
    assert not watertight(holed.subset(np.arange(holed.n_faces) < s.n_faces - 2))
    got = run(holed, {"op": "fill_holes", "size_mm": 60})
    assert got.rows[0]["holes"] == 1 and got.rows[0]["faces_added"] == 4
    fixed = got.mesh.subset(np.isin(np.arange(got.mesh.n_faces), np.r_[0:s.n_faces - 2, holed.n_faces:got.mesh.n_faces]))
    assert watertight(fixed)
    assert C.signed_volume(fixed.vertices, fixed.faces) > 0  # facing out, as the faces round it do
    assert got.mesh.corner_uvs.shape == (got.mesh.n_faces, 3, 2)
    assert (got.ids[-4:] >> C.PART_SHIFT >= C.ADDED_PART).all()


def test_flipped_faces_are_turned_back_and_their_shading_normals_too():
    s = sphere(r=0.1)
    bad = s.copy()
    turn = np.arange(10, 40)
    bad.faces[turn] = bad.faces[turn][:, ::-1]
    bad.corner_normals[turn] = -bad.corner_normals[turn][:, ::-1]
    got = run(bad, {"op": "fix_normals"})
    assert got.rows[0]["flipped"] == len(turn)
    assert C.signed_volume(got.mesh.vertices, got.mesh.faces) == pytest.approx(C.signed_volume(s.vertices, s.faces))
    fn = got.mesh.face_normals()
    assert (np.einsum("ij,ij->i", fn, got.mesh.corner_normals.sum(1)) > 0).all()
    # a whole surface inside out is turned out
    inside_out = TriMesh(s.vertices, s.faces[:, ::-1], corner_normals=s.corner_normals)
    assert run(inside_out, {"op": "fix_normals"}).rows[0]["flipped"] == s.n_faces


def test_duplicates_are_dropped_and_close_vertices_welded():
    cloth = sheet((0, 0, 0), (0.2, 0, 0), (0, 0, 0.2), n=4)
    two_sided = concatenate([cloth, TriMesh(cloth.vertices, cloth.faces[:, ::-1], cloth.corner_uvs, corner_normals=cloth.corner_normals)])
    got = run(two_sided, {"op": "dedupe"})
    assert got.mesh.n_faces == cloth.n_faces and got.rows[0]["faces_removed"] == cloth.n_faces
    split = concatenate([sheet((0, 0, 0), (0.1, 0, 0), (0, 0, 0.1), n=2),
                         sheet((0.1 + 2e-6, 0, 0), (0.1, 0, 0), (0, 0, 0.1), n=2)])
    got = run(split, {"op": "weld", "distance_mm": 0.01})
    assert got.rows[0]["merged"] == 3 and got.mesh.n_vertices == split.n_vertices - 3
    assert run(split, {"op": "weld", "distance_mm": 0.001}).rows[0]["merged"] == 0


# ------------------------------------------------------------------ 6. transform


def test_a_print_height_scales_the_model_and_the_finders_work_in_print_millimetres():
    body, cuirass, m = body_and_cuirass()
    got = run(m, {"op": "transform", "height_mm": 150, "floor": True})
    lo, hi = got.mesh.bounds()
    assert (hi - lo)[2] == pytest.approx(0.150) and lo[2] == pytest.approx(0)
    assert got.scale == pytest.approx(150 / 1800)
    assert got.game_size_mm[2] == pytest.approx(1800)
    assert got.rows[0]["text"].startswith("150 mm tall (×0.08333)")
    # 2.5 mm on the print is 30 mm in the game: the same faces as 30 mm at game size
    deep = run(m, {"op": "remove", "kind": "hidden_faces", "depth_mm": 2.5}, {"op": "transform", "height_mm": 150})
    assert deep.mesh.n_faces == m.n_faces - len(C.find_hidden_faces(m, 0.03))


def test_turning_mirroring_and_moving_keep_the_model_facing_out():
    s = sphere((0.3, 0, 0.5), r=0.1)
    got = run(s, {"op": "transform", "rotate": [90, 0, 0], "mirror": [True, False, False], "move": [10, 0, 0], "floor": True})
    assert C.signed_volume(got.mesh.vertices, got.mesh.faces) > 0
    fn = got.mesh.face_normals()
    assert (np.einsum("ij,ij->i", fn, got.mesh.corner_normals.astype(np.float64).sum(1)) > 0).all()
    lo, hi = got.mesh.bounds()
    assert lo[2] == pytest.approx(0) and (lo[0] + hi[0]) / 2 == pytest.approx(0.31)
    # the transform is applied last, wherever it is in the list, and only the last of them counts
    a = run(s, {"op": "transform", "scale": 2}, {"op": "fix_normals"})
    b = run(s, {"op": "fix_normals"}, {"op": "transform", "scale": 3}, {"op": "transform", "scale": 2})
    assert np.allclose(a.mesh.vertices, b.mesh.vertices)
    assert b.rows[1]["text"] == "replaced by the transform after it"


def test_the_command_line_steps():
    cl = C.parse_steps(["hidden-parts", "hidden-faces:2", "sheets:0.4", "holes", "duplicates", "weld:0.05", "normals",
                        "height:150", "rotate:90,0,0", "floor"])
    assert [op["op"] for op in cl.ops] == ["remove", "remove", "remove", "fill_holes", "dedupe", "weld", "fix_normals", "transform"]
    assert cl.ops[1]["depth_mm"] == 2 and cl.ops[2]["thickness_mm"] == 0.4 and cl.ops[5]["distance_mm"] == 0.05
    assert cl.transform()["height_mm"] == 150 and cl.transform()["rotate"] == [90, 0, 0] and cl.transform()["floor"]
    with pytest.raises(ValueError):
        C.parse_steps(["sparkle"])


# ------------------------------------------------------------------ the app's commands


def write_obj(path, parts) -> "Path":
    """``parts`` [(name, TriMesh)] as an OBJ in metres, a group each (the app's parts)."""
    lines, base = [], 1
    for name, m in parts:
        lines.append(f"g {name}")
        lines += [f"v {x:.6f} {z:.6f} {-y:.6f}" for x, y, z in m.vertices]  # OBJ is Y up
        lines += [f"f {a + base} {b + base} {c + base}" for a, b, c in m.faces]
        base += m.n_vertices
    path.write_text("\n".join(lines) + "\n")
    return path


def figure(folder):
    """A figure as game models are: a body (1.8 m), a cuirass round its chest, a lining inside the cuirass, and a
    cape behind it, each its own part."""
    body, cuirass, _ = body_and_cuirass()
    lining = box((-0.12, -0.08, 1.0), (0.12, 0.08, 1.3), n=3)  # inside the body, wholly
    cape = sheet((-0.2, 0.3, 0.4), (0.4, 0, 0), (0, 0, 1.0), n=8)
    return write_obj(folder / "figure.obj", [("body", body), ("cuirass", cuirass), ("lining", lining), ("cape", cape)]), \
        {"body": body.n_faces, "cuirass": cuirass.n_faces, "lining": lining.n_faces, "cape": cape.n_faces}


def wait(service, job_id, timeout=120):
    import time

    end = time.time() + timeout
    while time.time() < end:
        job = service.call("job", {"id": job_id})
        if job["state"] != "running":
            assert job["state"] == "done", job["error"]
            return job["result"]
        time.sleep(0.02)
    raise AssertionError("job didn't finish")


@pytest.fixture
def service(tmp_path, monkeypatch):
    from darkstl.app.config import Config
    from darkstl.app.service import Service

    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    return Service(Config.load(), tmp_path / "home" / "cache")


def test_the_app_finds_records_and_replays_a_clean_up(service, tmp_path):
    from darkstl.app.meshpack import unpack_mesh
    from darkstl.app.service import AppError

    path, counts = figure(tmp_path)
    m = service.call("model_open", {"path": str(path)})
    sid = m["sid"]
    total = m["stats"]["triangles"]
    assert [p["name"] for p in m["parts"]] == ["body", "cuirass", "lining", "cape"]
    # the lining is a part hidden inside the body
    found = wait(service, service.call("clean_find", {"sid": sid, "find": "hidden_parts"})["job"])
    assert found["parts"] == 1 and found["count"] == counts["lining"] and len(found["shown"]) == counts["lining"]
    ops = [{"op": "remove", "kind": "hidden_parts", "faces": found["faces"], "parts": 1}]
    st = service.call("model_stats", {"sid": sid, "clean": ops, "clean_id": "page-1"})
    assert st["triangles"] == total - counts["lining"]
    assert st["clean"]["rows"][0]["text"] == f"removed 1 part, {counts['lining']:,} faces"
    # the body under the cuirass, found on the model as cleaned so far
    found = wait(service, service.call("clean_find", {"sid": sid, "find": "hidden_faces", "depth_mm": 30, "clean": ops})["job"])
    assert 0 < found["count"] < counts["body"]
    ops.append({"op": "remove", "kind": "hidden_faces", "depth_mm": 30, "faces": found["faces"]})
    # the cape is a thin sheet
    sheets = wait(service, service.call("clean_find", {"sid": sid, "find": "sheets", "thickness_mm": 0.5, "clean": ops})["job"])
    # (the cuirass is one layer with no thickness too, as game armour often is: listed, for the user to choose)
    assert sorted(r["faces"] for r in sheets["regions"] if r["kind"] == "open") == [counts["cape"], counts["cuirass"]]
    cape = [r for r in sheets["regions"] if r["faces"] == counts["cape"]]
    assert len(cape[0]["shown"]) == counts["cape"]
    ops.append({"op": "remove", "kind": "sheets", "thickness_mm": 0.5, "regions": 1, "faces": cape[0]["faces_ids"]})
    st = service.call("clean_check", {"sid": sid, "clean": ops, "clean_id": "page-2"})
    left = total - counts["lining"] - found["count"] - counts["cape"]
    assert st["triangles"] == left and len(st["clean"]["rows"]) == 3
    # the mesh shown, by the page's name for the list in its address
    header, _ = unpack_mesh(service.call("model_mesh", {"sid": sid, "which": "original", "parts": "all", "flags": "",
                                                         "pose": "", "clean": "page-2"}))
    assert header["triangles"] == left
    with pytest.raises(AppError, match="isn't known here yet"):
        service.call("model_mesh", {"sid": sid, "which": "original", "clean": "never-sent"})
    # a click on the cuirass selects the side of it clicked (up to its corners)
    cuirass_shown = service.call("clean_select", {"sid": sid, "clean": ops, "face": 0, "angle": 30})
    assert 0 < cuirass_shown["count"] < left
    ids = service.call("clean_ids", {"sid": sid, "clean": ops, "shown": cuirass_shown["shown"]})
    assert ids["count"] == cuirass_shown["count"]
    ops.append({"op": "remove", "kind": "selection", "faces": ids["faces"]})
    st = service.call("model_stats", {"sid": sid, "clean": ops})
    assert st["triangles"] == left - cuirass_shown["count"]
    # the list replays the same after the file is opened again (a rescan), and with the cape's part left out
    service.sessions.clear()
    again = service.call("model_open", {"path": str(path)})
    st = service.call("model_stats", {"sid": again["sid"], "clean": ops, "parts": [0, 1, 2]})
    assert st["triangles"] == left - cuirass_shown["count"]
    assert st["clean"]["rows"][2]["text"] == "nothing to remove now (its parts may be switched off)"


def test_generated_models_and_exports_follow_the_clean_up(service, tmp_path):
    path, counts = figure(tmp_path)
    m = service.call("model_open", {"path": str(path)})
    sid = m["sid"]
    clean = [{"op": "remove", "kind": "sheets", "thickness_mm": 0.5}, {"op": "transform", "height_mm": 150, "floor": True}]
    settings = {"target_vertices": 1000, "method": "subdivide", "source": "none"}
    plain = wait(service, service.call("enhance", {"sid": sid, "settings": settings, "step": "mesh"})["job"])
    cleaned = wait(service, service.call("enhance", {"sid": sid, "settings": settings, "step": "mesh", "clean": clean})["job"])
    assert not cleaned["kept"]  # another clean-up is another model
    assert plain["size_mm"][2] == pytest.approx(1800, rel=1e-3) and cleaned["size_mm"][2] == pytest.approx(150, rel=1e-3)
    assert service.sessions[sid].result.made_with["clean"][1]["height_mm"] == 150
    out = tmp_path / "out"
    done = wait(service, service.call("export", {"sid": sid, "which": "original", "clean": clean, "format": ".stl",
                                                 "folder": str(out), "units": "mm"})["job"])
    data = next(out.glob("*.stl")).read_bytes()  # binary STL: 80 bytes, a count, 50 bytes a triangle (Z up, mm)
    count = int(np.frombuffer(data, "<u4", 1, 80)[0])
    tri = np.frombuffer(data, np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count, 84)["v"]
    assert done and count == m["stats"]["triangles"] - counts["cape"] - counts["cuirass"]  # every sheet thinner than 0.5 mm
    assert tri[..., 2].max() - tri[..., 2].min() == pytest.approx(150, rel=1e-3) and tri[..., 2].min() == pytest.approx(0, abs=1e-3)
