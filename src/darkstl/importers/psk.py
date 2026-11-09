"""Importer for ActorX models (``.psk`` skeletal meshes, ``.pskx`` static meshes), as UModel and FModel export
Unreal games' models.

* A PSK holds points, wedges (a point with its UV and material), triangles of wedges, materials by name, and
  for skeletal meshes a skeleton and weights. Positions are Unreal's: left-handed, Z up, in centimetres, so
  they're mirrored in Y and scaled to metres like the Unreal games' own models; faces are wound to agree with
  the stored normals.
* Each material is a part that can be left out.
* A material's textures are found by name beside the models: from its material file (UModel and FModel write
  ``<material>.mat``, FModel ``<material>.json`` as well, and UModel ``<material>.props.txt``), then from
  texture names that resemble the material's. Textures are picture files (TGA, PNG, DDS...) found anywhere
  in the folder; Unreal's normal maps have green pointing down (the DirectX way).
* The skeleton isn't read: the model is shown in the pose it was exported in.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import access
from ..errors import FormatError
from ..model import CatalogEntry, Material, MeshOptions, Part, SourceModel, Texture
from .base import Importer, ScanProgress
from .loose import PICTURES, LoosePictures, owner, picture

SUFFIXES = (".psk", ".pskx")
MATERIAL_FILES = (".mat", ".json", ".props.txt")
_PICTURE_ORDER = {s: i for i, s in enumerate((".dds", ".png", ".tga", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg"))}
# Folders tools export into: a model's textures are looked for anywhere below the nearest of them
_EXPORT_FOLDERS = {"umodelexport", "umodel_export", "exports", "export", "output", "fmodel"}


@dataclass
class PskFile:
    points: np.ndarray  # (P, 3) float32, Unreal space
    wedge_point: np.ndarray  # (W,) point of each wedge
    wedge_uv: np.ndarray  # (W, 2)
    faces: np.ndarray  # (F, 3) wedges
    face_material: np.ndarray  # (F,)
    materials: list[str]
    normals: np.ndarray | None = None  # per point or per wedge
    bones: int = 0
    chunks: list[str] = field(default_factory=list)


def read_psk(data: bytes, name: str = "") -> PskFile:
    pos = 0
    points = wedges = faces = normals = None
    face_material = None
    wedge_material = None
    materials: list[str] = []
    bones = 0
    chunks = []
    while pos + 32 <= len(data):
        cid, _flags, size, count = struct.unpack_from("<20siii", data, pos)
        cid = cid.split(b"\0", 1)[0].decode("latin-1")
        pos += 32
        end = pos + size * count
        if size < 0 or count < 0 or end > len(data):
            raise FormatError(f"{name}: chunk {cid} runs past the end of the file")
        chunks.append(cid)
        body = data[pos:end]
        if cid == "PNTS0000":
            points = np.frombuffer(body, "<f4").reshape(count, 3) if size == 12 else None
        elif cid == "VTXW0000":
            if size != 16:
                raise FormatError(f"{name}: wedges of {size} bytes aren't known")
            # the point is 16 bits and padding, or 32 bits for more wedges than 16 bits count; the material
            # a byte (some tools write 32 bits there, the same for fewer than 256 materials)
            w = np.frombuffer(body, np.dtype([("p", "<u2" if count <= 65536 else "<u4"),
                                              *([("pad", "<u2")] if count <= 65536 else []),
                                              ("u", "<f4"), ("v", "<f4"), ("m", "u1"), ("r", "u1"), ("pad2", "<u2")]))
            wedges = w
            wedge_material = w["m"].astype(np.int32)
        elif cid in ("FACE0000", "FACE3200"):
            wide = cid == "FACE3200"
            if size != (18 if wide else 12):
                raise FormatError(f"{name}: faces of {size} bytes aren't known")
            f = np.frombuffer(body, np.dtype([("w", "<u4" if wide else "<u2", 3), ("m", "u1"), ("aux", "u1"),
                                              ("smooth", "<u4")]))
            faces = f["w"].astype(np.int64)
            face_material = f["m"].astype(np.int32)
        elif cid == "MATT0000" and size == 88:
            for i in range(count):
                raw = body[i * 88:i * 88 + 64].split(b"\0", 1)[0]
                materials.append(raw.decode("utf-8", "replace") or f"Material {i}")
        elif cid in ("REFSKELT", "REFSKEL0"):
            bones = count
        elif cid == "VTXNORMS" and size == 12:
            normals = np.frombuffer(body, "<f4").reshape(count, 3)
        pos = end
    if points is None or wedges is None or faces is None:
        raise FormatError(f"{name} isn't an ActorX model (no points, wedges or faces)")
    if face_material is None:
        face_material = wedge_material[faces[:, 0]] if len(faces) else np.zeros(0, np.int32)
    n_mat = int(max(face_material.max(initial=-1), wedge_material.max(initial=-1))) + 1
    materials += [f"Material {i}" for i in range(len(materials), n_mat)]
    return PskFile(points, wedges["p"].astype(np.int64), np.column_stack([wedges["u"], wedges["v"]]), faces,
                   face_material, materials, normals, bones, chunks)


# -- material files ------------------------------------------------------------------------------------


def texture_name(ref) -> str:
    """A texture's name from however a material file refers to it (``Texture2D'/Game/A/T_X.T_X'``, a path,
    a file name, an object with ``ObjectName``/``ObjectPath``)."""
    if isinstance(ref, dict):
        ref = ref.get("ObjectName") or ref.get("ObjectPath") or ref.get("Name") or ""
    ref = str(ref).strip()
    quoted = re.search(r"'([^']*)'", ref)
    if quoted:
        ref = quoted.group(1)
    leaf = ref.replace("\\", "/").rsplit("/", 1)[-1]
    file = re.match(r"(.+)\.(png|tga|dds|jpe?g|bmp|tiff?)$", leaf, re.I)
    return file.group(1) if file else leaf.split(".", 1)[0]


def _json_textures(obj, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        if "ParameterInfo" in obj and "ParameterValue" in obj:
            info = obj["ParameterInfo"]
            param = info.get("Name", "") if isinstance(info, dict) else str(info)
            if obj["ParameterValue"]:
                out.append((str(param), texture_name(obj["ParameterValue"])))
        for key, value in obj.items():
            if key == "Textures" and isinstance(value, dict):
                for param, ref in value.items():
                    if ref and isinstance(ref, (str, dict)):
                        out.append((str(param), texture_name(ref)))
            elif key == "ReferencedTextures" and isinstance(value, list):
                out += [("", texture_name(r)) for r in value if r]
            else:
                _json_textures(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _json_textures(value, out)


def material_textures(path: Path) -> list[tuple[str, str]]:
    """(parameter, texture name) for each texture a material file names."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    low = Path(path).name.lower()
    out: list[tuple[str, str]] = []
    if low.endswith(".json"):
        try:
            _json_textures(json.loads(text), out)
        except ValueError:
            pass
    elif low.endswith(".props.txt"):
        for m in re.finditer(r"ParameterInfo\s*=\s*\{\s*Name\s*=\s*\"?([^\s,}\"]+).*?ParameterValue\s*=\s*([^\s,}]+)",
                             text, re.S):
            out.append((m.group(1), texture_name(m.group(2))))
    else:  # UModel's .mat: Diffuse=T_X_D, Normal=..., Other[0]=...
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and value.strip():
                out.append((key.strip(), texture_name(value)))
    return [(p, t) for p, t in out if t and t.lower() not in ("none", "null")]


