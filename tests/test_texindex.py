"""Textures kept elsewhere in a game folder: the index a scan makes of them, models finding theirs
in it, collecting them, and the previews kept until a rescan."""

import os

import numpy as np
import pytest

from darkstl import library, thumbnails
from darkstl.importers.fromsoft import files, texindex
from darkstl.importers.fromsoft.formats import dcx

import synth
from test_app import home, make_service, wait  # noqa: F401 - fixture


def _dds(red: int) -> bytes:
    img = np.zeros((8, 8, 4), np.uint8)
    img[..., 0] = red
    img[..., 3] = 255
    return synth.rgba_dds(img)


def _model(textures: list[tuple[str, str]]) -> bytes:
    pos, nrm, uv, idx = synth.grid_plane(2, size=1.0)
    return synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)],
                             [("Mat", "N:\\mtd\\C[DSB].mtd", [(f"N:\\tex\\{n}.tga", t) for n, t in textures])],
                             uv_float=True)


@pytest.fixture
def shared_dir(tmp_path):
    """A game whose models take their textures from elsewhere: an object from its map's split pack, an
    armour piece from a shared pack, and a character from another character's archive."""
    game = tmp_path / "DARK SOULS III" / "Game"
    for d in ("obj/o00", "map/m24", "parts", "chr"):
        (game / d).mkdir(parents=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    obj = _model([("m24_wall_a", "g_DiffuseTexture"), ("m24_wall_n", "g_BumpmapTexture")])
    (game / "obj/o00/o000100.objbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "o000100.flver", obj)])))
    bhd, bdt = synth.build_bxf4([(i, f"{n}.tpf.dcx", dcx.compress_dflt(synth.build_tpf([(n, _dds(10 * i))])))
                                 for i, n in enumerate(["m24_other_a", "m24_wall_a", "m24_wall_n"])])
    (game / "map/m24/m24_0000.tpfbhd").write_bytes(bhd)
    (game / "map/m24/m24_0000.tpfbdt").write_bytes(bdt)
    arm = _model([("common_body_a", "g_DiffuseTexture")])
    (game / "parts/am_m_1000.partsbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "am_m_1000.flver", arm)])))
    # a big pack, so its names are read from its first part only
    noise = np.random.default_rng(0).integers(0, 255, (512, 512, 4), np.uint8)
    common = synth.build_tpf([("common_body_a", _dds(200)), ("common_noise", synth.rgba_dds(noise))])
    (game / "parts/common_body.tpf.dcx").write_bytes(dcx.compress_dflt(common))
    borrower = _model([("c2509_body_a", "g_DiffuseTexture")])
    (game / "chr/c2500.chrbnd.dcx").write_bytes(dcx.compress_dflt(synth.build_bnd4([(200, "c2500.flver", borrower)])))
    lender = synth.build_bnd4([(200, "c2509.flver", _model([])), (100, "c2509.tpf", synth.build_tpf([("c2509_body_a", _dds(90))]))])
    (game / "chr/c2509.chrbnd.dcx").write_bytes(dcx.compress_dflt(lender))
    return game


def test_names_are_read_from_headers(shared_dir):
    assert texindex.names_in_file(shared_dir / "map/m24/m24_0000.tpfbhd") == ["m24_other_a", "m24_wall_a", "m24_wall_n"]
    big = shared_dir / "parts/common_body.tpf.dcx"
    assert big.stat().st_size > texindex.HEAD
    assert texindex.names_in_file(big) == ["common_body_a", "common_noise"]
    assert texindex.texture_name("N:\\x\\m24_wall_a.tpf.dcx") == "m24_wall_a"


def test_models_find_their_textures_elsewhere_in_the_game(shared_dir):
    obj = shared_dir / "obj/o00/o000100.objbnd.dcx"
    texindex.forget(shared_dir)
    assert set(files.load_model(obj).textures) == set()  # not scanned: nothing to look in
    library.Library().add_root(shared_dir)
    asset = files.load_model(obj)
    assert set(asset.textures) == {"m24_wall_a", "m24_wall_n"}  # only those it names, not the whole pack
    assert asset.textures["m24_wall_a"].decode()[0, 0, 0] == 10
    assert [(n, p.name) for n, p in asset.located] == [("m24_wall_a", "m24_0000.tpfbhd"), ("m24_wall_n", "m24_0000.tpfbhd")]
    assert any("elsewhere in the game folder" in n for n in asset.notes)
    arm = files.load_model(shared_dir / "parts/am_m_1000.partsbnd.dcx")
    assert set(arm.textures) == {"common_body_a"} and arm.textures["common_body_a"].decode()[0, 0, 0] == 200
    # by the start of its name: c2509_body_a is in c2509's archive
    borrower = files.load_model(shared_dir / "chr/c2500.chrbnd.dcx")
    assert set(borrower.textures) == {"c2509_body_a"} and borrower.located[0][1].name == "c2509.chrbnd.dcx"
    # and the materials use them
    from darkstl.importers import open_model

    model = open_model(obj)
    assert model.materials[0].albedo == "m24_wall_a" and model.materials[0].normal == "m24_wall_n"


def test_the_index_is_kept_between_scans(shared_dir, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    first = texindex.build(shared_dir, cache=cache)
    assert first.names["m24_wall_a"] == ["map/m24/m24_0000.tpfbhd"]
    read = []
    real = texindex.names_in_file
    monkeypatch.setattr(texindex, "names_in_file", lambda p: read.append(p.name) or real(p))
    again = texindex.build(shared_dir, cache=cache)
    assert read == [] and again.names == first.names
    pack = shared_dir / "parts/common_body.tpf.dcx"
    pack.write_bytes(dcx.compress_dflt(synth.build_tpf([("common_body_b", _dds(5))])))
    os.utime(pack, ns=(pack.stat().st_atime_ns, pack.stat().st_mtime_ns + 10**9))
    changed = texindex.build(shared_dir, cache=cache)
    assert read == ["common_body.tpf.dcx"] and "common_body_b" in changed.names and "common_body_a" not in changed.names


def test_collecting_writes_out_textures_taken_from_elsewhere(home, shared_dir):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(shared_dir)})["job"])
    (obj,) = s.call("models_query", {"game": "Dark Souls III", "q": "o000100"})["items"]
    plan = s.call("collect_plan", {"ids": [obj["id"]]})
    assert plan["kinds"]["texture"]["files"] == 2 and plan["files"] == 3
    r = wait(s, s.call("collect_run", {"ids": [obj["id"]]})["job"])
    folder = s.config.library_folder / "Dark Souls III"
    assert r["copied"] == 3
    assert sorted(p.name for p in (folder / "_textures").iterdir()) == ["m24_wall_a.dds", "m24_wall_n.dds"]
    assert not (folder / "map").exists()  # not the map's whole pack
    copy = folder / "obj/o00/o000100.objbnd.dcx"
    asset = files.load_model(copy)
    assert set(asset.textures) == {"m24_wall_a", "m24_wall_n"}
    assert all(str(p).startswith(str(folder)) for _n, p in asset.located)
    assert asset.textures["m24_wall_n"].decode()[0, 0, 0] == 20


def test_previews_are_kept_until_a_rescan(home, shared_dir, monkeypatch):  # noqa: F811
    s = make_service(home)
    wait(s, s.call("folder_add", {"path": str(shared_dir)})["job"])
    (obj,) = s.call("models_query", {"game": "Dark Souls III", "q": "o000100"})["items"]
    drawn = []
    real = thumbnails.ThumbnailCache._draw
    monkeypatch.setattr(thumbnails.ThumbnailCache, "_draw", lambda self, *a: drawn.append(a[0]) or real(self, *a))
    assert s.call("thumb", {"id": obj["id"], "cached": "1"}) == b""  # not drawn yet: said at once
    png = s.call("thumb", {"id": obj["id"]})
    assert png[:4] == b"\x89PNG" and len(drawn) == 1
    assert s.call("thumb", {"id": obj["id"], "cached": "1"}) == png
    s.call("thumb", {"id": obj["id"]})
    assert len(drawn) == 1  # kept
    wait(s, s.call("folder_add", {"path": str(shared_dir)})["job"])  # adding again is not a rescan
    s.call("thumb", {"id": obj["id"]})
    assert len(drawn) == 1
    wait(s, s.call("folder_rescan", {"path": str(shared_dir)})["job"])
    assert s.call("thumb", {"id": obj["id"], "cached": "1"}) == b""
    s.call("thumb", {"id": obj["id"]})
    assert len(drawn) == 2


def test_a_model_that_cant_be_drawn_is_not_tried_again(tmp_path, monkeypatch):
    cache = thumbnails.ThumbnailCache(tmp_path / "previews")
    model = tmp_path / "broken.flver"
    model.write_bytes(b"not a model")
    calls = []

    def boom(*a):
        calls.append(a)
        raise ValueError("broken")

    monkeypatch.setattr(cache, "_draw", boom)
    with pytest.raises(ValueError):
        cache.get(model)
    assert cache.get(model) is None and cache.get(model, draw=False) is None and len(calls) == 1
    assert cache.clear(only_failed=True) == 1
    with pytest.raises(ValueError):
        cache.get(model)
    assert len(calls) == 2
    cache.forget([model])
    with pytest.raises(ValueError):
        cache.get(model)
    assert len(calls) == 3
