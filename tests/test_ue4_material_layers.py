"""Unreal 4 layered materials whose layers the mesh's vertex colours pick (Returnal's player models: one
material, four texture sets), a slot that names no material (shown with the game's material of the slot's
name), and Collect taking every texture such a material names."""

import numpy as np

from darkstl.importers.borderlands.ue4 import vfs
from darkstl.importers.returnal import GAME, ReturnalImporter
from darkstl.model import MeshOptions

import synth_ue4 as S

# 565 colours: the base layer and layers 1-3, then the head
COLOURS = [0x0000, 0xF800, 0x07E0, 0x001F]
HEAD = 0xFFFF
FLAT_NORMAL = 0x841F
EXTRAS = 12  # more textures than Collect used to follow from one material
KID = "/Game/Gameplay/Story/Kid"


def _tri(i: int) -> list[list[float]]:
    x = 200.0 * i
    return [[x, 0, 0], [x + 100, 0, 0], [x, 0, 100]]


def _packages() -> dict[str, S.Ue4Package]:
    out = {}

    def texture(folder: str, name: str, colour: int, normal: bool = False) -> None:
        b = S.Ue4Package(f"{folder}/{name}", 426)
        e = b.add_export("Texture2D", name)
        b.set_data(e, S.texture_data(b, 8, 8, S.bc1_solid(8, 8, colour), normal=normal))
        out[f"{folder.removeprefix('/Game/')}/{name}"] = b

    for layer, colour in enumerate(COLOURS):
        texture(KID, f"T_UV{layer + 1}_BaseColor", colour)
        texture(KID, f"T_UV{layer + 1}_Comp", FLAT_NORMAL, normal=True)
    for k in range(EXTRAS):
        texture(KID, f"T_UV1_Extra{k}", 0x7BEF)
    texture(f"{KID}/Head", "T_Kid_Head_BaseColor", HEAD)

    b = S.Ue4Package(f"{KID}/MI_Kid", 426)
    params = []
    for layer in range(4):
        params.append((f"BaseColor Texture (L{layer})",
                       b.import_object(f"{KID}/T_UV{layer + 1}_BaseColor", "Texture2D", f"T_UV{layer + 1}_BaseColor")))
        params.append((f"Comp Texture (L{layer})",
                       b.import_object(f"{KID}/T_UV{layer + 1}_Comp", "Texture2D", f"T_UV{layer + 1}_Comp")))
    for k in range(EXTRAS):
        params.append((f"Detail {k} (L0)", b.import_object(f"{KID}/T_UV1_Extra{k}", "Texture2D", f"T_UV1_Extra{k}")))
    e = b.add_export("MaterialInstanceConstant", "MI_Kid")
    b.set_data(e, S.mic_data(b, 0, params,
                             vectors=[("Layer1_SourceChannel", (1, 0, 0, 0)), ("Layer2_SourceChannel", (0, 1, 0, 0)),
                                      ("Layer3_SourceChannel", (0, 0, 1, 0))],
                             switches=[("UseVertexColorsAsBlendSource", True), ("Layer1_Enable", True),
                                       ("Layer2_Enable", True), ("Layer3_Enable", True)]))
    out["Gameplay/Story/Kid/MI_Kid"] = b

    b = S.Ue4Package(f"{KID}/Head/M_Kid_Head", 426)
    t = b.import_object(f"{KID}/Head/T_Kid_Head_BaseColor", "Texture2D", "T_Kid_Head_BaseColor")
    e = b.add_export("MaterialInstanceConstant", "M_Kid_Head")
    b.set_data(e, S.mic_data(b, 0, [("BaseColor", t)]))
    out["Gameplay/Story/Kid/Head/M_Kid_Head"] = b

    # four triangles of the body, one per vertex colour (black, red, green, blue), then the head's
    pos = np.array([p for i in range(5) for p in _tri(i)], float)
    colours = [(0, 0, 0, 255), (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 255, 255)]
    rgba = np.repeat(np.array(colours, np.uint8), 3, axis=0)
    b = S.Ue4Package("/Game/Gameplay/Player/Mesh/Kid/SK_Player_Kid", 426)
    mi = b.import_object(f"{KID}/MI_Kid", "MaterialInstanceConstant", "MI_Kid")
    e = b.add_export("SkeletalMesh", "SK_Player_Kid")
    b.set_data(e, S.skeletal_data(b, [mi, 0], pos, np.tile([0.0, -1.0, 0.0], (15, 1)), np.zeros((15, 2)),
                                  list(range(15)), [(0, 0, 4, (0,), 0, 12), (1, 12, 1, (0,), 12, 3)],
                                  joints=[[0, 0, 0, 0]] * 15, weights=[[255, 0, 0, 0]] * 15, colors=rgba,
                                  slots=["Base_mat", "M_Kid_Head"]))
    out["Gameplay/Player/Mesh/Kid/SK_Player_Kid"] = b
    return out


