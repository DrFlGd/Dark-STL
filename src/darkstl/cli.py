"""Command-line interface.

    darkstl scan  <folder>                    list the models found, grouped by game and type
    darkstl info  <file>                      describe a model, texture pack or archive
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
    from .assets import read_bytes
    from .formats import binder, dds, flver, tpf

    path = Path(args.file)
    data = read_bytes(path)
    if flver.is_flver(data):
        _print_flver(flver.read_flver(data))
    elif tpf.is_tpf(data):
        pack = tpf.read_tpf(data)
        print(f"TPF ({tpf.PLATFORM_NAMES.get(pack.platform, pack.platform)}), {len(pack.textures)} textures")
        for t in pack.textures:
            try:
                info = dds.parse_dds(t.to_dds())
                desc = f"{info.width}x{info.height} {info.codec} mips={info.mipmaps}"
            except Exception as exc:  # noqa: BLE001 - report and keep listing
                desc = f"format {t.format} ({exc})"
            print(f"  {t.name}: {desc}")
    elif binder.is_binder(data):
        b = binder.read_binder(data)
        print(f"{b.kind} archive, {len(b.files)} files")
        for f in b.files:
            print(f"  [{f.id}] {f.name}")
        for f in b.by_extension(".flver", ".flver.dcx"):
            print(f"\n{f.basename}:")
            _print_flver(flver.read_flver(f.data))
    elif data[:4] == b"DDS ":
        info = dds.parse_dds(data)
        print(f"DDS {info.width}x{info.height} {info.codec} mips={info.mipmaps}")
    else:
        print(f"Unrecognised file (magic {data[:4]!r})")
        return 1
    return 0


def _print_flver(f) -> None:
    print(f"FLVER version 0x{f.version:X} ({f.game_hint}), {'big' if f.big_endian else 'little'}-endian")
    print(f"  bounds {np.round(f.bbox_min, 3).tolist()} .. {np.round(f.bbox_max, 3).tolist()}")
    print(f"  {len(f.bones)} bones, {len(f.materials)} materials, {len(f.meshes)} meshes")
    for i, m in enumerate(f.meshes):
        mat = f.materials[m.material_index].name if m.material_index < len(f.materials) else "?"
        print(f"  mesh {i}: {m.vertices.count} vertices, {len(m.triangles())} triangles, material '{mat}', "
              f"{'skinned' if m.dynamic else 'rigid'}, {len(m.vertices.uvs)} UV sets")
    for i, mat in enumerate(f.materials):
        print(f"  material {i}: {mat.name}  [{mat.mtd.replace(chr(92), '/').rsplit('/', 1)[-1]}]")
        for t in mat.textures:
            print(f"      {t.type}: {t.stem or '(no path)'}")


def _extra_dirs(args) -> tuple:
    return tuple(args.textures or ())


def cmd_textures(args) -> int:
    from .assets import TextureRef, add_texture_file, load_model
    from .export import write_png
    from .formats.binary import FormatError

    path = Path(args.file)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    textures: dict[str, TextureRef] = {}
    try:
        textures = load_model(path, extra_texture_dirs=_extra_dirs(args)).textures
    except FormatError:
        add_texture_file(textures, path)
    if not textures:
        print("No textures found")
        return 1
    for key, ref in sorted(textures.items()):
        try:
            write_png(ref.decode(), out / f"{ref.name}.png")
            print(f"  {ref.name}.png")
        except Exception as exc:  # noqa: BLE001
            print(f"  {ref.name}: failed ({exc})")
    return 0


def _mesh_options(args):
    from .geometry.model_mesh import MeshOptions

    return MeshOptions(include_meshes=_parse_meshes(args.meshes), apply_bone_transforms=not args.no_bones)


def _export_options(args, asset, mesh):
    from .export import ExportOptions
    from .materials import resolve_materials

    opts = ExportOptions(scale=args.scale, ascii_stl=getattr(args, "ascii", False))
    infos = resolve_materials(asset)
    opts.material_names = [m.name for m in infos]
    if Path(args.output).suffix.lower() in (".obj", ".glb"):
        for m in np.unique(mesh.face_material):
            m = int(m)
            if m < len(infos) and infos[m].albedo:
                try:
                    opts.textures[m] = asset.textures[infos[m].albedo].decode()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: could not decode {infos[m].albedo}: {exc}", file=sys.stderr)
    return opts


def cmd_export(args) -> int:
    from .assets import load_model
    from .export import export_mesh
    from .geometry.model_mesh import flver_to_trimesh

    asset = load_model(args.file, args.inner, _extra_dirs(args))
    mesh = flver_to_trimesh(asset.flver, _mesh_options(args))
    path = export_mesh(mesh, args.output, _export_options(args, asset, mesh))
    print(f"Wrote {path} ({mesh.n_vertices:,} vertices, {mesh.n_faces:,} triangles)")
    return 0


def cmd_enhance(args) -> int:
    from .assets import load_model
    from .export import export_mesh, write_png
    from .pipeline import EnhanceSettings, enhance

    asset = load_model(args.file, args.inner, _extra_dirs(args))
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
    )
    t0 = time.time()
    result = enhance(asset, settings, _progress)
    sys.stderr.write("\n")
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    path = export_mesh(result.displaced, args.output, _export_options(args, asset, result.displaced))
    st = result.stats()
    print(f"Wrote {path}: {st['vertices']:,} vertices, {st['faces']:,} triangles "
          f"(from {st['source_vertices']:,}); max displacement {st['max_displacement_mm']:.3f} mm "
          f"[{time.time() - t0:.1f}s]")
    for mat, f in result.fields.items():
        print(f"  material {mat}: {asset.textures[f.texture].name}, green={f.field.convention or 'n/a'}, "
              f"texel={f.texel_size * 1000:.3f} mm")
        if args.save_heights:
            d = Path(args.save_heights)
            d.mkdir(parents=True, exist_ok=True)
            h = f.world
            span = float(np.abs(h).max()) or 1.0
            write_png((h / span * 0.5 + 0.5).astype(np.float32), d / f"{asset.textures[f.texture].name}_height.png")
    return 0


def cmd_gui(args) -> int:
    from .ui.app import main as gui_main

    return gui_main(args.folders)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="darkstl", description="FromSoftware model explorer and detail enhancer")
    p.add_argument("--version", action="version", version=f"darkstl {__version__}")
    p.add_argument("--oodle", help="path to oo2core_6_win64.dll (Sekiro / Elden Ring files)")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="list models in extracted game folders")
    s.add_argument("folders", nargs="+")
    s.add_argument("--game", help="game name to use instead of auto-detection")
    s.add_argument("--lod", action="store_true", help="include low-detail (_l) variants")
    s.add_argument("--limit", type=int, default=12, help="names to show per group")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("info", help="describe a game file")
    s.add_argument("file")
    s.set_defaults(func=cmd_info)

    def model_args(sp):
        sp.add_argument("file", help="model archive (.partsbnd/.chrbnd/...) or .flver")
        sp.add_argument("-o", "--output", required=True, help="output file (.stl .obj .ply .glb)")
        sp.add_argument("--inner", help="FLVER name inside an archive that holds several")
        sp.add_argument("--meshes", help="comma-separated mesh indices to include (see 'info')")
        sp.add_argument("--no-bones", action="store_true", help="do not move rigid meshes by their bones")
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
    s.add_argument("--save-heights", help="folder to save the generated height maps")
    s.set_defaults(func=cmd_enhance)

    s = sub.add_parser("gui", help="open the explorer")
    s.add_argument("folders", nargs="*")
    s.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.oodle:
        from .formats import oodle

        oodle.add_search_dir(args.oodle)
    if not args.command:
        args.folders = []
        return cmd_gui(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
