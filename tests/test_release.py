"""What the Release workflow relies on: one version in both places, with notes for it."""

import re
from pathlib import Path

import darkstl

ROOT = Path(__file__).resolve().parents[1]


def test_one_version_with_its_release_notes():
    version = re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(), re.MULTILINE).group(1)
    assert darkstl.__version__ == version
    assert (ROOT / "docs" / "releases" / f"v{version}.md").is_file()
