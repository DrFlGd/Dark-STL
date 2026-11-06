"""RE Engine ``.mdf2`` material files (magic ``MDF\\0``).

A model's materials live in a separate file beside it (``pl0000.mdf2.10``). Each material names
its shader (an ``.mmtr`` path), a list of texture bindings (a slot type such as ``BaseMetalMap``
or ``NRMR``, and the texture's path below ``natives/<platform>/``, without the version in the
extension) and a list of float properties. Only the names and texture bindings are read here.

The layout follows the version in the file's extension (10 for RE2, 19 for Village, 32 for
RE4...), which moves a few fields around in each material entry. The header carries a version
field of its own, but it is not that number (real files say 1 there), so callers pass the one from
the file name; the header's is only a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .binary import FormatError, Reader

MAGIC = 0x0046444D  # "MDF\0"


@dataclass
class TextureBinding:
    type: str  # slot type, e.g. "BaseMetalMap", "NormalRoughnessMap", "ALBD", "NRMR"
    path: str  # e.g. "Character/Player/pl0000/pl0000_body_ALBD.tex" (no version suffix)


@dataclass
class MdfMaterial:
    name: str
    shader: str  # the .mmtr path
    textures: list[TextureBinding] = field(default_factory=list)


@dataclass
class MdfFile:
    version: int
    materials: list[MdfMaterial]

    def by_name(self) -> dict[str, MdfMaterial]:
        return {m.name: m for m in self.materials}


def is_mdf(data: bytes) -> bool:
    return len(data) >= 8 and int.from_bytes(data[:4], "little") == MAGIC


def read_mdf(data: bytes, version: int | None = None) -> MdfFile:
    """The materials in an mdf2 file; ``version`` is the number in the file's extension."""
    r = Reader(data)
    if not is_mdf(data):
        raise FormatError("Not an RE Engine material file")
    r.u32()
    header_version = r.u16()
    if version is None:
        version = header_version
    count = r.u16()
    r.u64()
    materials = []
    for _ in range(count):
        name_offset = r.u64()
        r.u32()  # name hash
        r.i32()  # property block size
        _prop_count = r.i32()
        tex_count = r.i32()
        if version >= 19:
            r.unpack("2i")  # GPU buffer name/path counts
        if version >= 31:
            r.i32()  # bake texture array size
        r.i32()  # shader type
        r.i32()  # flags
        if version >= 31:
            r.unpack("2i")  # flags B, shader LOD count
        if version >= 51:
            r.u64()
        _prop_headers = r.u64()
        tex_headers = r.u64()
        if version >= 19:
            r.u64()  # GPU buffer offset
        r.u64()  # property data offset
        shader_offset = r.u64()
        if version >= 31:
            r.u64()  # mmtrs data offset
        saved = r.pos
        material = MdfMaterial(r.wstring(name_offset), r.wstring(shader_offset) if shader_offset else "")
        if tex_count < 0 or tex_count > 4096:
            raise FormatError("Material has an impossible number of textures")
        r.seek(tex_headers)
        for _ in range(tex_count):
            type_offset = r.u64()
            r.unpack("2I")
            path_offset = r.u64()
            if version >= 13:
                r.u64()
            material.textures.append(TextureBinding(r.wstring(type_offset), r.wstring(path_offset)))
        materials.append(material)
        r.seek(saved)
    return MdfFile(version, materials)


__all__ = ["MdfFile", "MdfMaterial", "TextureBinding", "is_mdf", "read_mdf"]
