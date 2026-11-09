"""Drawables: GTA V's models (``.ydr``, the entries of a ``.ydd`` and the body of a ``.yft``).

A drawable has a shader group (shaders with their texture names, and sometimes its own texture
dictionary), a skeleton, and models in up to four levels of detail. Each model is bound to a bone (or
skinned) and holds geometries: a vertex buffer, whose declaration says where each value is, an index
buffer, the shader it uses and, for skinned geometry, the bones its blend indices count from. The
original PC release and GTA V Enhanced lay a few of these blocks out differently. Layouts from CodeWalker
(MIT).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ....errors import FormatError
from .rsc import Reader
from .texture import Texture
from .texture import read_dictionary as read_textures

LODS = ("high", "medium", "low", "very low")

# semantic slots (as the original release's declarations number them)
POSITION, WEIGHTS, INDICES, NORMAL, COLOUR0, COLOUR1 = 0, 1, 2, 3, 4, 5
TEXCOORD0 = 6
TANGENT = 14

# original release: component type -> (numpy type, count, bytes)
LEGACY_TYPES = {1: ("<f2", 2, 4), 2: ("<f4", 1, 4), 3: ("<f2", 4, 8), 5: ("<f4", 2, 8), 6: ("<f4", 3, 12),
                7: ("<f4", 4, 16), 8: ("u1", 4, 4), 9: ("u1", 4, 4), 10: ("i1", 4, 4)}
LEGACY_SIZES = {0: 0, 1: 4, 2: 4, 3: 8, 4: 0, 5: 8, 6: 12, 7: 16, 8: 4, 9: 4, 10: 4}
# Enhanced: element format -> (numpy type, count); 24 is a packed 10:10:10:2 value
G9_TYPES = {2: ("<f4", 4), 6: ("<f4", 3), 10: ("<f2", 4), 16: ("<f4", 2), 24: ("<u4", 1),
            28: ("u1", 4), 30: ("u1", 4), 34: ("<f2", 2)}
# Enhanced element index -> the semantic slot above
G9_SEMANTICS = {0: POSITION, 4: NORMAL, 8: TANGENT, 16: WEIGHTS, 20: INDICES, 24: COLOUR0, 25: COLOUR1,
                **{28 + k: TEXCOORD0 + k for k in range(8)}}


@dataclass
class Bone:
    name: str
    tag: int
    parent: int
    rotation: np.ndarray  # (4,) x, y, z, w
    translation: np.ndarray  # (3,)
    scale: np.ndarray  # (3,)

    def local(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = quat_matrix(self.rotation) * self.scale[None, :]
        m[:3, 3] = self.translation
        return m


def quat_matrix(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
        [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)],
    ])


@dataclass
class Skeleton:
    bones: list[Bone]

    def by_tag(self) -> dict[int, int]:
        return {b.tag: i for i, b in enumerate(self.bones)}

    def by_name(self) -> dict[str, int]:
        return {b.name.lower(): i for i, b in enumerate(self.bones)}

    def world(self, locals_: np.ndarray | None = None) -> np.ndarray:
        """Each bone's place in the model ((B, 4, 4)), from the bind pose or the locals given."""
        n = len(self.bones)
        loc = np.stack([b.local() for b in self.bones]) if locals_ is None else locals_
        out = np.empty((n, 4, 4))
        done = np.zeros(n, bool)
        for i in range(n):
            self._place(i, loc, out, done)
        return out

    def _place(self, i: int, loc, out, done) -> None:
        chain = []
        j = i
        while j >= 0 and not done[j] and len(chain) <= len(self.bones):
            chain.append(j)
            p = self.bones[j].parent
            j = p if 0 <= p < len(self.bones) and p != j else -1
        for k in reversed(chain):
            p = self.bones[k].parent
            out[k] = (out[p] @ loc[k]) if 0 <= p < len(self.bones) and p != k and done[p] else loc[k]
            done[k] = True


@dataclass
class Shader:
    name: int  # hash of the shader's name
    file: int  # hash of its preset file (".sps")
    textures: list[tuple[int, str]] = field(default_factory=list)  # (parameter hash, texture name)
    embedded: dict[str, Texture] = field(default_factory=dict)  # textures the parameters carry themselves


@dataclass
class Geometry:
    positions: np.ndarray  # (N, 3) float32
    triangles: np.ndarray  # (M, 3) int64
    shader: int = 0
    normals: np.ndarray | None = None
    uvs: list[np.ndarray] = field(default_factory=list)
    colours: np.ndarray | None = None  # (N, 4) uint8
    weights: np.ndarray | None = None  # (N, 4) float32
    indices: np.ndarray | None = None  # (N, 4) int: bone (after bone_ids) per weight
    bone_ids: list[int] = field(default_factory=list)


