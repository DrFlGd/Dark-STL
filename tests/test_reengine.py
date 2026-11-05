"""The RE Engine importer: its format readers, a fake game folder (extracted and packed), the library,
the app's engine, previews and Collect."""

from pathlib import Path

import numpy as np
import pytest

from darkstl import library, pipeline
from darkstl.errors import FormatError
from darkstl.importers import importer_for, open_model
from darkstl.importers.reengine import REEngineImporter, files
from darkstl.importers.reengine.catalog import classify
from darkstl.importers.reengine.formats import mesh as meshfmt
from darkstl.importers.reengine.formats import pak as pakfmt
from darkstl.importers.reengine.formats import tex as texfmt
from darkstl.importers.reengine.games import detect_game
from darkstl.model import MeshOptions

import synth_re as S
from test_app import home, make_service, wait  # noqa: F401 - fixture
from test_heightmap import encode_normals
from test_pipeline import bump_height

RED, GREEN = 0xF800, 0x07E0
CH = "natives/stm/_chainsaw/character/ch/cha0/cha000_00"  # the RE4 test character's folder
STREAM_CH = "natives/stm/streaming/_chainsaw/character/ch/cha0/cha000_00"
PL0000 = "natives/x64/sectionroot/character/player/pl0000/pl0000.mesh.1808312334"  # the RE2 test character


def _normal_tex(version: int) -> bytes:
    img = encode_normals(bump_height(), "opengl")  # (H, W, 4) uint8
    h, w = img.shape[:2]
    return S.build_tex(w, h, 0x1C, [S.rgba_surface(img)], version)


def _tex(width: int, color: int, version: int, mips: int = 1, pad_rows: int = 0) -> bytes:
    surfaces = [S.bc1_flat(max(1, width >> i), max(1, width >> i), color) for i in range(mips)]
    return S.build_tex(width, width, 0x47, surfaces, version, pad_rows=pad_rows)


def write_re4_game(root: Path) -> Path:
    """A Resident Evil 4 install as extracted by a pak tool: a character whose best detail is streamed and
    whose big textures sit under streaming/, and a weapon."""
    root.mkdir(parents=True)
    (root / "re4.exe").write_bytes(b"MZ")
    stm = root / "natives" / "stm"
    ch = stm / "_chainsaw" / "character" / "ch" / "cha0" / "cha000_00"
    ch.mkdir(parents=True)
    subs = [S.quad(1.0, 0.0, 0, 0, segments=4), S.quad(0.5, 0.2, 1, 1, streamed=True, segments=1)]
    mesh, stream = S.build_mesh(subs, ["cha000_00_body", "cha000_00_eye"], S.RE4_MESH, bones=["root", "hips"])
    (ch / "cha000_00.mesh.221108797").write_bytes(mesh)
    streaming = stm / "streaming" / "_chainsaw" / "character" / "ch" / "cha0" / "cha000_00"
    streaming.mkdir(parents=True)
    (streaming / "cha000_00.mesh.221108797").write_bytes(stream)
    (ch / "cha000_00.mdf2.32").write_bytes(S.build_mdf([
        ("cha000_00_body", "Shader/Character/ch_std.mmtr", [
            ("ALBD", "_Chainsaw/Character/ch/cha0/cha000_00/cha000_00_body_ALBD.tex"),
            ("NRMR", "_Chainsaw/Character/ch/cha0/cha000_00/cha000_00_body_NRMR.tex"),
            ("ATOS", "_Chainsaw/Character/ch/cha0/cha000_00/cha000_00_body_ATOS.tex")]),
        ("cha000_00_eye", "Shader/Character/eye.mmtr",
         [("ALBD", "_Chainsaw/Character/ch/cha0/cha000_00/cha000_00_eye_ALBD.tex")]),
    ], S.RE4_MDF))
    # small copies beside the mesh, full ones under streaming/ (as the game keeps them)
    (ch / "cha000_00_body_ALBD.tex.143221013").write_bytes(_tex(4, RED, S.RE4_TEX))
    (streaming / "cha000_00_body_ALBD.tex.143221013").write_bytes(_tex(16, RED, S.RE4_TEX, mips=3, pad_rows=32))
    (ch / "cha000_00_body_NRMR.tex.143221013").write_bytes(_normal_tex(S.RE4_TEX))
    (ch / "cha000_00_body_ATOS.tex.143221013").write_bytes(_tex(4, GREEN, S.RE4_TEX))
    (ch / "cha000_00_eye_ALBD.tex.143221013").write_bytes(_tex(4, GREEN, S.RE4_TEX))
    wp = stm / "_chainsaw" / "weapon" / "wp" / "wp1000" / "wp1000_00"
    wp.mkdir(parents=True)
    knife, _ = S.build_mesh([S.quad(0.3, 0.0, 0, 0)], ["wp1000_00_mat"], S.RE4_MESH)
    (wp / "wp1000_00.mesh.221108797").write_bytes(knife)
    (wp / "wp1000_00.mdf2.32").write_bytes(S.build_mdf([("wp1000_00_mat", "s.mmtr", [
        ("ALBD", "_Chainsaw/Weapon/wp/wp1000/wp1000_00/wp1000_00_ALBD.tex")])], S.RE4_MDF))
    (wp / "wp1000_00_ALBD.tex.143221013").write_bytes(_tex(4, RED, S.RE4_TEX))
    return root


