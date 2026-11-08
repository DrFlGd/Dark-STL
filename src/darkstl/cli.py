"""Command-line interface.

    darkstl scan  <folder>                    list the models found, grouped by game and type
    darkstl info  <file>                      describe a model (parts, materials, options), texture pack or archive
    darkstl textures <file> -o <dir>          save every texture as PNG
    darkstl export <file> -o model.obj        export the original model (stl/obj/ply/glb)
    darkstl enhance <file> -o model.stl -n 2000000
                                              even remesh + texture displacement + export
    darkstl gui                               open the explorer (also the default with no arguments)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__


def _progress(frac: float, msg: str) -> None:
    sys.stderr.write(f"\r[{frac * 100:5.1f}%] {msg[:90]:<90}")
    sys.stderr.flush()


def _parse_meshes(value: str | None) -> set[int] | None:
    if not value:
        return None
    return {int(x) for x in value.replace(" ", "").split(",") if x}


def cmd_scan(args) -> int:
    from .library import Library

    lib = Library()
    for folder in args.folders:
        lib.add_root(folder, args.game)
    tree = lib.grouped(include_lod=args.lod)
    for game, cats in tree.items():
        total = sum(len(v) for c in cats.values() for v in c.values())
        print(f"{game} ({total} models)")
        for cat, groups in cats.items():
            print(f"  {cat} ({sum(len(v) for v in groups.values())})")
            for group, entries in groups.items():
                names = ", ".join(e.stem for e in entries[: args.limit])
                more = f" ... +{len(entries) - args.limit}" if len(entries) > args.limit else ""
                print(f"    {group or '-'}: {names}{more}")
    return 0


def cmd_unpack(args) -> int:
    from .app.config import cache_dir, filelists_dir
    from .importers.fromsoft import unpack

    if args.download_lists:
        out = unpack.download_lists(filelists_dir(), None)
        print(f"Downloaded {len(out['downloaded'])} file lists into {out['folder']}")
        for f in out["failed"]:
            print(f"  failed: {f}")
        if not args.folder:
            return 0 if out["downloaded"] else 1
    if not args.folder:
        print("Name the game's folder (or --download-lists).")
        return 2

    def progress(frac: float, msg: str) -> None:
        print(f"\r{frac * 100:5.1f}%  {msg[:90]:<90}", end="", flush=True)

    groups = [g.strip() for g in args.only.split(",")] if args.only else list(unpack.DEFAULT_GROUPS)
    unknown = [g for g in groups if g not in {x.id for x in unpack.GROUPS}]
    if unknown:
        print(f"Unknown group(s) {', '.join(unknown)}; choose from {', '.join(g.id for g in unpack.GROUPS)}")
        return 2
    plan = unpack.plan(Path(args.folder), Path(args.out), args.game, cache_dir() / "archives", filelists_dir(), progress)
    print()
    s = plan.summary()
    print(f"{s['game']}: {sum(a['files'] for a in s['archives']):,} files in {len(s['archives'])} archives "
          f"({s['unknown']:,} without a known name, {s['guessed']:,} named by pattern)")
    for a in s["archives"]:
        if a["error"]:
            print(f"  {a['name']}: {a['error']}")
    for g in s["groups"]:
        mark = "*" if g["id"] in groups else " "
        print(f" {mark} {g['id']:<11} {g['files']:>7,} files {g['bytes'] / 2**30:8.2f} GB  ({g['to_unpack']:,} to unpack)")
    if args.dry_run:
        return 0
    result = unpack.run(plan, groups, progress)
    print()
    failed = f"; {result['failed_count']} failed" if result["failed_count"] else ""
    print(f"Wrote {result['written']:,} files ({result['bytes'] / 2**30:.2f} GB) into {result['dest']}{failed}")
    for f in result["failed"]:
        print(f"  {f}")
    return 0 if not result["failed_count"] else 1


def cmd_find(args) -> int:
    """Where objects are in a Borderlands (Unreal 3) game folder: for tracking down a missing material."""
    from .importers.borderlands.diagnose import find_objects

    def progress(i, n):
        if i % 100 == 0:
            print(f"  {i:,} / {n:,} packages", file=sys.stderr, flush=True)

    report = find_objects(Path(args.folder), args.names, progress)
    text = "\n".join(report.lines())
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", "utf-8")
        print(f"\nWritten to {args.output}", file=sys.stderr)
    return 0


def cmd_info(args) -> int:
    from .errors import FormatError
    from .importers import IMPORTERS, importer_for

    path = Path(args.file)
    described = False
    for imp in IMPORTERS:
        lines = imp.describe(path)
        if lines:
            print("\n".join(lines))
            described = True
            break
    try:
        imp = importer_for(path)
    except FormatError:
        if not described:
            print(f"Unrecognised file: {path.name}")
        return 0 if described else 1
    for variant in imp.variants(path) or [None]:
        model = imp.load(path, variant, _extra_dirs(args))
        print(f"\n{model.variant or model.name} ({imp.name}" + (f", {model.game})" if model.game else ")"))
        if not described:
            print("\n".join("  " + line for line in model.info))
        for i, part in enumerate(model.parts):
            print(f"  part {i}: {part.name}, {part.vertices:,} vertices, {part.triangles:,} triangles")
        if model.animations is not None:
            try:
                infos = model.animations.list()
                print(f"  {len(infos)} animations (use --pose NAME@FRAME):")
                for a in infos:
                    print(f"    {a.name}: {a.frames} frames, {a.duration:.2f} s")
            except FormatError as exc:
                print(f"  animations could not be read: {exc}")
        for i, mat in enumerate(model.materials):
            roles = ", ".join(f"{role} {key}" for role, key in
                              (("albedo", mat.albedo), ("normal", mat.normal), ("height", mat.height)) if key)
            print(f"  material {i}: {mat.name}: {roles or 'no textures'}{' (matched by name)' if mat.guessed else ''}")
        for option in model.options:
            print(f"  option {option.key}: {option.label} (default {'on' if option.default else 'off'})")
        for note in model.notes:
            print(f"  note: {note}")
    return 0


def _extra_dirs(args) -> tuple:
    return tuple(args.textures or ())


def cmd_textures(args) -> int:
    from .errors import FormatError
    from .export import write_png
    from .importers import open_model
    from .importers.fromsoft import FromSoftwareImporter

    path = Path(args.file)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    try:
        textures = open_model(path, texture_dirs=_extra_dirs(args)).textures
    except FormatError:  # a texture pack or texture file rather than a model
        from .importers.reengine import REEngineImporter

        if re.search(r"\.tex(\.\d+)?$", path.name, re.I):
            textures = REEngineImporter.texture_file(path)
        else:
            textures = FromSoftwareImporter.texture_pack(path)
    if not textures:
        print("No textures found")
        return 1
    for key, tex in sorted(textures.items()):
        try:
            write_png(tex.decode(), out / f"{Path(tex.name).stem}.png")
            print(f"  {Path(tex.name).stem}.png")
        except Exception as exc:  # noqa: BLE001
            print(f"  {tex.name}: failed ({exc})")
    return 0


def _mesh_options(args):
    from .model import MeshOptions

    flags = {}
    for item in args.option or ():
        key, _, value = item.partition("=")
        flags[key.strip()] = value.strip().lower() not in ("0", "off", "false", "no")
    if args.no_bones:
        flags["bones"] = False
    pose = None
    if getattr(args, "pose", None):
        name, _, frame = args.pose.rpartition("@")
        pose = (name or args.pose, int(frame) if name and frame else 0)
    return MeshOptions(parts=_parse_meshes(args.parts), flags=flags, pose=pose)


def _open(args):
    from .importers import open_model

    model = open_model(args.file, args.variant, _extra_dirs(args))
    for note in model.notes:
        print(f"note: {note}", file=sys.stderr)
    return model


def _export_options(args, model, mesh):
    from .export import ExportOptions

    opts = ExportOptions(scale=args.scale, ascii_stl=getattr(args, "ascii", False))
    opts.material_names = [m.name for m in model.materials] if model is not None else []
    ext = Path(args.output).suffix.lower()
    if ext == ".3mf":
        from .paint import PaintSettings

        palette = [c.strip() for c in (args.palette or "").split(",") if c.strip()] or None
        sources = {}
        for item in args.paint_texture or []:
            mat, sep, key = item.partition("=")
            if not sep or not mat.strip().isdigit():
                raise SystemExit(f"--paint-texture takes MATERIAL=TEXTURE (material number), not '{item}'")
            sources[str(int(mat))] = key.strip() or "none"
        opts.paint = PaintSettings(colours=args.colours, palette=palette, print_height_mm=args.print_height,
                                   feature_mm=args.feature, cleanup=args.cleanup, tolerance=args.tolerance,
                                   min_share=args.min_share, brightness=args.brightness, contrast=args.contrast,
                                   saturation=args.saturation, sources=sources or None)
        opts.progress = _progress
        if model is not None:
            from .paint import colour_textures

            try:
                opts.textures = colour_textures(model, mesh, opts.paint)
            except ValueError as exc:
                raise SystemExit(f"error: {exc}") from None
            return opts
    if ext in (".obj", ".glb") and model is not None:
        for m in np.unique(mesh.face_material):
            m = int(m)
            key = model.materials[m].albedo if m < len(model.materials) else None
            if key and key in model.textures:
                try:
                    opts.textures[m] = model.textures[key].decode()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: could not decode {key}: {exc}", file=sys.stderr)
    return opts


def _paint_from(args, model, textured):
    """For a painted 3MF of a model made from ``textured`` (--watertight, --cut), which has no textures of its
    own: the colours of the textured model's surface to paint it from (paint.surface_colours)."""
    if Path(args.output).suffix.lower() != ".3mf":
        return None
    from .paint import surface_colours

    opts = _export_options(args, model, textured)
    return surface_colours(textured, opts.textures, opts.paint, _progress)


