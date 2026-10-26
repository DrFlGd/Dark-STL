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
    except FormatError:
        textures = FromSoftwareImporter.texture_pack(path)  # a texture pack rather than a model
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
    opts.material_names = [m.name for m in model.materials]
    if Path(args.output).suffix.lower() in (".obj", ".glb"):
        for m in np.unique(mesh.face_material):
            m = int(m)
            key = model.materials[m].albedo if m < len(model.materials) else None
            if key and key in model.textures:
                try:
                    opts.textures[m] = model.textures[key].decode()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: could not decode {key}: {exc}", file=sys.stderr)
    return opts


def cmd_export(args) -> int:
    from .export import export_mesh

    model = _open(args)
    mesh = model.build_mesh(_mesh_options(args))
    if mesh.n_faces == 0:
        print(f"error: {model.name} has no triangles to export", file=sys.stderr)
        return 1
    path = export_mesh(mesh, args.output, _export_options(args, model, mesh))
    print(f"Wrote {path} ({mesh.n_vertices:,} vertices, {mesh.n_faces:,} triangles)")
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
    path = export_mesh(result.displaced, args.output, _export_options(args, model, result.displaced))
    st = result.stats()
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

    s = sub.add_parser("info", help="describe a model, texture pack or archive")
    s.add_argument("file")
    s.add_argument("--textures", action="append", help="extra folder to search for textures")
    s.set_defaults(func=cmd_info)

    def model_args(sp):
        sp.add_argument("file", help="model file: .partsbnd/.chrbnd/... archive, .flver or .obj")
        sp.add_argument("-o", "--output", required=True, help="output file (.stl .obj .ply .glb)")
        sp.add_argument("--variant", "--inner", dest="variant",
                        help="which model to use from a file that holds several (see 'info')")
        sp.add_argument("--parts", "--meshes", dest="parts", help="comma-separated part indices to include (see 'info')")
        sp.add_argument("--option", action="append", metavar="KEY=on|off",
                        help="importer option (see 'info'), e.g. --option mm=on for an OBJ in millimetres")
        sp.add_argument("--no-bones", action="store_true", help="same as --option bones=off")
        sp.add_argument("--pose", metavar="ANIMATION@FRAME",
                        help="pose the model with an animation from its anibnd (see 'info'), e.g. a000_003000@12")
        sp.add_argument("--scale", type=float, default=1000.0, help="unit scale (default 1000: metres -> mm)")
        sp.add_argument("--ascii", action="store_true", help="ASCII STL")
        sp.add_argument("--textures", action="append", help="extra folder to search for textures")

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