@pytest.fixture
def re4_dir(tmp_path):
    return write_re4_game(tmp_path / "RE4")


@pytest.fixture
def re2_pak_dir(tmp_path):
    """Resident Evil 2 still packed: one pak and the community file list beside it."""
    root = tmp_path / "RE2"
    root.mkdir()
    (root / "re2.exe").write_bytes(b"MZ")
    mesh, _ = S.build_mesh([S.quad(1.0, 0.0, 0, 0, segments=2)], ["pl0000_body"], S.RE2_MESH)
    mdf = S.build_mdf([("pl0000_body", "Shader/pl.mmtr", [
        ("BaseMetalMap", "SectionRoot/Character/Player/pl0000/pl0000_body_albm.tex"),
        ("NormalRoughnessMap", "SectionRoot/Character/Player/pl0000/pl0000_body_nrmr.tex")])], S.RE2_MDF)
    names = ["natives/x64/sectionroot/character/player/pl0000/pl0000.mesh.1808312334",
             "natives/x64/sectionroot/character/player/pl0000/pl0000.mdf2.10",
             "natives/x64/sectionroot/character/player/pl0000/pl0000_body_albm.tex.10",
             "natives/x64/sectionroot/character/player/pl0000/pl0000_body_nrmr.tex.10",
             "natives/x64/sectionroot/character/enemy/em0000/em0000.mesh.1808312334",
             "natives/x64/sectionroot/environment/stage/st1/st1_wall.mesh.1808312334"]
    enemy, _ = S.build_mesh([S.quad(2.0, 0.0, 0, 0)], ["em0000_mat"], S.RE2_MESH)
    wall, _ = S.build_mesh([S.quad(5.0, 0.0, 0, 0)], ["wall"], S.RE2_MESH)
    (root / "re_chunk_000.pak").write_bytes(S.build_pak(
        list(zip(names, [mesh, mdf, _tex(4, RED, S.RE2_TEX), _normal_tex(S.RE2_TEX), enemy, wall])),
        pakfmt.COMPRESSION_DEFLATE))
    listed = "\n".join(n.replace("/", "\\") for n in names)  # lists come with either slash
    (root / "re2.list").write_text(f"# RE2 file list\n{listed}\nnatives/x64/missing.tex.10\n")
    return root


# ---------------------------------------------------------------------------
# formats


def test_mesh_versions_map_to_eras_and_games():
    assert meshfmt.era_of(1808312334) == meshfmt.ERA_RE2 and meshfmt.game_of(221108797) == "Resident Evil 4"
    assert meshfmt.era_of(221108800) == meshfmt.ERA_RE4  # an unknown version takes the nearest
    assert meshfmt.game_of(1) is None


