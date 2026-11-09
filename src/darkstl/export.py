"""Mesh and texture writers: STL, OBJ (+MTL), PLY, glTF binary (GLB), PNG.

Meshes arrive in the internal frame (right-handed, Z up, metres). STL and PLY
keep Z up, which is what slicers expect; OBJ and glTF are Y-up formats, so
they get a proper rotation (no mirroring) on the way out.
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .geometry.mesh import TriMesh

FORMATS = {
    ".stl": "STL (binary) - 3D printing",
    ".obj": "Wavefront OBJ with materials",
    ".ply": "PLY (binary)",
    ".glb": "glTF binary with textures",
}

# Painted 3MF (and making models watertight, and cutting them into pieces) moved to Model Painter in 0.55.0
PAINTED_3MF_MOVED = ("Painted 3MF moved to Model Painter: export a .glb and open it there "
                     "(github.com/DrFlGd/Model-Painter)")

Z_UP_TO_Y_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])


@dataclass
class ExportOptions:
    scale: float = 1000.0  # metres -> millimetres
    ascii_stl: bool = False
    write_normals: bool = True
    textures: dict[int, np.ndarray] = field(default_factory=dict)  # material -> RGBA image (OBJ/GLB)
    material_names: list[str] = field(default_factory=list)
    progress: Callable[[float, str], None] | None = None
    # GLB: the model's skeleton in the pose the mesh is in, (bone names, parents, (B, 4, 4) world matrices in
    # metres, Z up), written as the glTF's nodes; with ``skin`` ((V, k) joints and weights per mesh vertex)
    # the mesh is bound to it, so the skeleton stands in the adjusted pose as its rest pose in Blender
    skeleton: tuple | None = None
    skin: tuple | None = None


def export_mesh(mesh: TriMesh, path: str | Path, options: ExportOptions | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    options = options or ExportOptions()
    ext = path.suffix.lower()
    if ext == ".stl":
        write_stl(mesh, path, options.scale, options.ascii_stl)
    elif ext == ".obj":
        write_obj(mesh, path, options)
    elif ext == ".ply":
        write_ply(mesh, path, options.scale)
    elif ext == ".glb":
        write_glb(mesh, path, options)
    elif ext == ".3mf":
        raise ValueError(PAINTED_3MF_MOVED)
    else:
        raise ValueError(f"Unsupported export format '{ext}' (choose from {', '.join(FORMATS)})")
    return path


# ---------------------------------------------------------------------------
# STL


def write_stl(mesh: TriMesh, path: Path, scale: float = 1000.0, ascii: bool = False) -> None:
    tri = (mesh.vertices * scale)[mesh.faces].astype(np.float32)
    normals = mesh.face_normals().astype(np.float32)
    if ascii:
        with open(path, "w", encoding="ascii") as fh:
            fh.write("solid darkstl\n")
            for n, t in zip(normals, tri):
                fh.write(f" facet normal {n[0]:e} {n[1]:e} {n[2]:e}\n  outer loop\n")
                for v in t:
                    fh.write(f"   vertex {v[0]:e} {v[1]:e} {v[2]:e}\n")
                fh.write("  endloop\n endfacet\n")
            fh.write("endsolid darkstl\n")
        return
    rec = np.zeros(len(tri), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"] = normals
    rec["v"] = tri
    with open(path, "wb") as fh:
        fh.write(b"Dark STL export".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tri)))
        fh.write(rec.tobytes())


def read_stl(path: str | Path) -> np.ndarray:
    """Read a binary STL into a (F, 3, 3) array (used by tests and previews)."""
    data = Path(path).read_bytes()
    (count,) = struct.unpack_from("<I", data, 80)
    rec = np.frombuffer(data, dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")], count=count, offset=84)
    return rec["v"].copy()


# ---------------------------------------------------------------------------
# PLY


def write_ply(mesh: TriMesh, path: Path, scale: float = 1000.0) -> None:
    v = (mesh.vertices * scale).astype("<f4")
    faces = np.zeros(mesh.n_faces, dtype=[("n", "u1"), ("i", "<i4", 3)])
    faces["n"] = 3
    faces["i"] = mesh.faces
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment Dark STL export\n"
        f"element vertex {len(v)}\nproperty float x\nproperty float y\nproperty float z\n"
        f"element face {mesh.n_faces}\nproperty list uchar int vertex_indices\nend_header\n"
    )
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(v.tobytes())
        fh.write(faces.tobytes())


# ---------------------------------------------------------------------------
# OBJ


def _material_name(options: ExportOptions, mat: int) -> str:
    if mat < len(options.material_names) and options.material_names[mat]:
        name = options.material_names[mat]
    else:
        name = f"material_{mat}"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def write_png(img: np.ndarray, path: Path) -> None:
    from PIL import Image

    arr = np.asarray(img)
    if arr.dtype != np.uint8:
        if arr.ndim == 2:
            Image.fromarray(np.clip(arr * 65535, 0, 65535).astype(np.uint16)).save(path)
            return
        arr = np.clip(arr * 255, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)


def write_obj(mesh: TriMesh, path: Path, options: ExportOptions) -> None:
    v = (mesh.vertices * options.scale) @ Z_UP_TO_Y_UP.T
    has_uv = mesh.corner_uvs is not None
    mtl_path = path.with_suffix(".mtl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# Dark STL export, {options.scale:g} units per metre\n")
        fh.write(f"mtllib {mtl_path.name}\n")
        np.savetxt(fh, v, fmt="v %.6f %.6f %.6f")
        if options.write_normals:
            np.savetxt(fh, mesh.vertex_normals() @ Z_UP_TO_Y_UP.T, fmt="vn %.5f %.5f %.5f")
        if has_uv:
            cuv = mesh.corner_uvs.reshape(-1, 2).astype(np.float64)
            keys = np.round(cuv * 1e6).astype(np.int64)
            uniq, inv = np.unique(keys, axis=0, return_inverse=True)
            vt = uniq / 1e6
            vt[:, 1] = 1.0 - vt[:, 1]  # OBJ's V points up
            np.savetxt(fh, vt, fmt="vt %.6f %.6f")
            vt_idx = inv.reshape(-1, 3) + 1
        vi = mesh.faces + 1
        for mat in np.unique(mesh.face_material):
            sel = mesh.face_material == mat
            fh.write(f"usemtl {_material_name(options, int(mat))}\n")
            if has_uv and options.write_normals:
                arr = np.stack([vi[sel], vt_idx[sel], vi[sel]], axis=2).reshape(-1, 9)
                np.savetxt(fh, arr, fmt="f %d/%d/%d %d/%d/%d %d/%d/%d")
            elif has_uv:
                arr = np.stack([vi[sel], vt_idx[sel]], axis=2).reshape(-1, 6)
                np.savetxt(fh, arr, fmt="f %d/%d %d/%d %d/%d")
            elif options.write_normals:
                arr = np.stack([vi[sel], vi[sel]], axis=2).reshape(-1, 6)
                np.savetxt(fh, arr, fmt="f %d//%d %d//%d %d//%d")
            else:
                np.savetxt(fh, vi[sel], fmt="f %d %d %d")
    with open(mtl_path, "w", encoding="utf-8") as fh:
        for mat in np.unique(mesh.face_material):
            name = _material_name(options, int(mat))
            fh.write(f"newmtl {name}\nKd 0.8 0.8 0.8\nKs 0 0 0\n")
            img = options.textures.get(int(mat))
            if img is not None:
                tex_path = path.with_name(f"{path.stem}_{name}.png")
                write_png(img, tex_path)
                fh.write(f"map_Kd {tex_path.name}\n")
            fh.write("\n")


# ---------------------------------------------------------------------------
# glTF binary


def write_glb(mesh: TriMesh, path: Path, options: ExportOptions) -> None:
    from PIL import Image

    scale = options.scale / 1000.0  # glTF is metric: keep metres unless the user scaled
    has_uv = mesh.corner_uvs is not None
    corner_v = mesh.faces.reshape(-1)
    if has_uv:
        cuv = mesh.corner_uvs.reshape(-1, 2)
        keys = np.column_stack([corner_v, np.round(cuv * 1e6).astype(np.int64)])
        uniq, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        src_vertex = uniq[:, 0]
        uv = cuv[first].astype(np.float32)
    else:
        src_vertex, inv = np.unique(corner_v, return_inverse=True)
        uv = None
    inv = inv.reshape(-1, 3)
    pos = ((mesh.vertices[src_vertex] * scale) @ Z_UP_TO_Y_UP.T).astype(np.float32)
    nrm = (mesh.vertex_normals()[src_vertex] @ Z_UP_TO_Y_UP.T).astype(np.float32)

    blob = bytearray()
    views, accessors = [], []

    def add_view(data: bytes, target: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            view["target"] = target
        blob.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(arr: np.ndarray, gltf_type: str, component: int, target: int | None, minmax: bool = False) -> int:
        acc = {"bufferView": add_view(arr.tobytes(), target), "componentType": component,
               "count": int(arr.shape[0]), "type": gltf_type}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    pos_acc = add_accessor(pos, "VEC3", 5126, 34962, minmax=True)
    nrm_acc = add_accessor(nrm, "VEC3", 5126, 34962)
    uv_acc = add_accessor(uv, "VEC2", 5126, 34962) if uv is not None else None

    images, textures, materials, primitives = [], [], [], []
    for mat in np.unique(mesh.face_material):
        sel = mesh.face_material == mat
        idx = inv[sel].astype(np.uint32).reshape(-1)
        idx_acc = add_accessor(idx, "SCALAR", 5125, 34963)
        material = {"name": _material_name(options, int(mat)),
                    "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        img = options.textures.get(int(mat))
        if img is not None and uv_acc is not None:
            buf = io.BytesIO()
            arr = img if img.dtype == np.uint8 else np.clip(img * 255, 0, 255).astype(np.uint8)
            Image.fromarray(arr).save(buf, format="PNG")
            images.append({"bufferView": add_view(buf.getvalue()), "mimeType": "image/png"})
            textures.append({"source": len(images) - 1})
            material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": len(textures) - 1}
        materials.append(material)
        attrs = {"POSITION": pos_acc, "NORMAL": nrm_acc}
        if uv_acc is not None:
            attrs["TEXCOORD_0"] = uv_acc
        primitives.append({"attributes": attrs, "indices": idx_acc, "material": len(materials) - 1})

    nodes = [{"mesh": 0, "name": path.stem}]
    scene_nodes = [0]
    if options.skeleton is not None and len(options.skeleton[0]):
        skin = _glb_skin(options, mesh, src_vertex, scale, nodes, scene_nodes, add_accessor)
        if skin is not None:
            joints_acc, weights_acc, skin_json = skin
            for prim in primitives:
                prim["attributes"]["JOINTS_0"] = joints_acc
                prim["attributes"]["WEIGHTS_0"] = weights_acc
            nodes[0]["skin"] = 0
    gltf = {
        "asset": {"version": "2.0", "generator": "Dark STL"},
        "scene": 0,
        "scenes": [{"nodes": scene_nodes}],
        "nodes": nodes,
        "meshes": [{"primitives": primitives, "name": path.stem}],
        "materials": materials,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    if options.skeleton is not None and nodes[0].get("skin") == 0:
        gltf["skins"] = [skin_json]
    if images:
        gltf["images"] = images
        gltf["textures"] = textures
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    while len(blob) % 4:
        blob.append(0)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, total))
        fh.write(struct.pack("<I4s", len(js), b"JSON"))
        fh.write(js)
        fh.write(struct.pack("<I4s", len(blob), b"BIN\0"))
        fh.write(bytes(blob))


def _glb_skin(options: ExportOptions, mesh: TriMesh, src_vertex: np.ndarray, scale: float, nodes: list, scene_nodes: list,
              add_accessor) -> tuple | None:
    """The skeleton's nodes (added to ``nodes`` and the scene), and, when the mesh's weights are given, its
    joints and weights accessors and the skin (inverse bind matrices of the pose itself, so the mesh stays as
    it is)."""
    names, parents, world = options.skeleton
    world = np.asarray(world, np.float64).reshape(-1, 4, 4)
    conv = np.eye(4)
    conv[:3, :3] = Z_UP_TO_Y_UP * scale
    conv_i = np.linalg.inv(conv)
    w = conv @ world @ conv_i  # the bones in the glTF's frame and units
    parents = [int(p) if 0 <= int(p) < len(names) and int(p) != i else -1 for i, p in enumerate(parents)]
    if _cycles(parents):
        parents = [-1] * len(names)
    first = len(nodes)
    for i, name in enumerate(names):
        local = w[i] if parents[i] < 0 else np.linalg.pinv(w[parents[i]]) @ w[i]
        nodes.append({"name": str(name), "matrix": [float(x) for x in local.T.reshape(-1)]})
    for i, p in enumerate(parents):
        if p >= 0:
            nodes[first + p].setdefault("children", []).append(first + i)
        else:
            scene_nodes.append(first + i)
    skin = options.skin
    if skin is None or len(skin[0]) != mesh.n_vertices:
        return None
    joints = np.asarray(skin[0], np.int64).reshape(mesh.n_vertices, -1)[src_vertex]
    weights = np.asarray(skin[1], np.float64).reshape(mesh.n_vertices, -1)[src_vertex]
    weights = np.where((joints >= 0) & (joints < len(names)), weights, 0.0)
    order = np.argsort(-weights, axis=1, kind="stable")[:, :4]  # glTF's one set: the four heaviest
    joints = np.take_along_axis(joints, order, axis=1)
    weights = np.take_along_axis(weights, order, axis=1)
    if joints.shape[1] < 4:
        pad = 4 - joints.shape[1]
        joints = np.pad(joints, ((0, 0), (0, pad)), constant_values=-1)
        weights = np.pad(weights, ((0, 0), (0, pad)))
    total = weights.sum(axis=1, keepdims=True)
    root = next((i for i, p in enumerate(parents) if p < 0), 0)
    none = total[:, 0] <= 0  # a vertex no bone holds goes with the skeleton's root
    weights = np.where(total > 0, weights / np.where(total > 0, total, 1), 0.0)
    joints = np.where(weights > 0, joints, 0)
    joints[none, 0], weights[none, 0] = root, 1.0
    component = 5123 if len(names) < 65535 else 5125
    joints_acc = add_accessor(joints.astype(np.uint16 if component == 5123 else np.uint32), "VEC4", component, 34962)
    weights_acc = add_accessor(weights.astype(np.float32), "VEC4", 5126, 34962)
    ibm = np.linalg.pinv(w)
    ibm_acc = add_accessor(np.ascontiguousarray(np.transpose(ibm, (0, 2, 1)).reshape(-1, 16)).astype(np.float32),
                           "MAT4", 5126, None)
    return joints_acc, weights_acc, {"joints": list(range(first, first + len(names))), "inverseBindMatrices": ibm_acc}


def _cycles(parents: list[int]) -> bool:
    for start in range(len(parents)):
        seen, i = set(), start
        while i >= 0:
            if i in seen:
                return True
            seen.add(i)
            i = parents[i]
    return False
