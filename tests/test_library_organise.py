"""Library II on the engine side: collections, tags and stars (kept across rescans), related models (the importers'
``related`` hook), each game's problems, and the queue that runs bulk actions one after another."""

import json
import shutil
import time
from pathlib import Path

import pytest

from darkstl import library_collections
from darkstl.app.config import Config
from darkstl.app.service import AppError, Service
from darkstl.importers.fromsoft import catalog

import synth
from test_app import wait
from test_pipeline import weapon_dir  # noqa: F401 - fixture


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def service(home, weapon_dir):  # noqa: F811
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(weapon_dir)})["job"])
    return s


def _ids(s, game="Dark Souls III"):
    return {i["stem"]: i["id"] for i in s.call("models_query", {"game": game, "limit": 500, "lod": True})["items"]}


# ------------------------------------------------------------------ collections, tags, stars

def test_the_collections_file_round_trips(tmp_path):
    path = tmp_path / "collections.json"
    c = library_collections.Collections(path)
    made = c.create("To print", [("DS3", "parts/a.partsbnd.dcx"), ("ER", "chr/c1.chrbnd.dcx")])
    again = c.create("to print")  # names are made unique, whatever their case
    assert again["name"] == "to print (2)"
    c.add(made["id"], [("DS3", "parts/a.partsbnd.dcx"), ("DS3", "parts/b.partsbnd.dcx")])  # one there already
    c.move(made["id"], [("DS3", "parts/b.partsbnd.dcx")], ("DS3", "parts/a.partsbnd.dcx"))
    c.tag([("DS3", "parts/a.partsbnd.dcx")], add=["Cloth", "  needs   supports "])
    c.tag([("ER", "chr/c1.chrbnd.dcx")], add=["cloth"])  # the same tag, kept as first written
    c.star([("ER", "chr/c1.chrbnd.dcx")], True)
    c.save()
    back = library_collections.Collections(path)
    first = back.get(made["id"])
    assert [m["key"] for m in first["models"]] == ["parts/b.partsbnd.dcx", "parts/a.partsbnd.dcx", "chr/c1.chrbnd.dcx"]
    assert back.mark(("DS3", "parts/a.partsbnd.dcx"))["tags"] == ["Cloth", "needs supports"]
    assert back.mark(("ER", "chr/c1.chrbnd.dcx")) == {"tags": ["Cloth"], "star": True}
    assert back.all_tags() == {"Cloth": 2, "needs supports": 1}
    assert back.holding(("DS3", "parts/a.partsbnd.dcx")) == [made["id"]]
    assert back.has_marks("ER") and not back.has_marks("Bloodborne")
    assert back.rename_tag("cloth", "fabric") == 2
    assert back.all_tags() == {"fabric": 2, "needs supports": 1}
    back.remove(made["id"], [("ER", "chr/c1.chrbnd.dcx")])
    assert back.delete(again["id"]) and len(back.collections()) == 1