@dataclass
class Model:
    geometries: list[Geometry]
    bone: int = 0  # the bone an unskinned model is placed by
    skinned: bool = False
    mask: int = 0  # render mask flags, as stored


@dataclass
class Drawable:
    name: str
    shaders: list[Shader]
    textures: dict[str, Texture]  # the drawable's own dictionary, by lower-case name
    skeleton: Skeleton | None
    lods: dict[str, list[Model]]
    notes: list[str] = field(default_factory=list)

    def models(self, lod: str = "high") -> list[Model]:
        if self.lods.get(lod):
            return self.lods[lod]
        for name in LODS:
            if self.lods.get(name):
                return self.lods[name]
        return []


# --- shaders ----------------------------------------------------------------------------------------

def _texture_name(r: Reader, ptr: int) -> tuple[str, Texture | None]:
    from .texture import read_texture

    if not ptr or not r.ok(ptr):
        return "", None
    try:
        t = read_texture(r, ptr)
    except FormatError:
        return "", None
    return t.name, (t if t.levels else None)


def _shader(r: Reader, ptr: int) -> Shader:
    if r.gen9:
        name, _preset, params, tex_refs, _unk, infos = r.unpack("IIQQQQ", ptr)
        sh = Shader(name, 0)
        if infos:
            nb, nt, nu, ns, npar = r.unpack("5B", infos)
            texptrs = r.pointers(tex_refs, nt) if tex_refs and nt else []
            for k in range(npar):
                h, data = r.unpack("II", infos + 8 + 8 * k)
                if data & 0x3 != 0:  # not a texture
                    continue
                idx = (data >> 2) & 0xFF
                if idx < len(texptrs):
                    tname, tex = _texture_name(r, texptrs[idx])
                    if tname:
                        sh.textures.append((h, tname))
                        if tex is not None:
                            sh.embedded[tname.lower()] = tex
        return sh
    params, name, _c, count = r.unpack("QIIB", ptr)
    file_hash = r.u32(ptr + 0x18)
    sh = Shader(name, file_hash)
    if not params or not count:
        return sh
    kinds = []
    for k in range(count):
        dtype, _a, _b, _c2, dptr = r.unpack("BBHIQ", params + 16 * k)
        kinds.append((dtype, dptr))
    hashes_at = params + 16 * count + sum(16 * d for d, _ in kinds if d)
    try:
        hashes = list(r.unpack(f"{count}I", hashes_at))
    except FormatError:
        hashes = [0] * count
    for (dtype, dptr), h in zip(kinds, hashes):
        if dtype == 0:
            tname, tex = _texture_name(r, dptr)
            if tname:
                sh.textures.append((h, tname))
                if tex is not None:
                    sh.embedded[tname.lower()] = tex
    return sh


def read_shader_group(r: Reader, ptr: int) -> tuple[list[Shader], dict[str, Texture]]:
    if not ptr:
        return [], {}
    tex_dict, shaders_ptr, count = r.unpack("QQH", ptr + 8)
    textures = {}
    if tex_dict:
        try:
            textures = read_textures(r, tex_dict)
        except FormatError:
            textures = {}
    shaders = []
    for sp in r.pointers(shaders_ptr, count):
        if not sp:
            shaders.append(Shader(0, 0))
            continue
        try:
            shaders.append(_shader(r, sp))
        except FormatError:
            shaders.append(Shader(0, 0))
    return shaders, textures


# --- skeleton -----------------------------------------------------------------------------------------

def read_skeleton(r: Reader, ptr: int) -> Skeleton | None:
    if not ptr:
        return None
    bones_ptr = r.u64(ptr + 0x20)
    count = r.u16(ptr + 0x5E)
    if not bones_ptr or not count:
        return None
    bones = []
    for i in range(count):
        b = bones_ptr + 80 * i
        rot = np.array(r.unpack("4f", b), np.float64)
        tr = np.array(r.unpack("3f", b + 0x10), np.float64)
        sc = np.array(r.unpack("3f", b + 0x20), np.float64)
        parent = r.i16(b + 0x32)
        name = r.string(r.u64(b + 0x38))
        tag = r.u16(b + 0x44)
        if not np.all(np.isfinite(sc)) or np.allclose(sc, 0):
            sc = np.ones(3)
        bones.append(Bone(name or f"bone {i}", tag, parent, rot, tr, sc))
    return Skeleton(bones)


# --- geometry -----------------------------------------------------------------------------------------

def _legacy_elements(r: Reader, info: int) -> tuple[list[tuple[int, int, int]], int]:
    flags, stride, _u, _count, types = r.unpack("IHBBQ", info)
    out, off = [], 0
    for k in range(16):
        if not flags >> k & 1:
            continue
        t = (types >> (4 * k)) & 0xF
        out.append((k, off, t))
        off += LEGACY_SIZES.get(t, 0)
    return out, stride