def _game(tmp_path):
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64" / "Returnal-Win64-Shipping.exe").write_bytes(b"MZ")
    files = {}
    for path, b in _packages().items():
        uasset, uexp, ubulk = b.build()
        files[f"Returnal/Content/{path}.uasset"] = uasset
        files[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            files[f"Returnal/Content/{path}.ubulk"] = ubulk
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(files, compress=True))
    vfs.forget(root)
    return root


def _kid(tmp_path):
    root = _game(tmp_path)
    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    found = {e.stem: e for e in imp.scan(root, GAME)}
    return imp, found["SK_Player_Kid"].path


def _albedo(m, index: int) -> str:
    return (m.materials[index].albedo or "").rsplit(":", 1)[-1]


def test_vertex_colours_pick_the_layer_each_triangle_shows(tmp_path):
    imp, path = _kid(tmp_path)
    m = imp.load(path)
    names = [x.name for x in m.materials]
    assert names == ["MI_Kid", "M_Kid_Head", "MI_Kid layer 1", "MI_Kid layer 2", "MI_Kid layer 3"]
    assert [_albedo(m, i) for i in range(5)] == ["T_UV1_BaseColor", "T_Kid_Head_BaseColor", "T_UV2_BaseColor",
                                                 "T_UV3_BaseColor", "T_UV4_BaseColor"]
    assert all((x.normal or "").endswith("_Comp") for i, x in enumerate(m.materials) if i != 1)
    assert any("vertex colour is R" in d for d in m.materials[2].details)
    mesh = m.build(MeshOptions())
    # each triangle shows its own layer's textures; the parts are still the mesh's sections
    assert sorted(np.bincount(mesh.face_material, minlength=5).tolist()) == [1, 1, 1, 1, 1]
    order = np.argsort(mesh.vertices[mesh.faces].mean(axis=1)[:, 0])
    assert mesh.face_material[order].tolist() == [0, 2, 3, 4, 1]
    assert sorted(set(mesh.face_group.tolist())) == [0, 1]
    for key in (m.materials[i].albedo for i in range(5)):
        assert key in m.textures


def test_a_slot_with_no_material_shows_the_material_named_like_it(tmp_path):
    imp, path = _kid(tmp_path)
    m = imp.load(path)
    head = m.materials[1]
    assert _albedo(m, 1) == "T_Kid_Head_BaseColor" and head.guessed
    assert "names no material for its slot M_Kid_Head" in head.details[0]


def test_collect_takes_every_texture_a_layered_material_names(tmp_path):
    imp, path = _kid(tmp_path)
    names = {r.path.name for r in imp.related_files(path)}
    want = {f"T_UV{n}_{kind}.uasset" for n in range(1, 5) for kind in ("BaseColor", "Comp")}
    want |= {f"T_UV1_Extra{k}.uasset" for k in range(EXTRAS)}
    want |= {"MI_Kid.uasset", "M_Kid_Head.uasset", "T_Kid_Head_BaseColor.uasset"}
    assert want <= names, sorted(want - names)


# -- layers picked by UV tile: Returnal's player suits (M_PlayerCharacter_Master's "LayerBlendUVBased") keep five
# texture sets as layers L0-L4, and each triangle shows the one whose number is the UV tile it sits in
SUIT = "/Game/Gameplay/Player/DLC_Suits/Proto"
TILE_COLOURS = [0x0000, 0xF800, 0x07E0]


