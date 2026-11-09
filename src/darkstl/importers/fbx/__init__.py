"""Importer for FBX models (``.fbx``), as Blender, Maya, 3ds Max, Unreal, Unity and the modding tools write them.

* Binary FBX (6.1 to 7.7) and text FBX are read by Dark STL itself (:mod:`.reader`, :mod:`.scene`).
* Every mesh the scene places is a part (one per material when a mesh has several), placed as the file's
  nodes place it, then carried into Dark STL's frame: the file says which axis is up and how many
  centimetres a unit is, and that becomes metres with Z up.
* Materials' textures are the picture files they name: beside the model as the file stored them
  (``RelativeFilename``), where they were when it was made, or a picture of the same name anywhere in the
  folder the models were exported into (a ``.fbm`` folder too); pictures embedded in the file are read from
  it. A material with no textures connected gets those its material file or similar names give, as for
  ActorX models.
* A skinned mesh is shown in its bind pose with its skin weights; its skeleton (the bones its clusters
  name and their parents) goes with it into model packages. The file's animations (its stacks' first
  layers) pose it on the Pose tab, sampled at the file's frame rate and interpolated linearly between keys.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import numpy as np

from ... import access
from ...errors import FormatError
from ...geometry.mesh import TriMesh, weld
from ...model import CatalogEntry, Material, MeshOptions, Part, Rig, SourceModel, Texture
from ..base import Importer, ScanProgress
from ..loose import LoosePictures, picture
from ..psk import NamedTextures, _nearest
from .reader import is_fbx
from .scene import Obj, Scene, geometric_matrix, local_matrices, read_geometry, sample

SUFFIXES = (".fbx",)
MAX_FRAMES = 2400  # longer animations are sampled more sparsely
MAX_INFLUENCES = 8

# a material property a texture is connected to -> the role it plays (None: not one Dark STL uses)
_SLOT_RULES = [
    (re.compile(r"transparen|opacity|alpha|emissi|ambient|reflection|incandescen|translucen", re.I), None),
    (re.compile(r"normal", re.I), "normal"),
    (re.compile(r"bump|displace|height", re.I), "bump"),  # a normal map or a height map, by the picture's name
    (re.compile(r"diffuse|base_?colou?r|albedo|^color$|tex_color", re.I), "albedo"),
    (re.compile(r"spec|metal|rough|gloss|shinin", re.I), "specular"),
]


def _slot(prop: str, stem: str) -> str | None:
    from ..borderlands.materials import role_from_name

    for rx, role in _SLOT_RULES:
        if rx.search(prop or ""):
            if role == "bump":
                return "normal" if role_from_name(stem) == "normal" else "height"
            return role
    by_name = role_from_name(stem)
    return by_name if by_name in ("albedo", "normal", "specular", "height") else None


def _decode_bytes(data: bytes) -> np.ndarray:
    from ..gltf import _decode_picture

    return _decode_picture(data)


def _describe_bytes(data: bytes) -> str:
    from ..gltf import _describe_picture

    return _describe_picture(data)


def _file_names(scene: Scene, tex: Obj) -> tuple[list[str], list[str]]:
    """(relative names, absolute names) a texture and its video give for its picture file."""
    rel, absolute = [], []
    for o in [tex] + scene.linked(tex.id, "Video"):
        for key in ("RelativeFilename", "RelativeFileName"):
            v = o.node.value(key)
            if v:
                rel.append(str(v))
        for key in ("FileName", "Filename"):
            v = o.node.value(key)
            if v:
                absolute.append(str(v))
        for key in ("Path", "FileName", "RelPath"):
            v = o.prop(key)
            if isinstance(v, str) and v:
                (rel if key == "RelPath" else absolute).append(v)
    return rel, absolute


def _leaf(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


class FbxAnimations:
    """The file's animations, with the interface the Pose tab uses."""

    def __init__(self, infos: list, skeleton: list[str], note: str | None = None):
        self._infos = {i.name: i for i in infos}
        self.skeleton = list(skeleton)
        self.note = note

    @property
    def names(self) -> list[str]:
        return list(self._infos)

    def __len__(self) -> int:
        return len(self._infos)

    def __contains__(self, name: str) -> bool:
        return name in self._infos

    def list(self) -> list:
        return list(self._infos.values())

    def info(self, name: str):
        if name not in self._infos:
            raise FormatError(f"No animation named {name!r}")
        return self._infos[name]

    def clamp(self, name: str, frame: int) -> int:
        return min(max(int(frame), 0), self.info(name).frames - 1)


