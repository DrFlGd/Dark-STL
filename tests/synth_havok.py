"""Small Havok files for the tests: a skeleton, a spline-compressed animation and its binding, written
as a 2014 or 2010 packfile or as a 2015 tagfile, plus an encoder for spline-compressed track data.

The writers only know what the readers need and lay objects out the way the games do (objects
16-byte aligned, arrays and strings after their owner, pointers patched by fixups or item indices).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import numpy as np

from darkstl.importers.fromsoft.formats import havok

# ---------------------------------------------------------------------------
# What a test file holds


@dataclass
class HkBone:
    name: str
    parent: int = -1
    translation: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (0.0, 0.0, 0.0, 1.0)  # quaternion xyzw
    scale: tuple = (1.0, 1.0, 1.0)

    def qs(self) -> list[float]:
        return [*self.translation, 0.0, *self.rotation, *self.scale, 1.0]


@dataclass
class HkAnimation:
    """A spline-compressed animation as its hkaSplineCompressedAnimation members."""

    frames: int
    duration: float
    tracks: int
    data: bytes
    block_offsets: list[int]
    mask_size: int
    max_frames_per_block: int = 256
    track_to_bone: list[int] = field(default_factory=list)
    name: str = "a000_000000"

    @property
    def frame_duration(self) -> float:
        return self.duration / max(self.frames - 1, 1)


@dataclass
class HkScene:
    bones: list[HkBone] = field(default_factory=list)
    skeleton_name: str = "master"
    animation: HkAnimation | None = None


# ---------------------------------------------------------------------------
# Spline track encoding

SQRT2 = math.sqrt(2.0)


def encode_three_comp40(q: np.ndarray) -> bytes:
    """One quaternion xyzw -> 5 bytes: three 12-bit components, the largest left implicit."""
    q = np.asarray(q, np.float64)
    q = q / np.linalg.norm(q)
    missing = int(np.argmax(np.abs(q)))
    negative = q[missing] < 0
    kept = [q[i] for i in range(4) if i != missing]
    start, step = -SQRT2 / 2, SQRT2 / 4094
    v = 0
    for k, c in enumerate(kept):
        v |= (min(max(int(round((c - start) / step)), 0), 4095)) << (12 * k)
    v |= missing << 36
    v |= int(negative) << 38
    return v.to_bytes(5, "little")


def encode_three_comp48(q: np.ndarray) -> bytes:
    """One quaternion xyzw -> three int16: 15-bit components, the implicit one's index in the top bits."""
    q = np.asarray(q, np.float64)
    q = q / np.linalg.norm(q)
    missing = int(np.argmax(np.abs(q)))
    negative = q[missing] < 0
    kept = [q[i] for i in range(4) if i != missing]
    vals = [min(max(int(round(c / 0.000043161)) + 0x3FFF, 0), 0x7FFF) for c in kept]
    vals[0] |= (missing & 1) << 15
    vals[1] |= ((missing >> 1) & 1) << 15
    vals[2] |= int(negative) << 15
    return struct.pack("<3H", *vals)


def _knots(count: int, degree: int) -> list[int]:
    """Knots that make a degree-1 or -3 B-spline pass through control point i at parameter i."""
    if degree == 1:
        return [0] + list(range(count)) + [count - 1]
    raise ValueError("only degree 1 is encoded")


