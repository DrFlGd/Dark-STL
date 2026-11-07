"""Models exported by other tools: glTF (Blender, FModel), ActorX PSK/PSKX (UModel, FModel), with the textures
their materials name found beside them, and imported as model packages like any game's."""

import json
import struct
from pathlib import Path

import numpy as np
import pytest
import synth
from test_app import home, wait  # noqa: F401 - fixture
from test_collect_offline import (  # noqa: F401 - fixtures
    _importer_cache,
    assert_same,
    collect_and_uninstall,
    keep_every_texture_and_uninstall,
    packages,
)
from test_heightmap import encode_normals
from test_pipeline import bump_height

from darkstl import export, library
from darkstl.importers import IMPORTERS, importer_for, open_model
from darkstl.importers.psk import material_textures, read_psk, texture_name
from darkstl.model import MeshOptions


def _png(img: np.ndarray) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def _albedo(green: int = 180) -> np.ndarray:
    img = np.zeros((16, 16, 4), np.uint8)
    img[..., 1] = green
    img[..., 3] = 255
    return img


# -- glTF ----------------------------------------------------------------------------------------------


def _gltf(d: Path, name: str = "panel", binary: bool = False) -> Path:
    """A 1 m square (Y up, facing +Y) under a node 2 m up, and a second mesh placed by a turned and scaled
    child node; a material with a colour and a normal map."""
    d.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    views, accessors = [], []

    def add(arr: np.ndarray, kind: str, component: int) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes})
        blob.extend(arr.tobytes())
        accessors.append({"bufferView": len(views) - 1, "componentType": component, "count": len(arr),
                          "type": kind})
        return len(accessors) - 1

    pos = np.array([[0, 0, 0], [1, 0, 0], [1, 0, -1], [0, 0, -1]], np.float32)
    nrm = np.tile(np.array([0, 1, 0], np.float32), (4, 1))
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], np.float32)
    idx = np.array([0, 1, 2, 0, 2, 3], np.uint16)
    a = {"POSITION": add(pos, "VEC3", 5126), "NORMAL": add(nrm, "VEC3", 5126), "TEXCOORD_0": add(uv, "VEC2", 5126)}
    i = add(idx, "SCALAR", 5123)
    images = []
    if binary:
        while len(blob) % 4:
            blob.append(0)
        data = _png(_albedo())
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob.extend(data)
        images.append({"bufferView": len(views) - 1, "mimeType": "image/png"})
        normal = _png(encode_normals(bump_height(32), "opengl"))
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(normal)})
        blob.extend(normal)
        images.append({"bufferView": len(views) - 1, "mimeType": "image/png"})
    else:
        (d / "tex").mkdir(exist_ok=True)
        (d / "tex" / f"{name}_colour.png").write_bytes(_png(_albedo()))
        (d / "tex" / f"{name}_normal.png").write_bytes(_png(encode_normals(bump_height(32), "opengl")))
        images = [{"uri": f"tex/{name}_colour.png"}, {"uri": f"tex/{name}_normal.png"}]
    gltf = {
        "asset": {"version": "2.0", "generator": "test"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Root", "translation": [0, 2, 0], "children": [1, 2]},
                  {"name": "Plate", "mesh": 0},
                  {"name": "Turned", "mesh": 1, "translation": [5, 0, 0], "scale": [2, 2, 2],
                   "rotation": [0, 0, float(np.sin(np.pi / 4)), float(np.cos(np.pi / 4))]}],  # 90 degrees about Z
        "meshes": [{"name": "plate", "primitives": [{"attributes": a, "indices": i, "material": 0}]},
                   {"name": "turned", "primitives": [{"attributes": {"POSITION": a["POSITION"]}, "indices": i}]}],
        "materials": [{"name": "Steel", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}},
                       "normalTexture": {"index": 1}}],
        "textures": [{"source": 0}, {"source": 1}],
        "images": images,
        "accessors": accessors, "bufferViews": views,
    }
    if binary:
        gltf["buffers"] = [{"byteLength": len(blob)}]
        js = json.dumps(gltf).encode()
        js += b" " * (-len(js) % 4)
        while len(blob) % 4:
            blob.append(0)
        out = d / f"{name}.glb"
        out.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(js) + len(blob))
                        + struct.pack("<I4s", len(js), b"JSON") + js
                        + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob))
    else:
        (d / f"{name}.bin").write_bytes(bytes(blob))
        gltf["buffers"] = [{"uri": f"{name}.bin", "byteLength": len(blob)}]
        out = d / f"{name}.gltf"
        out.write_text(json.dumps(gltf))
    return out


