import numpy as np
import pytest

from darkstl import bench, pipeline
from darkstl.geometry.integrate import SlopeLayer, integrate_on_mesh
from darkstl.importers.fromsoft import materials as fs_materials
from darkstl.textures import bc
from darkstl.textures import heightmap as hm


def _run(scene, compression, settings, vertices=20_000):
    return bench.run(scene, compression, bench.Method("t", settings), vertices)


@pytest.mark.parametrize("scene", ["plates", "engraving"])
def test_mesh_integration_beats_texture_integration(scene):
    old = _run(scene, "bc5", {"integration": "texture", "smooth_sampling": False})
    new = _run(scene, "bc5", {})
    assert new["error"] < old["error"] * 0.9
    assert new["detail"] < old["detail"]


def test_bc1_flat_bias_does_not_tilt_the_part():
    # BC1 cannot store a flat normal exactly; uncorrected, the bias integrates into a tilt (error ~150 % here)
    r = _run("engraving", "bc1", {})
    assert r["error"] < 75


def test_mesh_integration_is_continuous_across_the_seam():
    h = bench.scene_bumps()
    mesh = bench.panel_mesh(64)
    normal = bench.normal_texture(h)
    p, q = hm.normal_map_slopes(normal, "directx")
    got = integrate_on_mesh(mesh, {0: SlopeLayer(p, q, 0.001)}, screen_length=0.0)
    want = bench.truth_at_vertices(h, mesh)
    seam = np.isclose(mesh.vertices[:, 0], bench.N / 2000.0)
    near = np.isclose(mesh.vertices[:, 0], bench.N / 2000.0 - bench.N / 64 / 1000.0)
    # the seam column follows its neighbours as closely as the truth does
    step = np.abs((got[seam] - got[near]) - (want[seam] - want[near]))
    assert step.mean() < 0.15 * np.abs(want - want.mean()).mean()
    assert np.corrcoef(got, want)[0, 1] > 0.98


def test_uncovered_vertices_stay_flat():
    mesh = bench.panel_mesh(16)
    mesh.face_material[mesh.vertices[mesh.faces].mean(axis=1)[:, 0] > 0.128] = 1  # right half: no normal map
    p = np.full((32, 32), 0.5)
    got = integrate_on_mesh(mesh, {0: SlopeLayer(p, np.zeros_like(p), 0.001)}, screen_length=0.05)
    right = mesh.vertices[:, 0] > 0.2
    assert np.abs(got[right]).max() < 0.25 * np.abs(got).max()


def test_slope_bias_of_compressed_flat_map():
    flat = np.zeros((64, 64, 4), np.uint8)
    flat[..., :3] = (128, 128, 255)
    flat[..., 3] = 255
    p, q = hm.normal_map_slopes(bc.bc1(flat), "directx")
    bp, bq = hm.slope_bias(p, q)
    assert abs(bp) > 0.001 or abs(bq) > 0.001  # 128 is not exactly flat in 8 bits or RGB565
    assert np.abs(p - bp).max() < 1e-6 and np.abs(q - bq).max() < 1e-6


def test_bc_simulation_shapes():
    img = np.random.default_rng(0).integers(0, 255, (30, 18, 4), dtype=np.uint8)
    assert bc.bc5(img).shape == img.shape
    assert bc.bc1(img).shape == img.shape
    # BC5 keeps at most 8 levels per channel in each 4x4 block
    assert len(np.unique(bc.bc5(img)[:4, :4, 0])) <= 8


def test_bicubic_sampling():
    img = np.random.default_rng(1).normal(size=(16, 16))
    centres = np.stack(np.meshgrid((np.arange(16) + 0.5) / 16, (np.arange(16) + 0.5) / 16), -1).reshape(-1, 2)
    assert np.allclose(hm.sample_bicubic(img, centres), img.ravel())
    two = hm.sample_bicubic(np.stack([img, -img], -1), centres + 0.013)
    assert two.shape == (256, 2) and np.allclose(two[:, 0], -two[:, 1])
    ramp = np.tile(np.arange(16.0), (16, 1))
    mid = hm.sample_bicubic(ramp, np.array([[5.0 / 16, 0.5]]))  # between texels 4 and 5
    assert mid[0] == pytest.approx(4.5, abs=1e-6)