def test_mesh_round_trip_each_era():
    subs = [S.quad(1.0, 0.0, 0, 0, segments=2), S.quad(0.5, 0.2, 1, 1)]
    for version in (S.RE2_MESH, S.RE8_MESH, S.RE4_MESH):
        data, _ = S.build_mesh(subs, ["a", "b"], version, bones=["root"])
        mesh = meshfmt.read_mesh(data)
        assert mesh.version == version and mesh.material_names == ["a", "b"]
        assert [b.name for b in mesh.bones] == ["root"]
        out = meshfmt.submeshes(mesh)
        shape = [(s.group, s.material_index, len(s.positions), len(s.triangles)) for s in out]
        assert shape == [(0, 0, 9, 8), (1, 1, 4, 2)]
        assert np.allclose(out[0].positions, subs[0].positions)
        assert np.allclose(out[0].uvs, subs[0].uvs, atol=2e-3) and np.allclose(out[0].normals, [0, 0, 1], atol=0.01)


def test_streamed_buffers_are_read_from_the_second_file_or_left_out():
    subs = [S.quad(1.0, 0.0, 0, 0), S.quad(0.5, 0.2, 1, 1, streamed=True)]
    data, stream = S.build_mesh(subs, ["a", "b"], S.RE4_MESH)
    whole = meshfmt.read_mesh(data, streaming=stream)
    assert whole.streamed_count == 1 and whole.streamed_found and len(meshfmt.submeshes(whole)) == 2
    half = meshfmt.read_mesh(data)
    assert not half.streamed_found and half.best_lod() == 0 and not half.complete(0)
    assert [s.material_index for s in meshfmt.submeshes(half)] == [0]


def test_mesh_rejects_other_files():
    with pytest.raises(FormatError):
        meshfmt.read_mesh(b"MPLY" + b"\0" * 64)
    with pytest.raises(FormatError):
        meshfmt.read_mesh(b"DDS " + b"\0" * 64)


def test_tex_mips_padded_rows_and_versions():
    for version in (S.RE2_TEX, 190820018, S.RE8_TEX, S.RE4_TEX):
        tex = texfmt.read_tex(_tex(8, RED, version, mips=3, pad_rows=16))
        assert (tex.version, tex.width, tex.mip_count, tex.codec) == (version, 8, 3, "BC1")
        assert tex.decode()[0, 0].tolist() == [255, 0, 0, 255] and tex.decode(2).shape == (2, 2, 4)
        assert tex.describe() == "8 x 8, BC1, 3 mips"
    dds_bytes = tex.to_dds()
    from darkstl.textures import dds

    assert dds.parse_dds(dds_bytes).mipmaps == 3
    with pytest.raises(FormatError, match="GDeflate"):
        texfmt.read_tex(S.build_tex(4, 4, 0x47, [S.bc1_flat(4, 4, RED)], 241106027))


def test_pak_table_names_and_compression(tmp_path):
    files_ = [("natives/stm/a/B.mesh.221108797", b"x" * 100), ("natives/stm/a/c.tex.143221013", bytes(range(256)) * 4)]
    for compression in (pakfmt.COMPRESSION_NONE, pakfmt.COMPRESSION_DEFLATE, pakfmt.COMPRESSION_ZSTD):
        p = tmp_path / f"c{compression}.pak"
        p.write_bytes(S.build_pak(files_, compression))
        pak = pakfmt.read_pak(p)
        assert len(pak.entries) == 2 and not pak.encrypted
        assert pak.read(pak.find("natives\\stm\\a\\b.mesh.221108797")) == b"x" * 100  # names are case-insensitive
        assert pak.read(pak.find("natives/stm/a/C.tex.143221013")) == bytes(range(256)) * 4
        assert pak.find("natives/stm/a/other") is None
    (tmp_path / "v2.pak").write_bytes(S.build_pak(files_[:1], version=2))
    v2 = pakfmt.read_pak(tmp_path / "v2.pak")
    assert v2.version == (2, 0) and v2.read(v2.entries[0]) == b"x" * 100
    (tmp_path / "names.list").write_text("natives/stm/a/B.mesh.221108797\n#c\nnatives/stm/a/c.tex.143221013\n")
    assert pak.name_entries(pakfmt.read_name_list(tmp_path / "names.list")) == 2
    assert sorted(e.name for e in pak.entries) == sorted(n for n, _ in files_)