def _values(data: bytes, n: int, stride: int, off: int, dtype: str, count: int) -> np.ndarray:
    item = np.dtype(dtype).itemsize * count
    raw = np.frombuffer(data, np.uint8, count=n * stride).reshape(n, stride)[:, off:off + item]
    return np.ascontiguousarray(raw).view(dtype).reshape(n, count)


def _read_vertices(r: Reader, vb: int) -> tuple[int, dict[int, np.ndarray]]:
    if r.gen9:
        count, stride = r.unpack("IH", vb + 0x8)
        data_ptr = r.u64(vb + 0x18)
        info = r.u64(vb + 0x38)
        elems = []
        if info:
            offs = r.unpack("52I", info)
            types = r.bytes(info + 52 * 4 + 52, 52)
            for i, t in enumerate(types):
                if t and i in G9_SEMANTICS and t in G9_TYPES:
                    elems.append((G9_SEMANTICS[i], offs[i], ("g9", t)))
    else:
        stride = r.u16(vb + 0x8)
        data_ptr = r.u64(vb + 0x10)
        count = r.u32(vb + 0x18)
        if not data_ptr:
            data_ptr = r.u64(vb + 0x20)
        info = r.u64(vb + 0x30)
        elems, declared = _legacy_elements(r, info) if info else ([], stride)
        stride = stride or declared
        elems = [(k, off, ("legacy", t)) for k, off, t in elems]
    if not count or not stride or not data_ptr:
        return 0, {}
    data = r.bytes(data_ptr, count * stride)
    out = {}
    for sem, off, (kind, t) in elems:
        if kind == "legacy":
            if t not in LEGACY_TYPES:
                continue
            dtype, comps, size = LEGACY_TYPES[t]
        else:
            dtype, comps = G9_TYPES[t]
            size = np.dtype(dtype).itemsize * comps
        if off + size > stride:
            continue
        v = _values(data, count, stride, off, dtype, comps)
        if kind == "g9" and t == 24:  # 10:10:10:2, as signed normalised values
            u = v[:, 0].astype(np.uint32)
            parts = [((u >> s) & 0x3FF).astype(np.float32) / 1023.0 * 2 - 1 for s in (0, 10, 20)]
            v = np.stack(parts, -1)
        out[sem] = v
    return count, out


def _read_indices(r: Reader, ib: int) -> np.ndarray:
    count = r.u32(ib + 0x8)
    ptr = r.u64(ib + (0x18 if r.gen9 else 0x10))
    if not count or not ptr:
        return np.zeros(0, np.int64)
    return np.frombuffer(r.bytes(ptr, 2 * count), "<u2").astype(np.int64)


