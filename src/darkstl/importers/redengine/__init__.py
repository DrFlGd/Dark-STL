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


def _texture(ref) -> Texture:
    def describe() -> str:
        if hasattr(ref, "describe"):  # a blended multilayer map
            return ref.describe()
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


_UNNAMED = ("", "None")  # an appearance without a name: the property left out, or the CName "None"


def _label(name: str) -> str:
    """An appearance's name as the app shows it: the game leaves the first appearance of many meshes unnamed."""
    return "default" if name in _UNNAMED else name


def _appearance_named(m: meshfmt.RedMesh, variant: str | None) -> str | None:
    """The mesh appearance a chosen variant (a label from :func:`_label`) stands for, or the first one."""
    if not m.appearances:
        return None
    names = [a.name for a in m.appearances]
    if variant in names:
        return variant
    if variant == "default":
        unnamed = [n for n in names if n in _UNNAMED]
        if unnamed:
            return unnamed[0]
    return names[0]


class RedEngineImporter(Importer):
    key = "redengine"
    name = "REDengine 4 (Cyberpunk 2077)"
    file_filter = "Cyberpunk 2077 models (*.mesh *.archive)"
    games = (GAME,)

    def __init__(self):
        self._packed_in: dict[str, bool] = {}  # folder -> whether its last scan found models in archives

    def detect_game(self, folder: Path) -> str | None:
        return catalog.detect_game(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        found = catalog.scan(folder, game, progress, self.cache)
        self._packed_in[str(folder)] = any(e.packed for e in found)
        return found

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        from . import configurations as weapons

        notes = catalog.folder_notes(folder, found)
        if any(e.category == "Weapons" for e in found if e.importer == self.key):
            notes += weapons.notes_for(Path(folder))
        return notes

    def scan_state(self, folder: Path):
        """The folder's archives, whose names (kept in this importer's cache) are read again once one is opened."""
        if not self._packed_in.pop(str(folder), False):
            return None
        return [str(p) for p in catalog.iter_files(Path(folder)) if p.name.lower().endswith(".archive")]

    def restore_state(self, folder: Path, state) -> None:
        if isinstance(state, list) and state:  # read when one of them is first opened, not at start-up
            paths, cache = [Path(p) for p in state], self.cache
            catalog.oodle.add_hint_path(paths[0].parent)
            archive.when_first_opened(paths, lambda: catalog.restore_names(paths, cache))

    def configurations(self, folder: Path, game: str, entries: list[CatalogEntry]) -> list:
        """Weapons as their appearance files put them together (see :mod:`.configurations`); for folders of
        loose meshes without those files, weapons guessed from the files' names."""
        from ...configs import from_folders
        from . import configurations as weapons

        out = weapons.build(Path(folder), game, entries, self.cache)
        covered = {c.base.file for c in out}
        return out + [c for c in from_folders(entries, game) if c.base.file not in covered]

    def can_open(self, path: Path) -> bool:
        return bool(_MODEL.search(path.name)) or depot.is_archive_member(path)

    def variants(self, path: Path) -> list[str]:
        try:
            names = depot.open_file(path).appearance_names()
        except FormatError:
            return []
        return [_label(n) for n in names] if len(names) > 1 else []

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        source = depot.open_file(path)
        m = source.mesh()
        store = depot.store_for(path, texture_dirs)
        appearance = _appearance_named(m, variant)
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
            variant=_label(appearance or "") if len(m.appearances) > 1 else None,
            variants=[_label(a.name) for a in m.appearances] if len(m.appearances) > 1 else [],
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
        from . import configurations as weapons

        out = depot.related_files(Path(path), textures)
        if textures and "\\weapons\\" in (depot.open_file(Path(path)).depot_path or "").lower():
            for name, pack, h in weapons.appearance_files(Path(path), self.cache):
                ar = archive.open_archive(pack)
                out.append(RelatedFile(depot.member_path(pack, h, Path(name).suffix), "other",
                                       name.replace("\\", "/"), lambda ar=ar, h=h: ar.read(h), ar.size_of(h)))
        return out

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        return [RelatedFile(p, "other") for p in depot.shared_files(Path(root))]


__all__ = ["GAME", "RedEngineImporter", "mesh_summary"]
