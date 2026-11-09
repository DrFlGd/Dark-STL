"""Models attached to another one: a sword in a character's hand, a lantern on a belt, a shield on an arm.

An attachment names a model of the library (by its path in the game, as configurations do, so it is found as
the game's file, a collected copy or a model package) and where it goes on this one: a bone of this model's
skeleton, which it then follows through every pose and bone adjustment, or one of its sockets (the named
points a configurable model's parts fit to, which stay where the model's file puts them). An offset (in
millimetres, along the bone's own axes), a turn (degrees, X then Y then Z about those axes) and a scale
place it from there.

The model with its attachments is one :class:`darkstl.model.SourceModel` (:func:`attached`): its parts,
materials and textures followed by each attached model's, so the attached models show with their textures,
can be switched off part by part on the Model tab, and go through Clean, Mesh, Displace and Export with it.
Its mesh is built in the bind pose with each attached model weighted whole to its bone, then posed by the
skeleton like the rest of the model (:mod:`darkstl.posing`), so the attached model moves exactly as its
bone does. Where each one sits is ``MeshOptions.attach`` ({id: :class:`Place`}), so moving one is quick and
everything generated from the model depends on it; the overlay keeps the attachments and their places for
each model (see :mod:`darkstl.overlay`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from .errors import FormatError
from .geometry.mesh import TriMesh, concatenate
from .model import Material, MeshOptions, Part, SourceModel

MAX_ATTACHMENTS = 16
TEXTURE_PREFIX = "attached{}:"


@dataclass(frozen=True)
class Place:
    """Where an attached model sits: on a bone (or a socket), moved, turned and scaled from there."""

    bone: str | None = None  # a bone of the model's skeleton: the attached model follows it
    socket: str | None = None  # or a socket of the model (SourceModel.sockets), when no bone is named
    offset_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)  # along the bone's (or socket's) own axes
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0)  # degrees about those axes, X then Y then Z
    scale: float = 1.0

    def key(self) -> tuple:
        return (self.bone or "", self.socket or "", tuple(self.offset_mm), tuple(self.rotate), self.scale)


@dataclass
class Attachment:
    """One model attached to another, as the overlay keeps it."""

    id: str
    file: str  # the attached model's file: its path in the game (or an absolute path)
    variant: str | None = None  # the model inside that file, when it holds several
    label: str = ""
    place: Place = field(default_factory=Place)


def _three(raw, what: str) -> tuple[float, float, float]:
    try:
        values = [float(x) for x in (list(raw or []) + [0.0, 0.0, 0.0])[:3]]
    except (TypeError, ValueError):
        raise FormatError(f"An attachment's {what} is three numbers") from None
    if not all(math.isfinite(x) for x in values):
        raise FormatError(f"An attachment's {what} is three numbers")
    return tuple(round(x, 4) + 0.0 for x in values)


def parse_place(raw) -> Place:
    """A :class:`Place` from the page's or the overlay's JSON: {bone, socket, offset (mm), rotate (deg), scale}."""
    if isinstance(raw, Place):
        return raw
    if not isinstance(raw, dict):
        raise FormatError("An attachment's place is a bone or socket with an offset, a turn and a scale")
    try:
        scale = float(raw.get("scale", 1.0) if raw.get("scale") not in (None, "") else 1.0)
    except (TypeError, ValueError):
        raise FormatError("An attachment's scale is a number") from None
    if not math.isfinite(scale) or not 0.001 <= scale <= 1000:
        raise FormatError("An attachment's scale is between 0.001 and 1000")
    rotate = tuple(round(((a + 180.0) % 360.0) - 180.0, 4) + 0.0 for a in _three(raw.get("rotate"), "turn"))
    return Place(str(raw.get("bone") or "") or None, str(raw.get("socket") or "") or None,
                 _three(raw.get("offset_mm", raw.get("offset")), "offset"), rotate, round(scale, 6))


def place_json(p: Place) -> dict:
    return {"bone": p.bone, "socket": p.socket, "offset_mm": list(p.offset_mm), "rotate": list(p.rotate),
            "scale": p.scale}


def parse_attachment(raw) -> Attachment:
    if not isinstance(raw, dict) or not str(raw.get("id") or "") or not str(raw.get("file") or ""):
        raise FormatError("An attachment names its id and the model's file")
    return Attachment(str(raw["id"]), str(raw["file"]), str(raw.get("variant") or "") or None,
                      str(raw.get("label") or ""), parse_place(raw.get("place") or {}))


def attachment_json(a: Attachment) -> dict:
    return {"id": a.id, "file": a.file, "variant": a.variant, "label": a.label, "place": place_json(a.place)}


def parse_places(raw) -> dict[str, Place] | None:
    """``MeshOptions.attach`` from the page: {id: place}; None (or nothing sent) for the places kept."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, dict):
        raise FormatError("Attachments are sent as {id: place}")
    return {str(k): parse_place(v) for k, v in raw.items()}


