"""Model packages: a game model written out once in a form of its own, so the app no longer needs the game.

The importers read each game's own formats. Importing a model writes what they read into a **package**,
a folder named after the model's file in the game (``chr/c1120.chrbnd.dcx.darkpkg/``) holding:

- ``package.json``: what the model is (game, kind, group, name, the file it came from) and the models
  the file holds (most hold one; a FromSoftware archive can hold several).
- per model, ``<name>.glb``: a glTF binary with the mesh, one primitive per part and material, its UVs
  and shading normals, and its skeleton and skin weights. Blender and other tools open it as it is.
  Dark STL's own extra attribute ``_VERTEX`` names the welded vertex each glTF vertex is, so the mesh
  reads back exactly as the importer built it (glTF splits vertices along UV seams and hard edges).
- per model, ``<name>.json``: the materials and which texture is each one's colour, normal or height
  map, how its normal maps are made, the model's sockets, notes and description, and its animations.

Textures and animations go into the game's **asset store** (``assets/`` beside the packages), each
once however many models use it: textures under their name in the game, animations by their content.
A texture is kept compressed as the game keeps it, as a DDS file (FromSoftware and RE Engine games), or
else as the picture the importer decoded (PNG); an animation as the local matrices it gives each bone per
frame, which any skeleton with those bone names can be posed with (:mod:`darkstl.posing`).

Packages are listed and opened by :class:`darkstl.importers.package.PackageImporter` like any other
model, and everything after import (Clean, Generate, Pose, Export) works on them unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .errors import FormatError
from .geometry.mesh import TriMesh
from .model import CatalogEntry, Material, MeshOptions, PaintRecipe, PaintUVs, Part, Rig, SourceModel, Texture
from .posing import pose_mesh, pose_world

log = logging.getLogger(__name__)

FORMAT = "darkstl-package"
VERSION = 1
SUFFIX = ".darkpkg"
INDEX = "package.json"
ASSETS = "assets"

# glTF is Y up; the export frame is Z up. A rotation, so nothing is lost either way.
Z_UP_TO_Y_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
_Y4 = np.eye(4)
_Y4[:3, :3] = Z_UP_TO_Y_UP
_Y4I = _Y4.T.copy()

_FLOAT, _UINT, _USHORT, _UBYTE = 5126, 5125, 5123, 5121
_DTYPES = {_FLOAT: np.float32, _UINT: np.uint32, _USHORT: np.uint16, _UBYTE: np.uint8, 5122: np.int16, 5120: np.int8}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _safe(part: str) -> str:
    keep = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in part).strip(" .")
    return keep or "_"


def safe_relative(name: str) -> str:
    """A name from the game (``/Game/Chars/T_Body_D``, ``c1120_a``) as a relative path that is safe on disk."""
    parts = [p for p in re.split(r"[\\/]+", str(name)) if p and p not in (".", "..")]
    return "/".join(_safe(p) for p in parts) or "_"


# ---------------------------------------------------------------------------
# Asset store


class AssetStore:
    """Textures and animations of one game, each kept once (``<root>/assets/...``).

    Textures are stored under their name in the game; two different pictures with the same name get
    the second one's content hash added. Animations are stored by content, so models that share an
    animation set (armour pieces and the player's animations) share the files.
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.folder = self.root / ASSETS
        self._index_file = self.folder / "index.json"
        self._lock = threading.Lock()
        try:
            kept = json.loads(self._index_file.read_text("utf-8"))
            self._index: dict[str, str] = dict(kept.get("textures") or {})
            # a texture of the game's listed by its importer (``<importer>|<its reference>``) -> where it's stored
            self._sources: dict[str, str] = dict(kept.get("sources") or {})
        except (OSError, ValueError, AttributeError, TypeError):
            self._index, self._sources = {}, {}
        self._by_texture: dict[tuple[str, str], str] = {}  # (name, source) -> stored path, within one import
        self._changed = False
        self._saved = 0.0
        self.bytes_written = 0

    def save(self, every: float = 0.0) -> None:
        """Write the store's index, when it changed (with ``every``: and it was last written that many seconds
        ago, so that a whole game's import doesn't write it again after each model)."""
        with self._lock:
            if not self._changed or time.monotonic() - self._saved < every:
                return
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self._index_file.with_suffix(".part")
            tmp.write_text(json.dumps({"textures": self._index, "sources": self._sources}, indent=1, sort_keys=True),
                           "utf-8")
            os.replace(tmp, self._index_file)
            self._changed, self._saved = False, time.monotonic()

    def _write(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.bytes_written += len(data)

    def add_texture(self, texture: Texture, source: str | None = None) -> str:
        """Store a texture, returning its path relative to the store's root: the DDS file the importer gives
        (compressed as in the game), or else the picture it decodes. ``source``: the texture as its importer
        lists the game's textures (see :meth:`stored_source`)."""
        rel = self._add_texture(texture)
        if source is not None:
            with self._lock:
                if self._sources.get(source) != rel:
                    self._sources[source] = rel
                    self._changed = True
        return rel

    def stored_source(self, source: str) -> str | None:
        """Where a texture of the game's, as its importer lists it (``<importer>|<its reference>``), is stored,
        if it is."""
        rel = self._sources.get(source)
        return rel if rel is not None and rel in self._index else None

    def stored_under(self) -> set[str]:
        """The names textures are stored under (lower case, as :func:`texture_base` makes them)."""
        out = set()
        for rel in self._index:
            name = re.sub(r"\.(png|npy|dds)$", "", rel, flags=re.I)
            out.add(re.sub(r"~[0-9a-f]{10}$", "", name).lower())
        return out

    def _add_texture(self, texture: Texture) -> str:
        key = (texture.name, texture.source)
        known = self._by_texture.get(key)
        if known is not None:
            return known
        data = _raw_dds(texture)
        if data is not None:
            digest, ext, encode = hashlib.sha1(data).hexdigest(), ".dds", lambda: data
        else:
            img = np.ascontiguousarray(texture.decode())
            digest = hashlib.sha1(str((img.dtype.str, img.shape)).encode() + img.tobytes()).hexdigest()
            ext, encode = (".png" if img.dtype == np.uint8 else ".npy"), lambda: _encode_image(img)
        base = f"{ASSETS}/textures/{safe_relative(texture.name)}"
        with self._lock:
            rel = base + ext
            if self._index.get(rel, digest) != digest:
                rel = f"{base}~{digest[:10]}{ext}"
            if self._index.get(rel) != digest or not (self.root / rel).is_file():
                self._write(rel, encode())
                self._index[rel] = digest
                self._changed = True
        self._by_texture[key] = rel
        return rel

    def stored_textures(self) -> list[str]:
        """Every texture in the store (paths relative to its root)."""
        return [rel for rel in self._index if (self.root / rel).is_file()]

    def add_animation(self, names: list[str], local: np.ndarray) -> str:
        """Store an animation: the local matrices (frames, bones, 4, 4) it gives the bones ``names``."""
        local = np.asarray(local, np.float32).reshape(len(local), len(names), 4, 4)[:, :, :3, :]
        still = np.all(np.abs(local - local[:1]) <= 1e-7, axis=(0, 2, 3))
        arrays = {
            "names": np.array(json.dumps(list(names))),
            "still": np.flatnonzero(still).astype(np.int32),
            "still_local": local[0, still],
            "moving": np.flatnonzero(~still).astype(np.int32),
            "moving_local": local[:, ~still],
            "frames": np.array(len(local), np.int32),
        }
        digest = hashlib.sha1(b"".join(np.ascontiguousarray(a).tobytes() for a in arrays.values())).hexdigest()
        rel = f"{ASSETS}/animations/{digest[:24]}.npz"
        if (self.root / rel).is_file():
            return rel
        data = _npz(arrays)
        with self._lock:
            if not (self.root / rel).is_file():
                self._write(rel, data)
        return rel


def _npz(arrays: dict[str, np.ndarray]) -> bytes:
    """``np.savez_compressed``, compressed less (half the time, 1% larger: matrices of floats hardly compress)."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for name, value in arrays.items():
            with z.open(f"{name}.npy", "w", force_zip64=True) as f:
                np.lib.format.write_array(f, np.asanyarray(value), allow_pickle=False)
    return buf.getvalue()


def texture_base(name: str) -> str:
    """Where a texture called ``name`` is stored, without its extension (lower case)."""
    return f"{ASSETS}/textures/{safe_relative(name)}".lower()


def stored_texture_name(rel: str) -> str:
    """A stored texture's name in the game, from its path in the store (with the folders of a name that has
    them: a Cyberpunk depot path)."""
    rel = str(rel).replace("\\", "/")
    head = f"{ASSETS}/textures/"
    name = rel[len(head):] if rel.lower().startswith(head) else Path(rel).name
    name = re.sub(r"\.(png|npy|dds)$", "", name, flags=re.I)
    return re.sub(r"~[0-9a-f]{10}$", "", name)


def _raw_dds(texture: Texture) -> bytes | None:
    """The texture as the DDS file its importer gives, when it gives one that reads."""
    if texture.raw is None:
        return None
    from .textures import dds

    try:
        data = bytes(texture.raw())
        info = dds.parse_dds(data)
        if info.data_offset + info.mip_size(0)[2] > len(data):
            return None
    except Exception:  # noqa: BLE001 - kept as the decoded picture instead
        return None
    return data


def _encode_image(img: np.ndarray) -> bytes:
    if img.dtype == np.uint8:
        from PIL import Image

        if img.ndim == 2:
            mode = "L"
        else:
            mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[img.shape[2]]
            if img.shape[2] == 1:
                img = img[:, :, 0]
        buf = io.BytesIO()
        Image.fromarray(img, mode).save(buf, format="PNG", compress_level=1)  # 3.5 times faster than 6, 15% larger
        return buf.getvalue()
    buf = io.BytesIO()
    np.save(buf, img, allow_pickle=False)
    return buf.getvalue()


def _decode_image(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".npy":
        return np.load(path, allow_pickle=False)
    if path.suffix.lower() == ".dds":
        from .textures import dds

        return dds.decode_dds(path.read_bytes())
    from PIL import Image

    with Image.open(path) as im:
        if im.mode not in ("L", "LA", "RGB", "RGBA"):
            im = im.convert("RGBA")
        img = np.asarray(im)
    if img.ndim == 2 or img.shape[2] < 4:  # the app expects RGBA
        g = img if img.ndim == 2 else img[:, :, 0]
        if img.ndim == 2:
            rgb = np.stack([g, g, g], axis=2)
            a = np.full(g.shape, 255, np.uint8)
        elif img.shape[2] == 2:
            rgb = np.stack([g, g, g], axis=2)
            a = img[:, :, 1]
        else:
            rgb, a = img, np.full(g.shape, 255, np.uint8)
        img = np.dstack([rgb, a])
    return np.ascontiguousarray(img)


@dataclass
class StoredAnimation:
    names: list[str]
    frames: int
    still: np.ndarray
    still_local: np.ndarray
    moving: np.ndarray
    moving_local: np.ndarray

    def local(self, frame: int) -> np.ndarray:
        """(bones, 4, 4) local matrices at ``frame``."""
        out = np.zeros((len(self.names), 4, 4))
        out[:, 3, 3] = 1.0
        out[self.still, :3] = self.still_local
        if len(self.moving):
            out[self.moving, :3] = self.moving_local[min(max(int(frame), 0), self.frames - 1)]
        return out


_anim_cache: OrderedDict[str, StoredAnimation] = OrderedDict()
_anim_lock = threading.Lock()


def read_animation(path: Path) -> StoredAnimation:
    key = os.path.normcase(os.path.abspath(path))
    with _anim_lock:
        if key in _anim_cache:
            _anim_cache.move_to_end(key)
            return _anim_cache[key]
    try:
        with np.load(path, allow_pickle=False) as z:
            anim = StoredAnimation(json.loads(str(z["names"])), int(z["frames"]), z["still"], z["still_local"],
                                   z["moving"], z["moving_local"])
    except (OSError, ValueError, KeyError) as exc:
        raise FormatError(f"The animation file {path.name} couldn't be read: {exc}") from exc
    with _anim_lock:
        _anim_cache[key] = anim
        while len(_anim_cache) > 6:
            _anim_cache.popitem(last=False)
    return anim


# ---------------------------------------------------------------------------
# glTF binary: writing


class _Blob:
    def __init__(self):
        self.data = bytearray()
        self.views: list[dict] = []
        self.accessors: list[dict] = []

    def view(self, raw: bytes, target: int | None = None) -> int:
        while len(self.data) % 4:
            self.data.append(0)
        v = {"buffer": 0, "byteOffset": len(self.data), "byteLength": len(raw)}
        if target:
            v["target"] = target
        self.data.extend(raw)
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, arr: np.ndarray, kind: str, component: int, target: int | None = None,
                 minmax: bool = False) -> int:
        arr = np.ascontiguousarray(arr, _DTYPES[component])
        acc = {"bufferView": self.view(arr.tobytes(), target), "componentType": component,
               "count": int(arr.shape[0]), "type": kind}
        if minmax and len(arr):
            acc["min"] = arr.min(axis=0).astype(float).tolist()
            acc["max"] = arr.max(axis=0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1


def write_glb(path: Path, mesh: TriMesh, materials: list[Material], rig: Rig | None, name: str) -> None:
    """The package's mesh (and skeleton) as a glTF binary, read back exactly by :func:`read_glb`."""
    blob = _Blob()
    nodes: list[dict] = []
    gltf: dict = {"asset": {"version": "2.0", "generator": "Dark STL model package"}, "scene": 0,
                  "scenes": [{"nodes": []}], "nodes": nodes}
    gltf["materials"] = [{"name": m.name or f"material {i}",
                          "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
                         for i, m in enumerate(materials)]
    skin_index = None
    if rig is not None and len(rig.names):
        first = len(nodes)
        local = _Y4 @ np.asarray(rig.bind_local, np.float64) @ _Y4I
        for i, bone in enumerate(rig.names):
            nodes.append({"name": bone, "matrix": _gl_matrix(local[i])})
        roots = []
        for i, p in enumerate(np.asarray(rig.parents, np.int64)):
            if 0 <= p < len(rig.names) and p != i:
                nodes[first + int(p)].setdefault("children", []).append(first + i)
            else:
                roots.append(first + i)
        if _has_cycle(rig.parents):
            for n in nodes[first:]:
                n.pop("children", None)
            roots = list(range(first, len(nodes)))
        ibm = np.linalg.pinv(_Y4 @ np.asarray(rig.bind_world, np.float64) @ _Y4I)
        ibm_acc = blob.accessor(np.transpose(ibm, (0, 2, 1)).reshape(-1, 16), "MAT4", _FLOAT)
        gltf["skins"] = [{"joints": list(range(first, len(nodes))), "inverseBindMatrices": ibm_acc,
                          "skeleton": roots[0] if len(roots) == 1 else None}]
        if gltf["skins"][0]["skeleton"] is None:
            del gltf["skins"][0]["skeleton"]
        skin_index = 0
        gltf["scenes"][0]["nodes"] += roots
    if mesh.n_faces:
        gltf["meshes"] = [{"name": name, "primitives": _primitives(blob, mesh, skin_index is not None)}]
        node = {"name": name, "mesh": 0}
        if skin_index is not None:
            node["skin"] = skin_index
        nodes.append(node)
        gltf["scenes"][0]["nodes"].append(len(nodes) - 1)
    gltf["accessors"] = blob.accessors
    gltf["bufferViews"] = blob.views
    gltf["buffers"] = [{"byteLength": len(blob.data)}]
    if not blob.views:
        del gltf["accessors"], gltf["bufferViews"], gltf["buffers"]
    _write_glb_file(path, gltf, bytes(blob.data))


def _has_cycle(parents) -> bool:
    parents = np.asarray(parents, np.int64)
    for start in range(len(parents)):
        seen, i = set(), start
        while 0 <= i < len(parents):
            if i in seen:
                return True
            seen.add(i)
            nxt = int(parents[i])
            if nxt == i:
                break
            i = nxt
    return False


def _gl_matrix(m: np.ndarray) -> list[float]:
    return [float(x) for x in np.asarray(m, np.float64).T.reshape(-1)]  # glTF matrices are column-major


def _primitives(blob: _Blob, mesh: TriMesh, skinned: bool) -> list[dict]:
    faces = mesh.faces
    corner_v = faces.reshape(-1)
    cols = [corner_v[:, None].astype(np.int64)]
    uv = mesh.corner_uvs.reshape(-1, 2).astype(np.float32) if mesh.corner_uvs is not None else None
    nrm = mesh.corner_normals.reshape(-1, 3).astype(np.float32) if mesh.corner_normals is not None else None
    if uv is not None:
        cols.append(uv.view(np.int32).astype(np.int64))
    if nrm is not None:
        cols.append(nrm.view(np.int32).astype(np.int64))
    keys = np.concatenate(cols, axis=1)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1, 3)
    src = corner_v[first]
    attrs = {"POSITION": blob.accessor((mesh.vertices[src] @ Z_UP_TO_Y_UP.T).astype(np.float32), "VEC3", _FLOAT,
                                       34962, minmax=True),
             "_VERTEX": blob.accessor(src.astype(np.uint32), "SCALAR", _UINT, 34962)}
    if nrm is not None:
        attrs["NORMAL"] = blob.accessor(nrm[first] @ Z_UP_TO_Y_UP.T.astype(np.float32), "VEC3", _FLOAT, 34962)
    if uv is not None:
        attrs["TEXCOORD_0"] = blob.accessor(uv[first], "VEC2", _FLOAT, 34962)
    skin = mesh.metadata.get("skin") if skinned else None
    if skin is not None:
        joints, weights = (np.asarray(a) for a in skin)
        joints, weights = joints[src], weights[src].astype(np.float32)
        k = joints.shape[1]
        sets = (k + 3) // 4
        joints = np.pad(joints, ((0, 0), (0, sets * 4 - k)), constant_values=-1)
        weights = np.pad(weights, ((0, 0), (0, sets * 4 - k)))
        weights = np.where(joints >= 0, weights, 0.0).astype(np.float32)
        component = _USHORT if joints.max(initial=0) < 65535 else _UINT
        for s in range(sets):
            j = np.where(joints[:, s * 4:s * 4 + 4] >= 0, joints[:, s * 4:s * 4 + 4], 0)
            attrs[f"JOINTS_{s}"] = blob.accessor(j, "VEC4", component, 34962)
            attrs[f"WEIGHTS_{s}"] = blob.accessor(weights[:, s * 4:s * 4 + 4], "VEC4", _FLOAT, 34962)
    out = []
    order = np.lexsort((mesh.face_material, mesh.face_group))
    groups = np.stack([mesh.face_group[order], mesh.face_material[order]], axis=1)
    starts = np.flatnonzero(np.r_[True, np.any(groups[1:] != groups[:-1], axis=1)]) if len(order) else []
    ends = list(starts[1:]) + [len(order)]
    for a, b in zip(starts, ends):
        sel = order[a:b]
        idx = inverse[sel].reshape(-1).astype(np.uint32)
        out.append({"attributes": attrs, "indices": blob.accessor(idx, "SCALAR", _UINT, 34963),
                    "material": int(mesh.face_material[sel[0]]), "extras": {"part": int(mesh.face_group[sel[0]])}})
    return out


def _write_glb_file(path: Path, gltf: dict, binary: bytes) -> None:
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    binary = binary + b"\0" * ((-len(binary)) % 4)
    chunks = struct.pack("<I4s", len(js), b"JSON") + js
    if binary:
        chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    data = struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# glTF binary: reading


@dataclass
class PackageMesh:
    mesh: TriMesh
    bones: list[str]
    parents: np.ndarray
    bind_local: np.ndarray
    bind_world: np.ndarray


def read_glb(path: Path) -> PackageMesh:
    """A package's mesh (welded, with per-corner UVs and normals, parts, materials and skin weights) and
    its skeleton, in the export frame."""
    data = Path(path).read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        raise FormatError(f"{Path(path).name} isn't a glTF binary")
    gltf, binary, pos = None, b"", 12
    while pos + 8 <= len(data):
        length, kind = struct.unpack_from("<I4s", data, pos)
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"JSON":
            gltf = json.loads(chunk.decode("utf-8"))
        elif kind == b"BIN\0":
            binary = chunk
        pos += 8 + length
    if gltf is None:
        raise FormatError(f"{Path(path).name} has no glTF description")

    def acc(i: int) -> np.ndarray:
        a = gltf["accessors"][i]
        v = gltf["bufferViews"][a["bufferView"]]
        dtype = np.dtype(_DTYPES[a["componentType"]])
        width = _WIDTH[a["type"]]
        start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = v.get("byteStride") or dtype.itemsize * width
        count = a["count"]
        if stride == dtype.itemsize * width:
            arr = np.frombuffer(binary, dtype, count * width, start)
        else:
            rows = np.frombuffer(binary, np.uint8, stride * (count - 1) + dtype.itemsize * width, start)
            arr = np.lib.stride_tricks.as_strided(rows, (count, dtype.itemsize * width), (stride, 1)).copy().view(dtype)
        return arr.reshape(count, width) if width > 1 else arr.reshape(count)

    nodes = gltf.get("nodes", [])
    bones, parents, local, world = [], np.zeros(0, np.int64), np.zeros((0, 4, 4)), np.zeros((0, 4, 4))
    skins = gltf.get("skins") or []
    if skins:
        joints = skins[0]["joints"]
        where = {n: i for i, n in enumerate(joints)}
        bones = [nodes[n].get("name", f"bone {i}") for i, n in enumerate(joints)]
        parents = np.full(len(joints), -1, np.int64)
        for i, n in enumerate(joints):
            for c in nodes[n].get("children", []):
                if c in where:
                    parents[where[c]] = i
        local = np.array([np.array(nodes[n].get("matrix", np.eye(4).reshape(-1).tolist()), np.float64)
                          .reshape(4, 4).T for n in joints]).reshape(-1, 4, 4)
        local = _Y4I @ local @ _Y4
        if "inverseBindMatrices" in skins[0]:
            ibm = acc(skins[0]["inverseBindMatrices"]).astype(np.float64).reshape(-1, 4, 4).transpose(0, 2, 1)
            world = _Y4I @ np.linalg.pinv(ibm) @ _Y4
        else:
            from .posing import compose_hierarchy

            world = compose_hierarchy(local, parents)

    prims = [p for m in gltf.get("meshes", [])[:1] for p in m.get("primitives", [])]
    if not prims:
        mesh = TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
        return PackageMesh(mesh, bones, parents, local, world)
    attrs = prims[0]["attributes"]
    gl_pos = acc(attrs["POSITION"]).astype(np.float64) @ Z_UP_TO_Y_UP  # back to Z up
    vertex = acc(attrs["_VERTEX"]).astype(np.int64) if "_VERTEX" in attrs else np.arange(len(gl_pos))
    n_vertices = int(vertex.max()) + 1 if len(vertex) else 0
    vertices = np.zeros((n_vertices, 3))
    vertices[vertex] = gl_pos
    normals = acc(attrs["NORMAL"]).astype(np.float32) @ Z_UP_TO_Y_UP.astype(np.float32) if "NORMAL" in attrs else None
    uvs = acc(attrs["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in attrs else None
    faces, corner_uvs, corner_normals, mats, groups = [], [], [], [], []
    for p in prims:
        idx = acc(p["indices"]).astype(np.int64).reshape(-1, 3)
        faces.append(vertex[idx])
        if uvs is not None:
            corner_uvs.append(uvs[idx])
        if normals is not None:
            corner_normals.append(normals[idx])
        mats.append(np.full(len(idx), int(p.get("material", 0)), np.int32))
        groups.append(np.full(len(idx), int((p.get("extras") or {}).get("part", 0)), np.int32))
    mesh = TriMesh(vertices, np.concatenate(faces), np.concatenate(corner_uvs) if uvs is not None else None,
                   np.concatenate(mats), np.concatenate(groups),
                   corner_normals=np.concatenate(corner_normals) if normals is not None else None)
    sets = sorted(int(k.split("_")[1]) for k in attrs if k.startswith("JOINTS_"))
    if sets and bones:
        j = np.concatenate([acc(attrs[f"JOINTS_{s}"]).astype(np.int64) for s in sets], axis=1)
        w = np.concatenate([acc(attrs[f"WEIGHTS_{s}"]).astype(np.float32) for s in sets], axis=1)
        j = np.where(w > 0, j, -1)
        joints = np.full((n_vertices, j.shape[1]), -1, np.int32)
        weights = np.zeros((n_vertices, j.shape[1]), np.float32)
        joints[vertex] = j
        weights[vertex] = w
        mesh.metadata["skin"] = (joints, weights)
    return PackageMesh(mesh, bones, parents, local, world)


# ---------------------------------------------------------------------------
# Writing a package


@dataclass
class WriteOptions:
    animations: bool = True
    progress: Callable[[str], None] | None = None  # called now and then while animations are sampled


def package_folder(root: Path, source_rel: str) -> Path:
    """Where the package for the game file at ``source_rel`` (relative to its game folder) goes."""
    return Path(root) / (safe_relative(source_rel) + SUFFIX)


def write_package(root: Path, entry: CatalogEntry, source_rel: str, models: list[SourceModel], store: AssetStore,
                  options: WriteOptions | None = None, hidden: set | frozenset = frozenset()) -> Path:
    """Write the package of the file ``entry`` lists (``models``: each model it holds) below ``root``. Models
    whose variant is in ``hidden`` are ones the file doesn't list itself but configurations name (a Cyberpunk
    part's appearance): kept, opened by name, not listed."""
    options = options or WriteOptions()
    folder = package_folder(root, source_rel)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    used: set[str] = set()
    for model in models:
        stem = _safe((model.variant if len(models) > 1 else None) or model.name or entry.stem)[:120]
        name, n = stem, 2
        while name.lower() in used:
            name, n = f"{stem} ({n})", n + 1
        used.add(name.lower())
        stats = _write_model(folder, name, model, store, options)
        written.append({"variant": model.variant, "name": model.name, "file": f"{name}.json", "stats": stats,
                        **({"hidden": True} if model.variant is not None and model.variant in hidden else {})})
    index = {
        "format": FORMAT, "version": VERSION, "game": entry.game, "category": entry.category, "group": entry.group,
        "stem": entry.stem, "label": entry.label, "importer": entry.importer, "is_lod": bool(entry.is_lod),
        "source": source_rel.replace("\\", "/"), "models": written, "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "importer_version": importer_version(entry.importer),
    }
    game_name = entry.__dict__.get("_game_name")
    if game_name:  # the name the game gives it (see Importer.names), so it travels with the package
        index["game_name"] = str(game_name)
    from . import __version__

    index["app"] = __version__
    tmp = folder / (INDEX + ".part")
    tmp.write_text(json.dumps(index, indent=1), "utf-8")
    os.replace(tmp, folder / INDEX)
    return folder / INDEX


def importer_version(key: str) -> int:
    """The importer's ``package_version``: packages written by an earlier one are written again."""
    from .importers import IMPORTERS

    return next((int(i.package_version) for i in IMPORTERS if i.key == key), 1)


def _stored(store: AssetStore, rel_root: str, tex: Texture) -> dict:
    try:
        file = store.add_texture(tex)
    except Exception as exc:  # noqa: BLE001 - a texture that can't be decoded is left out, and said so
        return {"name": tex.name, "source": tex.source, "error": str(exc)}
    try:
        describe = tex.describe()
    except Exception:  # noqa: BLE001 - only for display
        describe = ""
    return {"name": tex.name, "source": tex.source, "describe": describe, "file": f"{rel_root}/{file}"}


def _write_model(folder: Path, name: str, model: SourceModel, store: AssetStore, options: WriteOptions) -> dict:
    """Write one model of a package; its stats (size and counts, see library_index) for the package's index."""
    from .library_index import model_stats

    mesh = model.build_mesh(MeshOptions())
    rig = model.rig if model.rig is not None and mesh.metadata.get("skin") is not None else None
    write_glb(folder / f"{name}.glb", mesh, model.materials, rig, name)
    rel_root = os.path.relpath(store.root, folder).replace("\\", "/")
    textures = {key: _stored(store, rel_root, tex) for key, tex in model.textures.items()}
    paint = None
    if model.paint is not None:  # a paint mixed for each model it goes on: how, and from which textures
        paint = {"kind": model.paint.kind, "values": model.paint.values,
                 "textures": {k: _stored(store, rel_root, t) for k, t in model.paint.textures.items()}}
    paint_uvs = None
    if model.paint_uvs is not None:  # what such a paint is mixed from on this model
        try:
            uv0, uv1 = model.paint_uvs.load()
            arrays = {"uv0": np.asarray(uv0)} | ({"uv1": np.asarray(uv1)} if uv1 is not None else {})
            tmp = folder / f"{name}.paintuv.npz.part"
            tmp.write_bytes(_npz(arrays))
            os.replace(tmp, folder / f"{name}.paintuv.npz")
            paint_uvs = f"{name}.paintuv.npz"
        except Exception:  # noqa: BLE001 - its skins are then shown with its own textures
            log.debug("%s: no texture coordinates kept for paints", name, exc_info=True)
    animations, note = [], None
    if options.animations and rig is not None and model.animations is not None:
        animations, note = _write_animations(model, rig, store, rel_root, options)
    data = {
        "format": FORMAT, "version": VERSION, "name": model.name, "variant": model.variant, "game": model.game,
        "importer": model.importer, "mesh": f"{name}.glb",
        "parts": [asdict(p) for p in model.parts],
        "materials": [_material_json(m) for m in model.materials],
        "textures": textures, "info": list(model.info), "notes": list(model.notes),
        "sockets": {k: np.asarray(v, np.float64).reshape(-1).tolist() for k, v in model.sockets.items()},
        "flags": {o.key: o.default for o in model.options},
        "skeleton": len(rig.names) if rig is not None else 0,  # bones the glTF keeps (adjusted on the Pose tab)
        "animations": animations, "animation_note": note,
        **({"paint": paint} if paint else {}), **({"paint_uvs": paint_uvs} if paint_uvs else {}),
    }
    tmp = folder / f"{name}.json.part"
    tmp.write_text(json.dumps(data, indent=1), "utf-8")
    os.replace(tmp, folder / f"{name}.json")
    return model_stats(model, mesh)


def _material_json(m: Material) -> dict:
    out = asdict(m)
    out["normal_scale"] = list(m.normal_scale)
    out["detail_scale"] = list(m.detail_scale)
    return out


def _write_animations(model: SourceModel, rig: Rig, store: AssetStore, rel_root: str,
                      options: WriteOptions) -> tuple[list[dict], str | None]:
    anims = model.animations
    try:
        infos = anims.list()
    except Exception as exc:  # noqa: BLE001 - a broken animation archive leaves the model unposed
        return [], f"The animations couldn't be read when this package was written: {exc}"
    out = []
    last = 0.0
    for i, info in enumerate(infos):
        if not info.frames:
            continue
        if options.progress is not None and time.monotonic() - last > 0.25:
            last = time.monotonic()
            options.progress(f"animation {i + 1:,} of {len(infos):,} ({info.name})")
        try:
            if rig.pose_all is not None:
                names, local = rig.pose_all(info.name)
            else:
                frames = [rig.pose_locals(info.name, f) for f in range(int(info.frames))]
                names, local = frames[0][0], np.stack([f[1] for f in frames])
        except Exception:  # noqa: BLE001 - an animation that can't be read is left out
            continue
        file = store.add_animation(names, local)
        row = info.as_dict()
        row["file"] = f"{rel_root}/{file}"
        out.append(row)
    return out, getattr(anims, "note", None)


# ---------------------------------------------------------------------------
# Reading a package


def read_index(path: str | os.PathLike) -> dict:
    """A package's ``package.json`` (``path`` is that file or its folder)."""
    path = Path(path)
    if path.is_dir():
        path = path / INDEX
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise FormatError(f"{path} isn't a model package: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise FormatError(f"{path} isn't a Dark STL model package")
    if int(data.get("version", 0)) > VERSION:
        raise FormatError(f"{path.parent.name} was written by a newer Dark STL; update the app to open it")
    return data


def package_stats(index_file: str | os.PathLike, index: dict | None = None) -> dict | None:
    """The stats (size and counts, see library_index) of a package's first listed model: kept in its index since
    0.44, else read from its sidecar (parts, textures, animations) and the head of its glTF file (bounds)."""
    index_file = Path(index_file)
    index = read_index(index_file) if index is None else index
    models = [m for m in index.get("models") or [] if not m.get("hidden")] or index.get("models") or []
    if not models:
        return None
    if isinstance(models[0].get("stats"), dict):
        return models[0]["stats"]
    folder = index_file.parent
    try:
        data = json.loads((folder / models[0]["file"]).read_text("utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    parts = data.get("parts") or []
    out = {"vertices": sum(int(p.get("vertices") or 0) for p in parts),
           "triangles": sum(int(p.get("triangles") or 0) for p in parts), "parts": len(parts) or 1,
           "textures": len(data.get("textures") or {}), "animated": bool(data.get("animations"))}
    size = _glb_size_mm(folder / str(data.get("mesh") or ""))
    if size is not None:
        out["size_mm"] = size
    return out


def _glb_size_mm(path: Path) -> list[float] | None:
    """The size of a package's mesh (mm, Z up) from its glTF file's positions' bounds, reading only its JSON."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
            if len(head) < 20 or head[:4] != b"glTF":
                return None
            length, kind = struct.unpack_from("<II", head, 12)
            if kind != 0x4E4F534A:  # JSON
                return None
            gltf = json.loads(fh.read(length).decode("utf-8"))
    except (OSError, ValueError, struct.error):
        return None
    lo, hi = [], []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            acc = (gltf.get("accessors") or [])[prim.get("attributes", {}).get("POSITION", -1)] \
                if prim.get("attributes", {}).get("POSITION") is not None else None
            if acc and acc.get("min") and acc.get("max"):
                lo.append(acc["min"])
                hi.append(acc["max"])
    if not lo:
        return None
    low = np.min(np.asarray(lo, np.float64), axis=0)
    high = np.max(np.asarray(hi, np.float64), axis=0)
    span = (high - low) * 1000  # glTF is Y up
    return [round(float(span[0]), 1), round(float(span[2]), 1), round(float(span[1]), 1)]


def is_package_index(path: Path) -> bool:
    return path.name == INDEX and path.parent.name.endswith(SUFFIX)


class _AnimationInfo:
    def __init__(self, row: dict):
        self.row = row
        self.name = str(row["name"])
        self.frames = int(row.get("frames") or 1)
        self.duration = float(row.get("duration") or 0)
        self.fps = float(row.get("fps") or 0)
        self.source = str(row.get("source") or "")

    def as_dict(self) -> dict:
        return {k: v for k, v in self.row.items() if k != "file"}


class PackageAnimations:
    """The animations a package keeps, with the interface the Pose tab uses."""

    def __init__(self, folder: Path, rows: list[dict], bones: list[str], note: str | None):
        self.folder = folder
        self._infos = {r["name"]: _AnimationInfo(r) for r in rows}
        self._files = {r["name"]: folder / r["file"] for r in rows}
        self.skeleton = list(bones)
        self.note = note

    @property
    def names(self) -> list[str]:
        return list(self._infos)

    def __len__(self) -> int:
        return len(self._infos)

    def __contains__(self, name: str) -> bool:
        return name in self._infos

    def list(self) -> list[_AnimationInfo]:
        return list(self._infos.values())

    def info(self, name: str) -> _AnimationInfo:
        if name not in self._infos:
            raise FormatError(f"No animation named {name!r}")
        return self._infos[name]

    def clamp(self, name: str, frame: int) -> int:
        return min(max(int(frame), 0), self.info(name).frames - 1)

    def stored(self, name: str) -> StoredAnimation:
        self.info(name)
        return read_animation(self._files[name])


def listed_variants(models: list[dict]) -> list[str]:
    """The models a package lists (as its file did: the hidden ones only configurations name are left out)."""
    shown = [m for m in models if not m.get("hidden")]
    return [m.get("variant") or m.get("name") for m in shown] if len(shown) > 1 else []


def load_package(path: str | os.PathLike, variant: str | None = None, cache: Path | None = None) -> SourceModel:
    """Open a package (its ``package.json``) as a :class:`SourceModel`, as ``variant`` when it holds several.
    ``cache``: a folder for what's made from it when it's shown (paints mixed for each model)."""
    index_file = Path(path)
    index = read_index(index_file)
    folder = index_file.parent
    models = index.get("models") or []
    if not models:
        raise FormatError(f"{folder.name} holds no models")
    chosen = next((m for m in models if variant is not None and variant in (m.get("variant"), m.get("name"))),
                  models[0])
    try:
        data = json.loads((folder / chosen["file"]).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise FormatError(f"{folder.name}: {chosen['file']} couldn't be read: {exc}") from exc
    glb = folder / data["mesh"]
    if not glb.is_file():
        raise FormatError(f"{folder.name}: its mesh {data['mesh']} is missing")
    loaded: list[PackageMesh] = []
    lock = threading.Lock()

    def package_mesh() -> PackageMesh:
        with lock:
            if not loaded:
                loaded.append(read_glb(glb))
            return loaded[0]

    missing = []

    def stored(key: str, t: dict) -> Texture | None:
        file = t.get("file")
        if not file:
            missing.append(t.get("name") or key)
            return None
        tex_path = (folder / file).resolve()
        describe = t.get("describe") or ""
        return Texture(str(t.get("name") or key), str(t.get("source") or ""), load=lambda p=tex_path: _decode_image(p),
                       describe_fn=(lambda d=describe: d) if describe else None)

    textures = {k: tex for k, t in (data.get("textures") or {}).items() if (tex := stored(k, t)) is not None}
    paint, paint_for = None, None
    if isinstance(data.get("paint"), dict):  # a paint mixed for each model it goes on (a Borderlands 2 skin)
        paint_rows = data["paint"].get("textures") or {}
        held = {k: tex for k, t in paint_rows.items() if (tex := stored(k, t)) is not None}
        paint = PaintRecipe(str(data["paint"].get("kind") or ""), dict(data["paint"].get("values") or {}), held)
        keys = {k: [paint_rows[k]["file"], *_stamp((folder / paint_rows[k]["file"]).resolve())] for k in held}
        paint_for = _painter(paint, keys, cache)
    paint_uvs = None
    if data.get("paint_uvs") and (folder / data["paint_uvs"]).is_file():
        uv_file = folder / data["paint_uvs"]
        paint_uvs = PaintUVs([str(uv_file), _stamp(uv_file)], lambda: _read_paint_uvs(uv_file))
    materials = [_material_from(m) for m in data.get("materials") or []]
    parts = [Part(**p) for p in data.get("parts") or []]
    notes = list(data.get("notes") or [])
    if missing:
        notes.append(f"Textures that couldn't be stored when this package was written: {', '.join(missing[:6])}"
                     + (" …" if len(missing) > 6 else ""))
    rows = data.get("animations") or []
    anims = None
    rig = None
    if rows or data.get("skeleton"):  # (packages before 0.47 keep a skeleton only with animations)
        pm = package_mesh()
        if rows:
            anims = PackageAnimations(folder, rows, pm.bones, data.get("animation_note"))

        def pose_locals(name: str, frame: int):
            if anims is None:
                raise FormatError(f"{data.get('name')} has no animations to pose it with")
            return anims.stored(name).names, anims.stored(name).local(frame)

        if pm.bones:
            rig = Rig(pm.bones, pm.parents, pm.bind_local, pm.bind_world, pose_locals)

    def build(options: MeshOptions) -> TriMesh:
        pm = package_mesh()
        mesh = pm.mesh
        if options.parts is not None:
            mesh = mesh.subset(np.isin(mesh.face_group, sorted(options.parts)))
        else:
            mesh = mesh.copy()
        if options.pose is None:
            return mesh
        if anims is None or rig is None:
            raise FormatError(f"{data.get('name')} has no animations to pose it with")
        name, frame = options.pose
        stored = anims.stored(name)
        world = pose_world(rig.names, rig.parents, rig.bind_local, stored.names, stored.local(anims.clamp(name, frame)))
        skin = world @ np.linalg.pinv(rig.bind_world)
        none = (np.full((mesh.n_vertices, 1), -1), np.zeros((mesh.n_vertices, 1)))
        joints, weights = mesh.metadata.get("skin") or none
        return pose_mesh(mesh, joints, weights, skin)

    info = list(data.get("info") or [])
    info.append(f"model package from {index.get('source')}")
    sockets = {k: np.asarray(v, np.float64).reshape(4, 4) for k, v in (data.get("sockets") or {}).items()}
    return SourceModel(
        name=str(data.get("name") or index.get("stem")), path=index_file, importer="package", build=build,
        variant=chosen.get("variant"),
        variants=listed_variants(models),
        game=data.get("game") or index.get("game"), parts=parts, materials=materials, textures=textures,
        info=info, notes=notes, animations=anims, sockets=sockets, rig=rig, paint=paint, paint_for=paint_for,
        paint_uvs=paint_uvs,
    )


def _stamp(path: Path) -> list:
    try:
        st = os.stat(path)
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return []


def _read_paint_uvs(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    with np.load(path, allow_pickle=False) as z:
        return z["uv0"], (z["uv1"] if "uv1" in z.files else None)


def _painter(paint: PaintRecipe, keys: dict[str, list], cache: Path | None):
    """``SourceModel.paint_for`` from a paint a package kept, by its kind; None for a kind this app doesn't know."""
    from .importers.borderlands import gunpaint

    if paint.kind == gunpaint.RECIPE:
        return gunpaint.painter(gunpaint.from_recipe(paint.values, paint.textures, keys), (), cache)
    return None


def _material_from(m: dict) -> Material:
    m = dict(m)
    for k in ("normal_scale", "detail_scale"):
        if k in m and m[k] is not None:
            m[k] = tuple(m[k])
    known = {f for f in Material.__dataclass_fields__}
    return Material(**{k: v for k, v in m.items() if k in known})


__all__ = ["ASSETS", "AssetStore", "FORMAT", "INDEX", "PackageAnimations", "SUFFIX", "WriteOptions",
           "is_package_index", "load_package", "package_folder", "read_glb", "read_index", "safe_relative",
           "stored_texture_name",
           "write_glb", "write_package"]
