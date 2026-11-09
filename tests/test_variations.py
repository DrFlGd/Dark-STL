"""Saved variations: an enhanced model kept with the original under a name, listed, shown and exported again,
its settings taken up again, and kept through importing the game as packages and removing it."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from darkstl import variations
from darkstl.app.service import AppError
from darkstl.geometry.mesh import TriMesh
from darkstl.app.meshpack import unpack_mesh
from test_app import home, make_service, wait  # noqa: F401 - fixture
from test_pipeline import weapon_dir  # noqa: F401 - fixture

SETTINGS = {"target_vertices": 4000, "strength": 2.0}


def _weapon(s):
    items = s.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
    return next(i for i in items if i["stem"] == "wp_a_0300")


def test_the_mesh_is_kept_as_made(tmp_path):
    rng = np.random.default_rng(1)
    mesh = TriMesh(rng.random((50, 3)), rng.integers(0, 50, (40, 3)), rng.random((40, 3, 2)).astype(np.float32),
                   rng.integers(0, 3, 40).astype(np.int32), rng.integers(0, 2, 40).astype(np.int32),
                   {"displacement": rng.random(50), "note": "not kept"})
    variations.write_mesh(tmp_path / "m.npz", mesh)
    back = variations.read_mesh(tmp_path / "m.npz")
    assert np.allclose(back.vertices, mesh.vertices, atol=1e-6) and (back.faces == mesh.faces).all()
    assert np.allclose(back.corner_uvs, mesh.corner_uvs) and (back.face_material == mesh.face_material).all()
    assert (back.face_group == mesh.face_group).all() and np.allclose(back.metadata["displacement"], mesh.metadata["displacement"], atol=1e-6)
    assert "note" not in back.metadata


def test_a_variation_is_saved_shown_renamed_and_deleted(home, weapon_dir, tmp_path):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    item = _weapon(s)
    m = s.call("model_open", {"id": item["id"]})
    assert s.call("variations", {"sid": m["sid"]})["items"] == []
    with pytest.raises(AppError, match="Generate the enhanced model first"):
        s.call("variation_save", {"sid": m["sid"], "name": "x"})
    made = wait(s, s.call("enhance", {"sid": m["sid"], "settings": SETTINGS})["job"])
    saved = wait(s, s.call("variation_save", {"sid": m["sid"], "name": "Strong relief"})["job"])
    row = saved["variation"]
    assert row["name"] == "Strong relief" and row["settings"] == SETTINGS and row["thumb"]  # what it was made with
    assert row["mesh"] == {"parts": None, "flags": {}, "pose": None, "clean": None} and row["step"] == "both"
    assert row["result"]["stats"] == made["stats"] and [r["id"] for r in saved["items"]] == [row["id"]]
    # kept beside the model's package, which isn't written
    folder = home / "library" / "Dark Souls III packages" / "parts" / "wp_a_0300.partsbnd.dcx.darkpkg"
    assert (folder / "variations" / row["id"] / "mesh.npz").is_file() and not (folder / "package.json").exists()
    assert s.call("variation_thumb", {"model": item["id"], "id": row["id"]})[:4] == b"\x89PNG"
    # the enhanced model shown says it's kept
    assert s.call("model_open", {"id": item["id"]})["enhanced"]["variation"] == row["id"]
    # another Generate replaces it, and showing the variation brings it back
    other = wait(s, s.call("enhance", {"sid": m["sid"], "settings": {"target_vertices": 2000}})["job"])
    assert other["variation"] is None and other["stats"]["vertices"] != made["stats"]["vertices"]
    shown = s.call("variation_open", {"sid": m["sid"], "id": row["id"]})
    assert shown["stats"]["vertices"] == made["stats"]["vertices"] and shown["stats"]["source_vertices"] == made["stats"]["source_vertices"]
    assert shown["fields"] == made["fields"] and shown["variation"] == row["id"]
    header, _ = unpack_mesh(s.call("model_mesh", {"sid": m["sid"], "which": "enhanced"}))
    assert header["triangles"] == made["stats"]["faces"]
    stl = wait(s, s.call("export", {"sid": m["sid"], "which": "enhanced", "format": ".stl", "folder": str(tmp_path)})["job"])
    assert stl["bytes"] == 84 + 50 * made["stats"]["faces"]
    renamed = s.call("variation_rename", {"sid": m["sid"], "id": row["id"], "name": "Best so far"})
    assert renamed["variation"]["name"] == "Best so far"
    assert s.call("variation_delete", {"sid": m["sid"], "id": row["id"]})["items"] == []
    assert not folder.exists()  # nothing left behind
    with pytest.raises(AppError):
        s.call("variation_open", {"sid": m["sid"], "id": row["id"]})
    with pytest.raises(AppError):
        s.call("variation_open", {"sid": m["sid"], "id": "../../etc"})


def test_variations_are_kept_through_importing_the_game(home, weapon_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    m = s.call("model_open", {"id": _weapon(s)["id"]})
    made = wait(s, s.call("enhance", {"sid": m["sid"], "settings": SETTINGS})["job"])
    row = wait(s, s.call("variation_save", {"sid": m["sid"], "name": "Kept"})["job"])["variation"]
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "packages": True})["job"])
    s.call("folder_remove", {"path": str(weapon_dir)})
    shutil.rmtree(weapon_dir)
    fresh = make_service(home)
    wait(fresh, fresh.load_folders().id)
    packed = _weapon(fresh)
    assert packed["importer"] == "package"
    pm = fresh.call("model_open", {"id": packed["id"]})
    (listed,) = fresh.call("variations", {"sid": pm["sid"]})["items"]
    assert listed["id"] == row["id"] and listed["name"] == "Kept"
    shown = fresh.call("variation_open", {"sid": pm["sid"], "id": row["id"]})
    assert shown["stats"]["faces"] == made["stats"]["faces"]
    assert Path(fresh.call("variations", {"sid": pm["sid"]})["folder"]).parent == Path(packed["file"]).parent