def test_collections_tags_and_stars_survive_a_rescan(service, weapon_dir):  # noqa: F811
    ids = _ids(service)
    c = service.call("collection_create", {"name": "Knights", "ids": [ids["c1100"], ids["hd_m_1000"]]})["collection"]
    assert c["count"] == 2 and c["present"] == 2
    service.call("collection_add", {"id": c["id"], "ids": [ids["wp_a_0300"]]})
    service.call("models_tag", {"ids": [ids["wp_a_0300"], ids["c1100"]], "add": ["Print next"]})
    service.call("models_star", {"ids": [ids["wp_a_0300"]]})
    # the page sees them on every model
    row = service.call("models_get", {"ids": [ids["wp_a_0300"]]})["items"][0]
    assert row["tags"] == ["Print next"] and row["starred"] and row["collections"] == [{"id": c["id"], "name": "Knights"}]
    # a tag is searched like a name, and filters
    assert {i["stem"] for i in service.call("models_query", {"q": "print next"})["items"]} == {"wp_a_0300", "c1100"}
    assert service.call("models_query", {"tags": ["print next"], "starred": True})["total"] == 1
    facets = service.call("models_query", {"facets": True})["facets"]
    assert facets["tags"] == [{"name": "Print next", "count": 2}]
    # the collection lists its models in its order; the order can change
    listed = service.call("models_query", {"collection": c["id"]})
    assert [i["stem"] for i in listed["items"]] == ["c1100", "hd_m_1000", "wp_a_0300"]
    service.call("collection_move", {"id": c["id"], "ids": [ids["wp_a_0300"]], "before": ids["c1100"]})
    assert [i["stem"] for i in service.call("models_query", {"collection": c["id"]})["items"]] == ["wp_a_0300", "c1100", "hd_m_1000"]
    # a rescan makes new entries: they're still in the collection, tagged and starred
    wait(service, service.call("folder_rescan", {"path": str(weapon_dir)})["job"])
    again = service.call("models_query", {"collection": c["id"]})
    assert [i["stem"] for i in again["items"]] == ["wp_a_0300", "c1100", "hd_m_1000"] and again["missing"] == 0
    assert service.call("models_query", {"tags": ["Print next"]})["total"] == 2
    # and so is a new start of the app
    s2 = Service(Config.load(), service.cache)
    wait(s2, s2.load_folders().id)
    assert [i["stem"] for i in s2.call("models_query", {"collection": c["id"]})["items"]] == ["wp_a_0300", "c1100", "hd_m_1000"]
    assert s2.call("collections", {})["starred"] == 1
    # the game's index carries them too, for the search
    index = json.loads(s2.index.file("Dark Souls III").read_text("utf-8"))
    rows = {r["stem"]: r for r in index["models"]}
    assert rows["wp_a_0300"]["tags"] == ["Print next"] and rows["wp_a_0300"]["starred"]
    # removed from the library, a model stays in the collection, counted as missing until it's back
    s2.call("folder_remove", {"path": str(weapon_dir)})
    gone = s2.call("models_query", {"collection": c["id"]})
    assert gone["total"] == 0 and gone["missing"] == 3
    s2.call("collection_delete", {"id": c["id"]})
    assert s2.call("collections", {})["collections"] == []


def test_a_collection_needs_a_name_and_library_models(service):
    with pytest.raises(AppError):
        service.call("collection_create", {"name": "  "})
    with pytest.raises(AppError):
        service.call("collection_create", {"name": "x", "ids": ["no-such-model"]})


# ------------------------------------------------------------------ related models

def test_fromsoftware_families():
    assert catalog.family("hd_m_1000") == ("set:m:1000", "Head")
    assert catalog.family("LG_M_1000_L") == ("set:m:1000", "Low-detail copy")
    assert catalog.family("wp_a_0300_1") == ("wp:a:0300", "Variant")
    assert catalog.family("c1100") == ("c:1100", "Character")
    assert catalog.family("o000612") == ("o:o000612", "Object")
    assert catalog.family("m10_00_00_00_000100") is None


@pytest.fixture
def set_dir(weapon_dir):  # noqa: F811
    """An armour set (head, body, arms, legs, one with its low-detail copy), a weapon with its sheath, a
    character and its variant."""
    parts = weapon_dir / "parts"
    model = (parts / "hd_m_1000.partsbnd.dcx").read_bytes()
    for name in ("bd_m_1000", "am_m_1000", "lg_m_1000", "lg_m_1000_l", "hd_m_1001", "wp_a_0300_1"):
        (parts / f"{name}.partsbnd.dcx").write_bytes(model)
    shutil.copy(weapon_dir / "chr" / "c1100.chrbnd.dcx", weapon_dir / "chr" / "c1100_1.chrbnd.dcx")
    return weapon_dir


