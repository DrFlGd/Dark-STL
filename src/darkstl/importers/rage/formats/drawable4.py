"""GTA IV's models and textures, read into the same objects as GTA V's (:mod:`.drawable`, :mod:`.texture`).

``.wdr`` drawables, ``.wdd`` drawable dictionaries (a ped's components), ``.wft`` fragments (vehicles and
anything that breaks) and ``.wtd`` texture dictionaries. GTA V's blocks grew from these: the same parts in
the same order, with 4-byte pointers, so most offsets are about half of GTA V's. The vertex declaration is
the one GTA V's original release keeps, with GTA IV's own component types. Layouts as RageLib (SparkIV,
by Aru) documents them; Dark STL reads them with code of its own.

All offsets below are from the start of each block.

- Texture dictionary (``pgDictionary``): name hashes ``(ptr, count)`` at 0x10, textures ``(ptr, count)``
  at 0x18. Texture (``grcTexturePC``): name at 0x14 (``pack:/name.dds``), width and height at 0x1C, the
  Direct3D format at 0x20, mip levels at 0x27, pixels (graphics part) at 0x48.
- Drawable (``gtaDrawable``): shader group at 0x08, skeleton at 0x0C, the four levels of detail at 0x40.
- Shader group: its own texture dictionary at 0x04, shaders ``(ptr, count)`` at 0x08. Shader: parameter
  pointers at 0x14, count at 0x1C, types (u8: 0 a texture) at 0x24, name hashes at 0x34, then the
  shader's name at 0x44. A texture parameter names its texture at 0x14.
- Model: geometries ``(ptr, count)`` at 0x04, shader per geometry at 0x10, then the skin flag (byte 0x15)
  and the bone it sits on (byte 0x17), as GTA V keeps them.
- Geometry: vertex buffer at 0x0C, index buffer at 0x1C, vertex count at 0x34, stride at 0x3C. Vertex
  buffer: count at 0x04, data at 0x08, stride at 0x0C, declaration at 0x10 (second data pointer 0x18).
  Index buffer: count at 0x04, data at 0x08 (u16).
- Skeleton: bones at 0x00, bone count at 0x14. Bone (0xE0 bytes): name at 0x00, parent at 0x10 (a
  pointer to the parent's bone), bone tag at 0x16, position at 0x20, rotation (quaternion) at 0x40.
- Dictionary (``.wdd``): name hashes at 0x10, drawables at 0x18, as a texture dictionary.
- Fragment (``.wft``): the main drawable at 0xB4, the children's list at 0xD4 and their count (a byte) at
  0x1F3; each child's drawable at 0x90 (children use the main drawable's shaders).
"""

from __future__ import annotations

import numpy as np

from ....errors import FormatError
from .drawable import (
    COLOUR0,
    INDICES,
    LODS,
    NORMAL,
    POSITION,
    TEXCOORD0,
    WEIGHTS,
    Bone,
    Drawable,
    Fragment,
    FragmentChild,
    Geometry,
    Model,
    Shader,
    Skeleton,
    _values,
)
from .rsc import Reader
from .texture import LEGACY_CODECS, Texture, _level_bytes

ROOT = 0x50000000
TEXTURE_PARAM = 0

# GTA IV's vertex component types: (numpy type, count, bytes); 10 is a packed 10:10:10 normal
IV_TYPES = {0: ("<f2", 1, 2), 1: ("<f2", 2, 4), 2: ("<f2", 3, 6), 3: ("<f2", 4, 8), 4: ("<f4", 1, 4),
            5: ("<f4", 2, 8), 6: ("<f4", 3, 12), 7: ("<f4", 4, 16), 8: ("u1", 4, 4), 9: ("u1", 4, 4),
            10: ("<u4", 1, 4)}
IV_SIZES = {k: v[2] for k, v in IV_TYPES.items()}


def texture_key(name: str) -> str:
    """``pack:/admiral_badges.dds`` -> ``admiral_badges``: how textures are matched by name."""
    n = name.strip().replace("\\", "/")
    n = n.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if n.lower().endswith(".dds"):
        n = n[:-4]
    return n.lower()


# --- textures ----------------------------------------------------------------------------------------

