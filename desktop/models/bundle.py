"""Fetch the AI upscaling models the app ships with and turn them into ONNX.

The models (models.json) are ESRGAN networks published as PyTorch .pth files. This downloads each
one from its pinned commit, checks its SHA-256, converts it to .onnx without PyTorch (the legacy
.pth pickle is read directly and the network rebuilt with the onnx package) and writes the
models' licence next to them. The Windows build puts the result in engine/models, beside the
engine rather than inside it, where the app finds it (darkstl.textures.upscale.bundled_dir).

    python desktop/models/bundle.py build/engine/models [--cache DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import struct
import sys
import urllib.request
from collections import OrderedDict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------------------------
# Reading a legacy (pre-1.6, non-zip) torch.save file without torch

_DTYPES = {"FloatStorage": np.float32, "HalfStorage": np.float16, "DoubleStorage": np.float64,
           "LongStorage": np.int64, "IntStorage": np.int32, "ByteStorage": np.uint8}


class _StorageType:
    def __init__(self, name):
        self.dtype = np.dtype(_DTYPES[name])


class _Storage:
    def __init__(self, dtype):
        self.dtype = dtype
        self.data: np.ndarray | None = None


def _rebuild_tensor(storage, offset, size, stride, *rest):
    return (storage, offset, tuple(size), tuple(stride))


class _Unpickler(pickle.Unpickler):
    def __init__(self, f, storages):
        super().__init__(f)
        self.storages = storages

    def find_class(self, module, name):
        if module == "collections" and name == "OrderedDict":
            return OrderedDict
        if module == "torch._utils" and name in ("_rebuild_tensor_v2", "_rebuild_tensor"):
            return _rebuild_tensor
        if module == "torch" and name in _DTYPES:
            return _StorageType(name)
        raise pickle.UnpicklingError(f"unexpected {module}.{name} in the model file")

    def persistent_load(self, pid):
        kind, stype, key, _location, _numel, *view = pid
        if kind != "storage" or (view and view[0] is not None):
            raise pickle.UnpicklingError(f"unsupported storage {pid!r}")
        return self.storages.setdefault(key, _Storage(stype.dtype))


def load_state_dict(path: Path) -> dict[str, np.ndarray]:
    storages: dict[str, _Storage] = {}
    with open(path, "rb") as f:
        if f.read(2) == b"PK":
            raise ValueError(f"{path.name} is a zip-format .pth; only the legacy format is supported")
        f.seek(0)
        for _ in range(3):  # magic number, protocol version, system info
            pickle.load(f)
        result = _Unpickler(f, storages).load()
        keys = pickle.load(f)
        for key in keys:
            (numel,) = struct.unpack("<q", f.read(8))
            st = storages[key]
            st.data = np.frombuffer(f.read(numel * st.dtype.itemsize), st.dtype)
    out = {}
    for name, (st, offset, size, stride) in result.items():
        item = st.dtype.itemsize
        arr = np.lib.stride_tricks.as_strided(st.data[offset:], size, [s * item for s in stride])
        out[name] = np.ascontiguousarray(arr, np.float32)
    return out


# ---------------------------------------------------------------------------------------------
# ESRGAN (RRDBNet) as an ONNX graph

def _old_names(sd: dict) -> dict:
    """Map the "new" BasicSR key names onto the original ESRGAN ones (model.0, model.1.sub...)."""
    if "model.0.weight" in sd:
        return sd
    out = {}
    blocks = len({k.split(".")[1] for k in sd if k.startswith("RRDB_trunk.")})
    tail = {"conv_first": "model.0", "trunk_conv": f"model.1.sub.{blocks}", "upconv1": "model.3",
            "upconv2": "model.6", "HRconv": "model.8", "conv_last": "model.10"}
    for k, v in sd.items():
        head, _, leaf = k.rpartition(".")
        if head in tail:
            out[f"{tail[head]}.{leaf}"] = v
        elif k.startswith("RRDB_trunk."):
            _, i, rdb, conv, leaf = k.split(".")
            out[f"model.1.sub.{i}.{rdb.upper()}.{conv}.0.{leaf}"] = v
        else:
            raise ValueError(f"unexpected key {k}")
    return out


def esrgan_onnx(sd: dict[str, np.ndarray], name: str):
    """Build the ONNX model of a 4x (two 2x nearest upsamplings) ESRGAN state dict."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    sd = _old_names(sd)
    blocks = len({k.split(".")[3] for k in sd if k.startswith("model.1.sub.") and "RDB" in k})
    ups = sorted(int(k.split(".")[1]) for k in sd if k.endswith(".weight") and k.split(".")[1] not in ("0", "1"))
    if ups != [3, 6, 8, 10]:
        raise ValueError(f"only 4x ESRGAN models are supported (convolutions {ups})")
    inits, nodes = [], []
    counter = [0]

    def tmp(prefix="t"):
        counter[0] += 1
        return f"{prefix}{counter[0]}"

    def conv(x, key, act=False):
        w, b = sd[f"{key}.weight"], sd[f"{key}.bias"]
        inits.append(numpy_helper.from_array(w, key + ".weight"))
        inits.append(numpy_helper.from_array(b, key + ".bias"))
        y = tmp()
        nodes.append(helper.make_node("Conv", [x, key + ".weight", key + ".bias"], [y], pads=[1, 1, 1, 1],
                                      kernel_shape=[3, 3]))
        if act:
            z = tmp()
            nodes.append(helper.make_node("LeakyRelu", [y], [z], alpha=0.2))
            return z
        return y

    def op(kind, inputs, **attrs):
        y = tmp()
        nodes.append(helper.make_node(kind, inputs, [y], **attrs))
        return y

    inits.append(numpy_helper.from_array(np.array(0.2, np.float32), "k02"))
    inits.append(numpy_helper.from_array(np.array([1, 1, 2, 2], np.float32), "up2"))

    def rdb(x, key):
        feats = [x]
        for i in range(1, 5):
            cat = op("Concat", feats, axis=1) if len(feats) > 1 else x
            feats.append(conv(cat, f"{key}.conv{i}.0", act=True))
        x5 = conv(op("Concat", feats, axis=1), f"{key}.conv5.0")
        return op("Add", [op("Mul", [x5, "k02"]), x])

    fea = conv("input", "model.0")
    t = fea
    for b in range(blocks):
        y = t
        for r in (1, 2, 3):
            y = rdb(y, f"model.1.sub.{b}.RDB{r}")
        t = op("Add", [op("Mul", [y, "k02"]), t])
    t = op("Add", [conv(t, f"model.1.sub.{blocks}"), fea])
    for key in ("model.3", "model.6"):
        t = op("Resize", [t, "", "up2"], mode="nearest")
        t = conv(t, key, act=True)
    t = conv(t, "model.8", act=True)
    t = conv(t, "model.10")
    nodes[-1].output[0] = "output"
    x = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, "height", "width"])
    y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, "height4", "width4"])
    graph = helper.make_graph(nodes, name, [x], [y], initializer=inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="dark-stl")
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


