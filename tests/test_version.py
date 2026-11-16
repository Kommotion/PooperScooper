"""Version is 2.7.1 and stays in one place."""

from __future__ import annotations

import tomllib
from pathlib import Path

from cogs.utils import constants

ROOT = Path(__file__).resolve().parents[1]
VERSION_ATTR = "cogs.utils.constants.VERSION"


def test_version_constant_is_2_7_1():
    assert constants.VERSION == "2.7.1"
    assert constants.__version__ == constants.VERSION


def test_pyproject_version_tracks_constant():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    if "version" in project:
        assert project["version"] == constants.VERSION
        return

    assert "version" in project.get("dynamic", [])
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == VERSION_ATTR


def test_about_and_status_display_version_constant():
    source = (ROOT / "cogs" / "general.py").read_text(encoding="utf-8")
    assert "async def about" in source
    assert "async def status" in source
    assert "name = 'Version'" in source
    assert "value = 'PooperScooper ' + VERSION" in source
    assert 'embed.set_footer(text=f"PooperScooper {VERSION}")' in source