def _role_from_name(name: str) -> str:
    from .borderlands.materials import _NAME_RULES, OTHER

    for rx, role in _NAME_RULES:
        if rx.search(name):
            return role
    return OTHER


def _base_name(material: str) -> str:
    """A material's name without its kind's prefix (``MI_Sword_Body`` -> ``sword_body``)."""
    return re.sub(r"^(mi|m|mat|mic|material)_", "", material.lower())


@dataclass
class _Index:
    """The pictures and material files below a folder, by name."""
    pictures: dict[str, list[Path]] = field(default_factory=dict)
    materials: dict[str, list[Path]] = field(default_factory=dict)


def _build_index(root: Path) -> _Index:
    index = _Index()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.lower().endswith(".darkpkg")]
        here = Path(dirpath)
        for name in files:
            low = name.lower()
            if low.endswith(PICTURES):
                index.pictures.setdefault(low.rsplit(".", 1)[0], []).append(here / name)
            else:
                for ext in MATERIAL_FILES:
                    if low.endswith(ext):
                        index.materials.setdefault(low[:-len(ext)], []).append(here / name)
                        break
    return index


def _nearest(paths: list[Path], near: Path) -> Path:
    def score(p: Path):
        common = 0
        for a, b in zip(p.parent.parts, near.parts):
            if a.lower() != b.lower():
                break
            common += 1
        return (-common, _PICTURE_ORDER.get(p.suffix.lower(), 99), p.name.lower().endswith(".json"), str(p))

    return min(paths, key=score)


