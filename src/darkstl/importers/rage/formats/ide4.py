"""GTA IV's item definitions (``.ide``) and text (``.gxt``): what each model is and what the game calls it.

The ``.ide`` files are the same kind of text tables as in the RenderWare games (sections closed by
``end``): ``cars`` (model, texture dictionary, type, handling, game name...), ``peds`` (model,
props, ped type...) and ``txdp`` (a texture dictionary and its parent). The text is a version 4
``.gxt`` with UTF-16 strings keyed by the hash of each key, read with the RenderWare reader.
"""

from __future__ import annotations

from ...renderware.formats.gxt import read_gxt
from ...renderware.formats.text import sections
from .text import PedInfo, VehicleInfo, joaat

VEHICLE_TYPES = {"car": "CAR", "bike": "BIKE", "boat": "BOAT", "heli": "HELI", "plane": "PLANE",
                 "train": "TRAIN", "automobile": "CAR"}


def definitions(text: str) -> tuple[list[VehicleInfo], list[PedInfo], list[tuple[str, str]]]:
    s = sections(text)
    cars = []
    for row in s.get("cars", []):
        if len(row) < 5:
            continue
        kind = VEHICLE_TYPES.get(row[2].lower(), row[2].upper())
        cars.append(VehicleInfo(row[0].lower(), row[1].lower(), row[4], "", kind, kind))
    peds = []
    for row in s.get("peds", []):
        if row:
            peds.append(PedInfo(row[0].lower(), (row[2] if len(row) > 2 else "").upper(),
                                row[1].lower() if len(row) > 1 and row[1].lower() != "null" else ""))
    parents = [(row[0].lower(), row[1].lower()) for row in s.get("txdp", []) if len(row) >= 2]
    return cars, peds, parents


def text(data: bytes) -> dict[int, str]:
    """The game's text by the hash of each key (the GTA V hash, as :func:`.text.joaat` makes it), plus
    the keys stored in plain."""
    g = read_gxt(data)
    out = dict(g.by_hash)
    for k, v in g.by_key.items():
        out.setdefault(joaat(k), v)
    return out
