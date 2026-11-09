"""Importer for Grand Theft Auto V (PC: the original release and GTA V Enhanced) and Grand Theft Auto IV.

The game keeps its files in RPF7 archives (nested, encrypted with keys that are in the game's
executable: they are found there when the folder is added and kept in the app's settings, never
shipped). Models are drawables (``.ydr``), drawable dictionaries (``.ydd``: a ped's components) and
fragments (``.yft``: vehicles, and anything that breaks), with textures in ``.ytd`` dictionaries and
animations in ``.ycd`` clip dictionaries. ``vehicles.meta``, ``peds.meta`` and ``weapons*.meta`` say what
each model is, and the ``.gxt2`` text names them. Files exported with OpenIV or CodeWalker (``RSC7``
resources) open too.

GTA IV is the same engine a generation earlier: ``.img`` archives (tables encrypted with an AES key that
is found in ``GTAIV.exe`` the same way), RSC5 resources (``.wdr``, ``.wdd``, ``.wft``, ``.wtd``) with
4-byte pointers, read into the same objects, and ``.ide`` files and ``.gxt`` text for kinds and names.
Formats in :mod:`.formats`, the game profiles in :mod:`.games`.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ...collect import RelatedFile
from ...errors import FormatError
from ...model import CatalogEntry, SourceModel, Texture
from ..base import Importer, ScanProgress
from . import catalog, files, games
from . import model as ragemodel
from .formats import keys as keysmod
from .formats import rsc, rsc5


class RageImporter(Importer):
    key = "rage"
    name = "RAGE (Grand Theft Auto IV and V)"
    file_filter = "GTA IV and V models (*.ydr *.ydd *.yft *.wdr *.wdd *.wft)"
    parallel = True
    package_version = 1

    @property
    def games(self) -> tuple[str, ...]:
        return games.game_names()

    def detect_game(self, folder: Path) -> str | None:
        found = games.detect(Path(folder))
        return found[0].name if found is not None else None

    def _game_files(self, folder: Path, game: str | None, progress=None) -> files.GameFiles | None:
        folder = Path(folder)
        found = games.detect(folder)
        if found is not None:
            profile, root = found
        elif games.profile(game) is not None:
            profile, root = games.profile(game), folder
        else:
            from ...listing import files_under

            if not any(p.suffix.lower() in files.MODEL_EXT for p in files_under(folder)):
                return None
            profile, root = None, folder
        from ...listing import files_under

        def keys_progress(f: float) -> None:
            if progress is not None:
                progress(-1, f"Looking for the archive keys in the game ({int(f * 100)}%)")

        keys = keysmod.cached()
        iv_key = None
        why = None
        if profile is not None and profile.generation == 4:
            iv_key, why = files.iv_key_for(root, keys_progress)
        elif keys is None and any(p.suffix.lower() == ".rpf" for p in files_under(root)):
            keys, why = keysmod.for_game(root, keys_progress)
        gf = files.for_root(root, profile, keys, rebuild=True, progress=progress, iv_key=iv_key)
        if why and gf.notes:
            gf.notes.insert(0, why)
        return gf

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        gf = self._game_files(Path(folder), game, progress)
        if gf is None:
            return []
        if progress is not None:
            progress(-1, f"Listing {gf.root.name}")
        self._last = gf
        found = catalog.scan(gf, gf.game or game, self.key)
        if files._key(gf.root) != files._key(Path(folder)):
            found = [e for e in found if files._is_under(e.path, Path(folder))]
        return found

    def _ours(self, folder: Path) -> files.GameFiles | None:
        gf = getattr(self, "_last", None)
        if gf is not None and (files._key(gf.root) == files._key(Path(folder)) or files._is_under(Path(folder), gf.root)):
            return gf
        return files.root_of(Path(folder))

    def folder_notes(self, folder: Path, found: list[CatalogEntry]) -> list[str]:
        gf = self._ours(folder)
        return list(gf.notes) if gf is not None else []

    def scan_state(self, folder: Path):
        return self._ours(folder)

    def restore_state(self, folder: Path, state) -> None:
        if isinstance(state, files.GameFiles):
            files.register(state)

    def configurations(self, folder: Path, game: str, entries: list[CatalogEntry]) -> list:
        """A ped's outfit: one component per slot (head, top, legs, shoes...), from its ``.ydd``, or from
        the files of a streamed ped (an online character's)."""
        from ...configs import Configuration, ModelRef, PartOption, Slot

        out = []
        gf = self._ours(folder)
        streamed: dict[str, list[CatalogEntry]] = defaultdict(list)
        for e in entries:
            if e.importer != self.key or e.category != "Characters" or e.path.suffix.lower() not in (".ydd", ".wdd"):
                continue
            if e.group.endswith(" components"):
                streamed[e.group[:-len(" components")]].append(e)
                continue
            if gf is None:
                continue
            try:
                names = ragemodel.ydd_names(gf, e.path)
            except (FormatError, OSError):
                continue
            cfg = _outfit(e.stem, e.label, game, e.group, [(n, str(e.path), n) for n in names],
                          Configuration, ModelRef, PartOption, Slot)
            if cfg is not None:
                out.append(cfg)
        for ped, items in streamed.items():
            opts = [(e.stem.split("^", 1)[-1], str(e.path), None) for e in sorted(items, key=lambda x: x.stem)]
            cfg = _outfit(ped, ped, game, catalog.ped_group(gf, ped) if gf else "Characters", opts,
                          Configuration, ModelRef, PartOption, Slot)
            if cfg is not None:
                out.append(cfg)
        return out

    def can_open(self, path: Path) -> bool:
        path = Path(path)
        if path.suffix.lower() not in files.MODEL_EXT:
            return False
        gf = files.root_of(path)
        if gf is not None and gf.member(path) is not None:
            return True
        try:
            with open(path, "rb") as f:
                head = f.read(4)
                return rsc.is_resource_file(head) or rsc5.is_resource_file(head)
        except OSError:
            return False

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        return ragemodel.load(self.key, Path(path), variant, self.cache)

    def variants(self, path: Path) -> list[str]:
        path = Path(path)
        if path.suffix.lower() not in (".ydd", ".wdd"):
            return []
        gf = files.for_path(path)
        try:
            return ragemodel.ydd_names(gf, path)
        except FormatError:
            return []

    def describe(self, path: Path) -> list[str] | None:
        path = Path(path)
        low = path.name.lower()
        try:
            if low.endswith(".rpf"):
                from .formats import rpf

                arc = rpf.read_archive(path, keysmod.cached())
                kinds: dict[str, int] = {}
                for e in arc.entries:
                    ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
                    kinds[ext] = kinds.get(ext, 0) + 1
                top = ", ".join(f"{c:,} .{k}" for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])[:6])
                return [f"GTA V archive (RPF7), {len(arc.entries):,} files: {top}"]
            if low.endswith(".img"):
                from .formats import img

                arc = img.read_img(path, files.keys4.cached())
                return [f"GTA IV archive (IMG3), {len(arc.entries):,} files"]
            if low.endswith((".ytd", ".wtd")):
                gf = files.for_path(path)
                d = ragemodel.read_texture_file(gf, path)
                game = "GTA IV" if low.endswith(".wtd") else "GTA V"
                return [f"{game} texture dictionary, {len(d)} textures"] + \
                       [f"{t.name}: {t.width} x {t.height}, {t.codec}" for t in list(d.values())[:40]]
        except (FormatError, OSError) as exc:
            return [f"{path.name} couldn't be read: {exc}"]
        return None

    # -- textures -------------------------------------------------------------

    def list_textures(self, folder: Path, deep: bool = False) -> list[tuple[str, str]]:
        """Every texture of the loose dictionaries (and, ``deep``, of those in the archives)."""
        gf = files.root_of(Path(folder))
        if gf is None:
            return []
        out = []
        for name, places in sorted(gf.names.items()):
            if not name.endswith((".ytd", ".wtd")):
                continue
            rel = places[0]
            if not deep and rel not in gf.loose:
                continue
            p = gf.path(rel)
            try:
                d = ragemodel.read_texture_file(gf, p)
            except (FormatError, OSError):
                continue
            for t in d.values():
                out.append((f"{name[:-4]}/{t.name}", f"{p}|{t.name}"))
        return out

    def open_texture(self, folder: Path, ref: str) -> Texture:
        where, _, name = ref.rpartition("|")
        gf = files.for_path(Path(where))
        d = ragemodel.read_texture_file(gf, Path(where))
        t = d.get(name.lower())
        if t is None:
            raise FormatError(f"{Path(where).name} holds no texture named {name}")
        return ragemodel._texture(name, t, Path(where).name)

    # -- Collect --------------------------------------------------------------

    def related_files(self, path: Path, textures: bool = True, animations: bool = True,
                      lod: bool = False) -> list[RelatedFile]:
        """The model, the texture dictionaries it reads, its ped's skeleton and, for a posable model, its
        clip dictionaries; archive members as loose files at the same paths below the game folder."""
        path = Path(path)
        gf = files.for_path(path)
        wanted: list[tuple[Path, str]] = [(path, "model")]
        rel = gf.rel(path) or ""
        stem = path.stem.lower()
        frag = ragemodel.frag_ext(path)
        vehicle = path.suffix.lower() == frag and ("vehicles" in rel or stem.removesuffix("_hi") in gf.vehicles)
        if textures:
            wanted += [(p, "texture") for p in ragemodel.texture_chain(gf, path, vehicle)]
        if path.suffix.lower() in (".ydd", ".wdd") or "peds" in rel:
            skel = gf.find(ragemodel.ped_name(gf, path) + frag)
            if skel is not None and skel != path:
                wanted.append((skel, "other"))
        if animations:
            wanted += [(p, "animation") for p in ragemodel.animation_files(gf, path)]
        return [self._related(gf, p, kind) for p, kind in dict.fromkeys(wanted)]

    @staticmethod
    def _related(gf: files.GameFiles, p: Path, kind: str) -> RelatedFile:
        m = gf.member(p)
        if m is None:
            return RelatedFile(p, kind)
        rel = gf.rel(p)
        return RelatedFile(p, kind, rel, lambda q=p: gf.raw(q), m.entry.stored)

    def shared_files(self, root: Path, game: str, player_animations: bool = False) -> list[RelatedFile]:
        """What every model of the game is listed and named with: the vehicle, ped and weapon lists, the
        texture parents and the text, and the shared vehicle dictionaries."""
        root = Path(root)
        found = games.detect(root)
        if found is None:
            return []
        gf = files.root_of(found[1]) or files.for_root(found[1], found[0], keysmod.cached())
        out = [self._related(gf, gf.path(rel), "other") for rel in dict.fromkeys(gf.data_files)]
        for n in [*gf.profile.vehicle_textures, *gf.profile.shared_textures]:
            p = gf.find(n + gf.txd_ext)
            if p is not None:
                out.append(self._related(gf, p, "texture"))
        return out

    def review_info(self, path: Path, files_: list[str]) -> dict:
        gf = files.root_of(Path(path))
        arc = gf.archive_of(Path(path)) if gf is not None else None
        return {"archive": arc} if arc else {}


