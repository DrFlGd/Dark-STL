"""What an FBX file's node tree describes: objects joined by connections.

* ``Objects`` holds every object (a ``Model`` node of the scene, a ``Geometry``, a ``Material``, a
  ``Texture``, a skin ``Deformer`` and its ``Cluster`` per bone, a ``Pose``, animation stacks, layers,
  curve nodes and curves), each with an id; ``Connections`` joins them child to parent, sometimes to a
  property of the parent (a texture to a material's ``DiffuseColor``, a curve to an ``X`` channel).
  FBX 6 files name objects (``Model::Box01``) instead of numbering them, and keep a mesh's geometry in its
  ``Model``; both are read the same way here.
* A mesh's geometry has control points and polygons (``PolygonVertexIndex``, the last corner of each
  polygon stored as ``-index - 1``); normals, UVs and materials are layer elements mapped per polygon
  corner, per control point, per polygon or all the same, directly or through an index array.
* A node's place is its parent's place times its local transform: translation, rotation offset and pivot,
  pre-rotation, the Euler rotation in its rotation order, inverse post-rotation, scaling offset and pivot,
  scaling (as the FBX SDK composes them). A geometric transform moves the geometry alone.
* ``GlobalSettings`` says which axis is up, which is front and which is right, and how many centimetres a
  unit is: :func:`export_frame` turns that into Dark STL's frame (metres, Z up).
* A skin's clusters each name a bone, the control points it moves with their weights, the bone's place
  at bind time (``TransformLink``) and the mesh's place relative to the bone (``Transform``).
* Animations: a stack's first layer holds curve nodes animating a node's translation, rotation or
  scaling, with a curve per axis (key times in 1/46186158000 of a second).
"""

from __future__ import annotations

import base64
import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ...errors import FormatError
from .reader import FbxNode, read

KTIME = 46186158000  # FBX time units per second
_TIME_MODES = {1: 120.0, 2: 100.0, 3: 60.0, 4: 50.0, 5: 48.0, 6: 30.0, 7: 30.0, 8: 29.97, 9: 29.97, 10: 25.0,
               11: 24.0, 12: 1000.0, 13: 23.976, 15: 96.0, 16: 72.0, 17: 59.94, 18: 119.88}
_ROTATION_ORDERS = ["XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX", "XYZ"]
TRANSFORM_PROPS = ("Lcl Translation", "Lcl Rotation", "Lcl Scaling")


@dataclass
class Obj:
    id: object  # an int (FBX 7) or the object's full name (FBX 6)
    kind: str  # the node's name: Model, Geometry, Material, Texture, Deformer...
    name: str
    sub: str  # what kind of that kind: Mesh, LimbNode, Null, Skin, Cluster, BindPose...
    node: FbxNode
    props: dict = field(default_factory=dict)  # Properties70 (or 60): name -> values

    def prop(self, name: str, default=None):
        v = self.props.get(name)
        if not v:
            return default
        return v[0] if len(v) == 1 else v

    def vector(self, name: str, default=(0.0, 0.0, 0.0)) -> np.ndarray:
        v = self.props.get(name)
        try:
            out = np.asarray([float(x) for x in v[:3]], np.float64) if v and len(v) >= 3 else None
        except (TypeError, ValueError):
            out = None
        return out if out is not None else np.asarray(default, np.float64)


def properties(node: FbxNode | None) -> dict:
    """A node's ``Properties70`` (FBX 7) or ``Properties60`` (FBX 6): name -> its values."""
    out: dict = {}
    if node is None:
        return out
    for block, skip in (("Properties70", 4), ("Properties60", 3)):
        b = node.find(block)
        if b is None:
            continue
        for p in b.children:
            if p.name in ("P", "Property") and p.values:
                out[str(p.values[0])] = list(p.values[skip:])
    return out


def _split_name(value) -> str:
    """An object's own name from its full name (``Model::Box01`` -> ``Box01``)."""
    s = str(value)
    cls, sep, name = s.partition("::")
    return name if sep and cls and " " not in cls else s


# -- transforms -------------------------------------------------------------------------------------------------