def _suit_packages(uv_offsets) -> dict[str, S.Ue4Package]:
    out = {}
    for layer, colour in enumerate(TILE_COLOURS):
        for kind, c, normal in (("BaseColor", colour, False), ("Comp", FLAT_NORMAL, True)):
            name = f"T_Proto_Uv0{layer + 1}_{kind}"
            b = S.Ue4Package(f"{SUIT}/Maps/{name}", 426)
            e = b.add_export("Texture2D", name)
            b.set_data(e, S.texture_data(b, 8, 8, S.bc1_solid(8, 8, c), normal=normal))
            out[f"Gameplay/Player/DLC_Suits/Proto/Maps/{name}"] = b
    b = S.Ue4Package(f"{SUIT}/MI_Proto", 426)
    params = []
    for layer in range(3):
        for kind in ("BaseColor", "Comp"):
            name = f"T_Proto_Uv0{layer + 1}_{kind}"
            params.append((f"{kind} Texture (L{layer})", b.import_object(f"{SUIT}/Maps/{name}", "Texture2D", name)))
    e = b.add_export("MaterialInstanceConstant", "MI_Proto")
    b.set_data(e, S.mic_data(b, 0, params, switches=[("Layer1_Enable", True), ("Layer2_Enable", True)]))
    out["Gameplay/Player/DLC_Suits/Proto/MI_Proto"] = b

    # one triangle per entry of uv_offsets, its UVs moved by it; every vertex black, so no colour picks a layer
    n = len(uv_offsets)
    pos = np.array([p for i in range(n) for p in _tri(i)], float)
    corners = np.array([(0.1, 0.1), (0.9, 0.1), (0.1, 0.9)])
    uv = np.concatenate([corners + np.asarray(off, float) for off in uv_offsets])
    rgba = np.zeros((3 * n, 4), np.uint8)
    rgba[:, 3] = 255
    b = S.Ue4Package("/Game/Gameplay/Player/Mesh/SK_PlayerFP_Proto", 426)
    mi = b.import_object(f"{SUIT}/MI_Proto", "MaterialInstanceConstant", "MI_Proto")
    e = b.add_export("SkeletalMesh", "SK_PlayerFP_Proto")
    b.set_data(e, S.skeletal_data(b, [mi], pos, np.tile([0.0, -1.0, 0.0], (3 * n, 1)), uv, list(range(3 * n)),
                                  [(0, 0, n, (0,), 0, 3 * n)], joints=[[0, 0, 0, 0]] * (3 * n),
                                  weights=[[255, 0, 0, 0]] * (3 * n), colors=rgba))
    out["Gameplay/Player/Mesh/SK_PlayerFP_Proto"] = b
    return out


def _suit(tmp_path, uv_offsets):
    root = tmp_path / "Returnal"
    paks = root / "Returnal" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "Returnal" / "Binaries" / "Win64" / "Returnal-Win64-Shipping.exe").write_bytes(b"MZ")
    files = {}
    for path, b in _suit_packages(uv_offsets).items():
        uasset, uexp, ubulk = b.build()
        files[f"Returnal/Content/{path}.uasset"] = uasset
        files[f"Returnal/Content/{path}.uexp"] = uexp
        if ubulk:
            files[f"Returnal/Content/{path}.ubulk"] = ubulk
    (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(S.build_pak(files, compress=True))
    vfs.forget(root)
    imp = ReturnalImporter()
    imp.cache = tmp_path / "cache"
    found = {e.stem: e for e in imp.scan(root, GAME)}
    return imp.load(found["SK_PlayerFP_Proto"].path)


def _faces_left_to_right(m) -> list[int]:
    mesh = m.build(MeshOptions())
    order = np.argsort(mesh.vertices[mesh.faces].mean(axis=1)[:, 0])
    return mesh.face_material[order].tolist()


def test_uv_tiles_pick_the_layer_each_triangle_shows(tmp_path):
    """Triangles in UV tiles 0, 2, 1 and 0 (u 0-1, 2-3, 1-2, 0-1) show layers 0, 2, 1 and 0: each texture set
    where the game's suit material puts it, not the first set everywhere."""
    m = _suit(tmp_path, [(0, 0), (2, 0), (1, 0), (0, 0)])
    names = [x.name for x in m.materials]
    assert names == ["MI_Proto", "MI_Proto layer 1", "MI_Proto layer 2"]
    assert [_albedo(m, i) for i in range(3)] == ["T_Proto_Uv01_BaseColor", "T_Proto_Uv02_BaseColor",
                                                 "T_Proto_Uv03_BaseColor"]
    assert _faces_left_to_right(m) == [0, 2, 1, 0]
    assert "UVs sit in tile 2 (u 2 to 3" in m.materials[2].details[0]


def test_uvs_that_tile_across_whole_tiles_pick_no_layer(tmp_path):
    """UVs that repeat a texture (triangles reaching across tiles) aren't texture-set tiles: the material's own
    textures are shown on every triangle, as before."""
    m = _suit(tmp_path, [(0, 0), (1.5, 0), (2.5, 0)])
    assert [x.name for x in m.materials] == ["MI_Proto"]
    assert _faces_left_to_right(m) == [0, 0, 0]
