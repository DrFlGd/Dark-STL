"""Byte-built FBX files (binary FBX 7.4, as Autodesk's SDK, Blender and the game tools write them) for the tests.

``write_fbx`` writes a scene of nodes; ``skinned_quad_scene`` builds a small one with what the FBX importer reads:
a mesh with two materials (a texture each, named by file), normals and UVs, a two-bone skeleton with a skin
cluster per bone, a bind pose, and optionally an animation that turns the second bone.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"


class Node:
    def __init__(self, name: str, *props, children: list | None = None):
        self.name = name
        self.props = list(props)
        self.children = list(children or [])

    def add(self, *nodes: "Node") -> "Node":
        self.children += nodes
        return self


class Arr:
    """An array property: ``kind`` is d (float64), f (float32), i (int32), l (int64) or b (bool)."""

    def __init__(self, kind: str, values, compress: bool = False):
        self.kind = kind
        self.values = list(values)
        self.compress = compress


def _prop(p) -> bytes:
    if isinstance(p, bool):
        return b"C" + struct.pack("<?", p)
    if isinstance(p, int):
        return (b"I" + struct.pack("<i", p)) if -2**31 <= p < 2**31 else (b"L" + struct.pack("<q", p))
    if isinstance(p, float):
        return b"D" + struct.pack("<d", p)
    if isinstance(p, str):
        data = p.encode("utf-8")
        return b"S" + struct.pack("<I", len(data)) + data
    if isinstance(p, bytes):
        return b"R" + struct.pack("<I", len(p)) + p
    if isinstance(p, Arr):
        fmt = {"d": "d", "f": "f", "i": "i", "l": "q", "b": "?"}[p.kind]
        raw = struct.pack(f"<{len(p.values)}{fmt}", *p.values)
        enc = 0
        if p.compress:
            raw, enc = zlib.compress(raw), 1
        return p.kind.encode() + struct.pack("<III", len(p.values), enc, len(raw)) + raw
    if isinstance(p, tuple) and p and p[0] == "L":  # an explicit 64-bit id
        return b"L" + struct.pack("<q", p[1])
    raise TypeError(f"Can't write {p!r}")


def _node(n: Node, offset: int, wide: bool = False) -> bytes:
    """One node record; from version 7.5 (``wide``) its offsets and counts are 64-bit."""
    props = b"".join(_prop(p) for p in n.props)
    name = n.name.encode()
    fixed = 25 if wide else 13
    head = fixed + len(name)
    body = b""
    pos = offset + head + len(props)
    for c in n.children:
        rec = _node(c, pos, wide)
        body += rec
        pos += len(rec)
    if n.children or not n.props:  # a node with children, or with nothing at all, ends with an empty record
        body += b"\0" * fixed
    end = offset + head + len(props) + len(body)
    return struct.pack("<QQQB" if wide else "<IIIB", end, len(n.props), len(props), len(name)) + name + props + body


def write_fbx(nodes: list[Node], version: int = 7400) -> bytes:
    wide = version >= 7500
    out = bytearray(MAGIC + struct.pack("<I", version))
    for n in nodes:
        out += _node(n, len(out), wide)
    out += b"\0" * (25 if wide else 13)
    # the footer: a fixed id, padding to 16 bytes, the version and the magic tail
    out += bytes.fromhex("fad1b11f9ab55bd8f7b90d60f2fc6b62")
    out += b"\0" * ((16 - len(out) % 16) % 16 or 16)
    out += struct.pack("<I", version) + b"\0" * 120
    out += bytes.fromhex("f85a8c6adef5d97ec5ac9c4c3b6c2fa1")
    return bytes(out)


def _text_value(p) -> str:
    if isinstance(p, bool):
        return "T" if p else "F"
    if isinstance(p, (int, float)):
        return repr(p)
    if isinstance(p, str):
        if "\x00\x01" in p:  # Name\x00\x01Class is written Class::Name in text files
            name, _, cls = p.partition("\x00\x01")
            p = f"{cls}::{name}"
        return '"' + p.replace('"', "&quot;") + '"'
    if isinstance(p, tuple) and p and p[0] == "L":
        return str(p[1])
    raise TypeError(f"Can't write {p!r} in a text file")


def _text_node(n: Node, depth: int, out: list[str]) -> None:
    pad = "\t" * depth
    if len(n.props) == 1 and isinstance(n.props[0], Arr):
        a = n.props[0]
        values = ",".join(repr(float(v)) if a.kind in "df" else str(int(v)) for v in a.values)
        out += [f"{pad}{n.name}: *{len(a.values)} {{", f"{pad}\ta: {values}", f"{pad}}}"]
        return
    values = ", ".join(_text_value(v) for v in n.props)
    if n.children or not n.props:
        out.append(f"{pad}{n.name}: {values} {{")
        for c in n.children:
            _text_node(c, depth + 1, out)
        out.append(f"{pad}}}")
    else:
        out.append(f"{pad}{n.name}: {values}")


def write_fbx_text(nodes: list[Node], version: int = 7400) -> bytes:
    """The same scene as a text ("ASCII") FBX file."""
    out = [f"; FBX {version // 1000}.{version % 1000 // 100}.0 project file", "; Dark STL tests", ""]
    for n in nodes:
        _text_node(n, 0, out)
    return ("\n".join(out) + "\n").encode("utf-8")


def P(name: str, typ: str, label: str, flags: str, *values) -> Node:
    return Node("P", name, typ, label, flags, *values)


def _props70(*ps: Node) -> Node:
    return Node("Properties70", children=list(ps))


def _matrix(m: np.ndarray) -> Arr:
    return Arr("d", [float(x) for x in np.asarray(m, float).T.reshape(-1)])  # FBX stores matrices column by column


def _translation(x: float, y: float, z: float) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def skinned_quad_scene(texture_dir: str = "textures", unit_cm: float = 100.0, up_axis: int = 2,
                       animated: bool = False, with_skin: bool = True, text: bool = False,
                       embed: bytes | None = None, version: int = 7400, compress: bool = False) -> bytes:
    """A 2 x 1 strip of two quads (8 x 4 units) in two materials, each with a texture named by file; with a
    skeleton (Root at the origin, Tip at x 4) skinning the left quad to Root and the right one to Tip.
    ``unit_cm``: centimetres per unit (100: metres; 1: centimetres). ``up_axis``: 1 for Y up, 2 for Z up.
    ``text``: written as a text FBX file. ``embed``: the picture embedded in the file for the first texture.
    ``version``: the binary file's version (7500 and later: 64-bit offsets). ``compress``: arrays zlib-compressed."""
    # the strip lies in the plane of the two axes other than "up"
    if up_axis == 2:
        pts = [(0, 0, 0), (4, 0, 0), (8, 0, 0), (0, 4, 0), (4, 4, 0), (8, 4, 0)]
        normal = (0.0, 0.0, 1.0)
    else:
        pts = [(0, 0, 0), (4, 0, 0), (8, 0, 0), (0, 0, -4), (4, 0, -4), (8, 0, -4)]
        normal = (0.0, 1.0, 0.0)
    verts = [float(c) for p in pts for c in p]
    polys = [0, 1, 4, ~3, 1, 2, 5, ~4]
    uv = [0, 0, 0.5, 0, 1, 0, 0, 1, 0.5, 1, 1, 1]
    uv_index = [0, 1, 4, 3, 1, 2, 5, 4]
    normals = [c for _ in range(8) for c in normal]
    geom_id, mesh_id, mat_a, mat_b, tex_a, tex_b, vid_a, vid_b = 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008
    root_id, tip_id, root_attr, tip_attr, skin_id, cl_root, cl_tip, pose_id = (2001, 2002, 2003, 2004, 2005, 2006,
                                                                                2007, 2008)
    geometry = Node("Geometry", ("L", geom_id), "Strip\x00\x01Geometry", "Mesh").add(
        Node("Vertices", Arr("d", verts, compress)),
        Node("PolygonVertexIndex", Arr("i", polys, compress)),
        Node("GeometryVersion", 124),
        Node("LayerElementNormal", 0).add(Node("Version", 101), Node("Name", ""),
                                          Node("MappingInformationType", "ByPolygonVertex"),
                                          Node("ReferenceInformationType", "Direct"),
                                          Node("Normals", Arr("d", normals))),
        Node("LayerElementUV", 0).add(Node("Version", 101), Node("Name", "UVMap"),
                                      Node("MappingInformationType", "ByPolygonVertex"),
                                      Node("ReferenceInformationType", "IndexToDirect"),
                                      Node("UV", Arr("d", [float(x) for x in uv])),
                                      Node("UVIndex", Arr("i", uv_index))),
        Node("LayerElementMaterial", 0).add(Node("Version", 101), Node("Name", ""),
                                            Node("MappingInformationType", "ByPolygon"),
                                            Node("ReferenceInformationType", "IndexToDirect"),
                                            Node("Materials", Arr("i", [0, 1]))),
        Node("Layer", 0).add(Node("Version", 100),
                             Node("LayerElement").add(Node("Type", "LayerElementNormal"), Node("TypedIndex", 0)),
                             Node("LayerElement").add(Node("Type", "LayerElementUV"), Node("TypedIndex", 0)),
                             Node("LayerElement").add(Node("Type", "LayerElementMaterial"), Node("TypedIndex", 0))),
    )
    mesh = Node("Model", ("L", mesh_id), "Strip\x00\x01Model", "Mesh").add(
        Node("Version", 232), _props70(P("DefaultAttributeIndex", "int", "Integer", "", 0)))

    def material(mid: int, name: str) -> Node:
        return Node("Material", ("L", mid), f"{name}\x00\x01Material", "").add(
            Node("Version", 102), Node("ShadingModel", "phong"),
            _props70(P("DiffuseColor", "Color", "", "A", 0.8, 0.8, 0.8)))

    def texture(tid: int, vid: int, name: str, file: str) -> list[Node]:
        rel = f"{texture_dir}\\{file}" if texture_dir else file
        return [Node("Texture", ("L", tid), f"{name}\x00\x01Texture", "").add(
            Node("Type", "TextureVideoClip"), Node("Version", 202), Node("TextureName", f"{name}\x00\x01Texture"),
            Node("FileName", f"Z:\\nowhere\\{file}"), Node("RelativeFilename", rel)),
            Node("Video", ("L", vid), f"{name}\x00\x01Video", "Clip").add(
                Node("Type", "Clip"), Node("FileName", f"Z:\\nowhere\\{file}"), Node("RelativeFilename", rel),
                *([Node("Content", embed)] if embed is not None and tid == tex_a else []))]

    objects = Node("Objects").add(geometry, mesh, material(mat_a, "Skin"), material(mat_b, "Cloth"),
                                  *texture(tex_a, vid_a, "Skin_D", "Skin_D.png"),
                                  *texture(tex_b, vid_b, "Cloth_N", "Cloth_N.png"))
    conns = [("OO", mesh_id, 0), ("OO", geom_id, mesh_id), ("OO", mat_a, mesh_id), ("OO", mat_b, mesh_id),
             ("OP", tex_a, mat_a, "DiffuseColor"), ("OP", tex_b, mat_b, "NormalMap"), ("OO", vid_a, tex_a),
             ("OO", vid_b, tex_b)]
    if with_skin:
        tip_at = (4.0, 0.0, 0.0)

        def bone(bid: int, aid: int, name: str, t) -> list[Node]:
            return [Node("Model", ("L", bid), f"{name}\x00\x01Model", "LimbNode").add(
                Node("Version", 232), _props70(P("Lcl Translation", "Lcl Translation", "", "A", *map(float, t)))),
                Node("NodeAttribute", ("L", aid), f"{name}\x00\x01NodeAttribute", "LimbNode").add(
                    Node("TypeFlags", "Skeleton"))]

        world_root, world_tip = np.eye(4), _translation(*tip_at)

        def cluster(cid: int, name: str, indexes, link) -> Node:
            return Node("Deformer", ("L", cid), f"{name}\x00\x01SubDeformer", "Cluster").add(
                Node("Version", 100), Node("UserData", "", ""), Node("Indexes", Arr("i", indexes)),
                Node("Weights", Arr("d", [1.0] * len(indexes))), Node("Transform", _matrix(np.linalg.inv(link))),
                Node("TransformLink", _matrix(link)))

        objects.add(*bone(root_id, root_attr, "Root", (0, 0, 0)), *bone(tip_id, tip_attr, "Tip", tip_at),
                    Node("Deformer", ("L", skin_id), "Skin\x00\x01Deformer", "Skin").add(
                        Node("Version", 101), Node("Link_DeformAcuracy", 50.0)),
                    cluster(cl_root, "Root", [0, 3], world_root), cluster(cl_tip, "Tip", [1, 2, 4, 5], world_tip),
                    Node("Pose", ("L", pose_id), "BindPose\x00\x01Pose", "BindPose").add(
                        Node("Type", "BindPose"), Node("Version", 100), Node("NbPoseNodes", 3),
                        Node("PoseNode").add(Node("Node", ("L", mesh_id)), Node("Matrix", _matrix(np.eye(4)))),
                        Node("PoseNode").add(Node("Node", ("L", root_id)), Node("Matrix", _matrix(world_root))),
                        Node("PoseNode").add(Node("Node", ("L", tip_id)), Node("Matrix", _matrix(world_tip)))))
        conns += [("OO", root_id, 0), ("OO", tip_id, root_id), ("OO", root_attr, root_id), ("OO", tip_attr, tip_id),
                  ("OO", skin_id, geom_id), ("OO", cl_root, skin_id), ("OO", cl_tip, skin_id),
                  ("OO", root_id, cl_root), ("OO", tip_id, cl_tip)]
        if animated:  # one second at 30 fps: Tip turns 90 degrees about the up axis
            stack, layer, node, curve = 3001, 3002, 3003, 3004
            ktime = 46186158000  # FBX time units per second
            axis = "d|Z" if up_axis == 2 else "d|Y"
            objects.add(
                Node("AnimationStack", ("L", stack), "Wave\x00\x01AnimStack", "").add(
                    _props70(P("LocalStop", "KTime", "Time", "", ("L", ktime)),
                             P("ReferenceStop", "KTime", "Time", "", ("L", ktime)))),
                Node("AnimationLayer", ("L", layer), "Base\x00\x01AnimLayer", ""),
                Node("AnimationCurveNode", ("L", node), "R\x00\x01AnimCurveNode", "").add(
                    _props70(P("d|X", "Number", "", "A", 0.0), P("d|Y", "Number", "", "A", 0.0),
                             P("d|Z", "Number", "", "A", 0.0))),
                Node("AnimationCurve", ("L", curve), "\x00\x01AnimCurve", "").add(
                    Node("Default", 0.0), Node("KeyVer", 4009), Node("KeyTime", Arr("l", [0, ktime])),
                    Node("KeyValueFloat", Arr("f", [0.0, 90.0])), Node("KeyAttrFlags", Arr("i", [8456])),
                    Node("KeyAttrDataFloat", Arr("f", [0.0, 0.0, 0.0, 0.0])), Node("KeyAttrRefCount", Arr("i", [2]))))
            conns += [("OO", layer, stack), ("OO", node, layer), ("OP", node, tip_id, "Lcl Rotation"),
                      ("OP", curve, node, axis)]
    connections = Node("Connections", children=[
        Node("C", c[0], ("L", c[1]), ("L", c[2]), *c[3:]) for c in conns])
    settings = Node("GlobalSettings").add(Node("Version", 1000), _props70(
        P("UpAxis", "int", "Integer", "", up_axis), P("UpAxisSign", "int", "Integer", "", 1),
        P("FrontAxis", "int", "Integer", "", 1 if up_axis == 2 else 2), P("FrontAxisSign", "int", "Integer", "", -1 if up_axis == 2 else 1),
        P("CoordAxis", "int", "Integer", "", 0), P("CoordAxisSign", "int", "Integer", "", 1),
        P("UnitScaleFactor", "double", "Number", "", float(unit_cm)),
        P("TimeMode", "enum", "", "", 6), P("CustomFrameRate", "double", "Number", "", 30.0)))
    header = Node("FBXHeaderExtension").add(Node("FBXHeaderVersion", 1003), Node("FBXVersion", 7400),
                                            Node("Creator", "Dark STL tests"))
    nodes = [header, settings, Node("Documents"), Node("Definitions"), objects, connections]
    return write_fbx_text(nodes, version) if text else write_fbx(nodes, version)


def png(color: tuple[int, int, int, int], size: int = 8) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


__all__ = ["Arr", "Node", "P", "png", "skinned_quad_scene", "write_fbx", "write_fbx_text"]