@pytest.mark.parametrize("binary", [False, True], ids=["gltf", "glb"])
def test_gltf_meshes_are_parts_placed_by_their_nodes(tmp_path, binary):
    path = _gltf(tmp_path / "models", binary=binary)
    assert importer_for(path).key == "gltf"
    model = open_model(path)
    assert [(p.name, p.triangles) for p in model.parts] == [("Plate", 2), ("Turned", 2)]
    mat = model.materials[0]
    assert mat.name == "Steel" and mat.normal_green == "opengl"
    assert model.textures[mat.albedo].decode()[0, 0, 1] == 180
    assert model.textures[mat.normal].decode().shape == (32, 32, 4)
    assert model.materials[model.parts[1].material].name == "Default"

    plate = model.build_mesh(MeshOptions(parts={0}))
    # Y up -> Z up, 2 m up: the square lies flat at z = 2, facing +Z, toward -Y as glTF's -Z is forward
    assert np.allclose(plate.vertices[:, 2], 2)
    assert np.allclose(plate.vertices.min(0), [0, 0, 2]) and np.allclose(plate.vertices.max(0), [1, 1, 2])
    assert np.allclose(plate.face_normals(), [0, 0, 1])
    assert np.allclose(plate.corner_normals.reshape(-1, 3), [0, 0, 1], atol=1e-6)
    # V kept as glTF has it (pointing down the picture, like the export frame)
    corner = np.argwhere(np.all(np.isclose(plate.vertices[plate.faces], [0, 0, 2]), axis=-1))[0]
    assert np.allclose(plate.corner_uvs[tuple(corner)], [0, 1])

    turned = model.build_mesh(MeshOptions(parts={1}))
    # turned 90 degrees about glTF's Z (x -> y), scaled 2, moved 5 along x, all under the root's 2 m
    assert np.allclose(np.sort(turned.vertices[:, 0]), [5, 5, 5, 5], atol=1e-6)
    assert np.ptp(turned.vertices[:, 2]) == pytest.approx(2) and turned.vertices[:, 2].min() == pytest.approx(2)
    assert np.ptp(turned.vertices[:, 1]) == pytest.approx(2)
    assert model.build_mesh().n_faces == 4
    assert not model.options[0].default  # metres


def test_gltf_written_by_dark_stl_reads_back(tmp_path):
    model = open_model(_gltf(tmp_path / "models"))
    mesh = model.build_mesh()
    out = tmp_path / "out" / "again.glb"
    out.parent.mkdir()
    export.write_glb(mesh, out, export.ExportOptions(scale=1000.0))
    back = open_model(out).build_mesh()
    assert back.n_faces == mesh.n_faces
    assert np.allclose(np.sort(back.vertices, axis=0), np.sort(mesh.vertices, axis=0), atol=1e-5)


def test_a_gltf_in_centimetres_is_read_as_such(tmp_path):
    path = _gltf(tmp_path / "models")
    gltf = json.loads(path.read_text())
    gltf["nodes"][0]["scale"] = [100, 100, 100]
    gltf["nodes"][0]["translation"] = [0, 0, 0]
    path.write_text(json.dumps(gltf))
    model = open_model(path)
    assert {o.key: o.default for o in model.options}["cm"] is True
    assert np.ptp(model.build_mesh(MeshOptions(parts={0}, flags={"cm": True})).vertices[:, 0]) == pytest.approx(1)


def test_model_packages_arent_listed_as_gltf_files(tmp_path):
    d = tmp_path / "models"
    _gltf(d, binary=True)
    pkg = d / "Game packages" / "sword.darkpkg"
    pkg.mkdir(parents=True)
    (pkg / "sword.glb").write_bytes((d / "panel.glb").read_bytes())
    assert importer_for(d / "panel.glb").key == "gltf" and not importer_for(d / "panel.glb").can_open(pkg / "sword.glb")
    assert [e.stem for e in library.scan_folder(d)[0] if e.importer == "gltf"] == ["panel"]


# -- PSK -----------------------------------------------------------------------------------------------


def _chunk(cid: str, size: int, rows: list[bytes]) -> bytes:
    return struct.pack("<20siii", cid.encode(), 0x1e83b9, size, len(rows)) + b"".join(rows)


