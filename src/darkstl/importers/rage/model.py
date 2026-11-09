"""Opening a GTA V model: a ``.ydr``, one drawable of a ``.ydd``, or a ``.yft`` (vehicles, weapons, props).

Each model of the drawable (high detail) is a part, named after the bone it sits on; unskinned models are
placed by that bone. A vehicle's wheels and loose panels are the fragment's children, placed on their bones
(the game draws the one wheel it stores on every wheel bone, turned round for the right-hand side). Peds'
components are skinned to the skeleton in the ped's ``.yft`` and posed with the game's clips.

Textures come from the drawable's own dictionary, then the ``.ytd`` of the model's name (and its ``+hi``
dictionary), the parents ``gtxd.meta`` gives, the vehicle's ``txdName`` and the shared vehicle
dictionaries, and last the other dictionaries in the same archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...geometry.mesh import TriMesh, weld
from ...model import Material, MeshOptions, Part, SourceModel, Texture
from ...posing import blend
from . import files
from .animation import RageAnimations, RageSkeleton, rig
from .formats import clip as clipfmt
from .formats import drawable as dfmt
from .formats import rsc
from .formats import texture as tfmt
from .formats.text import joaat

SLOTS = ("head", "berd", "hair", "uppr", "lowr", "hand", "feet", "teef", "accs", "task", "decl", "jbib")
SLOT_LABELS = {"head": "Head", "berd": "Mask", "hair": "Hair", "uppr": "Upper body", "lowr": "Legs",
               "hand": "Hands", "feet": "Feet", "teef": "Teeth", "accs": "Accessories", "task": "Gear",
               "decl": "Decals", "jbib": "Top"}

DIFFUSE = {joaat(n) for n in ("DiffuseSampler", "DiffuseSampler2", "TextureSampler", "DiffuseTexture",
                              "PlateBgSampler", "DiffuseExtraSampler")}
NORMAL = {joaat(n) for n in ("BumpSampler", "BumpSampler2", "NormalSampler", "BumpTexture")}
SPECULAR = {joaat(n) for n in ("SpecSampler", "SpecularSampler", "SpecTexture")}
DETAIL = {joaat(n) for n in ("DetailSampler",)}

WHEEL_FRONT = {27922, 26418}  # wheel_lf, wheel_rf
WHEEL_REAR = {29921, 29922, 29923, 27902, 5857, 5858, 5859, 26398}
WHEEL_RIGHT = {26418, 5857, 5858, 5859, 26398}


@lru_cache(maxsize=1)
def component_names() -> dict[int, str]:
    """Hash -> name for the names a ped's component drawables have (``uppr_001_u``)."""
    out = {}
    for s in SLOTS:
        for i in range(256):
            for suffix in ("u", "r", "m"):
                n = f"{s}_{i:03d}_{suffix}"
                out[joaat(n)] = n
    return out


def drawable_names(entries: list[tuple[int, dfmt.Drawable]]) -> list[str]:
    known = component_names()
    names = []
    for h, d in entries:
        n = d.name.split(".")[0] if d.name else known.get(h, f"{h:08x}")
        base, k = n, 2
        while n in names:
            n = f"{base} ({k})"
            k += 1
        names.append(n)
    return names


# -- reading --------------------------------------------------------------------------------------------

@dataclass
class Placed:
    """One model of the file, where it goes."""

    name: str
    drawable: dfmt.Drawable
    model: dfmt.Model
    matrix: np.ndarray  # into the model's space (unskinned models)
    owner: int  # which drawable (for its shaders)


def _reader(gf: files.GameFiles, path: Path) -> tuple[rsc.Reader, rsc.Resource]:
    res = gf.resource(path)
    ext = Path(path).suffix.lower()
    return res.reader(rsc.is_enhanced(ext, res.version)), res


def read_file(gf: files.GameFiles, path: Path):
    """What a model file holds: ("ydr", Drawable), ("ydd", [(hash, Drawable)]) or ("yft", Fragment)."""
    ext = Path(path).suffix.lower()
    r, res = _reader(gf, path)
    if ext == ".ydr":
        return "ydr", dfmt.read_drawable(r), res
    if ext == ".ydd":
        return "ydd", dfmt.read_dictionary(r), res
    if ext == ".yft":
        return "yft", dfmt.read_fragment(r), res
    raise FormatError(f"{Path(path).name} isn't a GTA V model")


