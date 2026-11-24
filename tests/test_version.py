"""Version is 2.8.5 and stays in one place."""

from __future__ import annotations

import tomllib
from pathlib import Path

from cogs.utils import constants

ROOT = Path(__file__).resolve().parents[1]
VERSION_ATTR = "cogs.utils.constants.VERSION"


def test_version_constant_is_2_8_5():
    assert constants.VERSION == "2.8.5"
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
    music = (ROOT / "cogs" / "music.py").read_text(encoding="utf-8")
    game = (ROOT / "cogs" / "gametime.py").read_text(encoding="utf-8")
    image = (ROOT / "cogs" / "imagediffusion.py").read_text(encoding="utf-8")
    assert "async def status" in source
    assert "async def about" not in source
    assert "async def stats(" not in source
    assert "async def uptime(" not in source
    assert "async def reload_music" in source
    assert "@commands.is_owner()" in source
    assert 'f"**Version:** PooperScooper {VERSION}"' in source
    assert 'embed.set_footer(text=f"PooperScooper {VERSION}")' in source
    assert "async def join" not in music
    assert "async def count" not in music
    assert "async def played_legacy" not in game
    assert "async def save_legacy" not in game
    assert "async def comfy_status" not in image


def test_status_reports_health_and_hides_owner_comfy_details():
    import asyncio
    from unittest.mock import AsyncMock

    from cogs.general import General

    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    sent = {}

    async def send(embed):
        sent["embed"] = embed

    ctx = type("Ctx", (), {})()
    ctx.typing = lambda: _Typing()
    ctx.send = send
    ctx.author = type("Author", (), {"id": 1})()
    bot = type("Bot", (), {})()
    bot.guilds = [1, 2]
    bot.users = [1, 2, 3]
    bot.commands_executed = 4
    bot.cogs = {"General": object()}
    bot.uptime = __import__("discord").utils.utcnow()
    bot.lavalink_settings = None
    bot.get_cog = lambda name: None
    bot.is_owner = AsyncMock(return_value=False)
    asyncio.run(General(bot).status.callback(General(bot), ctx))
    description = sent["embed"].description
    assert "Uptime" in description
    assert "PooperScooper" in description
    assert "Members" in description
    assert "ComfyUI root" not in description
    assert "ComfyUI PID" not in description
    assert sent["embed"].footer.text.startswith("PooperScooper ")
