"""Importer for Capcom's RE Engine games: Resident Evil 2, 3, 4, 7 and Village (and Devil May Cry 5).

Models are ``.mesh`` files with their materials in a ``.mdf2`` file beside them and their textures
in ``.tex`` files, all with the format version as a numeric extension. They are read from a
folder of extracted files or straight from the game's ``.pak`` archives when a file list for the
game is in the folder. The formats live in :mod:`.formats`; what differs between the games in
:mod:`.games`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...geometry.mesh import TriMesh, weld
from ...model import CatalogEntry, MeshOptions, Part, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, files, materials
from . import games as profiles
from .formats import mdf as mdffmt
from .formats import mesh as meshfmt
from .formats import pak as pakfmt
from .formats import tex as texfmt

# RE Engine models are Y up and right-handed; the export frame is Z up: a quarter turn about X.
GAME_TO_EXPORT = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def _texture(ref: files.TexRef) -> Texture:
    return Texture(ref.name, ref.source, ref.decode, ref.describe)


def mesh_summary(mesh: meshfmt.ReMesh, name: str = "") -> list[str]:
    game = mesh.game or "unknown game"
    lines = [f"RE Engine mesh version {mesh.version} ({game})",
             f"{len(mesh.lods)} level{'s' if len(mesh.lods) != 1 else ''} of detail, {len(mesh.material_names)} "
             f"materials, {len(mesh.bones)} bones, {mesh.uv_count} UV set{'s' if mesh.uv_count != 1 else ''}"]
    if mesh.streamed_count:
        lines.append(f"{mesh.streamed_count} streamed buffer{'s' if mesh.streamed_count != 1 else ''} "
                     f"({'found' if mesh.streamed_found else 'not found'})")
    lines.append(f"bounds {np.round(mesh.bbox_min, 3).tolist()} .. {np.round(mesh.bbox_max, 3).tolist()}")
    for i, lod in enumerate(mesh.lods):
        verts = sum(g.vertex_count for g in lod.groups)
        tris = sum(g.face_count for g in lod.groups) // 3
        lines.append(f"LOD {i}: {len(lod.groups)} groups, {verts:,} vertices, {tris:,} triangles"
                     + (f", distance {lod.distance:g}" if lod.distance else ""))
    for i, m in enumerate(mesh.material_names):
        lines.append(f"material {i}: {m}")
    return lines


class REEngineImporter(Importer):
    key = "reengine"
    name = "Capcom RE Engine (.mesh)"
    file_filter = "RE Engine models (*.mesh *.mesh.*)"
    games = profiles.GAMES

    def detect_game(self, folder: Path) -> str | None:
        return profiles.detect_game(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        paths, notes = files.walk(Path(folder), self.cache, progress)
        self._notes = {str(folder): notes}
        files.build_index(Path(folder), paths, self.cache / "index" if self.cache else None)
        return catalog.scan(Path(folder), game, paths, self.key)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        return list(getattr(self, "_notes", {}).get(str(folder), []))

    def can_open(self, path: Path) -> bool:
        return bool(profiles.MESH_FILE.search(path.name))

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        asset = files.load_model(path, texture_dirs)
        mesh = asset.mesh
        subs = meshfmt.submeshes(mesh)
        by_name = asset.mdf.by_name() if asset.mdf else {}
        infos = [materials.resolve(m, by_name.get(m), asset.textures) for m in mesh.material_names]
        if not infos:  # a mesh without material names still has material indices
            count = max((s.material_index for s in subs), default=-1) + 1
            infos = [materials.resolve(f"material {i}", None, asset.textures) for i in range(count)]
        parts = []
        for i, s in enumerate(subs):
            mat = mesh.material_names[s.material_index] if s.material_index < len(mesh.material_names) else ""
            parts.append(Part(f"Group {s.group}: {mat or f'mesh {i}'}", s.material_index, len(s.positions),
                              len(s.triangles)))
        notes = list(asset.notes)
        if not subs:
            notes.append(f"{asset.name} has no geometry.")
        game = mesh.game or profiles.detect_game(Path(path).parent)

        def build(options: MeshOptions) -> TriMesh:
            chosen = [(i, s) for i, s in enumerate(subs) if options.parts is None or i in options.parts]
            if not chosen:
                return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
            positions, normals, faces, uvs, mats, groups = [], [], [], [], [], []
            offset = 0
            for i, s in chosen:
                positions.append(s.positions.astype(np.float64))
                normals.append(s.normals.astype(np.float64) if s.normals is not None else np.zeros_like(positions[-1]))
                faces.append(s.triangles + offset)
                uv = s.uvs if s.uvs is not None else np.zeros((len(s.positions), 2), np.float32)
                uvs.append(uv[s.triangles])
                mats.append(np.full(len(s.triangles), s.material_index, np.int32))
                groups.append(np.full(len(s.triangles), i, np.int32))
                offset += len(s.positions)
            p = np.concatenate(positions) @ GAME_TO_EXPORT.T
            n = np.concatenate(normals) @ GAME_TO_EXPORT.T
            f = np.concatenate(faces)
            length = np.linalg.norm(n, axis=1, keepdims=True)
            n = n / np.where(length > 0, length, 1)
            out = TriMesh(p, f, np.concatenate(uvs).astype(np.float32), np.concatenate(mats), np.concatenate(groups),
                          corner_normals=n[f].astype(np.float32))
            # make every outward face counter-clockwise, as the stored normals say
            flip = np.einsum("ij,ij->i", out.face_cross(), out.corner_normals.sum(axis=1)) < 0
            if flip.any():
                out.faces[flip] = out.faces[flip][:, [0, 2, 1]]
                out.corner_uvs[flip] = out.corner_uvs[flip][:, [0, 2, 1]]
                out.corner_normals[flip] = out.corner_normals[flip][:, [0, 2, 1]]
            return weld(out)

        info = mesh_summary(mesh, asset.name)
        if asset.mdf_path is not None:
            info.append(f"materials from {asset.mdf_path.name}")
        if asset.streaming_path is not None:
            info.append(f"streamed buffers from {asset.streaming_path.as_posix().split('/natives/', 1)[-1]}")
        return SourceModel(
            name=asset.name, path=Path(path), importer=self.key, build=build, game=game, parts=parts,
            materials=[materials.to_material(i) for i in infos],
            textures={k: _texture(ref) for k, ref in asset.textures.items()},
            info=info, notes=notes,
        )

    def describe(self, path: Path) -> list[str] | None:
        name = Path(path).name
        if profiles.PAK_FILE.search(name):
            pak = pakfmt.read_pak(path)
            if pak.encrypted:
                return [f"RE Engine pak {pak.version[0]}.{pak.version[1]}: the table of contents is encrypted"]
            named = sum(1 for e in pak.entries if e.name)
            return [f"RE Engine pak {pak.version[0]}.{pak.version[1]}, {len(pak.entries):,} files"
                    + (f", {named:,} named" if named else " (no file list)")]
        data = files.read_bytes(Path(path))
        if meshfmt.is_mesh(data):
            return mesh_summary(meshfmt.read_mesh(data, profiles.version_of(name)), files.file_stem(name))
        if texfmt.is_tex(data):
            tex = texfmt.read_tex(data)
            return [f"RE Engine texture version {tex.version}: {tex.describe()}"
                    + (f", {tex.image_count} images" if tex.image_count > 1 else "")]
        if mdffmt.is_mdf(data):
            mdf = mdffmt.read_mdf(data)
            lines = [f"RE Engine materials version {mdf.version}, {len(mdf.materials)} materials"]
            for m in mdf.materials:
                lines.append(f"material {m.name}  [{m.shader.replace(chr(92), '/').rsplit('/', 1)[-1]}]")
                for t in m.textures:
                    lines.append(f"    {t.type}: {t.path}")
            return lines
        return None

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        out = []
        for p, kind in files.related_files(Path(path), textures):
            found = files.locate(p)
            if found is None:
                out.append(RelatedFile(p, kind))
            else:
                pak, entry = found
                out.append(RelatedFile(p, kind, entry.name or p.name, lambda pk=pak, e=entry: pk.read(e), entry.size))
        return out

    @staticmethod
    def texture_file(path: Path) -> dict[str, Texture]:
        """The texture in a .tex file, for showing texture files on their own."""
        stem = files.file_stem(Path(path).name)
        ref = files.TexRef(stem, Path(path), str(path))
        return {stem.lower(): _texture(ref)}


__all__ = ["REEngineImporter", "mesh_summary"]
