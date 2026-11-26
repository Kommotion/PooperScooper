"""Version is 2.8.8 and stays in one place."""

from __future__ import annotations

import tomllib
from pathlib import Path

from cogs.utils import constants

ROOT = Path(__file__).resolve().parents[1]
VERSION_ATTR = "cogs.utils.constants.VERSION"


def test_version_constant_is_2_8_8():
    assert constants.VERSION == "2.8.8"
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
    assert "async def about" in source
    assert '@commands.group(name="status", invoke_without_command=True)' in source
    assert "async def status_admin" in source
    assert '@status.command(name="admin")' in source
    assert "async def stats(" not in source
    assert "async def uptime(" not in source
    assert "async def reload_music" in source
    assert "@commands.is_owner()" in source
    assert 'embed.set_footer(text=f"PooperScooper v{VERSION}")' in source
    assert 'embed.set_footer(text=f"PooperScooper v{VERSION} • Admin Diagnostics")' in source
    assert "async def join" not in music
    assert "async def count" not in music
    assert "async def played_legacy" not in game
    assert "async def save_legacy" not in game
    assert "async def comfy_status" not in image


class _Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


def _capture_ctx():
    sent = {}

    async def send(*args, **kwargs):
        sent["args"] = args
        sent["kwargs"] = kwargs
        if args:
            sent["embed"] = args[0]
        if "embed" in kwargs:
            sent["embed"] = kwargs["embed"]
        if "files" in kwargs:
            sent["files"] = kwargs["files"]

    ctx = type("Ctx", (), {})()
    ctx.typing = lambda: _Typing()
    ctx.send = send
    ctx.author = type("Author", (), {"id": 1})()
    return ctx, sent


def _embed_text(embed) -> str:
    parts = [
        embed.title or "",
        embed.description or "",
        getattr(getattr(embed, "footer", None), "text", "") or "",
    ]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(str(field.value or ""))
    return "\n".join(str(part) for part in parts)


def _status_bot(image_cog):
    from unittest.mock import AsyncMock

    bot = type("Bot", (), {})()
    bot.guilds = [1, 2]
    bot.users = [1, 2, 3]
    bot.commands_executed = 4
    bot.cogs = {"General": object()}
    bot.uptime = __import__("discord").utils.utcnow()
    bot.user = None
    bot.lavalink_settings = None
    bot.lavalink_server = None
    bot.get_cog = lambda name: image_cog if name == "ImageDiffusion" else None
    bot.is_owner = AsyncMock(return_value=True)
    return bot


def test_status_reports_health_and_hides_owner_comfy_details():
    import asyncio
    from unittest.mock import patch

    from cogs.general import General
    from cogs.utils.constants import VERSION

    class _Queue:
        def qsize(self):
            return 2

        maxsize = 8

    image_cog = type("Image", (), {})()
    image_cog.image_queue = _Queue()
    image_cog.comfy_startup_error = r"boom C:\ComfyUI\main.py"
    comfy = {
        "running": False,
        "ready": False,
        "source": "local",
        "host": "127.0.0.1",
        "port": 8188,
        "root": r"C:\ComfyUI",
        "pid": 4242,
        "bot_owns_process": True,
        "last_health_check": 1700000000,
    }
    ctx, sent = _capture_ctx()
    bot = _status_bot(image_cog)
    with patch("cogs.utils.comfy_client.get_comfy_status", return_value=comfy):
        asyncio.run(General(bot).status.callback(General(bot), ctx))
    embed = sent["embed"]
    text = _embed_text(embed)
    lowered = text.lower()
    for banned in (
        "pid",
        "root",
        "source",
        "host",
        "port",
        "127.0.0.1",
        "8188",
        "c:\\",
        "bot_owns_process",
        "last_health_check",
        "ready",
    ):
        assert banned not in lowered
    assert embed.description is not None
    assert embed.description.startswith("🟡 **Some services degraded**\n")
    assert f"v{VERSION}  •  Up " in embed.description
    assert "hours" in embed.description
    assert "[GitHub](https://github.com/Kommotion/PooperScooper)" in embed.description
    assert VERSION in text
    assert "members" in lowered
    assert embed.footer.text == f"PooperScooper v{VERSION}"
    assert "Admin Diagnostics" not in (embed.footer.text or "")
    assert embed.timestamp is not None
    assert embed.colour == __import__("discord").Colour.from_rgb(243, 156, 18)
    assert [field.name for field in embed.fields] == ["📊 Overview", "⚙️ Services"]
    assert all(field.inline is False for field in embed.fields)
    services = embed.fields[1].value
    assert "Image Gen" in services
    assert "🔴 **Image Gen** — Offline" in services
    assert "127.0.0.1" not in services
    assert "8188" not in services