def test_related_models_from_the_importers(home, set_dir):
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(set_dir)})["job"])
    ids = _ids(s)
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": ids["hd_m_1000"]})["items"]}
    assert rel == {"bd_m_1000": "Same armour set: body", "am_m_1000": "Same armour set: arms",
                   "lg_m_1000": "Same armour set: legs"}  # not another set, nor another piece's low-detail copy
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": ids["lg_m_1000"]})["items"]}
    assert rel["lg_m_1000_l"] == "Low-detail copy"
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": ids["wp_a_0300"]})["items"]}
    assert rel == {"wp_a_0300_1": "Sheath or variant", "wp_a_0300_l": "Low-detail copy"}
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": ids["wp_a_0300_1"]})["items"]}
    assert rel == {"wp_a_0300": "Its weapon"}
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": ids["c1100"]})["items"]}
    assert rel == {"c1100_1": "Variant"}
    # the cards say how many each has
    cards = {i["stem"]: i.get("related", 0) for i in s.call("models_query", {"game": "Dark Souls III", "category": "Armor"})["items"]}
    assert cards["hd_m_1000"] == 3 and cards["hd_m_1001"] == 0
    # a configurable model made of two of them: its parts, and it for each part
    base = s.call("models_get", {"ids": [ids["hd_m_1000"]]})["items"][0]
    part = s.call("models_get", {"ids": [ids["bd_m_1000"]]})["items"][0]
    made = s.call("config_save", {"base_id": base["id"], "configuration": {
        "label": "Knight set", "category": "Armor", "group": "Sets",
        "slots": [{"id": "body", "label": "Body", "required": False, "default": "on",
                   "options": [{"id": "on", "label": "Body", "path": part["file"]}]}]}})
    rel = {i["stem"]: i["relation"] for i in s.call("model_related", {"id": made["id"]})["items"]}
    assert rel == {"hd_m_1000": "Its base model", "bd_m_1000": "A part: Body"}
    rel = [i["relation"] for i in s.call("model_related", {"id": ids["bd_m_1000"]})["items"]]
    assert "Configurable model it's part of" in rel


def test_related_models_follow_into_packages(home, set_dir):
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(set_dir)})["job"])
    wait(s, s.call("collect_run", {"game": "Dark Souls III", "category": "Armor", "packages": True, "animations": False})["job"])
    s.call("folder_remove", {"path": str(set_dir)})
    ids = _ids(s)
    items = s.call("model_related", {"id": ids["hd_m_1000"]})["items"]
    assert {i["stem"] for i in items} == {"bd_m_1000", "am_m_1000", "lg_m_1000"}
    assert all(i["importer"] == "package" for i in items)


def test_re_engine_characters_parts_are_related(tmp_path):
    """A character's body, face and hair, each in its own folder below the character's, are related; so are the
    packages made of them (which mirror the game's folders)."""
    from darkstl.importers.package import PackageImporter
    from darkstl.importers.reengine import REEngineImporter
    from darkstl.model import CatalogEntry

    def entry(root, rel, importer="reengine"):
        p = root / "natives" / "stm" / rel
        stem = p.name.split(".")[0]
        if importer == "package":
            p = p.with_name(p.name + ".darkpkg") / "package.json"
        e = CatalogEntry("RE8", "Characters", "ch03", stem, stem, p, importer=importer)
        if importer == "package":
            e.__dict__["_source_importer"] = "reengine"
        return e

    for root, importer in ((tmp_path / "game", "reengine"), (tmp_path / "RE8 packages", "package")):
        entries = [entry(root, "character/ch/ch03/0200/body/body00/ch03_0200_body00.mesh.2101050001", importer),
                   entry(root, "character/ch/ch03/0200/face/face00/ch03_0200_face00.mesh.2101050001", importer),
                   entry(root, "character/ch/ch03/0200/hair/hair00/ch03_0200_hair00.mesh.2101050001", importer),
                   entry(root, "character/ch/ch03/0210/body/body00/ch03_0210_body00.mesh.2101050001", importer)]
        imp = PackageImporter() if importer == "package" else REEngineImporter()
        rel = {e.stem: r for r, e in imp.related(entries[0], entries)}
        assert rel == {"ch03_0200_face00": "Same character: face", "ch03_0200_hair00": "Same character: hair"}
        assert imp.related(entries[3], entries) == []


# ------------------------------------------------------------------ problems

