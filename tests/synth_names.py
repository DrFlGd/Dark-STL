"""Byte-built FromSoftware name files: item text (msgbnd of FMG files) and item tables (PARAM files, in a
parambnd or an encrypted regulation file), as the games ship them."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import synth
from darkstl.importers.fromsoft import names as fs_names
from darkstl.importers.fromsoft.formats import dcx
from darkstl.importers.fromsoft.formats.fmg import write_fmg
from darkstl.importers.fromsoft.formats.param import write_param

ROW_SIZES = {"Dark Souls III": (608, 400, 580), "Elden Ring": (664, 416, 736), "Dark Souls Remastered": (272, 232, 336),
             "Bloodborne": (316, 268, 388)}
SLOT_BITS = {"hd": 1, "bd": 2, "am": 3, "lg": 4}


def params(game: str, weapons: dict, armour: dict, npcs: dict, long_offsets: bool = True) -> list[tuple[str, bytes]]:
    """(file name, PARAM bytes): ``weapons`` {row id: model number}, ``armour`` {row id: (slot, model number)},
    ``npcs`` {row id: name id}."""
    layout = fs_names.LAYOUTS[game]
    wsize, psize, nsize = ROW_SIZES[game]
    wrows = []
    for row_id, model in sorted(weapons.items()):
        row = bytearray(wsize)
        struct.pack_into("<H", row, layout.weapon_model, model)
        wrows.append((row_id, bytes(row)))
    prows = []
    for row_id, (slot, model) in sorted(armour.items()):
        row = bytearray(psize)
        struct.pack_into("<H", row, layout.protector_model, model)
        row[layout.protector_slots] = 1 << SLOT_BITS[slot] | 1  # bit 0 (isDeposit) set, as the games have it
        prows.append((row_id, bytes(row)))
    nrows = []
    for row_id, name_id in sorted(npcs.items()):
        row = bytearray(nsize)
        struct.pack_into("<i", row, layout.npc_name, name_id)
        nrows.append((row_id, bytes(row)))
    return [("EquipParamWeapon.param", write_param("EQUIP_PARAM_WEAPON_ST", wrows, long_offsets=long_offsets)),
            ("EquipParamProtector.param", write_param("EQUIP_PARAM_PROTECTOR_ST", prows, long_offsets=long_offsets)),
            ("NpcParam.param", write_param("NPC_PARAM_ST", nrows, long_offsets=long_offsets))]


def msgbnd(weapon_names: dict, protector_names: dict, npc_names: dict, japanese: bool = False, version: int = 2) -> bytes:
    """An item.msgbnd.dcx with the three FMG files (named as Dark Souls III and later name them, or in Japanese
    as Dark Souls does), plus a description file that must not be taken for names."""
    names = ("武器名", "防具名", "NPC名") if japanese else ("WeaponName", "ProtectorName", "NpcName")
    files = [(11, f"N:\\GR\\data\\INTERROOT_win64\\msg\\engUS\\{names[0]}.fmg", write_fmg(weapon_names, version)),
             (12, f"N:\\GR\\data\\INTERROOT_win64\\msg\\engUS\\{names[1]}.fmg", write_fmg(protector_names, version)),
             (18, f"N:\\GR\\data\\INTERROOT_win64\\msg\\engUS\\{names[2]}.fmg", write_fmg(npc_names, version)),
             (21, "N:\\GR\\data\\INTERROOT_win64\\msg\\engUS\\WeaponInfo.fmg",
              write_fmg({k: "A description, not a name" for k in weapon_names}, version))]
    return dcx.compress_dflt(synth.build_bnd4(files))


def encrypt_regulation(payload: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = os.urandom(16)
    payload = payload + b"\0" * (-len(payload) % 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + enc.update(payload) + enc.finalize()


# what the synthetic games call their models
WEAPONS = {100000: 300, 100001: 300, 100100: 300, 200000: 400}  # two weapons; the first's +1 and infusion too
WEAPON_NAMES = {100000: "Dagger", 100100: "Heavy Dagger", 200000: "Uchigatana", 300000: "Unused sword"}
ARMOUR = {10000: ("hd", 1000), 11000: ("bd", 1000), 12000: ("am", 1000)}
ARMOUR_NAMES = {10000: "Knight Helm", 11000: "Knight Armor", 12000: "Knight Gauntlets"}
NPCS = {110000: 9001, 110001: 9001, 110002: 9002, 6000: 9003}  # two names for c1100: the commoner wins
NPC_NAMES = {9001: "Hollow Soldier", 9002: "Hollow Soldier Captain", 9003: "Solaire of Astora"}


def add_names(root: Path, game: str = "Dark Souls III", how: str = "regulation") -> None:
    """Write the name files into the game folder ``root``: the item text, and the item tables as the game
    keeps them (``how``: "regulation", encrypted beside the game as Dark Souls III and Elden Ring keep them,
    or "gameparam", a plain parambnd as Dark Souls Remastered and Bloodborne do)."""
    msg = root / "msg" / "engus"
    msg.mkdir(parents=True, exist_ok=True)
    (msg / "item_dlc2.msgbnd.dcx").write_bytes(msgbnd(WEAPON_NAMES, ARMOUR_NAMES, NPC_NAMES))
    files = params(game, WEAPONS, ARMOUR, NPCS, long_offsets=game in ("Dark Souls III", "Elden Ring"))
    bnd = synth.build_bnd4([(i, f"N:\\FDP\\data\\Param\\GameParam\\{n}", d) for i, (n, d) in enumerate(files)])
    if how == "regulation":
        name, key = ("regulation.bin", fs_names.ER_REGULATION_KEY) if game == "Elden Ring" else \
            ("Data0.bdt", fs_names.DS3_REGULATION_KEY)
        (root / name).write_bytes(encrypt_regulation(dcx.compress_dflt(bnd), key))
    else:
        gp = root / "param" / "gameparam"
        gp.mkdir(parents=True, exist_ok=True)
        (gp / "gameparam.parambnd.dcx").write_bytes(dcx.compress_dflt(bnd))