def test_about_sends_embed_and_image_files():
    import asyncio

    from cogs.general import General
    from cogs.utils.constants import BOT_AUTHOR, VERSION

    ctx, sent = _capture_ctx()
    bot = type("Bot", (), {})()
    asyncio.run(General(bot).about.callback(General(bot), ctx))
    embed = sent["kwargs"]["embed"]
    files = sent["kwargs"]["files"]
    try:
        assert embed.title == "PooperScooper"
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Version"] == "PooperScooper " + VERSION
        assert fields["Author"] == BOT_AUTHOR
        assert fields["Github Link"] == "https://github.com/Kommotion/PooperScooper"
        assert "Discord.py" in fields["Library"]
        assert fields["Language"] == "Python"
        assert embed.footer.text == (
            "In memory of all the good boys and girls - Junior, Cole, Shirley, and Mocha."
        )
        assert [item.filename for item in files] == ["pooper.png", "thumb.png", "image.png"]
    finally:
        for item in files:
            item.close()


def test_status_admin_requires_admin_check_and_shows_diagnostics():
    import asyncio
    from unittest.mock import patch

    from cogs.general import General
    from cogs.utils.constants import VERSION

    admin_cmd = General.status_admin
    assert admin_cmd.name == "admin"
    assert any(
        "is_admin_or_illuminaughty" in (getattr(check, "__qualname__", "") or "")
        or "is_admin_or_illuminaughty" in (getattr(check, "__name__", "") or "")
        for check in admin_cmd.checks
    )

    class _Queue:
        def qsize(self):
            return 1

        maxsize = 4

    image_cog = type("Image", (), {})()
    image_cog.image_queue = _Queue()
    image_cog.comfy_startup_error = r"startup failed C:\ComfyUI\main.py"

    def _run(comfy):
        ctx, sent = _capture_ctx()
        bot = _status_bot(image_cog)
        with patch("cogs.utils.comfy_client.get_comfy_status", return_value=comfy):
            asyncio.run(General(bot).status_admin.callback(General(bot), ctx))
        return sent["embed"]

    stamp = 1700000000
    unhealthy = {
        "running": False,
        "ready": False,
        "source": "local",
        "host": "127.0.0.1",
        "port": 8188,
        "root": r"C:\ComfyUI",
        "pid": 4242,
        "bot_owns_process": True,
        "last_health_check": stamp,
    }
    embed = _run(unhealthy)
    text = _embed_text(embed)
    assert "127.0.0.1:8188" in text
    assert "127.0.0.1" in text
    assert "8188" in text
    assert r"C:\ComfyUI" in text
    assert "4242" in text
    assert "local" in text
    assert "Ready: `No`" in text
    assert "Bot-managed: `Yes`" in text
    assert f"<t:{int(stamp)}:R>" in text
    assert "Last check: <t:" in text
    assert r"C:\ComfyUI\main.py" in text
    assert "bot_owns_process" not in text
    assert "last_health_check" not in text
    assert [field.name for field in embed.fields] == [
        "🎨 ComfyUI",
        "⚠️ ComfyUI Startup Error",
        "🎵 Lavalink & Wavelink",
        "🦖 Palworld & Bot",
    ]
    assert all(field.inline is False for field in embed.fields)
    error_field = embed.fields[1]
    assert error_field.value.startswith("```")
    assert r"C:\ComfyUI\main.py" in error_field.value
    assert len(error_field.value) <= 1024
    assert embed.fields[2].value == "⚪ Disabled"
    assert embed.footer.text == f"PooperScooper v{VERSION} • Admin Diagnostics"
    assert embed.footer.text.endswith("Admin Diagnostics")
    assert embed.timestamp is not None
    assert embed.colour == __import__("discord").Colour.from_rgb(88, 101, 242)
    assert embed.description is not None
    assert embed.description.startswith(f"v{VERSION}  •  Up ")
    assert "server config" in embed.description

    healthy = dict(unhealthy)
    healthy["running"] = True
    healthy["ready"] = True
    healthy["last_health_check"] = None
    image_cog.comfy_startup_error = None
    healthy_embed = _run(healthy)
    healthy_text = _embed_text(healthy_embed)
    assert "Last check: n/a" in healthy_text
    assert "Ready: `Yes`" in healthy_text
    assert "Bot-managed: `Yes`" in healthy_text
    assert "127.0.0.1:8188" in healthy_text
    assert r"C:\ComfyUI" in healthy_text
    assert "4242" in healthy_text
    assert "local" in healthy_text
    assert "bot_owns_process" not in healthy_text
    assert "last_health_check" not in healthy_text
    assert [field.name for field in healthy_embed.fields] == [
        "🎨 ComfyUI",
        "🎵 Lavalink & Wavelink",
        "🦖 Palworld & Bot",
    ]
    assert healthy_embed.footer.text == f"PooperScooper v{VERSION} • Admin Diagnostics"
    assert healthy_embed.footer.text.endswith("Admin Diagnostics")
    assert healthy_embed.colour == __import__("discord").Colour.from_rgb(88, 101, 242)