def test_a_model_with_a_missing_texture_is_a_problem(home, tmp_path):
    """A model whose material names a texture the game's folder doesn't have is listed under Textures not found
    once it's opened; one that can't be read under Models that couldn't be read; a file gone since the scan
    under Missing files."""
    from darkstl.importers.fromsoft.formats import dcx

    game = tmp_path / "DARK SOULS III" / "Game"
    parts = game / "parts"
    parts.mkdir(parents=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    pos, nrm, uv, idx = synth.grid_plane(2, size=0.5)
    mats = [("Cloth", "N:\\mtd\\C[DSB].mtd", [("N:\\tex\\WP_A_0500_a.tga", "g_DiffuseTexture"),
                                             ("N:\\tex\\WP_A_0500_n.tga", "g_BumpmapTexture")])]
    flver_bytes = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], mats, uv_float=True)
    import numpy as np

    albedo = np.full((8, 8, 4), 200, np.uint8)
    tpf = synth.build_tpf([("WP_A_0500_a", synth.rgba_dds(albedo))])  # the normal map isn't there
    bnd = synth.build_bnd4([(200, "N:\\parts\\Weapon\\WP_A_0500\\WP_A_0500.flver", flver_bytes),
                            (100, "N:\\parts\\Weapon\\WP_A_0500\\WP_A_0500.tpf", tpf)])
    (parts / "wp_a_0500.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    (parts / "wp_a_0600.partsbnd.dcx").write_bytes(b"not a model at all")
    (parts / "wp_a_0700.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    s = Service(Config.load(), home / "cache")
    wait(s, s.call("folder_add", {"path": str(game)})["job"])
    ids = _ids(s)
    before = s.call("problems", {"game": "Dark Souls III"})
    assert before["groups"] == [] and before["models"] == 3  # (nothing known to be wrong before they are looked at)
    s.call("model_open", {"id": ids["wp_a_0500"]})
    assert s.call("thumb", {"id": ids["wp_a_0600"]}) is None  # can't be drawn
    (parts / "wp_a_0700.partsbnd.dcx").unlink()
    found = s.call("problems", {"game": "Dark Souls III"})
    groups = {g["id"]: g for g in found["groups"]}
    assert [m["stem"] for m in groups["textures"]["models"]] == ["wp_a_0500"]
    assert "1 texture it names" in groups["textures"]["models"][0]["why"]
    assert [m["stem"] for m in groups["unreadable"]["models"]] == ["wp_a_0600"]
    assert groups["unreadable"]["models"][0]["why"]
    assert [m["stem"] for m in groups["missing"]["models"]] == ["wp_a_0700"]
    assert found["count"] == 3
    # read again once it's fixed: off the list
    (parts / "wp_a_0600.partsbnd.dcx").write_bytes(dcx.compress_dflt(bnd))
    s.call("model_open", {"id": ids["wp_a_0600"]})
    assert "unreadable" not in {g["id"] for g in s.call("problems", {"game": "Dark Souls III"})["groups"]}


# ------------------------------------------------------------------ previews

def test_a_turntable_sprite_is_drawn_after_the_preview(service):
    import io

    import numpy as np
    from PIL import Image

    from darkstl import thumbnails

    mid = _ids(service)["wp_a_0300"]
    assert service.call("thumb", {"id": mid, "turn": "1", "cached": "1"}) == b""  # not drawn yet
    assert service.call("thumb", {"id": mid})[:4] == b"\x89PNG"
    service.thumbs.wait_turns()  # drawn in the background from the model just drawn
    strip = service.call("thumb", {"id": mid, "turn": "1", "cached": "1"})
    img = np.asarray(Image.open(io.BytesIO(strip)))
    size = thumbnails.TURN_SIZE
    assert img.shape == (size, size * thumbnails.TURN_FRAMES, 4)
    frames = [img[:, i * size:(i + 1) * size, 3] > 0 for i in range(thumbnails.TURN_FRAMES)]
    assert all(f.sum() > 50 for f in [frames[1], frames[3]])  # a plane seen from the side is thin: 0 and 4 may not be
    assert (frames[1] != frames[2]).any()  # it turns
    # forgotten with the preview, and drawn when it's asked for (a card hovered) if it isn't waiting
    service.call("thumbs_redraw", {"ids": [mid]})
    assert service.call("thumb", {"id": mid, "turn": "1", "cached": "1"}) == b""
    assert service.call("thumb", {"id": mid, "turn": "1"})[:4] == b"\x89PNG"


def test_characters_previews_are_drawn_posed(service):
    from pathlib import Path

    from darkstl import thumbnails

    class Anims:
        def __init__(self, names):
            self._names = names

        def names(self):
            return self._names

    assert thumbnails.resting_pose(Anims(["a050_000100", "a000_000000", "a000_000020"])) == ("a000_000000", 0)
    assert thumbnails.resting_pose(Anims(["Walk", "Stand_Loop", "Idle_Combat", "Idle"])) == ("Idle", 0)
    assert thumbnails.resting_pose(Anims(["Attack", "Run"])) is None
    ids = _ids(service)
    character = service.call("models_query", {"game": "Dark Souls III", "category": "Characters"})["items"][0]
    service.call("thumb", {"id": character["id"]})
    path = Path(service.library.find(character["id"]).path)
    assert service.thumbs.file(path, None, posed=True).exists()  # a character: drawn posed (if it can be)
    weapon = Path(service.library.find(ids["wp_a_0300"]).path)
    service.call("thumb", {"id": ids["wp_a_0300"]})
    assert service.thumbs.file(weapon).exists() and not service.thumbs.file(weapon, None, posed=True).exists()
    assert service.call("settings_get", {})["posed_previews"] is True
    assert service.call("settings_set", {"posed_previews": False})["posed_previews"] is False
    service.call("thumb", {"id": character["id"]})
    assert service.thumbs.file(path).exists()  # Settings › Previews: not posed


# ------------------------------------------------------------------ the queue

def _queue_done(s, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        q = s.call("queue", {})
        if not q["active"]:
            return q
        time.sleep(0.05)
    raise AssertionError("the queue didn't finish")


def test_bulk_export_writes_each_model_through_the_queue(service, tmp_path):
    ids = _ids(service)
    out = tmp_path / "exports"
    chosen = [ids["wp_a_0300"], ids["hd_m_1000"], ids["c1100"]]
    first = service.call("queue_add", {"action": "export_glb", "ids": chosen, "folder": str(out)})
    second = service.call("queue_add", {"action": "export_glb", "ids": chosen[:1], "folder": str(out)})
    assert len(first["added"]) == 1 and len(second["items"]) == 2
    q = _queue_done(service)
    a, b = q["items"]
    assert a["state"] == "done" and b["state"] == "done", (a, b)
    assert a["result"]["done"] == 3 and a["result"]["failed"] == []
    assert sorted(p.suffix for p in out.iterdir()) == [".glb"] * 4  # one after another, none overwritten
    assert a["label"] == "Exporting 3 models as GLB"
    service.call("queue_clear", {})
    assert service.call("queue", {})["items"] == []


def test_a_waiting_item_is_taken_off_and_the_running_one_stopped(service, monkeypatch, tmp_path):
    import threading

    ids = _ids(service)
    gate = threading.Event()
    real = service.cmd_model_open

    def slow_open(args):
        gate.wait(10)
        return real(args)

    monkeypatch.setattr(service, "cmd_model_open", slow_open)
    out = tmp_path / "exports"
    running = service.call("queue_add", {"action": "export_glb", "ids": [ids["wp_a_0300"], ids["c1100"]], "folder": str(out)})["added"][0]
    waiting = service.call("queue_add", {"action": "export_glb", "ids": [ids["c1100"]], "folder": str(out)})["added"][0]
    service.call("queue_stop", {"id": waiting})
    service.call("queue_stop", {"id": running})
    gate.set()
    q = {i["id"]: i for i in _queue_done(service)["items"]}
    assert q[waiting]["state"] == "stopped" and q[waiting]["job"] is None
    assert q[running]["state"] == "stopped"
    assert not out.exists() or len(list(out.iterdir())) <= 1


def test_bulk_enhance_saves_a_variation_with_the_last_settings(service):
    ids = _ids(service)
    opened = service.call("model_open", {"id": ids["wp_a_0300"]})
    wait(service, service.call("enhance", {"sid": opened["sid"], "settings": {"target_vertices": 2000, "strength": 2}})["job"])
    assert service.config.last_enhance["Dark Souls III"]["target_vertices"] == 2000
    service.call("queue_add", {"action": "enhance", "ids": [ids["hd_m_1000"]]})
    (item,) = _queue_done(service)["items"]
    assert item["state"] == "done" and item["result"]["done"] == 1, item
    saved = service.call("variations", {"model": ids["hd_m_1000"]})["items"]
    assert len(saved) == 1 and saved[0]["settings"]["target_vertices"] == 2000


def test_bulk_collect_and_packages_go_a_game_at_a_time(service, tmp_path):
    ids = _ids(service)
    added = service.call("queue_add", {"action": "packages", "ids": [ids["wp_a_0300"], ids["hd_m_1000"]]})["added"]
    assert len(added) == 1
    (item,) = _queue_done(service)["items"]
    assert item["state"] == "done", item
    assert item["label"] == "Writing packages of 2 models from Dark Souls III"
    stems = {i["stem"]: i for i in service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]}
    assert stems["wp_a_0300"]["importer"] == "package" and stems["hd_m_1000"]["importer"] == "package"
    with pytest.raises(AppError):
        service.call("queue_add", {"action": "melt", "ids": [ids["c1100"]]})
