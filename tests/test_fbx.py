"""FBX models (binary and text), byte-built by synth_fbx: meshes, materials with their textures beside the file,
skin weights and the skeleton, axes and units, animations, and model packages made from them."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from darkstl import package as pkg
from darkstl.importers import importer_for, open_model
from darkstl.importers.fbx import FbxImporter
from darkstl.importers.fbx.reader import read
from darkstl.model import CatalogEntry, MeshOptions

import synth_fbx as F

STRIP = [(0, 0, 0), (4, 0, 0), (8, 0, 0), (0, 4, 0), (4, 4, 0), (8, 4, 0)]


def _folder(tmp_path: Path, scene: bytes, textures: bool = True) -> Path:
    """An export folder: the model in ``models/``, its textures in ``models/textures/``."""
    models = tmp_path / "export" / "models"
    (models / "textures").mkdir(parents=True)
    if textures:
        (models / "textures" / "Skin_D.png").write_bytes(F.png((200, 50, 50, 255)))
        (models / "textures" / "Cloth_N.png").write_bytes(F.png((128, 128, 255, 255)))
    path = models / "strip.fbx"
    path.write_bytes(scene)
    return path


def _points(mesh) -> set:
    return {tuple(np.round(v, 4)) for v in mesh.vertices}


@pytest.mark.parametrize("kind", ["binary", "text", "binary 7.5", "compressed"])
def test_an_fbx_model_loads_with_its_textures_and_skin(tmp_path, kind):
    scene = F.skinned_quad_scene(text=kind == "text", version=7500 if kind == "binary 7.5" else 7400,
                                 compress=kind == "compressed")
    path = _folder(tmp_path, scene)
    assert importer_for(path).key == "fbx"
    model = open_model(path)
    assert [p.name for p in model.parts] == ["Strip (Skin)", "Strip (Cloth)"]
    assert [(m.name, m.albedo, m.normal) for m in model.materials] == [("Skin", "Skin_D", None),
                                                                     ("Cloth", None, "Cloth_N")]
    assert model.textures["Skin_D"].decode()[0, 0].tolist() == [200, 50, 50, 255]
    assert model.textures["Cloth_N"].source.endswith("Cloth_N.png")
    mesh = model.build_mesh()
    assert _points(mesh) == {tuple(float(c) for c in p) for p in STRIP}  # metres, Z up, as the file says
    assert mesh.n_faces == 4
    assert np.allclose(mesh.face_normals(), [0, 0, 1])  # wound to face the way its normals do
    assert np.allclose(mesh.corner_normals, [0, 0, 1])
    # UVs as the file has them, V turned to Dark STL's way (0 at the top)
    at = {tuple(np.round(mesh.vertices[v], 3)): tuple(mesh.corner_uvs[f, k]) for f, face in enumerate(mesh.faces)
          for k, v in enumerate(face)}
    assert at[(0.0, 0.0, 0.0)] == (0.0, 1.0) and at[(8.0, 4.0, 0.0)] == (1.0, 0.0)
    # skin: the left quad follows Root, the right one Tip
    joints, weights = mesh.metadata["skin"]
    by_point = {tuple(np.round(v, 3)): int(j[0]) for v, j in zip(mesh.vertices, joints)}
    assert by_point[(0.0, 0.0, 0.0)] == 0 and by_point[(0.0, 4.0, 0.0)] == 0
    assert by_point[(8.0, 0.0, 0.0)] == 1 and by_point[(4.0, 4.0, 0.0)] == 1
    assert np.allclose(weights[:, 0], 1)
    assert model.rig.names == ["Root", "Tip"]
    assert model.rig.parents.tolist() == [-1, 0]
    assert np.allclose(model.rig.bind_world[1][:3, 3], (4, 0, 0))
    assert np.allclose(model.rig.bind_local[1][:3, 3], (4, 0, 0))
    assert model.animations is None and "no animations" in model.pose_note
    assert "FBX 7." in model.info[0] and ("text" if kind == "text" else "binary") in model.info[0]


def test_axes_and_units_become_metres_with_z_up(tmp_path):
    # Y up and centimetres (Maya's way): the same strip, a hundredth of the size
    path = _folder(tmp_path, F.skinned_quad_scene(up_axis=1, unit_cm=1.0))
    mesh = open_model(path).build_mesh()
    assert _points(mesh) == {tuple(round(c * 0.01, 4) for c in p) for p in STRIP}
    assert np.allclose(mesh.face_normals(), [0, 0, 1])
    rig = open_model(path).rig
    assert np.allclose(rig.bind_world[1][:3, 3], (0.04, 0, 0))
    assert np.allclose(rig.bind_world[1][:3, :3], np.eye(3))  # turned into the new axes, not scaled


def test_its_animations_pose_it(tmp_path):
    path = _folder(tmp_path, F.skinned_quad_scene(animated=True))
    model = open_model(path)
    (info,) = model.animations.list()
    assert (info.name, info.frames, info.fps) == ("Wave", 31, 30.0)
    assert info.as_dict()["moves"] == 1
    assert model.pose_note is None
    # Tip turns 90 degrees about Z over the second: (8, 0) swings round its joint at (4, 0) to (4, 4)
    end = model.build_mesh(MeshOptions(pose=("Wave", 30)))
    assert (4.0, 4.0, 0.0) in _points(end) and (8.0, 0.0, 0.0) not in _points(end)
    half = model.build_mesh(MeshOptions(pose=("Wave", 15)))
    r = 4 * np.sqrt(0.5)
    assert (round(4 + r, 4), round(r, 4), 0.0) in _points(half)
    # the left quad, on Root, stays
    assert {(0.0, 0.0, 0.0), (0.0, 4.0, 0.0)} <= _points(end)
    names, local = model.rig.pose_locals("Wave", 30)
    assert names == ["Root", "Tip"] and np.allclose(local[1][:3, 3], (4, 0, 0))
    assert model.rig.pose_all("Wave")[1].shape == (31, 2, 4, 4)


def test_its_bones_can_be_turned_without_animations(tmp_path):
    model = open_model(_folder(tmp_path, F.skinned_quad_scene()))
    turned = model.build_mesh(MeshOptions(bones={"Tip": (0.0, 0.0, 90.0)}))
    assert (4.0, 4.0, 0.0) in _points(turned) and (8.0, 0.0, 0.0) not in _points(turned)
    assert {(0.0, 0.0, 0.0), (0.0, 4.0, 0.0)} <= _points(turned)
    with pytest.raises(Exception, match="no animations"):
        model.rig.pose_locals("Wave", 0)


def test_a_model_without_a_skin_has_no_skeleton(tmp_path):
    model = open_model(_folder(tmp_path, F.skinned_quad_scene(with_skin=False)))
    assert model.rig is None and model.animations is None
    assert "skeleton" in model.pose_note
    assert "skin" not in model.build_mesh().metadata
    with pytest.raises(Exception):
        model.build_mesh(MeshOptions(pose=("Wave", 0)))


def test_textures_are_found_by_name_or_read_from_the_file(tmp_path):
    # the file says textures\\Skin_D.png, but it was moved into another folder of the export
    path = _folder(tmp_path, F.skinned_quad_scene(texture_dir="old place"), textures=False)
    moved = path.parents[1] / "pictures"
    moved.mkdir()
    (moved / "Skin_D.tga").write_bytes(b"")  # a picture of the same name, another kind
    (moved / "Skin_D.png").write_bytes(F.png((10, 20, 30, 255)))
    model = FbxImporter().load(path)
    assert model.materials[0].albedo == "Skin_D"
    assert model.textures["Skin_D"].source.endswith("Skin_D.png")
    assert any("Cloth_N.png wasn't found" in n for n in model.notes)

    # a picture embedded in the file is read from it
    other = tmp_path / "embedded"
    other.mkdir()
    (other / "e.fbx").write_bytes(F.skinned_quad_scene(texture_dir="gone", embed=F.png((1, 2, 3, 255))))
    model = FbxImporter().load(other / "e.fbx")
    key = model.materials[0].albedo
    assert "embedded" in model.textures[key].source
    assert model.textures[key].decode()[0, 0].tolist() == [1, 2, 3, 255]


def test_scanned_into_models_and_collected_with_its_textures(tmp_path):
    path = _folder(tmp_path, F.skinned_quad_scene())
    imp = FbxImporter()
    (entry,) = imp.scan(path.parents[1], "Other models")
    assert (entry.category, entry.group, entry.stem, entry.importer) == ("Models", "models", "strip", "fbx")
    related = {(r.path.name, r.kind) for r in imp.related_files(path)}
    assert related == {("strip.fbx", "model"), ("Skin_D.png", "texture"), ("Cloth_N.png", "texture")}
    assert ("Skin_D", "models/textures/Skin_D.png") in imp.list_textures(path.parents[1])


def test_an_fbx_model_becomes_a_package_that_poses_without_the_file(tmp_path):
    path = _folder(tmp_path, F.skinned_quad_scene(animated=True))
    model = open_model(path)
    root = tmp_path / "packages"
    store = pkg.AssetStore(root)
    entry = CatalogEntry("Other models", "Models", "", "strip", "strip", path, "fbx", packed=False)
    index = pkg.write_package(root, entry, "strip.fbx", [model], store)
    store.save()
    path.unlink()
    packed = open_model(index)
    assert packed.importer == "package"
    assert [m.albedo for m in packed.materials] == ["Skin_D", None]
    assert [a.name for a in packed.animations.list()] == ["Wave"]
    a = packed.build_mesh(MeshOptions(pose=("Wave", 30)))
    assert (4.0, 4.0, 0.0) in _points(a)


FBX6 = b"""; FBX 6.1.0 project file
; ----------------------------------------------------

