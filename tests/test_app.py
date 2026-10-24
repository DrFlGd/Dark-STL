"""The app's engine side without a browser: its commands (service.py), the packed meshes the
3D view reads, the preview pictures, and the local server's checks."""

import http.client
import json
import time
from pathlib import Path

import numpy as np
import pytest

from darkstl.app.config import Config
from darkstl.app.meshpack import pack_mesh, unpack_mesh
from darkstl.app.server import AppServer
from darkstl.app.service import AppError, Service
from darkstl.geometry.mesh import TriMesh
from darkstl import thumbnails

import synth
from test_pipeline import weapon_dir  # noqa: F401 - fixture


def wait(service, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        job = service.call("job", {"id": job_id})
        if job["state"] != "running":
            assert job["state"] == "done", job["error"]
            return job["result"]
        time.sleep(0.02)
    raise AssertionError("job didn't finish")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def make_service(home):
    return Service(Config.load(), home / "cache")


@pytest.fixture
def service(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    result = wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    assert result["game"] == "Dark Souls III"
    return s


def test_add_folder_lists_the_game(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    check = s.call("folder_check", {"path": str(weapon_dir)})
    assert check["exists"] and check["game"] == "Dark Souls III" and not check["added"]
    assert s.call("app_info", {})["first_run"]
    r = wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    assert r["count"] == 4 and r["notes"] == []
    ov = s.call("overview", {})
    assert ov["total"] == 4 and not ov["scanning"]
    (game,) = ov["games"]
    assert game["name"] == "Dark Souls III"
    assert [c["name"] for c in game["categories"]] == ["Weapons", "Armor", "Characters"]
    assert game["folders"] == [{"path": str(weapon_dir), "count": 4, "notes": [], "missing": False, "collected": False}]
    # the low-detail copy is left out unless asked for
    assert s.call("overview", {"lod": True})["total"] == 5
    weapons = s.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})
    assert weapons["total"] == 2 and {m["stem"].lower() for m in weapons["items"]} == {"wp_a_0300", "wp_a_0400"}
    assert s.call("models_query", {"game": "Dark Souls III", "q": "c1100"})["total"] == 1
    # a folder inside it can't be added again; the folder itself can (a rescan)
    inside = s.call("folder_check", {"path": str(weapon_dir / "parts")})
    assert inside["inside"] == str(weapon_dir) and inside["inside_game"] == "Dark Souls III"
    with pytest.raises(AppError, match="already in the library"):
        s.call("folder_add", {"path": str(weapon_dir / "parts")})
    assert s.call("folder_check", {"path": str(weapon_dir)})["added"]


def test_folders_are_kept_between_runs(service, home, weapon_dir):  # noqa: F811
    saved = json.loads((home / "settings.json").read_text())
    assert saved["game_folders"] == [{"path": str(weapon_dir), "game": "Dark Souls III"}]
    again = make_service(home)
    assert not again.call("app_info", {})["first_run"]
    wait(again, again.load_folders().id)
    assert again.call("overview", {})["total"] == 4

    again.call("folder_set_game", {"path": str(weapon_dir), "game": "My DS3"})
    assert [g["name"] for g in again.call("overview", {})["games"]] == ["My DS3"]
    again.call("folder_remove", {"path": str(weapon_dir)})
    assert again.call("overview", {})["games"] == []
    assert json.loads((home / "settings.json").read_text())["game_folders"] == []


def test_a_parent_folder_takes_in_the_folders_inside_it(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir / "parts"), "game": "Dark Souls III"})["job"])
    assert s.call("folder_check", {"path": str(weapon_dir)})["contains"] == [str(weapon_dir / "parts")]
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    (game,) = s.call("overview", {})["games"]
    assert [f["path"] for f in game["folders"]] == [str(weapon_dir)] and game["count"] == 4


def test_a_folder_with_nothing_says_so(home, tmp_path):
    s = make_service(home)
    empty = tmp_path / "empty"
    empty.mkdir()
    r = wait(s, s.call("folder_add", {"path": str(empty), "game": "Other models"})["job"])
    assert r["count"] == 0 and "No models were found" in r["notes"][0]


