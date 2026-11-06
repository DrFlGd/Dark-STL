"""Configurations: a base model with parts chosen per slot (configs.py), listed in the library and
opened, assembled, like any model."""

import json
from pathlib import Path

import numpy as np
import pytest
from test_app import wait

from darkstl import configs
from darkstl.app.config import Config
from darkstl.app.service import AppError, Service
from darkstl.errors import FormatError
from darkstl.geometry.mesh import TriMesh
from darkstl.library import Library
from darkstl.model import CatalogEntry, Material, MeshOptions, Part, SourceModel


def cube_obj(path: Path, size: float, at=(0.0, 0.0, 0.0), material: str | None = None) -> Path:
    h = size / 2
    x, y, z = at
    v = [(x + sx * h, y + sy * h, z + sz * h) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    quads = [(1, 3, 4, 2), (5, 6, 8, 7), (1, 2, 6, 5), (3, 7, 8, 4), (1, 5, 7, 3), (2, 4, 8, 6)]
    lines = [f"v {a} {b} {c}" for a, b, c in v]
    if material:
        lines.append(f"usemtl {material}")
    lines += [f"f {a} {b} {c}\nf {a} {c} {d}" for a, b, c, d in quads]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def gun_dir(tmp_path):
    root = tmp_path / "game"
    w = root / "weapons" / "rifle"
    cube_obj(w / "rifle_body.obj", 0.2)
    cube_obj(w / "rifle_mag_std.obj", 0.05, (0, 0, -0.15))
    cube_obj(w / "rifle_mag_drum.obj", 0.08, (0, 0, -0.16))
    cube_obj(w / "rifle_scope.obj", 0.04)  # modelled at the origin: placed by its attach offset
    listing = {
        "format": "darkstl-configurations", "version": 1, "game": "Test game",
        "configurations": [{
            "id": "rifle", "label": "Test rifle", "category": "Weapons", "group": "Rifles",
            "base": {"file": "weapons/rifle/rifle_body.obj"},
            "slots": [
                {"id": "mag", "label": "Magazine", "required": True, "default": "std", "options": [
                    {"id": "std", "label": "Standard", "model": "rifle_mag_std"},
                    {"id": "drum", "label": "Drum", "model": {"file": "WEAPONS/rifle/rifle_mag_drum.obj"}}]},
                {"id": "sight", "label": "Sight", "options": [
                    {"id": "scope", "label": "Scope", "model": "rifle_scope.obj", "attach": {"offset": [0, 0, 0.2]}},
                    {"id": "gone", "label": "Not there", "model": "rifle_scope_missing"}]},
            ],
            "presets": {"Heavy": {"mag": "drum", "sight": "scope"}},
        }],
    }
    (root / "test.darkstl-configs.json").write_text(json.dumps(listing))
    return root


def test_lists_parse_and_choices_are_made_valid(gun_dir):
    (c,) = configs.load_file(gun_dir / "test.darkstl-configs.json")
    assert c.id == "rifle" and c.game == "Test game" and [s.id for s in c.slots] == ["mag", "sight"]
    assert c.defaults() == {"mag": "std", "sight": ""}
    assert c.choose({"mag": "nope", "sight": "scope"}) == {"mag": "std", "sight": "scope"}
    assert c.choose({"mag": ""}) == {"mag": "std", "sight": ""}  # a required slot can't be emptied
    again = configs.parse(configs.dump([c], c.game))
    assert again[0].to_json() == c.to_json()
    with pytest.raises(FormatError):
        configs.parse({"format": "something else"})


def test_library_lists_configurable_models(gun_dir):
    lib = Library()
    lib.add_root(gun_dir, "Test game")
    (entry,) = lib.configurations
    assert entry.label == "Test rifle" and entry.category == "Weapons" and entry.group == "Rifles"
    assert entry.config.base.file == str(gun_dir / "weapons" / "rifle" / "rifle_body.obj")
    mag = entry.config.slots[0]
    assert [o.id for o in mag.options] == ["std", "drum"]
    # the option whose file isn't there is left out, and said so
    assert [o.id for o in entry.config.slots[1].options] == ["scope"]
    assert any("rifle_scope_missing" in n for n in lib.config_notes["Test game"])
    assert lib.find(entry.id) is entry and entry.id != lib.entries[0].id
    assert entry in lib.query("Test game", "Weapons")
    assert lib.query("Test game", text="test rifle") == [entry]


def test_assembled_model_holds_the_chosen_parts(gun_dir):
    from darkstl.importers import open_model

    lib = Library()
    lib.add_root(gun_dir, "Test game")
    config = lib.configurations[0].config
    load = open_model
    plain = configs.assemble(config, None, load)
    assert [p.name.split(" / ")[0] for p in plain.parts] == [plain.parts[0].name, "Magazine: Standard"]
    mesh = plain.build_mesh()
    assert mesh.n_faces == 24 and mesh.face_group.max() == 1
    full = configs.assemble(config, {"mag": "drum", "sight": "scope"}, load)
    mesh = full.build_mesh()
    assert mesh.n_faces == 36
    _lo, hi = mesh.bounds()
    assert hi[2] == pytest.approx(0.22)  # the scope, 4 cm, lifted 20 cm by its offset
    assert hi[1] == pytest.approx(0.20)  # the drum (OBJ's -Z is the export frame's +Y)
    assert full.configuration["chosen"] == {"mag": "drum", "sight": "scope"}
    assert full.configuration["presets"] == [{"name": "Heavy", "choices": {"mag": "drum", "sight": "scope"}}]
    # parts can still be left out, by their place in the assembled list
    no_mag = full.build_mesh(MeshOptions(parts={0, 2}))
    assert no_mag.n_faces == 24 and no_mag.bounds()[1][1] == pytest.approx(0.1)


def _fake(name, verts, sockets=None, n_parts=1):
    v = np.asarray(verts, float)
    faces = np.array([[0, 1, 2]] * n_parts)
    mesh = TriMesh(v, faces, np.zeros((n_parts, 3, 2), np.float32), np.zeros(n_parts, np.int32),
                   np.arange(n_parts, dtype=np.int32))

    def build(o: MeshOptions):
        keep = np.array([o.parts is None or i in o.parts for i in range(n_parts)])
        return mesh.subset(keep)

    return SourceModel(name, Path(name), "test", build, parts=[Part(f"p{i}", 0, 3, 1) for i in range(n_parts)],
                       materials=[Material("m", albedo="tex")], textures={"tex": object()}, sockets=sockets or {})


def test_parts_go_on_sockets_of_the_base_or_of_another_part():
    turn = np.eye(4)
    turn[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]  # 90 degrees about Z
    turn[:3, 3] = [1, 0, 0]
    rail_top = np.eye(4)
    rail_top[2, 3] = 0.5
    models = {
        "body": _fake("body", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], {"rail": turn}),
        "rail": _fake("rail", [[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0]], {"top": rail_top}),
        "scope": _fake("scope", [[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0]]),
    }
    config = configs.Configuration("g", "Gun", "Game", configs.ModelRef("body"), [
        configs.Slot("sight", "Sight", [configs.PartOption("s", "Scope", configs.ModelRef("scope"),
                                                           configs.Attach(socket="top", to="rail"))]),
        configs.Slot("rail", "Rail", [configs.PartOption("r", "Rail", configs.ModelRef("rail"),
                                                         configs.Attach(socket="rail"))], required=True),
    ])
    m = configs.assemble(config, {"sight": "s"}, lambda p, v: models[p])
    mesh = m.build_mesh()
    scope = mesh.vertices[3:6]  # in the order of the slots
    rail = mesh.vertices[6:9]
    assert np.allclose(rail, [[1, 0, 0], [1, 0.1, 0], [0.9, 0, 0]])  # turned and moved onto the body's socket
    assert np.allclose(scope, rail + [0, 0, 0.5])  # on the rail's own socket, wherever the rail went
    # each part's textures and materials keep their own names
    assert set(m.textures) == {"tex", "rail:tex", "sight:tex"}
    assert [x.albedo for x in m.materials] == ["tex", "sight:tex", "rail:tex"]
    assert sorted(set(mesh.face_material.tolist())) == [0, 1, 2]
    # a socket that isn't there leaves the part at the origin, with a note
    config.slots[1].options[0].attach = configs.Attach(socket="nowhere")
    m = configs.assemble(config, {}, lambda p, v: models[p])
    assert any("nowhere" in n for n in m.notes)


