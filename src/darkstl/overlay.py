"""The library overlay: the user's own changes to a game's library, kept apart from the game and the packages.

One file per game (``<settings>/overlays/<game>.json``) holds what the user changed. Nothing in the game's
folder or its model packages is touched, and every entry is keyed by the model's path in the game (the
same for the game's file and for the package made from it), so the changes survive rescans, moving the
library, and importing the game as packages.

It holds:

- per model, its name and its folder (``Kind/Group``, the user's own: "Bosses", "Weapons/Long swords"), in
  place of what the importer made of its file names;
- the names the game itself gives its models (``game_names``: read from the game's text and item tables at
  each scan, see :meth:`Importer.names`), under the user's own: a model is called by the name the user gave
  it, else the game's, else what the importer made of its file name. Kept here, so they stay once the
  game is uninstalled;
- folder renames: a kind or group of the game's renamed or moved as a whole ("Characters" -> "Bosses");
- per model and material, the texture to use for its colour, normal or height map, picked by name from
  the game's textures in the app (the texture picker). The texture itself is kept in the game's asset
  store (see :mod:`darkstl.package`), so the choice still works once the game is uninstalled.

Models combined into one (face, body and hair) are configurations the user saves (see
:mod:`darkstl.configs`), filed like any other model.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

from .model import SourceModel, Texture

FORMAT = "darkstl-overlay"
VERSION = 1
ROLES = ("albedo", "normal", "height")
ROLE_NAMES = {"albedo": "colour map", "normal": "normal map", "height": "height map"}

_lock = threading.Lock()


def model_key(rel: str, variant: str | None = None) -> str:
    """The key an overlay files a model under: its path in the game (``/`` separators), and the model
    inside the file when the file holds several."""
    rel = str(rel).replace("\\", "/").lstrip("/")
    return f"{rel}|{variant}" if variant else rel


class Overlay:
    """One game's overlay file."""

    def __init__(self, path: str | os.PathLike, game: str):
        self.path = Path(path)
        self.game = game
        self.data = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            data = {"format": FORMAT, "version": VERSION, "game": self.game, "models": {}}
        data.setdefault("models", {})
        return data

    def save(self) -> None:
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), "utf-8")
            os.replace(tmp, self.path)

    def model(self, key: str) -> dict:
        return self.data["models"].get(key) or {}

    def textures(self, key: str) -> dict[str, dict[str, dict]]:
        """{material index (as text): {role: {"file": stored texture, "name": its name in the game}}}."""
        return dict(self.model(key).get("textures") or {})

    def assign(self, key: str, material: int, role: str, texture: dict | None) -> None:
        """Use ``texture`` ({"file", "name"}) for ``role`` of material ``material``; None goes back to the
        texture the model names itself."""
        if role not in ROLES:
            raise ValueError(f"unknown texture role {role!r}")
        models = self.data["models"]
        entry = dict(models.get(key) or {})
        slots = dict(entry.get("textures") or {})
        slot = dict(slots.get(str(material)) or {})
        if texture is None:
            slot.pop(role, None)
        else:
            slot[role] = {"file": str(texture["file"]), "name": str(texture.get("name") or "")}
        if slot:
            slots[str(material)] = slot
        else:
            slots.pop(str(material), None)
        if slots:
            entry["textures"] = slots
        else:
            entry.pop("textures", None)
        if entry:
            models[key] = entry
        else:
            models.pop(key, None)

    def is_empty(self) -> bool:
        return not self.data["models"] and not self.data.get("folders") and not self.data.get("game_names")

    def game_name(self, key: str) -> str | None:
        """The model's name in the game, if it's known."""
        return (self.data.get("game_names") or {}).get(key) or None

    def set_game_names(self, names: dict[str, str]) -> int:
        """Keep the names the game gives these models (others stay as they were). How many changed."""
        known = dict(self.data.get("game_names") or {})
        changed = 0
        for key, name in names.items():
            name = " ".join(str(name or "").split())
            if name and known.get(key) != name:
                known[key] = name
                changed += 1
        if known:
            self.data["game_names"] = known
        return changed

    def set_place(self, key: str, name: str | None = None, folder: str | None = None) -> None:
        """Name and file a model as the user says; "" or None for either goes back to the game's own."""
        models = self.data["models"]
        entry = dict(models.get(key) or {})
        for field_name, value in (("name", name), ("folder", folder)):
            value = clean_folder(value) if field_name == "folder" else (value or "").strip()
            if value:
                entry[field_name] = value
            else:
                entry.pop(field_name, None)
        if entry:
            models[key] = entry
        else:
            models.pop(key, None)

    def rename_folder(self, old: str, new: str | None) -> None:
        """Rename or move a kind (``"Characters"``) or a group in it (``"Weapons/IDs 03xx"``) as a whole;
        ``new`` "" or None undoes it."""
        folders = dict(self.data.get("folders") or {})
        old, new = clean_folder(old), clean_folder(new)
        if not old:
            raise ValueError("no folder to rename")
        if new and new != old:
            folders[old] = new
        else:
            folders.pop(old, None)
        if folders:
            self.data["folders"] = folders
        else:
            self.data.pop("folders", None)

    def place(self, key: str, category: str, group: str) -> tuple[str | None, str | None, str | None]:
        """(category, group, label) the user gives a model filed by the game under ``category``/``group``
        (the label: the user's name for it, else the game's); None where the file's own stays."""
        entry = self.model(key)
        name = entry.get("name") or self.game_name(key)
        folder = entry.get("folder")
        if not folder:
            folders = self.data.get("folders") or {}
            folder = folders.get(clean_folder(f"{category}/{group}")) if group else None
            if folder is None and clean_folder(category) in folders:
                folder = folders[clean_folder(category)] + (f"/{group}" if group else "")
        if not folder:
            return None, None, name
        kind, _, rest = folder.partition("/")
        return kind, rest.replace("/", " / "), name

    def signature_of_file(self, rel: str) -> str:
        """Changes when the textures the overlay gives any model of the file at ``rel`` change (what is
        open, and every result made from it, depends on them; a new name or folder changes neither)."""
        rel = model_key(rel)
        found = {k: v["textures"] for k, v in self.data["models"].items()
                 if (k == rel or k.startswith(rel + "|")) and v.get("textures")}
        return json.dumps(found, sort_keys=True) if found else ""


