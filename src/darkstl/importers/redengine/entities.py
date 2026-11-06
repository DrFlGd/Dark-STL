"""Appearances (``.app``) and entity templates (``.ent``): which meshes an item is drawn with.

A weapon's ``.app`` lists its appearances (the ``frank``, ``military1``,
``neon2``... the game picks from); each one's components are packed in its
``compiledData`` (see :mod:`.formats.package`). The mesh components name a
mesh (by path hash), the appearance of that mesh to draw, and where it goes:
a local transform, and often a binding to a slot of an ``entSlotComponent``
or to a bone of the rig. The ``.ent`` the appearance's ``baseEntity`` names
can hold slot components too (attachment points for scopes and muzzles).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import numpy as np

from .formats.cr2w import BufferRef, Cr2wFile, Handle, Red, Ref
from .formats.package import HashRef, Package, read_package

MESH_COMPONENTS = ("entMeshComponent", "entSkinnedMeshComponent", "entPhysicalMeshComponent",
                   "entPhysicalSkinnedMeshComponent", "entGarmentSkinnedMeshComponent",
                   "entMorphTargetSkinnedMeshComponent", "entAppearanceProxyMeshComponent")
FIXED_POINT = 131072.0


@dataclass
class MeshComponent:
    name: str
    mesh: str | int  # depot path, or its hash when the package keeps hashes
    appearance: str = ""
    transform: np.ndarray = field(default_factory=lambda: np.eye(4))  # local transform, metres
    bind_name: str | None = None  # the component (or bone) it is placed relative to
    slot_name: str | None = None  # the slot of that component
    chunk_mask: int = -1
    cls: str = ""

    @property
    def mesh_hash(self) -> int:
        from .formats.archive import path_hash

        return self.mesh if isinstance(self.mesh, int) else path_hash(self.mesh)

    @property
    def mesh_stem(self) -> str:
        if isinstance(self.mesh, int):
            return f"{self.mesh:016x}"
        return self.mesh.replace("/", "\\").rsplit("\\", 1)[-1].rsplit(".", 1)[0].lower()


@dataclass
class SlotDef:
    name: str
    bone: str = ""
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)  # i, j, k, r
    component: str = ""  # the slot component holding it

    def matrix(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = quaternion_matrix(self.rotation)
        m[:3, 3] = self.position
        return m


@dataclass
class AppearanceDef:
    name: str
    meshes: list[MeshComponent] = field(default_factory=list)
    slots: list[SlotDef] = field(default_factory=list)
    parts: list[str | int] = field(default_factory=list)  # partsValues: entity templates added to it
    tags: list[str] = field(default_factory=list)


@dataclass
class AppResource:
    appearances: list[AppearanceDef]
    base_entity: str | None = None  # depot path of the .ent
    partial: bool = False  # some appearance couldn't be read


def quaternion_matrix(q: tuple[float, float, float, float]) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
        [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)],
    ])


def world_transform(t: Red | None) -> np.ndarray:
    m = np.eye(4)
    if not isinstance(t, Red):
        return m
    pos = t.get("Position")
    if isinstance(pos, Red):
        for i, axis in enumerate(("x", "y", "z")):
            v = pos.get(axis)
            if isinstance(v, Red):
                m[i, 3] = float(v.get("Bits", 0)) / FIXED_POINT
            elif isinstance(v, (int, float)):
                m[i, 3] = float(v)
    rot = t.get("Orientation")
    if isinstance(rot, Red):
        m[:3, :3] = quaternion_matrix(rot.vec("i", "j", "k") + (float(rot.get("r", 1.0)),))
    return m


def _ref_value(v) -> str | int | None:
    if isinstance(v, HashRef):
        return v.hash
    if isinstance(v, Ref) and v.path:
        return v.path
    return None


def components_of(pkg: Package) -> tuple[list[MeshComponent], list[SlotDef]]:
    meshes: list[MeshComponent] = []
    slots: list[SlotDef] = []
    for c in pkg.chunks:
        name = str(c.get("name", "") or "")
        if c.cls in MESH_COMPONENTS:
            mesh = _ref_value(c.get("mesh"))
            if mesh is None:
                continue
            comp = MeshComponent(name, mesh, str(c.get("meshAppearance", "") or ""), world_transform(c.get("localTransform")),
                                 cls=c.cls)
            binding = pkg.chunk(c.get("parentTransform")) if isinstance(c.get("parentTransform"), Handle) else None
            if isinstance(binding, Red):
                comp.bind_name = str(binding.get("bindName", "") or "") or None
                comp.slot_name = str(binding.get("slotName", "") or "") or None
            mask = c.get("chunkMask")
            if isinstance(mask, int):
                comp.chunk_mask = mask
            meshes.append(comp)
        elif c.cls == "entSlotComponent":
            for s in c.get("slots") or []:
                if not isinstance(s, Red):
                    continue
                rot = s.get("relativeRotation")
                pos = s.get("relativePosition")
                slots.append(SlotDef(str(s.get("slotName", "") or ""), str(s.get("boneName", "") or ""),
                                     pos.vec("X", "Y", "Z") if isinstance(pos, Red) else (0.0, 0.0, 0.0),
                                     (rot.vec("i", "j", "k") + (float(rot.get("r", 1.0)),)) if isinstance(rot, Red)
                                     else (0.0, 0.0, 0.0, 1.0), name))
    return meshes, slots


def _package_bytes(f: Cr2wFile, value) -> bytes:
    if isinstance(value, (BufferRef, bytes)):
        return f.buffer(value)
    return b""


def read_app(f: Cr2wFile) -> AppResource:
    root = f.root
    if root.cls != "appearanceAppearanceResource":
        raise ValueError(f"Not an appearance resource: the file holds a {root.cls}")
    base = root.get("baseEntity")
    out = AppResource([], base.path if isinstance(base, Ref) and base.path else None)
    for h in root.get("appearances") or []:
        a = f.chunk(h) if isinstance(h, Handle) else None
        if not isinstance(a, Red):
            continue
        app = AppearanceDef(str(a.get("name", "") or ""))
        for p in a.get("partsValues") or []:
            r = _ref_value(p.get("resource")) if isinstance(p, Red) else None
            if r is not None:
                app.parts.append(r)
        tags = a.get("visualTags")
        if isinstance(tags, Red):
            app.tags = [str(t) for t in (tags.get("tags") or [])]
        data = _package_bytes(f, a.get("compiledData"))
        if data:
            try:
                app.meshes, app.slots = components_of(read_package(data, imports_as_hash=True))
            except Exception:  # noqa: BLE001 - one unreadable appearance should not lose the others
                out.partial = True
        out.appearances.append(app)
    return out


def read_entity(f: Cr2wFile) -> tuple[list[MeshComponent], list[SlotDef], list[tuple[str, str, str]]]:
    """A template's packed components and its appearance list as (name, appearance resource, appearance name)."""
    root = f.root
    if root.cls != "entEntityTemplate":
        raise ValueError(f"Not an entity template: the file holds a {root.cls}")
    meshes: list[MeshComponent] = []
    slots: list[SlotDef] = []
    data = _package_bytes(f, root.get("compiledData"))
    if data:
        with contextlib.suppress(Exception):  # an unreadable package leaves the appearance list to be read
            meshes, slots = components_of(read_package(data))
    apps = []
    for a in root.get("appearances") or []:
        if isinstance(a, Red):
            res = a.get("appearanceResource")
            apps.append((str(a.get("name", "") or ""), res.path if isinstance(res, Ref) else "",
                         str(a.get("appearanceName", "") or "")))
    return meshes, slots, apps


__all__ = ["AppResource", "AppearanceDef", "MeshComponent", "SlotDef", "components_of", "quaternion_matrix",
           "read_app", "read_entity", "world_transform"]
