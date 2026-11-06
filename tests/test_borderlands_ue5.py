"""The Borderlands importer for Borderlands 4 (Unreal 5): IoStore containers, zen packages, type mappings and
unversioned properties."""

import struct

import pytest

from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.borderlands import BorderlandsImporter, files
from darkstl.importers.borderlands.ue4 import asset, iostore, unversioned, usmap, vfs
from darkstl.importers.borderlands.ue4.props import Array4, Struct4
from darkstl.model import MeshOptions

import synth_ue4 as S
import synth_zen as Z
from test_borderlands4 import IDX as IDX_, KEY, NRM as NRM_, POS as POS_, UV as UV_, gun_packages


def _bl4(tmp_path, key=None, compress=True):
    root = tmp_path / "Borderlands 4"
    paks = root / "OakGame" / "Content" / "Paks"
    paks.mkdir(parents=True)
    (root / "OakGame" / "Binaries" / "Win64").mkdir(parents=True)
    (root / "OakGame" / "Binaries" / "Win64" / "Borderlands4.exe").write_bytes(b"MZ")
    Z.build_game(paks, gun_packages(426), key=key, compress=compress)
    vfs.forget(root)
    return root


def test_toc_and_directory_index(tmp_path):
    chunks = [((11, 0, 1), b"a" * 70000, "OakGame/Content/A/B.uasset"),
              ((11, 0, 2), b"b" * 10, "OakGame/Content/A/B.ubulk"),
              ((12, 0, 1), bytes(range(256)) * 9, "OakGame/Content/C.uasset"), ((99, 0, 6), b"hdr", None)]
    for key in (None, KEY):
        utoc, ucas = Z.build_utoc(chunks, 99, key=key)
        (tmp_path / "x.utoc").write_bytes(utoc)
        (tmp_path / "x.ucas").write_bytes(ucas)
        toc = iostore.read_toc(tmp_path / "x.utoc")
        assert toc.entry_count == 4 and toc.container_id == 99 and toc.encrypted == (key is not None)
        assert toc.chunk_ids[1] == (11, 0, 2) and toc.methods == ["none", "Zlib"]
        k, mount, listed = iostore._directory(toc, [bytes(32), KEY])
        assert k == key and mount == "../../../"
        assert sorted(listed) == [("OakGame/Content/A/B.uasset", 0), ("OakGame/Content/A/B.ubulk", 1),
                                  ("OakGame/Content/C.uasset", 2)]
        c = iostore.Container(toc, k)
        for i, (_cid, data, _p) in enumerate(chunks):
            assert c.read(i) == data
        assert c.read(0, 16380, 10) == b"a" * 10
        c.close()


@pytest.mark.parametrize("key", [None, KEY])
def test_scan_and_open_bl4(tmp_path, key):
    root = _bl4(tmp_path, key=key)
    if key is not None:
        (root / vfs.KEY_FILE).write_text("0x" + KEY.hex())
    imp = BorderlandsImporter()
    assert files.detect_game(root) == files.GAME_BL4
    found = imp.scan(root, files.GAME_BL4)
    stems = {e.stem: e for e in found}
    assert set(stems) == {"SK_AR_ATL_Body", "SK_AR_ATL_Barrel_01", "SK_AR_ATL_Barrel_02", "SK_AR_ATL_Mag_01",
                          "SM_Crate"}
    body = stems["SK_AR_ATL_Body"]
    assert body.packed and importer_for(body.path).key == "borderlands"
    for stem in ("SK_AR_ATL_Body", "SK_AR_ATL_Barrel_01"):
        m = open_model(stems[stem].path)
        mesh = m.build(MeshOptions())
        assert len(mesh.faces) == 4
        mat = m.materials[0]
        assert mat.name == "MI_Gun" and mat.albedo and mat.normal
        albedo = m.textures[mat.albedo].load()
        assert albedo.shape[:2] == (8, 8) and albedo[0, 0, 0] > 240
    crate = open_model(stems["SM_Crate"].path)
    assert len(crate.parts) == 2
    configs = imp.configurations(root, files.GAME_BL4, found)
    assert len(configs) == 1 and [s.id for s in configs[0].slots] == ["barrel", "magazine"]