def _report_paint(opts) -> None:
    """What a painted 3MF was painted with: the filament slots' colours and how much they cover."""
    p = opts.painting
    if p is None:
        return
    sys.stderr.write("\n")
    st = p.stats()
    slots = ", ".join(f"{i + 1}: {c} ({cov * 100:.0f}%)" for i, (c, cov) in enumerate(zip(st["colours"], st["coverage"])))
    print(f"Painted with {len(st['colours'])} filaments: {slots}; {st['split_faces']:,} triangles in more than one colour")
    for note in st["notes"]:
        print(f"note: {note}")


def _write_pieces(args, mesh, colours=None) -> int:
    """With --cut: the model cut into pieces, each written beside the output as <name>_pieceN.<ext>; for a
    painted 3MF each piece is painted from ``colours`` (see _paint_from), with one palette for all."""
    from . import cut
    from .export import ExportOptions, export_mesh, write_palette_png

    try:
        settings = cut.CutSettings([cut.parse_cut(c, mesh) for c in args.cut], sheet_mm=args.sheet)
        for c in settings.cuts:
            c.joint.clearance_mm = args.clearance
        result = cut.cut_mesh(mesh, settings, _progress)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sys.stderr.write("\n")
    for n in result.notes:
        print(f"note: {n}", file=sys.stderr)
    out = Path(args.output)
    paint = None
    if colours is not None:
        paint = _export_options(args, None, mesh).paint
        out.parent.mkdir(parents=True, exist_ok=True)
        write_palette_png(colours.palette, colours.coverage, out.with_name(f"{out.stem}_palette.png"))
        slots = ", ".join(f"{i + 1}: #{int(r):02X}{int(g):02X}{int(b):02X}" for i, (r, g, b) in enumerate(colours.palette))
        print(f"Painting the pieces with {len(colours.palette)} filaments: {slots}")
    for piece in result.pieces:
        slug = piece.name.lower().replace(" ", "")
        opts = ExportOptions(scale=args.scale, ascii_stl=getattr(args, "ascii", False), paint=paint, paint_from=colours,
                             palette_png=False)
        path = export_mesh(cut.print_pose(piece), out.with_name(f"{out.stem}_{slug}{out.suffix}"), opts)
        print(f"Wrote {path} ({piece.mesh.n_faces:,} triangles, {piece.volume_mm3 / 1000:.2f} cm3)")
        for note in opts.painting.notes if opts.painting is not None else []:
            print(f"note: {note}")
    return 0


