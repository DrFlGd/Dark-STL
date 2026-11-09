"""Importer for id Tech 7/8 games: DOOM Eternal (and, as their phases land, DOOM 2016 and DOOM: The Dark Ages).

Models are read straight from the game's ``.resources`` containers and ``.streamdb`` files (or from a folder that
Collect wrote): static models (``.lwo``: props, pickups, map pieces) and skinned models (``.md6mesh``: monsters,
characters, weapons) with their ``.md6skl`` skeletons and ``.md6anim`` animations, textures from ``image``
resources, materials from ``material2`` decls. Formats in :mod:`.formats`, the game's index in :mod:`.store`; see
docs/PLAN.md, "Reading id Tech 6/7/8 files". Worked out from Valen (jandk/valen, MIT); no code is copied.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...geometry.mesh import TriMesh, weld
from ...model import CatalogEntry, Material, MeshOptions, Part, Rig, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, games, materials
from .animation import IdAnimations
from .formats import geometry as geo
from .formats import md6model, md6skel, staticmodel
from .formats import resources as res
from .store import META, Store, open_store, split_path

_MODEL_SUFFIXES = (".md6mesh", ".lwo")


def _is_model_name(name: str) -> bool:
    low = name.lower().split("$", 1)[0]
    return low.endswith(_MODEL_SUFFIXES)


class _Loaded:
    """A model's surfaces as the game keeps them, with what they need."""

    def __init__(self, store: Store, type_: str, name: str):
        self.store, self.type, self.name = store, type_, name
        loc = store.loc(type_, name)
        data = store.read(type_, name)
        self.notes: list[str] = []
        self.info: list[str] = []
        self.skeleton_name: str | None = None
        self.skeleton: md6skel.Skeleton | None = None
        self.mesh_names: list[str] = []
        self.surfaces: list[geo.Surface] = []
        self.materials: list[str] = []
        self.pieces: list[tuple[int, int]] = []  # StreamDB pieces read (id, size)
        if type_ == "baseModel":
            m = md6model.read_md6_model(data)
            self.skeleton_name = m.skeleton
            lods = [ms.lods[0] for ms in m.meshes]
            disk = m.disk[0]
            if disk.size == 0 or any(lo is None for lo in lods):
                raise FormatError(f"{name} has no full-detail geometry")
            ident = store.stream_id(loc.hash, 0)
            buf = store.stream(ident, disk.size)
            if buf is None:
                raise FormatError(f"{name}: its geometry isn't in the game's StreamDB files")
            self.pieces.append((ident, disk.size))
            surfaces = geo.read_streamed(buf, lods, disk.layouts, True)
            lookup = m.joint_lookup()
            for ms, s in zip(m.meshes, surfaces):
                if s is None:
                    self.notes.append(f"{ms.name}: its vertex layout isn't in the file")
                    continue
                if s.joints is not None:
                    s.joints = lookup[np.clip(s.joints + ms.joint_offset, 0, 255)]
                self.surfaces.append(s)
                self.mesh_names.append(ms.name)
                self.materials.append(ms.material)
            if store.has("skeleton", m.skeleton):
                self.skeleton = md6skel.read_skeleton(store.read("skeleton", m.skeleton))
            else:
                self.notes.append(f"Its skeleton {m.skeleton} isn't in the game's files, so it can't be posed.")
            self.info = [f"id Tech skinned model, {len(m.meshes)} meshes, skeleton {m.skeleton}"]
        else:
            m = staticmodel.read_static_model(data)
            if m.streamable:
                lod_i = next((i for i, d in enumerate(m.disk) if d.size > 0), None)
                if lod_i is None:
                    raise FormatError(f"{name} has no geometry")
                ident = store.stream_id(loc.hash, lod_i)
                buf = store.stream(ident, m.disk[lod_i].size)
                if buf is None:
                    raise FormatError(f"{name}: its geometry isn't in the game's StreamDB files")
                self.pieces.append((ident, m.disk[lod_i].size))
                lods = [s.lods[lod_i] for s in m.surfaces]
                got = geo.read_streamed(buf, [lo for lo in lods if lo is not None], m.disk[lod_i].layouts, False)
                it = iter(got)
                for s, lo in zip(m.surfaces, lods):
                    surf = next(it) if lo is not None else None
                    if surf is not None:
                        self.surfaces.append(surf)
                        self.materials.append(s.material)
                        self.mesh_names.append(PurePosix(s.material))
                if lod_i:
                    self.notes.append(f"The full-detail level isn't kept; level {lod_i} is shown.")
            else:
                for s, surf in zip(m.surfaces, m.embedded):
                    self.surfaces.append(surf)
                    self.materials.append(s.material)
                    self.mesh_names.append(PurePosix(s.material))
            self.info = [f"id Tech static model, {len(m.surfaces)} surfaces"
                         + (" (streamed geometry)" if m.streamable else "")]


def PurePosix(material: str) -> str:
    return material.replace("\\", "/").rsplit("/", 1)[-1] or "surface"


