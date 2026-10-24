"""The included upscalers: desktop/models/bundle.py reads ESRGAN .pth files without PyTorch and
rebuilds them as ONNX. Checked on a tiny network saved the way torch.save's legacy format does."""

import json
import pickle
import struct
import sys
import types
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")

MODELS = Path(__file__).resolve().parent.parent / "desktop" / "models"
sys.path.insert(0, str(MODELS))
import bundle  # noqa: E402


def tiny_esrgan(nf=4, gc=2, blocks=2, seed=0):
    rng = np.random.default_rng(seed)
    sd = OrderedDict()

    def conv(key, cin, cout):
        sd[f"{key}.weight"] = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float32)
        sd[f"{key}.bias"] = (rng.standard_normal(cout) * 0.05).astype(np.float32)

    conv("model.0", 3, nf)
    for b in range(blocks):
        for r in (1, 2, 3):
            for i in range(1, 6):
                conv(f"model.1.sub.{b}.RDB{r}.conv{i}.0", nf + (i - 1) * gc, gc if i < 5 else nf)
    conv(f"model.1.sub.{blocks}", nf, nf)
    for key in ("model.3", "model.6", "model.8"):
        conv(key, nf, nf)
    conv("model.10", nf, 3)
    return sd


def save_legacy(sd, path, monkeypatch):
    """Write ``sd`` as torch.save did before 1.6, with stand-ins for torch's classes."""
    torch = types.ModuleType("torch")
    utils = types.ModuleType("torch._utils")

    class FloatStorage:
        pass

    FloatStorage.__module__, FloatStorage.__qualname__ = "torch", "FloatStorage"

    def _rebuild_tensor_v2(*args):
        raise AssertionError("not called when writing")

    _rebuild_tensor_v2.__module__, _rebuild_tensor_v2.__qualname__ = "torch._utils", "_rebuild_tensor_v2"
    torch.FloatStorage, utils._rebuild_tensor_v2 = FloatStorage, _rebuild_tensor_v2
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch._utils", utils)

    class Storage:
        def __init__(self, key, data):
            self.key, self.data = key, data

    class Tensor:
        def __init__(self, storage, shape):
            self.storage, self.shape = storage, shape

        def __reduce__(self):
            stride = tuple(int(np.prod(self.shape[i + 1:])) for i in range(len(self.shape)))
            return _rebuild_tensor_v2, (self.storage, 0, self.shape, stride, False, OrderedDict())

    storages = [Storage(str(i), v.ravel()) for i, v in enumerate(sd.values())]
    obj = OrderedDict((k, Tensor(s, v.shape)) for (k, v), s in zip(sd.items(), storages))

    class Pickler(pickle.Pickler):
        def persistent_id(self, o):
            if isinstance(o, Storage):
                return ("storage", FloatStorage, o.key, "cpu", o.data.size, None)
            return None

    with open(path, "wb") as f:
        pickle.dump(0x1950A86A20F9469CFC6C, f, protocol=2)
        pickle.dump(1001, f, protocol=2)
        pickle.dump({"protocol_version": 1001, "little_endian": True}, f, protocol=2)
        Pickler(f, protocol=2).dump(obj)
        pickle.dump([s.key for s in storages], f, protocol=2)
        for s in storages:
            f.write(struct.pack("<q", s.data.size))
            f.write(s.data.astype("<f4").tobytes())


def _conv(x, w, b):
    pad = np.pad(x, ((0, 0), (1, 1), (1, 1)))
    h, wd = x.shape[1:]
    out = np.zeros((w.shape[0], h, wd), np.float64)
    for dy in range(3):
        for dx in range(3):
            out += np.einsum("oi,ihw->ohw", w[:, :, dy, dx], pad[:, dy:dy + h, dx:dx + wd])
    return out + b[:, None, None]


def reference(sd, x, blocks):
    lrelu = lambda v: np.where(v > 0, v, 0.2 * v)  # noqa: E731
    c = lambda v, k: _conv(v, sd[f"{k}.weight"], sd[f"{k}.bias"])  # noqa: E731
    fea = c(x, "model.0")
    t = fea
    for b in range(blocks):
        y = t
        for r in (1, 2, 3):
            feats = [y]
            for i in range(1, 5):
                feats.append(lrelu(c(np.concatenate(feats), f"model.1.sub.{b}.RDB{r}.conv{i}.0")))
            y = c(np.concatenate(feats), f"model.1.sub.{b}.RDB{r}.conv5.0") * 0.2 + y
        t = y * 0.2 + t
    t = c(t, f"model.1.sub.{blocks}") + fea
    for k in ("model.3", "model.6"):
        t = lrelu(c(t.repeat(2, 1).repeat(2, 2), k))
    return c(lrelu(c(t, "model.8")), "model.10")


def test_legacy_pth_to_onnx_matches_the_network(tmp_path, monkeypatch):
    sd = tiny_esrgan()
    path = tmp_path / "tiny.pth"
    save_legacy(sd, path, monkeypatch)
    loaded = bundle.load_state_dict(path)
    assert list(loaded) == list(sd)
    assert all(np.array_equal(loaded[k], sd[k]) for k in sd)

    model = bundle.esrgan_onnx(loaded, "tiny")
    sess = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    x = np.random.default_rng(1).random((1, 3, 9, 7)).astype(np.float32)
    out = sess.run(None, {"input": x})[0]
    assert out.shape == (1, 3, 36, 28)
    assert np.allclose(out[0], reference(sd, x[0].astype(np.float64), 2), atol=1e-4)


def test_new_style_key_names_are_understood():
    sd = tiny_esrgan(blocks=1)
    renamed = {"model.0": "conv_first", "model.1.sub.1": "trunk_conv", "model.3": "upconv1", "model.6": "upconv2",
               "model.8": "HRconv", "model.10": "conv_last"}
    new = {}
    for k, v in sd.items():
        head, _, leaf = k.rpartition(".")
        if head in renamed:
            new[f"{renamed[head]}.{leaf}"] = v
        else:  # model.1.sub.0.RDB1.conv1.0.weight -> RRDB_trunk.0.RDB1.conv1.weight
            _, _, _, b, rdb, conv, _, leaf = k.split(".")
            new[f"RRDB_trunk.{b}.{rdb.lower()}.{conv}.{leaf}"] = v
    assert bundle._old_names(new).keys() == sd.keys()


def test_included_models_are_pinned_and_credited():
    spec = json.loads((MODELS / "models.json").read_text("utf-8"))
    names = [m["name"] for m in spec["models"]]
    assert len(set(names)) == len(names) >= 1
    for m in spec["models"]:
        assert len(m["sha256"]) == 64 and int(m["sha256"], 16) >= 0
        assert "/raw/" in m["url"] and m["license"] == "CC-BY-4.0"
    assert sum("*" in m["codecs"] for m in spec["models"]) == 1  # exactly one fallback
    licence = (MODELS / "LICENSE.txt").read_text("utf-8")
    assert "RunDevelopment" in licence and "Attribution 4.0 International" in licence