def test_open_a_model_and_read_its_mesh_and_textures(service):
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})
    assert m["entry"]["id"] == item["id"] and m["parts"] and m["textures"]
    assert m["materials"][0]["albedo"] and m["materials"][0]["normal"]
    header, arrays = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "uv": "1"}))
    assert header["triangles"] == m["stats"]["triangles"] and header["uv"]
    assert arrays["index"].max() < header["vertices"]
    # size in mm: the test weapon is a 1 m square
    assert np.allclose(np.array(header["max"]) - np.array(header["min"]), [1000, 1000, 0], atol=1)
    # no parts switched on: an empty mesh
    header, _ = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "parts": ""}))
    assert header["triangles"] == 0
    png = service.call("texture", {"sid": m["sid"], "key": m["materials"][0]["albedo"], "max": "64"})
    assert png[:4] == b"\x89PNG"
    assert "128" in service.call("texture_info", {"sid": m["sid"], "key": m["materials"][0]["albedo"]})["info"]
    thumb = service.call("thumb", {"id": item["id"]})
    assert thumb[:4] == b"\x89PNG" and service.call("thumb", {"id": item["id"]}) == thumb
    assert service.call("thumbs_clear", {})["removed"] >= 1


def test_enhance_then_export(service, tmp_path):
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})
    with pytest.raises(AppError, match="Generate the enhanced model first"):
        service.call("export", {"sid": m["sid"], "which": "enhanced"})
    r = wait(service, service.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 4000}})["job"])
    assert 3000 < r["stats"]["vertices"] < 6000 and r["fields"] and r["fields"][0]["convention"]
    header, _ = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "which": "enhanced"}))
    assert header["triangles"] == r["stats"]["faces"] and header["which"] == "enhanced"
    # opened again, the page gets the enhanced result back
    assert service.call("model_open", {"id": item["id"]})["enhanced"]["stats"] == r["stats"]
    out = tmp_path / "exports"
    stl = wait(service, service.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".stl", "folder": str(out)})["job"])
    assert Path(stl["path"]).name == "WP_A_0300.stl" and stl["bytes"] == 84 + 50 * r["stats"]["faces"]
    again = wait(service, service.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".stl", "folder": str(out)})["job"])
    assert Path(again["path"]).name == "WP_A_0300 (2).stl"
    glb = wait(service, service.call("export", {"sid": m["sid"], "which": "original", "format": ".glb", "folder": str(out),
                                                "name": "plate"})["job"])
    assert Path(glb["path"]).name == "plate.glb" and Path(glb["path"]).read_bytes()[:4] == b"glTF"


def test_generated_models_are_kept(service, monkeypatch):
    from darkstl import pipeline

    runs = []
    enhance = pipeline.enhance
    monkeypatch.setattr(pipeline, "enhance", lambda *a, **k: runs.append(1) or enhance(*a, **k))
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})

    def generate(**settings):
        return wait(service, service.call("enhance", {"sid": m["sid"], "settings": settings})["job"])

    a = generate(target_vertices=4000)
    b = generate(target_vertices=5000)
    assert len(runs) == 2 and not a["kept"] and a["id"] != b["id"]
    # the same settings again: the kept model, at once
    again = generate(target_vertices=4000)
    assert len(runs) == 2 and again["kept"] and again["id"] == a["id"] and again["stats"] == a["stats"]
    header, _ = unpack_mesh(service.call("model_mesh", {"sid": m["sid"], "which": "enhanced"}))
    assert header["triangles"] == a["stats"]["faces"]
    # any setting the page sends counts, even one this version doesn't know
    assert generate(target_vertices=4000, later_setting=1)["id"] != a["id"] and len(runs) == 3
    # a model opened again (the engine had let it go) shows the last one generated
    service.sessions.clear()
    assert service.call("model_open", {"id": item["id"]})["enhanced"]["id"] not in (a["id"], b["id"])
    # three are kept: the oldest (5000) was let go
    generate(target_vertices=6000)
    assert generate(target_vertices=5000)["kept"] is False and len(runs) == 5
    # other texture folders: other textures, so nothing kept is used
    service.call("settings_set", {"texture_dirs": []})
    assert not service.generated