def clean_folder(path: str | None) -> str:
    """A folder path as the overlay keeps it: ``Kind/Group/...``, no empty or padded parts."""
    parts = [p.strip() for p in str(path or "").replace("\\", "/").replace(" / ", "/").split("/")]
    return "/".join(p for p in parts if p)


def apply(model: SourceModel, assigned: dict[str, dict[str, dict]], store_root: Path) -> SourceModel:
    """``model`` with the textures the overlay assigns to its materials (``assigned`` as
    :meth:`Overlay.textures` gives it; files relative to the game's asset store at ``store_root``)."""
    from .package import _decode_image

    if not assigned:
        return model
    materials = list(model.materials)
    textures = dict(model.textures)
    notes = list(model.notes)
    for index_text, roles in assigned.items():
        try:
            i = int(index_text)
        except ValueError:
            continue
        if not 0 <= i < len(materials):
            continue
        mat = materials[i]
        changes = {}
        for role, tex in roles.items():
            if role not in ROLES or not tex.get("file"):
                continue
            key = f"assigned:{tex['file']}"
            path = Path(store_root) / tex["file"]
            if not path.is_file():
                notes.append(f"The {ROLE_NAMES[role]} chosen for {mat.name} ({tex.get('name') or tex['file']}) "
                             "isn't in the game's asset store any more, so the model's own is used.")
                continue
            if key not in textures:
                textures[key] = Texture(tex.get("name") or Path(tex["file"]).stem, f"assigned in Dark STL ({tex['file']})",
                                        load=lambda p=path: _decode_image(p))
            changes[role] = key
        if changes:
            note = ", ".join(f"{ROLE_NAMES[r]} {textures[k].name}" for r, k in changes.items())
            materials[i] = replace(mat, **changes, details=list(mat.details) + [f"assigned in the app: {note}"])
    return replace(model, materials=materials, textures=textures, notes=notes)


__all__ = ["FORMAT", "Overlay", "ROLES", "apply", "clean_folder", "model_key"]