def test_bl4_locked_without_key(tmp_path):
    root = _bl4(tmp_path, key=KEY)
    imp = BorderlandsImporter()
    found = imp.scan(root, files.GAME_BL4)
    assert found == []
    assert any(vfs.KEY_FILE in n for n in imp.folder_notes(root, found))


def test_zen_imports_resolve_through_other_packages(tmp_path):
    root = _bl4(tmp_path)
    gf = vfs.game_files(root, files.GAME_BL4)
    pkg = gf.open_package("OakGame/Content/Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun.uasset")
    assert pkg.main_export().class_name == "MaterialInstanceConstant"
    names = {(i.name, i.class_name) for i in pkg.imports}
    assert ("T_Gun_Dif", "Texture2D") in names
    ref = [i for i, x in enumerate(pkg.imports) if x.name == "T_Gun_Dif"][0]
    assert pkg.import_package(-(ref + 1)) == "/Game/Gear/Weapons/AssaultRifles/Atlas/Textures/T_Gun_Dif"
    assert gf.zen.class_of("OakGame/Content/InteractiveObjects/Crates/SM_Crate.uasset") == "StaticMesh"


# -- type mappings and unversioned properties ---------------------------------------------

def _usmap(structs, enums) -> bytes:
    """A .usmap (version 3) for structs given as (name, super, [(index, dim, name, type)]), types as tuples."""
    names: list[str] = []

    def n(s):
        if s not in names:
            names.append(s)
        return struct.pack("<i", names.index(s))

    kinds = {k: i for i, k in enumerate(usmap.TYPES)}

    def t(tp):
        out = bytes([kinds[tp[0]]])
        if tp[0] == "StructProperty":
            out += n(tp[1])
        elif tp[0] in ("ArrayProperty", "SetProperty"):
            out += t(tp[1])
        elif tp[0] == "EnumProperty":
            out += t(tp[1]) + n(tp[2])
        return out

    body = struct.pack("<I", len(enums))
    for en, values in enums.items():
        body += n(en) + struct.pack("<H", len(values)) + b"".join(n(v) for v in values)
    body += struct.pack("<I", len(structs))
    for sn, sup, props in structs:
        count = sum(p[1] for p in props)
        body += n(sn) + (n(sup) if sup else struct.pack("<i", -1)) + struct.pack("<HH", count, len(props))
        for idx, dim, pn, tp in props:
            body += struct.pack("<HB", idx, dim) + n(pn) + t(tp)
    lut = struct.pack("<I", len(names)) + b"".join(struct.pack("<H", len(s)) + s.encode() for s in names)
    payload = lut + body
    return struct.pack("<HB", 0x30C4, 3) + struct.pack("<i", 0) + bytes([0]) + struct.pack("<II", len(payload),
                                                                                          len(payload)) + payload


