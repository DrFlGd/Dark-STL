"""GTA V's names and game data: Jenkins hashes, ``.gxt2`` text, and the ``.meta`` XML lists.

- :func:`joaat`: the one-at-a-time hash GTA V names things by (lower case).
- ``.gxt2``: ``GXT2``, an entry count, (hash, offset) pairs, ``GXT2`` and the end, then UTF-8 strings.
- ``vehicles.meta``: each vehicle's model, texture dictionary, label (``gameName``, a text key), maker
  and class; ``peds.meta``: each ped's model and type; ``weapons*.meta``: each weapon's model and label.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass

GXT2 = 0x47585432  # "2TXG" read little-endian


def joaat(text: str) -> int:
    h = 0
    for b in text.lower().encode("utf-8"):
        h = (h + b) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    return (h + (h << 15)) & 0xFFFFFFFF


def read_gxt2(data: bytes) -> dict[int, str]:
    if len(data) < 8 or struct.unpack_from("<I", data, 0)[0] != GXT2:
        return {}
    count = struct.unpack_from("<I", data, 4)[0]
    if 8 + 8 * count + 8 > len(data):
        return {}
    out = {}
    for i in range(count):
        h, off = struct.unpack_from("<II", data, 8 + 8 * i)
        if off >= len(data):
            continue
        end = data.find(b"\0", off)
        out[h] = data[off:end if end >= 0 else len(data)].decode("utf-8", "replace")
    return out


@dataclass
class VehicleInfo:
    model: str
    txd: str
    label: str  # text key
    maker: str
    kind: str  # VEHICLE_TYPE_CAR ...
    vclass: str  # VC_SUPER ...


@dataclass
class PedInfo:
    model: str
    ped_type: str  # CIVMALE, COP ...
    props: str = ""
    clipset: str = ""


@dataclass
class WeaponInfo:
    name: str  # WEAPON_PISTOL
    model: str
    label: str  # text key (WT_PIST)
    group: str = ""


def _xml(data: bytes):
    if len(data) > 64 << 20:
        return None
    text = data.decode("utf-8", "replace")
    if "<!ENTITY" in text:  # no entity tricks in game data
        return None
    text = re.sub(r"^﻿", "", text)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _t(item, tag: str) -> str:
    e = item.find(tag)
    if e is None:
        return ""
    return (e.text or e.get("value") or "").strip()


def vehicles(data: bytes) -> list[VehicleInfo]:
    root = _xml(data)
    if root is None:
        return []
    out = []
    for item in root.iter("Item"):
        model = _t(item, "modelName")
        if not model or item.find("handlingId") is None:
            continue
        out.append(VehicleInfo(model.lower(), _t(item, "txdName").lower(), _t(item, "gameName"),
                               _t(item, "vehicleMakeName"), _t(item, "type"), _t(item, "vehicleClass")))
    return out


def peds(data: bytes) -> list[PedInfo]:
    root = _xml(data)
    if root is None:
        return []
    out = []
    for item in root.iter("Item"):
        name = _t(item, "Name")
        if not name or item.find("Pedtype") is None:
            continue
        out.append(PedInfo(name.lower(), _t(item, "Pedtype").upper(), _t(item, "PropsName").lower(),
                           _t(item, "MovementClipSet")))
    return out


def weapons(data: bytes) -> list[WeaponInfo]:
    root = _xml(data)
    if root is None:
        return []
    out = []
    for item in root.iter("Item"):
        if (item.get("type") or "") != "CWeaponInfo":
            continue
        name, model = _t(item, "Name"), _t(item, "Model")
        if not name or not model:
            continue
        out.append(WeaponInfo(name.upper(), model.lower(), _t(item, "HumanNameHash"), _t(item, "Group")))
    return out
