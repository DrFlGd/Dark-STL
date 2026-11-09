"""The library overlay: the user's own changes to a game's library, kept apart from the game and the packages.

One file per game (``<settings>/overlays/<game>.json``) holds what the user changed. Nothing in the game's
folder or its model packages is touched, and every entry is keyed by the model's path in the game (the
same for the game's file and for the package made from it), so the changes survive rescans, moving the
library, and importing the game as packages.

It holds:

- per model, its name (the one it has in the game, say) and its folder (``Kind/Group``, the user's own:
  "Bosses", "Weapons/Long swords"), in place of what the importer made of its file names;
- folder renames: a kind or group of the game's renamed or moved as a whole ("Characters" -> "Bosses");
- per model and material, the texture to use for its colour, normal or height map, picked by name from
  the game's textures in the app (the texture picker). The texture itself is kept in the game's asset
  store (see :mod:`darkstl.package`), so the choice still works once the game is uninstalled.
- per model, its saved poses: a name for an animation, a frame and the bone adjustments made on top
  (``poses``), offered to every model of the game whose skeleton has the same bones (``skeleton``);
- per model, the models attached to it (``attach``): each one's file (its path in the game), the model in
  it, and the bone or socket it goes on with its offset, turn and scale (see :mod:`darkstl.attach`).

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

    def _set(self, key: str, field_name: str, value) -> None:
        models = self.data["models"]
        entry = dict(models.get(key) or {})
        if value:
            entry[field_name] = value
        else:
            entry.pop(field_name, None)
        if entry:
            models[key] = entry
        else:
            models.pop(key, None)

    # ---------------------------------------------------------------- saved poses

    def poses(self, key: str) -> dict[str, dict]:
        """{name: {"animation", "frame", "bones", "skeleton", "label", "saved"}}: the model's saved poses."""
        return dict(self.model(key).get("poses") or {})

    def save_pose(self, key: str, name: str, pose: dict) -> str:
        """Keep ``pose`` under ``name`` (in place of one of that name); the name it is kept under."""
        name = clean_name(name)
        if not name:
            raise ValueError("A pose needs a name")
        poses = self.poses(key)
        poses[name] = {k: v for k, v in pose.items() if v is not None}
        self._set(key, "poses", dict(sorted(poses.items(), key=lambda kv: kv[0].lower())))
        return name

    def rename_pose(self, key: str, old: str, new: str) -> str:
        poses = self.poses(key)
        if old not in poses:
            raise KeyError(old)
        new = clean_name(new)
        if not new:
            raise ValueError("A pose needs a name")
        if new != old and new in poses:
            raise ValueError(f"There is a pose called {new} already")
        poses[new] = poses.pop(old)
        self._set(key, "poses", dict(sorted(poses.items(), key=lambda kv: kv[0].lower())))
        return new

    def delete_pose(self, key: str, name: str) -> bool:
        poses = self.poses(key)
        if poses.pop(name, None) is None:
            return False
        self._set(key, "poses", poses)
        return True

    def poses_like(self, skeleton: str, but: str | None = None) -> list[tuple[str, str, dict]]:
        """(model key, name, pose) of every pose saved on another model whose skeleton is ``skeleton``."""
        out = []
        for key, entry in sorted(self.data["models"].items()):
            if key == but:
                continue
            for name, pose in (entry.get("poses") or {}).items():
                if pose.get("skeleton") == skeleton:
                    out.append((key, name, pose))
        return out

    # ---------------------------------------------------------------- attached models

    def attachments(self, key: str) -> list[dict]:
        """The models attached to the model: [{"id", "file", "variant", "label", "place"}]."""
        return [dict(a) for a in self.model(key).get("attach") or [] if isinstance(a, dict)]

    def set_attachments(self, key: str, items: list[dict]) -> None:
        self._set(key, "attach", [dict(a) for a in items])

    def is_empty(self) -> bool:
        return not self.data["models"] and not self.data.get("folders")

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
        """(category, group, label) the user gives a model filed by the game under ``category``/``group``;
        None where the game's own stays."""
        entry = self.model(key)
        name = entry.get("name") or None
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
        """Changes when the textures the overlay gives any model of the file at ``rel`` change, or the models
        attached to one (what is open, and every result made from it, depends on them; a new name or folder
        changes neither, nor does moving an attached model, which the mesh options carry)."""
        rel = model_key(rel)
        found = {}
        for k, v in self.data["models"].items():
            if not (k == rel or k.startswith(rel + "|")):
                continue
            if v.get("textures"):
                found[k] = v["textures"]
            if v.get("attach"):
                found[k + "#attach"] = [[a.get("id"), a.get("file"), a.get("variant"), a.get("label")]
                                        for a in v["attach"] if isinstance(a, dict)]
        return json.dumps(found, sort_keys=True) if found else ""


def clean_name(name: str | None) -> str:
    """A pose's name as kept: one line, no padding, at most 80 characters."""
    return " ".join(str(name or "").split())[:80]


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


__all__ = ["FORMAT", "Overlay", "ROLES", "apply", "clean_folder", "clean_name", "model_key"]