def write_psk(path: Path, materials=("MI_Sword_Blade", "MI_Sword_Grip"), normals=True, bones=0) -> Path:
    """Two squares in Unreal's frame (cm, left-handed, Z up), 1 m wide, one per material, facing +Z."""
    points = [(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0), (0, 0, 50), (100, 0, 50), (100, 100, 50),
              (0, 100, 50)]
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)] * 2
    wedges = [struct.pack("<HHffBBH", p, 0, *uvs[p], p // 4, 0, 0) for p in range(8)]
    # wound as Unreal's own meshes are (see borderlands.mesh)
    faces = [struct.pack("<HHHBBI", *f, m, 0, 1) for f, m in
             [((0, 1, 2), 0), ((0, 2, 3), 0), ((4, 5, 6), 1), ((4, 6, 7), 1)]]
    data = _chunk("ACTRHEAD", 0, [])
    data += _chunk("PNTS0000", 12, [struct.pack("<3f", *p) for p in points])
    data += _chunk("VTXW0000", 16, wedges)
    data += _chunk("FACE0000", 12, faces)
    data += _chunk("MATT0000", 88, [m.encode().ljust(64, b"\0") + struct.pack("<iIiIii", i, 0, 0, 0, 0, 0)
                                    for i, m in enumerate(materials)])
    if bones:
        data += _chunk("REFSKELT", 120, [b"root".ljust(64, b"\0") + bytes(56)] * bones)
    if normals:
        data += _chunk("VTXNORMS", 12, [struct.pack("<3f", 0, 0, 1)] * 8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def umodel_export(root: Path) -> Path:
    """UModel's layout: a package's meshes, materials (.mat) and textures (DDS) in folders by kind, and a
    texture no material names."""
    pkg = root / "UmodelExport" / "Sword"
    psk = write_psk(pkg / "SkeletalMesh3" / "SK_Sword.psk", bones=2)
    (pkg / "MaterialInstanceConstant").mkdir(parents=True)
    (pkg / "MaterialInstanceConstant" / "MI_Sword_Blade.mat").write_text(
        "Diffuse=T_Sword_Blade_D\nNormal=T_Sword_Blade_N\nSpecular=T_Sword_Blade_S\nOther[0]=T_Noise\n")
    (pkg / "Texture2D").mkdir()
    for name, img in [("T_Sword_Blade_D", _albedo(180)), ("T_Sword_Blade_N", encode_normals(bump_height(32))),
                      ("T_Sword_Blade_S", _albedo(40)), ("T_Sword_Grip_D", _albedo(90)),
                      ("T_Sword_Grip_N", encode_normals(bump_height(16))), ("T_Unused", _albedo(10))]:
        (pkg / "Texture2D" / f"{name}.dds").write_bytes(synth.rgba_dds(img))
    return psk


def test_psk_is_mirrored_into_the_export_frame_with_its_materials_textures(tmp_path):
    psk = umodel_export(tmp_path)
    assert importer_for(psk).key == "psk"
    model = open_model(psk)
    assert [(p.name, p.triangles) for p in model.parts] == [("MI_Sword_Blade", 2), ("MI_Sword_Grip", 2)]
    blade, grip = model.materials
    # the blade's from its .mat file; the grip has none, so its textures are found by name
    assert (blade.albedo, blade.normal, blade.specular, blade.guessed) == \
        ("T_Sword_Blade_D", "T_Sword_Blade_N", "T_Sword_Blade_S", False)
    assert (grip.albedo, grip.normal, grip.guessed) == ("T_Sword_Grip_D", "T_Sword_Grip_N", True)
    assert blade.normal_green == "directx"
    assert model.textures["T_Sword_Blade_D"].decode()[0, 0, 1] == 180
    assert model.textures["T_Sword_Blade_D"].raw is not None  # a DDS file, kept as it is
    assert any("skeleton" in n for n in model.notes)

    mesh = model.build_mesh()
    # centimetres -> metres, Y mirrored: Unreal's (0, 100, 0) is (0, -1, 0)
    assert np.allclose(mesh.vertices.min(0), [0, -1, 0]) and np.allclose(mesh.vertices.max(0), [1, 0, 0.5])
    assert np.allclose(mesh.face_normals(), [0, 0, 1])
    corner = np.argwhere(np.all(np.isclose(mesh.vertices[mesh.faces], [0, -1, 0]), axis=-1))[0]
    assert np.allclose(mesh.corner_uvs[tuple(corner)], [0, 1])  # UVs as Unreal has them (V down)
    assert model.build_mesh(MeshOptions(parts={1})).vertices[:, 2].min() == pytest.approx(0.5)


def test_psk_without_normals_faces_up_too(tmp_path):
    with_normals = open_model(write_psk(tmp_path / "a" / "a.psk")).build_mesh()
    without = open_model(write_psk(tmp_path / "b" / "b.psk", normals=False)).build_mesh()
    assert np.allclose(with_normals.face_normals(), without.face_normals())


def test_psk_with_more_wedges_than_16_bits_hold(tmp_path):
    n = 70000
    pts = np.zeros((n, 3), np.float32)
    pts[:, 0] = np.arange(n)
    wedge = np.zeros(n, np.dtype([("p", "<u4"), ("u", "<f4"), ("v", "<f4"), ("m", "<u4")]))
    wedge["p"] = np.arange(n)
    face = np.zeros(1, np.dtype([("w", "<u4", 3), ("m", "u1"), ("aux", "u1"), ("smooth", "<u4")]))
    face["w"] = [0, n - 1, 1]
    data = (struct.pack("<20siii", b"PNTS0000", 0, 12, n) + pts.tobytes()
            + struct.pack("<20siii", b"VTXW0000", 0, 16, n) + wedge.tobytes()
            + struct.pack("<20siii", b"FACE3200", 0, 18, 1) + face.tobytes())
    psk = read_psk(data)
    assert psk.faces.tolist() == [[0, n - 1, 1]] and psk.wedge_point[-1] == n - 1
    assert psk.materials == ["Material 0"]


def test_material_files_of_fmodel_and_umodel(tmp_path):
    (tmp_path / "MI_A.json").write_text(json.dumps([{"Type": "MaterialInstanceConstant", "Name": "MI_A", "Properties": {
        "TextureParameterValues": [
            {"ParameterInfo": {"Name": "BaseColor"},
             "ParameterValue": {"ObjectName": "Texture2D'T_A_BC'", "ObjectPath": "/Game/A/T_A_BC.0"}},
            {"ParameterInfo": {"Name": "Normal"}, "ParameterValue": "Texture2D'/Game/A/T_A_N.T_A_N'"}]}}]))
    assert material_textures(tmp_path / "MI_A.json") == [("BaseColor", "T_A_BC"), ("Normal", "T_A_N")]
    (tmp_path / "MI_B.json").write_text(json.dumps({"Textures": {"Diffuse": "Game/B/T_B_D.T_B_D", "Mask": None}}))
    assert material_textures(tmp_path / "MI_B.json") == [("Diffuse", "T_B_D")]
    (tmp_path / "MI_C.props.txt").write_text(
        "TextureParameterValues[1] =\n{\n\t{\n\t\tParameterInfo = { Name=Diffuse }\n"
        "\t\tParameterValue = Texture2D'T_C_D'\n\t}\n}\n")
    assert material_textures(tmp_path / "MI_C.props.txt") == [("Diffuse", "T_C_D")]
    assert texture_name("T_D.tga") == "T_D" and texture_name("/Game/X/T_E.T_E") == "T_E"


def test_fmodel_export_finds_textures_through_material_json(tmp_path):
    root = tmp_path / "Exports" / "Game" / "Weapons"
    psk = write_psk(root / "Meshes" / "SM_Gun.pskx", materials=("MI_Gun",))
    (root / "Materials").mkdir()
    (root / "Materials" / "MI_Gun.json").write_text(json.dumps({"Textures": {"Diffuse": "/Game/Weapons/T_Gun_C"}}))
    (root / "Textures").mkdir()
    (root / "Textures" / "T_Gun_C.png").write_bytes(_png(_albedo(77)))
    model = open_model(psk)
    assert model.materials[0].albedo == "T_Gun_C"
    assert model.textures["T_Gun_C"].decode()[0, 0, 1] == 77
    assert "static" in model.info[0]


def test_a_folder_of_exports_lists_its_models_and_every_picture(tmp_path):
    psk = umodel_export(tmp_path / "Sword export")
    _gltf(tmp_path / "Sword export" / "blender")
    lib = library.Library()
    found = lib.add_root(tmp_path / "Sword export")
    assert sorted((e.stem, e.importer) for e in found) == [("SK_Sword", "psk"), ("panel", "gltf")]
    listings = [imp.list_textures(tmp_path / "Sword export") for imp in IMPORTERS]
    names = sorted(n for listed in listings for n, _ref in listed)
    assert sum(bool(x) for x in listings) == 1, "listed by one importer, once"
    assert "T_Unused" in names and "panel_colour" in names and len(names) == 8
    assert importer_for(psk).open_texture(tmp_path / "Sword export", next(
        r for listed in listings for n, r in listed if n == "T_Unused")).decode()[0, 0, 1] == 10


# -- imported as packages, then without the exports ----------------------------------------------------


def test_exports_collected_open_without_them(home, packages, tmp_path):  # noqa: F811
    root = tmp_path / "Sword export"
    umodel_export(root)
    _gltf(root / "blender", binary=True)
    before, after, _plan = collect_and_uninstall(home, root, packages=packages, dds=False)
    assert len(before) == 2
    assert_same(before, after)


def test_every_picture_of_exports_is_kept(home, tmp_path):  # noqa: F811
    root = tmp_path / "Sword export"
    umodel_export(root)
    t = keep_every_texture_and_uninstall(home, root)
    assert t["listed"] == 6  # the five the materials name, and one nothing does