def test_pak_hash_matches_the_reference_murmur3():
    def murmur3(data: bytes, seed: int) -> int:  # the textbook x86_32 variant
        c1, c2, h = 0xCC9E2D51, 0x1B873593, seed
        for i in range(0, len(data) & ~3, 4):
            k = int.from_bytes(data[i:i + 4], "little")
            k = (k * c1) & 0xFFFFFFFF
            k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
            k = (k * c2) & 0xFFFFFFFF
            h ^= k
            h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
            h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
        tail = data[len(data) & ~3:]
        k = 0
        for i, b in enumerate(tail):
            k ^= b << (8 * i)
        if tail:
            k = (k * c1) & 0xFFFFFFFF
            k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
            k = (k * c2) & 0xFFFFFFFF
            h ^= k
        h ^= len(data)
        h ^= h >> 16
        h = (h * 0x85EBCA6B) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 0xC2B2AE35) & 0xFFFFFFFF
        return h ^ (h >> 16)

    names = ["natives/stm/_chainsaw/character/ch/cha0/cha000_00/cha000_00.mesh.221108797", "a", "ab", "x/Ä.tex"]
    for name, h in zip(names, pakfmt.hash_names(names)):
        lo = murmur3(name.lower().encode("utf-16-le"), 0xFFFFFFFF) if name.isascii() else None
        hi = murmur3(name.upper().encode("utf-16-le"), 0xFFFFFFFF) if name.isascii() else None
        if lo is not None:
            assert int(h) == (hi << 32) | lo
    assert len(set(pakfmt.hash_names(names).tolist())) == len(names)


def test_classification_by_path():
    def kind(rel):
        return classify(rel, rel.rsplit("/", 1)[-1].split(".")[0])[:2]

    assert kind("natives/stm/_chainsaw/character/ch/cha0/cha000_00/cha000_00.mesh.221108797") == ("Characters", "cha0")
    assert kind("natives/x64/sectionroot/character/player/pl0000/pl0000.mesh.1808312334") == ("Characters", "Player")
    assert kind("natives/x64/sectionroot/character/enemy/em0000/em0000.mesh.1808312334") == ("Characters", "Enemies")
    assert kind("natives/stm/_chainsaw/weapon/wp/wp1000/wp1000_00/wp1000_00.mesh.221108797") == ("Weapons", "wp1000")
    assert kind("natives/x64/sectionroot/environment/stage/st1/st1_wall.mesh.1808312334") == ("Map Pieces", "stage")
    assert classify("natives/x64/objectroot/prefab/item/it0001/it0001.mesh.1808312334", "it0001")[0] == "Objects"
    assert classify("natives/x64/gui/icons/x.mesh.1808312334", "x")[0] == "Other"
    assert classify("loose/thing.mesh.1808312334", "thing") == ("Models", "loose", "thing", False)


# ---------------------------------------------------------------------------
# an extracted game


def test_game_is_recognised_by_exe_name_or_file_versions(re4_dir, tmp_path):
    assert detect_game(re4_dir) == "Resident Evil 4"
    assert detect_game(re4_dir / "natives" / "stm") == "Resident Evil 4"  # the exe is a few folders up
    loose = tmp_path / "stuff"
    loose.mkdir()
    (loose / "x.mesh.1808312334").write_bytes(b"")
    assert detect_game(loose) == "Resident Evil 2"
    assert detect_game(tmp_path / "Games" / "Resident Evil Village" / "extract") == "Resident Evil Village"
    (tmp_path / "empty").mkdir()
    assert detect_game(tmp_path / "empty") is None


def test_library_lists_the_extracted_game(re4_dir):
    entries, notes = library.scan_folder(re4_dir)
    assert notes == []
    assert [(e.game, e.category, e.group, e.stem, e.importer) for e in entries] == [
        ("Resident Evil 4", "Weapons", "wp1000", "wp1000_00", "reengine"),
        ("Resident Evil 4", "Characters", "cha0", "cha000_00", "reengine")]
    assert not any("streaming" in str(e.path) for e in entries)


