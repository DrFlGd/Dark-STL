"""The names the FromSoftware games give their weapons, armour and characters, for the library.

A weapon's model file is named by a number (``wp_a_0402.partsbnd.dcx``); the game knows it as
"Uchigatana". The link is in two of the game's files:

* its item tables (``EquipParamWeapon``, ``EquipParamProtector``, ``NpcParam``), in
  ``param/gameparam/gameparam.parambnd.dcx`` (Dark Souls, Bloodborne, Sekiro), in ``Data0.bdt`` (Dark
  Souls III) or ``regulation.bin`` (Elden Ring), the last two encrypted (AES-256-CBC, keys published by
  the modding community, as SoulsFormats has them). Each weapon's row gives the number of its model
  (``equipModelId``), each armour piece's its model number and which slot it is (head, body, arms, legs),
  each character's the id of its name (``nameId``); a character's row id begins with its model's number
  (``NpcParam`` 223000 is ``c2230``, Elden Ring's 21000010 is ``c2100``);
* its text (``msg/<language>/item.msgbnd.dcx``): the names, by row id (``WeaponName.fmg``,
  ``ProtectorName.fmg``) or name id (``NpcName.fmg``).

The few fields needed are read at offsets known per game (from the community's paramdefs, Paramdex),
checked against the size of the rows. Unpack copies these files out of the game's archives (the "In-game
names" group). Dark Souls II keeps its tables another way and isn't read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ...errors import FormatError
from ...model import LOD_SUFFIX, CatalogEntry
from . import catalog
from .formats import binder as bnd
from .formats import dcx, oodle
from .formats.fmg import is_fmg, read_fmg
from .formats.param import Param, read_param


@dataclass(frozen=True)
class Layout:
    """Where the fields Dark STL reads are in a game's rows (byte offsets)."""
    weapon_model: int  # EquipParamWeapon.equipModelId (u16)
    protector_model: int  # EquipParamProtector.equipModelId (u16)
    protector_slots: int  # the byte holding headEquip:1 (bit 1), bodyEquip (2), armEquip (3), legEquip (4)
    npc_name: int  # NpcParam.nameId (s32)


_SOULS = Layout(184, 160, 216, 8)  # Dark Souls (and Remastered), Bloodborne, Dark Souls III, Sekiro
LAYOUTS = {"Dark Souls": _SOULS, "Dark Souls Remastered": _SOULS, "Dark Souls III": _SOULS, "Bloodborne": _SOULS,
           "Sekiro": _SOULS, "Elden Ring": Layout(188, 164, 220, 12)}
SLOTS = ((1, "hd"), (2, "bd"), (3, "am"), (4, "lg"))

DS3_REGULATION_KEY = b"ds3#jn/8_7(rsY9pg55GFN7VFL#+3n/)"
ER_REGULATION_KEY = bytes.fromhex("99BFFC366A6BC8C6F5827D093602D676C42892A01C207FB024D3AF4E493FEF99")

PARAMS = ("EquipParamWeapon", "EquipParamProtector", "NpcParam")
_TEXTS = {  # which FMG files hold which names, by the start of their names (English or Japanese)
    "weapon": ("weaponname", "武器名"),
    "protector": ("protectorname", "防具名"),
    "npc": ("npcname", "npc名"),
}
_PREFERRED_LANGUAGES = ("engus", "english", "enguk")

_MSG_FILE = re.compile(r"(^|/)msg/(?P<lang>[^/]+)/(?P<name>(item|menu)[^/]*\.msgbnd(\.dcx)?)$", re.I)
_LOOSE_FMG = re.compile(r"(^|/)msg/(?P<lang>[^/]+)/.*\.fmg$", re.I)
_GAMEPARAM = re.compile(r"(^|/)gameparam[^/]*\.parambnd(\.dcx)?$", re.I)
_LOOSE_PARAM = re.compile(r"(^|/)(" + "|".join(PARAMS) + r")\.param$", re.I)
_JUNK = re.compile(r"^\s*$|%null%|^\s*\[ERROR\]|^\s*(error|no text|\?+|-+)\s*$", re.I)