# ---------------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(entry: dict, cache: Path) -> Path:
    path = cache / Path(entry["url"]).name
    if not path.is_file() or _sha256(path) != entry["sha256"]:
        print(f"downloading {entry['url']}", flush=True)
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(entry["url"], timeout=300) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        got = _sha256(tmp)
        if got != entry["sha256"]:
            tmp.unlink()
            raise SystemExit(f"{path.name}: SHA-256 {got} is not the pinned {entry['sha256']}")
        tmp.replace(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="folder for the .onnx models (made if missing)")
    ap.add_argument("--cache", type=Path, default=Path("build/model-cache"), help="where downloads are kept")
    args = ap.parse_args(argv)
    import onnx

    spec = json.loads((HERE / "models.json").read_text("utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    for entry in spec["models"]:
        pth = fetch(entry, args.cache)
        model = esrgan_onnx(load_state_dict(pth), entry["name"])
        dest = args.out / f"{entry['name']}.onnx"
        onnx.save(model, str(dest))
        print(f"{dest} ({dest.stat().st_size / 1e6:.1f} MB)", flush=True)
    (args.out / "LICENSE.txt").write_text((HERE / "LICENSE.txt").read_text("utf-8"), "utf-8")
    (args.out / "models.json").write_text(json.dumps(spec, indent=2) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