FBXHeaderExtension:  {
    FBXHeaderVersion: 1003
    FBXVersion: 6100
    Creator: "FBX SDK/FBX Plugins build 20070228"
}
Objects:  {
    Model: "Model::Box", "Mesh" {
        Version: 232
        Properties60:  {
            Property: "Lcl Translation", "Lcl Translation", "A+",0,0,1
            Property: "Lcl Rotation", "Lcl Rotation", "A+",0,0,0
            Property: "Lcl Scaling", "Lcl Scaling", "A+",2,2,2
        }
        Vertices: 0,0,0,1,0,0,1,1,0,
                  0,1,0
        PolygonVertexIndex: 0,1,2,-4
        LayerElementUV: 0 {
            Version: 101
            Name: "UVChannel_1"
            MappingInformationType: "ByPolygonVertex"
            ReferenceInformationType: "IndexToDirect"
            UV: 0,0,1,0,1,1,0,1
            UVIndex: 0,1,2,3
        }
        LayerElementMaterial: 0 {
            MappingInformationType: "AllSame"
            ReferenceInformationType: "IndexToDirect"
            Materials: 0
        }
    }
    Material: "Material::Stone", "" {
        Version: 102
    }
    Texture: "Texture::Stone_D", "TextureVideoClip" {
        FileName: "C:\\\\work\\\\Stone_D.png"
        RelativeFilename: "Stone_D.png"
    }
    GlobalSettings:  {
        Version: 1000
        Properties60:  {
            Property: "UpAxis", "int", "",2
            Property: "UpAxisSign", "int", "",1
            Property: "FrontAxis", "int", "",1
            Property: "FrontAxisSign", "int", "",-1
            Property: "CoordAxis", "int", "",0
            Property: "CoordAxisSign", "int", "",1
            Property: "UnitScaleFactor", "double", "",100
        }
    }
}
Connections:  {
    Connect: "OO", "Model::Box", "Model::Scene"
    Connect: "OO", "Material::Stone", "Model::Box"
    Connect: "OO", "Texture::Stone_D", "Model::Box"
}
"""


def test_an_fbx_6_text_file_loads(tmp_path):
    (tmp_path / "Stone_D.png").write_bytes(F.png((90, 90, 90, 255)))
    (tmp_path / "box.fbx").write_bytes(FBX6)
    version, root = read(FBX6)
    assert version == 6100 and root.find("Objects") is not None
    model = open_model(tmp_path / "box.fbx")
    assert [m.name for m in model.materials] == ["Stone"] and model.materials[0].albedo == "Stone_D"
    mesh = model.build_mesh()
    assert _points(mesh) == {(0.0, 0.0, 1.0), (2.0, 0.0, 1.0), (2.0, 2.0, 1.0), (0.0, 2.0, 1.0)}
    assert mesh.n_faces == 2 and "FBX 6.1 (text)" in model.info[0]


def test_files_that_arent_fbx_are_refused(tmp_path):
    (tmp_path / "x.fbx").write_bytes(b"not a model at all")
    with pytest.raises(Exception, match="FBX"):
        FbxImporter().load(tmp_path / "x.fbx")
    cut = F.skinned_quad_scene()[:400]
    (tmp_path / "cut.fbx").write_bytes(cut)
    with pytest.raises(Exception):
        FbxImporter().load(tmp_path / "cut.fbx")
