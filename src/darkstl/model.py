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
from typing import Callable, Protocol

import numpy as np

from .geometry.mesh import TriMesh


@dataclass(eq=False)
class Texture:
    """A texture that is decoded on first use."""

    name: str
    source: str  # where it was found, for display
    load: Callable[[], np.ndarray] = field(repr=False)  # -> (H, W, 4) uint8 or float32 RGBA
    describe_fn: Callable[[], str] | None = field(default=None, repr=False)

    def decode(self) -> np.ndarray:
        return _decode_cached(self)

    def describe(self) -> str:
        """Format and size, for display."""
        if self.describe_fn is not None:
            return self.describe_fn()
        h, w = self.decode().shape[:2]
        return f"{w} x {h}"


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

    def build_mesh(self, options: MeshOptions | None = None) -> TriMesh:
        options = options or MeshOptions()
        flags = {o.key: o.default for o in self.options}
        flags.update(options.flags)
        return self.build(MeshOptions(options.parts, flags, options.pose))


class Animations(Protocol):
    """What an importer offers for posing a model: a list of animations and their frame counts.

    ``MeshOptions.pose`` names one of them and a frame; the importer's ``build`` does the posing.
    """

    def list(self) -> list: ...  # objects with .name, .frames, .duration, .fps and .as_dict()

    def info(self, name: str): ...


CATEGORY_ORDER = ["Weapons", "Armor", "Characters", "Faces & Hair", "Objects", "Map Pieces", "Models", "Other"]

# what a low-detail copy adds to its full model's file stem (wp_a_0300_l, c1100_lod1)
LOD_SUFFIX = re.compile(r"_l$|_lod\d*$", re.I)


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

    @property
    def id(self) -> str:
        """A short stable id for the file, used by the app's pages and its preview cache."""
        return path_id(self.path)

    @property
    def sort_key(self) -> tuple:
        cat = CATEGORY_ORDER.index(self.category) if self.category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return (self.game, cat, self.group, self.stem)


def path_id(path: str | os.PathLike) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]