class IdTechImporter(Importer):
    key = "idtech"
    name = "id Tech 7/8 (DOOM Eternal)"
    file_filter = "DOOM models (*.md6mesh *.lwo)"
    games = games.GAMES
    record_reads = False  # related_files names every resource and StreamDB piece a model reads

    def detect_game(self, folder: Path) -> str | None:
        return catalog.detect_game(Path(folder))

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        if game not in games.GAMES and catalog.detect_game(Path(folder)) is None:
            return []
        return catalog.scan(Path(folder), game, progress, self.cache, self.key)

    def names(self, folder: Path, game: str, entries: list[CatalogEntry], fresh: bool = False) -> dict[str, str]:
        """None yet: DOOM's names are in its entityDef decls (which name each model) and .blang string tables,
        which aren't joined to the models yet. The models keep their file names."""
        return {}

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        from ..fromsoft.formats import oodle

        notes = []
        if any(e.packed for e in found if e.importer == self.key) and oodle.located() is None:
            notes.append("DOOM's resources are compressed with Oodle. Dark STL uses the game's own "
                         "oo2core_8_win64.dll (beside DOOMEternalx64vk.exe); it wasn't found, so point to it in "
                         "Settings › Game tools › Oodle.")
        return notes

    def can_open(self, path: Path) -> bool:
        path = Path(path)
        return _is_model_name(path.name) and split_path(path) is not None

    def _open(self, path: Path) -> tuple[Store, str, str]:
        return catalog.store_for(Path(path), self.cache)

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        store, type_, name = self._open(path)
        if type_ not in catalog.MODEL_TYPES:
            raise FormatError(f"{name} isn't a model")
        loaded = _Loaded(store, type_, name)
        unit = store.profile.unit
        infos, textures, mats = {}, {}, []
        notes = list(loaded.notes)
        for mname in loaded.materials:
            if mname not in infos:
                infos[mname] = materials.resolve(store, mname)
        order = list(dict.fromkeys(loaded.materials))
        for mname in order:
            info = infos[mname]
            keys = {}
            for role, image in info.roles.items():
                ref = materials.ImageRef(store, image)
                key = image.split("$", 1)[0]
                textures.setdefault(key, _texture(ref))
                keys[role] = key
            details = [f"decl {c}" for c in info.chain[:3]] + [f"{k}: {v}" for k, v in info.slots]
            mats.append(Material(PurePosix(mname), keys.get("albedo"), keys.get("normal"), None,
                                 details=details, normal_green=store.profile.normal_green,
                                 specular=keys.get("specular")))
            if not info.roles:
                notes.append(f"No textures were found for material {mname}"
                             + (f" ({', '.join(info.missing[:3])} not in the game)" if info.missing else "") + ".")
        mat_index = {m: i for i, m in enumerate(order)}
        parts = [Part(f"{n} ({PurePosix(m)})" if n != PurePosix(m) else n, mat_index[m], len(s.positions),
                      len(s.triangles)) for n, m, s in zip(loaded.mesh_names, loaded.materials, loaded.surfaces)]
        anims = None
        if loaded.skeleton is not None:
            anims = IdAnimations(store, loaded.skeleton_name, loaded.skeleton,
                                 self.cache / "anims" if self.cache else None)
        surfaces = loaded.surfaces

        def build(options: MeshOptions) -> TriMesh:
            chosen = [(i, s) for i, s in enumerate(surfaces) if options.parts is None or i in options.parts]
            if not chosen:
                return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
            skin = None
            if options.pose is not None:
                if anims is None or not len(anims):
                    raise FormatError(f"{name} has no animations to pose it with")
                anim, frame = options.pose
                skin = anims.skin_matrices(anim, anims.clamp(anim, frame))
            pos, nor, faces, uvs, fm, fg, joints, weights = [], [], [], [], [], [], [], []
            offset = 0
            for i, s in chosen:
                p = s.positions.astype(np.float64) * unit
                n = s.normals.astype(np.float64) if s.normals is not None else np.zeros_like(p)
                j = s.joints if s.joints is not None else np.full((len(p), 4), -1, np.int32)
                w = s.weights if s.weights is not None else np.zeros((len(p), 4), np.float32)
                if skin is not None and s.joints is not None:
                    from ...posing import blend

                    m, _ = blend(j, w, skin)
                    p = np.einsum("nij,nj->ni", m[:, :3, :3], p) + m[:, :3, 3]
                    n = np.einsum("nij,nj->ni", m[:, :3, :3], n)
                pos.append(p)
                nor.append(n)
                tri = s.triangles[:, [0, 2, 1]]  # the game's triangles turn the other way
                faces.append(tri + offset)
                uv = s.uvs if s.uvs is not None else np.zeros((len(p), 2), np.float32)
                uvs.append(uv[tri])
                fm.append(np.full(len(tri), mat_index[loaded.materials[i]], np.int32))
                fg.append(np.full(len(tri), i, np.int32))
                joints.append(j)
                weights.append(w)
                offset += len(p)
            p = np.concatenate(pos)
            n = np.concatenate(nor)
            f = np.concatenate(faces)
            length = np.linalg.norm(n, axis=1, keepdims=True)
            n = n / np.where(length > 0, length, 1)
            out = TriMesh(p, f, np.concatenate(uvs).astype(np.float32), np.concatenate(fm), np.concatenate(fg),
                          corner_normals=n[f].astype(np.float32))
            if skin is None and loaded.skeleton is not None:
                out.metadata["skin"] = (np.concatenate(joints).astype(np.int32),
                                        np.concatenate(weights).astype(np.float32))
            flip = np.einsum("ij,ij->i", out.face_cross(), out.corner_normals.sum(axis=1)) < 0
            if flip.mean() > 0.5:  # most faces disagree with their normals: the winding is the other way
                out.faces = out.faces[:, [0, 2, 1]]
                out.corner_uvs = out.corner_uvs[:, [0, 2, 1]]
                out.corner_normals = out.corner_normals[:, [0, 2, 1]]
            return weld(out)

        info = loaded.info + [f"{store.game}: {name}", f"in {store.loc(type_, name).container}"]
        if store.notes:
            notes += store.notes[:3]
        rig = _rig(anims) if anims is not None else None
        return SourceModel(
            name=catalog.label_of(name), path=Path(path), importer=self.key, build=build, game=store.game,
            parts=parts, materials=mats, textures=textures, info=info, notes=notes,
            animations=anims if anims is not None else None, rig=rig,
            pose_note=None if anims is not None else "This model has no skeleton, so it can't be posed.",
        )

    def describe(self, path: Path) -> list[str] | None:
        path = Path(path)
        if path.suffix.lower() == ".resources" and path.is_file():
            try:
                c = res.read_container(path)
            except FormatError:
                return None
            kinds: dict[str, int] = {}
            for e in c.entries:
                kinds[e.type] = kinds.get(e.type, 0) + 1
            lines = [f"id Tech container (IDCL version {c.version}), {len(c.entries):,} resources"]
            lines += [f"  {k}: {v:,}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:20]]
            return lines
        return None

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        store, type_, name = self._open(Path(path))
        loaded = _Loaded(store, type_, name)
        wanted: list[tuple[str, str, str]] = [(type_, name, "model")]
        pieces: list[tuple[int, int, str]] = [(i, s, "model") for i, s in loaded.pieces]
        if loaded.skeleton_name and store.has("skeleton", loaded.skeleton_name):
            wanted.append(("skeleton", loaded.skeleton_name, "model"))
        if textures:
            for mname in dict.fromkeys(loaded.materials):
                info = materials.resolve(store, mname)
                wanted += [("rs_streamfile", d, "texture") for d in info.chain]
                for image in info.roles.values():
                    wanted.append(("image", image, "texture"))
                    try:
                        pieces += [(i, s, "texture") for i, s in materials.ImageRef(store, image).streamed_pieces()]
                    except (FormatError, OSError):
                        pass
        if animations and loaded.skeleton is not None:
            anims = IdAnimations(store, loaded.skeleton_name, loaded.skeleton,
                                 self.cache / "anims" if self.cache else None)
            wanted += [("anim", a, "animation") for a in anims.files_used()]
        out: list[RelatedFile] = []
        seen = set()
        for t, n, kind in wanted:
            if (t, n) in seen or not store.has(t, n):
                continue
            seen.add((t, n))
            p = store.path_of(t, n)
            rel = p.relative_to(store.root).as_posix()
            loc = store.loc(t, n)
            out.append(RelatedFile(p, kind, rel, lambda t=t, n=n: store.read(t, n), loc.size))
            meta = json.dumps(store.meta(t, n)).encode()
            out.append(RelatedFile(Path(str(p) + META), kind, rel + META, lambda b=meta: b, len(meta)))
        for ident, size, kind in dict.fromkeys(pieces):
            rel = f"base/streamdb/{ident:016x}.bin"
            out.append(RelatedFile(store.root / rel, kind, rel,
                                   lambda i=ident, s=size: store.stream(i, s) or b"", size))
        return out

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        from .store import find_root

        root = find_root(Path(folder))
        if root is None or catalog.detect_game(root) is None:
            return []
        store = open_store(root, None, self.cache)
        seen: dict[str, str] = {}
        for n in store.names("image"):
            seen.setdefault(n.split("$", 1)[0], n)
        return sorted(seen.items())

    def open_texture(self, folder: Path, ref: str) -> Texture:
        from .store import find_root

        root = find_root(Path(folder)) or Path(folder)
        store = open_store(root, None, self.cache)
        return _texture(materials.ImageRef(store, ref))


def _texture(ref: materials.ImageRef) -> Texture:
    return Texture(ref.name.split("$", 1)[0], ref.name, ref.decode, ref.describe, raw=ref.dds)


def _rig(anims: IdAnimations) -> Rig:
    def pose_locals(anim: str, frame: int):
        return list(anims.names_), anims.local_pose(anim, anims.clamp(anim, frame))

    return Rig(list(anims.names_), anims.parents.copy(), anims.local.copy(), anims.bind_world.copy(), pose_locals)


__all__ = ["IdTechImporter"]
