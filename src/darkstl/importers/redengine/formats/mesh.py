"""CMesh: the geometry of a ``.mesh`` file.

The CR2W root is a ``CMesh`` whose ``renderResourceBlob`` is a
``rendRenderMeshBlob``: a header describing the chunks (sub-meshes) and one
buffer holding every chunk's vertex streams followed by the index buffer.
Positions are quantised to signed 16-bit (``quantizationScale`` /
``quantizationOffset`` restore metres), normals and tangents are packed
10-10-10-2, UVs are half floats. A mesh keeps all its levels of detail as
separate chunks, told apart by ``lodMask`` (bit 0 is the full-detail level).
Game space is right-handed and Z up, the export frame, so nothing is rotated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ...fromsoft.formats.binary import FormatError
from .cr2w import Cr2wFile, Handle, Red

ELEMENT_BYTES = {
    "PT_Float1": 4, "PT_Float2": 8, "PT_Float3": 12, "PT_Float4": 16, "PT_Float16_2": 4, "PT_Float16_4": 8,
    "PT_UShort1": 2, "PT_UShort2": 4, "PT_UShort4": 8, "PT_UShort4N": 8, "PT_Short1": 2, "PT_Short2": 4,
    "PT_Short4": 8, "PT_Short4N": 8, "PT_UInt1": 4, "PT_UInt2": 8, "PT_UInt3": 12, "PT_UInt4": 16,
    "PT_Int1": 4, "PT_Int2": 8, "PT_Int3": 12, "PT_Int4": 16, "PT_Color": 4, "PT_UByte1": 1, "PT_UByte1F": 1,
    "PT_UByte4": 4, "PT_UByte4N": 4, "PT_Byte4N": 4, "PT_Dec4": 4, "PT_Index16": 2, "PT_Index32": 4,
}


@dataclass
class Element:
    usage: str
    type: str
    usage_index: int
    stream: int
    offset: int  # within the vertex of its stream


@dataclass
class Chunk:
    index: int
    vertex_count: int
    index_count: int
    lod_mask: int
    positions: np.ndarray  # (n, 3) float32, metres
    indices: np.ndarray  # (k,) triangle list
    normals: np.ndarray | None = None  # (n, 3)
    uvs: list[np.ndarray] = field(default_factory=list)  # each (n, 2)
    skinned: bool = False
    material_index: int = 0  # into RedMesh.material_names, set for the chosen appearance

    @property
    def lod_level(self) -> int:
        """0 for the full-detail level, 1, 2... for the coarser ones."""
        m = self.lod_mask or 1
        return (m & -m).bit_length() - 1

    def triangles(self) -> np.ndarray:
        n = len(self.indices) // 3 * 3
        return self.indices[:n].reshape(-1, 3)


@dataclass
class Appearance:
    name: str
    chunk_materials: list[str]


@dataclass
class RedMesh:
    version: int
    blob_version: int
    chunks: list[Chunk]
    material_names: list[str]
    appearances: list[Appearance]
    bone_names: list[str]
    local_materials: list[Red | None]  # CMaterialInstance of each local material entry (by its index)
    external_materials: list  # Ref of each external entry
    material_entries: list[tuple[str, int, bool]]  # (name, index, is local)
    local_material_files: list = field(default_factory=list, repr=False)

    @property
    def lod_levels(self) -> list[int]:
        return sorted({c.lod_level for c in self.chunks})

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        pts = [c.positions for c in self.chunks if len(c.positions)]
        if not pts:
            return np.zeros(3), np.zeros(3)
        p = np.concatenate(pts)
        return p.min(0), p.max(0)

    def appearance(self, name: str | None) -> Appearance | None:
        if not self.appearances:
            return None
        return next((a for a in self.appearances if a.name == name), self.appearances[0])

    def assign_appearance(self, name: str | None) -> list[str]:
        """Give each chunk the material the appearance uses for it; returns the materials by chunk."""
        app = self.appearance(name)
        by_name = {n: i for i, n in enumerate(self.material_names)}
        out = []
        for c in self.chunks:
            mat = None
            if app is not None and app.chunk_materials:
                mat = app.chunk_materials[c.index % len(app.chunk_materials)]
            if mat is None or mat not in by_name:
                mat = self.material_names[c.index % len(self.material_names)] if self.material_names else ""
            c.material_index = by_name.get(mat, 0)
            out.append(mat)
        return out


def _vec4(v) -> np.ndarray:
    if isinstance(v, Red):
        return np.array(v.vec("X", "Y", "Z", "W"), np.float64)
    return np.array([0.0, 0.0, 0.0, 0.0])


def _layout(vl: Red) -> tuple[list[Element], list[int]]:
    """The elements of a vertex layout with each one's offset in its stream, and the stream strides."""
    elements: list[Element] = []
    cursor: dict[int, int] = {}
    for e in vl.get("elements") or []:
        if not isinstance(e, Red):
            continue
        t = e.get("type", "PT_Invalid")
        if t not in ELEMENT_BYTES or e.get("streamType", "ST_PerVertex") != "ST_PerVertex":
            continue
        stream = int(e.get("streamIndex", 0))
        off = cursor.get(stream, 0)
        elements.append(Element(e.get("usage", "PS_Invalid"), t, int(e.get("usageIndex", 0)), stream, off))
        cursor[stream] = off + ELEMENT_BYTES[t]
    strides = [int(s) for s in (vl.get("slotStrides") or [])]
    for s, used in cursor.items():
        while len(strides) <= s:
            strides.append(0)
        if strides[s] <= 0:
            strides[s] = used
    return elements, strides