def _skin_arrays(points: int, rows: list[tuple[np.ndarray, int, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Per control point its bones and weights, (P, k), the heaviest first and summing to 1, from (control
    points, bone, weights) per cluster."""
    if not rows:
        return np.full((points, 1), -1, np.int32), np.zeros((points, 1), np.float32)
    pts = np.concatenate([r[0] for r in rows]).astype(np.int64)
    bones = np.concatenate([np.full(len(r[0]), r[1], np.int64) for r in rows])
    ws = np.concatenate([r[2] for r in rows]).astype(np.float64)
    keep = (pts >= 0) & (pts < points) & (ws > 0)
    pts, bones, ws = pts[keep], bones[keep], ws[keep]
    k = 1
    if len(pts):
        k = int(min(MAX_INFLUENCES, np.bincount(pts, minlength=points).max()))
    joints = np.full((points, max(k, 1)), -1, np.int32)
    weights = np.zeros((points, max(k, 1)), np.float32)
    if not len(pts):
        return joints, weights
    order = np.lexsort((-ws, pts))
    pts, bones, ws = pts[order], bones[order], ws[order]
    first = np.searchsorted(pts, pts, side="left")
    rank = np.arange(len(pts)) - first
    sel = rank < k
    joints[pts[sel], rank[sel]] = bones[sel]
    weights[pts[sel], rank[sel]] = ws[sel]
    total = weights.sum(axis=1, keepdims=True)
    weights = np.where(total > 0, weights / np.where(total > 0, total, 1), 0).astype(np.float32)
    return joints, weights


class FbxImporter(NamedTextures, LoosePictures, Importer):
    key = "fbx"
    name = "FBX (Blender, Maya, Unreal and other tools)"
    file_filter = "FBX (*.fbx)"

    def __init__(self) -> None:
        super().__init__()
        self._scene_lock = threading.Lock()

    def can_open(self, path: Path) -> bool:
        return Path(path).suffix.lower() in SUFFIXES

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        from ...listing import files_under

        folder = Path(folder)
        out = []
        for path in files_under(folder):
            if path.suffix.lower() in SUFFIXES:
                rel = path.parent.relative_to(folder).as_posix()
                out.append(CatalogEntry(game, "Models", "" if rel == "." else rel, path.stem, path.stem, path,
                                        importer=self.key, packed=False))
        self.note(folder, out)
        self.forget_index(folder)
        return out

    @staticmethod
    def _scene(path: Path) -> Scene:
        data = path.read_bytes()
        if not is_fbx(data):
            raise FormatError(f"{path.name} isn't an FBX file")
        try:
            return Scene(data, path.name)
        except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise FormatError(f"{path.name}: {exc}") from exc

    # -- textures

    @staticmethod
    def _named_picture(path: Path, scene: Scene, tex: Obj) -> Path | None:
        """The picture file a texture names, where it names it: beside the model, or where it was made."""
        rel, absolute = _file_names(scene, tex)
        for name in rel:
            p = path.parent / name.replace("\\", "/")
            if p.is_file():
                return p
        for name in absolute:
            p = Path(name)
            if p.is_absolute() and p.is_file():
                return p
        return None

    def _find_picture(self, path: Path, scene: Scene, tex: Obj, texture_dirs: tuple[str, ...]) -> Path | None:
        """A picture of the name a texture gives, anywhere in the folder the model was exported into."""
        rel, absolute = _file_names(scene, tex)
        here = path.parent
        stems = [Path(_leaf(n)).stem for n in rel + absolute if _leaf(n)]
        if not stems and tex.name:
            stems = [tex.name]
        indexes = [self._index(self._root(path))] + [self._index(Path(d)) for d in texture_dirs if Path(d).is_dir()]
        for stem in stems:
            for idx in indexes:
                hits = idx.pictures.get(stem.lower())
                if hits:
                    return _nearest(hits, here)
        return None

    def _material_textures(self, path: Path, scene: Scene, material: Obj | None, model: Obj,
                           texture_dirs: tuple[str, ...]):
        """{role: (picture file or embedded bytes, its name, the slot it's in)} and the names not found."""
        found: dict = {}
        missing: list[str] = []
        connected = scene.textures(material) if material is not None else []
        if not connected and scene.version < 7000:
            connected = [("", t) for t in scene.model_textures(model)]
        for prop, tex in connected:
            rel, absolute = _file_names(scene, tex)
            stem = Path(_leaf((rel + absolute)[0])).stem if rel + absolute else tex.name
            role = _slot(prop, stem)
            if role is None or role in found:
                continue
            file = self._named_picture(path, scene, tex)
            embedded = scene.video_content(tex) if file is None else None
            if file is None and not embedded:
                file = self._find_picture(path, scene, tex, texture_dirs)
            if file is not None:
                found[role] = (file, file.stem, prop)
            elif embedded:
                found[role] = (embedded, stem or tex.name, prop)
            else:
                missing.append(_leaf((rel + absolute)[0]) if rel + absolute else tex.name)
        return found, missing, bool(connected)

    # -- loading

    def related_files(self, path: Path, textures: bool = True, animations: bool = True, lod: bool = False):
        from ...collect import RelatedFile

        path = Path(path)
        out = [RelatedFile(path, "model")]
        if not textures:
            return out
        try:
            scene = self._scene(path)
        except (FormatError, OSError):
            return out
        seen = {path}
        for model, _geom in scene.meshes():
            mats = scene.materials(model) or [None]
            for m in mats:
                found, _missing, connected = self._material_textures(path, scene, m, model, ())
                files = [v[0] for v in found.values() if isinstance(v[0], Path)]
                if not connected and m is not None:
                    for albedo, normal, specular, *_ in self._materials(path, [m.name], ()):
                        files += [p for p in (albedo, normal, specular) if p is not None]
                    index = self._index(self._root(path))
                    hits = index.materials.get(m.name.lower())
                    if hits:
                        out.append(RelatedFile(_nearest(hits, path.parent), "other"))
                for f in files:
                    if f not in seen:
                        seen.add(f)
                        out.append(RelatedFile(f, "texture"))
        return out

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        access.note_file(path, "model")
        scene = self._scene(path)
        C = scene.frame
        C_inv = np.linalg.inv(C)
        notes: list[str] = []
        textures: dict[str, Texture] = {}
        creator = scene.creator.lower()
        green = "directx" if "unreal" in creator else "opengl" if "blender" in creator else None

        def texture(value, name: str) -> str | None:
            if isinstance(value, Path):
                key = value.stem
                if key not in textures:
                    access.note_file(value, "texture")
                    textures[key] = picture(value)
                return key
            key = f"{name} (in the file)"
            if key not in textures:
                data = bytes(value)
                textures[key] = Texture(name, f"{path.name} (embedded)", lambda d=data: _decode_bytes(d),
                                        lambda d=data: _describe_bytes(d),
                                        raw=(lambda d=data: d) if data[:4] == b"DDS " else None)
            return key

        materials: list[Material] = []
        material_index: dict = {}  # material object id (or None for the default) -> index in ``materials``

        def material_for(m: Obj | None, model: Obj) -> int:
            key = m.id if m is not None else None
            if key in material_index:
                return material_index[key]
            name = m.name if m is not None else "Default"
            found, missing, connected = self._material_textures(path, scene, m, model, texture_dirs)
            details = [f"{prop or role}: {label}" for role, (_value, label, prop) in found.items()]
            guessed = False
            if not connected and m is not None:  # nothing connected: its material file, or textures named like it
                albedo, normal, specular, guessed, extra, _gone = self._materials(path, [name], texture_dirs)[0]
                for role, file in (("albedo", albedo), ("normal", normal), ("specular", specular)):
                    if file is not None:
                        found[role] = (file, file.stem, role)
                details += extra
            if missing:
                notes.append(f"{name}: {', '.join(missing)} wasn't found beside the model")
            mat = Material(name, *(texture(found[r][0], found[r][1]) if r in found else None
                                   for r in ("albedo", "normal", "height")),
                           guessed=guessed, details=details, normal_green=green,
                           specular=texture(found["specular"][0], found["specular"][1]) if "specular" in found
                           else None)
            material_index[key] = len(materials)
            materials.append(mat)
            return material_index[key]

        # the meshes and the skeleton their skins name
        meshes = []  # (model, geometry, Geometry, clusters)
        for model, geom in scene.meshes():
            g = read_geometry(geom.node, f"{path.name}: {model.name}")
            if g is None or not len(g.triangles):
                continue
            meshes.append((model, geom, g, scene.clusters(geom)))
        if not meshes:
            raise FormatError(f"{path.name} has no meshes")

        bones: list[Obj] = []
        bone_index: dict = {}
        link_of: dict = {}
        for _m, _g, _geo, clusters in meshes:
            for c in clusters:
                link_of.setdefault(c.bone.id, c.link)
                for o in [c.bone] + scene.ancestors(c.bone):
                    if o.id not in bone_index:
                        bone_index[o.id] = len(bones)
                        bones.append(o)
        rig = anims = None
        bind_world_file: list[np.ndarray] = []
        if bones:
            depth = {o.id: len(scene.ancestors(o)) for o in bones}
            bones.sort(key=lambda o: (depth[o.id], bone_index[o.id]))
            bone_index = {o.id: i for i, o in enumerate(bones)}
            parents = np.array([bone_index.get(p.id, -1) if (p := scene.parent_model(o)) is not None else -1
                                for o in bones], np.int64)
            bind_pose = scene.bind_pose()
            for i, o in enumerate(bones):
                if o.id in link_of:
                    w = link_of[o.id]
                elif o.id in bind_pose:
                    w = bind_pose[o.id]
                elif parents[i] >= 0:
                    w = bind_world_file[parents[i]] @ local_matrices(o)[0]
                else:
                    w = scene.world(o)
                bind_world_file.append(np.asarray(w, np.float64))
            names, used = [], {}
            for o in bones:
                n = o.name or "bone"
                used[n] = used.get(n, 0) + 1
                names.append(n if used[n] == 1 else f"{n} ({used[n]})")
            bind_world = np.stack([C @ w @ C_inv for w in bind_world_file])
            bind_local = np.empty_like(bind_world)
            for i, p in enumerate(parents):
                bind_local[i] = np.linalg.inv(bind_world[p]) @ bind_world[i] if p >= 0 else bind_world[i]

            stacks = scene.stacks()
            infos, by_name = [], {}
            if stacks:
                from ..fromsoft.animation import AnimationInfo

                for s in stacks:
                    duration = max(s.stop - s.start, 0.0)
                    fps = scene.fps
                    frames = int(round(duration * fps)) + 1
                    if frames > MAX_FRAMES:
                        frames, fps = MAX_FRAMES, (MAX_FRAMES - 1) / duration
                    name = s.name
                    while name in by_name:
                        name += " (2)"
                    moves = sum(1 for o in bones if o.id in s.channels)
                    by_name[name] = (s, fps, frames)
                    infos.append(AnimationInfo(name, frames, duration, fps, path.name, moves, frames <= 1))

            def locals_at(anim: str, times: np.ndarray) -> np.ndarray:
                s, _fps, _frames = by_name[anim]
                out = np.empty((len(times), len(bones), 4, 4))
                for i, o in enumerate(bones):
                    out[:, i] = sample(s, o, times)
                return np.einsum("ij,fbjk,kl->fbil", C, out, C_inv)

            def times_of(anim: str, frames: np.ndarray) -> np.ndarray:
                s, fps, _n = by_name[anim]
                return s.start + np.asarray(frames, np.float64) / fps

            def pose_locals(anim: str, frame: int):
                if anim not in by_name:
                    raise FormatError(f"{path.stem} has no animation called {anim}" if by_name
                                      else f"{path.stem} has no animations to pose it with")
                n = by_name[anim][2]
                return list(names), locals_at(anim, times_of(anim, [min(max(int(frame), 0), n - 1)]))[0]

            def pose_all(anim: str):
                return list(names), locals_at(anim, times_of(anim, np.arange(by_name[anim][2])))

            # without animations it keeps its bones, for bone adjustments on its bind pose (the Pose tab)
            rig = Rig(names, parents, bind_local, bind_world, pose_locals, pose_all if by_name else None)
            if infos:
                anims = FbxAnimations(infos, names)

        # each mesh's points in the export frame, its triangles by material and its skin
        pieces = []  # (positions, normals per corner, uvs per corner, faces, face materials, joints, weights, part ids)
        parts: list[Part] = []
        no_normals = False
        for model, geom, g, clusters in meshes:
            skin_rows = [(c.indexes, bone_index[c.bone.id], c.weights) for c in clusters if c.bone.id in bone_index]
            weighted = [c for c in clusters if len(c.indexes)]
            if weighted:  # placed as at bind time: the bone's place then, with the mesh's place relative to it
                place = weighted[0].link @ weighted[0].transform
            else:
                place = scene.world(model)
                holder = next((a for a in scene.ancestors(model) if a.id in bone_index), None)
                if holder is not None:  # a mesh hung from a bone moves with it
                    b = bone_index[holder.id]
                    place = bind_world_file[b] @ np.linalg.inv(scene.world(holder)) @ place
                    skin_rows = [(np.arange(len(g.points)), b, np.ones(len(g.points)))]
            a = C @ place @ geometric_matrix(model)
            lin = a[:3, :3]
            positions = g.points @ lin.T + a[:3, 3]
            corner_normals = None
            if g.normals is not None:
                nm = np.linalg.inv(lin).T if abs(np.linalg.det(lin)) > 1e-20 else lin
                n = g.normals @ nm.T
                length = np.linalg.norm(n, axis=1, keepdims=True)
                corner_normals = (n / np.where(length > 0, length, 1)).astype(np.float32)
            else:
                no_normals = True
            tri = g.triangles
            if np.linalg.det(lin) < 0:
                tri = tri[:, [0, 2, 1]]
            uv = None
            if g.uvs is not None:
                uv = np.column_stack([g.uvs[:, 0], 1.0 - g.uvs[:, 1]]).astype(np.float32)
            model_mats = scene.materials(model)
            local = g.poly_material[g.triangle_poly]
            face_mat = np.zeros(len(tri), np.int32)
            face_part = np.zeros(len(tri), np.int32)
            several = len(np.unique(local)) > 1
            for li in np.unique(local):
                m = model_mats[li] if 0 <= li < len(model_mats) else None
                gi = material_for(m, model)
                sel = local == li
                face_mat[sel] = gi
                face_part[sel] = len(parts)
                label = model.name or f"Mesh {len(parts) + 1}"
                if several:
                    label = f"{label} ({materials[gi].name})"
                parts.append(Part(label, gi, int(len(np.unique(g.corners[tri[sel]]))), int(sel.sum())))
            joints = weights = None
            if rig is not None:
                joints, weights = _skin_arrays(len(g.points), skin_rows)
            pieces.append((positions, corner_normals, uv, tri, g.corners, face_mat, face_part, joints, weights))

        k = max((p[7].shape[1] for p in pieces if p[7] is not None), default=1)

        def assemble(keep: set[int] | None, skinned: bool) -> TriMesh:
            verts, faces, uvs, normals, mats, groups, joints, weights, base = [], [], [], [], [], [], [], [], 0
            for positions, cn, uv, tri, corners, face_mat, face_part, j, w in pieces:
                sel = np.ones(len(tri), bool) if keep is None else np.isin(face_part, list(keep))
                if not sel.any():
                    continue
                t = tri[sel]
                verts.append(positions)
                faces.append(corners[t] + base)
                uvs.append(uv[t] if uv is not None else np.zeros((len(t), 3, 2), np.float32))
                normals.append(cn[t] if cn is not None and not no_normals else None)
                mats.append(face_mat[sel])
                groups.append(face_part[sel])
                if skinned:
                    jj = np.full((len(positions), k), -1, np.int32)
                    ww = np.zeros((len(positions), k), np.float32)
                    jj[:, :j.shape[1]], ww[:, :w.shape[1]] = j, w
                    joints.append(jj)
                    weights.append(ww)
                base += len(positions)
            if not faces:
                return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
            mesh = TriMesh(np.concatenate(verts), np.concatenate(faces), np.concatenate(uvs), np.concatenate(mats),
                           np.concatenate(groups),
                           corner_normals=None if any(n is None for n in normals) else np.concatenate(normals))
            if skinned:
                mesh.metadata["skin"] = (np.concatenate(joints), np.concatenate(weights))
            return mesh

        def build(options: MeshOptions) -> TriMesh:
            if options.pose is None:
                mesh = assemble(options.parts, rig is not None)
                return weld(mesh.subset(np.ones(mesh.n_faces, bool)))
            if rig is None or anims is None:
                raise FormatError(f"{path.stem} has no animations Dark STL can pose it with")
            from ...posing import compose_hierarchy, pose_mesh

            name, frame = options.pose
            _names, local = rig.pose_locals(name, anims.clamp(name, frame))
            world = compose_hierarchy(local, rig.parents)
            mesh = assemble(options.parts, True)
            joints, weights = mesh.metadata["skin"]
            posed = pose_mesh(mesh, joints, weights, world @ np.linalg.inv(rig.bind_world))
            return weld(posed.subset(np.ones(posed.n_faces, bool)))

        pose_note = None
        if rig is None:
            pose_note = "It has no skeleton: none of its meshes is skinned."
        elif anims is None:
            pose_note = "The file has no animations to pose it with."
            notes.append("Shown in its bind pose; the file has no animations.")
        if all(m.albedo is None and m.normal is None for m in materials):
            notes.append("No textures were found for its materials: keep them beside the model (as PNG, TGA, DDS...), "
                         "or choose them on the Textures tab")
        every = np.concatenate([p[0] for p in pieces])
        size = np.ptp(every, axis=0) if len(every) else np.zeros(3)
        tris = sum(len(p[3]) for p in pieces)
        version = f"{scene.version // 1000}.{scene.version % 1000 // 100}"
        info = [f"FBX {version} ({'text' if scene.text else 'binary'})"
                + (f", made by {scene.creator}" if scene.creator else ""),
                f"{len(meshes)} meshes, {tris:,} triangles, {len(materials)} materials"
                + (f", {len(bones)} bones" if bones else "")
                + (f", {len(anims)} animations" if anims is not None else ""),
                f"{size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} m"]
        return SourceModel(name=path.stem, path=path, importer=self.key, build=build, parts=parts,
                           materials=materials, textures=textures, info=info, notes=notes, animations=anims,
                           pose_note=pose_note, rig=rig)

    def describe(self, path: Path) -> list[str] | None:
        return None


__all__ = ["FbxAnimations", "FbxImporter"]
