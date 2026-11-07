"""The library overlay: the user's own changes to a game's library, kept apart from the game and the packages.

One file per game (``<settings>/overlays/<game>.json``) holds what the user changed. Nothing in the game's
folder or its model packages is touched, and every entry is keyed by the model's path in the game (the
same for the game's file and for the package made from it), so the changes survive rescans, moving the
library, and importing the game as packages.

Today it holds texture assignments: per model and material, the texture to use for its colour, normal or
height map, picked by name from the game's textures in the app (the texture picker). The texture itself
is kept in the game's asset store (see :mod:`darkstl.package`), so the choice still works once the game
is uninstalled.
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

    def signature_of_file(self, rel: str) -> str:
        """Changes when what the overlay says about any model of the file at ``rel`` changes."""
        rel = model_key(rel)
        found = {k: v for k, v in self.data["models"].items() if k == rel or k.startswith(rel + "|")}
        return json.dumps(found, sort_keys=True) if found else ""

    def signature(self, key: str) -> str:
        """Changes when what the overlay says about ``key`` changes (so open models and results are redone)."""
        entry = self.model(key)
        return json.dumps(entry, sort_keys=True) if entry else ""


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


__all__ = ["FORMAT", "Overlay", "ROLES", "apply", "model_key"]