def _watertight(args, mesh):
    """With --watertight: the model as one watertight solid, the surfaces inside it removed (finish.py)."""
    from . import finish

    settings = finish.WatertightSettings(sheet_mm=args.sheet, fill_hollows=not args.keep_hollows,
                                         close_gaps_mm=args.close_gaps, loose_percent=args.drop_loose)
    result = finish.make_watertight(mesh, settings, _progress)
    sys.stderr.write("\n")
    for n in result.notes:
        print(f"note: {n}", file=sys.stderr)
    return result.mesh


def _finish(args, mesh):
    """The post-processing asked for on the command line, or None with an error printed."""
    if not getattr(args, "watertight", False):
        return mesh
    try:
        return _watertight(args, mesh)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def cmd_export(args) -> int:
    from .export import export_mesh

    model = _open(args)
    mesh = model.build_mesh(_mesh_options(args))
    if mesh.n_faces == 0:
        print(f"error: {model.name} has no triangles to export", file=sys.stderr)
        return 1
    textured = mesh
    mesh = _finish(args, mesh)
    if mesh is None:
        return 1
    colours = _paint_from(args, model, textured) if mesh is not textured or args.cut else None
    if args.cut:
        return _write_pieces(args, mesh, colours)
    opts = _export_options(args, model, mesh)
    opts.paint_from = colours
    path = export_mesh(mesh, args.output, opts)
    print(f"Wrote {path} ({mesh.n_vertices:,} vertices, {mesh.n_faces:,} triangles)")
    _report_paint(opts)
    return 0