def ydd_names(gf: files.GameFiles, path: Path) -> list[str]:
    """The drawables a ``.ydd`` holds, by name, read from its system part only."""
    rel = gf.rel(path)
    if rel in gf.components:
        return gf.components[rel]
    res = gf.resource_system(path)
    r = res.reader(rsc.is_enhanced(".ydd", res.version))
    root = 0x50000000
    hashes_ptr, hcount = r.unpack("QH", root + 0x20)
    hashes = list(r.unpack(f"{hcount}I", hashes_ptr)) if hashes_ptr and hcount else []
    known = component_names()
    names = []
    for h in hashes:
        n = known.get(h, f"{h:08x}")
        while n in names:
            n += "+"
        names.append(n)
    if rel is not None:
        gf.components[rel] = names
    return names


def ped_name(gf: files.GameFiles, path: Path) -> str:
    """The ped a model belongs to: its own name, or the folder or prefix of a streamed component."""
    from .catalog import _component_ped

    rel = gf.rel(path) or Path(path).name.lower()
    ped = _component_ped(rel)
    return ped or Path(path).stem.lower()


def ped_skeleton(gf: files.GameFiles, path: Path) -> dfmt.Skeleton | None:
    """The skeleton in the ped's ``.yft``."""
    name = ped_name(gf, path)
    p = gf.find(name + ".yft")
    if p is None:
        return None
    try:
        _k, frag, _res = read_file(gf, p)
    except (FormatError, OSError):
        return None
    if frag.drawable is not None and frag.drawable.skeleton is not None:
        return frag.drawable.skeleton
    return None


# -- textures -----------------------------------------------------------------------------------------

class TextureFinder:
    def __init__(self, gf: files.GameFiles, path: Path, own: list[dict[str, tfmt.Texture]], vehicle: bool):
        self.gf = gf
        self.own = own
        self.chain = texture_chain(gf, path, vehicle)
        self.loaded: dict[Path, dict[str, tfmt.Texture] | None] = {}
        self.used: list[Path] = []
        self.path = path

    def _dict(self, p: Path) -> dict[str, tfmt.Texture] | None:
        if p not in self.loaded:
            try:
                r, _res = _reader(self.gf, p)
                self.loaded[p] = tfmt.read_dictionary(r, 0x50000000)
            except (FormatError, OSError):
                self.loaded[p] = None
        return self.loaded[p]

    def find(self, name: str) -> tuple[tfmt.Texture, str] | None:
        key = name.lower()
        for d in self.own:
            t = d.get(key)
            if t is not None and t.levels:
                return t, Path(self.path).name
        for p in self.chain:
            d = self._dict(p)
            t = d.get(key) if d else None
            if t is not None and t.levels:
                if p not in self.used:
                    self.used.append(p)
                return t, p.name
        return None


def texture_chain(gf: files.GameFiles, path: Path, vehicle: bool) -> list[Path]:
    """The dictionaries a model's textures are looked for in, in order."""
    stem = Path(path).stem.lower()
    base = stem.removesuffix("_hi")
    names: list[str] = []

    def add(n: str) -> None:
        if n and n not in names:
            names.append(n)

    if vehicle:
        add(base + "+hi")
    add(stem)
    add(base)
    if "^" in stem:
        add(stem.split("^", 1)[1])
    ped = ped_name(gf, path)
    if ped != stem:
        add(ped)
    info = gf.vehicles.get(base)
    if info is not None and info.txd:
        add(info.txd + "+hi")
        add(info.txd)
    k = 0
    for n in names:
        p = n
        while p in gf.txd_parents and k < 32:
            p = gf.txd_parents[p]
            add(p)
            k += 1
    if vehicle and gf.profile is not None:
        for n in gf.profile.vehicle_textures:
            add(n)
    if gf.profile is not None:
        for n in gf.profile.shared_textures:
            add(n)
    out = []
    for n in names:
        for p in gf.find_all(n + ".ytd")[:1]:
            out.append(p)
    # last: the other dictionaries beside the model (map pieces share them)
    here = gf.rel(path)
    if here is not None:
        folder = here.rsplit("/", 1)[0] + "/"
        extra = [rel for rel in list(gf.members) + list(gf.loose)
                 if rel.startswith(folder) and rel.endswith(".ytd") and "/" not in rel[len(folder):]]
        for rel in sorted(extra)[:12]:
            p = gf.path(rel)
            if p not in out:
                out.append(p)
    return out