def read_texture(r: Reader, ptr: int) -> Texture:
    name = texture_key(r.string(r.u32(ptr + 0x14)))
    w, h, fmt = r.unpack("HHI", ptr + 0x1C)
    levels = r.u8(ptr + 0x27)
    data_ptr = r.u32(ptr + 0x48)
    codec = LEGACY_CODECS.get(fmt)
    tex = Texture(name, w, h, codec, fmt)
    if codec is None or not data_ptr or not r.ok(data_ptr) or not w or not h:
        return tex
    p = data_ptr
    for i in range(max(1, levels)):
        lw, lh = tex.level_size(i)
        n = _level_bytes(codec, lw, lh)
        try:
            tex.levels.append(r.bytes(p, n))
        except FormatError:
            break
        p += n
    return tex


def read_texture_dictionary(r: Reader, ptr: int = ROOT) -> dict[str, Texture]:
    out: dict[str, Texture] = {}
    if not ptr:
        return out
    for tp in r.pointer_list(ptr + 0x18):
        if not tp:
            continue
        try:
            t = read_texture(r, tp)
        except FormatError:
            continue
        out.setdefault(t.name, t)
    return out


# --- shaders -----------------------------------------------------------------------------------------

def _shader(r: Reader, ptr: int) -> Shader:
    params_ptr = r.u32(ptr + 0x14)
    count = r.u32(ptr + 0x1C)
    types_ptr = r.u32(ptr + 0x24)
    shader_hash = r.u32(ptr + 0x28)
    names_ptr = r.u32(ptr + 0x34)
    sh = Shader(shader_hash, 0)
    if not params_ptr or not count or count > 256:
        return sh
    params = r.pointers(params_ptr, count)
    types = r.bytes(types_ptr, count) if types_ptr else bytes(count)
    hashes = list(r.unpack(f"{count}I", names_ptr)) if names_ptr else [0] * count
    for p, t, h in zip(params, types, hashes):
        if t != TEXTURE_PARAM or not p or not r.ok(p):
            continue
        try:
            tname = texture_key(r.string(r.u32(p + 0x14)))
        except FormatError:
            continue
        if tname:
            sh.textures.append((h, tname))
    return sh


def read_shader_group(r: Reader, ptr: int) -> tuple[list[Shader], dict[str, Texture]]:
    if not ptr:
        return [], {}
    tex_dict = r.u32(ptr + 0x04)
    textures = {}
    if tex_dict:
        try:
            textures = read_texture_dictionary(r, tex_dict)
        except FormatError:
            textures = {}
    shaders = []
    for sp in r.pointer_list(ptr + 0x08):
        try:
            shaders.append(_shader(r, sp) if sp else Shader(0, 0))
        except FormatError:
            shaders.append(Shader(0, 0))
    return shaders, textures


# --- skeleton ----------------------------------------------------------------------------------------

BONE_BYTES = 0xE0


def read_skeleton(r: Reader, ptr: int) -> Skeleton | None:
    if not ptr:
        return None
    bones_ptr = r.u32(ptr)
    count = r.u16(ptr + 0x14)
    if not bones_ptr or not count or count > 1024:
        return None
    bones = []
    for i in range(count):
        b = bones_ptr + BONE_BYTES * i
        name = r.string(r.u32(b))
        parent_ptr = r.u32(b + 0x10)
        tag = r.u16(b + 0x16)
        pos = np.array(r.unpack("3f", b + 0x20), np.float64)
        quat = np.array(r.unpack("4f", b + 0x40), np.float64)
        parent = -1
        if parent_ptr and (parent_ptr - bones_ptr) % BONE_BYTES == 0:
            parent = (parent_ptr - bones_ptr) // BONE_BYTES
            if not 0 <= parent < count or parent == i:
                parent = -1
        if not np.all(np.isfinite(quat)) or np.linalg.norm(quat) < 1e-6:
            quat = np.array([0.0, 0.0, 0.0, 1.0])
        bones.append(Bone(name or f"bone {i}", tag, parent, quat, pos, np.ones(3)))
    return Skeleton(bones)


# --- geometry ----------------------------------------------------------------------------------------

def _read_vertices(r: Reader, vb: int) -> tuple[int, dict[int, np.ndarray]]:
    count = r.u16(vb + 0x04)
    data_ptr = r.u32(vb + 0x08) or r.u32(vb + 0x18)
    stride = r.u32(vb + 0x0C)
    decl = r.u32(vb + 0x10)
    if not count or not stride or not data_ptr or not decl:
        return 0, {}
    flags, dstride, _alt, _kind, types = r.unpack("IHBBQ", decl)
    stride = stride or dstride
    data = r.bytes(data_ptr, count * stride)
    out, off = {}, 0
    for k in range(16):
        if not flags >> k & 1:
            continue
        t = (types >> (4 * k)) & 0xF
        if t not in IV_TYPES:
            break  # an unknown size: the rest can't be placed
        dtype, comps, size = IV_TYPES[t]
        if off + size <= stride:
            v = _values(data, count, stride, off, dtype, comps)
            if t == 10:  # 10:10:10, signed
                u = v[:, 0].astype(np.int64)
                parts = []
                for s in (0, 10, 20):
                    x = (u >> s) & 0x3FF
                    parts.append(np.where(x >= 512, x - 1024, x).astype(np.float32) / 511.0)
                v = np.stack(parts, -1)
            out[k] = v
        off += size
    return count, out