def _axis(axis: str, rad: np.ndarray) -> np.ndarray:
    """(n, 3, 3) rotations about one axis."""
    c, s = np.cos(rad), np.sin(rad)
    m = np.zeros((len(rad), 3, 3))
    i = "XYZ".index(axis)
    j, k = (i + 1) % 3, (i + 2) % 3
    m[:, i, i] = 1
    m[:, j, j] = c
    m[:, j, k] = -s
    m[:, k, j] = s
    m[:, k, k] = c
    return m


def euler(degrees: np.ndarray, order: str = "XYZ") -> np.ndarray:
    """(n, 3, 3) rotations from (n, 3) Euler angles in degrees; ``XYZ`` turns about X first, then Y, then Z."""
    d = np.radians(np.asarray(degrees, np.float64).reshape(-1, 3))
    out = np.broadcast_to(np.eye(3), (len(d), 3, 3)).copy()
    for axis in order:
        out = _axis(axis, d[:, "XYZ".index(axis)]) @ out
    return out


def _translations(t: np.ndarray) -> np.ndarray:
    t = np.asarray(t, np.float64).reshape(-1, 3)
    m = np.broadcast_to(np.eye(4), (len(t), 4, 4)).copy()
    m[:, :3, 3] = t
    return m


def _rotations(r: np.ndarray) -> np.ndarray:
    m = np.broadcast_to(np.eye(4), (len(r), 4, 4)).copy()
    m[:, :3, :3] = r
    return m


def _scalings(s: np.ndarray) -> np.ndarray:
    s = np.asarray(s, np.float64).reshape(-1, 3)
    m = np.broadcast_to(np.eye(4), (len(s), 4, 4)).copy()
    m[:, 0, 0], m[:, 1, 1], m[:, 2, 2] = s[:, 0], s[:, 1], s[:, 2]
    return m


def local_matrices(obj: Obj, t: np.ndarray | None = None, r: np.ndarray | None = None,
                   s: np.ndarray | None = None) -> np.ndarray:
    """(n, 4, 4) local transforms of a node, with its translation, rotation and scaling replaced by the (n, 3)
    values given (an animation's), else its own."""
    n = max(len(x) for x in (t, r, s) if x is not None) if any(x is not None for x in (t, r, s)) else 1
    t = np.broadcast_to(obj.vector("Lcl Translation") if t is None else t, (n, 3))
    r = np.broadcast_to(obj.vector("Lcl Rotation") if r is None else r, (n, 3))
    s = np.broadcast_to(obj.vector("Lcl Scaling", (1, 1, 1)) if s is None else s, (n, 3))
    active = bool(obj.prop("RotationActive", 0))
    order = _ROTATION_ORDERS[int(obj.prop("RotationOrder", 0) or 0) % 7] if active else "XYZ"
    pre = euler(obj.vector("PreRotation"))[0] if active else np.eye(3)
    post = euler(obj.vector("PostRotation"))[0] if active else np.eye(3)
    rp, roff = obj.vector("RotationPivot"), obj.vector("RotationOffset")
    sp, soff = obj.vector("ScalingPivot"), obj.vector("ScalingOffset")
    rot = pre @ euler(r, order) @ post.T
    m = _translations(t + roff + rp) @ _rotations(rot) @ _translations(np.broadcast_to(-rp, (n, 3)))
    return m @ _translations(np.broadcast_to(soff + sp, (n, 3))) @ _scalings(s) @ _translations(
        np.broadcast_to(-sp, (n, 3)))


def geometric_matrix(obj: Obj) -> np.ndarray:
    """What moves a node's geometry alone (not its children)."""
    return (_translations(obj.vector("GeometricTranslation")) @ _rotations(euler(obj.vector("GeometricRotation")))
            @ _scalings(obj.vector("GeometricScaling", (1, 1, 1))))[0]