MAPPINGS = _usmap(
    [("Object", "", []),
     ("MaterialInterface", "Object", [(0, 1, "SubsurfaceProfile", ("ObjectProperty",))]),
     ("MaterialInstance", "MaterialInterface", [
         (0, 1, "Parent", ("ObjectProperty",)), (1, 1, "bHasStaticPermutationResource", ("BoolProperty",)),
         (2, 1, "ScalarParameterValues", ("ArrayProperty", ("StructProperty", "ScalarParameterValue"))),
         (3, 1, "TextureParameterValues", ("ArrayProperty", ("StructProperty", "TextureParameterValue")))]),
     ("MaterialInstanceConstant", "MaterialInstance", []),
     ("MaterialParameterInfo", "", [(0, 1, "Name", ("NameProperty",)),
                                    (1, 1, "Association",
                                     ("EnumProperty", ("ByteProperty",), "EMaterialParameterAssociation")),
                                    (2, 1, "Index", ("IntProperty",))]),
     ("TextureParameterValue", "", [(0, 1, "ParameterInfo", ("StructProperty", "MaterialParameterInfo")),
                                    (1, 1, "ParameterValue", ("ObjectProperty",)),
                                    (2, 1, "ExpressionGUID", ("StructProperty", "Guid"))]),
     ("ScalarParameterValue", "", [(0, 1, "ParameterInfo", ("StructProperty", "MaterialParameterInfo")),
                                   (1, 1, "ParameterValue", ("FloatProperty",)),
                                   (2, 1, "ExpressionGUID", ("StructProperty", "Guid"))]),
     ("Thing", "Object", [(0, 2, "Pair", ("IntProperty",)), (2, 1, "Where", ("StructProperty", "Vector")),
                          (3, 1, "Tags", ("ArrayProperty", ("NameProperty",)))])],
    {"EMaterialParameterAssociation": ["LayerParameter", "BlendParameter", "GlobalParameter"]})


