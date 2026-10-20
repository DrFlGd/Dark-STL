"""Meshes for the 3D view, as one binary message the page reads straight into three.js buffers.

Layout (little-endian): ``DSTLMESH``, a uint32 header length, the JSON header
(padded to 4 bytes), then float32 positions (millimetres, Z up), float32
normals, float32 UVs (when the header says ``uv``) and uint32 triangle indices.
Triangles are sorted by material and the header lists each material's range,
so the page can give each one its texture. Where UVs are sent, vertices are
split along UV seams so every corner keeps its own texture coordinate.
"""

from __future__ import annotations

import json
import struct

import numpy as np

from ..geometry.mesh import TriMesh

MAGIC = b"DSTLMESH"


def pack_mesh(mesh: TriMesh, with_uv: bool = True, scale: float = 1000.0, extra: dict | None = None) -> bytes:
    order = np.argsort(mesh.face_material, kind="stable")
    faces = mesh.faces[order]
    materials = mesh.face_material[order]
    normals = mesh.vertex_normals()
    uv = with_uv and mesh.corner_uvs is not None
    if uv:
        corner_uv = mesh.corner_uvs[order].reshape(-1, 2)
        corner_v = faces.reshape(-1)
        keys = np.column_stack([corner_v, np.round(corner_uv * 1e5).astype(np.int64)])
        _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        src = corner_v[first]
        positions, norms, uvs = mesh.vertices[src], normals[src], corner_uv[first]
        index = inverse.reshape(-1).astype(np.uint32)
    else:
        positions, norms, uvs = mesh.vertices, normals, None
        index = faces.reshape(-1).astype(np.uint32)
    groups = []
    if len(materials):
        cuts = np.flatnonzero(np.diff(materials)) + 1
        starts = np.concatenate([[0], cuts])
        ends = np.concatenate([cuts, [len(materials)]])
        groups = [{"start": int(a) * 3, "count": int(b - a) * 3, "material": int(materials[a])}
                  for a, b in zip(starts, ends)]
    lo, hi = (mesh.vertices.min(0), mesh.vertices.max(0)) if mesh.n_vertices else (np.zeros(3), np.zeros(3))
    header = {"vertices": int(len(positions)), "triangles": int(mesh.n_faces), "uv": bool(uv), "groups": groups,
              "min": (lo * scale).round(4).tolist(), "max": (hi * scale).round(4).tolist(), **(extra or {})}
    head = json.dumps(header).encode("utf-8")
    head += b" " * (-len(head) % 4)
    parts = [MAGIC, struct.pack("<I", len(head)), head,
             (positions * scale).astype("<f4").tobytes(), norms.astype("<f4").tobytes()]
    if uv:
        parts.append(uvs.astype("<f4").tobytes())
    parts.append(index.astype("<u4").tobytes())
    return b"".join(parts)


def unpack_mesh(data: bytes) -> tuple[dict, dict[str, np.ndarray]]:
    """The header and arrays of a packed mesh (for tests)."""
    if data[:8] != MAGIC:
        raise ValueError("not a packed mesh")
    (n,) = struct.unpack_from("<I", data, 8)
    header = json.loads(data[12:12 + n])
    at = 12 + n
    out = {}
    for name, count, dtype in (("positions", header["vertices"] * 3, "<f4"), ("normals", header["vertices"] * 3, "<f4"),
                               ("uvs", header["vertices"] * 2 if header["uv"] else 0, "<f4"),
                               ("index", header["triangles"] * 3, "<u4")):
        out[name] = np.frombuffer(data, dtype, count, at)
        at += count * 4
    return header, out