def export_frame(settings: dict) -> np.ndarray:
    """The 4x4 matrix carrying a point of the file (its axes and units) into Dark STL's frame: metres, X right,
    Y away from the viewer, Z up."""

    def num(name: str, default):
        v = settings.get(name)
        try:
            return type(default)(v[0]) if v else default
        except (TypeError, ValueError):
            return default

    up, up_sign = num("UpAxis", 1), num("UpAxisSign", 1)
    front, front_sign = num("FrontAxis", 2), num("FrontAxisSign", 1)
    coord, coord_sign = num("CoordAxis", 0), num("CoordAxisSign", 1)
    if sorted((up, front, coord)) != [0, 1, 2]:
        up, up_sign, front, front_sign, coord, coord_sign = 1, 1, 2, 1, 0, 1
    m = np.zeros((3, 3))
    m[0, coord] = 1 if coord_sign >= 0 else -1
    m[1, front] = -1 if front_sign >= 0 else 1  # the front axis points at the viewer
    m[2, up] = 1 if up_sign >= 0 else -1
    scale = num("UnitScaleFactor", 1.0) / 100.0  # centimetres per unit
    if not math.isfinite(scale) or scale <= 0:
        scale = 0.01
    out = np.eye(4)
    out[:3, :3] = m * scale
    return out


# -- geometry ---------------------------------------------------------------------------------------------------


@dataclass
class Geometry:
    points: np.ndarray  # (P, 3) control points
    corners: np.ndarray  # (C,) control point of each polygon corner
    corner_poly: np.ndarray  # (C,) polygon of each corner
    triangles: np.ndarray  # (T, 3) corners, fanned from each polygon's first corner
    triangle_poly: np.ndarray  # (T,)
    polygons: int
    normals: np.ndarray | None  # (C, 3)
    uvs: np.ndarray | None  # (C, 2)
    poly_material: np.ndarray  # (polygons,) index into the model's materials
    uv_sets: int = 0