def _header(present: list[tuple[int, bool]]) -> bytes:
    """An unversioned header for (schema index, non-zero) pairs in order."""
    frags, zeros = [], []
    index = 0
    i = 0
    while i < len(present):
        skip = present[i][0] - index
        run = [present[i]]
        while i + len(run) < len(present) and present[i + len(run)][0] == run[-1][0] + 1:
            run.append(present[i + len(run)])
        has_zero = any(not nz for _, nz in run)
        if has_zero:
            zeros += [not nz for _, nz in run]
        frags.append([skip, has_zero, len(run), False])
        index = run[-1][0] + 1
        i += len(run)
    if not frags:
        frags = [[0, False, 0, True]]
    frags[-1][3] = True
    out = b"".join(struct.pack("<H", s | (0x80 if z else 0) | (0x100 if last else 0) | (n << 9))
                   for s, z, n, last in frags)
    if zeros:
        mask = sum(1 << j for j, z in enumerate(zeros) if z)
        nbytes = 1 if len(zeros) <= 8 else 2 if len(zeros) <= 16 else 4 * ((len(zeros) + 31) // 32)
        out += mask.to_bytes(nbytes, "little")
    return out


def test_usmap_and_unversioned_properties():
    m = usmap.parse(MAPPINGS)
    assert m.resolve("MaterialInstanceConstant", 0)[0].name == "SubsurfaceProfile"
    assert m.resolve("MaterialInstanceConstant", 4)[0].name == "TextureParameterValues"
    assert m.resolve("Thing", 1)[1] == 1
    b = S.Ue4Package("/Game/M/MI_X", 426)
    tex = b.import_object("/Game/T/T_X", "Texture2D", "T_X")
    info = _header([(0, True), (1, True)]) + b.fname("Diffuse") + b"\x02"  # Index left at zero
    elem = _header([(0, True), (1, True)]) + info + struct.pack("<i", tex)
    data = _header([(1, True), (2, False), (4, True)]) + struct.pack("<i", 0) + struct.pack("<i", 1) + elem
    pkg = asset.Package("/Game/M/MI_X", asset.Versions(ue5=1013, engine=505), b.names, [], [], b"")
    r = S_reader(data, pkg)
    props = unversioned.UnversionedReader(m, True).read_object(r, "MaterialInstanceConstant")
    assert r.remaining() == 0
    assert props.get("bHasStaticPermutationResource") is False
    values = props.get("TextureParameterValues")
    assert isinstance(values, Array4) and len(values) == 1
    p = values.structs()[0]
    info = p.get("ParameterInfo")
    assert isinstance(info, Struct4) and info.props().get("Name") == "Diffuse"
    assert info.props().get("Association") == "EMaterialParameterAssociation::GlobalParameter"
    assert p.get("ParameterValue").ref == tex
    thing = _header([(1, True), (2, True), (3, True)]) + struct.pack("<i", 7) + struct.pack("<3d", 1, 2, 3) \
        + struct.pack("<i", 2) + b.fname("A") + b.fname("B")
    props = unversioned.UnversionedReader(m, True).read_object(S_reader(thing, pkg), "Thing")
    assert props.get("Pair", index=1) == 7 and props.get("Where").vector() == (1, 2, 3)
    assert props.get("Tags").names() == ["A", "B"]
    with pytest.raises(FormatError):
        unversioned.UnversionedReader(m, True).read_object(S_reader(thing, pkg), "Unknown")


def S_reader(data, pkg):
    from darkstl.importers.borderlands.formats.upk import Reader
    return Reader(data, pkg)


def test_bulk_data_map_index():
    pkg = asset.Package("/Game/X", asset.Versions(), ["None"], [], [], b"", bulk_map=[asset.Bulk(0x100, 4, 4, 8)])
    pkg.payload = lambda ext: b"........abcd" if ext == ".ubulk" else None
    from darkstl.importers.borderlands.formats.upk import Reader
    r = Reader(struct.pack("<i", 0), pkg)
    assert asset.read_bulk(r) == b"abcd"


def test_unversioned_mesh_needs_mappings(tmp_path):
    root = tmp_path / "Borderlands 4"
    paks = root / "OakGame" / "Content" / "Paks"
    paks.mkdir(parents=True)
    pk = gun_packages(426)
    Z.build_game(paks, pk, unversioned={"Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun"})
    vfs.forget(root)
    unversioned.forget_mappings()
    gf = vfs.game_files(root, files.GAME_BL4)
    pkg = gf.open_package("OakGame/Content/Gear/Weapons/AssaultRifles/Atlas/Materials/MI_Gun.uasset")
    assert pkg.unversioned_props is not None
    from darkstl.importers.borderlands.ue4.mesh import object_props
    with pytest.raises(FormatError, match="usmap"):
        object_props(pkg, pkg.export_data(pkg.main_export()), pkg.main_export())


def test_bl4_part_names_are_read_from_the_headers(tmp_path):
    root = tmp_path / "Borderlands 4"
    paks = root / "OakGame" / "Content" / "Paks"
    paks.mkdir(parents=True)
    pk = {}
    for i, part in enumerate(("Body", "Barrel_01", "Scope_01_L1")):
        stem = f"DAD_AR_{part}"
        b = S.Ue4Package(f"/Game/Gear/Weapons/AssaultRifles/DAD/Parts/{stem}", 426)
        e = b.add_export("SkeletalMesh", stem)
        b.set_data(e, S.skeletal_data(b, [0], POS_ + [i * 200, 0, 0], NRM_, UV_, IDX_, [(0, 0, 4)]))
        pk[f"Gear/Weapons/AssaultRifles/DAD/Parts/{stem}"] = b
    b = S.Ue4Package("/Game/Gear/Weapons/AssaultRifles/DAD/Parts/DAD_AR_Barrel_Data", 426)
    b.add_export("DataTable", "DAD_AR_Barrel_Data", data=b.props())
    pk["Gear/Weapons/AssaultRifles/DAD/Parts/DAD_AR_Barrel_Data"] = b
    Z.build_game(paks, pk)
    vfs.forget(root)
    found = BorderlandsImporter().scan(root, files.GAME_BL4)
    assert sorted(e.stem for e in found) == ["DAD_AR_Barrel_01", "DAD_AR_Body", "DAD_AR_Scope_01_L1"]
    assert {e.group for e in found} == {"Assault rifles / Daedalus"}
    configs = BorderlandsImporter().configurations(root, files.GAME_BL4, found)
    assert [s.id for s in configs[0].slots] == ["barrel", "sight"]