class NamedTextures:
    """For an importer of models other tools exported: finds pictures and material files by name in the folder
    the models were exported into (a model's own folder's parent, or the nearest export folder)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._indexes: dict[Path, _Index] = {}

    def forget_index(self, folder: Path) -> None:
        with self._lock:
            self._indexes.pop(Path(folder), None)  # looked through again when a model is opened

    def _root(self, path: Path) -> Path:
        """The folder a model's textures and material files are looked for in."""
        path = Path(path)
        for parent in path.parents:
            if owner(parent) is not None:
                return parent
        for parent in path.parents:
            if parent.name.lower() in _EXPORT_FOLDERS:
                return parent
        return path.parent.parent if len(path.parents) > 1 else path.parent

    def _index(self, root: Path) -> _Index:
        with self._lock:
            index = self._indexes.get(root)
        if index is None:
            index = _build_index(root)
            with self._lock:
                self._indexes[root] = index
        return index

    def _materials(self, path: Path, names: list[str], texture_dirs: tuple[str, ...]):
        """Each material's (albedo, normal, specular) picture files, whether they were guessed, details, and
        the textures its file names that weren't found."""
        from .borderlands.materials import ALBEDO, NORMAL, OTHER, SPECULAR, role_from_param

        index = self._index(self._root(path))
        extra = [self._index(Path(d)) for d in texture_dirs if Path(d).is_dir()]
        near = path.parent

        def find(kind: str, name: str) -> Path | None:
            for idx in (index, *extra):
                hits = getattr(idx, kind).get(name.lower())
                if hits:
                    return _nearest(hits, near)
            return None

        out = []
        for material in names:
            roles: dict[str, Path] = {}
            details: list[str] = []
            missing: list[str] = []
            guessed = False
            file = find("materials", material)
            listed = []
            if file is not None:
                access.note_file(file, "other")
                details.append(f"material file: {file.name}")
                try:
                    listed = material_textures(file)
                except OSError:
                    listed = []
            if not listed:  # no material file: textures named like the material
                base = _base_name(material)
                if base:
                    for idx in (index, *extra):
                        for stem, hits in idx.pictures.items():
                            core = re.sub(r"^(t|tex|tx)_", "", stem)
                            if core.startswith(base) and re.fullmatch(r"(_[a-z0-9]+)?", core[len(base):]):
                                listed.append(("", hits[0].stem))
                        if listed:
                            break
                guessed = bool(listed)
            for param, tex in listed:
                role = role_from_param(param) if param else OTHER
                if role == OTHER or param.lower().startswith("other"):
                    role = _role_from_name(tex)
                    guessed = guessed or role != OTHER
                if role not in (ALBEDO, NORMAL, SPECULAR) or role in roles:
                    continue
                hit = find("pictures", tex)
                if hit is None:
                    missing.append(tex)
                    continue
                roles[role] = hit
                details.append(f"{param or role}: {hit.name}")
            out.append((roles.get(ALBEDO), roles.get(NORMAL), roles.get(SPECULAR), guessed, details, missing))
        return out