def _layer(elem: FbxNode | None, data_name: str, index_name: str, width: int, corners: np.ndarray,
           corner_poly: np.ndarray, polygons: int) -> np.ndarray | None:
    """A layer element's values per polygon corner (None if it can't be mapped)."""
    if elem is None:
        return None
    data = elem.array(data_name)
    if data is None or not len(data):
        return None
    data = data[:len(data) // width * width].reshape(-1, width)
    mapping = str(elem.value("MappingInformationType", "ByPolygonVertex"))
    reference = str(elem.value("ReferenceInformationType", "Direct"))
    index = elem.array(index_name, np.int64) if reference in ("IndexToDirect", "Index") else None
    n = len(corners)
    if mapping == "ByPolygonVertex":
        at = np.arange(n)
    elif mapping in ("ByVertex", "ByVertice", "ByControlPoint"):
        at = corners
    elif mapping == "ByPolygon":
        at = corner_poly
    elif mapping == "AllSame":
        at = np.zeros(n, np.int64)
    else:  # ByEdge and the like aren't mapped
        return None
    if index is not None:
        if not len(index):
            return None
        at = index[np.clip(at, 0, len(index) - 1)]
    at = np.clip(at, 0, len(data) - 1)
    return data[at]


def read_geometry(node: FbxNode, name: str = "") -> Geometry | None:
    points = node.array("Vertices")
    pvi = node.array("PolygonVertexIndex", np.int64)
    if points is None or pvi is None or not len(points) or not len(pvi):
        return None
    points = points[:len(points) // 3 * 3].reshape(-1, 3)
    ends = pvi < 0
    if not ends[-1]:
        ends = ends.copy()
        ends[-1] = True
    corners = np.where(pvi < 0, ~pvi, pvi)
    if (corners >= len(points)).any():
        raise FormatError(f"{name}: a polygon names a point the mesh doesn't have")
    corner_poly = np.concatenate([[0], np.cumsum(ends)[:-1]]).astype(np.int64)
    polygons = int(ends.sum())
    starts = np.flatnonzero(np.concatenate([[True], ends[:-1]]))
    first = starts[corner_poly]
    # corner j (not the first or last of its polygon) makes the triangle (first, j, j + 1)
    j = np.flatnonzero((np.arange(len(pvi)) > first) & ~ends)
    triangles = np.stack([first[j], j, j + 1], axis=1) if len(j) else np.zeros((0, 3), np.int64)
    triangle_poly = corner_poly[j] if len(j) else np.zeros(0, np.int64)

    def first_layer(kind: str) -> FbxNode | None:
        found = node.find_all(kind)
        found.sort(key=lambda e: e.values[0] if e.values and isinstance(e.values[0], int) else 0)
        return found[0] if found else None

    normals = _layer(first_layer("LayerElementNormal"), "Normals", "NormalsIndex", 3, corners, corner_poly, polygons)
    uv_elem = first_layer("LayerElementUV")
    uvs = _layer(uv_elem, "UV", "UVIndex", 2, corners, corner_poly, polygons)
    mat_elem = first_layer("LayerElementMaterial")
    poly_material = np.zeros(polygons, np.int64)
    if mat_elem is not None:
        mats = mat_elem.array("Materials", np.int64)
        mapping = str(mat_elem.value("MappingInformationType", "AllSame"))
        if mats is not None and len(mats):
            if mapping == "ByPolygon":
                poly_material[:min(polygons, len(mats))] = mats[:polygons]
            else:
                poly_material[:] = mats[0]
    return Geometry(points, corners, corner_poly, triangles, triangle_poly, polygons, normals, uvs,
                    np.maximum(poly_material, 0), len(node.find_all("LayerElementUV")))


# -- the scene --------------------------------------------------------------------------------------------------


@dataclass
class Cluster:
    bone: Obj
    indexes: np.ndarray  # control points
    weights: np.ndarray
    transform: np.ndarray  # the mesh node relative to the bone, at bind time
    link: np.ndarray  # the bone's place at bind time


@dataclass
class Curve:
    times: np.ndarray  # seconds
    values: np.ndarray


@dataclass
class Stack:
    name: str
    start: float  # seconds
    stop: float
    # node id -> property (Lcl Translation...) -> (default (3,), [curve or None per axis])
    channels: dict = field(default_factory=dict)


def _matrix(node: FbxNode | None, name: str) -> np.ndarray | None:
    if node is None:
        return None
    m = node.array(name)
    if m is None or len(m) != 16:
        return None
    return m.reshape(4, 4).T  # stored column by column


class Scene:
    def __init__(self, data: bytes, name: str = ""):
        self.name = name
        self.version, self.root = read(data, name)
        self.text = not data.startswith(b"Kaydara FBX Binary")
        objects = self.root.find("Objects")
        if objects is None:
            raise FormatError(f"{name} has no objects")
        self.objects: dict = {}
        settings_node = self.root.find("GlobalSettings") or objects.find("GlobalSettings")
        self.settings = properties(settings_node)
        named = self.version < 7000
        for n in objects.children:
            if not n.values or n.name == "GlobalSettings":
                continue
            if isinstance(n.values[0], (int, np.integer)) and not named:
                oid, label = int(n.values[0]), n.values[1] if len(n.values) > 1 else ""
                sub = n.values[2] if len(n.values) > 2 else ""
            else:
                oid, label, sub = str(n.values[0]), n.values[0], n.values[1] if len(n.values) > 1 else ""
            self.objects[oid] = Obj(oid, n.name, _split_name(label), str(sub), n, properties(n))
        self.children: dict = defaultdict(list)  # parent id -> [(child id, property)]
        self.parents: dict = defaultdict(list)  # child id -> [(parent id, property)]
        conns = self.root.find("Connections")
        for c in conns.children if conns is not None else []:
            if c.name not in ("C", "Connect") or len(c.values) < 3:
                continue
            child, parent = c.values[1], c.values[2]
            if named:
                child, parent = str(child), str(parent)
            prop = str(c.values[3]) if len(c.values) > 3 else ""
            self.children[parent].append((child, prop))
            self.parents[child].append((parent, prop))
        header = self.root.find("FBXHeaderExtension")
        creator = self.root.value("Creator") or (header.value("Creator") if header is not None else None)
        scene_info = header.find("SceneInfo") if header is not None else None
        app = properties(scene_info).get("Original|ApplicationName") if scene_info is not None else None
        self.creator = str(app[0]) if app else (str(creator) if creator else "")
        self.frame = export_frame(self.settings)
        mode = int((self.settings.get("TimeMode") or [0])[0] or 0)
        custom = float((self.settings.get("CustomFrameRate") or [0])[0] or 0)
        self.fps = _TIME_MODES.get(mode) or (custom if custom > 0 else 30.0)
        self._world: dict = {}

    # -- connections

    def linked(self, oid, kind: str | None = None, prop: str | None = None) -> list[Obj]:
        """The objects connected to ``oid`` (its children), in connection order."""
        out = []
        for child, p in self.children.get(oid, ()):
            o = self.objects.get(child)
            if o is not None and (kind is None or o.kind == kind) and (prop is None or p == prop):
                out.append(o)
        return out

    def linked_props(self, oid, kind: str | None = None) -> list[tuple[Obj, str]]:
        out = []
        for child, p in self.children.get(oid, ()):
            o = self.objects.get(child)
            if o is not None and (kind is None or o.kind == kind):
                out.append((o, p))
        return out

    def owners(self, oid, kind: str | None = None) -> list[tuple[Obj, str]]:
        """The objects ``oid`` is connected to (its parents), with the property it's connected to."""
        out = []
        for parent, p in self.parents.get(oid, ()):
            o = self.objects.get(parent)
            if o is not None and (kind is None or o.kind == kind):
                out.append((o, p))
        return out

    def parent_model(self, obj: Obj) -> Obj | None:
        for o, p in self.owners(obj.id, "Model"):
            if not p:
                return o
        return None

    def ancestors(self, obj: Obj) -> list[Obj]:
        out, seen = [], {obj.id}
        p = self.parent_model(obj)
        while p is not None and p.id not in seen and len(out) < 1024:
            out.append(p)
            seen.add(p.id)
            p = self.parent_model(p)
        return out

    def world(self, obj: Obj) -> np.ndarray:
        """A node's place in the file's space, as it stands (its own transform under its parents')."""
        known = self._world.get(obj.id)
        if known is not None:
            return known
        chain = [obj] + self.ancestors(obj)
        m = np.eye(4)
        for o in reversed(chain):
            m = m @ local_matrices(o)[0]
            self._world.setdefault(o.id, m)
        return m

    # -- what the importer reads

    def meshes(self) -> list[tuple[Obj, Obj]]:
        """(mesh node, geometry object) of every mesh the scene places (a geometry used twice is listed twice)."""
        out = []
        for o in self.objects.values():
            if o.kind != "Model":
                continue
            if o.node.find("Vertices") is not None:  # FBX 6: the geometry is in the node
                out.append((o, o))
                continue
            for g in self.linked(o.id, "Geometry"):
                if g.sub in ("Mesh", "") and g.node.find("Vertices") is not None:
                    out.append((o, g))
        return out

    def materials(self, model: Obj) -> list[Obj]:
        return self.linked(model.id, "Material")

    def textures(self, material: Obj) -> list[tuple[str, Obj]]:
        """(the material property it's connected to, texture) for each texture of a material."""
        out = []
        for o, p in self.linked_props(material.id):
            if o.kind == "Texture":
                out.append((p, o))
            elif o.kind == "LayeredTexture":
                out += [(p, t) for t in self.linked(o.id, "Texture")[:1]]
        return out

    def model_textures(self, model: Obj) -> list[Obj]:
        """Textures connected to a node itself (FBX 6 files connect them there, not to materials)."""
        return self.linked(model.id, "Texture")

    def video_content(self, texture: Obj) -> bytes | None:
        """A texture's picture when the file embeds it."""
        for v in self.linked(texture.id, "Video"):
            c = v.node.find("Content")
            if c is None or not c.values:
                continue
            if isinstance(c.values[0], (bytes, bytearray)) and c.values[0]:
                return bytes(c.values[0])
            if all(isinstance(x, str) for x in c.values):
                try:
                    data = base64.b64decode("".join(c.values))
                except ValueError:
                    continue
                if data:
                    return data
        return None

    def clusters(self, geometry: Obj) -> list[Cluster]:
        out = []
        for skin in self.linked(geometry.id, "Deformer"):
            if skin.sub != "Skin":
                continue
            for c in self.linked(skin.id, "Deformer"):
                if c.sub != "Cluster":
                    continue
                bones = self.linked(c.id, "Model")
                if not bones:
                    continue
                idx = c.node.array("Indexes", np.int64)
                w = c.node.array("Weights")
                link = _matrix(c.node, "TransformLink")
                transform = _matrix(c.node, "Transform")
                if link is None:
                    link = self.bind_pose().get(bones[0].id, self.world(bones[0]))
                if transform is None:
                    transform = np.linalg.inv(link) @ self.world(geometry)
                if idx is None or w is None:
                    idx, w = np.zeros(0, np.int64), np.zeros(0)
                n = min(len(idx), len(w))
                out.append(Cluster(bones[0], idx[:n], w[:n], transform, link))
        return out

    def bind_pose(self) -> dict:
        """Node id -> its place at bind time, from the scene's bind poses."""
        known = getattr(self, "_bind", None)
        if known is not None:
            return known
        out: dict = {}
        for o in self.objects.values():
            if o.kind != "Pose" or o.sub != "BindPose":
                continue
            for pn in o.node.find_all("PoseNode"):
                nid = pn.value("Node")
                m = _matrix(pn, "Matrix")
                if nid is None or m is None:
                    continue
                nid = str(nid) if self.version < 7000 else nid
                out.setdefault(nid, m)
        self._bind = out
        return out

    def stacks(self) -> list[Stack]:
        out = []
        for o in self.objects.values():
            if o.kind != "AnimationStack":
                continue
            layers = self.linked(o.id, "AnimationLayer")
            if not layers:
                continue
            stack = Stack(o.name or f"Take {len(out) + 1}", 0.0, 0.0)
            times = []
            for node in self.linked(layers[0].id, "AnimationCurveNode"):
                targets = [(m, p) for m, p in self.owners(node.id, "Model") if p in TRANSFORM_PROPS]
                if not targets:
                    continue
                default = np.array([float((node.props.get(f"d|{a}") or [0.0])[0]) for a in "XYZ"])
                curves: list = [None, None, None]
                for c, p in self.linked_props(node.id, "AnimationCurve"):
                    axis = p.rsplit("|", 1)[-1].upper()
                    if axis not in ("X", "Y", "Z"):
                        continue
                    kt = c.node.array("KeyTime", np.int64)
                    kv = c.node.array("KeyValueFloat")
                    if kv is None:
                        kv = c.node.array("KeyValue")
                    if kt is None or kv is None or not len(kt):
                        continue
                    n = min(len(kt), len(kv))
                    curves["XYZ".index(axis)] = Curve(kt[:n] / KTIME, kv[:n].astype(np.float64))
                    times += [kt[0] / KTIME, kt[n - 1] / KTIME]
                for model, prop in targets:
                    stack.channels.setdefault(model.id, {})[prop] = (default, curves)
            if not stack.channels:
                continue

            def ktime(name: str):
                v = o.props.get(name)
                try:
                    return int(v[0]) / KTIME if v else None
                except (TypeError, ValueError):
                    return None

            start = ktime("LocalStart")
            stop = ktime("LocalStop")
            if start is None or stop is None or stop <= start:
                start = min(times) if times else 0.0
                stop = max(times) if times else 0.0
            stack.start, stack.stop = start, stop
            out.append(stack)
        return out


def sample(stack: Stack, obj: Obj, times: np.ndarray) -> np.ndarray:
    """(n, 4, 4) local transforms of ``obj`` at ``times`` (seconds) in an animation."""
    own = stack.channels.get(obj.id)
    if not own:
        return np.broadcast_to(local_matrices(obj)[0], (len(times), 4, 4)).copy()
    values = {}
    for prop in TRANSFORM_PROPS:
        found = own.get(prop)
        if found is None:
            continue
        default, curves = found
        v = np.empty((len(times), 3))
        for k in range(3):
            c = curves[k]
            v[:, k] = np.interp(times, c.times, c.values) if c is not None else default[k]
        values[prop] = v
    return local_matrices(obj, values.get("Lcl Translation"), values.get("Lcl Rotation"), values.get("Lcl Scaling"))


__all__ = ["KTIME", "Cluster", "Geometry", "Obj", "Scene", "Stack", "euler", "export_frame", "geometric_matrix",
           "local_matrices", "properties", "read_geometry", "sample"]
