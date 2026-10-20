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

import numpy as np

from .geometry.mesh import TriMesh

FORMATS = {
    ".stl": "STL (binary) - 3D printing",
    ".obj": "Wavefront OBJ with materials",
    ".ply": "PLY (binary)",
    ".glb": "glTF binary with textures",
}

Z_UP_TO_Y_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])


@dataclass
class ExportOptions:
    scale: float = 1000.0  # metres -> millimetres
    ascii_stl: bool = False
    write_normals: bool = True
    textures: dict[int, np.ndarray] = field(default_factory=dict)  # material -> RGBA image (OBJ/GLB)
    material_names: list[str] = field(default_factory=list)


def export_mesh(mesh: TriMesh, path: str | Path, options: ExportOptions | None = None) -> Path:
    path = Path(path)
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
        fh.write("# Dark STL export\n")
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

    gltf = {
        "asset": {"version": "2.0", "generator": "Dark STL"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": path.stem}],
        "meshes": [{"primitives": primitives, "name": path.stem}],
        "materials": materials,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
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
