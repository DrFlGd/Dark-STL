"""Putting a RenderWare clump together as one model: parts placed by their frames, vehicles' wheels
copied to every axle, materials with the textures they read, a car's colours, and peds' skins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ...geometry.mesh import TriMesh, weld
from ...model import BuildOption, Material, MeshOptions, Part, Rig, SourceModel, Texture
from ...posing import blend
from ...textures.dds import build_dds
from . import files
from .animation import RWAnimations, Skeleton
from .formats import dff as dfffmt
from .formats import txd as txdfmt

DAMAGED = re.compile(r"_dam$", re.IGNORECASE)
LOW_DETAIL = re.compile(r"_vlo$|_lo$|_lod$", re.IGNORECASE)
WHEEL_DUMMY = re.compile(r"^wheel_([lr])([fbm])_dummy$", re.IGNORECASE)
UNPAINTED = (190, 190, 190)  # a car's paint where the game's colour list names none

OPTIONS = [
    BuildOption("damaged", "Damaged panels", False,
                "Vehicles carry a damaged copy of each door, bonnet and bumper. Leave this off to build the car "
                "with its undamaged panels only."),
    BuildOption("low_detail", "Low-detail copies", False,
                "Vehicles carry a simpler copy of the body for far away. Leave this off so it doesn't overlap "
                "the full model."),
    BuildOption("wheels", "A wheel on every axle", True,
                "The game keeps one wheel and copies it to each axle when it draws the car; Dark STL does the "
                "same. Turn it off to keep only the stored wheel."),
]


@dataclass
class PlacedPart:
    name: str
    geometry: int
    matrix: np.ndarray
    flag: str | None = None  # "damaged" or "low_detail": left out unless that option is on
    copy: bool = False  # a wheel copied to another axle
    frame: int = -1


def _rot_z180() -> np.ndarray:
    m = np.eye(4)
    m[0, 0] = m[1, 1] = -1
    return m


def plan_parts(clump: dfffmt.Clump) -> list[PlacedPart]:
    world = clump.frame_world()
    names = [f.name for f in clump.frames]
    out = []
    wheel = None
    for i, a in enumerate(clump.atomics):
        if not 0 <= a.geometry < len(clump.geometries):
            continue
        fname = names[a.frame] if 0 <= a.frame < len(names) else ""
        m = world[a.frame] if 0 <= a.frame < len(world) else np.eye(4)
        flag = "damaged" if DAMAGED.search(fname) else "low_detail" if LOW_DETAIL.search(fname) else None
        out.append(PlacedPart(fname or f"part {i + 1}", a.geometry, m, flag, frame=a.frame))
        if wheel is None and fname.lower().startswith("wheel") and flag is None:
            wheel = out[-1]
    if wheel is not None:
        # the dummy the stored wheel hangs from (its own frame when it is the dummy)
        f = wheel.frame
        dummy = f if WHEEL_DUMMY.match(names[f]) else clump.frames[f].parent
        if 0 <= dummy < len(names) and WHEEL_DUMMY.match(names[dummy]):
            rel = np.linalg.solve(world[dummy], world[f])
            stored_left = WHEEL_DUMMY.match(names[dummy]).group(1).lower() == "l"
            dummies = [j for j, n in enumerate(names) if WHEEL_DUMMY.match(n)]
            for j in dummies:
                m = WHEEL_DUMMY.match(names[j])
                if j == dummy:
                    continue
                if m.group(2).lower() == "m" and not _own_axle(j, dummies, world, clump):
                    continue  # most cars keep middle dummies they draw no wheel at (on another axle, or at 0)
                left = m.group(1).lower() == "l"
                turn = _rot_z180() if left != stored_left else np.eye(4)
                side = {"lf": "left front", "rf": "right front", "lb": "left back", "rb": "right back",
                        "lm": "left middle", "rm": "right middle"}[(m.group(1) + m.group(2)).lower()]
                out.append(PlacedPart(f"wheel ({side})", wheel.geometry, world[j] @ turn @ rel, None, True, j))
    return out


def _own_axle(j: int, dummies: list[int], world: np.ndarray, clump: dfffmt.Clump) -> bool:
    """Whether a middle wheel dummy marks an axle of its own (a six-wheeler's), not one it shares with
    the front or back wheels or the car's centre."""
    if np.linalg.norm(clump.frames[j].position) < 0.05:
        return False
    here = world[j][:3, 3]
    for k in dummies:
        if k != j and abs(world[k][1, 3] - here[1]) < 0.2:
            name = clump.frames[k].name.lower()
            if not name.endswith("m_dummy"):
                return False
    return True


def _vertex_normals(positions: np.ndarray, tris: np.ndarray) -> np.ndarray:
    n = np.zeros_like(positions, np.float64)
    if len(tris):
        a, b, c = positions[tris[:, 0]], positions[tris[:, 1]], positions[tris[:, 2]]
        cross = np.cross(b - a, c - a)
        for k in range(3):
            np.add.at(n, tris[:, k], cross)
    length = np.linalg.norm(n, axis=1, keepdims=True)
    return n / np.where(length > 0, length, 1)


# ---------------------------------------------------------------------------
# materials and textures


@dataclass
class TextureFinder:
    """Looks a texture up by name in the dictionaries a model reads, in order."""

    chain: list[Path]
    used: list[Path] = field(default_factory=list)
    _dicts: dict = field(default_factory=dict)

    def find(self, name: str) -> tuple[Path, txdfmt.NativeTexture] | None:
        low = name.lower()
        for p in self.chain:
            d = self._dicts.get(p)
            if d is None:
                try:
                    d = files.read_txd(p).by_name()
                except FormatError:
                    d = {}
                self._dicts[p] = d
            t = d.get(low)
            if t is not None:
                if p not in self.used:
                    self.used.append(p)
                return p, t
        return None


def _game_texture(path: Path, tex: txdfmt.NativeTexture) -> Texture:
    src = path.name if files.split_member(path) is None else f"{path.parent.name}/{path.name}"
    return Texture(tex.name, src, lambda t=tex: t.decode(0), t_desc(tex), raw=lambda t=tex: t.to_dds())


def t_desc(tex: txdfmt.NativeTexture):
    return lambda: tex.describe()


def _tinted(base: Texture | None, colour: tuple[int, int, int], name: str) -> Texture:
    c = np.array(colour, np.float32) / 255.0

    def load() -> np.ndarray:
        if base is None:
            img = np.full((4, 4, 4), 255, np.uint8)
        else:
            img = np.asarray(base.decode())
            if img.dtype != np.uint8:
                img = np.clip(img * 255, 0, 255).astype(np.uint8)
        out = img.astype(np.float32)
        out[..., :3] *= c
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    def raw() -> bytes:
        img = np.ascontiguousarray(load())
        return build_dds("R8G8B8A8", img.shape[1], img.shape[0], img.tobytes())

    src = base.source if base is not None else "material colour"
    return Texture(name, src, load, lambda: f"{base.describe() if base else '4 x 4'}, coloured", raw=raw)


@dataclass
class Look:
    """What a material looks like: its texture (by name) and the colour it is multiplied by."""

    texture: str | None
    colour: tuple[int, int, int]
    paint: int | None = None  # which of the car's colours it takes, when it is painted


def material_look(m: dfffmt.Material, paint: list[tuple[int, int, int]], lights: list[tuple[int, int, int]],
                  vehicle: bool) -> Look:
    rgb = tuple(int(v) for v in m.colour[:3])
    if vehicle:
        if rgb in paint:
            return Look(m.texture, rgb, paint.index(rgb))
        if rgb in lights:
            return Look(m.texture, (255, 255, 255))
    return Look(m.texture, rgb)


def paint_variants(combos: list[tuple[tuple[int, int, int], ...]]) -> list[str]:
    return [f"Paint {i + 1} ({', '.join('#%02x%02x%02x' % c for c in combo)})" for i, combo in enumerate(combos)]


# ---------------------------------------------------------------------------


def load(importer_key: str, path: Path, variant: str | None, cache: Path | None) -> SourceModel:
    path = Path(path)
    gf = files.for_path(path)
    data = files.read_bytes(path, "model")
    clump = dfffmt.read_clump(data)
    stem = path.name[:-4] if path.name.lower().endswith(".dff") else path.stem
    d = gf.definition(stem)
    names = {f.name.lower() for f in clump.frames}
    vehicle = (d is not None and d.section == "cars") or "chassis_dummy" in names or "chassis" in names
    placed = plan_parts(clump)
    profile = gf.profile
    paint_slots = list(profile.paint) if profile else [(60, 255, 0), (255, 0, 175)]
    lights = list(profile.lights) if profile else []

    combos = gf.colours.combinations(stem) if vehicle else []
    variants = paint_variants(combos)
    chosen = combos[variants.index(variant)] if variant in variants else (combos[0] if combos else ())

    chain = gf.texture_chain(stem, vehicle)
    if not chain and gf.profile is None:
        chain = [ps[0] for n, ps in gf.names.items() if n.endswith(files.TEXTURE_EXT)]
    finder = TextureFinder(chain)
    textures: dict[str, Texture] = {}
    materials: list[Material] = []
    mat_index: dict[tuple, int] = {}
    missing: list[str] = []
    geo_mats: list[list[int]] = []

    def base_texture(name: str | None) -> Texture | None:
        if not name:
            return None
        key = name.lower()
        if key in textures:
            return textures[key]
        hit = finder.find(name)
        if hit is None:
            if name not in missing:
                missing.append(name)
            return None
        textures[key] = _game_texture(*hit)
        return textures[key]

    for g in clump.geometries:
        idx = []
        for m in g.materials:
            look = material_look(m, paint_slots, lights, vehicle)
            colour = look.colour
            if look.paint is not None:
                colour = chosen[look.paint] if look.paint < len(chosen) else UNPAINTED
            key = (look.texture.lower() if look.texture else None, colour)
            if key not in mat_index:
                base = base_texture(look.texture)
                tex_key = None
                if colour == (255, 255, 255) and base is not None:
                    tex_key = look.texture.lower()
                elif colour != (255, 255, 255) or base is not None:
                    tex_key = f"{(look.texture or 'colour').lower()}#{'%02x%02x%02x' % colour}"
                    textures[tex_key] = _tinted(base, colour, tex_key)
                label = look.texture or "untextured"
                if look.paint is not None:
                    label += f" (paint {look.paint + 1})"
                elif colour != (255, 255, 255):
                    label += " (#%02x%02x%02x)" % colour
                details = [f"texture: {look.texture or 'none'}", "colour: #%02x%02x%02x" % colour]
                if m.mask:
                    details.append(f"mask: {m.mask}")
                mat_index[key] = len(materials)
                materials.append(Material(label, albedo=tex_key, details=details))
            idx.append(mat_index[key])
        geo_mats.append(idx or [_plain(materials, mat_index)])

    parts = []
    for p in placed:
        g = clump.geometries[p.geometry]
        mat = geo_mats[p.geometry][0] if geo_mats[p.geometry] else 0
        parts.append(Part(p.name + (" (damaged)" if p.flag == "damaged" else " (low detail)" if p.flag == "low_detail"
                                    else ""), mat, len(g.positions), len(g.triangles)))

    # skins: the bones of a skinned ped, and its animations
    skinned = [(i, p) for i, p in enumerate(placed) if clump.geometries[p.geometry].skin is not None]
    skeleton = anims = rig = None
    pose_note = None
    if skinned and clump.bone_frames():
        first = clump.geometries[skinned[0][1].geometry].skin
        skeleton = Skeleton(clump, skinned[0][1].frame, first.inverse_bind)
        packages = _animation_packages(gf, stem, d)
        if packages:
            anims = RWAnimations(skeleton, packages, profile.bone_match if profile else "name")
            rig = _rig(skeleton, anims)
        else:
            pose_note = ("No animations were found for this character: the game's ped.ifp (in anim/) is needed to "
                         "pose it.")

    def build(options: MeshOptions) -> TriMesh:
        flags = options.flags
        chosen_parts = []
        for i, p in enumerate(placed):
            if options.parts is not None and i not in options.parts:
                continue
            if p.flag is not None and not flags.get(p.flag, False):
                continue
            if p.copy and not flags.get("wheels", True):
                continue
            chosen_parts.append((i, p))
        skin_now = None
        if options.pose is not None:
            if anims is None:
                raise FormatError(f"{stem} has no animations to pose it with")
            name, frame = options.pose
            skin_now = anims.skin_matrices(name, anims.clamp(name, frame))
        verts, norms, faces, uvs, mats, groups, joints, weights = [], [], [], [], [], [], [], []
        offset = 0
        for i, p in chosen_parts:
            g = clump.geometries[p.geometry]
            if not len(g.triangles):
                continue
            pos = g.positions.astype(np.float64) @ p.matrix[:3, :3].T + p.matrix[:3, 3]
            given = g.normals is not None
            nor = (g.normals.astype(np.float64) @ p.matrix[:3, :3].T) if given else _vertex_normals(pos, g.triangles)
            j = np.full((len(pos), 4), -1, np.int32)
            w = np.zeros((len(pos), 4), np.float32)
            if g.skin is not None:
                j = g.skin.indices.astype(np.int32)
                w = g.skin.weights.astype(np.float32)
                j = np.where(w > 0, j, -1)
                if skin_now is not None:
                    blended, has = blend(j, w, skin_now)
                    pos = np.where(has[:, None], np.einsum("nij,nj->ni", blended[:, :3, :3], pos) + blended[:, :3, 3],
                                   pos)
                    nor = np.where(has[:, None], np.einsum("nij,nj->ni", blended[:, :3, :3], nor), nor)
            verts.append(pos)
            norms.append(nor)
            faces.append(g.triangles + offset)
            uv = g.uvs[0] if g.uvs else np.zeros((len(pos), 2), np.float32)
            uvs.append(uv[g.triangles])
            lut = np.array(geo_mats[p.geometry], np.int32)
            mats.append(lut[np.clip(g.triangle_material, 0, len(lut) - 1)] if len(lut) else
                        np.zeros(len(g.triangles), np.int32))
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
        if skeleton is not None and skin_now is None:
            out.metadata["skin"] = (np.concatenate(joints), np.concatenate(weights))
        flip = np.einsum("ij,ij->i", out.face_cross(), out.corner_normals.sum(axis=1)) < 0
        if flip.any():
            out.faces[flip] = out.faces[flip][:, [0, 2, 1]]
            out.corner_uvs[flip] = out.corner_uvs[flip][:, [0, 2, 1]]
            out.corner_normals[flip] = out.corner_normals[flip][:, [0, 2, 1]]
        return weld(out)

    notes = list(clump.notes)
    if missing:
        notes.append(f"{len(missing)} texture{'s' if len(missing) != 1 else ''} weren't found in "
                     f"{', '.join(p.name for p in chain) or 'any texture dictionary'}: {', '.join(missing[:8])}"
                     + ("..." if len(missing) > 8 else ""))
    if vehicle and not combos and any(material_look(m, paint_slots, lights, True).paint is not None
                                      for g in clump.geometries for m in g.materials):
        notes.append("The game's colour list (carcols.dat) has no colours for this vehicle, so its paint is grey.")
    info = _info(clump, gf, d, finder)
    label = gf.display_name(stem)
    return SourceModel(
        name=label or stem, path=path, importer=importer_key, build=build, variant=variant if variant in variants
        else (variants[0] if variants else None), variants=variants, game=gf.game, parts=parts,
        materials=materials, textures=textures, options=[o for o in OPTIONS if _option_applies(o, placed)],
        info=info, notes=notes, animations=anims, pose_note=pose_note, rig=rig,
    )


def _plain(materials: list[Material], index: dict) -> int:
    key = (None, (255, 255, 255))
    if key not in index:
        index[key] = len(materials)
        materials.append(Material("untextured"))
    return index[key]


def _option_applies(option: BuildOption, placed: list[PlacedPart]) -> bool:
    if option.key == "wheels":
        return any(p.copy for p in placed)
    return any(p.flag == option.key for p in placed)


def _info(clump: dfffmt.Clump, gf: files.GameFiles, d, finder: TextureFinder) -> list[str]:
    v = clump.version
    verts = sum(len(g.positions) for g in clump.geometries)
    tris = sum(len(g.triangles) for g in clump.geometries)
    lines = [f"RenderWare {v >> 16 & 0xF}.{v >> 12 & 0xF}.{v >> 8 & 0xF}.{v & 0xFF} clump"
             + (f" ({gf.game})" if gf.game else ""),
             f"{len(clump.frames)} frames, {len(clump.atomics)} atomics, {len(clump.geometries)} geometries, "
             f"{verts:,} vertices, {tris:,} triangles"]
    skins = [g.skin for g in clump.geometries if g.skin is not None]
    if skins:
        lines.append(f"skinned: {skins[0].bones} bones")
    if d is not None:
        lines.append(f"defined in {d.file} ({d.section}), textures {d.txd}")
        if d.section in ("objs", "tobj", "anim"):
            area = gf.areas().get(d.model.lower())
            if area:
                lines.append(f"placed in {area}")
    if finder.used:
        lines.append("textures from " + ", ".join(p.name for p in finder.used))
    return lines


def _animation_packages(gf: files.GameFiles, stem: str, d) -> list[tuple[str, callable]]:
    """The animation packages a ped can be posed with: the game's ped.ifp, and the one its definition
    names, wherever they are (loose in anim/, or in an archive)."""
    names = []
    if gf.profile is not None:
        names.append(gf.profile.ped_animations.lower())
    else:
        names.append("ped.ifp")
    if d is not None and d.anim_file:
        n = d.anim_file if d.anim_file.endswith(".ifp") else d.anim_file + ".ifp"
        if n not in names:
            names.append(n)
    out = []
    for n in names:
        p = gf.find(n)
        if p is not None:
            out.append((p.name, lambda q=p: files.read_bytes(q, "animation")))
    return out


def _rig(skeleton: Skeleton, anims: RWAnimations) -> Rig:
    def pose_locals(name: str, frame: int):
        return list(skeleton.names), anims.local_pose(name, frame)

    return Rig(list(skeleton.names), skeleton.parents.copy(), skeleton.bind_local.copy(), skeleton.bind_world.copy(),
               pose_locals, anims.pose_all)