def read_geometry(r: Reader, ptr: int) -> Geometry:
    vb = r.u64(ptr + 0x18)
    ib = r.u64(ptr + 0x38)
    bone_ids_ptr = r.u64(ptr + 0x68)
    bone_count = r.u16(ptr + 0x72)
    if not vb or not ib:
        raise FormatError("A geometry has no vertex or index buffer")
    n, vals = _read_vertices(r, vb)
    if POSITION not in vals:
        raise FormatError("A geometry has no positions")
    idx = _read_indices(r, ib)
    tri = idx[: len(idx) // 3 * 3].reshape(-1, 3)
    tri = tri[(tri < n).all(axis=1)]
    pos = vals[POSITION][:, :3].astype(np.float32)
    g = Geometry(pos, tri)
    if NORMAL in vals and vals[NORMAL].shape[1] >= 3:
        nor = vals[NORMAL][:, :3].astype(np.float32)
        if vals[NORMAL].dtype == np.int8:
            nor = nor / 127.0
        g.normals = nor
    for k in range(8):
        uv = vals.get(TEXCOORD0 + k)
        if uv is None:
            break
        g.uvs.append(uv[:, :2].astype(np.float32))
    if COLOUR0 in vals and vals[COLOUR0].dtype == np.uint8:
        g.colours = vals[COLOUR0][:, :4]
    if WEIGHTS in vals and INDICES in vals:
        w = vals[WEIGHTS]
        g.weights = (w.astype(np.float32) / 255.0) if w.dtype == np.uint8 else w[:, :4].astype(np.float32)
        g.indices = vals[INDICES][:, :4].astype(np.int64)
    if bone_ids_ptr and bone_count:
        g.bone_ids = list(r.unpack(f"{bone_count}H", bone_ids_ptr))
    return g


def read_model(r: Reader, ptr: int) -> Model:
    geoms_ptr, count = r.unpack("QH", ptr + 0x8)
    mapping_ptr = r.u64(ptr + 0x20)
    binding = r.u32(ptr + 0x28)
    mask = r.u16(ptr + 0x2C)
    mapping = list(r.unpack(f"{count}H", mapping_ptr)) if mapping_ptr and count else [0] * count
    geoms = []
    for k, gp in enumerate(r.pointers(geoms_ptr, count)):
        if not gp:
            continue
        g = read_geometry(r, gp)
        g.shader = mapping[k] if k < len(mapping) else 0
        geoms.append(g)
    return Model(geoms, (binding >> 24) & 0xFF, bool((binding >> 8) & 0xFF), mask)


def read_drawable_at(r: Reader, ptr: int, name: str = "") -> Drawable:
    """The drawable block at ``ptr`` (a ``.ydr``'s root, a dictionary entry or a fragment's drawable)."""
    shader_group = r.u64(ptr + 0x10)
    skel = r.u64(ptr + 0x18)
    lod_ptrs = r.unpack("4Q", ptr + 0x50)
    shaders, textures = read_shader_group(r, shader_group)
    try:
        skeleton = read_skeleton(r, skel)
    except FormatError:
        skeleton = None
    lods: dict[str, list[Model]] = {}
    notes = []
    for lod, lp in zip(LODS, lod_ptrs):
        if not lp:
            continue
        models = []
        for mp in r.pointer_list(lp):
            if not mp:
                continue
            try:
                models.append(read_model(r, mp))
            except FormatError as exc:
                notes.append(f"A {lod}-detail model couldn't be read ({exc})")
        if models:
            lods[lod] = models
    for sh in shaders:
        for k, t in sh.embedded.items():
            textures.setdefault(k, t)
    return Drawable(name, shaders, textures, skeleton, lods, notes)


def read_drawable(r: Reader) -> Drawable:
    """A ``.ydr``: its drawable, named as the drawable says."""
    root = 0x50000000
    d = read_drawable_at(r, root)
    try:
        d.name = r.string(r.u64(root + 0xA8))
    except FormatError:
        pass
    return d


def read_dictionary(r: Reader) -> list[tuple[int, Drawable]]:
    """A ``.ydd``: (name hash, drawable) for each entry, in order."""
    root = 0x50000000
    hashes_ptr, hcount = r.unpack("QH", root + 0x20)
    draws_ptr, dcount = r.unpack("QH", root + 0x30)
    hashes = list(r.unpack(f"{hcount}I", hashes_ptr)) if hashes_ptr and hcount else []
    out = []
    for i, dp in enumerate(r.pointers(draws_ptr, dcount)):
        if not dp:
            continue
        d = read_drawable_at(r, dp)
        try:
            d.name = r.string(r.u64(dp + 0xA8))
        except FormatError:
            pass
        out.append((hashes[i] if i < len(hashes) else 0, d))
    return out


@dataclass
class FragmentChild:
    bone_tag: int  # the bone of the main skeleton it sits on
    drawable: Drawable | None
    damaged: Drawable | None = None


@dataclass
class Fragment:
    name: str
    drawable: Drawable | None
    extra: list[Drawable] = field(default_factory=list)
    cloth: Drawable | None = None
    children: list[FragmentChild] = field(default_factory=list)


def _frag_drawable(r: Reader, ptr: int, name: str = "") -> Drawable | None:
    if not ptr or not r.ok(ptr):
        return None
    try:
        d = read_drawable_at(r, ptr, name)
    except FormatError:
        return None
    try:
        own = r.string(r.u64(ptr + 0x130))
    except FormatError:
        own = ""
    d.name = own or name
    return d


def read_fragment(r: Reader) -> Fragment:
    """A ``.yft``: its main drawable, the parts that break off (wheels, panels), the extra drawables it
    names, and a cloth drawable."""
    root = 0x50000000
    dp, arr_ptr, names_ptr, count = r.unpack("QQQI", root + 0x30)
    name = r.string(r.u64(root + 0x58))
    cloth_ptr = r.u64(root + 0xF8)
    main = _frag_drawable(r, dp, name)
    extra = []
    if arr_ptr and count:
        names = r.pointers(names_ptr, count) if names_ptr else [0] * count
        for k, p in enumerate(r.pointers(arr_ptr, count)):
            d = _frag_drawable(r, p, r.string(names[k]) if names[k] else f"{name} {k + 1}")
            if d is not None:
                extra.append(d)
    children = []
    group = r.u64(root + 0xF0)
    if group:
        try:
            lod = r.u64(group + 0x10)
            if lod:
                kids_ptr = r.u64(lod + 0xD0)
                n = r.u8(lod + 0x11D)
                for cp in r.pointers(kids_ptr, n):
                    if not cp:
                        continue
                    tag = r.u16(cp + 0x12)
                    d1, d2 = r.unpack("QQ", cp + 0xA0)
                    children.append(FragmentChild(tag, _frag_drawable(r, d1), _frag_drawable(r, d2)))
        except FormatError:
            children = []
    cloth = _frag_drawable(r, cloth_ptr, name + " cloth")
    return Fragment(name, main, extra, cloth, children)