def places_key(places: dict[str, Place] | None) -> tuple | None:
    return None if places is None else tuple(sorted((k, p.key()) for k, p in places.items()))


def place_matrix(place: Place, holder: np.ndarray | None) -> np.ndarray:
    """The 4x4 matrix (column vectors, metres) that puts an attached model in its place: at the holder's
    point, turned with its axes (scale and shear left out), then moved, turned and scaled as ``place`` says."""
    from .posing import orthonormal, turn_matrix

    base = np.eye(4)
    if holder is not None:
        holder = np.asarray(holder, np.float64).reshape(4, 4)
        r = orthonormal(holder[:3, :3])
        if np.linalg.det(r) < 0:  # a mirrored bone: keep the model the right way round
            r = r @ np.diag([1.0, 1.0, -1.0])
        base[:3, :3] = r
        base[:3, 3] = holder[:3, 3]
    local = np.eye(4)
    local[:3, :3] = turn_matrix(place.rotate) * float(place.scale or 1.0)
    local[:3, 3] = np.asarray(place.offset_mm, np.float64) / 1000.0
    return base @ local


@dataclass
class Held:
    """An attachment and the model it opens, in the model made with it: its first part and material there."""

    spec: Attachment
    model: SourceModel
    part_base: int
    n_parts: int
    material_base: int
    whole: bool  # the attached model has no parts list of its own: it is one part here


def attached(base: SourceModel, held: list[tuple[Attachment, SourceModel]], notes: list[str] | None = None) -> SourceModel:
    """``base`` with the models of ``held`` attached, as one model (see the module's description)."""
    if base.parts:
        parts = list(base.parts)
    else:  # a model without a parts list is one part here, before the attached ones
        whole = base.build_mesh(MeshOptions())
        parts = [Part(base.name, 0, whole.n_vertices, whole.n_faces)]
    base_count = len(parts)
    materials = list(base.materials)
    textures = dict(base.textures)
    notes = list(base.notes) + list(notes or [])
    info = list(base.info)
    holds: list[Held] = []
    for n, (spec, model) in enumerate(held):
        label = spec.label or model.name
        prefix = TEXTURE_PREFIX.format(n + 1)

        def ren(k, prefix=prefix):
            return None if k is None else prefix + k

        material_base = len(materials)
        for mat in model.materials or [Material(label)]:
            materials.append(replace(mat, name=f"{label} / {mat.name}", albedo=ren(mat.albedo), normal=ren(mat.normal),
                                     height=ren(mat.height), specular=ren(mat.specular),
                                     detail_normal=ren(mat.detail_normal)))
        for k, t in model.textures.items():
            textures[prefix + k] = t
        part_base = len(parts)
        if model.parts:
            for p in model.parts:
                parts.append(Part(f"{label} / {p.name}", p.material + material_base, p.vertices, p.triangles))
        else:
            mesh = model.build_mesh(MeshOptions())
            parts.append(Part(label, material_base, mesh.n_vertices, mesh.n_faces))
        holds.append(Held(spec, model, part_base, len(parts) - part_base, material_base, not model.parts))
        info.append(f"attached: {label} ({spec.file.replace(chr(92), '/').rsplit('/', 1)[-1]})")
        notes += [f"{label}: {x}" for x in model.notes]
    rig = base.rig
    bone_index = {n: i for i, n in enumerate(rig.names)} if rig is not None else {}
    for h in holds:
        p = h.spec.place
        if p.bone and p.bone not in bone_index:
            notes.append(f"{h.spec.label or h.model.name} goes on the bone {p.bone}, which this model's skeleton "
                         "doesn't have: it is placed at the model's origin.")
        elif not p.bone and p.socket and p.socket not in base.sockets:
            notes.append(f"{h.spec.label or h.model.name} goes on the socket {p.socket}, which this model doesn't "
                         "have: it is placed at the model's origin.")

    def holder_of(p: Place, world: np.ndarray | None) -> tuple[int, np.ndarray | None]:
        if p.bone and p.bone in bone_index and world is not None:
            i = bone_index[p.bone]
            return i, world[i]
        if not p.bone and p.socket and p.socket in base.sockets:
            return -1, np.asarray(base.sockets[p.socket], np.float64).reshape(4, 4)
        return -1, None

    def child_mesh(h: Held, options: MeshOptions, place: Place, world: np.ndarray | None) -> tuple[TriMesh, int] | None:
        if options.parts is None:
            sel = None
        else:
            local = sorted(i - h.part_base for i in options.parts if h.part_base <= i < h.part_base + h.n_parts)
            if not local:
                return None
            sel = None if h.whole or len(local) == h.n_parts else set(local)
        from .configs import transform_mesh

        mesh = h.model.build_mesh(MeshOptions(sel))
        if not mesh.n_faces:
            return None
        bone, holder = holder_of(place, world)
        mesh = transform_mesh(mesh, place_matrix(place, holder))
        group = (np.zeros_like(mesh.face_group) if h.whole else mesh.face_group) + h.part_base
        mesh = replace(mesh, face_material=mesh.face_material + h.material_base, face_group=group,
                       metadata={k: v for k, v in mesh.metadata.items() if k not in ("skin", "pose")})
        return mesh, bone

    def base_mesh(options: MeshOptions, posed: bool) -> TriMesh | None:
        """The model's own mesh (its parts chosen), in the bind pose or posed; None when none is chosen."""
        if options.parts is None:
            sel = None
        elif base.parts:
            sel = {i for i in options.parts if i < base_count}
            if not sel:
                return None
        elif 0 in options.parts:
            sel = None
        else:
            return None
        own = {o.key for o in base.options}
        flags = {k: v for k, v in options.flags.items() if k in own}
        if posed:
            return base.build_mesh(MeshOptions(sel, flags, options.pose, options.bones))
        return base.build_mesh(MeshOptions(sel, flags))

    def build(options: MeshOptions) -> TriMesh:
        places = {h.spec.id: h.spec.place for h in holds}
        if options.attach:
            places.update({k: v for k, v in options.attach.items() if k in places})
        bind = base_mesh(options, False) if rig is not None else None
        if rig is None or (bind is not None and bind.n_faces and bind.metadata.get("skin") is None):
            # no skeleton to hold them: the model as its importer poses it, the attached models placed once
            mesh = base_mesh(options, True)
            built = [x for h in holds if (x := child_mesh(h, options, places[h.spec.id], None)) is not None]
            return _joined(([mesh] if mesh is not None else []) + [m for m, _ in built], None)
        meshes, skins = [], []
        k = 1
        if bind is not None and bind.n_faces:
            joints, weights = (np.asarray(a) for a in bind.metadata["skin"])
            joints, weights = joints.reshape(len(joints), -1), weights.reshape(len(weights), -1)
            k = joints.shape[1]
            meshes.append(bind)
            skins.append((joints, weights))
        for h in holds:
            made = child_mesh(h, options, places[h.spec.id], rig.bind_world)
            if made is None:
                continue
            mesh, bone = made
            j = np.full((mesh.n_vertices, k), -1, np.int32)
            w = np.zeros((mesh.n_vertices, k), np.float32)
            if bone >= 0:  # weighted whole to its bone: it moves as the bone does
                j[:, 0], w[:, 0] = bone, 1.0
            meshes.append(mesh)
            skins.append((j, w))
        out = _joined(meshes, skins)
        if not out.n_faces or (options.pose is None and not options.bones):
            return out
        from .posing import bone_world, pose_mesh

        world = bone_world(rig, options.pose, options.bones)
        skin = out.metadata["skin"]
        posed = pose_mesh(out, skin[0], skin[1], world @ np.linalg.pinv(rig.bind_world))
        posed.metadata["skin"] = skin
        posed.metadata["pose"] = world
        return posed

    return SourceModel(
        name=base.name, path=base.path, importer=base.importer, build=build, variant=base.variant,
        variants=list(base.variants), game=base.game, parts=parts, materials=materials, textures=textures,
        options=list(base.options), info=info, notes=notes, animations=base.animations, pose_note=base.pose_note,
        sockets=dict(base.sockets), configuration=base.configuration, rig=rig, adjusts_bones=True,
        attachments=[{**attachment_json(h.spec), "label": h.spec.label or h.model.name, "parts": [h.part_base, h.n_parts]}
                     for h in holds],
    )


