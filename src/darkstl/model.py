"""What every importer produces and the rest of Dark STL consumes.

An importer turns some game's files into a :class:`SourceModel`: a triangle
mesh in the export frame (right-handed, Z up, metres) with per-corner UVs and
shading normals, the materials that say which texture is the albedo, normal or
height map, and the textures themselves as RGBA arrays. The upscaling
pipeline, the viewer and the exporters only ever see this, so supporting a new
game means writing an importer and nothing else.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from .geometry.mesh import TriMesh


@dataclass(eq=False)
class Texture:
    """A texture that is decoded on first use."""

    name: str
    source: str  # where it was found, for display
    load: Callable[[], np.ndarray] = field(repr=False)  # -> (H, W, 4) uint8 or float32 RGBA
    describe_fn: Callable[[], str] | None = field(default=None, repr=False)
    # -> the texture as a DDS file, still compressed as the game keeps it, when the importer can give it:
    # what a model package stores (smaller than a picture, and nothing to decode or encode on import)
    raw: Callable[[], bytes] | None = field(default=None, repr=False)

    def decode(self) -> np.ndarray:
        return _decode_cached(self)

    def describe(self) -> str:
        """Format and size, for display."""
        if self.describe_fn is not None:
            return self.describe_fn()
        h, w = self.decode().shape[:2]
        return f"{w} x {h}"

    def codec(self) -> str | None:
        """How the texture is stored ("BC1", "BC7", "R8G8B8A8"...), when the format says."""
        if self.describe_fn is None:
            return None
        try:
            parts = [x.strip() for x in self.describe_fn().split(",")]
        except Exception:  # noqa: BLE001 - only a hint
            return None
        return parts[1] if len(parts) > 2 else None


@lru_cache(maxsize=48)
def _decode_cached(texture: Texture) -> np.ndarray:
    return texture.load()


@dataclass
class Material:
    """One material and the texture keys it uses for each role."""

    name: str
    albedo: str | None = None
    normal: str | None = None
    height: str | None = None
    normal_scale: tuple[float, float] = (1.0, 1.0)  # UV tiling of the normal map
    guessed: bool = False  # roles were matched by texture names rather than read from the file
    details: list[str] = field(default_factory=list)  # shader and texture slots, for display
    normal_green: str | None = None  # "directx" or "opengl" when the importer knows how its normal maps are made
    specular: str | None = None  # specular / reflectance / metallic mask: hints where materials meet
    detail_normal: str | None = None  # small tiled normal map layered on top (leather grain, cloth weave)
    detail_scale: tuple[float, float] = (1.0, 1.0)  # its UV tiling


@dataclass
class Part:
    """A piece of the model that can be left out (a FLVER mesh, an OBJ group...)."""

    name: str
    material: int
    vertices: int
    triangles: int


@dataclass
class BuildOption:
    """An importer-specific on/off switch for building the mesh, shown in the app."""

    key: str
    label: str
    default: bool = True
    tooltip: str = ""


@dataclass
class MeshOptions:
    parts: set[int] | None = None  # part indices to include (None = all)
    flags: dict[str, bool] = field(default_factory=dict)  # BuildOption values by key
    pose: tuple[str, int] | None = None  # (animation name, frame) to pose the model in; None = bind pose


@dataclass
class PaintRecipe:
    """How a paint that's mixed for each model it goes on (a Borderlands 2 gun skin) mixes it, kept so that a
    model package can mix it without the game: ``kind`` names the mixing (see :mod:`darkstl.package`),
    ``values`` are its numbers (as JSON), ``textures`` the pictures it reads."""

    kind: str
    values: dict
    textures: dict[str, Texture] = field(default_factory=dict)


@dataclass
class PaintUVs:
    """What a model gives a paint mixed for it: its texture coordinates (UV0 and UV1 triangles, (n, 3, 2)),
    read when first needed. ``key`` says where from (a file and its date), so a mixed paint can be kept."""

    key: list
    load: Callable[[], tuple[np.ndarray, np.ndarray | None]] = field(repr=False)


@dataclass
class SourceModel:
    name: str
    path: Path
    importer: str  # Importer.key
    build: Callable[[MeshOptions], TriMesh] = field(repr=False)
    variant: str | None = None  # which model inside a file that holds several
    variants: list[str] = field(default_factory=list)  # all of them, when the file holds several
    game: str | None = None
    parts: list[Part] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)  # indexed by TriMesh.face_material
    textures: dict[str, Texture] = field(default_factory=dict)  # key -> texture
    options: list[BuildOption] = field(default_factory=list)
    info: list[str] = field(default_factory=list)  # description lines (format, version, counts)
    notes: list[str] = field(default_factory=list)  # things the user should know
    animations: Animations | None = field(default=None, repr=False)  # how the model can be posed, if it can
    # named points parts can be fitted to (a weapon's magazine or sight socket): 4x4 matrices, column vectors,
    # in the export frame (metres, Z up), in the bind pose. See configs.Attach.
    sockets: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    configuration: dict | None = field(default=None, repr=False)  # set on a model assembled by configs.assemble
    # a paint that has no colour texture of its own but is mixed per model (a Borderlands 2 gun skin):
    # ``paint_for(target)`` returns the colour texture it gives ``target`` (made when it's first decoded),
    # or None. configs.assemble uses it for material and finish slots.
    paint_for: Callable[["SourceModel"], Texture | None] | None = field(default=None, repr=False)
    paint: PaintRecipe | None = field(default=None, repr=False)  # how ``paint_for`` mixes, for model packages
    paint_uvs: PaintUVs | None = field(default=None, repr=False)  # what such a paint is mixed from on this model
    rig: Rig | None = field(default=None, repr=False)  # the skeleton animations pose, for model packages

    def build_mesh(self, options: MeshOptions | None = None) -> TriMesh:
        options = options or MeshOptions()
        flags = {o.key: o.default for o in self.options}
        flags.update(options.flags)
        return self.build(MeshOptions(options.parts, flags, options.pose))


@dataclass
class Rig:
    """A posable model's skeleton, so a model package can pose it without the game's files.

    Matrices are 4x4, column vectors, in the export frame (metres, Z up). The mesh the model builds in
    its bind pose carries ``metadata["skin"]``: ``(joints, weights)``, (V, k) per vertex, joints indexing
    ``names`` (-1: none). A vertex then poses as the weighted sum of ``world @ inv(bind_world)`` of its
    bones, where ``world`` composes ``bind_local`` with the local matrices an animation gives bones of
    the same name (:func:`darkstl.posing.pose_world`).
    """

    names: list[str]
    parents: np.ndarray  # (B,) int, -1 for a root
    bind_local: np.ndarray  # (B, 4, 4)
    bind_world: np.ndarray  # (B, 4, 4): where the bind-pose mesh was placed with
    # (animation, frame) -> (bone names, (n, 4, 4) local matrices of those bones at that frame)
    pose_locals: Callable[[str, int], tuple[list[str], np.ndarray]] = field(repr=False)
    # animation -> (bone names, (frames, n, 4, 4)): every frame at once, when the importer can do it faster
    pose_all: Callable[[str], tuple[list[str], np.ndarray]] | None = field(default=None, repr=False)


def frame_matrix(game_to_export: np.ndarray) -> np.ndarray:
    """A 3x3 axis change as a 4x4 matrix, for carrying an importer's bone matrices into the export frame
    (``M4 @ m @ inv(M4)``)."""
    m = np.eye(4)
    m[:3, :3] = np.asarray(game_to_export, np.float64)
    return m


class Animations(Protocol):
    """What an importer offers for posing a model: a list of animations and their frame counts.

    ``MeshOptions.pose`` names one of them and a frame; the importer's ``build`` does the posing.
    """

    def list(self) -> list: ...  # objects with .name, .frames, .duration, .fps and .as_dict()

    def info(self, name: str): ...


CATEGORY_ORDER = ["Weapons", "Armor", "Characters", "Faces & Hair", "Objects", "Map Pieces", "Models", "Other"]

# what a low-detail copy adds to its full model's file stem (wp_a_0300_l, c1100_lod1, Cyberpunk's
# w_rifle_shadow / _proxy meshes)
LOD_SUFFIX = re.compile(r"_l$|_lod\d*$|_shadow$|_proxy$", re.I)


@dataclass
class CatalogEntry:
    """One model found while scanning a folder."""

    game: str
    category: str
    group: str  # sub-category, e.g. armour slot or ID range
    stem: str  # file name without extensions, e.g. 'wp_a_0300'
    label: str  # display name
    path: Path
    importer: str = ""
    is_lod: bool = False
    packed: bool = True
    config: Any = None  # a configs.Configuration (its files resolved) when this entry is a configurable model

    @property
    def id(self) -> str:
        """A short stable id for the file, used by the app's pages and its preview cache (worked out once:
        the library asks for it over and over across 100,000 models)."""
        key = (self.path, self.config.id if self.config is not None else None)
        known = self.__dict__.get("_id")
        if known is None or known[0] != key:
            text = f"{self.path}#config:{self.config.id}" if self.config is not None else self.path
            known = (key, path_id(text))
            self.__dict__["_id"] = known
        return known[1]

    @property
    def sort_key(self) -> tuple:
        cat = CATEGORY_ORDER.index(self.category) if self.category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return (self.game, cat, self.group, self.stem)


def path_id(path: str | os.PathLike) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]