class PskImporter(NamedTextures, LoosePictures, Importer):
    key = "psk"
    name = "ActorX PSK (UModel, FModel)"
    file_filter = "ActorX models (*.psk *.pskx)"

    def can_open(self, path: Path) -> bool:
        return Path(path).suffix.lower() in SUFFIXES

    def scan(self, folder: Path, game: str, progress: ScanProgress | None = None) -> list[CatalogEntry]:
        from ..listing import files_under

        folder = Path(folder)
        out = []
        for path in files_under(folder):
            if path.suffix.lower() in SUFFIXES:
                rel = path.parent.relative_to(folder).as_posix()
                out.append(CatalogEntry(game, "Models", "" if rel == "." else rel, path.stem, path.stem, path,
                                        importer=self.key, packed=False))
        self.note(folder, out)
        self.forget_index(folder)
        return out

    def related_files(self, path: Path, textures: bool = True, animations: bool = True, lod: bool = False):
        from ..collect import RelatedFile

        path = Path(path)
        out = [RelatedFile(path, "model")]
        try:
            psk = read_psk(path.read_bytes(), path.name)
        except (FormatError, OSError):
            return out
        index = self._index(self._root(path))
        for material in psk.materials:
            hits = index.materials.get(material.lower())
            if hits:
                out.append(RelatedFile(_nearest(hits, path.parent), "other"))
        if textures:
            for albedo, normal, specular, *_ in self._materials(path, psk.materials, ()):
                out += [RelatedFile(p, "texture") for p in (albedo, normal, specular) if p is not None]
        seen, unique = set(), []
        for r in out:
            if r.path not in seen:
                seen.add(r.path)
                unique.append(r)
        return unique

    def load(self, path: Path, variant: str | None = None, texture_dirs: tuple[str, ...] = ()) -> SourceModel:
        from .borderlands.mesh import build_trimesh

        path = Path(path)
        access.note_file(path, "model")
        try:
            psk = read_psk(path.read_bytes(), path.name)
        except (struct.error, ValueError) as exc:
            raise FormatError(f"{path.name}: {exc}") from exc
        if not len(psk.faces):
            raise FormatError(f"{path.name} has no triangles")
        notes: list[str] = []
        textures: dict[str, Texture] = {}

        def texture(file: Path | None) -> str | None:
            if file is None:
                return None
            key = file.stem
            if key not in textures:
                access.note_file(file, "texture")
                textures[key] = picture(file)
            return key

        materials = []
        for name, (albedo, normal, specular, guessed, details, missing) in zip(
                psk.materials, self._materials(path, psk.materials, texture_dirs)):
            materials.append(Material(name, texture(albedo), texture(normal), guessed=guessed, details=details,
                                      normal_green="directx", specular=texture(specular)))
            if missing:
                notes.append(f"{name}: {', '.join(missing)} wasn't found beside the model")
        if all(m.albedo is None and m.normal is None for m in materials):
            notes.append("No textures were found for its materials: export them with the model (as TGA, PNG or "
                         "DDS), or choose them on the Textures tab")

        keep = (psk.faces < len(psk.wedge_point)).all(axis=1)
        faces, mats = psk.faces[keep], psk.face_material[keep]
        wedge_point = np.clip(psk.wedge_point, 0, len(psk.points) - 1)
        positions = psk.points[wedge_point]
        normals = None
        if psk.normals is not None:
            if len(psk.normals) == len(psk.points):
                normals = psk.normals[wedge_point]
            elif len(psk.normals) == len(psk.wedge_point):
                normals = psk.normals
        used = sorted(int(m) for m in np.unique(mats))
        parts = []
        for m in used:
            sel = faces[mats == m]
            parts.append(Part(psk.materials[m] if m < len(psk.materials) else f"Material {m}", m,
                              len(np.unique(wedge_point[sel])), len(sel)))

        def build(options: MeshOptions):
            if options.pose is not None:
                raise FormatError(f"{path.stem} has no animations Dark STL can pose it with")
            ranges = [(faces[mats == m], m, g) for g, m in enumerate(used)
                      if options.parts is None or g in options.parts]
            return build_trimesh(positions, normals, psk.wedge_uv, ranges)

        size = np.ptp(psk.points, axis=0) * 0.01 if len(psk.points) else np.zeros(3)
        info = [f"ActorX {'static' if path.suffix.lower() == '.pskx' else 'skeletal'} mesh: "
                f"{len(psk.points):,} points, {len(faces):,} triangles, {len(psk.materials)} materials"
                + (f", {psk.bones} bones" if psk.bones else ""),
                f"{size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} m"]
        if psk.bones:
            notes.append("Its skeleton isn't read: it is shown in the pose it was exported in.")
        return SourceModel(name=path.stem, path=path, importer=self.key, build=build, parts=parts,
                           materials=materials, textures=textures, info=info, notes=notes)

    def describe(self, path: Path) -> list[str] | None:
        return None


__all__ = ["NamedTextures", "PskFile", "PskImporter", "material_textures", "read_psk", "texture_name"]