def _texture(name: str, tex: tfmt.Texture, source: str) -> Texture:
    return Texture(tex.name, source, lambda t=tex: t.decode(0),
                   lambda t=tex: f"{t.width} x {t.height}, {t.codec}, {len(t.levels)} mips",
                   raw=lambda t=tex: t.to_dds())


def _material(shader: dfmt.Shader, label: str, finder: TextureFinder, textures: dict, missing: list) -> Material:
    roles: dict[str, str] = {}
    details = []
    guessed = False
    for h, tname in shader.textures:
        low = tname.lower()
        role = ("albedo" if h in DIFFUSE else "normal" if h in NORMAL else "specular" if h in SPECULAR
                else "detail_normal" if h in DETAIL else None)
        if role is None:  # unnamed parameters (GTA V Enhanced): by the texture's name
            guessed = True
            role = ("normal" if low.endswith(("_n", "_nm", "_normal")) else
                    "specular" if low.endswith(("_s", "_spec")) else
                    None if low.endswith(("_d2", "_detail", "_m", "_mask")) else "albedo")
        details.append(f"{role or 'other'}: {tname}")
        if role is None or role in roles:
            continue
        if low not in textures:
            got = finder.find(low)
            if got is None:
                if low not in missing and not low.startswith(("givemechecker", "vehicle_genericmud",
                                                              "nxg_", "dummy")):
                    missing.append(low)
                continue
            textures[low] = _texture(low, got[0], got[1])
        roles[role] = low
    return Material(label, roles.get("albedo"), roles.get("normal"), None, guessed=guessed, details=details,
                    normal_green="directx", specular=roles.get("specular"), detail_normal=roles.get("detail_normal"))


# -- loading ----------------------------------------------------------------------------------------------

def _bone_world(skel: dfmt.Skeleton | None) -> np.ndarray | None:
    return skel.world() if skel is not None and skel.bones else None