FORMAT_VERSION = 1  # of the table kept in the cache: raised when what's read changes


@dataclass
class Sources:
    """The files of a game folder the names are read from."""
    texts: list[Path] = field(default_factory=list)  # msgbnd files, then loose FMG files, of one language
    params: list[Path] = field(default_factory=list)  # parambnd files and loose .param files
    regulation: list[tuple[Path, bytes]] = field(default_factory=list)  # (encrypted file, its key)
    language: str = ""

    def all(self) -> list[Path]:
        return self.texts + self.params + [p for p, _k in self.regulation]

    def signature(self) -> list:
        out = []
        for p in self.all():
            try:
                st = p.stat()
                out.append([str(p), st.st_size, st.st_mtime_ns])
            except OSError:
                out.append([str(p), -1, 0])
        return out


@dataclass
class NameTable:
    """The game's names by model: weapons by model number, armour by slot and number, characters by number."""
    weapons: dict[int, str] = field(default_factory=dict)
    armour: dict[tuple[str, int], str] = field(default_factory=dict)
    characters: dict[int, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.weapons) + len(self.armour) + len(self.characters)

    def as_json(self) -> dict:
        return {"weapons": {str(k): v for k, v in self.weapons.items()},
                "armour": {f"{s}:{n}": v for (s, n), v in self.armour.items()},
                "characters": {str(k): v for k, v in self.characters.items()}}

    @classmethod
    def from_json(cls, data: dict) -> "NameTable":
        armour = {}
        for k, v in (data.get("armour") or {}).items():
            slot, _, n = k.partition(":")
            armour[(slot, int(n))] = v
        return cls({int(k): v for k, v in (data.get("weapons") or {}).items()}, armour,
                   {int(k): v for k, v in (data.get("characters") or {}).items()})

    def name_for(self, stem: str) -> str | None:
        """The game's name for the model file ``stem`` (``wp_a_0402``, ``hd_m_1000_l``, ``c2230``)."""
        s = LOD_SUFFIX.sub("", stem.lower())
        m = catalog._RX_PART.match(s) or catalog._RX_PART_DS2.match(s)
        if m:
            kind, number = m.group("kind"), int(m.group("id"))
            if kind == "wp":
                return self.weapons.get(number)
            if kind in ("hd", "bd", "am", "lg"):
                return self.armour.get((kind, number))
            return None
        m = catalog._RX_CHR.match(s)
        if m and m.group("id") != "0000":
            return self.characters.get(int(m.group("id")))
        return None


def find_sources(folder: Path, files: list[Path] | None = None) -> Sources:
    """The name files in a game folder (``files``: its files, listed already)."""
    from ...listing import listing

    folder = Path(folder)
    files = listing(folder).files if files is None else files
    by_lang: dict[str, list[tuple[int, Path]]] = {}
    params, regulation = [], []
    names_here = {p.name.lower() for p in files if p.parent == folder}
    for p in files:
        try:
            rel = p.relative_to(folder).as_posix()
        except ValueError:
            continue
        m = _MSG_FILE.search(rel)
        if m:
            by_lang.setdefault(m.group("lang").lower(), []).append((0, p))
            continue
        m = _LOOSE_FMG.search(rel)
        if m:
            by_lang.setdefault(m.group("lang").lower(), []).append((1, p))
            continue
        low = p.name.lower()
        if _GAMEPARAM.search(rel) or _LOOSE_PARAM.search(rel):
            params.append(p)
        elif low == "regulation.bin" and rel.count("/") <= 2:
            regulation.append((p, ER_REGULATION_KEY))
        elif low == "data0.bdt" and rel.count("/") <= 2 and not (p.with_name("Data0.bhd").exists()
                                                                 or "data0.bhd" in names_here):
            regulation.append((p, DS3_REGULATION_KEY))  # Dark Souls III's: Elden Ring's Data0 is an archive
    lang = next((x for x in _PREFERRED_LANGUAGES if x in by_lang), sorted(by_lang)[0] if by_lang else "")
    texts = [p for _k, p in sorted(by_lang.get(lang, []), key=lambda kp: (kp[0], str(kp[1]).lower()))]
    return Sources(texts, sorted(params), regulation, lang)