def test_displacement_settings_reuse_the_evened_out_mesh(service, monkeypatch):
    from darkstl import pipeline

    made = []
    resample = pipeline.resample
    monkeypatch.setattr(pipeline, "resample", lambda *a, **k: made.append(1) or resample(*a, **k))
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})

    def generate(**settings):
        return wait(service, service.call("enhance", {"sid": m["sid"], "settings": settings})["job"])

    def kept(**settings):
        return service.call("enhance_mesh", {"sid": m["sid"], "settings": settings})

    assert kept(target_vertices=4000) == {"kept": False, "vertices": 0, "triangles": 0}
    a = generate(target_vertices=4000, strength=1.0)
    assert len(made) == 1 and not a["mesh_reused"]
    assert kept(target_vertices=4000, strength=3.0)["vertices"] == a["stats"]["vertices"]
    # step 2 only: the same mesh, displaced again
    b = generate(target_vertices=4000, strength=3.0)
    assert len(made) == 1 and b["mesh_reused"] and b["id"] != a["id"]
    assert b["stats"]["vertices"] == a["stats"]["vertices"] and b["stats"]["faces"] == a["stats"]["faces"]
    assert b["stats"]["max_displacement_mm"] > a["stats"]["max_displacement_mm"]
    flat = generate(target_vertices=4000, source="none")
    assert len(made) == 1 and flat["mesh_reused"] and flat["stats"]["max_displacement_mm"] == 0
    # a step-1 setting: made again
    assert not kept(target_vertices=4000, smooth=False)["kept"]
    assert not generate(target_vertices=4000, smooth=False)["mesh_reused"] and len(made) == 2
    # two are kept: the oldest goes
    generate(target_vertices=5000)
    assert len(made) == 3 and not kept(target_vertices=4000)["kept"]
    # other texture folders let go of the meshes too (the model is read again)
    service.call("settings_set", {"texture_dirs": []})
    assert not service.meshes


