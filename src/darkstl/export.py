"""Mesh and texture writers: STL, OBJ (+MTL), PLY, glTF binary (GLB), painted 3MF, PNG.

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
from typing import Any, Callable

import numpy as np

from .geometry.mesh import TriMesh

FORMATS = {
    ".stl": "STL (binary) - 3D printing",
    ".obj": "Wavefront OBJ with materials",
    ".ply": "PLY (binary)",
    ".glb": "glTF binary with textures",
    ".3mf": "Painted 3MF - multi-colour printing (Bambu, Orca, Prusa)",
}

Z_UP_TO_Y_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])


@dataclass
class ExportOptions:
    scale: float = 1000.0  # metres -> millimetres
    ascii_stl: bool = False
    write_normals: bool = True
    textures: dict[int, np.ndarray] = field(default_factory=dict)  # material -> RGBA image (OBJ/GLB/3MF)
    material_names: list[str] = field(default_factory=list)
    paint: Any = None  # paint.PaintSettings for 3MF (None: the defaults); the Painting made is put in `painting`
    painting: Any = None
    progress: Callable[[float, str], None] | None = None


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
        write_3mf(mesh, path, options)
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


# ---------------------------------------------------------------------------
# painted 3MF


def write_3mf(mesh: TriMesh, path: Path, options: ExportOptions) -> None:
    """A 3MF painted from the model's colour textures for multi-colour printing (see :mod:`darkstl.paint`):
    the mesh as one object, each triangle carrying its paint as Bambu Studio / OrcaSlicer (``paint_color``)
    and PrusaSlicer (``slic3rpe:mmu_segmentation``) store it. Filament slot n is the palette's nth colour,
    which is listed in the file's metadata and drawn in a PNG beside it (``<name>_palette.png``)."""
    import zipfile
    from xml.sax.saxutils import escape

    from . import paint as painting_mod

    if not options.textures:
        raise ValueError("This model has no colour textures to paint from (the Finished model and cut pieces have none).")
    settings = options.paint or painting_mod.PaintSettings()
    painting = painting_mod.paint_mesh(mesh, options.textures, settings, options.progress)
    options.painting = painting
    if options.progress:
        options.progress(0.96, "Writing")
    v = mesh.vertices * options.scale
    name = escape(path.stem)
    palette = ";".join(painting_mod.hex_colour(c) for c in painting.palette)
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
        ' <metadata name="Application">Dark STL</metadata>\n'
        f' <metadata name="Title">{name}</metadata>\n'
        ' <metadata name="slic3rpe:Version3mf">1</metadata>\n'
        ' <metadata name="slic3rpe:MmPaintingVersion">1</metadata>\n'
        f' <metadata name="DarkSTL:Palette">{palette}</metadata>\n'
        ' <resources>\n'
        f'  <object id="1" name="{name}" type="model">\n'
        '   <mesh>\n    <vertices>\n'
    )
    parts = [head]
    verts = ["     <vertex x=\"%.6g\" y=\"%.6g\" z=\"%.6g\"/>" % tuple(row) for row in v.tolist()]
    parts.append("\n".join(verts))
    parts.append("\n    </vertices>\n    <triangles>\n")
    tris = []
    for (a, b, c), s in zip(mesh.faces.tolist(), painting.strings):
        if s:
            tris.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}" paint_color="{s}" slic3rpe:mmu_segmentation="{s}"/>')
        else:
            tris.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>')
    parts.append("\n".join(tris))
    parts.append(
        "\n    </triangles>\n   </mesh>\n  </object>\n </resources>\n <build>\n"
        '  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>\n'
        " </build>\n</model>\n"
    )
    model_xml = "".join(parts)
    n_faces = mesh.n_faces
    prusa_config = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <object id="1" instances_count="1">\n'
        f'  <metadata type="object" key="name" value="{name}"/>\n'
        '  <metadata type="object" key="extruder" value="1"/>\n'
        f'  <volume firstid="0" lastid="{max(n_faces - 1, 0)}">\n'
        f'   <metadata type="volume" key="name" value="{name}"/>\n'
        '   <metadata type="volume" key="volume_type" value="ModelPart"/>\n'
        '   <metadata type="volume" key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
        '   <metadata type="volume" key="source_object_id" value="0"/>\n'
        '   <metadata type="volume" key="source_volume_id" value="0"/>\n'
        '   <mesh edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
        "  </volume>\n </object>\n</config>\n"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        "</Types>\n"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model_xml)
        z.writestr("Metadata/Slic3r_PE_model.config", prusa_config)
    write_palette_png(painting.palette, painting.coverage, path.with_name(f"{path.stem}_palette.png"))


def write_palette_png(palette: np.ndarray, coverage: np.ndarray | None, path: Path) -> None:
    """A strip of the palette's colours, numbered by filament slot, to set the slicer's filaments by."""
    from PIL import Image, ImageDraw

    k = len(palette)
    sw, sh, pad = 120, 90, 8
    img = Image.new("RGB", (pad + k * (sw + pad), sh + 2 * pad + 44), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    for i, rgb in enumerate(palette):
        x = pad + i * (sw + pad)
        colour = tuple(int(c) for c in rgb[:3])
        draw.rectangle([x, pad, x + sw, pad + sh], fill=colour, outline=(120, 120, 120))
        lum = 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]
        draw.text((x + 8, pad + 6), str(i + 1), fill=(255, 255, 255) if lum < 128 else (0, 0, 0))
        text = f"{i + 1}: #{colour[0]:02X}{colour[1]:02X}{colour[2]:02X}"
        if coverage is not None and i < len(coverage):
            text += f"  {coverage[i] * 100:.0f}%"
        draw.text((x + 2, pad + sh + 8), text, fill=(30, 30, 30))
    draw.text((pad, pad + sh + 26), "Filament slot: colour, share of the surface", fill=(110, 110, 110))
    img.save(path)


def read_3mf(path: str | Path) -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    """Read back a 3MF written here: vertices, faces, the paint string per triangle (Bambu's), and the
    palette (for tests and checks)."""
    import zipfile
    import xml.etree.ElementTree as ET

    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
          "slic3rpe": "http://schemas.slic3r.org/3mf/2017/06"}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("3D/3dmodel.model"))
    palette = []
    for meta in root.findall("m:metadata", ns):
        if meta.get("name") == "DarkSTL:Palette":
            palette = [c for c in (meta.text or "").split(";") if c]
    mesh = root.find("m:resources/m:object/m:mesh", ns)
    verts = np.array([[float(e.get(a)) for a in "xyz"] for e in mesh.findall("m:vertices/m:vertex", ns)])
    tris = mesh.findall("m:triangles/m:triangle", ns)
    faces = np.array([[int(e.get(a)) for a in ("v1", "v2", "v3")] for e in tris], np.int64).reshape(-1, 3)
    paint = [e.get("paint_color") or "" for e in tris]
    prusa = [e.get("{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation") or "" for e in tris]
    if prusa != paint:
        raise ValueError("The Bambu and PrusaSlicer paint strings differ")
    return verts, faces, paint, palette