def decrypt_regulation(raw: bytes, key: bytes) -> bytes:
    """An encrypted regulation file (the 16-byte IV, then AES-256-CBC), decrypted."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(raw) < 32:
        raise FormatError("The regulation file is too short")
    body = raw[16:]
    body = body[:len(body) - len(body) % 16]
    dec = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    return dec.update(body) + dec.finalize()


def _unpacked(data: bytes, where: Path) -> bytes:
    if dcx.is_dcx(data) and b"KRAK" in data[:0x40]:
        oodle.add_hint_path(where)
    return dcx.maybe_decompress(data)


def _binder_files(data: bytes, where: Path) -> list[bnd.BinderFile]:
    data = _unpacked(data, where)
    if not bnd.is_binder(data):
        raise FormatError("not a binder")
    return bnd.read_binder(data).files


def _text_kind(name: str) -> str | None:
    stem = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for kind, starts in _TEXTS.items():
        if stem.startswith(starts):
            return kind
    return None


def read_texts(paths: list[Path]) -> dict[str, dict[int, str]]:
    """{"weapon" | "protector" | "npc": {id: name}} from msgbnd and FMG files (later files' names win: a
    DLC's file holds the newer text)."""
    out: dict[str, dict[int, str]] = {k: {} for k in _TEXTS}

    def take(kind: str, data: bytes) -> None:
        if not is_fmg(data):
            return
        for i, text in read_fmg(data).items():
            text = " ".join(text.split())
            if not _JUNK.search(text):
                out[kind][i] = text

    for p in paths:
        try:
            raw = p.read_bytes()
            if p.name.lower().endswith(".fmg"):
                kind = _text_kind(p.name)
                if kind:
                    take(kind, raw)
                continue
            for f in _binder_files(raw, p.parent):
                kind = _text_kind(f.name)
                if kind:
                    take(kind, dcx.maybe_decompress(f.data))
        except (OSError, FormatError, ValueError):
            continue
    return out


def read_params(paths: list[Path], regulation: list[tuple[Path, bytes]]) -> dict[str, Param]:
    """The item tables Dark STL reads, by name, from parambnd files, loose .param files and encrypted
    regulation files (those last win: they are what the game reads)."""
    wanted = {p.lower(): p for p in PARAMS}
    out: dict[str, Param] = {}

    def take(name: str, data: bytes) -> None:
        stem = name.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0].lower()
        if stem in wanted:
            try:
                out[wanted[stem]] = read_param(dcx.maybe_decompress(data))
            except FormatError:
                pass

    for p in paths:
        try:
            raw = p.read_bytes()
            if p.name.lower().endswith(".param"):
                take(p.name, raw)
                continue
            for f in _binder_files(raw, p.parent):
                take(f.name, f.data)
        except (OSError, FormatError, ValueError):
            continue
    for p, key in regulation:
        try:
            for f in _binder_files(decrypt_regulation(p.read_bytes(), key), p.parent):
                take(f.name, f.data)
        except (OSError, FormatError, ValueError):
            continue
    return out


def _lowest_named(rows: list[tuple[int, int]], names: dict[int, str]) -> dict[int, str]:
    """{model number: the name of the first row (by id) with a name} from (row id, model number) pairs."""
    out: dict[int, str] = {}
    for row_id, model in sorted(rows):
        if model in out or not 0 <= model <= 99999:
            continue
        name = names.get(row_id)
        if name:
            out[model] = name
    return out