def read_geometry(r: Reader, ptr: int) -> Geometry:
    vb = r.u32(ptr + 0x0C)
    ib = r.u32(ptr + 0x1C)
    if not vb or not ib:
        raise FormatError("A geometry has no vertex or index buffer")
    n, vals = _read_vertices(r, vb)
    if POSITION not in vals:
        raise FormatError("A geometry has no positions")
    icount = r.u32(ib + 0x04)
    iptr = r.u32(ib + 0x08)
    idx = np.frombuffer(r.bytes(iptr, 2 * icount), "<u2").astype(np.int64) if icount and iptr else \
        np.zeros(0, np.int64)
    tri = idx[: len(idx) // 3 * 3].reshape(-1, 3)
    tri = tri[(tri < n).all(axis=1)]
    g = Geometry(vals[POSITION][:, :3].astype(np.float32), tri)
    if NORMAL in vals and vals[NORMAL].shape[1] >= 3:
        g.normals = vals[NORMAL][:, :3].astype(np.float32)
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
    return g


def read_model(r: Reader, ptr: int) -> Model:
    geoms_ptr, count = r.unpack("IH", ptr + 0x04)
    mapping_ptr = r.u32(ptr + 0x10)
    _a, skinned, _b, bone = r.unpack("4B", ptr + 0x14)
    mapping = list(r.unpack(f"{count}H", mapping_ptr)) if mapping_ptr and count else [0] * count
    geoms = []
    for k, gp in enumerate(r.pointers(geoms_ptr, count)):
        if not gp:
            continue
        g = read_geometry(r, gp)
        g.shader = mapping[k] if k < len(mapping) else 0
        geoms.append(g)
    return Model(geoms, bone, bool(skinned))


def read_drawable_at(r: Reader, ptr: int, name: str = "", shaders: tuple | None = None) -> Drawable:
    sg = r.u32(ptr + 0x08)
    skel = r.u32(ptr + 0x0C)
    lod_ptrs = r.unpack("4I", ptr + 0x40)
    own_shaders, textures = read_shader_group(r, sg)
    if not own_shaders and shaders is not None:
        own_shaders, textures = shaders
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
    return Drawable(name, own_shaders, textures, skeleton, lods, notes)


def read_drawable(r: Reader) -> Drawable:
    return read_drawable_at(r, ROOT)


def read_dictionary(r: Reader) -> list[tuple[int, Drawable]]:
    hashes_ptr, hcount = r.unpack("IH", ROOT + 0x10)
    hashes = list(r.unpack(f"{hcount}I", hashes_ptr)) if hashes_ptr and hcount else []
    out = []
    for i, dp in enumerate(r.pointer_list(ROOT + 0x18)):
        if not dp:
            continue
        out.append((hashes[i] if i < len(hashes) else 0, read_drawable_at(r, dp)))
    return out


def dictionary_hashes(r: Reader) -> list[int]:
    hashes_ptr, hcount = r.unpack("IH", ROOT + 0x10)
    return list(r.unpack(f"{hcount}I", hashes_ptr)) if hashes_ptr and hcount else []


def read_fragment(r: Reader, name: str = "") -> Fragment:
    dp = r.u32(ROOT + 0xB4)
    if not dp:
        raise FormatError("The fragment has no drawable")
    main = read_drawable_at(r, dp, name)
    children = []
    try:
        count = r.u8(ROOT + 0x1F3)
        kids = r.u32(ROOT + 0xD4)
        bones = main.skeleton.bones if main.skeleton is not None else []
        for cp in r.pointers(kids, count):
            if not cp:
                continue
            cd = r.u32(cp + 0x90)
            if not cd or not r.ok(cd):
                continue
            d = read_drawable_at(r, cd, "", (main.shaders, main.textures))
            d.shaders, d.textures = main.shaders, main.textures
            models = d.models()
            bi = models[0].bone if models else 0
            tag = bones[bi].tag if 0 <= bi < len(bones) else 0
            children.append(FragmentChild(tag, d))
    except FormatError:
        children = []
    return Fragment(name, main, [], None, children)
