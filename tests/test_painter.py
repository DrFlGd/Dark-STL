"""The handoff to Model Painter: the For Model Painter export preset (a GLB with the textures and materials, Y up,
named <model>_for_painter.glb), where Model Painter is (set in Settings, or found where it's installed), and opening
an export in it."""

import json
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from darkstl.app import painter
from darkstl.app.service import AppError

from test_app import home, make_service, service, wait  # noqa: F401 - fixtures
from test_pipeline import weapon_dir  # noqa: F401 - fixture


def read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    assert data[:4] == b"glTF"
    (length,) = struct.unpack_from("<I", data, 12)
    gltf = json.loads(data[20:20 + length])
    blob = data[20 + length + 8:]
    return gltf, blob


def opened(service):  # noqa: F811
    item = service.call("models_query", {"game": "Dark Souls III", "category": "Weapons"})["items"][0]
    return service.call("model_open", {"id": item["id"]})


def test_the_painter_preset_writes_a_textured_y_up_glb(service, tmp_path):  # noqa: F811
    m = opened(service)
    out = tmp_path / "out"
    def export(**more):
        args = {"sid": m["sid"], "which": "original", "preset": "painter", "folder": str(out), **more}
        return wait(service, service.call("export", args)["job"])

    r = export(format=".stl", units="in", textures=False)
    path = Path(r["path"])
    assert path.name == "WP_A_0300_for_painter.glb"  # whatever the file settings say: the preset's own
    gltf, blob = read_glb(path)
    assert gltf["images"] and gltf["materials"][0]["name"] == "Blade"
    assert "baseColorTexture" in gltf["materials"][0]["pbrMetallicRoughness"]
    # the test weapon is a 1 m square lying flat (Z up in the app): Y up in the file, in metres
    pos = gltf["accessors"][gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    size = np.array(pos["max"]) - np.array(pos["min"])
    assert np.allclose(size, [1, 0, 1], atol=1e-3)
    # a name typed on the Export tab gets the preset's ending too, and nothing is written over
    again = export(name="sword")
    assert Path(again["path"]).name == "sword_for_painter.glb"
    third = export(name="sword")
    assert Path(third["path"]).name == "sword_for_painter (2).glb"


def test_the_painter_preset_takes_the_original_or_enhanced_model(service, tmp_path):  # noqa: F811
    m = opened(service)
    with pytest.raises(AppError, match="Finish and Cut moved to Model Painter"):  # no Finished model or Pieces since 0.55.0
        service.call("export", {"sid": m["sid"], "which": "pieces", "preset": "painter", "folder": str(tmp_path)})
    with pytest.raises(AppError, match="no export preset"):
        service.call("export", {"sid": m["sid"], "which": "original", "preset": "slicer", "folder": str(tmp_path)})


def test_model_painter_is_found_where_it_is_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(painter.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    assert painter.find("") is None
    # the portable zip unpacked in Downloads, into a folder of the zip's name
    unzipped = tmp_path / "user" / "Downloads" / "ModelPainter_0.2.0_x64-portable"
    portable = unzipped / "Model Painter" / "Model Painter.exe"
    portable.parent.mkdir(parents=True)
    portable.write_bytes(b"")
    assert painter.find("") == portable
    # installed for the user: found first
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    installed = tmp_path / "local" / "Programs" / "Model Painter" / "Model Painter.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")
    assert painter.find("") == installed
    # set in Settings: that one, or none when it's gone (not another found elsewhere)
    mine = tmp_path / "tools" / "Model Painter.exe"
    mine.parent.mkdir()
    mine.write_bytes(b"")
    assert painter.find(str(mine)) == mine
    assert painter.find(str(tmp_path / "gone.exe")) is None


def test_model_painter_setting_and_open(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(painter, "candidates", list)  # (none of this computer's)
    s = make_service(home)
    assert s.call("settings_get", {})["model_painter_found"] is None
    with pytest.raises(AppError, match="no program at"):
        s.call("settings_set", {"model_painter": str(tmp_path / "missing.exe")})
    exe = tmp_path / "Model Painter" / "Model Painter.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    r = s.call("settings_set", {"model_painter": f'"{exe}"'})  # pasted with quotes, as Explorer copies a path
    assert r["model_painter"] == str(exe) and r["model_painter_found"] == str(exe) and not r["model_painter_missing"]
    assert make_service(home).call("settings_get", {})["model_painter"] == str(exe)  # kept
    model = tmp_path / "sword_for_painter.glb"
    with pytest.raises(AppError, match="isn't there any more"):
        s.call("painter_open", {"path": str(model)})
    model.write_bytes(b"glTF")
    started = []

    class Host:
        dialogs = False

        def open_with(self, program, path):
            started.append((program, path))

    s.host = Host()
    assert s.call("painter_target", {"path": str(model)}) == {"exe": str(exe), "path": str(model)}
    assert started == []  # the desktop app's window starts it itself
    s.call("painter_open", {"path": str(model)})
    assert started == [(str(exe), str(model))]
    exe.unlink()
    assert s.call("settings_get", {})["model_painter_missing"]
    with pytest.raises(AppError, match="Model Painter wasn't found"):
        s.call("painter_open", {"path": str(model)})
    assert s.call("settings_set", {"model_painter": ""})["model_painter"] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in for Model Painter is a shell script")
def test_model_painter_is_started_with_the_file(tmp_path):
    said = tmp_path / "said.txt"
    stub = tmp_path / "Model Painter"
    stub.write_text(f'#!/bin/sh\nprintf "%s|%s" "$PWD" "$1" > "{said}"\n')
    os.chmod(stub, 0o755)
    model = tmp_path / "out" / "sword_for_painter.glb"
    painter.launch(stub, model)
    end = time.time() + 10
    while not (said.exists() and said.read_text()) and time.time() < end:
        time.sleep(0.02)
    where, given = said.read_text().split("|")
    assert Path(where).resolve() == tmp_path.resolve()  # started in its own folder
    assert given == str(model)  # with the file