def _outfit(ped: str, label: str, game: str, group: str, options: list[tuple[str, str, str | None]],
            Configuration, ModelRef, PartOption, Slot):
    """A ped's configuration: ``options`` are (component name, file, variant) like ``uppr_001_u``."""
    by_slot: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for name, file, variant in options:
        slot = name.split("_", 1)[0].lower()
        by_slot[slot].append((name, file, variant))
    if not by_slot:
        return None
    order = [s for s in ragemodel.SLOTS if s in by_slot] + sorted(s for s in by_slot if s not in ragemodel.SLOTS)
    base_slot = "head" if "head" in by_slot else order[0]
    base_name, base_file, base_variant = sorted(by_slot[base_slot])[0]
    slots = []
    for s in order:
        items = sorted(by_slot[s])
        if s == base_slot:
            items = [i for i in items if i[0] != base_name]
            if not items:
                continue
        opts = [PartOption(n.lower(), n.replace("_", " "), ModelRef(f, v)) for n, f, v in items]
        default = opts[0].id if s != base_slot and s in ("uppr", "lowr", "hand", "feet", "hair", "teef", "jbib") \
            else None
        slots.append(Slot(f"{s}2" if s == base_slot else s,
                          ragemodel.SLOT_LABELS.get(s, s) + (" (other)" if s == base_slot else ""),
                          opts, False, default))
    prefix = "gta4" if base_file.lower().endswith(".wdd") else "gta5"
    return Configuration(f"{prefix}-{ped}", label, game, ModelRef(base_file, base_variant), slots, {},
                         "Characters", group, "game data")


__all__ = ["RageImporter", "games"]
