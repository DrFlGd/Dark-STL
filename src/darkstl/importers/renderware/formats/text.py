"""The games' text data files: object definitions (``.ide``), placements (``.ipl``, text and San
Andreas' binary ``bnry`` streams), the level lists (``gta.dat``, ``default.dat``) and the car
colours (``carcols.dat``).

IDE and IPL files are sections: a line with the section's name (``objs``, ``tobj``, ``anim``,
``peds``, ``cars``, ``weap``, ``hier``, ``txdp``, ``inst``...), lines of comma or space separated
fields, then ``end``. ``#`` starts a comment.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

_SPLIT = re.compile(r"[,\s]+")


def _lines(text: str):
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0] if not raw.lstrip().startswith(";") else ""
        line = line.strip()
        if line:
            yield line


def sections(text: str) -> dict[str, list[list[str]]]:
    """Each section's rows (fields as strings), in order."""
    out: dict[str, list[list[str]]] = {}
    current: list[list[str]] | None = None
    for line in _lines(text):
        low = line.lower()
        if current is None:
            if re.fullmatch(r"[a-z0-9]+", low):
                current = out.setdefault(low, [])
            continue
        if low == "end":
            current = None
            continue
        current.append([f for f in _SPLIT.split(line) if f])
    return out


@dataclass
class Definition:
    """One model an IDE file defines."""

    id: int
    model: str
    txd: str
    section: str  # objs, tobj, anim, peds, cars, weap, hier
    file: str  # the IDE file it is in (path below the game folder, / separators)
    fields: list[str] = field(default_factory=list)  # the whole row

    @property
    def ped_type(self) -> str:
        return self.fields[3] if self.section == "peds" and len(self.fields) > 3 else ""

    @property
    def vehicle_type(self) -> str:
        return self.fields[3].lower() if self.section == "cars" and len(self.fields) > 3 else ""

    @property
    def game_name(self) -> str:
        """A vehicle's text key (looked up in the game's GXT)."""
        if self.section == "cars" and len(self.fields) > 5:
            return self.fields[5]
        return ""

    @property
    def anim_file(self) -> str:
        """The animation package a ped or animated object uses, when it names one."""
        if self.section == "peds":  # VC: field 7; SA: field 8 (after the flags); III names none
            for f in self.fields[7:9]:
                if not re.fullmatch(r"-?[0-9a-fA-F]+", f):
                    return "" if f.lower() == "null" else f.lower()
            return ""
        if self.section == "anim" and len(self.fields) > 3:
            return self.fields[3].lower()
        if self.section == "weap" and len(self.fields) > 3:
            return self.fields[3].lower()
        return ""


DEF_SECTIONS = ("objs", "tobj", "anim", "peds", "cars", "weap", "hier")


def definitions(text: str, file: str) -> tuple[list[Definition], list[tuple[str, str]]]:
    """The models an IDE file defines, and its texture parents (``txdp``: child, parent)."""
    out = []
    parents = []
    for name, rows in sections(text).items():
        if name == "txdp":
            parents += [(r[0].lower(), r[1].lower()) for r in rows if len(r) >= 2]
            continue
        if name not in DEF_SECTIONS:
            continue
        for r in rows:
            if len(r) < 3:
                continue
            try:
                ident = int(r[0])
            except ValueError:
                continue
            out.append(Definition(ident, r[1], r[2], name, file, r))
    return out, parents


@dataclass
class Placement:
    id: int
    model: str  # empty in binary IPLs (only ids)
    position: tuple[float, float, float]


def placements(text: str) -> list[Placement]:
    out = []
    for r in sections(text).get("inst", []):
        if len(r) < 5:
            continue
        try:
            ident = int(r[0])
        except ValueError:
            continue
        nums = []
        for f in r[2:]:
            try:
                nums.append(float(f))
            except ValueError:
                nums.append(0.0)
        # SA and VC put the interior before the position; III doesn't. Positions follow the model name in III.
        pos = nums[1:4] if len(r) in (11, 13) else nums[0:3]
        out.append(Placement(ident, r[1], tuple(pos[:3]) if len(pos) >= 3 else (0.0, 0.0, 0.0)))
    return out


def binary_placements(data: bytes) -> list[Placement]:
    """San Andreas' streamed placements (``bnry``): 40-byte entries of position, rotation, model id,
    interior and LOD index."""
    if data[:4] != b"bnry" or len(data) < 32:
        return []
    count = struct.unpack_from("<i", data, 4)[0]
    offset = struct.unpack_from("<i", data, 28)[0]
    out = []
    for i in range(max(count, 0)):
        at = offset + i * 40
        if at + 40 > len(data):
            break
        x, y, z, _rx, _ry, _rz, _rw, ident, _interior, _lod = struct.unpack_from("<7f3i", data, at)
        out.append(Placement(ident, "", (x, y, z)))
    return out


def level_files(text: str) -> list[tuple[str, str]]:
    """``gta.dat`` / ``default.dat`` lines: (kind, path) such as ("IDE", "DATA/MAPS/LA/LAE.IDE")."""
    out = []
    for line in _lines(text):
        parts = line.split(None, 1)
        if len(parts) == 2:
            kind, rest = parts[0].upper(), parts[1].strip()
            if kind == "COLFILE":  # COLFILE <level> <path>
                rest = rest.split(None, 1)[-1]
            out.append((kind, rest.replace("\\", "/")))
    return out


@dataclass
class CarColours:
    palette: list[tuple[int, int, int]]
    cars: dict[str, list[tuple[int, ...]]]  # model -> colour index combinations (2 or 4 each)

    def combinations(self, model: str) -> list[tuple[tuple[int, int, int], ...]]:
        out = []
        for combo in self.cars.get(model.lower(), []):
            out.append(tuple(self.palette[i] if 0 <= i < len(self.palette) else (255, 255, 255) for i in combo))
        return out


def car_colours(text: str) -> CarColours:
    sec = sections(text)
    palette = []
    for r in sec.get("col", []):
        nums = []
        for f in r:
            try:
                nums.append(int(float(f)))
            except ValueError:
                pass
        if len(nums) >= 3:
            palette.append(tuple(max(0, min(255, v)) for v in nums[:3]))
    cars: dict[str, list[tuple[int, ...]]] = {}
    for key, width in (("car", 2), ("car4", 4)):
        for r in sec.get(key, []):
            if not r:
                continue
            nums = []
            for f in r[1:]:
                try:
                    nums.append(int(f))
                except ValueError:
                    pass
            combos = [tuple(nums[i:i + width]) for i in range(0, len(nums) - width + 1, width)]
            if combos:
                cars.setdefault(r[0].lower(), []).extend(combos)
    return CarColours(palette, cars)