def cmd_enhance(args) -> int:
    from .export import export_mesh, write_png
    from .app.config import models_dir
    from .pipeline import EnhanceSettings, enhance

    model = _open(args)
    settings = EnhanceSettings(
        target_vertices=args.vertices,
        method=args.method,
        smooth=0.0 if args.flat else 0.5,
        source=args.source,
        strength=args.strength,
        height_range_mm=args.height_range,
        green=args.green,
        detail_fraction=args.detail,
        max_displacement_mm=args.max_mm,
        mesh_options=_mesh_options(args),
        integration=args.integration,
        edge_keeping=args.edge_keeping,
        deblock=args.deblock,
        edge_smoothing=args.edge_smoothing,
        imported_maps=dict(x.split("=", 1) for x in args.use_map),
        upsample=args.upsample,
        ai_model=args.ai_model,
        deblock_model=args.deblock_model,
        models_dir=models_dir(),
        edge_hints=args.edge_hints,
        albedo_detail_mm=args.albedo_detail,
        detail_normals=args.detail_normals,
    )
    t0 = time.time()
    result = enhance(model, settings, _progress)
    sys.stderr.write("\n")
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    out_mesh = _finish(args, result.displaced)
    if out_mesh is None:
        return 1
    colours = _paint_from(args, model, result.displaced) if out_mesh is not result.displaced or args.cut else None
    if args.cut:
        return _write_pieces(args, out_mesh, colours)
    opts = _export_options(args, model, out_mesh)
    opts.paint_from = colours
    path = export_mesh(out_mesh, args.output, opts)
    _report_paint(opts)
    st = result.stats()
    if out_mesh is not result.displaced:
        st = {**st, "vertices": out_mesh.n_vertices, "faces": out_mesh.n_faces}
    print(f"Wrote {path}: {st['vertices']:,} vertices, {st['faces']:,} triangles "
          f"(from {st['source_vertices']:,}); max displacement {st['max_displacement_mm']:.3f} mm "
          f"[{time.time() - t0:.1f}s]")
    for mat, f in result.fields.items():
        name = model.textures[f.texture].name
        print(f"  material {mat}: {name}, green={f.field.convention or 'n/a'}, texel={f.texel_size * 1000:.3f} mm"
              + "".join(f"; {step}" for step in f.steps))
        if args.save_heights:
            d = Path(args.save_heights)
            d.mkdir(parents=True, exist_ok=True)
            h = f.world
            span = float(np.abs(h).max()) or 1.0
            write_png((h / span * 0.5 + 0.5).astype(np.float32), d / f"{Path(name).stem}_height.png")
    return 0


def cmd_gui(args) -> int:
    from .app.desktop import run

    return run(args.folders, browser=getattr(args, "browser", False), port=getattr(args, "port", 0))


