"""Files as Bloodborne ships them: PS4 textures in the archive, project paths inside it, and armour
extracted on its own (only the low-detail ``_l`` copy). Built byte by byte like the other tests."""

import numpy as np
import pytest

from darkstl import library, pipeline
from darkstl.importers import open_model
from darkstl.importers.fromsoft.formats import dcx, ps4
from darkstl.importers.fromsoft.games import game_from_archive_path

import synth
from test_heightmap import encode_normals
from test_pipeline import bump_height

ROOT = "N:\\SPRJ\\data\\INTERROOT_ps4\\parts\\FullBody\\AM_F_9300\\"


def armour_piece(stem: str, normal: np.ndarray) -> bytes:
    """A 1 m plane whose normal map sits tiled in a PS4 TPF, packed the way Bloodborne packs parts."""
    pos, nrm, uv, idx = synth.grid_plane(4, size=1.0)
    mats = [("arm", "N:\\SPRJ\\data\\Material\\mtd\\P[ARSN].mtd",
             [(f"N:\\SPRJ\\data\\Model\\parts\\FullBody\\AM_F_9300\\tex\\{stem}_n.tga", "g_BumpmapTexture")])]
    flver_bytes = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], mats, uv_float=True)
    h, w = normal.shape[:2]
    tiled = ps4.tile(normal.tobytes(), w, h, 4)
    tpf_bytes = synth.build_tpf_ps4(f"{stem}_n", 105, w, h, tiled, 28)  # R8G8B8A8
    bnd = synth.build_bnd4([(100, f"{ROOT}{stem.upper()}.tpf", tpf_bytes),
                            (200, f"{ROOT}{stem.upper()}.flver", flver_bytes)])
    return dcx.compress_dflt(bnd)


@pytest.fixture
def normal():
    return encode_normals(bump_height(64), "directx")


def test_loose_ps4_armour_piece(tmp_path, normal):
    folder = tmp_path / "extracted"
    folder.mkdir()
    path = folder / "am_f_9300_l.partsbnd.dcx"
    path.write_bytes(armour_piece("am_f_9300_l", normal))

    # nothing in the folder's name says which game: the paths inside the archive do
    assert library.detect_game(folder) == "Bloodborne"
    lib = library.Library()
    (entry,) = lib.add_root(folder)
    assert entry.game == "Bloodborne" and entry.is_lod
    # the low-detail copy is all there is, so it is listed
    assert not lib.hides(entry)
    assert lib.query("Bloodborne") == [entry]
    assert lib.overview()[0]["count"] == 1

    model = open_model(path)
    assert model.game == "Bloodborne"
    key = model.materials[0].normal
    assert np.array_equal(model.textures[key].decode()[..., :3], normal[..., :3])  # untiled exactly
    assert model.materials[0].normal_green == "directx"

    result = pipeline.enhance(model, pipeline.EnhanceSettings(target_vertices=2000))
    (field,) = result.fields.values()
    assert field.field.convention == "directx" and field.field.confidence == 0  # the game's, not a guess
    assert not any("unclear" in w for w in result.warnings)
    assert result.stats()["max_displacement_mm"] > 0


def test_low_detail_copy_hidden_beside_its_full_model(tmp_path, normal):
    folder = tmp_path / "parts"
    folder.mkdir()
    for stem in ("am_f_9300", "am_f_9300_l"):
        (folder / f"{stem}.partsbnd.dcx").write_bytes(armour_piece(stem, normal))
    lib = library.Library()
    lib.add_root(folder)
    assert [e.stem.lower() for e in lib.query()] == ["am_f_9300"]
    assert len(lib.query(include_lod=True)) == 2
    assert lib.overview()[0]["count"] == 1


@pytest.mark.parametrize("name, game", [
    ("N:\\SPRJ\\data\\INTERROOT_ps4\\chr\\c1120\\c1120.flver", "Bloodborne"),
    ("N:\\FRPG\\data\\INTERROOT_x64\\parts\\WP_A_0402\\WP_A_0402.flver", "Dark Souls Remastered"),
    ("N:\\FRPG\\data\\INTERROOT_win32\\chr\\c2500\\c2500.flver", "Dark Souls"),
    ("N:\\FDP\\data\\INTERROOT_win64\\parts\\WP_A_0300\\WP_A_0300.flver", "Dark Souls III"),
    ("N:\\NTC\\data\\Target\\INTERROOT_win64\\chr\\c1000\\c1000.flver", "Sekiro"),
    ("N:\\GR\\data\\INTERROOT_win64\\asset\\aeg\\aeg099\\aeg099_001.flver", "Elden Ring"),
    ("c1100.flver", None),
    (None, None),
])
def test_game_from_archive_path(name, game):
    assert game_from_archive_path(name) == game