def _joined(meshes: list[TriMesh], skins: list[tuple[np.ndarray, np.ndarray]] | None) -> TriMesh:
    keep = [i for i, m in enumerate(meshes) if m.n_faces]
    if not keep:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros((0, 3, 2), np.float32))
    chosen = [replace(meshes[i], metadata={k: v for k, v in meshes[i].metadata.items() if k not in ("skin", "pose")})
              for i in keep]
    if len(chosen) == 1 and skins is None:
        return chosen[0]
    out = concatenate(chosen)
    if out.corner_uvs is None and any(m.corner_uvs is not None for m in chosen):
        out.corner_uvs = np.concatenate([m.corner_uvs if m.corner_uvs is not None else np.zeros((m.n_faces, 3, 2), np.float32)
                                         for m in chosen])
    if out.corner_normals is None and any(m.corner_normals is not None for m in chosen):
        out.corner_normals = np.concatenate([m.corner_normals if m.corner_normals is not None
                                             else np.zeros((m.n_faces, 3, 3), np.float32) for m in chosen])
    if skins is not None:
        picked = [skins[i] for i in keep]
        out.metadata["skin"] = (np.concatenate([j for j, _ in picked]).astype(np.int32),
                                np.concatenate([w for _, w in picked]).astype(np.float32))
    return out


__all__ = ["Attachment", "Held", "MAX_ATTACHMENTS", "Place", "attached", "attachment_json", "parse_attachment",
           "parse_place", "parse_places", "place_json", "place_matrix", "places_key"]
