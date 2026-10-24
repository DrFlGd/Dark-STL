import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from onnx import TensorProto, helper

from darkstl import bench, pipeline
from darkstl.textures import upscale


def _resize_model(path, scale):
    """A stand-in for an ESRGAN model: nearest-neighbour resize by ``scale``."""
    x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, None, None])
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, None, None])
    scales = helper.make_tensor("scales", TensorProto.FLOAT, [4], [1.0, 1.0, float(scale), float(scale)])
    node = helper.make_node("Resize", ["input", "", "scales"], ["output"], mode="nearest")
    graph = helper.make_graph([node], "up", [x], [y], initializer=[scales])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))
    return path


def test_load_detects_scale_and_tiles_seamlessly(tmp_path):
    m = upscale.load(str(_resize_model(tmp_path / "x4.onnx", 4)))
    assert m.scale == 4
    img = np.random.default_rng(0).random((100, 70, 3)).astype(np.float32)
    out = upscale.run(m, img, tile=48, overlap=8)
    assert out.shape == (400, 280, 3)
    assert np.allclose(out[::4, ::4], img, atol=1e-5)


def test_normal_map_output_is_unit_length(tmp_path):
    m = upscale.load(str(_resize_model(tmp_path / "x2.onnx", 2)))
    rgba = bench.normal_texture(bench.scene_bumps(64))
    out = upscale.normal_map(m, rgba)
    assert out.shape == (128, 128, 4)
    n = out[..., :3].astype(np.float64) / 255 * 2 - 1
    assert np.allclose(np.linalg.norm(n, axis=-1), 1.0, atol=0.02)
    assert np.allclose(out[::2, ::2, :2], rgba[..., :2], atol=1)


def test_pipeline_ai_upscale_and_deblock(tmp_path):
    _resize_model(tmp_path / "x2.onnx", 2)
    _resize_model(tmp_path / "x1.onnx", 1)
    mesh = bench.panel_mesh(32)
    model = bench.source_model(mesh, bench.normal_texture(bench.scene_bumps(32)))
    s = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="ai", ai_model="x2.onnx",
                                 deblock="ai", deblock_model="x1.onnx", models_dir=tmp_path)
    r = pipeline.enhance(model, s)
    steps = r.fields[0].steps
    assert any("2x by x2.onnx" in x for x in steps) and any("deblocked by x1.onnx" in x for x in steps)
    assert r.fields[0].slopes.p.shape == (64, 64)
    assert not r.warnings


def test_ai_upscaling_skipped_when_texels_are_already_finer(tmp_path):
    _resize_model(tmp_path / "x2.onnx", 2)
    model = bench.source_model(bench.panel_mesh(32), bench.normal_texture(bench.scene_bumps()))
    s = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="ai", ai_model="x2.onnx", models_dir=tmp_path)
    r = pipeline.enhance(model, s)
    assert any("no AI upscaling needed" in x for x in r.fields[0].steps)
    assert r.fields[0].slopes.p.shape == (256, 256)


def _shifting_model(path, scale, shift):
    """An upscaler that also tilts everything a little, as real ones do: resize, then add ``shift``."""
    x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, None, None])
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, None, None])
    scales = helper.make_tensor("scales", TensorProto.FLOAT, [4], [1.0, 1.0, float(scale), float(scale)])
    add = helper.make_tensor("shift", TensorProto.FLOAT, [1, 3, 1, 1], [shift, shift, 0.0])
    nodes = [helper.make_node("Resize", ["input", "", "scales"], ["r"], mode="nearest"),
             helper.make_node("Add", ["r", "shift"], ["output"])]
    graph = helper.make_graph(nodes, "up", [x], [y], initializer=[scales, add])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))
    return path


def test_ai_upscaling_keeps_the_textures_broad_shape(tmp_path):
    _shifting_model(tmp_path / "tilt.onnx", 2, 0.03)
    model = bench.source_model(bench.panel_mesh(32), bench.normal_texture(bench.scene_bumps(32)))
    base = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="guided", models_dir=tmp_path)
    ai = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="ai", ai_model="tilt.onnx", models_dir=tmp_path)
    p_ref = pipeline.enhance(model, base).fields[0].slopes.p
    p_ai = pipeline.enhance(model, ai).fields[0].slopes.p
    assert p_ai.shape == p_ref.shape == (64, 64)
    # a 0.03 shift in the texture is a slope of about 0.06; none of it survives
    assert abs(float(p_ai.mean() - p_ref.mean())) < 0.005


def test_a_map_made_ready_gives_the_same_displacement_as_making_it_inline():
    from darkstl.geometry.subdivide import edge_length_for_vertex_count

    model = bench.source_model(bench.panel_mesh(32), bench.normal_texture(bench.scene_bumps(32)))
    s = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="guided", upsample_factor=2)
    inline = pipeline.enhance(model, s)
    source = model.build_mesh(s.mesh_options)
    (dm,) = pipeline.detail_maps(model, source, model.materials, s)
    spacing = edge_length_for_vertex_count(source.surface_area(), 8000)
    img = model.textures[dm.key].decode()
    up = pipeline.upscale_normal_map(model, model.materials[dm.materials[0]], img, dm.all_uv, dm.green, dm.texel,
                                     spacing, s, dm.key, dm.codec)
    assert up.factor == 2 and up.p.shape == (64, 64) and up.normal_map().shape == (64, 64, 4)
    assert up.normal_map(False, (0, 8, 0, 8)).shape == (8, 8, 4)
    # drawn back as a normal map, the texture's own slopes give the texture back (its slope bias aside)
    back = up.normal_map(upscaled=False)
    assert np.abs(back[..., :2].astype(int) - img[..., :2].astype(int)).mean() < 3
    ready = pipeline.enhance(model, s, prepared={dm.key: up})
    assert np.allclose(ready.displaced.vertices, inline.displaced.vertices)
    assert ready.fields[0].steps == inline.fields[0].steps and any("2x" in x for x in ready.fields[0].steps)
    # the factor asked for wins over what the spacing needs
    four = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="guided", upsample_factor=4)
    assert pipeline.plan_upscale(four, dm.texel, spacing, dm.codec, 32, 32) == (4, None)
    assert pipeline.plan_upscale(pipeline.EnhanceSettings(upsample="off"), dm.texel, spacing, dm.codec, 32, 32) == (1, None)


def test_bundled_model_is_picked_by_compression(tmp_path, monkeypatch):
    import json

    for name in ("up-bc1", "up-any"):
        _resize_model(tmp_path / f"{name}.onnx", 2)
    (tmp_path / "models.json").write_text(json.dumps({"models": [
        {"name": "up-bc1", "codecs": ["BC1", "BC2", "BC3"]}, {"name": "up-any", "codecs": ["*"]},
        {"name": "missing", "codecs": ["BC5"]}]}))
    monkeypatch.setenv("DARKSTL_BUNDLED_MODELS", str(tmp_path))
    upscale._bundled_spec.cache_clear()
    assert [m["name"] for m in upscale.bundled_models()] == ["up-bc1", "up-any"]
    assert upscale.automatic_model("BC1").name == "up-bc1.onnx"
    assert upscale.automatic_model("BC7").name == "up-any.onnx"
    assert upscale.automatic_model(None).name == "up-any.onnx"

    model = bench.source_model(bench.panel_mesh(32), bench.normal_texture(bench.scene_bumps(32)))
    r = pipeline.enhance(model, pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="ai"))
    assert any("2x by up-any.onnx (included" in x for x in r.fields[0].steps)
    assert not r.warnings
