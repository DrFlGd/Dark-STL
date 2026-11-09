"""Importer for the RenderWare Grand Theft Auto games: GTA III, Vice City and San Andreas (PC).

Models are ``.dff`` clumps with their textures in ``.txd`` dictionaries, kept loose or in the
games' ``.img`` archives (read in place). The ``.ide`` files say what each model is (ped, vehicle,
weapon, map object) and which dictionary its textures are in; ``carcols.dat`` gives cars their
colours, the ``.gxt`` text their names, and ``ped.ifp`` poses the peds. The formats live in
:mod:`.formats`; what differs between the three games is a JSON profile each (:mod:`.games`).
"""

from __future__ import annotations

from pathlib import Path

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import CatalogEntry, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, files, games
from . import model as rwmodel
from .formats import chunks
from .formats import dff as dfffmt
from .formats import ifp as ifpfmt
from .formats import img as imgfmt
from .formats import txd as txdfmt


class RenderWareImporter(Importer):
    key = "renderware"
    name = "RenderWare (GTA III, Vice City, San Andreas)"
    file_filter = "GTA models (*.dff)"
    parallel = True
    package_version = 1

    @property
    def games(self) -> tuple[str, ...]:  # read from the profiles, so a profile added in the settings counts
        return games.game_names()

    def detect_game(self, folder: Path) -> str | None:
        found = games.detect(Path(folder))
        return found[0].name if found is not None else None

    def _game_files(self, folder: Path, game: str | None) -> files.GameFiles | None:
        folder = Path(folder)
        found = games.detect(folder)
        if found is not None:  # the game's folder, or a folder inside it (its models listed with the game's data)
            profile, root = found
            return files.for_root(root, profile, rebuild=True)
        profile = games.profile(game)
        if profile is not None:  # the user said which game: a folder of its files
            return files.for_root(folder, profile, rebuild=True)
        from ...listing import files_under

        if any(p.suffix.lower() == ".dff" for p in files_under(folder)):
            return files.for_root(folder, None, rebuild=True)
        return None

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        gf = self._game_files(Path(folder), game)
        if gf is None:
            return []
        if progress is not None:
            progress(-1, f"Listing {gf.root.name}")
        self._last = gf
        found = catalog.scan(gf, gf.game or game, self.key)
        if files._key(gf.root) != files._key(Path(folder)):
            found = [e for e in found if files._is_under(e.path, Path(folder))]
        return found

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        gf = getattr(self, "_last", None)
        if gf is None or not (files._key(gf.root) == files._key(Path(folder)) or files._is_under(Path(folder), gf.root)):
            return []
        notes = list(gf.notes)
        ours = [e for e in found if e.importer == self.key]
        if gf.profile is not None and ours and not gf.definitions:
            notes.append(f"No .ide files were found in {Path(folder).name}, so its models can't be sorted into "
                         "characters, vehicles and so on, and their textures are looked up by name only. Add the "
                         "game's install folder (with its data folder) to sort them.")
        return notes

    def scan_state(self, folder: Path):
        gf = getattr(self, "_last", None)
        if gf is not None and (files._key(gf.root) == files._key(Path(folder)) or files._is_under(Path(folder), gf.root)):
            return gf
        return None

    def restore_state(self, folder: Path, state) -> None:
        if isinstance(state, files.GameFiles):
            files.register(state)

    def can_open(self, path: Path) -> bool:
        path = Path(path)
        if path.suffix.lower() != ".dff":
            return False
        if files.split_member(path) is not None:
            return True
        try:
            with open(path, "rb") as f:
                return chunks.is_dff(f.read(12))
        except OSError:
            return False

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        return rwmodel.load(self.key, Path(path), variant, self.cache)

    def variants(self, path: Path) -> list[str]:
        path = Path(path)
        gf = files.for_path(path)
        stem = path.name[:-4]
        d = gf.definition(stem)
        if d is None or d.section != "cars":
            return []
        return rwmodel.paint_variants(gf.colours.combinations(stem))

    def describe(self, path: Path) -> list[str] | None:
        path = Path(path)
        low = path.name.lower()
        try:
            if low.endswith(".img"):
                arc = imgfmt.read_img(path)
                kinds: dict[str, int] = {}
                for n in arc.names():
                    ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
                    kinds[ext] = kinds.get(ext, 0) + 1
                top = ", ".join(f"{c:,} .{k}" for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])[:6])
                return [f"GTA archive (IMG version {arc.version}), {len(arc.entries):,} files: {top}"]
            if low.endswith(".txd"):
                d = files.read_txd(path)
                return [f"RenderWare texture dictionary, {len(d.textures)} textures"] + \
                       [f"{t.name}: {t.describe()}" for t in d.textures[:40]]
            if low.endswith(".ifp"):
                pkg = ifpfmt.read_ifp(files.read_bytes(path))
                return [f"GTA animations ({pkg.format}) {pkg.name}: {len(pkg.animations)} animations"]
            if low.endswith(".dff"):
                c = dfffmt.read_clump(files.read_bytes(path))
                return [f"RenderWare clump: {len(c.frames)} frames, {len(c.atomics)} atomics"]
        except (FormatError, OSError) as exc:
            return [f"{path.name} couldn't be read: {exc}"]
        return None

    # -- textures -------------------------------------------------------------

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        """Every texture of the folder's dictionaries (``deep``), or the loose dictionaries' only."""
        gf = files.for_root(Path(folder)) if files._key(Path(folder)) in files._REGISTRY else None
        if gf is None:
            return []
        out = []
        for name, places in sorted(gf.names.items()):
            if not name.endswith(files.TEXTURE_EXT):
                continue
            where = places[0]
            if not deep and files.split_member(where) is not None:
                continue
            try:
                d = files.read_txd(where)
            except FormatError:
                continue
            for t in d.textures:
                out.append((f"{name[:-4]}/{t.name}", f"{where}|{t.name}"))
        return out

    def open_texture(self, folder: Path, ref: str) -> Texture:
        where, _, name = ref.rpartition("|")
        d = files.read_txd(Path(where)).by_name()
        t = d.get(name.lower())
        if t is None:
            raise FormatError(f"{Path(where).name} holds no texture named {name}")
        return rwmodel._game_texture(Path(where), t)

    # -- Collect --------------------------------------------------------------

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        """The model, the texture dictionaries it reads and, for a ped, its animation packages; archive
        members as loose copies below the game folder."""
        path = Path(path)
        gf = files.for_path(path)
        stem = path.name[:-4]
        wanted: list[tuple[Path, str]] = [(path, "model")]
        d = gf.definition(stem)
        if textures:
            vehicle = d is not None and d.section == "cars"
            wanted += [(p, "texture") for p in gf.texture_chain(stem, vehicle)]
        if animations and d is not None and d.section == "peds":
            for n, _read in rwmodel._animation_packages(gf, stem, d):
                p = gf.find(n)
                if p is not None:
                    wanted.append((p, "animation"))
        return [self._related(gf, p, kind) for p, kind in wanted]

    @staticmethod
    def _related(gf: files.GameFiles, p: Path, kind: str) -> RelatedFile:
        member = files.split_member(p)
        if member is None:
            return RelatedFile(p, kind)
        arc = files.open_archive(member[0])
        e = arc.find(member[1])
        if e is None:
            return RelatedFile(p, kind)
        save_as = files.member_save_as(member[0], e.name, gf.root if gf.profile else None)
        return RelatedFile(p, kind, save_as, lambda a=arc, x=e: a.read(x), e.size)

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """The data every model of the game is listed and opened with: the level lists and the .ide files
        they name, the placements, the car colours and the text, and the shared texture dictionaries."""
        root = Path(root)
        found = games.detect(root)
        if found is None:
            return []
        gf = files.for_root(found[1], found[0])
        gf.text  # noqa: B018 - read, so its file is listed
        gf.colours  # noqa: B018
        out = [RelatedFile(p, "other") for p in dict.fromkeys(gf.data_files)]
        for n in [*gf.profile.shared_textures, *gf.profile.vehicle_textures]:
            p = gf.find(n + files.TEXTURE_EXT)
            if p is not None:
                out.append(self._related(gf, p, "texture"))
        p = gf.find(gf.profile.ped_animations)
        if p is not None:
            out.append(self._related(gf, p, "animation"))
        return out

    def review_info(self, path: Path, files_: list[str]) -> dict:
        member = files.split_member(Path(path))
        return {"archive": member[0].name} if member is not None else {}


__all__ = ["RenderWareImporter", "games", "txdfmt"]
