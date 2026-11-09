"""Where the app keeps its settings and caches, and the settings themselves.

Settings are one JSON file: the game folders the user added (and which game
each is), extra texture folders, the Oodle DLL, where exports go, where Model
Painter is and the page's own preferences (theme, layout). ``DARKSTL_HOME`` puts everything in
one folder instead (for tests and portable installs).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import retired


def config_dir() -> Path:
    if os.environ.get("DARKSTL_HOME"):
        return Path(os.environ["DARKSTL_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "DarkSTL"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DarkSTL"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "darkstl"


def cache_dir() -> Path:
    if os.environ.get("DARKSTL_HOME"):
        return Path(os.environ["DARKSTL_HOME"]) / "cache"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "DarkSTL" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "DarkSTL"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "darkstl"


def models_dir() -> Path:
    """Where the user's AI upscaling/deblocking models (.onnx) live."""
    return config_dir() / "models"


def filelists_dir() -> Path:
    """Where newer file lists for unpacking game archives go when downloaded from Settings."""
    return config_dir() / "filelists"


def default_export_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "Dark STL exports"


@dataclass
class Config:
    game_folders: list[dict] = field(default_factory=list)  # [{"path": ..., "game": ...}], in the order added
    texture_dirs: list[str] = field(default_factory=list)
    oodle: str = ""
    export_dir: str = ""
    library_dir: str = ""  # where collected copies of game files go (empty: the app's own folder)
    model_painter: str = ""  # Model Painter's program, to open exports in (empty: found by itself, painter.py)
    prefs: dict = field(default_factory=dict)  # the page's own preferences
    posed_previews: bool = True  # draw characters' previews in a resting frame of an idle animation
    last_enhance: dict = field(default_factory=dict)  # game -> the settings it was last generated with (bulk Enhance)
    path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        keys = ("game_folders", "texture_dirs", "oodle", "export_dir", "library_dir", "model_painter", "prefs",
                "posed_previews", "last_enhance")
        known = {k: raw[k] for k in keys if k in raw}
        cfg = cls(**known)
        cfg.game_folders = [f for f in cfg.game_folders if isinstance(f, dict) and f.get("path")]
        cfg.posed_previews = bool(cfg.posed_previews)
        # what Finish, Cut and the painted 3MF kept (moved to Model Painter in 0.55.0): ignored, and gone once saved
        cfg.prefs = retired.prefs(cfg.prefs)
        last = cfg.last_enhance if isinstance(cfg.last_enhance, dict) else {}
        cfg.last_enhance = {str(game): retired.settings(s) for game, s in last.items() if isinstance(s, dict)}
        cfg.path = path
        return cfg

    def save(self) -> None:
        if self.path is None:
            return
        with _write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v for k, v in asdict(self).items() if k != "path"}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            os.replace(tmp, self.path)

    @property
    def export_folder(self) -> Path:
        return Path(self.export_dir) if self.export_dir else default_export_dir()

    @property
    def library_folder(self) -> Path:
        return Path(self.library_dir) if self.library_dir else config_dir() / "library"


_write_lock = threading.Lock()
