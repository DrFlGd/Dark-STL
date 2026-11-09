"""RenderWare models (``.dff``): a Clump of frames, geometries and atomics.

- **Frame List**: per frame a 3x3 rotation (right, up, at), a position, the parent's index and
  flags; extensions give each frame its name (Rockstar's node name chunk) and, for skinned
  models, an HAnim bone id (the root bone's HAnim chunk lists the whole hierarchy).
- **Geometry List**: per geometry its flags (positions, normals, prelit colours, UV sets),
  triangles as ``v1, v0, material, v2`` (u16), one morph target with positions and normals, a
  Material List (colour, texture and mask names) and extensions, of which the Skin plugin
  (bone indices and weights for four bones per vertex, an inverse bind matrix per bone) is read.
- **Atomics**: a frame and a geometry each: the model's parts.

Layouts from the gtamods.com wiki and librw (MIT). GTA's space is right-handed, Z up, metres.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from . import chunks as ck
from .chunks import Reader

# geometry flags
TRISTRIP = 0x01
POSITIONS = 0x02
TEXTURED = 0x04
PRELIT = 0x08
NORMALS = 0x10
TEXTURED2 = 0x80
NATIVE = 0x01000000


@dataclass
class Frame:
    rotation: np.ndarray  # (3, 3): columns right, up, at
    position: np.ndarray  # (3,)
    parent: int
    name: str = ""
    bone_id: int | None = None  # HAnim id, on skinned models' bones
    hierarchy: list[tuple[int, int, int]] | None = None  # (bone id, index, flags) on the root bone

    def local(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.rotation
        m[:3, 3] = self.position
        return m


@dataclass
class Material:
    colour: tuple[int, int, int, int]
    texture: str | None = None
    mask: str | None = None


@dataclass
class Skin:
    bones: int
    indices: np.ndarray  # (V, 4) uint8: positions in the HAnim hierarchy
    weights: np.ndarray  # (V, 4) float32
    inverse_bind: np.ndarray  # (bones, 4, 4), column vectors


@dataclass
class Geometry:
    flags: int
    positions: np.ndarray  # (V, 3) float32
    normals: np.ndarray | None
    uvs: list[np.ndarray]  # each (V, 2)
    colours: np.ndarray | None  # (V, 4) uint8
    triangles: np.ndarray  # (T, 3) int64
    triangle_material: np.ndarray  # (T,) int32, into materials
    materials: list[Material]
    skin: Skin | None = None
    native: bool = False


@dataclass
class Atomic:
    frame: int
    geometry: int
    flags: int = 0


@dataclass
class Clump:
    version: int
    frames: list[Frame]
    geometries: list[Geometry]
    atomics: list[Atomic]
    notes: list[str] = field(default_factory=list)

    def frame_world(self) -> np.ndarray:
        """Each frame's matrix in the model's space (4x4, column vectors)."""
        out = np.zeros((len(self.frames), 4, 4))
        done = [False] * len(self.frames)

        def world(i: int, depth: int = 0) -> np.ndarray:
            if done[i]:
                return out[i]
            f = self.frames[i]
            m = f.local()
            if 0 <= f.parent < len(self.frames) and f.parent != i and depth < 256:
                m = world(f.parent, depth + 1) @ m
            out[i] = m
            done[i] = True
            return m

        for i in range(len(self.frames)):
            world(i)
        return out

    def hierarchy(self) -> list[tuple[int, int, int]] | None:
        """The HAnim bone list (bone id, index, flags) of a skinned model."""
        for f in self.frames:
            if f.hierarchy:
                return f.hierarchy
        return None

    def bone_frames(self) -> list[int] | None:
        """For each bone of the HAnim hierarchy (as skins index them), the frame that is that bone."""
        h = self.hierarchy()
        if not h:
            return None
        by_id = {f.bone_id: i for i, f in enumerate(self.frames) if f.bone_id is not None}
        out = []
        for bone_id, _index, _flags in h:
            out.append(by_id.get(bone_id, -1))
        return out


def _matrix_rows(values) -> np.ndarray:
    """A RenderWare matrix (right, flags, up, pad, at, pad, pos, pad: rows for row vectors) as a 4x4
    matrix for column vectors."""
    a = np.asarray(values, np.float64).reshape(4, 4)
    m = np.eye(4)
    m[:3, 0] = a[0, :3]
    m[:3, 1] = a[1, :3]
    m[:3, 2] = a[2, :3]
    m[:3, 3] = a[3, :3]
    return m


def _read_frames(chunk: ck.Chunk) -> list[Frame]:
    st = chunk.struct()
    r = Reader.of(st)
    count = r.i32()
    if not 0 <= count <= 100_000:
        raise FormatError(f"Implausible frame count {count}")
    frames = []
    for _ in range(count):
        vals = r.unpack("12fiI")
        rot = np.array(vals[:9], np.float64).reshape(3, 3).T  # rows right, up, at -> columns
        frames.append(Frame(rot, np.array(vals[9:12], np.float64), int(vals[12])))
    i = 0
    for c in chunk.children():
        if c.type != ck.EXTENSION:
            continue
        if i >= len(frames):
            break
        for e in c.children():
            if e.type == ck.FRAME_NAME:
                frames[i].name = bytes(e.body).split(b"\0", 1)[0].decode("latin-1")
            elif e.type == ck.HANIM_PLG:
                er = Reader.of(e)
                _version, bone_id, nodes = er.unpack("Iii")
                frames[i].bone_id = bone_id
                if nodes > 0:
                    er.unpack("II")  # flags, key frame size
                    frames[i].hierarchy = [tuple(er.unpack("iiI")) for _ in range(nodes)]
        i += 1
    return frames


def _read_texture(chunk: ck.Chunk) -> tuple[str | None, str | None]:
    strings = [c for c in chunk.children() if c.type in (ck.STRING, 0x13)]
    name = strings[0].string() if strings else None
    mask = strings[1].string() if len(strings) > 1 else None
    return (name or None), (mask or None)


def _read_materials(chunk: ck.Chunk, version: int) -> list[Material]:
    st = chunk.struct()
    r = Reader.of(st)
    count = r.i32()
    refs = [r.i32() for _ in range(max(count, 0))]
    made = []
    for c in chunk.children():
        if c.type != ck.MATERIAL:
            continue
        mr = Reader.of(c.struct())
        _flags = mr.i32()
        colour = tuple(mr.unpack("4B"))
        mr.i32()
        textured = mr.i32()
        texture = mask = None
        if textured:
            t = c.child(ck.TEXTURE)
            if t is not None:
                texture, mask = _read_texture(t)
        made.append(Material(colour, texture, mask))
    out = []
    it = iter(made)
    for ref in refs:
        if ref < 0:
            m = next(it, None)
            out.append(m if m is not None else Material((255, 255, 255, 255)))
        else:
            out.append(out[ref] if ref < len(out) else Material((255, 255, 255, 255)))
    return out


def _read_skin(chunk: ck.Chunk, vertices: int) -> Skin:
    r = Reader.of(chunk)
    bones, used, _max_weights, _pad = r.unpack("4B")
    old = used == 0
    if not old:
        r.take(used)
    indices = np.frombuffer(r.take(vertices * 4), np.uint8).reshape(vertices, 4).copy()
    weights = np.frombuffer(r.take(vertices * 16), "<f4").reshape(vertices, 4).copy()
    mats = []
    for _ in range(bones):
        if old:
            r.take(4)  # 0xDEADDEAD
        mats.append(_matrix_rows(r.unpack("16f")))
    return Skin(bones, indices, weights, np.array(mats).reshape(-1, 4, 4))


def read_geometry(chunk: ck.Chunk) -> Geometry:
    version = chunk.version
    r = Reader.of(chunk.struct())
    flags, tris, verts, morphs = r.unpack("Iiii")
    if not (0 <= verts <= 2_000_000 and 0 <= tris <= 4_000_000):
        raise FormatError(f"Implausible geometry ({verts} vertices, {tris} triangles)")
    uv_sets = (flags >> 16) & 0xFF
    if uv_sets == 0 and flags & TEXTURED:
        uv_sets = 1
    if flags & TEXTURED2:
        uv_sets = max(uv_sets, 2)
    if version < 0x34000:
        r.take(12)  # ambient, specular, diffuse
    native = bool(flags & NATIVE)
    colours = None
    uvs: list[np.ndarray] = []
    triangles = np.zeros((0, 3), np.int64)
    tri_mat = np.zeros(0, np.int32)
    if not native:
        if flags & PRELIT:
            colours = np.frombuffer(r.take(verts * 4), np.uint8).reshape(verts, 4).copy()
        for _ in range(uv_sets):
            uvs.append(np.frombuffer(r.take(verts * 8), "<f4").reshape(verts, 2).copy())
        raw = np.frombuffer(r.take(tris * 8), "<u2").reshape(tris, 4).astype(np.int64)
        triangles = raw[:, [1, 0, 3]]
        tri_mat = raw[:, 2].astype(np.int32)
    positions = np.zeros((verts, 3), np.float32)
    normals = None
    for m in range(max(morphs, 1) if not native else 0):
        if r.left() < 24:
            break
        r.take(16)  # bounding sphere
        has_pos, has_nor = r.unpack("ii")
        p = np.frombuffer(r.take(verts * 12), "<f4").reshape(verts, 3).copy() if has_pos else None
        n = np.frombuffer(r.take(verts * 12), "<f4").reshape(verts, 3).copy() if has_nor else None
        if m == 0:
            if p is not None:
                positions = p
            normals = n
    ml = chunk.child(ck.MATERIAL_LIST)
    materials = _read_materials(ml, version) if ml is not None else []
    skin = None
    ext = chunk.child(ck.EXTENSION)
    if ext is not None:
        for e in ext.children():
            if e.type == ck.SKIN_PLG and verts and not native:
                try:
                    skin = _read_skin(e, verts)
                except FormatError:
                    skin = None
    if len(tri_mat) and materials:
        tri_mat = np.clip(tri_mat, 0, len(materials) - 1)
    return Geometry(flags, positions, normals, uvs, colours, triangles, tri_mat, materials, skin, native)


def read_clump(data: bytes) -> Clump:
    root = ck.read_root(data, ck.CLUMP)
    version = root.version
    frames: list[Frame] = []
    geometries: list[Geometry] = []
    atomics: list[Atomic] = []
    notes = []
    for c in root.children():
        if c.type == ck.FRAME_LIST:
            frames = _read_frames(c)
        elif c.type == ck.GEOMETRY_LIST:
            for g in c.children():
                if g.type == ck.GEOMETRY:
                    geometries.append(read_geometry(g))
        elif c.type == ck.ATOMIC:
            r = Reader.of(c.struct())
            frame, geo, aflags = r.unpack("iiI")
            inner = c.child(ck.GEOMETRY)
            if inner is not None:  # a geometry kept in the atomic itself (files without a geometry list)
                geometries.append(read_geometry(inner))
                geo = len(geometries) - 1
            atomics.append(Atomic(frame, geo, aflags))
    if any(g.native for g in geometries):
        notes.append("Some geometry is stored in a console's own format (PS2/Xbox), which isn't read.")
    return Clump(version, frames, geometries, atomics, notes)