def _view(data: bytes, offset: int, count: int, stride: int, dtype, width: int) -> np.ndarray:
    item = np.dtype(dtype)
    need = offset + (count - 1) * stride + width * item.itemsize if count else 0
    if need > len(data):
        raise FormatError("The mesh's vertex buffer is shorter than its chunks say")
    return np.ndarray((count, width), item, buffer=data, offset=offset, strides=(stride, item.itemsize))


def _dec4(u: np.ndarray) -> np.ndarray:
    u = u.astype(np.uint32)
    x = (u & 0x3FF).astype(np.float32)
    y = ((u >> 10) & 0x3FF).astype(np.float32)
    z = ((u >> 20) & 0x3FF).astype(np.float32)
    return np.stack([x, y, z], axis=1) * (2.0 / 1023.0) - 1.0


def _read_chunk(index: int, info: Red, data: bytes, scale: np.ndarray, offset: np.ndarray,
                index_base: int) -> Chunk:
    cv = info.get("chunkVertices")
    ci = info.get("chunkIndices")
    n = int(info.get("numVertices", 0))
    k = int(info.get("numIndices", 0))
    positions = np.zeros((n, 3), np.float32)
    normals = None
    uvs: list[tuple[int, np.ndarray]] = []
    skinned = False
    if isinstance(cv, Red):
        elements, strides = _layout(cv.get("vertexLayout") or Red("GpuWrapApiVertexLayoutDesc"))
        bases = [int(b) for b in (cv.get("byteOffsets") or [])]
        for e in elements:
            if e.stream >= len(bases) or e.stream >= len(strides) or strides[e.stream] <= 0:
                continue
            at = bases[e.stream] + e.offset
            stride = strides[e.stream]
            if e.usage == "PS_Position":
                if e.type == "PT_Short4N":
                    q = _view(data, at, n, stride, "<i2", 3).astype(np.float32)
                    positions = q / 32767.0 * scale[:3] + offset[:3]
                elif e.type in ("PT_Float3", "PT_Float4"):
                    positions = _view(data, at, n, stride, "<f4", 3).astype(np.float32)
                else:
                    raise FormatError(f"Unsupported position packing {e.type}")
            elif e.usage == "PS_Normal" and e.usage_index == 0:
                if e.type == "PT_Dec4":
                    normals = _dec4(_view(data, at, n, stride, "<u4", 1)[:, 0])
                elif e.type in ("PT_Float3", "PT_Float4"):
                    normals = _view(data, at, n, stride, "<f4", 3).astype(np.float32)
            elif e.usage == "PS_TexCoord":
                if e.type == "PT_Float16_2":
                    uv = _view(data, at, n, stride, "<f2", 2).astype(np.float32)
                elif e.type == "PT_Float2":
                    uv = _view(data, at, n, stride, "<f4", 2).astype(np.float32)
                else:
                    continue
                uvs.append((e.usage_index, uv))
            elif e.usage == "PS_SkinIndices":
                skinned = True
    indices = np.zeros(0, np.int64)
    if isinstance(ci, Red) and k:
        at = index_base + int(ci.get("teOffset", 0))
        wide = ci.get("pe", "IBCT_IndexUShort") == "IBCT_IndexUInt"
        width = 4 if wide else 2
        if at + k * width > len(data):
            raise FormatError("The mesh's index buffer is shorter than its chunks say")
        indices = np.frombuffer(data, "<u4" if wide else "<u2", k, at).astype(np.int64)
    uvs.sort(key=lambda t: t[0])
    return Chunk(index, n, k, int(info.get("lodMask", 1)), np.ascontiguousarray(positions), indices,
                 None if normals is None else np.ascontiguousarray(normals), [u for _, u in uvs], skinned)