def encode_spline_data(poses: np.ndarray, rotation_format: int = 1, static_translation: bool = False,
                       max_frames_per_block: int = 256, scalar_bits: int = 16) -> tuple[bytes, list[int], int]:
    """Encode (frames, tracks, 12) local transforms as spline-compressed blocks.

    Translations are linear splines (or static when every frame agrees / ``static_translation``),
    rotations linear splines of quantized quaternions (``rotation_format`` 1 ThreeComp40,
    2 ThreeComp48, 5 uncompressed), scales static. Returns (data, block offsets, mask size).
    """
    poses = np.asarray(poses, np.float64)
    frames, tracks, _ = poses.shape
    fpb = max_frames_per_block - 1
    n_blocks = max(1, -(-(frames - 1) // fpb))
    out = bytearray()
    offsets = []
    quant = (scalar_bits == 16) | (rotation_format << 2) | (1 << 6)
    rot_size = {1: 5, 2: 6, 5: 16}[rotation_format]
    rot_align = {1: 1, 2: 2, 5: 4}[rotation_format]

    def align(n: int) -> None:
        while len(out) % n:
            out.append(0)

    for b in range(n_blocks):
        offsets.append(len(out))
        lo = b * fpb
        hi = min(frames, lo + fpb + 1)
        block = poses[lo:hi]
        n = len(block)
        headers = bytearray()
        bodies = []
        for t in range(tracks):
            tr = block[:, t, 0:3]
            rot = block[:, t, 4:8]
            body = bytearray()
            # translation
            const = static_translation or np.allclose(tr, tr[0])
            if const:
                pos_flags = 0x07 if np.any(tr[0] != 0) else 0
                if pos_flags:
                    body += struct.pack("<3f", *tr[0])
            else:
                pos_flags = 0x70
                body += struct.pack("<hB", n - 1, 1) + bytes(_knots(n, 1))
                while len(body) % 4:
                    body.append(0)
                mins, maxs = tr.min(axis=0), tr.max(axis=0)
                maxs = np.where(maxs > mins, maxs, mins + 1.0)
                for a in range(3):
                    body += struct.pack("<2f", mins[a], maxs[a])
                for f in range(n):
                    for a in range(3):
                        r = (tr[f, a] - mins[a]) / (maxs[a] - mins[a])
                        body += struct.pack("<H", int(round(r * 65535))) if scalar_bits == 16 else bytes([int(round(r * 255))])
            while len(body) % 4:
                body.append(0)
            # rotation
            const_rot = np.allclose(rot, rot[0])
            if const_rot and np.allclose(rot[0], [0, 0, 0, 1]):
                rot_flags = 0
            elif const_rot:
                rot_flags = 0x0F
                body += _quat(rot[0], rotation_format)
            else:
                rot_flags = 0xF0
                body += struct.pack("<hB", n - 1, 1) + bytes(_knots(n, 1))
                while len(body) % rot_align:
                    body.append(0)
                for f in range(n):
                    body += _quat(rot[f], rotation_format)
            while len(body) % 4:
                body.append(0)
            # scale: static 1 (default)
            headers += bytes([quant, pos_flags, rot_flags, 0])
            bodies.append(bytes(body))
        out += headers
        align(4)
        for body in bodies:
            out += body
            align(4)
        align(16)
    return bytes(out), offsets, tracks * 4


def _quat(q, fmt: int) -> bytes:
    if fmt == 1:
        return encode_three_comp40(q)
    if fmt == 2:
        return encode_three_comp48(q)
    return struct.pack("<4f", *q)


def spline_animation(poses: np.ndarray, duration: float, track_to_bone: list[int] | None = None,
                     rotation_format: int = 1, max_frames_per_block: int = 256, **kw) -> HkAnimation:
    data, offsets, mask = encode_spline_data(poses, rotation_format, max_frames_per_block=max_frames_per_block, **kw)
    frames, tracks = np.asarray(poses).shape[:2]
    return HkAnimation(frames, duration, tracks, data, offsets, mask, max_frames_per_block,
                       list(track_to_bone if track_to_bone is not None else range(tracks)))


# ---------------------------------------------------------------------------
# Packfile writer


class _Packfile:
    def __init__(self, pointer_size: int, version: str):
        self.ptr = pointer_size
        self.version = version
        self.year = 2010 if "2010" in version else 2014
        self.layouts = havok._LayoutTable(self.ptr, self.year)
        self.data = bytearray()
        self.local: list[tuple[int, int]] = []
        self.glob: list[tuple[int, int]] = []
        self.virt: list[tuple[int, int]] = []
        self.names = bytearray()
        self.name_offsets: dict[str, int] = {}

    def classname(self, name: str) -> int:
        if name not in self.name_offsets:
            self.names += struct.pack("<I", 0) + b"\x09"
            self.name_offsets[name] = len(self.names)
            self.names += name.encode() + b"\0"
        return self.name_offsets[name]

    def _reserve(self, size: int, align: int = 16) -> int:
        while len(self.data) % align:
            self.data.append(0)
        at = len(self.data)
        self.data += bytes(size)
        return at

    def alloc(self, class_name: str) -> int:
        """Room for an object, filled later (the root must come first in the file)."""
        lay = self.layouts.get(class_name)
        at = self._reserve(lay.size)
        self.virt.append((at, self.classname(class_name)))
        return at

    def obj(self, class_name: str, values: dict, at: int | None = None) -> int:
        if at is None:
            at = self.alloc(class_name)
        self._fill(self.layouts.get(class_name), at, values)
        return at

    def _fill(self, lay: havok.Layout, at: int, values: dict) -> None:
        for name, mtype, off in lay.members:
            if name == "vtable" or name not in values:
                continue
            self._member(mtype, at + off, values[name])

    def _member(self, mtype: str, at: int, value) -> None:
        d = self.data
        if mtype in havok._PRIMITIVES:
            struct.pack_into("<" + havok._PRIMITIVES[mtype][2], d, at, value)
        elif mtype == "vec4":
            struct.pack_into("<4f", d, at, *value)
        elif mtype == "qs":
            struct.pack_into("<12f", d, at, *value)
        elif mtype == "str":
            if value:
                dst = self._reserve(len(value.encode()) + 1)
                d[dst:dst + len(value.encode())] = value.encode()
                self.local.append((at, dst))
        elif mtype.startswith("ptr:"):
            if value is not None:
                self.glob.append((at, value))
        elif mtype.startswith("class:"):
            self._fill(self.layouts.get(mtype[6:]), at, value)
        elif mtype.startswith("array:"):
            etype = mtype[6:]
            n = len(value)
            struct.pack_into("<iI", d, at + self.ptr, n, n | 0x80000000)
            if n:
                size, align = self.layouts.size_align(etype) if etype else (1, 1)
                dst = self._reserve(size * n, 16)
                self.local.append((at, dst))
                for i, v in enumerate(value):
                    self._member(etype, dst + i * size, v)
        else:
            raise ValueError(mtype)

    def build(self, root: int) -> bytes:
        little = True
        file_version = 11 if self.year >= 2014 else 8
        header = bytearray(havok.PACKFILE_MAGIC)
        header += struct.pack("<iiBBBBiiiii", 0, file_version, self.ptr, int(little), 1, 1, 3, 2, 0, 0, 0)
        header += self.version.encode().ljust(16, b"\0")
        header += struct.pack("<i", 0)
        header += struct.pack("<hh", 0, 0x10) if file_version >= 11 else bytes(4)
        assert len(header) == 0x40
        if file_version >= 11:
            header += b"\xff" * 0x10  # the predicate array's padding
        section_header = 0x40 if file_version >= 11 else 0x30
        sections_at = 0x40 + (0x10 if file_version >= 11 else 0)
        body_at = sections_at + 3 * section_header

        def fixups(entries, width):
            out = bytearray()
            for e in entries:
                out += struct.pack("<" + "I" * width, *e)
            while len(out) % 16:
                out += b"\xff"
            return out

        names = bytes(self.names)
        while len(names) % 16:
            names += b"\xff"
        data = bytes(self.data)
        while len(data) % 16:
            data += b"\0"
        loc = fixups(self.local, 2)
        glo = fixups([(src, 2, dst) for src, dst in self.glob], 3)
        vir = fixups([(off, 2, name) for off, name in self.virt], 3)
        sections = []
        pos = body_at
        # classnames: no fixups, every table offset is the end
        sections.append(("__classnames__", pos, [len(names)] * 6, names))
        pos += len(names)
        sections.append(("__types__", pos, [0] * 6, b""))
        d_local = len(data)
        d_glob = d_local + len(loc)
        d_virt = d_glob + len(glo)
        d_end = d_virt + len(vir)
        sections.append(("__data__", pos, [d_local, d_glob, d_virt, d_end, d_end, d_end], data + loc + glo + vir))
        out = bytearray(header)
        for tag, start, offs, _ in sections:
            h = tag.encode().ljust(19, b"\0") + b"\xff" + struct.pack("<7I", start, *offs)
            h = h.ljust(section_header, b"\xff")
            out += h
        assert len(out) == body_at
        for _, _, _, body in sections:
            out += body
        return bytes(out)


def build_hkx_packfile(scene: HkScene, pointer_size: int = 8, version: str = "hk_2014.1.0-r1") -> bytes:
    w = _Packfile(pointer_size, version)
    root = w.alloc("hkRootLevelContainer")
    skeletons, animations, bindings = [], [], []
    if scene.bones:
        skeletons.append(w.obj("hkaSkeleton", {
            "name": scene.skeleton_name, "parentIndices": [b.parent for b in scene.bones],
            "bones": [{"name": b.name, "lockTranslation": False} for b in scene.bones],
            "referencePose": [b.qs() for b in scene.bones], "referenceFloats": [], "floatSlots": [],
            "localFrames": [], "partitions": []}))
    if scene.animation is not None:
        a = scene.animation
        anim = w.obj("hkaSplineCompressedAnimation", {
            "type": 3, "duration": a.duration, "numberOfTransformTracks": a.tracks, "numberOfFloatTracks": 0,
            "extractedMotion": None, "annotationTracks": [{"trackName": f"track{i}", "annotations": []} for i in range(a.tracks)],
            "numFrames": a.frames, "numBlocks": len(a.block_offsets), "maxFramesPerBlock": a.max_frames_per_block,
            "maskAndQuantizationSize": a.mask_size, "blockDuration": (a.max_frames_per_block - 1) * a.frame_duration,
            "blockInverseDuration": 1.0 / max((a.max_frames_per_block - 1) * a.frame_duration, 1e-9),
            "frameDuration": a.frame_duration, "blockOffsets": list(a.block_offsets), "floatBlockOffsets": [len(a.data)],
            "transformOffsets": [], "floatOffsets": [], "data": list(a.data), "endian": 0})
        animations.append(anim)
        bindings.append(w.obj("hkaAnimationBinding", {
            "originalSkeletonName": scene.skeleton_name, "animation": anim,
            "transformTrackToBoneIndices": list(a.track_to_bone), "floatTrackToFloatSlotIndices": [],
            "partitionIndices": [], "blendHint": 0}))
    container = w.obj("hkaAnimationContainer", {"skeletons": skeletons, "animations": animations, "bindings": bindings,
                                                "attachments": [], "skins": []})
    w.obj("hkRootLevelContainer", {"namedVariants": [
        {"name": "Merged Animation Container", "className": "hkaAnimationContainer", "variant": container}]}, at=root)
    return w.build(root)


# ---------------------------------------------------------------------------
# Tagfile writer (Havok 2015, as Dark Souls Remastered)


class _TagType:
    def __init__(self, name: str, fmt: int = 0, size: int = 0, align: int = 0, parent: int = 0, pointee: int = 0,
                 members: list[tuple[str, int, int]] | None = None, templates: list[tuple[str, int]] | None = None):
        self.name, self.fmt, self.size, self.align = name, fmt, size, align
        self.parent, self.pointee = parent, pointee
        self.members = members or []  # (name, offset, type index)
        self.templates = templates or []  # (template name, type index)


def _varint(v: int) -> bytes:
    if v < 0x80:
        return bytes([v])
    if v < 0x4000:
        return struct.pack(">H", 0x8000 | v)
    if v < 0x200000:
        return bytes([0xC0 | (v >> 16), (v >> 8) & 0xFF, v & 0xFF])
    return struct.pack(">I", 0xE0000000 | v)


def _section(tag: str, body: bytes) -> bytes:
    return struct.pack(">I", 0x40000000 | (len(body) + 8)) + tag.encode() + body


class _Tagfile:
    INT32 = 0x4 | (0x02 | 0x80) << 8
    UINT8 = 0x4 | 0x20 << 8
    INT16 = 0x4 | (0x02 | 0x40) << 8
    UINT16 = 0x4 | 0x40 << 8
    UINT32 = 0x4 | 0x80 << 8
    FLOAT = 0x1746 << 8 | 5

    def __init__(self):
        self.types: list[_TagType | None] = [None]
        self.items: list[tuple[int, int, int] | None] = [None]
        self.data = bytearray()
        self.t: dict[str, int] = {}

    def add(self, name: str, **kw) -> int:
        self.types.append(_TagType(name, **kw))
        idx = len(self.types) - 1
        self.t.setdefault(name, idx)
        return idx

    def array_of(self, elem: int, label: str) -> int:
        """hkArray<T> as the 2015 files write it: m_data pointer, m_size, m_capacityAndFlags."""
        ptr = self.add(f"T*<{label}>", fmt=6, size=8, align=8, pointee=elem)
        return self.add("hkArray", fmt=8, size=16, align=8, pointee=elem,
                        members=[("m_data", 0, ptr), ("m_size", 8, self.t["int"]), ("m_capacityAndFlags", 12, self.t["int"])],
                        templates=[("tT", elem), ("tAllocator", self.t["void"])])

    def ref(self, target: int, label: str) -> int:
        ptr = self.add(f"T*<{label}>", fmt=6, size=8, align=8, pointee=target)
        return self.add("hkRefPtr", fmt=6, size=8, align=8, pointee=target, members=[("ptr", 0, ptr)], templates=[("tTYPE", target)])

    # ---- data

    def _reserve(self, size: int, align: int = 16) -> int:
        while len(self.data) % align:
            self.data.append(0)
        at = len(self.data)
        self.data += bytes(size)
        return at

    def item(self, type_idx: int, count: int, payload: bytes, align: int = 16) -> int:
        at = self._reserve(len(payload), align)
        self.data[at:at + len(payload)] = payload
        self.items.append((type_idx, at, count))
        return len(self.items) - 1

    def string(self, s: str) -> int:
        return 0 if not s else self.item(self.t["char"], len(s) + 1, s.encode() + b"\0", 1)

    def build(self) -> bytes:
        names = sorted({t.name for t in self.types[1:]} | {n for t in self.types[1:] for n, _ in t.templates})
        name_idx = {n: i for i, n in enumerate(names)}
        members = sorted({m[0] for t in self.types[1:] for m in t.members})
        member_idx = {n: i for i, n in enumerate(members)}
        tstr = "\0".join(names).encode() + b"\0"
        fstr = "\0".join(members).encode() + b"\0"
        tnam = _varint(len(self.types))
        for t in self.types[1:]:
            tnam += _varint(name_idx[t.name]) + _varint(len(t.templates))
            for n, v in t.templates:
                tnam += _varint(name_idx[n]) + _varint(v)
        tbod = b""
        for i, t in enumerate(self.types[1:], 1):
            flags = (1 if t.fmt else 0) | (2 if t.pointee else 0) | (8 if t.size else 0) | (32 if t.members else 0)
            body = _varint(i) + _varint(t.parent) + _varint(flags)
            if t.fmt:
                body += _varint(t.fmt)
            if t.pointee:
                body += _varint(t.pointee)
            if t.size:
                body += _varint(t.size) + _varint(t.align)
            if t.members:
                body += _varint(len(t.members))
                for n, off, ti in t.members:
                    body += _varint(member_idx[n]) + _varint(0) + _varint(off) + _varint(ti)
            tbod += body
        type_sec = _section("TYPE", _section("TPTR", bytes(8)) + _section("TSTR", tstr) + _section("TNAM", tnam)
                            + _section("FSTR", fstr) + _section("TBOD", tbod) + _section("THSH", bytes(1)) + _section("TPAD", b""))
        items = b""
        for it in self.items:
            if it is None:
                items += bytes(12)
            else:
                items += struct.pack("<III", it[0] | (0x10 << 24), it[1], it[2])
        data = bytes(self.data)
        while len(data) % 16:
            data += b"\0"
        indx = _section("INDX", _section("ITEM", items) + _section("PTCH", b""))
        body = _section("SDKV", b"20150100") + _section("DATA", data) + type_sec + indx
        return _section("TAG0", body)


def build_hkx_tagfile(scene: HkScene) -> bytes:
    w = _Tagfile()
    T = w.t
    w.add("void")
    w.add("int", fmt=w.INT32, size=4, align=4)
    w.add("char", fmt=0x4 | (0x02 | 0x20) << 8, size=1, align=1)
    w.add("float", fmt=w.FLOAT, size=4, align=4)
    w.add("hkBool", fmt=2, size=1, align=1)
    w.add("hkStringPtr", fmt=3, size=8, align=8)
    w.add("hkBaseObject", fmt=7, size=8, align=8)
    w.add("hkUint16", fmt=w.UINT16, size=2, align=2)
    w.add("short", fmt=w.INT16, size=2, align=2)
    w.add("hkInt16", parent=T["short"])  # a typedef: no format of its own
    w.add("hkUint8", fmt=w.UINT8, size=1, align=1)
    w.add("hkUint32", fmt=w.UINT32, size=4, align=4)
    ref_obj = w.add("hkReferencedObject", fmt=7, size=16, align=8, parent=T["hkBaseObject"],
                    members=[("memSizeAndFlags", 8, T["hkUint16"]), ("refCount", 10, T["hkUint16"])])
    f4 = w.add("float[4]", fmt=0x28 | 4 << 8, size=16, align=16, pointee=T["float"])
    vec4 = w.add("hkVector4f", fmt=7, size=16, align=16, members=[("data", 0, f4)])
    quat = w.add("hkQuaternionf", fmt=7, size=16, align=16, members=[("vec", 0, vec4)])
    qs = w.add("hkQsTransformf", fmt=7, size=48, align=16,
               members=[("translation", 0, vec4), ("rotation", 16, quat), ("scale", 32, vec4)])
    bone = w.add("hkaBone", fmt=7, size=16, align=8, members=[("name", 0, T["hkStringPtr"]), ("lockTranslation", 8, T["hkBool"])])
    skeleton = w.add("hkaSkeleton", fmt=7, size=136, align=8, parent=ref_obj, members=[
        ("name", 16, T["hkStringPtr"]), ("parentIndices", 24, w.array_of(T["hkInt16"], "hkInt16")),
        ("bones", 40, w.array_of(bone, "hkaBone")), ("referencePose", 56, w.array_of(qs, "hkQsTransformf")),
        ("referenceFloats", 72, w.array_of(T["float"], "float")), ("floatSlots", 88, w.array_of(T["hkStringPtr"], "hkStringPtr")),
        ("localFrames", 104, w.array_of(T["void"], "lf")), ("partitions", 120, w.array_of(T["void"], "part"))])
    anim_base = w.add("hkaAnimation", fmt=7, size=56, align=8, parent=ref_obj, members=[
        ("type", 12, T["int"]), ("duration", 16, T["float"]), ("numberOfTransformTracks", 20, T["int"]),
        ("numberOfFloatTracks", 24, T["int"]), ("annotationTracks", 40, w.array_of(T["void"], "ann"))])
    u32s = w.array_of(T["hkUint32"], "hkUint32")
    spline = w.add("hkaSplineCompressedAnimation", fmt=7, size=176, align=8, parent=anim_base, members=[
        ("numFrames", 56, T["int"]), ("numBlocks", 60, T["int"]), ("maxFramesPerBlock", 64, T["int"]),
        ("maskAndQuantizationSize", 68, T["int"]), ("blockDuration", 72, T["float"]), ("blockInverseDuration", 76, T["float"]),
        ("frameDuration", 80, T["float"]), ("blockOffsets", 88, u32s), ("floatBlockOffsets", 104, u32s),
        ("transformOffsets", 120, u32s), ("floatOffsets", 136, u32s), ("data", 152, w.array_of(T["hkUint8"], "hkUint8")),
        ("endian", 168, T["int"])])
    i16s = w.array_of(T["hkInt16"], "hkInt16b")
    binding = w.add("hkaAnimationBinding", fmt=7, size=88, align=8, parent=ref_obj, members=[
        ("originalSkeletonName", 16, T["hkStringPtr"]), ("animation", 24, w.ref(anim_base, "hkaAnimation")),
        ("transformTrackToBoneIndices", 32, i16s), ("floatTrackToFloatSlotIndices", 48, i16s),
        ("partitionIndices", 64, i16s), ("blendHint", 80, T["hkUint8"])])
    container = w.add("hkaAnimationContainer", fmt=7, size=96, align=8, parent=ref_obj, members=[
        ("skeletons", 16, w.array_of(w.ref(skeleton, "hkaSkeleton"), "skel")),
        ("animations", 32, w.array_of(w.ref(anim_base, "hkaAnimation2"), "anim")),
        ("bindings", 48, w.array_of(w.ref(binding, "hkaAnimationBinding"), "bind")),
        ("attachments", 64, w.array_of(T["void"], "att")), ("skins", 80, w.array_of(T["void"], "skin"))])
    variant = w.add("hkRefVariant", fmt=6, size=8, align=8, pointee=ref_obj,
                    members=[("ptr", 0, w.add("T*<var>", fmt=6, size=8, align=8, pointee=ref_obj))])
    named = w.add("hkRootLevelContainer::NamedVariant", fmt=7, size=24, align=8, members=[
        ("name", 0, T["hkStringPtr"]), ("className", 8, T["hkStringPtr"]), ("variant", 16, variant)])
    root_t = w.add("hkRootLevelContainer", fmt=7, size=16, align=8, members=[("namedVariants", 0, w.array_of(named, "nv"))])

    def arr(idx: int, count: int) -> bytes:
        return struct.pack("<IiiI", idx, 0, count, count | 0x80000000)

    def u(v: int) -> bytes:
        return struct.pack("<Ii", v, 0)  # a pointer / string slot: item index then padding

    # the root item is item 1: reserve it first
    w.items.append(None)
    root_item = len(w.items) - 1
    skeletons = animations = bindings = 0
    if scene.bones:
        names = [w.string(b.name) for b in scene.bones]
        bones_item = w.item(bone, len(scene.bones), b"".join(u(n) + struct.pack("<?7x", False) for n in names))
        parents_item = w.item(T["hkInt16"], len(scene.bones), struct.pack(f"<{len(scene.bones)}h", *[b.parent for b in scene.bones]), 2)
        pose_item = w.item(qs, len(scene.bones), b"".join(struct.pack("<12f", *b.qs()) for b in scene.bones))
        sk_name = w.string(scene.skeleton_name)
        sk = bytearray(136)
        sk[16:24] = u(sk_name)
        sk[24:40] = arr(parents_item, len(scene.bones))
        sk[40:56] = arr(bones_item, len(scene.bones))
        sk[56:72] = arr(pose_item, len(scene.bones))
        sk_item = w.item(skeleton, 1, bytes(sk))
        skeletons = w.item(w.t["hkRefPtr"], 1, struct.pack("<Ii", sk_item, 0))
    if scene.animation is not None:
        a = scene.animation
        blocks = w.item(T["hkUint32"], len(a.block_offsets), struct.pack(f"<{len(a.block_offsets)}I", *a.block_offsets), 4)
        fblocks = w.item(T["hkUint32"], 1, struct.pack("<I", len(a.data)), 4)
        data_item = w.item(T["hkUint8"], len(a.data), a.data)
        an = bytearray(176)
        struct.pack_into("<ifii", an, 12, 3, a.duration, a.tracks, 0)
        struct.pack_into("<iiiifff", an, 56, a.frames, len(a.block_offsets), a.max_frames_per_block, a.mask_size,
                         (a.max_frames_per_block - 1) * a.frame_duration,
                         1.0 / max((a.max_frames_per_block - 1) * a.frame_duration, 1e-9), a.frame_duration)
        an[88:104] = arr(blocks, len(a.block_offsets))
        an[104:120] = arr(fblocks, 1)
        an[152:168] = arr(data_item, len(a.data))
        an_item = w.item(spline, 1, bytes(an))
        map_item = w.item(T["hkInt16"], len(a.track_to_bone), struct.pack(f"<{len(a.track_to_bone)}h", *a.track_to_bone), 2)
        bd = bytearray(88)
        bd[16:24] = u(w.string(scene.skeleton_name))
        bd[24:32] = u(an_item)
        bd[32:48] = arr(map_item, len(a.track_to_bone))
        bd_item = w.item(binding, 1, bytes(bd))
        animations = w.item(w.t["hkRefPtr"], 1, struct.pack("<Ii", an_item, 0))
        bindings = w.item(w.t["hkRefPtr"], 1, struct.pack("<Ii", bd_item, 0))
    ct = bytearray(96)
    ct[16:32] = arr(skeletons, 1 if skeletons else 0)
    ct[32:48] = arr(animations, 1 if animations else 0)
    ct[48:64] = arr(bindings, 1 if bindings else 0)
    ct_item = w.item(container, 1, bytes(ct))
    nv = u(w.string("Merged Animation Container")) + u(w.string("hkaAnimationContainer")) + u(ct_item)
    nv_item = w.item(named, 1, nv)
    root = arr(nv_item, 1)
    at = w._reserve(len(root))
    w.data[at:at + len(root)] = root
    w.items[root_item] = (root_t, at, 1)
    return w.build()
