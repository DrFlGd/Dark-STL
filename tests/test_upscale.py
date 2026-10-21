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
    model = bench.source_model(mesh, bench.normal_texture(bench.scene_bumps()))
    s = pipeline.EnhanceSettings(target_vertices=8000, smooth=0.0, upsample="ai", ai_model="x2.onnx",
                                 deblock="ai", deblock_model="x1.onnx", models_dir=tmp_path)
    r = pipeline.enhance(model, s)
    steps = r.fields[0].steps
    assert any("2x by x2.onnx" in x for x in steps) and any("deblocked by x1.onnx" in x for x in steps)
    assert r.fields[0].slopes.p.shape == (512, 512)
    assert not r.warnings
