"""Importer for REDengine 4 games: Cyberpunk 2077.

Models are ``.mesh`` files and textures ``.xbm`` files, both in CDPR's CR2W
container, either loose (as WolvenKit exports them, under their depot paths
such as ``base\\weapons\\...``) or still packed in the game's ``.archive``
files, which Dark STL reads directly. Materials are ``.mi`` instances that
chain to ``.mt`` templates; the textures they name are looked up by depot
path in the archives and loose folders the model came from.

The formats were worked out with WolvenKit (GPL) as the reference, in the
same way SoulsFormats is for the FromSoftware importer; no code is shared.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import BuildOption, CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, depot, materials
from .formats import archive
from .formats import mesh as meshfmt
from .formats import texture as texfmt
from .mesh import RedMeshOptions, chunks_to_trimesh

GAME = "Cyberpunk 2077"
_MODEL = re.compile(r"\.mesh$", re.IGNORECASE)


def _texture(ref: depot.TextureRef) -> Texture:
    def describe() -> str:
        t = ref.texture()
        return f"{t.width} x {t.height}, {t.codec}, {t.mips} mips"

    return Texture(ref.name, ref.source, ref.decode, describe)


def _material(info: materials.MaterialInfo) -> Material:
    details = [f"template {info.template or '-'}"] + [f"{slot}: {Path(path).stem}" for slot, path in info.slots]
    return Material(info.name, info.albedo, info.normal, info.height, guessed=info.guessed, details=details,
                    specular=info.specular, detail_normal=info.detail_normal, detail_scale=info.detail_scale)


def mesh_summary(m: meshfmt.RedMesh) -> list[str]:
    lines = [f"CMesh (CR2W version {m.version}), render blob version {m.blob_version}",
             (f"{len(m.chunks)} chunks over {len(m.lod_levels)} LOD levels, {len(m.material_names)} materials, "
             f"{len(m.appearances)} appearances, {len(m.bone_names)} bones")]
    lo, hi = m.bounds()
    if m.chunks:
        lines.append(f"bounds {np.round(lo, 3).tolist()} .. {np.round(hi, 3).tolist()}")
    for i, c in enumerate(m.chunks):
        lines.append(f"chunk {i}: {c.vertex_count} vertices, {c.index_count // 3} triangles, LOD mask {c.lod_mask}, "
                     f"{len(c.uvs)} UV sets, {'skinned' if c.skinned else 'rigid'}")
    for i, name in enumerate(m.material_names):
        lines.append(f"material {i}: {name}")
    for a in m.appearances:
        lines.append(f"appearance {a.name}: {', '.join(a.chunk_materials) or '-'}")
    return lines


class RedEngineImporter(Importer):
    key = "redengine"
    name = "REDengine 4 (Cyberpunk 2077)"
    file_filter = "Cyberpunk 2077 models (*.mesh *.archive)"
    games = (GAME,)

    def detect_game(self, folder: Path) -> str | None:
        return catalog.detect_game(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        return catalog.scan(folder, game, progress, self.cache)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        return catalog.folder_notes(folder, found)

    def can_open(self, path: Path) -> bool:
        return bool(_MODEL.search(path.name)) or depot.is_archive_member(path)

    def variants(self, path: Path) -> list[str]:
        try:
            names = depot.open_file(path).appearance_names()
        except FormatError:
            return []
        return names if len(names) > 1 else []

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        source = depot.open_file(path)
        m = source.mesh()
        store = depot.store_for(path, texture_dirs)
        appearance = variant or (m.appearances[0].name if m.appearances else None)
        infos, textures, notes = materials.resolve(m, source, store, appearance)
        parts = [Part(f"Chunk {i}" + (f" (LOD {c.lod_level})" if c.lod_level else ""), c.material_index,
                      c.vertex_count, c.index_count // 3) for i, c in enumerate(m.chunks)]
        notes = list(source.notes) + notes
        if not m.chunks:
            notes.append(f"{source.name} has no geometry.")

        def build(options: MeshOptions):
            if options.pose is not None:
                raise FormatError(f"{source.name} has no animations to pose it with")
            return chunks_to_trimesh(m, RedMeshOptions(include_chunks=options.parts,
                                                      all_lods=not options.flags.get("lod0", True)))

        return SourceModel(
            name=source.name, path=path, importer=self.key, build=build,
            variant=appearance if len(m.appearances) > 1 else None,
            variants=[a.name for a in m.appearances] if len(m.appearances) > 1 else [],
            game=GAME, parts=parts, materials=[_material(i) for i in infos],
            textures={k: _texture(ref) for k, ref in textures.items()},
            options=[BuildOption("lod0", "Highest detail level only", True,
                                 "Cyberpunk meshes carry several levels of detail in one file. Keep only the "
                                 "full-detail chunks; turn off to see every level at once.")],
            info=mesh_summary(m), notes=notes,
        )

    def describe(self, path: Path) -> list[str] | None:
        path = Path(path)
        if archive.is_archive_file(path):
            ar = archive.open_archive(path)
            lines = [f"REDengine archive, {len(ar.entries)} files, {len(ar.names)} with known names"]
            for e in list(ar.entries.values())[:200]:
                lines.append(f"  {ar.name_of(e.hash)}: {ar.size_of(e.hash):,} bytes in {e.segment_count} segments")
            if len(ar.entries) > 200:
                lines.append(f"  ... {len(ar.entries) - 200:,} more")
            return lines
        try:
            source = depot.open_file(path)
        except FormatError:
            return None
        f = source.file()
        if f.root_class == "CMesh":
            return mesh_summary(source.mesh())
        if f.root_class == "CBitmapTexture":
            t = texfmt.read_texture(f)
            return [f"CBitmapTexture {t.width}x{t.height} {t.codec}, {t.mips} mips, {t.compression}, {t.raw_format}"]
        lines = [f"CR2W version {f.version}, root {f.root_class}, {len(f.chunks)} chunks, {len(f.buffers)} buffers"]
        lines += [f"  imports {imp.path} ({imp.class_name})" for imp in f.imports[:50]]
        return lines

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        return depot.related_files(Path(path), textures)

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        return [RelatedFile(p, "other") for p in depot.shared_files(Path(root))]


__all__ = ["GAME", "RedEngineImporter", "mesh_summary"]
