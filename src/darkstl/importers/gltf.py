"""Importer for glTF 2.0 models: ``.gltf`` (with its ``.bin`` and pictures beside it) and ``.glb``.

glTF is what Blender, FModel and most other tools export, so models taken out of a game with another
tool (or made in Blender) can be listed, enhanced and imported as model packages like any game's.

* Each mesh in the scene is a part that can be left out, placed where its node puts it.
* A material's base colour texture is the albedo and its normal texture the normal map (glTF's normal
  maps have green pointing up, the OpenGL way).
* glTF is Y up, in metres, with V pointing down: positions are turned to Z up, nothing is mirrored, and
  UVs are kept as they are. A model too big to be in metres is read as centimetres (some tools write
  Unreal's units), which can be switched off.
* Skinned meshes are shown in their bind pose; glTF animations aren't read.

Dark STL's own model packages are glTF files too (``.darkpkg`` folders); those are opened by the
package importer instead.
"""

from __future__ import annotations

import base64
import io
import json
import struct
from pathlib import Path
from urllib.parse import unquote

import numpy as np

from .. import access
from ..errors import FormatError
from ..geometry.mesh import TriMesh, weld
from ..model import BuildOption, CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from ..textures import dds
from .base import Importer, ScanProgress
from .loose import LoosePictures

SUFFIXES = (".gltf", ".glb")
CM_GUESS_SIZE = 50.0  # models wider than this many units are taken to be in centimetres (Unreal's units)
# glTF (Y up) -> the export frame (Z up), as row vectors times this
Y_UP_TO_Z_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])
_DTYPES = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8, 5122: np.int16, 5120: np.int8}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_NORMALIZED_MAX = {np.uint8: 255.0, np.uint16: 65535.0, np.int8: 127.0, np.int16: 32767.0}


def is_package_file(path: Path) -> bool:
    """Whether ``path`` is part of a Dark STL model package (opened by the package importer)."""
    return any(p.lower().endswith(".darkpkg") for p in Path(path).parts[:-1])