def build_table(game: str, texts: dict[str, dict[int, str]], params: dict[str, Param]) -> NameTable:
    """The game's names by model, from its texts and item tables."""
    layout = LAYOUTS.get(game)
    table = NameTable()
    if layout is None:
        return table
    weapons = params.get("EquipParamWeapon")
    if weapons is not None and texts.get("weapon"):
        table.weapons = _lowest_named(weapons.column(layout.weapon_model, "H"), texts["weapon"])
    armour = params.get("EquipParamProtector")
    if armour is not None and texts.get("protector") and armour.row_size > layout.protector_slots:
        models = dict(armour.column(layout.protector_model, "H"))
        slots = dict(armour.column(layout.protector_slots, "B"))
        rows: dict[str, list[tuple[int, int]]] = {}
        for row_id, model in models.items():
            bits = slots.get(row_id, 0)
            for bit, slot in SLOTS:
                if bits >> bit & 1:
                    rows.setdefault(slot, []).append((row_id, model))
        for slot, pairs in rows.items():
            for model, name in _lowest_named(pairs, texts["protector"]).items():
                table.armour[(slot, model)] = name
    npcs = params.get("NpcParam")
    if npcs is not None and texts.get("npc"):
        found: dict[int, Counter] = {}
        first: dict[tuple[int, str], int] = {}
        for row_id, name_id in npcs.column(layout.npc_name, "i"):
            name = texts["npc"].get(name_id)
            if not name or row_id < 100000:  # shorter ids are the human characters (c0000), not one model
                continue
            digits = len(str(row_id))
            number = row_id // 10 ** (digits - 4)
            found.setdefault(number, Counter())[name] += 1
            first.setdefault((number, name), row_id)
            first[(number, name)] = min(first[(number, name)], row_id)
        for number, counts in found.items():
            best = max(counts.items(), key=lambda kv: (kv[1], -first[(number, kv[0])]))
            table.characters[number] = best[0]
    return table


def read_table(folder: Path, game: str, sources: Sources | None = None) -> NameTable:
    """The game's names by model, read from a game folder's files (see :func:`find_sources`)."""
    sources = sources or find_sources(folder)
    if not sources.texts or not (sources.params or sources.regulation):
        return NameTable()
    return build_table(game, read_texts(sources.texts), read_params(sources.params, sources.regulation))


def cached_table(folder: Path, game: str, cache: Path | None, files: list[Path] | None = None,
                 force: bool = False) -> NameTable:
    """:func:`read_table`, kept in ``cache`` until the files it was read from change (or ``force``)."""
    if LAYOUTS.get(game) is None:
        return NameTable()
    sources = find_sources(folder, files)
    if not sources.texts:
        return NameTable()
    signature = [FORMAT_VERSION, game, sources.signature()]
    kept = None
    if cache is not None:
        key = hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode("utf-8")).hexdigest()[:16]
        kept = Path(cache) / f"{key}.json"
        if not force:
            try:
                data = json.loads(kept.read_text("utf-8"))
                if data.get("signature") == signature:
                    return NameTable.from_json(data.get("table") or {})
            except (OSError, ValueError, AttributeError, TypeError):
                pass
    table = read_table(folder, game, sources)
    if kept is not None:
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            tmp = kept.with_suffix(".part")
            tmp.write_text(json.dumps({"signature": signature, "table": table.as_json()}, ensure_ascii=False), "utf-8")
            os.replace(tmp, kept)
        except OSError:
            pass
    return table


def entry_names(folder: Path, entries: list[CatalogEntry], table: NameTable) -> dict[str, str]:
    """{overlay key (the model's path in the folder): its name in the game} for the models ``table`` names."""
    from ...overlay import model_key

    out = {}
    for e in entries:
        if e.config is not None:
            continue
        name = table.name_for(e.stem)
        if not name:
            continue
        try:
            rel = Path(e.path).relative_to(folder).as_posix()
        except ValueError:
            continue
        out[model_key(rel)] = name
    return out


__all__ = ["LAYOUTS", "NameTable", "Sources", "build_table", "cached_table", "decrypt_regulation", "entry_names",
           "find_sources", "read_params", "read_table", "read_texts"]