def test_mirroring_keeps_triangles_facing_out():
    mesh = TriMesh(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float), np.array([[0, 1, 2]]))
    a = configs.attach_matrix(configs.Attach(matrix=[-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]), None)
    out = configs.transform_mesh(mesh, a)
    assert np.allclose(out.face_normals()[0], mesh.face_normals()[0])


def test_parts_guessed_from_a_weapons_folder(tmp_path):
    def entry(stem, folder="w/rifle"):
        return CatalogEntry("G", "Weapons", "", stem, stem, tmp_path / folder / f"{stem}.mesh")

    entries = [entry("w_rifle_ajax"), entry("w_rifle_ajax_mag_std"), entry("w_rifle_ajax_mag_ext"),
               entry("w_rifle_ajax_scope"), entry("w_rifle_ajax_flag"), entry("lonely", "w/other")]
    (c,) = configs.from_folders(entries, "G")
    assert c.guessed and c.base.file.endswith("w_rifle_ajax.mesh")
    slots = {s.id: [o.id for o in s.options] for s in c.slots}
    assert slots == {"magazine": ["w_rifle_ajax_mag_ext", "w_rifle_ajax_mag_std"], "sight": ["w_rifle_ajax_scope"],
                     "part_flag": ["w_rifle_ajax_flag"]}
    assert c.defaults()["magazine"] == "w_rifle_ajax_mag_ext"
    # a body named as such is the base even when another name is shorter
    entries = [entry("gun_body", "w/g"), entry("gun_mag", "w/g"), entry("gun_stock", "w/g")]
    (c,) = configs.from_folders(entries, "G")
    assert c.base.file.endswith("gun_body.mesh") and {s.id for s in c.slots} == {"magazine", "stock"}


