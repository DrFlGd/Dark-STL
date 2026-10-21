"""What the Release workflow relies on: one version in the package and the desktop app, with
notes for it, and the app's engine found where its bundle puts it."""

import json
import re
from pathlib import Path

import darkstl

ROOT = Path(__file__).resolve().parents[1]


def version_in(path: str) -> str:
    return re.search(r'^version = "(.+)"$', (ROOT / path).read_text(), re.MULTILINE).group(1)


def test_one_version_with_its_release_notes():
    version = version_in("pyproject.toml")
    assert darkstl.__version__ == version
    assert version_in("desktop/Cargo.toml") == version  # the app's version, and its installer's name
    lock = (ROOT / "desktop" / "Cargo.lock").read_text()
    assert f'name = "dark-stl"\nversion = "{version}"' in lock
    assert (ROOT / "docs" / "releases" / f"v{version}.md").is_file()


def test_the_app_bundles_the_engine_and_the_page():
    conf = json.loads((ROOT / "desktop" / "tauri.conf.json").read_text())
    assert conf["bundle"]["resources"] == {"../build/engine": "engine"}  # desktop/engine/engine.spec's output
    assert (ROOT / "desktop" / conf["build"]["frontendDist"] / "platform-desktop.js").is_file()
    main = (ROOT / "desktop" / "src" / "main.rs").read_text()
    assert '.join("engine")' in main and '"darkstl.exe"' in main