def test_a_model_file_can_be_opened_on_its_own(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    path = weapon_dir / "chr" / "c1100.chrbnd.dcx"
    m = s.call("model_open", {"path": str(path)})
    assert m["entry"] is None and m["textures"]  # its textures come from the .texbnd beside it
    assert s.call("model_entry", {"id": m["id"]})["file"] == str(path)
    assert s.call("thumb", {"id": m["id"]})[:4] == b"\x89PNG"
    with pytest.raises(AppError, match="no file"):
        s.call("model_open", {"path": str(weapon_dir / "nothing.flver")})


def test_settings_round_trip(home, tmp_path):
    s = make_service(home)
    with pytest.raises(AppError):
        s.call("settings_set", {"oodle": str(tmp_path / "missing.dll")})
    r = s.call("settings_set", {"texture_dirs": [str(tmp_path)], "export_dir": str(tmp_path / "out")})
    assert r["texture_dirs"] == [str(tmp_path)] and r["export_dir"] == str(tmp_path / "out")
    s.call("prefs_set", {"prefs": {"ds-ui": {"theme": "night"}}})
    assert make_service(home).call("prefs_get", {}) == {"ds-ui": {"theme": "night"}}
    with pytest.raises(AppError, match="Unknown command"):
        s.call("nothing_like_this", {})


def test_packed_mesh_splits_uv_seams_and_groups_materials():
    pos, nrm, uv, idx = synth.grid_plane(2, size=1.0)
    faces = idx.reshape(-1, 3)
    corner = uv[faces]
    f, c = np.argwhere(faces == 4)[0]
    corner[f, c] = [0.9, 0.9]  # the middle vertex has another UV in one triangle: it's sent twice
    mesh = TriMesh(pos, faces, corner_uvs=corner, face_material=np.array([1, 0] * (len(faces) // 2)))
    header, a = unpack_mesh(pack_mesh(mesh))
    assert header["vertices"] == len(pos) + 1 and header["triangles"] == len(faces)
    assert [g["material"] for g in header["groups"]] == [0, 1]
    assert sum(g["count"] for g in header["groups"]) == 3 * len(faces)
    assert np.allclose(a["positions"].reshape(-1, 3).max(0), [1000, 0, 1000])  # millimetres
    header, a = unpack_mesh(pack_mesh(mesh, with_uv=False))
    assert header["vertices"] == len(pos) and len(a["uvs"]) == 0


def test_thumbnail_draws_the_model_and_lays_long_ones_diagonally():
    pos, nrm, uv, idx = synth.tube_mesh(radius=0.01, length=0.5, caps=True)
    img = thumbnails.render(TriMesh(pos, idx), size=64)
    assert img.shape == (64, 64, 4)
    drawn = np.argwhere(img[..., 3] > 0)
    assert len(drawn) > 50
    span = drawn.max(0) - drawn.min(0)
    assert span[0] > 30 and span[1] > 30  # corner to corner, not a thin line


# ----------------------------------------------------------------- the server


def request(server, method, path, token=None, host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    headers = {"Host": host or f"127.0.0.1:{server.server_address[1]}"}
    if token:
        headers["X-DarkSTL-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def test_server_answers_only_its_own_page(home):
    server = AppServer(make_service(home), 0).start()
    try:
        status, page = request(server, "GET", "/")
        assert status == 200 and f'content="{server.token}"'.encode() in page
        assert request(server, "GET", "/ui/shell.js")[0] == 200
        assert request(server, "GET", "/../app/server.py")[0] == 404
        assert request(server, "GET", "/%2e%2e/app/server.py")[0] == 404
        # another site's page can't reach it: wrong Host (DNS rebinding) or no token
        assert request(server, "GET", "/", host="evil.example:80")[0] == 403
        assert request(server, "POST", "/api/app_info", body={})[0] == 403
        assert request(server, "GET", "/bin/thumb?id=x")[0] == 403
        status, data = request(server, "POST", "/api/app_info", token=server.token, body={})
        assert status == 200 and json.loads(data)["result"]["games"]
        status, data = request(server, "POST", "/api/model_open", token=server.token, body={"id": "nope"})
        assert status == 400 and "isn't in the library" in json.loads(data)["error"]
        assert request(server, "GET", f"/bin/thumb?id=nope&t={server.token}")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_without_a_web_view_the_page_opens_in_the_browser(home, monkeypatch):
    import socket
    import sys
    import types

    from darkstl.app import desktop

    def no_web_view():
        raise RuntimeError("no web view here")

    monkeypatch.setitem(sys.modules, "webview",
                        types.SimpleNamespace(create_window=lambda *a, **k: object(), start=no_web_view))
    opened, served = [], []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    real_sleep = time.sleep

    def sleep(_):  # the wait for Ctrl+C: check the page the browser was sent to, then stop
        while not opened:
            real_sleep(0.02)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        served.append((opened[0], conn.getresponse().status))
        conn.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop.time, "sleep", sleep)
    with socket.socket() as s:  # a fixed port, as --port gives
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert desktop.run(port=port) == 0
    assert served == [(f"http://127.0.0.1:{port}/", 200)]


def test_generating_can_be_stopped(service, monkeypatch):
    import threading

    from darkstl import pipeline

    started, release = threading.Event(), threading.Event()

    def slow(model, settings, progress, **k):
        progress(0.1, "Building mesh")
        started.set()
        release.wait(10)
        progress(0.5, "Subdividing")  # the next step: where a stopped job ends
        raise AssertionError("should have stopped")

    monkeypatch.setattr(pipeline, "enhance", slow)
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    m = service.call("model_open", {"id": item["id"]})
    job_id = service.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 4000}})["job"]
    assert started.wait(10)
    job = service.call("job", {"id": job_id})
    assert job["stoppable"] and job["state"] == "running"
    stopped = service.call("job_stop", {"id": job_id})
    assert stopped["stopping"] and stopped["message"] == "Stopping…"
    release.set()
    end = time.time() + 10
    while service.call("job", {"id": job_id})["state"] == "running" and time.time() < end:
        time.sleep(0.02)
    job = service.call("job", {"id": job_id})
    assert job["state"] == "stopped" and not job["stoppable"]
    assert service.call("model_open", {"id": item["id"]})["enhanced"] is None  # nothing was kept
    # the start-up read of the game folders can't be stopped (the library would be half read)
    reading = service.load_folders()
    with pytest.raises(AppError, match="can't be stopped"):
        service.call("job_stop", {"id": reading.id})
    wait(service, reading.id)