def read_mesh(f: Cr2wFile) -> RedMesh:
    root = f.root
    if root.cls != "CMesh":
        raise FormatError(f"Not a mesh: the file holds a {root.cls}")
    blob = f.chunk(root.get("renderResourceBlob"))
    chunks: list[Chunk] = []
    blob_version = 0
    if isinstance(blob, Red) and blob.cls == "rendRenderMeshBlob":
        header = blob.get("header")
        if not isinstance(header, Red):
            raise FormatError("The mesh's render blob has no header")
        blob_version = int(header.get("version", 0))
        data = f.buffer(blob.get("renderBuffer"))
        scale = _vec4(header.get("quantizationScale"))
        offset = _vec4(header.get("quantizationOffset"))
        index_base = int(header.get("indexBufferOffset", 0))
        for i, info in enumerate(header.get("renderChunkInfos") or []):
            if isinstance(info, Red):
                chunks.append(_read_chunk(i, info, data, scale, offset, index_base))
    entries = []
    for e in root.get("materialEntries") or []:
        if isinstance(e, Red):
            entries.append((str(e.get("name", "")), int(e.get("index", 0)), bool(e.get("isLocalInstance", False))))
    appearances = []
    for h in root.get("appearances") or []:
        a = f.chunk(h) if isinstance(h, Handle) else None
        if isinstance(a, Red):
            appearances.append(Appearance(str(a.get("name", "")), [str(n) for n in (a.get("chunkMaterials") or [])]))
    local: list[Red | None] = []
    local_files: list = []
    buf = root.get("localMaterialBuffer")
    if isinstance(buf, Red) and buf.get("rawData") is not None:
        from .cr2w import read_cr2w_list

        raw = f.buffer(buf.get("rawData"))
        slices = [(int(h.get("offset", 0)), int(h.get("size", 0))) for h in (buf.get("rawDataHeaders") or [])
                  if isinstance(h, Red)]
        local_files = read_cr2w_list(raw, slices or None)
        local = [lf.root if lf.chunks else None for lf in local_files]
    if not local:
        for h in root.get("preloadLocalMaterialInstances") or root.get("localMaterialInstances") or []:
            local.append(f.chunk(h) if isinstance(h, Handle) else None)
    external = list(root.get("externalMaterials") or root.get("preloadExternalMaterials") or [])
    bones = [str(b) for b in (root.get("boneNames") or [])]
    mesh = RedMesh(f.version, blob_version, chunks, [e[0] for e in entries], appearances, bones, local, external,
                   entries, local_files)
    mesh.assign_appearance(None)
    return mesh


__all__ = ["Appearance", "Chunk", "Element", "RedMesh", "read_mesh"]