class GltfFile:
    """A glTF file's description and buffers."""

    def __init__(self, path: Path):
        self.path = Path(path)
        data = self.path.read_bytes()
        binary = None
        if data[:4] == b"glTF":
            if len(data) < 20:
                raise FormatError(f"{self.path.name} is cut short")
            gltf, pos = None, 12
            while pos + 8 <= len(data):
                length, kind = struct.unpack_from("<I4s", data, pos)
                chunk = data[pos + 8:pos + 8 + length]
                if kind == b"JSON":
                    gltf = json.loads(chunk.decode("utf-8"))
                elif kind == b"BIN\0":
                    binary = chunk
                pos += 8 + length
            if gltf is None:
                raise FormatError(f"{self.path.name} has no glTF description")
        else:
            try:
                gltf = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise FormatError(f"{self.path.name} isn't a glTF file ({exc})") from exc
        if not isinstance(gltf, dict) or "asset" not in gltf:
            raise FormatError(f"{self.path.name} isn't a glTF file")
        version = str(gltf["asset"].get("version", "2.0"))
        if not version.startswith("2"):
            raise FormatError(f"{self.path.name} is glTF {version}; only glTF 2 is read")
        self.gltf = gltf
        self.buffers: list[bytes] = []
        for i, b in enumerate(gltf.get("buffers", [])):
            uri = b.get("uri")
            if uri is None:
                if binary is None:
                    raise FormatError(f"{self.path.name}: buffer {i} has no data")
                self.buffers.append(binary)
            else:
                self.buffers.append(self._read_uri(uri))

    def _read_uri(self, uri: str) -> bytes:
        if uri.startswith("data:"):
            head, _, payload = uri.partition(",")
            return base64.b64decode(payload) if head.endswith(";base64") else unquote(payload).encode("latin-1")
        path = self.uri_path(uri)
        access.note_file(path, "other")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FormatError(f"{self.path.name}: {uri} wasn't found beside it") from exc

    def uri_path(self, uri: str) -> Path:
        return self.path.parent / unquote(uri).replace("\\", "/")

    def view(self, index: int) -> bytes:
        v = self.gltf["bufferViews"][index]
        start = v.get("byteOffset", 0)
        return self.buffers[v["buffer"]][start:start + v["byteLength"]]

    def accessor(self, index: int) -> np.ndarray:
        a = self.gltf["accessors"][index]
        dtype = np.dtype(_DTYPES[a["componentType"]])
        width = _WIDTH[a["type"]]
        count = a["count"]
        if "bufferView" in a:
            v = self.gltf["bufferViews"][a["bufferView"]]
            buf = self.buffers[v["buffer"]]
            start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
            stride = v.get("byteStride") or dtype.itemsize * width
            if stride == dtype.itemsize * width:
                arr = np.frombuffer(buf, dtype, count * width, start).copy()
            else:
                rows = np.frombuffer(buf, np.uint8, stride * (count - 1) + dtype.itemsize * width, start)
                arr = np.lib.stride_tricks.as_strided(rows, (count, dtype.itemsize * width), (stride, 1)).copy()
                arr = arr.view(dtype)
        else:
            arr = np.zeros(count * width, dtype)
        arr = arr.reshape(count, width) if width > 1 else arr.reshape(count)
        sparse = a.get("sparse")
        if sparse:
            idx = self._sparse(sparse["indices"], sparse["count"], 1, None).astype(np.int64)
            arr = arr.copy()
            arr[idx] = self._sparse(sparse["values"], sparse["count"], width, dtype)
        if a.get("normalized") and dtype.type in _NORMALIZED_MAX:
            arr = np.maximum(arr.astype(np.float32) / _NORMALIZED_MAX[dtype.type], -1.0)
        return arr

    def _sparse(self, part: dict, count: int, width: int, dtype) -> np.ndarray:
        dt = np.dtype(dtype or _DTYPES[part["componentType"]])
        v = self.gltf["bufferViews"][part["bufferView"]]
        start = v.get("byteOffset", 0) + part.get("byteOffset", 0)
        arr = np.frombuffer(self.buffers[v["buffer"]], dt, count * width, start)
        return arr.reshape(count, width) if width > 1 else arr

    def image_bytes(self, index: int) -> tuple[bytes, str]:
        """An image's file bytes and a name for it: its own, its file's, or the model's and its number."""
        img = self.gltf["images"][index]
        uri = img.get("uri", "")
        name = img.get("name") or (Path(unquote(uri)).stem if uri and not uri.startswith("data:") else
                                   f"{self.path.stem}_image{index}")
        if "bufferView" in img:
            return self.view(img["bufferView"]), name
        if not uri.startswith("data:"):
            access.note_file(self.uri_path(uri), "texture")
        return self._read_uri(uri), name


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], np.float64).reshape(4, 4).T
    m = np.eye(4)
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m[:3, :3] = r * np.asarray(node.get("scale", (1.0, 1.0, 1.0)), np.float64)
    m[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
    return m


def mesh_nodes(gltf: dict) -> list[tuple[int, np.ndarray]]:
    """(node, its world matrix) for each node of the scene shown that has a mesh, in scene order."""
    nodes = gltf.get("nodes", [])
    scenes = gltf.get("scenes") or []
    if scenes:
        roots = scenes[gltf.get("scene", 0) if gltf.get("scene", 0) < len(scenes) else 0].get("nodes", [])
    else:
        children = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in children]
    out, seen = [], set()
    stack = [(r, np.eye(4)) for r in reversed(roots)]
    while stack:
        i, parent = stack.pop()
        if i in seen or not 0 <= i < len(nodes):
            continue
        seen.add(i)
        world = parent @ _node_matrix(nodes[i])
        if "mesh" in nodes[i]:
            out.append((i, world))
        stack += [(c, world) for c in reversed(nodes[i].get("children", []))]
    if not out and not scenes:  # meshes no node places
        out = [(-1 - m, np.eye(4)) for m in range(len(gltf.get("meshes", [])))]
    return out


def _decode_picture(data: bytes) -> np.ndarray:
    if data[:4] == b"DDS ":
        return dds.decode_dds(data)
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(im.convert("RGBA"), np.uint8)


def _describe_picture(data: bytes) -> str:
    if data[:4] == b"DDS ":
        info = dds.parse_dds(data)
        return f"{info.width} x {info.height}, {info.codec}, {info.mipmaps} mips"
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return f"{im.width} x {im.height}, {im.format} {im.mode}"


