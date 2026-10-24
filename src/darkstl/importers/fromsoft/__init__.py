"""Importer for FromSoftware games: Dark Souls, Bloodborne, Dark Souls III, Sekiro, Elden Ring.

Models are FLVER2 files, usually packed in ``.partsbnd`` / ``.chrbnd`` /
``.objbnd`` / ``.geombnd`` / ``.mapbnd`` archives (optionally DCX compressed),
with TPF texture packs inside the archive or next to it. What differs between
the games lives in :mod:`.games`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import BuildOption, CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from ...textures import dds
from ..base import Importer, ScanProgress
from . import catalog, files, materials
from . import games as profiles
from .formats import binder, flver, tpf
from .mesh import FlverMeshOptions, flver_to_trimesh

_OPENABLE = re.compile(r"\.(partsbnd|chrbnd|objbnd|geombnd|mapbnd|flver|flv)(\.dcx)?$", re.I)
# The big archives a game ships its files in until they are unpacked (UXM, UDSFM).
_PACKED = re.compile(r"^(data\d*|dlc\d*|dvdbnd\d*|gamedata\w*|sd)\.bdt$", re.I)


def _basename(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _texture(ref: files.TextureRef) -> Texture:
    def describe() -> str:
        info = ref.info()
        return f"{info.width} x {info.height}, {info.codec}, {info.mipmaps} mips"

    return Texture(ref.name, ref.source, ref.decode, describe)


def _material(info: materials.MaterialInfo) -> Material:
    shader = info.mtd.replace("\\", "/").rsplit("/", 1)[-1]
    details = [f"shader {shader}"] + [f"{s.type}: {s.texture_name or Path(_basename(s.path)).stem or '-'}"
                                      for s in info.slots]
    # every FromSoftware game bakes its normal maps with green pointing down the texture (DirectX)
    return Material(info.name, info.albedo, info.normal, info.height, info.normal_scale, info.guessed, details,
                    normal_green="directx", specular=info.specular, detail_normal=info.detail_normal,
                    detail_scale=info.detail_scale)


def flver_summary(f: flver.Flver) -> list[str]:
    lines = [f"FLVER version 0x{f.version:X} ({f.game_hint}), {'big' if f.big_endian else 'little'}-endian",
             f"{len(f.bones)} bones, {len(f.materials)} materials, {len(f.meshes)} meshes"]
    if f.meshes:
        lines.append(f"bounds {np.round(f.bbox_min, 3).tolist()} .. {np.round(f.bbox_max, 3).tolist()}")
    for i, m in enumerate(f.meshes):
        mat = f.materials[m.material_index].name if m.material_index < len(f.materials) else "?"
        lines.append(f"mesh {i}: {m.vertices.count} vertices, {len(m.triangles())} triangles, material '{mat}', "
                     f"{'skinned' if m.dynamic else 'rigid'}, {len(m.vertices.uvs)} UV sets")
    for i, mat in enumerate(f.materials):
        lines.append(f"material {i}: {mat.name}  [{mat.mtd.replace(chr(92), '/').rsplit('/', 1)[-1]}]")
        for t in mat.textures:
            lines.append(f"    {t.type}: {t.stem or '(no path)'}")
    return lines


def _game_from_archives(folder: Path, archives: int = 3, max_files: int = 5000,
                        max_bytes: int = 64 << 20) -> str | None:
    """Recognise a folder of loose extracted files by the paths inside the first few of its archives."""
    seen = opened = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in sorted(filenames):
            seen += 1
            if seen > max_files or opened >= archives:
                return None
            path = Path(dirpath) / name
            if not _OPENABLE.search(name):
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
                opened += 1
                names = files.list_models_in_file(path)
            except Exception:  # noqa: BLE001, S112 - a guess: unreadable files just don't count
                continue
            game = next(filter(None, map(profiles.game_from_archive_path, names)), None)
            if game:
                return game
    return None


def _kind_of(path: Path) -> str:
    low = files.strip_dcx(path.name.lower())
    if files.ANIBND_FILE.search(path.name) or files.ANIBND_FOLDER.search(path.name):
        return "animation"
    if low.endswith((".tpf", ".tpfbhd", ".tpfbdt", ".chrtpfbhd", ".chrtpfbdt", ".texbnd", ".dds")) or \
            re.search(r"-(tpf|texbnd)(-dcx)?$", path.name, re.I):
        return "texture"
    if _OPENABLE.search(path.name) or re.search(r"-(partsbnd|chrbnd|objbnd|geombnd|mapbnd)(-dcx)?$", path.name, re.I):
        return "model"
    return "other"


class FromSoftwareImporter(Importer):
    key = "fromsoft"
    name = "FromSoftware (FLVER)"
    file_filter = "FromSoftware models (*.partsbnd *.chrbnd *.objbnd *.geombnd *.mapbnd *.flver *.dcx)"
    games = profiles.GAMES

    def detect_game(self, folder: Path) -> str | None:
        return profiles.detect_game(folder) or _game_from_archives(folder)

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        return catalog.scan(folder, game, progress)

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        packed = []
        for d in (folder, folder / "sd"):
            try:
                packed += sorted(p.name for p in d.iterdir() if _PACKED.match(p.name))
            except OSError:
                pass
        if not packed or len(found) >= 50:
            return []
        names = ", ".join(packed[:4]) + (" ..." if len(packed) > 4 else "")
        return [f"Most of this game is still packed in its archives ({names}), so only {len(found)} models "
                "were found. Unpack the game first (UXM Selective Unpacker for Dark Souls II, Dark Souls III, "
                "Sekiro and Elden Ring; UDSFM for Dark Souls: Prepare to Die Edition), then rescan this folder."]

    def can_open(self, path: Path) -> bool:
        return bool(_OPENABLE.search(path.name))

    def variants(self, path: Path) -> list[str]:
        names = [_basename(n) for n in files.list_models_in_file(path)]
        return names if len(names) > 1 else []

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        asset = files.load_model(path, variant, texture_dirs)
        f = asset.flver
        infos = materials.resolve_materials(asset)
        parts = [Part(f"Mesh {i}", m.material_index, m.vertices.count, len(m.triangles()))
                 for i, m in enumerate(f.meshes)]
        notes = list(asset.notes)
        if not f.meshes:
            notes.append(f"{asset.name} has no geometry: its model is an empty placeholder "
                         f"({len(f.bones)} bone{'s' if len(f.bones) != 1 else ''}). FromSoftware uses these for "
                         "variants that borrow another model; this file only carries textures.")
        game = profiles.game_from_archive_path(asset.inner_name) or profiles.detect_game(Path(path).parent)
        animations = files.find_animations(Path(path), asset.name, asset.binder) if f.bones else None

        def build(options: MeshOptions):
            bone_world = None
            if options.pose is not None:
                if animations is None:
                    raise FormatError(f"{asset.name} has no animations to pose it with")
                anim, frame = options.pose
                bone_world = animations.pose_bones([b.name for b in f.bones], np.array([b.parent for b in f.bones]),
                                                   np.array([b.local_matrix() for b in f.bones]), anim,
                                                   animations.clamp(anim, frame))
            return flver_to_trimesh(f, FlverMeshOptions(include_meshes=options.parts,
                                                        apply_bone_transforms=options.flags.get("bones", True),
                                                        bone_world=bone_world))

        return SourceModel(
            name=asset.name, path=Path(path), importer=self.key, build=build,
            variant=_basename(asset.inner_name) if asset.inner_name else None, variants=asset.variants, game=game, parts=parts,
            materials=[_material(i) for i in infos],
            textures={k: _texture(ref) for k, ref in asset.textures.items()},
            options=[BuildOption("bones", "Place rigid parts using their bones", True,
                                 "Moves non-skinned meshes (blades, guards, armour plates) to where their bone "
                                 "holds them. Turn off if parts end up out of place.")],
            info=flver_summary(f), notes=notes, animations=animations,
        )

    def describe(self, path: Path) -> list[str] | None:
        data = files.read_bytes(path)
        if flver.is_flver(data):
            return flver_summary(flver.read_flver(data))
        if tpf.is_tpf(data):
            pack = tpf.read_tpf(data)
            lines = [f"TPF ({tpf.PLATFORM_NAMES.get(pack.platform, pack.platform)}), {len(pack.textures)} textures"]
            for t in pack.textures:
                try:
                    info = dds.parse_dds(t.to_dds())
                    lines.append(f"  {t.name}: {info.width}x{info.height} {info.codec} mips={info.mipmaps}")
                except FormatError as exc:
                    lines.append(f"  {t.name}: format {t.format} ({exc})")
            return lines
        if binder.is_binder(data):
            b = binder.read_binder(data)
            lines = [f"{b.kind} archive, {len(b.files)} files"] + [f"  [{x.id}] {x.name}" for x in b.files]
            for x in b.by_extension(".flver", ".flver.dcx"):
                lines += ["", f"{x.basename}:"] + ["  " + s for s in flver_summary(flver.read_flver(x.data))]
            return lines
        if data[:4] == b"DDS ":
            info = dds.parse_dds(data)
            return [f"DDS {info.width}x{info.height} {info.codec} mips={info.mipmaps}"]
        return None

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        return [RelatedFile(p, _kind_of(p)) for p in files.related_files(Path(path), textures, animations, lod)]

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        return [RelatedFile(p, _kind_of(p)) for p in files.shared_files(Path(root), player_animations)]

    @staticmethod
    def texture_pack(path: Path) -> dict[str, Texture]:
        """Textures in a texture-only file (.tpf, .texbnd, .dds)."""
        refs: dict[str, files.TextureRef] = {}
        files.add_texture_file(refs, Path(path))
        return {k: _texture(r) for k, r in refs.items()}


__all__ = ["FromSoftwareImporter", "flver_summary"]