def test_character_opens_with_streamed_detail_materials_and_textures(re4_dir):
    path = re4_dir / CH / "cha000_00.mesh.221108797"
    assert importer_for(path).key == "reengine"
    model = open_model(path)
    assert model.importer == "reengine" and model.game == "Resident Evil 4" and model.notes == []
    assert [p.name for p in model.parts] == ["Group 0: cha000_00_body", "Group 1: cha000_00_eye"]
    body, eye = model.materials
    assert (body.albedo, body.normal) == ("cha000_00_body_albd", "cha000_00_body_nrmr")
    assert body.specular == "cha000_00_body_atos"
    assert body.normal_green == "opengl" and eye.albedo == "cha000_00_eye_albd"
    # the full-size copy under streaming/ is the one shown
    albedo = model.textures["cha000_00_body_albd"]
    assert albedo.describe() == "16 x 16, BC1, 3 mips" and "streaming" in albedo.source
    assert albedo.decode()[3, 7].tolist() == [255, 0, 0, 255]
    assert model.textures["cha000_00_body_nrmr"].decode().shape[2] == 4
    mesh = model.build_mesh()
    assert mesh.n_faces == 32 + 2  # both parts, the streamed one included
    # Y up became Z up: the upright square now stands along Z, facing -Y
    lo, hi = mesh.bounds()
    assert hi[2] == pytest.approx(1.0) and lo[1] == pytest.approx(-0.2) and hi[0] == pytest.approx(1.0)
    assert mesh.corner_uvs.min() >= 0 and mesh.corner_uvs.max() <= 1
    assert model.build_mesh(MeshOptions(parts={1})).n_faces == 2
    assert any("RE Engine mesh version 221108797 (Resident Evil 4)" in line for line in model.info)


def test_without_the_streamed_file_the_model_still_opens(re4_dir):
    streaming = re4_dir / STREAM_CH
    (streaming / "cha000_00.mesh.221108797").unlink()
    path = re4_dir / CH / "cha000_00.mesh.221108797"
    model = open_model(path)
    assert any("streaming" in n for n in model.notes)
    assert [p.name for p in model.parts] == ["Group 0: cha000_00_body"] and model.build_mesh().n_faces == 32


def test_enhance_and_preview_an_re_model(re4_dir):
    from darkstl import thumbnails

    path = re4_dir / "natives/stm/_chainsaw/weapon/wp/wp1000/wp1000_00/wp1000_00.mesh.221108797"
    model = open_model(path)
    result = pipeline.enhance(model, pipeline.EnhanceSettings(target_vertices=400, method="subdivide"))
    assert result.displaced.n_vertices >= 300 and result.displaced.n_faces > 0
    img = thumbnails.render(model.build_mesh(), size=64)
    assert img.shape == (64, 64, 4) and img[..., 3].max() > 0


def test_describe_re_files(re4_dir):
    imp = REEngineImporter()
    ch = re4_dir / CH
    assert imp.describe(ch / "cha000_00.mesh.221108797")[0].startswith("RE Engine mesh version 221108797")
    assert "4 x 4, BC1" in imp.describe(ch / "cha000_00_body_ALBD.tex.143221013")[0]
    lines = imp.describe(ch / "cha000_00.mdf2.32")
    assert lines[0] == "RE Engine materials version 32, 2 materials" and any("NRMR:" in ln for ln in lines)


def test_related_files_take_the_streamed_half_and_both_texture_copies(re4_dir):
    ch = re4_dir / CH
    related = REEngineImporter().related_files(ch / "cha000_00.mesh.221108797")
    names = sorted((f.path.relative_to(re4_dir).as_posix(), f.kind) for f in related)
    assert (f"{STREAM_CH}/cha000_00.mesh.221108797", "model") in names
    assert (f"{CH}/cha000_00.mdf2.32", "texture") in names
    assert (f"{STREAM_CH}/cha000_00_body_ALBD.tex.143221013", "texture") in names
    assert (f"{CH}/cha000_00_body_ALBD.tex.143221013", "texture") in names
    assert len(names) == 8


# ---------------------------------------------------------------------------
# a packed game


