"""Model Painter (github.com/DrFlGd/Model-Painter), the app models go to for painting and print finishing:
where it is, and opening a model in it.

The Export tab's *For Model Painter* writes a GLB (textures and materials, Y up) and *Open in Model Painter*
starts Model Painter with the file, which it opens as the model it was started with. Where the program is
comes from Settings, or is found by itself: the installer's folder (for the user, or for everyone), or the
portable app's folder beside Dark STL's own or in Downloads.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NAMES = ("Model Painter.exe", "model-painter.exe")  # the installed app's name, then cargo's own
FOLDERS = ("Model Painter", "ModelPainter")


def candidates() -> list[Path]:
    """Where Model Painter may be, the likeliest first."""
    out: list[Path] = []
    env = lambda k: os.environ.get(k) or ""  # noqa: E731
    roots: list[Path] = []
    if env("LOCALAPPDATA"):  # Windows: the installer puts it for the user under one of these
        local = Path(env("LOCALAPPDATA"))
        roots += [local / "Programs" / "Model Painter", local / "Model Painter"]
    for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if env(k):
            roots.append(Path(env(k)) / "Model Painter")
    for root in roots:
        out += [root / n for n in NAMES]
    # the portable app (a "Model Painter" folder, as its zip unpacks), beside Dark STL's own folder or in Downloads
    near: list[Path] = []
    if getattr(sys, "frozen", False):  # the app's engine: <app>\engine\darkstl.exe
        app = Path(sys.executable).resolve().parent.parent
        near += [app.parent, app]
    near.append(Path.home() / "Downloads")
    for folder in near:
        out += portable_in(folder)
    if sys.platform == "darwin":
        out += [Path("/Applications/Model Painter.app"), Path.home() / "Applications" / "Model Painter.app"]
    return out


def portable_in(folder: Path) -> list[Path]:
    """The portable Model Painter's program in ``folder``: unzipped there as it is, or into a folder named after
    the zip."""
    out = [folder / f / n for f in FOLDERS for n in NAMES]
    try:
        unzipped = sorted((p for p in folder.glob("ModelPainter*") if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        unzipped = []
    for d in unzipped:  # ModelPainter_0.2.0_x64-portable\Model Painter\Model Painter.exe, the newest first
        out += [d / f / n for f in FOLDERS for n in NAMES] + [d / n for n in NAMES]
    return out


def found(path: Path) -> bool:
    return path.is_file() or (path.suffix == ".app" and path.is_dir())


def find(configured: str = "") -> Path | None:
    """Model Painter's program: the one set in Settings when it's there, else the first found where it's put."""
    if configured:
        p = Path(configured.strip().strip('"'))
        return p if found(p) else None
    for p in candidates():
        try:
            if found(p):
                return p
        except OSError:
            continue
    return None


def launch(program: str | Path, path: str | Path) -> None:
    """Start ``program`` with ``path`` on its own: it doesn't hold up Dark STL or close with it."""
    program, path = str(program), str(path)
    if program.endswith(".app"):  # a macOS app bundle
        subprocess.Popen(["open", "-a", program, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return
    extra: dict = {}
    if sys.platform == "win32":
        extra["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extra["start_new_session"] = True
    subprocess.Popen([program, path], cwd=str(Path(program).parent), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **extra)