def cmd_engine(args) -> int:
    from .app.engine import main

    return main(args.folders)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="darkstl", description="Game model explorer and detail enhancer")
    p.add_argument("--version", action="version", version=f"darkstl {__version__}")
    p.add_argument("--oodle", help="path to oo2core_6_win64.dll (Sekiro / Elden Ring files)")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="list models in extracted game folders")
    s.add_argument("folders", nargs="+")
    s.add_argument("--game", help="game name to use instead of auto-detection")
    s.add_argument("--lod", action="store_true", help="include low-detail (_l) variants")
    s.add_argument("--limit", type=int, default=12, help="names to show per group")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("unpack", help="unpack a game's models from its archives (Data0.bhd/.bdt ...)")
    s.add_argument("folder", nargs="?", help="the game's install folder")
    s.add_argument("-o", "--out", default="unpacked", help="folder to write into (default: ./unpacked)")
    s.add_argument("--only", help="groups, comma-separated: characters,parts,objects,maps (default: all but maps)")
    s.add_argument("--game", help="the game, when its archives don't say (Sekiro and Dark Souls III look alike)")
    s.add_argument("--dry-run", action="store_true", help="only say what would be unpacked")
    s.add_argument("--download-lists", action="store_true", help="first download newer file lists (UXM Selective Unpacker's)")
    s.set_defaults(func=cmd_unpack)

    s = sub.add_parser("find", help="find where objects are stored in a Borderlands (Unreal 3) game folder")
    s.add_argument("folder", help="the game's install folder")
    s.add_argument("names", nargs="+", help="part of an object's name, e.g. Mati_DahlEpicAR")
    s.add_argument("-o", "--output", help="also write the list to this text file")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("info", help="describe a model, texture pack or archive")
    s.add_argument("file")
    s.add_argument("--textures", action="append", help="extra folder to search for textures")
    s.set_defaults(func=cmd_info)

    def model_args(sp):
        sp.add_argument("file", help="model file: .partsbnd/.chrbnd/... archive, .flver or .obj")
        sp.add_argument("-o", "--output", required=True, help="output file (.stl .obj .ply .glb, or .3mf painted)")
        sp.add_argument("--variant", "--inner", dest="variant",
                        help="which model to use from a file that holds several (see 'info')")
        sp.add_argument("--parts", "--meshes", dest="parts", help="comma-separated part indices to include (see 'info')")
        sp.add_argument("--option", action="append", metavar="KEY=on|off",
                        help="importer option (see 'info'), e.g. --option mm=on for an OBJ in millimetres")
        sp.add_argument("--no-bones", action="store_true", help="same as --option bones=off")
        sp.add_argument("--pose", metavar="ANIMATION@FRAME",
                        help=("pose the model with one of its animations (see 'info'): from its anibnd, e.g. "
                              "a000_003000@12, or for RE Engine models from the game's motlists, "
                              "e.g. \"pl0000_move / wave@12\""))
        sp.add_argument("--scale", type=float, default=1000.0, help="unit scale (default 1000: metres -> mm)")
        sp.add_argument("--ascii", action="store_true", help="ASCII STL")
        sp.add_argument("--textures", action="append", help="extra folder to search for textures")
        sp.add_argument("--colours", "--colors", dest="colours", type=int, default=4,
                        help="painted 3MF: filament colours the textures are reduced to, 2-16 (default 4)")
        sp.add_argument("--palette", metavar="#RRGGBB,...",
                        help="painted 3MF: the filament colours themselves, in slot order (instead of --colours)")
        sp.add_argument("--print-height", type=float, default=150.0, metavar="MM",
                        help="painted 3MF: the model's height when printed, for --feature (default 150)")
        sp.add_argument("--feature", type=float, default=0.5, metavar="MM",
                        help="painted 3MF: smallest colour feature kept at that print height (default 0.5)")
        sp.add_argument("--cleanup", choices=["none", "light", "strong"], default="light",
                        help="painted 3MF: specks removed from the reduced textures (default light)")
        sp.add_argument("--tolerance", type=float, default=0.0, metavar="N",
                        help="painted 3MF: suggested colours closer than this (0-50, Oklab distance x 100) are "
                             "merged, so there can be fewer than --colours (default 0)")
        sp.add_argument("--min-share", type=float, default=0.0, metavar="PCT",
                        help="painted 3MF: suggested colours covering less than this %% of the surface are dropped")
        sp.add_argument("--brightness", type=float, default=0.0, help="painted 3MF: -1..1 added to the texture colours")
        sp.add_argument("--contrast", type=float, default=1.0, help="painted 3MF: texture contrast, 0-3 (default 1)")
        sp.add_argument("--saturation", type=float, default=1.0, help="painted 3MF: texture saturation, 0-3 (default 1)")
        sp.add_argument("--paint-texture", action="append", metavar="MAT=TEXTURE",
                        help="painted 3MF: paint material number MAT from this texture instead of its colour "
                             "texture (MAT= alone: not painted); repeat for more")
        sp.add_argument("--cut", action="append", metavar="AXIS=MM[:JOINT]",
                        help="cut into pieces across an axis at that many mm (or AXIS=50%%), with a joint: flat, pegs, "
                             "dowels, magnets or key (default pegs); repeat for more cuts. Each piece is written as "
                             "<output>_pieceN, laid on a cut face")
        sp.add_argument("--clearance", type=float, default=0.2, help="gap in mm between parts of a joint (with --cut)")
        sp.add_argument("--watertight", action="store_true",
                        help="wrap the model into one watertight solid: parts joined, the surfaces inside one another "
                             "removed, hollows sealed inside filled (before --cut, if given)")
        sp.add_argument("--close-gaps", type=float, default=0.0, metavar="MM",
                        help="with --watertight: also close gaps between parts up to this wide")
        sp.add_argument("--drop-loose", type=float, default=0.0, metavar="PERCENT",
                        help="with --watertight: remove loose bits smaller than this %% of the model's volume")
        sp.add_argument("--keep-hollows", action="store_true", help="with --watertight: leave hollows sealed inside open")
        sp.add_argument("--sheet", type=float, default=1.2, metavar="MM",
                        help="with --watertight or --cut: thickness given to open surfaces (default 1.2)")

    s = sub.add_parser("textures", help="save a model's textures as PNG")
    s.add_argument("file")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--textures", action="append", help="extra folder to search for textures")
    s.set_defaults(func=cmd_textures)

    s = sub.add_parser("export", help="export the original model")
    model_args(s)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("enhance", help="remesh evenly, displace with textures, export")
    model_args(s)
    s.add_argument("-n", "--vertices", type=int, default=1_000_000, help="target vertex count")
    s.add_argument("--method", choices=["subdivide", "isotropic"], default="subdivide",
                   help="subdivide: exact UVs, keeps edges; isotropic: exact count, uniform triangles")
    s.add_argument("--source", choices=["normal", "height", "none"], default="normal")
    s.add_argument("--flat", action="store_true",
                   help="keep new vertices on the flat triangles (default: round them off along the model's normals)")
    s.add_argument("--strength", type=float, default=1.0, help="relief multiplier (1 = as implied by the normal map)")
    s.add_argument("--height-range", type=float, default=1.0, help="mm of displacement for a full-range height map")
    s.add_argument("--green", choices=["auto", "directx", "opengl"], default="auto")
    s.add_argument("--detail", type=float, default=1 / 16, help="high-pass radius as a fraction of texture size")
    s.add_argument("--max-mm", type=float, help="clamp displacement to this many mm")
    s.add_argument("--integration", choices=["mesh", "texture"], default="mesh",
                   help="integrate normal-map slopes on the model's surface (default) or per texture (v0.1)")
    s.add_argument("--edge-keeping", type=float, default=0.0, help="0..1, keep inconsistent slopes from smearing")
    s.add_argument("--deblock", choices=["off", "smooth", "ai"], default="off", help="block-compression clean-up")
    s.add_argument("--edge-smoothing", type=float, default=1.0,
                   help="0..1, smooth stair-stepped edges in normal maps along their length (0 = off)")
    s.add_argument("--use-map", action="append", default=[], metavar="TEXTURE=FILE",
                   help="use a normal map upscaled elsewhere for this texture (repeatable)")
    s.add_argument("--upsample", choices=["off", "guided", "ai"], default="off", help="upsample normal maps")
    s.add_argument("--ai-model", default="", help="upscaling .onnx model (path, or a name in the models folder); "
                   "default: the included one for each texture's compression")
    s.add_argument("--deblock-model", default="", help="1x deblocking .onnx model")
    s.add_argument("--edge-hints", action="store_true", help="use specular/metallic mask edges as hints")
    s.add_argument("--albedo-detail", type=float, default=0.0, help="mm of fine relief guessed from the albedo")
    s.add_argument("--detail-normals", action="store_true", help="add the materials' tiled detail normal maps")
    s.add_argument("--save-heights", help="folder to save the generated height maps")
    s.set_defaults(func=cmd_enhance)

    s = sub.add_parser("gui", help="open the explorer (the default with no command)")
    s.add_argument("folders", nargs="*", help="game folders to add to the library")
    s.add_argument("--browser", action="store_true", help="open in the web browser instead of an app window")
    s.add_argument("--port", type=int, default=0, help="port for the local server (default: any free port)")
    s.set_defaults(func=cmd_gui)

    # the desktop app's engine: commands on stdin, answers on stdout (app/engine.py)
    s = sub.add_parser("engine")
    s.add_argument("folders", nargs="*", help="game folders to add to the library")
    s.set_defaults(func=cmd_engine)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.oodle:
        from .importers.fromsoft.formats import oodle

        oodle.add_search_dir(args.oodle)
    if not args.command:
        args.folders = []
        return cmd_gui(args)
    from .errors import FormatError

    try:
        return args.func(args)
    except (FormatError, ValueError, FileNotFoundError) as exc:
        sys.stderr.write("\n")
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
