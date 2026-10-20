"""Importer for Wavefront OBJ files with MTL materials.

This covers models exported by other tools (Blender, model viewers, earlier
Dark STL exports), so anything that can be saved as OBJ can be upscaled.

* Groups (``o``/``g``) become parts that can be left out.
* ``map_Kd`` is the albedo, ``norm`` the normal map and ``disp`` the height
  map. ``map_Bump``/``bump`` is a normal map when it looks like one (mostly
  blue, named ``_n``/``normal``) and a height map otherwise.
* OBJ is Y-up with V pointing up; the export frame is Z-up with V pointing
  down, so positions are rotated (no mirroring) and V is flipped.
* OBJ has no units. Metres are assumed unless the file is a Dark STL export
  that says otherwise or is so large it can only be millimetres.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from ..errors import FormatError
from ..geometry.mesh import TriMesh, weld
from ..model import BuildOption, CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from ..textures import dds
from .base import Importer, ScanProgress

Y_UP_TO_Z_UP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
MM_GUESS_SIZE = 20.0  # models wider than this many units are taken to be in millimetres
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds")

# Options of MTL texture statements and how many values each takes (-o/-s/-t take 1 to 3).
_MAP_OPTIONS = {"-blendu": 1, "-blendv": 1, "-bm": 1, "-boost": 1, "-cc": 1, "-clamp": 1, "-imfchan": 1,
                "-mm": 2, "-texres": 1, "-type": 1, "-o": 3, "-s": 3, "-t": 3}
_NORMAL_NAME = re.compile(r"(_n|_nm|_nrm|normal)$", re.I)


class _Obj:
    """The parsed contents of an OBJ file (OBJ's own frame and units)."""

    def __init__(self) -> None:
        self.v: list[str] = []
        self.vt: list[str] = []
        self.vn: list[str] = []
        self.corners: list[tuple[int, int, int]] = []  # (v, vt, vn) per triangle corner, 1-based or negative
        self.face_material: list[int] = []
        self.face_group: list[int] = []
        self.materials: list[str] = []
        self.groups: list[str] = []
        self.mtllibs: list[str] = []
        self.units_per_metre: float | None = None


def _index(token: str, count: int) -> int:
    """0-based index from an OBJ index (1-based, or negative counting back), -1 when absent."""
    if not token:
        return -1
    i = int(token)
    return i - 1 if i > 0 else count + i


def parse_obj(text: str) -> _Obj:
    obj = _Obj()
    material = 0
    group = 0
    mat_ids: dict[str, int] = {}
    group_ids: dict[str, int] = {}
    pending = ""
    for raw in text.splitlines():
        line = pending + raw
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        pending = ""
        line = line.strip()
        if not line:
            continue
        if line[0] == "#":
            m = re.search(r"([\d.]+)\s+units per metre", line)
            if m:
                obj.units_per_metre = float(m.group(1))
            continue
        tag, _, rest = line.partition(" ")
        if tag == "v":
            obj.v.append(rest)
        elif tag == "vt":
            obj.vt.append(rest)
        elif tag == "vn":
            obj.vn.append(rest)
        elif tag == "f":
            nv, nt, nn = len(obj.v), len(obj.vt), len(obj.vn)
            refs = []
            for tok in rest.split():
                parts = tok.split("/")
                refs.append((_index(parts[0], nv),
                             _index(parts[1], nt) if len(parts) > 1 else -1,
                             _index(parts[2], nn) if len(parts) > 2 else -1))
            for k in range(1, len(refs) - 1):  # fan-triangulate polygons
                obj.corners += [refs[0], refs[k], refs[k + 1]]
                obj.face_material.append(material)
                obj.face_group.append(group)
        elif tag == "usemtl":
            name = rest.strip()
            if name not in mat_ids:
                mat_ids[name] = len(obj.materials)
                obj.materials.append(name)
            material = mat_ids[name]
        elif tag in ("o", "g"):
            name = rest.strip() or "default"
            if name not in group_ids:
                group_ids[name] = len(obj.groups)
                obj.groups.append(name)
            group = group_ids[name]
        elif tag == "mtllib":
            obj.mtllibs.append(rest.strip())
    if not obj.groups:
        obj.groups.append("Mesh")
    if not obj.materials:
        obj.materials.append("default")
    return obj


def _floats(lines: list[str], width: int) -> np.ndarray:
    if not lines:
        return np.zeros((0, width))
    rows = [ln.split()[:width] for ln in lines]
    rows = [r + ["0"] * (width - len(r)) for r in rows]
    return np.asarray(rows, np.float64)


def parse_map_statement(rest: str) -> tuple[str, dict[str, list[float]]]:
    """The file name and options of an MTL texture statement such as ``-bm 0.5 -s 2 2 leather_n.png``."""
    tokens = rest.split()
    options: dict[str, list[float]] = {}
    i = 0
    while i < len(tokens) and tokens[i].lower() in _MAP_OPTIONS:
        key, n = tokens[i].lower(), _MAP_OPTIONS[tokens[i].lower()]
        i += 1
        values = []
        while i < len(tokens) and len(values) < n:
            try:
                values.append(float(tokens[i]))
            except ValueError:
                if key in ("-clamp", "-imfchan", "-type", "-blendu", "-blendv", "-cc"):
                    values.append(0.0)  # on/off and channel letters
                else:
                    break
            i += 1
        options[key] = values
    return " ".join(tokens[i:]), options


def parse_mtl(text: str) -> dict[str, dict[str, tuple[str, dict]]]:
    """Material name -> statement ('map_kd', 'norm', ...) -> (file, options)."""
    out: dict[str, dict[str, tuple[str, dict]]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag, _, rest = line.partition(" ")
        low = tag.lower()
        if low == "newmtl":
            current = out.setdefault(rest.strip(), {})
        elif current is not None and (low.startswith("map_") or low in ("bump", "norm", "disp", "refl")):
            current[low] = parse_map_statement(rest)
    return out


def load_image(path: Path) -> np.ndarray:
    """RGBA image: uint8 for 8-bit files, float32 in 0..1 for 16-bit and float ones (height maps)."""
    if path.suffix.lower() == ".dds":
        return dds.decode_dds(path.read_bytes())
    from PIL import Image

    with Image.open(path) as im:
        if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
            arr = np.asarray(im, np.float32)
            if im.mode == "F":
                lo, hi = float(arr.min()), float(arr.max())
                arr = (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)
            else:
                arr = arr / 65535.0
            return np.dstack([arr, arr, arr, np.ones_like(arr)]).astype(np.float32)
        return np.asarray(im.convert("RGBA"), np.uint8)


def looks_like_normal_map(img: np.ndarray) -> bool:
    f = img.astype(np.float32) / (255.0 if img.dtype == np.uint8 else 1.0)
    r, g, b = (float(f[..., c].mean()) for c in range(3))
    return b > 0.65 and abs(r - 0.5) < 0.15 and abs(g - 0.5) < 0.15


class _TextureFinder:
    """Find texture files by the (often stale) paths written in MTL files."""

    def __init__(self, dirs: list[Path]) -> None:
        self.dirs = [d for d in dirs if d.is_dir()]
        self._listings: dict[Path, dict[str, Path]] = {}

    def _listing(self, d: Path) -> dict[str, Path]:
        if d not in self._listings:
            try:
                self._listings[d] = {p.name.lower(): p for p in d.iterdir() if p.is_file()}
            except OSError:
                self._listings[d] = {}
        return self._listings[d]

    def find(self, name: str, base: Path) -> Path | None:
        name = name.strip().strip('"')
        if not name:
            return None
        direct = Path(name) if os.path.isabs(name) else base / name.replace("\\", "/")
        if direct.is_file():
            return direct
        leaf = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        stem = leaf.rsplit(".", 1)[0]
        try:
            subdirs = sorted(p for p in base.iterdir() if p.is_dir())  # e.g. a 'textures' folder next to the OBJ
        except OSError:
            subdirs = []
        for d in [base, *subdirs, *self.dirs]:
            listing = self._listing(d)
            if leaf in listing:
                return listing[leaf]
            for ext in _IMAGE_EXTENSIONS:  # same name, another format (e.g. .tga converted to .png)
                if stem + ext in listing:
                    return listing[stem + ext]
        return None


def _describe_image(path: Path) -> str:
    if path.suffix.lower() == ".dds":
        info = dds.parse_dds(path.read_bytes())
        return f"{info.width} x {info.height}, {info.codec}, {info.mipmaps} mips"
    from PIL import Image

    with Image.open(path) as im:
        return f"{im.width} x {im.height}, {im.format} {im.mode}"


class WavefrontImporter(Importer):
    key = "wavefront"
    name = "Wavefront OBJ"
    file_filter = "Wavefront OBJ (*.obj)"

    def can_open(self, path: Path) -> bool:
        return Path(path).suffix.lower() == ".obj"

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        out = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for name in sorted(filenames):
                if name.lower().endswith(".obj"):
                    path = Path(dirpath) / name
                    rel = path.parent.relative_to(folder).as_posix()
                    out.append(CatalogEntry(game, "Models", "" if rel == "." else rel, path.stem, path.stem, path,
                                            importer=self.key, packed=False))
        return out

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        path = Path(path)
        try:
            obj = parse_obj(path.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, IndexError) as exc:
            raise FormatError(f"{path.name}: not a readable OBJ file ({exc})") from exc
        if not obj.corners:
            raise FormatError(f"{path.name} has no faces")
        notes: list[str] = []

        mtl: dict[str, dict[str, tuple[str, dict]]] = {}
        for lib in obj.mtllibs:
            lib_path = path.parent / lib.replace("\\", "/")
            if lib_path.is_file():
                mtl.update(parse_mtl(lib_path.read_text(encoding="utf-8", errors="replace")))
            else:
                notes.append(f"Material library {lib} was not found next to the OBJ")
        finder = _TextureFinder([path.parent, *(Path(d) for d in texture_dirs)])
        textures: dict[str, Texture] = {}

        def texture(statement: tuple[str, dict] | None) -> str | None:
            if statement is None:
                return None
            found = finder.find(statement[0], path.parent)
            if found is None:
                notes.append(f"Texture {statement[0]} was not found")
                return None
            key = found.name.lower()
            if key not in textures:
                textures[key] = Texture(found.name, str(found), lambda p=found: load_image(p),
                                        lambda p=found: _describe_image(p))
            return key

        materials = []
        for name in obj.materials:
            slots = mtl.get(name, {})
            albedo = texture(slots.get("map_kd"))
            normal = texture(slots.get("norm"))
            height = texture(slots.get("disp"))
            bump = slots.get("map_bump") or slots.get("bump")
            bump_key = texture(bump)
            guessed = False
            if bump_key:
                stem = Path(bump[0]).stem
                is_normal = bool(_NORMAL_NAME.search(stem)) or looks_like_normal_map(textures[bump_key].decode())
                guessed = True
                if is_normal and normal is None:
                    normal = bump_key
                elif not is_normal and height is None:
                    height = bump_key
            scale = (1.0, 1.0)
            for key, statement in (("norm", slots.get("norm")), ("bump", bump)):
                if statement and statement[1].get("-s"):
                    s = statement[1]["-s"]
                    scale = (s[0], s[1] if len(s) > 1 else s[0])
                    break
            details = [f"{k}: {v[0]}" for k, v in slots.items()]
            materials.append(Material(name, albedo, normal, height, scale, guessed, details))

        v = _floats(obj.v, 3)
        corners = np.asarray(obj.corners, np.int64).reshape(-1, 3, 3)
        if (corners[..., 0] < 0).any() or (corners[..., 0] >= len(v)).any():
            raise FormatError(f"{path.name}: a face refers to a vertex that does not exist")
        vt = _floats(obj.vt, 2)
        vn = _floats(obj.vn, 3)
        face_material = np.asarray(obj.face_material, np.int32)
        face_group = np.asarray(obj.face_group, np.int32)
        parts = [Part(g, int(np.bincount(face_material[face_group == i]).argmax()) if (face_group == i).any() else 0,
                      len(np.unique(corners[face_group == i][..., 0])), int((face_group == i).sum()))
                 for i, g in enumerate(obj.groups)]

        size = float(np.linalg.norm(v.max(0) - v.min(0))) if len(v) else 0.0
        fixed_scale = None  # set when the file states units other than metres or millimetres
        if obj.units_per_metre:
            mm = abs(obj.units_per_metre - 1000.0) < 1e-6
            if not mm and abs(obj.units_per_metre - 1.0) > 1e-6:
                fixed_scale = 1.0 / obj.units_per_metre
        else:
            mm = size > MM_GUESS_SIZE
            if mm:
                notes.append(f"Read as millimetres because the model is {size:.0f} units across; "
                             "switch the option off if it is in metres")

        def build(options: MeshOptions) -> TriMesh:
            keep = np.ones(len(face_group), bool) if options.parts is None else np.isin(face_group, list(options.parts))
            c = corners[keep]
            scale = fixed_scale or (0.001 if options.flags.get("mm") else 1.0)
            frame = Y_UP_TO_Z_UP if options.flags.get("y_up", True) else np.eye(3)
            pos = (v @ frame.T) * scale
            uv = None
            if len(vt):
                ti = c[..., 1]
                uv = np.where((ti >= 0)[..., None], vt[np.clip(ti, 0, len(vt) - 1)], 0.0)
                uv[..., 1] = 1.0 - uv[..., 1]
                uv = uv.astype(np.float32)
            normals = None
            ni = c[..., 2]
            if len(vn) and (ni >= 0).all():
                n = vn[np.clip(ni, 0, len(vn) - 1)] @ frame.T
                length = np.linalg.norm(n, axis=-1, keepdims=True)
                normals = (n / np.where(length > 0, length, 1)).astype(np.float32)
            mesh = TriMesh(pos, c[..., 0], uv, face_material[keep], face_group[keep], corner_normals=normals)
            return weld(mesh.subset(np.ones(mesh.n_faces, bool)))  # drop vertices of parts left out, then weld

        info = [f"Wavefront OBJ: {len(v):,} vertices, {len(face_group):,} triangles, {len(obj.groups)} groups, "
                f"{len(obj.materials)} materials",
                f"{'normals' if len(vn) else 'no normals'}, {'UVs' if len(vt) else 'no UVs'}, "
                f"{size:.3g} units across"]
        if not len(vt):
            notes.append("The model has no UVs, so textures cannot be used for displacement")
        options = [BuildOption("y_up", "Y axis points up", True,
                               "Most programs save OBJ with Y up. Turn off for files saved Z-up.")]
        if fixed_scale is None:
            options.append(BuildOption("mm", "Coordinates are in millimetres", mm,
                                       "OBJ has no units. Turn on if the model is in millimetres rather than metres."))
        return SourceModel(name=path.stem, path=path, importer=self.key, build=build, parts=parts,
                           materials=materials, textures=textures, options=options, info=info, notes=notes)