def load(importer_key: str, path: Path, variant: str | None = None, cache=None) -> SourceModel:
    path = Path(path)
    gf = files.for_path(path)
    kind, data, res = read_file(gf, path)
    ext = path.suffix.lower()
    gen9 = rsc.is_enhanced(ext, res.version)
    stem = path.stem.lower()
    variants: list[str] = []
    drawables: list[dfmt.Drawable] = []
    children: list[dfmt.FragmentChild] = []
    if kind == "ydr":
        drawables = [data]
    elif kind == "ydd":
        variants = drawable_names(data)
        if not data:
            raise FormatError(f"{path.name} holds no drawables")
        pick = variants.index(variant) if variant in variants else 0
        drawables = [data[pick][1]]
        variant = variants[pick]
    else:
        if data.drawable is None:
            raise FormatError(f"{path.name} has no drawable")
        drawables = [data.drawable]
        children = [c for c in data.children]
        if data.cloth is not None and data.cloth.models():
            drawables.append(data.cloth)
    main = drawables[0]
    skel = main.skeleton
    is_ped = kind == "ydd" or ("peds" in (gf.rel(path) or "") and kind != "yft")
    if (skel is None or not skel.bones) and is_ped:
        skel = ped_skeleton(gf, path)
    world = _bone_world(skel)
    vehicle = kind == "yft" and ("vehicles" in (gf.rel(path) or "") or stem.removesuffix("_hi") in gf.vehicles)

    def bone_matrix(i: int) -> np.ndarray:
        return world[i] if world is not None and 0 <= i < len(world) else np.eye(4)

    placed: list[Placed] = []
    for di, d in enumerate(drawables):
        for mi, m in enumerate(d.models("high")):
            bone_name = skel.bones[m.bone].name if skel is not None and m.bone < len(skel.bones) else ""
            label = bone_name if bone_name and not m.skinned else (d.name or stem)
            if len(d.models("high")) > 1 and label == (d.name or stem):
                label = f"{label} {mi + 1}"
            placed.append(Placed(label, d, m, np.eye(4) if m.skinned else bone_matrix(m.bone), di))
    # a fragment's children: wheels and panels, on their bones; the stored wheels drawn on every wheel bone
    if children and skel is not None:
        tags = skel.by_tag()
        front = next((c.drawable for c in children if c.bone_tag in WHEEL_FRONT and c.drawable is not None
                      and c.drawable.models()), None)
        rear = next((c.drawable for c in children if c.bone_tag in WHEEL_REAR and c.drawable is not None
                     and c.drawable.models()), None)
        for c in children:
            d = c.drawable
            if (d is None or not d.models()) and c.bone_tag in WHEEL_FRONT | WHEEL_REAR:
                d = (front or rear) if c.bone_tag in WHEEL_FRONT else (rear or front)
            if d is None or not d.models():
                continue
            bi = tags.get(c.bone_tag)
            if bi is None:
                continue
            m = bone_matrix(bi).copy()
            if c.bone_tag in WHEEL_RIGHT:
                m[:3, :3] = m[:3, :3] @ np.diag([-1.0, 1.0, -1.0])
            if d not in drawables:
                drawables.append(d)
            for mi, mod in enumerate(d.models("high")):
                placed.append(Placed(skel.bones[bi].name, d, mod, m, drawables.index(d)))
    # materials, one per (drawable, shader)
    own = [d.textures for d in drawables]
    finder = TextureFinder(gf, path, own, vehicle)
    materials: list[Material] = []
    mat_index: dict[tuple[int, int], int] = {}
    textures: dict[str, Texture] = {}
    missing: list[str] = []
    for p in placed:
        for g in p.model.geometries:
            key = (p.owner, g.shader)
            if key in mat_index:
                continue
            shaders = drawables[p.owner].shaders
            sh = shaders[g.shader] if g.shader < len(shaders) else dfmt.Shader(0, 0)
            base = drawables[p.owner].name or (p.name if p.owner else stem)
            label = f"{base} {g.shader + 1}" if len(shaders) > 1 else base
            mat_index[key] = len(materials)
            materials.append(_material(sh, label, finder, textures, missing))
    parts = [Part(p.name, mat_index.get((p.owner, p.model.geometries[0].shader), 0) if p.model.geometries else 0,
                  sum(len(g.positions) for g in p.model.geometries),
                  sum(len(g.triangles) for g in p.model.geometries)) for p in placed]
    # posing
    anims = None
    the_rig = None
    pose_note = None
    skinned = any(p.model.skinned for p in placed)
    if skel is not None and skel.bones and skinned:
        rs = RageSkeleton(skel)
        dicts = _clip_dictionaries(gf, path, is_ped)
        if dicts:
            anims = RageAnimations(rs, dicts)
            the_rig = rig(rs, anims)
        else:
            pose_note = ("No animations were found for this model: the game's clip dictionaries (.ycd, in the "
                         "anim archives) are needed to pose it.")
    skin_skeleton = RageSkeleton(skel) if skel is not None and skel.bones and skinned else None

    def build(options: MeshOptions) -> TriMesh:
        skin_now = None
        if options.pose is not None:
            if anims is None:
                raise FormatError(f"{stem} has no animations to pose it with")
            name, frame = options.pose
            skin_now = anims.skin_matrices(name, anims.clamp(name, frame))
        verts, norms, faces, uvs, mats, groups, joints, weights = [], [], [], [], [], [], [], []
        offset = 0
        for i, p in enumerate(placed):
            if options.parts is not None and i not in options.parts:
                continue
            for g in p.model.geometries:
                if not len(g.triangles):
                    continue
                pos = g.positions.astype(np.float64) @ p.matrix[:3, :3].T + p.matrix[:3, 3]
                nor = (g.normals.astype(np.float64) @ p.matrix[:3, :3].T) if g.normals is not None else \
                    _vertex_normals(pos, g.triangles)
                j = np.full((len(pos), 4), -1, np.int64)
                w = np.zeros((len(pos), 4), np.float32)
                if p.model.skinned and g.weights is not None and g.indices is not None:
                    j = g.indices.astype(np.int64)
                    if g.bone_ids:
                        lut = np.array(g.bone_ids, np.int64)
                        j = np.where(j < len(lut), lut[np.clip(j, 0, len(lut) - 1)], j)
                    w = g.weights.astype(np.float32)
                    j = np.where(w > 0, j, -1)
                    if skin_now is not None:
                        blended, has = blend(j, w, skin_now)
                        pos = np.where(has[:, None], np.einsum("nij,nj->ni", blended[:, :3, :3], pos)
                                       + blended[:, :3, 3], pos)
                        nor = np.where(has[:, None], np.einsum("nij,nj->ni", blended[:, :3, :3], nor), nor)
                verts.append(pos)
                norms.append(nor)
                faces.append(g.triangles + offset)
                uv = g.uvs[0] if g.uvs else np.zeros((len(pos), 2), np.float32)
                uvs.append(uv[g.triangles])
                mats.append(np.full(len(g.triangles), mat_index.get((p.owner, g.shader), 0), np.int32))
                groups.append(np.full(len(g.triangles), i, np.int32))
                joints.append(j)
                weights.append(w)
                offset += len(pos)
        if not faces:
            return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
        v = np.concatenate(verts)
        n = np.concatenate(norms)
        f = np.concatenate(faces)
        length = np.linalg.norm(n, axis=1, keepdims=True)
        n = n / np.where(length > 0, length, 1)
        out = TriMesh(v, f, np.concatenate(uvs).astype(np.float32), np.concatenate(mats), np.concatenate(groups),
                      corner_normals=n[f].astype(np.float32))
        if skin_skeleton is not None and skin_now is None:
            out.metadata["skin"] = (np.concatenate(joints), np.concatenate(weights))
        flip = np.einsum("ij,ij->i", out.face_cross(), out.corner_normals.sum(axis=1)) < 0
        if flip.any():
            out.faces[flip] = out.faces[flip][:, [0, 2, 1]]
            out.corner_uvs[flip] = out.corner_uvs[flip][:, [0, 2, 1]]
            out.corner_normals[flip] = out.corner_normals[flip][:, [0, 2, 1]]
        return weld(out)

    notes = [n for d in drawables for n in d.notes]
    if missing:
        notes.append(f"{len(missing)} texture{'s' if len(missing) != 1 else ''} weren't found in "
                     f"{', '.join(p.name for p in finder.chain[:6]) or 'any texture dictionary'}: "
                     f"{', '.join(missing[:8])}" + ("..." if len(missing) > 8 else ""))
    if vehicle:
        notes.append("Paint colours are chosen by the game at run time, so painted panels show the base texture.")
    info = _info(gf, path, kind, res, gen9, drawables, placed, skel, finder)
    label = _label(gf, path)
    return SourceModel(
        name=label or (variant or stem), path=path, importer=importer_key, build=build,
        variant=variant if variants else None, variants=variants, game=gf.game, parts=parts,
        materials=materials, textures=textures, info=info, notes=notes, animations=anims, pose_note=pose_note,
        rig=the_rig,
    )