@pytest.fixture
def app(tmp_path, monkeypatch, gun_dir):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    s = Service(Config.load(), tmp_path / "home" / "cache")
    wait(s, s.call("folder_add", {"path": str(gun_dir), "game": "Test game"})["job"])
    return s


def test_app_opens_a_configuration_with_choices(app):
    items = app.call("models_query", {"game": "Test game", "category": "Weapons"})["items"]
    (cfg,) = [i for i in items if i.get("configurable")]
    assert cfg["configurable"] == {"slots": 2, "options": 3, "guessed": False}
    m = app.call("model_open", {"id": cfg["id"]})
    assert m["configuration"]["chosen"] == {"mag": "std", "sight": ""} and m["stats"]["triangles"] == 24
    heavy = app.call("model_open", {"id": cfg["id"], "choices": {"mag": "drum", "sight": "scope"}})
    assert heavy["sid"] != m["sid"] and heavy["stats"]["triangles"] == 36
    assert len(heavy["parts"]) == 3
    assert app.call("configs", {"game": "Test game"})["items"][0]["id"] == cfg["id"]
    # collecting it takes the base and every part
    plan = app.call("collect_plan", {"ids": [cfg["id"]]})
    assert plan["models"] == 4


def test_app_saves_your_own_configurations(app, gun_dir):
    body = next(i for i in app.call("models_query", {"game": "Test game", "q": "rifle_body"})["items"]
                if not i.get("configurable"))
    made = app.call("config_save", {"base_id": body["id"], "configuration": {"label": "My rifle", "slots": [
        {"id": "mag", "label": "Mag", "options": [
            {"id": "a", "label": "Drum", "path": str(gun_dir / "weapons" / "rifle" / "rifle_mag_drum.obj"),
             "attach": {"offset": [0, 0, 0], "rotate": [0, 0, 0]}}]}]}})
    saved = json.loads(Path(made["file"]).read_text())
    (c,) = saved["configurations"]
    assert c["base"] == {"file": "weapons/rifle/rifle_body.obj"} and c["id"] == "my:rifle_body"
    assert c["slots"][0]["options"][0] == {"id": "a", "label": "Drum", "model": {"file": "weapons/rifle/rifle_mag_drum.obj"}}
    m = app.call("model_open", {"id": made["id"], "choices": {"mag": "a"}})
    assert m["stats"]["triangles"] == 24 and m["configuration"]["label"] == "My rifle"
    # editing the folder's list keeps a copy of your own, which wins
    rifle = next(i for i in app.call("configs", {})["items"] if i["label"] == "Test rifle")
    cfg = app.call("model_open", {"id": rifle["id"]})["configuration"]
    cfg["label"] = "Renamed rifle"
    again = app.call("config_save", {"configuration": cfg})
    assert again["id"] == rifle["id"]
    assert app.call("model_open", {"id": rifle["id"]})["configuration"]["label"] == "Renamed rifle"
    gone = app.call("config_delete", {"id": rifle["id"]})
    assert gone["removed"] and gone["still_listed"]  # the folder's own list shows again
    assert app.call("model_open", {"id": rifle["id"]})["configuration"]["label"] == "Test rifle"
    with pytest.raises(AppError):
        app.call("config_delete", {"id": body["id"]})


def test_a_material_slot_repaints_the_parts():
    def model(name, mats):
        def build(options):
            return TriMesh(np.zeros((3, 3)) + [[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.array([[0, 1, 2]]))
        return SourceModel(name=name, path=Path(name), importer="t", build=build,
                           materials=[Material(m, f"{m}.dds") for m in mats], textures={f"{m}.dds": m for m in mats},
                           parts=[Part(name, 0, 3, 1)])

    files = {"base": model("base", ["gun", "glass"]), "barrel": model("barrel", ["metal"]),
             "skin": model("skin", ["paint"])}
    c = configs.Configuration("g", "Gun", "G", configs.ModelRef("base"), [
        configs.Slot("barrel", "Barrel", [configs.PartOption("b", "B", configs.ModelRef("barrel"))], True),
        configs.Slot("skin", "Skin", [configs.PartOption("red", "Red", configs.ModelRef("skin", "v", []))], False,
                     None, "material")])
    plain = configs.assemble(c, None, lambda f, v: files[f])
    assert [m.albedo for m in plain.materials] == ["gun.dds", "glass.dds", "barrel:metal.dds"]
    painted = configs.assemble(c, {"skin": "red"}, lambda f, v: files[f])
    # the skin's one material replaces each model's first; the base's second stays
    assert [m.albedo for m in painted.materials] == ["skin:paint.dds", "glass.dds", "skin:paint.dds"]
    assert painted.textures["skin:paint.dds"] == "paint" and painted.build_mesh().n_faces == 2
    with pytest.raises(FormatError):
        configs.parse({"configurations": [{"id": "x", "base": "b", "slots": [{"id": "s", "kind": "colour"}]}]})