class GltfImporter(LoosePictures, Importer):
    key = "gltf"
    name = "glTF (Blender, FModel and other tools)"
    file_filter = "glTF (*.gltf *.glb)"
    record_reads = False  # it notes the files it reads itself

    def can_open(self, path: Path) -> bool:
        path = Path(path)
        return path.suffix.lower() in SUFFIXES and not is_package_file(path)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        from ..listing import files_under

        out = []
        for path in files_under(folder):
            if path.suffix.lower() in SUFFIXES and not is_package_file(path):
                rel = path.parent.relative_to(folder).as_posix()
                out.append(CatalogEntry(game, "Models", "" if rel == "." else rel, path.stem, path.stem, path,
                                        importer=self.key, packed=False))
        self.note(folder, out)
        return out

    def related_files(self, path: Path, textures: bool = True, animations: bool = True, lod: bool = False):
        from ..collect import RelatedFile

        path = Path(path)
        out = [RelatedFile(path, "model")]
        try:
            f = GltfFile(path)
        except (FormatError, OSError, ValueError):
            return out
        for b in f.gltf.get("buffers", []):
            if b.get("uri") and not b["uri"].startswith("data:"):
                out.append(RelatedFile(f.uri_path(b["uri"]), "other"))
        if textures:
            for img in f.gltf.get("images", []):
                if img.get("uri") and not img["uri"].startswith("data:"):
                    out.append(RelatedFile(f.uri_path(img["uri"]), "texture"))
        return [r for r in out if r.path.is_file()]

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        access.note_file(path, "model")
        try:
            f = GltfFile(path)
        except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise FormatError(f"{path.name}: {exc}") from exc
        gltf = f.gltf
        notes: list[str] = []
        textures: dict[str, Texture] = {}

        def texture(info: dict | None) -> str | None:
            if not info or "index" not in info:
                return None
            try:
                tex = gltf["textures"][info["index"]]
                ext = tex.get("extensions") or {}
                source = tex.get("source", ext.get("MSFT_texture_dds", {}).get("source"))
                if source is None:
                    notes.append(f"Texture {info['index']} has no picture Dark STL can read")
                    return None
                key = f"image {source}"
                if key not in textures:
                    data, name = f.image_bytes(source)
                    textures[key] = Texture(name, str(path.name), lambda d=data: _decode_picture(d),
                                            lambda d=data: _describe_picture(d),
                                            raw=(lambda d=data: d) if data[:4] == b"DDS " else None)
                return key
            except (FormatError, KeyError, IndexError, TypeError) as exc:
                notes.append(f"A texture couldn't be read: {exc}")
                return None

        materials = []
        for i, m in enumerate(gltf.get("materials", [])):
            pbr = m.get("pbrMetallicRoughness") or {}
            materials.append(Material(m.get("name") or f"Material {i}", texture(pbr.get("baseColorTexture")),
                                      texture(m.get("normalTexture")), normal_green="opengl",
                                      details=[f"{k}: texture {v.get('index')}" for k, v in
                                               [("base colour", pbr.get("baseColorTexture")),
                                                ("normal", m.get("normalTexture"))] if v]))
        default_material = None

        pieces, parts, skinned, skipped = [], [], False, 0
        meshes = gltf.get("meshes", [])
        for node_index, world in mesh_nodes(gltf):
            node = gltf["nodes"][node_index] if node_index >= 0 else {"mesh": -1 - node_index}
            mesh = meshes[node["mesh"]]
            if "skin" in node:  # a skinned mesh's own transform doesn't place it: its joints do (bind pose)
                world, skinned = np.eye(4), True
            linear = world[:3, :3]
            normal_matrix = np.linalg.inv(linear).T if abs(np.linalg.det(linear)) > 1e-12 else linear
            mirrored = np.linalg.det(linear) < 0
            group = len(parts)
            tris = 0
            for prim in mesh.get("primitives", []):
                if prim.get("mode", 4) != 4:
                    skipped += 1
                    continue
                attrs = prim.get("attributes", {})
                if "POSITION" not in attrs:
                    continue
                pos = f.accessor(attrs["POSITION"]).astype(np.float64)
                pos = pos @ linear.T + world[:3, 3]
                normals = None
                if "NORMAL" in attrs:
                    normals = f.accessor(attrs["NORMAL"]).astype(np.float64) @ normal_matrix.T
                uv = f.accessor(attrs["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in attrs else None
                idx = (f.accessor(prim["indices"]).astype(np.int64) if "indices" in prim
                       else np.arange(len(pos), dtype=np.int64))
                idx = idx[:len(idx) // 3 * 3].reshape(-1, 3)
                idx = idx[(idx < len(pos)).all(axis=1)]
                if mirrored:
                    idx = idx[:, [0, 2, 1]]
                material = prim.get("material")
                if material is None or not 0 <= material < len(materials):
                    if default_material is None:
                        default_material = len(materials)
                        materials.append(Material("Default"))
                    material = default_material
                pieces.append((pos, normals, uv, idx, material, group))
                tris += len(idx)
            name = node.get("name") or mesh.get("name") or f"Mesh {group}"
            parts.append(Part(name, pieces[-1][4] if pieces and pieces[-1][5] == group else 0, 0, tris))
        if skipped:
            notes.append(f"{skipped} pieces drawn as lines or points were left out")
        if skinned:
            notes.append("Skinned meshes are shown in their bind pose; glTF animations aren't read.")
        if not pieces:
            raise FormatError(f"{path.name} has no triangles")
        for p in parts:  # vertices per part, for the parts list
            p.vertices = int(sum(len(np.unique(x[3])) for x in pieces if x[5] == parts.index(p)))

        def build(options: MeshOptions) -> TriMesh:
            if options.pose is not None:
                raise FormatError(f"{path.stem} has no animations Dark STL can pose it with")
            verts, faces, uvs, normals, mats, groups, base = [], [], [], [], [], [], 0
            for pos, nrm, uv, idx, material, group in pieces:
                if options.parts is not None and group not in options.parts:
                    continue
                verts.append(pos @ Y_UP_TO_Z_UP)
                faces.append(idx + base)
                uvs.append(uv[idx] if uv is not None else np.zeros((len(idx), 3, 2), np.float32))
                if nrm is not None:
                    length = np.linalg.norm(nrm, axis=1, keepdims=True)
                    nrm = (nrm / np.where(length > 0, length, 1)) @ Y_UP_TO_Z_UP
                    normals.append(nrm[idx].astype(np.float32))
                else:
                    normals.append(None)
                mats.append(np.full(len(idx), material, np.int32))
                groups.append(np.full(len(idx), group, np.int32))
                base += len(pos)
            if not faces:
                return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
            corner_normals = None
            if all(n is not None for n in normals):
                corner_normals = np.concatenate(normals)
            scale = 0.01 if options.flags.get("cm") else 1.0
            mesh = TriMesh(np.concatenate(verts) * scale, np.concatenate(faces), np.concatenate(uvs),
                           np.concatenate(mats), np.concatenate(groups), corner_normals=corner_normals)
            return weld(mesh.subset(np.ones(mesh.n_faces, bool)))  # drop vertices no face uses, then weld

        size = 0.0
        if pieces:
            every = np.concatenate([p[0] for p in pieces])
            size = float(np.linalg.norm(every.max(0) - every.min(0)))
        cm = size > CM_GUESS_SIZE
        if cm:
            notes.append(f"Read as centimetres because the model is {size:.0f} units across; "
                         "switch the option off if it is in metres")
        options = [BuildOption("cm", "Coordinates are in centimetres", cm,
                               "glTF is in metres, but some tools write Unreal's centimetres. Turn on if the "
                               "model is a hundred times too big.")]
        asset = gltf.get("asset", {})
        info = [f"glTF {asset.get('version', '2.0')}" + (f", made by {asset['generator']}" if asset.get("generator")
                                                         else ""),
                f"{len(parts)} meshes, {sum(len(p[3]) for p in pieces):,} triangles, {len(materials)} materials"]
        return SourceModel(name=path.stem, path=path, importer=self.key, build=build, parts=parts,
                           materials=materials, textures=textures, options=options, info=info, notes=notes)

    def describe(self, path: Path) -> list[str] | None:
        if not self.can_open(path):
            return None
        try:
            f = GltfFile(Path(path))
        except (FormatError, OSError):
            return None
        g = f.gltf
        return [f"glTF {g['asset'].get('version', '2.0')}, {len(g.get('meshes', []))} meshes, "
                f"{len(g.get('materials', []))} materials, {len(g.get('images', []))} pictures"]


__all__ = ["GltfFile", "GltfImporter", "is_package_file", "mesh_nodes"]
