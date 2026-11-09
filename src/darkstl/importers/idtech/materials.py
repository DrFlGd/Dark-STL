"""Materials of id Tech models: ``material2`` decls (with their inherited parents) -> texture roles.

A model names a material decl; its ``RenderLayers`` and ``Parms`` give each texture parm a ``filePath``. The roles
follow the parm names. The image a path names is one of the resources made from that picture (their names add
``$`` options), the one whose ``$mtlkind=`` matches the role when there are several.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ...errors import FormatError
from .formats import decl
from .formats import image as imgfmt
from .store import Store

MATERIAL2 = "generated/decls/material2/"
ROLES = {
    "albedo": ("albedo", "decalalbedo", "eyealbedomap", "glassalbedomap", "hairalbedomap", "albedomap",
               "diffuse", "diffusemap"),
    "normal": ("normal", "decalnormal", "eyenormalmap", "glassnormalmap", "hairnormalmap", "normalmap", "bumpmap"),
    "specular": ("specular", "decalspecular", "eyespecularmap", "glassspecularmap", "hairspecularmap",
                 "specularmap"),
    "smoothness": ("smoothness", "glasssmoothnessmap", "hairsmoothnessmap", "smoothnessmap"),
}
KIND_OF_ROLE = {"albedo": ("albedo", "decalalbedo"), "normal": ("normal", "decalnormal"),
                "specular": ("specular", "decalspecular"), "smoothness": ("smoothness",)}


def decl_name(material: str) -> str:
    m = material.replace("\\", "/").strip("/")
    if m.startswith("generated/decls/"):
        return m if m.endswith(".decl") else m + ".decl"
    return MATERIAL2 + m + ".decl"


@dataclass
class MaterialInfo:
    name: str
    roles: dict[str, str] = field(default_factory=dict)  # role -> image resource name
    chain: list[str] = field(default_factory=list)  # the decls read, the material's own first
    slots: list[tuple[str, str]] = field(default_factory=list)  # (parm, file path) for display
    missing: list[str] = field(default_factory=list)


_decl_cache: dict[tuple[int, str], dict | None] = {}
_lock = threading.Lock()


def read_decl(store: Store, name: str, chain: list[str] | None = None, depth: int = 0) -> dict | None:
    """A decl with what it inherits merged in (None when the game doesn't have it)."""
    key = (id(store), name)
    with _lock:
        if key in _decl_cache:
            got = _decl_cache[key]
            if chain is not None and got is not None:
                chain.extend(got.get("__chain__", [name]))
            return got
    if not store.has("rs_streamfile", name) or depth > 16:
        return None
    try:
        text = store.read("rs_streamfile", name).decode("utf-8", "replace")
        d = decl.parse(text)
    except (FormatError, OSError):
        return None
    sub: list[str] = [name]
    parent = d.get("inherit")
    if isinstance(parent, str) and parent:
        base = parent if parent.startswith("generated/decls/") else _sibling_decl(name, parent)
        p = read_decl(store, base, sub, depth + 1)
        if p is not None:
            d = decl.merge({k: v for k, v in p.items() if k != "__chain__"}, d)
    d["__chain__"] = sub
    with _lock:
        _decl_cache[key] = d
    if chain is not None:
        chain.extend(sub)
    return d


def _sibling_decl(name: str, parent: str) -> str:
    """An inherited decl's resource: in the same decl folder (``material2``, ``entitydef``...)."""
    folder = "/".join(name.split("/")[:3])  # generated/decls/<kind>
    return f"{folder}/{parent.lower()}.decl"


def _tex_parms(d: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    layers = decl.get(d, "edit", "RenderLayers")
    sources = []
    if isinstance(layers, dict):
        first = layers.get("item[0]") or next((v for v in layers.values() if isinstance(v, dict)), None)
        sources.append(decl.get(first, "parms"))
    sources.append(decl.get(d, "edit", "Parms"))
    for parms in sources:
        if not isinstance(parms, dict):
            continue
        for k, v in parms.items():
            path = decl.get(v, "filePath") if isinstance(v, dict) else (v if isinstance(v, str) else None)
            if isinstance(path, str) and path and not path.startswith("_"):
                out[k.lower()] = path
    return out


def pick_image(store: Store, path: str, role: str) -> str | None:
    base = path.replace("\\", "/").lower()
    names = store.image_names(base)
    if not names and "." not in PurePosixPath(base).name:
        names = store.image_names(base + ".tga")
    if not names:
        return None
    kinds = KIND_OF_ROLE.get(role, ())
    for n in names:
        if any(f"$mtlkind={k}" in n.lower() for k in kinds):
            return n
    return min(names, key=len)


def resolve(store: Store, material: str) -> MaterialInfo:
    info = MaterialInfo(material)
    name = decl_name(material)
    d = read_decl(store, name, info.chain)
    if d is None:
        info.missing.append(f"material decl {name}")
        return info
    parms = _tex_parms(d)
    info.slots = sorted(parms.items())
    for role, keys in ROLES.items():
        for k in keys:
            if k in parms:
                found = pick_image(store, parms[k], role)
                if found:
                    info.roles[role] = found
                    break
                info.missing.append(parms[k])
    return info


class ImageRef:
    """An image resource, read (with its streamed mips) on first use."""

    def __init__(self, store: Store, name: str):
        self.store, self.name = store, name
        self._img: imgfmt.Image | None = None
        self._lock = threading.Lock()

    def image(self) -> imgfmt.Image:
        with self._lock:
            if self._img is None:
                loc = self.store.loc("image", self.name)
                darkages = self.store.profile.darkages
                holder: dict = {}

                def fetch(i: int, size: int):
                    return self.store.stream(imgfmt.mip_stream_id(holder["img"], i, loc.hash, darkages), size)

                data = self.store.read("image", self.name)
                head, _ = imgfmt.read_header(data)
                holder["img"] = head
                self._img = imgfmt.read_image(data, fetch)
            return self._img

    def streamed_pieces(self) -> list[tuple[int, int]]:
        """The StreamDB pieces (id, size) the image's mips are kept in."""
        data = self.store.read("image", self.name)
        img, _ = imgfmt.read_header(data)
        loc = self.store.loc("image", self.name)
        darkages = self.store.profile.darkages
        if not img.streamed_mips:
            return []
        if img.single_stream:
            last = img.mips[-1]
            return [(imgfmt.mip_stream_id(img, -1, loc.hash, darkages), last.cumulative + last.size)]
        return [(imgfmt.mip_stream_id(img, i, loc.hash, darkages), img.mips[i].size) for i in range(img.streamed_mips)]

    def decode(self):
        return self.image().decode()

    def describe(self) -> str:
        return self.image().describe()

    def dds(self) -> bytes | None:
        return self.image().to_dds()