def test_guided_upsample_snaps_to_guide_edges():
    p = np.zeros((16, 16))
    p[:, 8:] = 1.0
    guide = np.zeros((64, 64))
    guide[:, 34:] = 1.0  # the albedo's edge sits slightly right of the slope edge
    up, _ = hm.guided_upsample(p, np.zeros_like(p), guide, 4)
    assert up.shape == (64, 64)
    plain, _ = hm.guided_upsample(p, np.zeros_like(p), None, 4)
    assert up[:, 33].mean() < plain[:, 33].mean()  # pulled towards the dark side of the guide


def test_albedo_detail_dark_is_deep():
    albedo = np.full((64, 64, 4), 180, np.uint8)
    albedo[30:34, :, :3] = 60  # a dark groove
    d = hm.albedo_detail(albedo)
    assert d[31, 10] < -0.5 and abs(d[5, 5]) < 0.1


def test_edge_hint_marks_mask_edges():
    mask = np.zeros((32, 32, 4), np.uint8)
    mask[:, 16:, :3] = 255
    e = hm.edge_hint(mask)
    assert e[10, 16] > 0.5 and e[10, 4] < 0.05


def test_material_roles():
    assert fs_materials.role_from_type("g_SpecularTexture") == fs_materials.ROLE_SPECULAR
    assert fs_materials.role_from_type("AMSN_MetallicMap_0") == fs_materials.ROLE_SPECULAR
    assert fs_materials.role_from_type("g_DetailBumpmapTexture") == fs_materials.ROLE_DETAIL_NORMAL
    assert fs_materials.role_from_name("wp_a_0300_r") == fs_materials.ROLE_SPECULAR


def _panel_model(with_albedo=False, detail=False, specular=False):
    h = bench.scene_bumps()
    mesh = bench.panel_mesh(32)
    normal = bench.normal_texture(h)
    albedo = None
    if with_albedo:
        albedo = np.full((256, 256, 4), 200, np.uint8)
        albedo[100:104, :, :3] = 40
    model = bench.source_model(mesh, normal, albedo)
    mat = model.materials[0]
    if detail:
        dn = bench.normal_texture(bench.scene_cloth(64))
        model.textures["panel_dn"] = bench.Texture("panel_dn", "bench", lambda: dn)
        mat.detail_normal, mat.detail_scale = "panel_dn", (4.0, 4.0)
    if specular:
        spec = np.zeros((256, 256, 4), np.uint8)
        spec[:, 128:, :3] = 255
        model.textures["panel_r"] = bench.Texture("panel_r", "bench", lambda: spec)
        mat.specular = "panel_r"
    return model


def _heights(model, **kw):
    s = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, **kw)
    r = pipeline.enhance(model, s)
    return r, r.displaced.metadata["displacement"]


def test_material_strength_scales_relief():
    model = _panel_model()
    _, base = _heights(model)
    _, double = _heights(model, material_strength={0: 2.0})
    assert np.allclose(double, 2 * base, atol=1e-7)


def test_albedo_detail_and_detail_normals_add_relief():
    model = _panel_model(with_albedo=True, detail=True)
    _, base = _heights(model)
    r, extra = _heights(model, albedo_detail_mm=0.3, detail_normals=True)
    assert not np.allclose(base, extra)
    steps = r.fields[0].steps
    assert any("albedo detail" in s for s in steps) and any("detail normal" in s for s in steps)


def test_edge_hints_and_guided_upsampling_run():
    model = _panel_model(with_albedo=True, specular=True)
    r, h = _heights(model, edge_hints=True, upsample="guided", edge_keeping=0.5)
    assert np.isfinite(h).all() and np.abs(h).max() > 0
    assert any("edge hints" in s for s in r.fields[0].steps)


def test_texture_integration_still_available():
    model = _panel_model()
    _, h = _heights(model, integration="texture", deblock="smooth")
    assert np.abs(h).max() > 0


def test_missing_ai_model_warns_and_continues(tmp_path):
    model = _panel_model()
    r, h = _heights(model, upsample="ai", ai_model="nothing.onnx", models_dir=tmp_path)
    assert any("AI upscaling skipped" in w for w in r.warnings)
    assert np.abs(h).max() > 0