def _label(gf: files.GameFiles, path: Path) -> str | None:
    from .catalog import classify

    rel = gf.rel(path)
    if rel is None:
        return None
    c = classify(gf, rel)
    return c[2] if c else None


def _info(gf, path, kind, res, gen9, drawables, placed, skel, finder) -> list[str]:
    what = {"ydr": "drawable", "ydd": "drawable dictionary", "yft": "fragment"}[kind]
    verts = sum(len(g.positions) for p in placed for g in p.model.geometries)
    tris = sum(len(g.triangles) for p in placed for g in p.model.geometries)
    lines = [f"GTA V {what} (resource version {res.version}, {'Enhanced' if gen9 else 'original PC'} layout)"
             + (f" ({gf.game})" if gf.game else ""),
             f"{len(placed)} models, {verts:,} vertices, {tris:,} triangles"]
    arc = gf.archive_of(path)
    if arc:
        lines.append(f"in {arc}")
    if skel is not None and skel.bones:
        lines.append(f"skeleton: {len(skel.bones)} bones")
    if finder.used:
        lines.append("textures from " + ", ".join(p.name for p in finder.used))
    return lines


def _vertex_normals(pos: np.ndarray, tri: np.ndarray) -> np.ndarray:
    n = np.zeros_like(pos)
    if len(tri):
        fn = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
        for k in range(3):
            np.add.at(n, tri[:, k], fn)
    return n


def _clip_dictionaries(gf: files.GameFiles, path: Path, is_ped: bool) -> list[tuple[str, callable]]:
    """The clip dictionaries a model is posed with: its own name's, then (characters) the profile's list."""
    names = [Path(path).stem.lower()]
    if is_ped and gf.profile is not None:
        names += gf.profile.ped_animations
    out = []
    for n in dict.fromkeys(names):
        p = gf.find(n + ".ycd")
        if p is None:
            continue

        def read(p=p):
            res = gf.resource(p)
            return clipfmt.read_clips(res.reader(False))

        out.append((p.name, read))
    return out


def animation_files(gf: files.GameFiles, path: Path) -> list[Path]:
    is_ped = Path(path).suffix.lower() == ".ydd" or "peds" in (gf.rel(path) or "")
    out = []
    for n, _r in _clip_dictionaries(gf, path, is_ped):
        p = gf.find(n)
        if p is not None:
            out.append(p)
    return out