def test_pak_entries_are_listed_and_opened_through_the_file_list(re2_pak_dir):
    entries, notes = library.scan_folder(re2_pak_dir)
    assert notes == []
    assert [(e.category, e.group, e.stem, e.packed) for e in entries] == [
        ("Characters", "Enemies", "em0000", True), ("Characters", "Player", "pl0000", True),
        ("Map Pieces", "stage", "st1_wall", True)]
    player = next(e for e in entries if e.stem == "pl0000")
    assert player.path == re2_pak_dir / "re_chunk_000.pak" / PL0000
    model = open_model(player.path)
    assert model.game == "Resident Evil 2" and model.notes == []
    mat = model.materials[0]
    assert (mat.albedo, mat.normal) == ("pl0000_body_albm", "pl0000_body_nrmr")
    assert model.textures["pl0000_body_albm"].decode()[0, 0].tolist() == [255, 0, 0, 255]
    assert model.build_mesh().n_faces == 8
    related = REEngineImporter().related_files(player.path)
    assert all(f.extract is not None and f.save_as.startswith("natives/x64/") for f in related)
    assert len(related) == 4 and related[0].extract()[:4] == b"MESH"


def test_a_pak_without_a_file_list_says_so(re2_pak_dir):
    (re2_pak_dir / "re2.list").unlink()
    files.forget(re2_pak_dir)
    entries, notes = library.scan_folder(re2_pak_dir)
    assert entries == [] and len(notes) == 1 and "file list" in notes[0]


def test_an_encrypted_pak_says_so(tmp_path):
    import struct

    (tmp_path / "re_chunk_000.pak").write_bytes(struct.pack("<4sBBHII", b"KPKA", 4, 2, 8, 10, 0) + b"\0" * 64)
    entries, notes = library.scan_folder(tmp_path, "Resident Evil 4")
    assert entries == [] and len(notes) == 1 and "encrypted" in notes[0]


# ---------------------------------------------------------------------------
# through the app's engine


def test_the_engine_adds_opens_and_collects_an_re_game(home, re4_dir, re2_pak_dir):  # noqa: F811
    s = make_service(home)
    check = s.call("folder_check", {"path": str(re4_dir)})
    assert check["game"] == "Resident Evil 4"
    r = wait(s, s.call("folder_add", {"path": str(re4_dir)})["job"])
    assert r["count"] == 2 and r["notes"] == []
    r = wait(s, s.call("folder_add", {"path": str(re2_pak_dir)})["job"])
    assert r["count"] == 3 and r["game"] == "Resident Evil 2"
    ov = s.call("overview", {})
    assert [g["name"] for g in ov["games"]] == ["Resident Evil 4", "Resident Evil 2"]
    items = s.call("models_query", {"game": "Resident Evil 4"})["items"]
    assert [i["stem"] for i in items] == ["wp1000_00", "cha000_00"]
    opened = s.call("model_open", {"id": items[1]["id"]})
    assert opened["game"] == "Resident Evil 4" and len(opened["materials"]) == 2 and opened["stats"]["triangles"] == 34
    png = s.call("texture", {"sid": opened["sid"], "key": "cha000_00_body_albd", "max": "64"})
    assert png[:4] == b"\x89PNG"
    assert "16 x 16" in s.call("texture_info", {"sid": opened["sid"], "key": "cha000_00_body_albd"})["info"]
    assert s.call("thumb", {"id": items[1]["id"]})[:4] == b"\x89PNG"
    pak_items = s.call("models_query", {"game": "Resident Evil 2"})["items"]
    player = next(i for i in pak_items if i["stem"] == "pl0000")
    opened = s.call("model_open", {"id": player["id"]})
    assert opened["game"] == "Resident Evil 2" and opened["stats"]["triangles"] == 8
    plan = s.call("collect_plan", {"ids": [player["id"]]})
    assert plan["files"] == 4 and plan["models"] == 1
    done = wait(s, s.call("collect_run", {"ids": [player["id"]]})["job"])
    assert done["copied"] == 4
    folder = Path(plan["folder"])
    assert (folder / PL0000).read_bytes()[:4] == b"MESH"
    assert (folder / "natives/x64/sectionroot/character/player/pl0000/pl0000_body_albm.tex.10").is_file()
    # the collected copy opens on its own, with its textures
    copy = open_model(folder / "natives/x64/sectionroot/character/player/pl0000/pl0000.mesh.1808312334")
    assert copy.materials[0].albedo == "pl0000_body_albm" and copy.build_mesh().n_faces == 8
