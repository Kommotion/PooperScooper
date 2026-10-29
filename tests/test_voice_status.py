"""Voice-channel now-playing status. Fully mocked: no gateway and no HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from cogs.utils.voice_status import (
    CHANNEL_INFO,
    DEFAULT_VOICE_STATUS_PATH,
    VOICE_CHANNEL_STATUS_UPDATE,
    VOICE_STATUS_LIMIT,
    VoiceStatusManager,
    format_now_playing,
)

NOTE = "\N{MUSICAL NOTE}"
DASH = "\N{EM DASH}"


class Track:
    def __init__(self, title, author="M83"):
        self.title = title
        self.author = author


class _Perms:
    def __init__(self, allowed=True):
        self.set_voice_channel_status = allowed


class Channel:
    def __init__(self, channel_id, guild, *, allowed=True):
        self.id = channel_id
        self.guild = guild
        self.allowed = allowed
        self.edits = []
        self.fail = None

    def permissions_for(self, member):
        return _Perms(self.allowed)

    async def edit(self, *, status=None):
        if self.fail is not None:
            raise self.fail
        self.edits.append(status)


class Socket:
    def __init__(self, on_send=None):
        self.sent = []
        self.on_send = on_send

    async def send_as_json(self, payload):
        self.sent.append(payload)
        if self.on_send is not None:
            self.on_send(payload)


class _Bot:
    def __init__(self, ws=None):
        self._connection = SimpleNamespace(parsers={})
        self.ws = ws if ws is not None else Socket()

    def get_shard(self, shard_id):
        return SimpleNamespace(ws=self.ws)


def _guild(guild_id=1, shard_id=0):
    return SimpleNamespace(id=guild_id, shard_id=shard_id, me=SimpleNamespace(id=99))


@pytest.fixture(autouse=True)
def _isolated_voice_status_file(tmp_path, monkeypatch):
    """Keep unit tests from reading or writing the real data/voice_status.json."""
    monkeypatch.setattr(
        "cogs.utils.voice_status.DEFAULT_VOICE_STATUS_PATH",
        tmp_path / "voice_status.json",
    )


def _pair(*, timeout=0.05, status=None, allowed=True, on_send=None, path=None):
    """A manager whose CHANNEL_INFO answer is whatever ``status`` is.

    ``status`` left as the default sentinel means Discord never answers.
    """
    channel = Channel(10, _guild(), allowed=allowed)
    holder = {}

    def answer(payload):
        if status is _NO_ANSWER:
            return
        holder["manager"]._on_channel_info(
            {"guild_id": "1", "channels": [{"id": "10", "status": status}]}
        )

    socket = Socket(on_send if on_send is not None else answer)
    bot = _Bot(socket)
    manager = VoiceStatusManager(bot, info_timeout=timeout, path=path)
    manager.install()
    holder["manager"] = manager
    return manager, channel, socket


_NO_ANSWER = object()


def _warnings(caplog):
    return [record for record in caplog.records if record.name == "cogs.utils.voice_status"]


def test_empty_status_is_replaced_with_the_track():
    async def scenario(raw_status):
        manager, channel, socket = _pair(status=raw_status)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        assert channel.edits == [f"{NOTE} Midnight City {DASH} M83"]
        assert manager.bot_text[10] == channel.edits[0]
        assert manager.known_status[10] == channel.edits[0]
        assert socket.sent[0] == {"op": 43, "d": {"guild_id": "1", "fields": ["status"]}}

    asyncio.run(scenario(None))
    asyncio.run(scenario(""))


def test_user_status_is_left_alone():
    async def scenario():
        manager, channel, _socket = _pair(status="listening party")
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] == "listening party"

    asyncio.run(scenario())


def test_unknown_status_is_skipped():
    async def scenario():
        manager, channel, socket = _pair(status=_NO_ANSWER, timeout=0.05)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        assert channel.edits == []
        assert 10 not in manager.known_status
        assert 10 not in manager.bot_text
        assert socket.sent[0]["op"] == 43

    asyncio.run(scenario())


def test_bot_owned_status_is_replaced_when_the_track_changes():
    async def scenario():
        manager, channel, _socket = _pair(status=None)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        await manager.set_now_playing(channel, Track("Outro", "M83"))
        assert channel.edits == [
            f"{NOTE} Midnight City {DASH} M83",
            f"{NOTE} Outro {DASH} M83",
        ]
        assert manager.bot_text[10] == channel.edits[1]
        assert manager.known_status[10] == channel.edits[1]

    asyncio.run(scenario())


def test_bot_owned_status_is_cleared_on_stop():
    async def scenario():
        manager, channel, _socket = _pair(status=None)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        await manager.clear(channel)
        assert channel.edits == [f"{NOTE} Midnight City {DASH} M83", None]
        assert 10 not in manager.bot_text
        assert manager.known_status[10] is None

    asyncio.run(scenario())


def test_mismatched_status_is_left_alone_on_replace_and_clear():
    async def scenario():
        manager, channel, _socket = _pair(status=None)
        owned = f"{NOTE} Midnight City {DASH} M83"
        manager.known_status[10] = "brb"
        manager.bot_text[10] = owned

        await manager.set_now_playing(channel, Track("Outro", "M83"))
        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] == "brb"

        manager.bot_text[10] = owned
        await manager.clear(channel)
        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] == "brb"

    asyncio.run(scenario())


def test_missing_permission_skips_and_logs_once(caplog):
    caplog.set_level(logging.WARNING, logger="cogs.utils.voice_status")

    async def scenario():
        manager, channel, _socket = _pair(status=None, allowed=False)
        track = Track("Midnight City", "M83")
        await manager.set_now_playing(channel, track)
        await manager.set_now_playing(channel, track)
        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.permission_errors_logged == {10}

    asyncio.run(scenario())
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "10" in warnings[0].getMessage()
    assert "Set Voice Channel Status" in warnings[0].getMessage()


def test_forbidden_edit_skips(caplog):
    caplog.set_level(logging.WARNING, logger="cogs.utils.voice_status")

    async def scenario():
        manager, channel, _socket = _pair(status=None)
        channel.fail = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "Missing Permissions",
        )
        track = Track("Midnight City", "M83")
        await manager.set_now_playing(channel, track)
        await manager.set_now_playing(channel, track)
        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] is None
        assert manager.permission_errors_logged == {10}

    asyncio.run(scenario())
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "Could not edit voice status" in warnings[0].getMessage()


def test_status_format_and_500_character_limit():
    assert format_now_playing(Track("Midnight City", "M83")) == f"{NOTE} Midnight City {DASH} M83"
    assert format_now_playing(Track("Midnight City", None)) == f"{NOTE} Midnight City"
    assert format_now_playing(Track("Midnight City", "")) == f"{NOTE} Midnight City"
    assert DASH not in format_now_playing(Track("Midnight City", None))

    long_title = "T" * 600
    truncated = format_now_playing(Track(long_title, None))
    assert len(truncated) == VOICE_STATUS_LIMIT
    assert truncated == f"{NOTE} {long_title}"[:VOICE_STATUS_LIMIT]

    title = "T" * 400
    author = "A" * 200
    full = f"{NOTE} {title} {DASH} {author}"
    assert len(full) > VOICE_STATUS_LIMIT
    assert format_now_playing(Track(title, author)) == full[:VOICE_STATUS_LIMIT]

    async def scenario():
        manager, channel, _socket = _pair(status=None)
        await manager.set_now_playing(channel, Track(long_title, None))
        assert channel.edits == [truncated]

    asyncio.run(scenario())


def test_gateway_parsers_track_status_and_channel_info_unblocks_the_wait():
    async def scenario():
        channel = Channel(10, _guild())
        socket = Socket()
        bot = _Bot(socket)
        manager = VoiceStatusManager(bot, info_timeout=1.0)
        manager.install()
        assert bot._connection.parsers[VOICE_CHANNEL_STATUS_UPDATE] == manager._on_voice_channel_status_update
        assert bot._connection.parsers[CHANNEL_INFO] == manager._on_channel_info

        waiting = asyncio.create_task(manager.ensure_known(channel))
        for _ in range(5):
            if socket.sent:
                break
            await asyncio.sleep(0)
        assert socket.sent == [{"op": 43, "d": {"guild_id": "1", "fields": ["status"]}}]
        manager._on_channel_info(
            {
                "guild_id": "1",
                "channels": [
                    {"id": "10", "status": ""},
                    {"id": "12"},
                ],
            }
        )
        await asyncio.wait_for(waiting, timeout=1)
        assert manager.known_status[10] is None
        assert manager.known_status[12] is None

        owned = f"{NOTE} Old {DASH} Me"
        manager.bot_text[10] = owned
        manager._on_voice_channel_status_update({"id": "10", "guild_id": "1", "status": owned})
        assert manager.known_status[10] == owned
        assert manager.bot_text[10] == owned

        manager._on_voice_channel_status_update({"id": "10", "guild_id": "1", "status": "hijacked"})
        assert manager.known_status[10] == "hijacked"
        assert 10 not in manager.bot_text

        manager.uninstall()
        assert VOICE_CHANNEL_STATUS_UPDATE not in bot._connection.parsers
        assert CHANNEL_INFO not in bot._connection.parsers

    asyncio.run(scenario())


def test_status_request_uses_the_shard_socket_then_the_bot_socket():
    async def scenario():
        parent_socket = Socket()
        decoy = Socket()

        class _ShardInfo:
            def __init__(self):
                self._parent = SimpleNamespace(ws=parent_socket)

        class _ShardedBot:
            def __init__(self):
                self._connection = SimpleNamespace(parsers={})
                self.ws = decoy

            def get_shard(self, shard_id):
                return _ShardInfo()

        channel = Channel(10, _guild())
        manager = VoiceStatusManager(_ShardedBot(), info_timeout=0.05)
        await manager.ensure_known(channel)
        assert parent_socket.sent[0]["op"] == 43
        assert decoy.sent == []

        fallback = Socket()

        class _PlainBot:
            def __init__(self):
                self._connection = SimpleNamespace(parsers={})
                self.ws = fallback

            def get_shard(self, shard_id):
                return SimpleNamespace()

        other = Channel(11, _guild())
        fallback_manager = VoiceStatusManager(_PlainBot(), info_timeout=0.05)
        await fallback_manager.ensure_known(other)
        assert fallback.sent[0]["d"]["guild_id"] == "1"

    asyncio.run(scenario())


def _write_saved(path, saved):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({str(channel_id): text for channel_id, text in saved.items()}), encoding="utf-8")


def _read_saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _manager_with_channels(path, live_status, *, channel_ids, playing=()):
    """A manager that can see ``channel_ids`` and learns ``live_status`` from CHANNEL_INFO."""
    guild = _guild()
    channels = {channel_id: Channel(channel_id, guild) for channel_id in channel_ids}
    holder = {}

    def answer(payload):
        reported = []
        for channel_id, status in live_status.items():
            channel = channels.get(channel_id)
            # A later snapshot should show a status this test already cleared.
            if channel is not None and channel.edits:
                status = channel.edits[-1]
            reported.append({"id": str(channel_id), "status": status})
        holder["manager"]._on_channel_info({"guild_id": "1", "channels": reported})

    bot = _Bot(Socket(answer))
    bot.get_channel = lambda channel_id: channels.get(channel_id)
    playing_ids = set(playing)
    manager = VoiceStatusManager(
        bot,
        info_timeout=0.05,
        path=path,
        is_playing=lambda channel_id: channel_id in playing_ids,
    )
    manager.install()
    holder["manager"] = manager
    return manager, channels


def test_default_status_path_is_under_data():
    assert DEFAULT_VOICE_STATUS_PATH.name == "voice_status.json"
    assert DEFAULT_VOICE_STATUS_PATH.parent.name == "data"


def test_set_saves_the_exact_status_text(tmp_path, monkeypatch):
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr("cogs.utils.voice_status.os.replace", spy)
    path = tmp_path / "data" / "voice_status.json"

    async def scenario():
        manager, channel, _socket = _pair(status=None, path=path)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        text = f"{NOTE} Midnight City {DASH} M83"
        assert _read_saved(path) == {"10": text}
        await manager.set_now_playing(channel, Track("Outro", "M83"))
        updated = f"{NOTE} Outro {DASH} M83"
        assert _read_saved(path) == {"10": updated}
        assert manager.bot_text[10] == updated
        assert not (path.parent / (path.name + ".tmp")).exists()

    asyncio.run(scenario())
    assert replaced
    source, destination = replaced[-1]
    assert source.name == "voice_status.json.tmp"
    assert destination == path


def test_clear_removes_the_saved_status(tmp_path):
    async def scenario():
        path = tmp_path / "voice_status.json"
        manager, channel, _socket = _pair(status=None, path=path)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        await manager.clear(channel)
        assert _read_saved(path) == {}
        assert 10 not in manager.bot_text
        assert manager.known_status[10] is None

    asyncio.run(scenario())


def test_reloaded_manager_keeps_ownership(tmp_path):
    async def scenario():
        path = tmp_path / "voice_status.json"
        manager, channel, _socket = _pair(status=None, path=path)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        text = channel.edits[0]

        reloaded, again, _socket = _pair(status=text, path=path)
        assert reloaded is not manager
        assert reloaded.bot_text[10] == text
        await reloaded.set_now_playing(again, Track("Outro", "M83"))
        assert again.edits == [f"{NOTE} Outro {DASH} M83"]
        assert _read_saved(path) == {"10": again.edits[0]}

    asyncio.run(scenario())


def test_corrupt_or_missing_status_file_starts_empty(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="cogs.utils.voice_status")
    path = tmp_path / "voice_status.json"
    path.write_text("{not json", encoding="utf-8")
    broken = VoiceStatusManager(_Bot(), path=path)
    assert broken.bot_text == {}

    path.write_text("[]", encoding="utf-8")
    shaped = VoiceStatusManager(_Bot(), path=path)
    assert shaped.bot_text == {}

    missing = VoiceStatusManager(_Bot(), path=tmp_path / "missing.json")
    assert missing.bot_text == {}

    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert all("starting with none" in record.getMessage() for record in warnings)


def test_startup_cleanup_clears_only_matching_statuses(tmp_path):
    async def scenario():
        path = tmp_path / "voice_status.json"
        matching = f"{NOTE} Midnight City {DASH} M83"
        unknown_text = f"{NOTE} Hidden {DASH} M83"
        playing_text = f"{NOTE} Live {DASH} M83"
        stale = f"{NOTE} Old {DASH} M83"
        _write_saved(
            path,
            {10: matching, 11: unknown_text, 12: playing_text, 13: stale},
        )
        manager, channels = _manager_with_channels(
            path,
            {10: matching, 12: playing_text, 13: "listening party"},
            channel_ids=(10, 11, 12, 13),
            playing=(12,),
        )
        assert manager.bot_text[10] == matching

        await manager.clear_stale_after_restart()

        assert channels[10].edits == [None]
        assert channels[11].edits == []
        assert channels[12].edits == []
        assert channels[13].edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] is None
        assert manager.bot_text[11] == unknown_text
        assert 11 not in manager.known_status
        assert manager.bot_text[12] == playing_text
        assert 13 not in manager.bot_text
        assert manager.known_status[13] == "listening party"
        assert _read_saved(path) == {"11": unknown_text, "12": playing_text}

    asyncio.run(scenario())


def test_user_changed_status_is_left_alone_and_forgotten(tmp_path):
    async def scenario():
        path = tmp_path / "voice_status.json"
        saved = f"{NOTE} Midnight City {DASH} M83"
        _write_saved(path, {10: saved})
        manager, channel, _socket = _pair(status="listening party", path=path)
        assert manager.bot_text[10] == saved

        await manager.set_now_playing(channel, Track("Outro", "M83"))

        assert channel.edits == []
        assert 10 not in manager.bot_text
        assert manager.known_status[10] == "listening party"
        assert _read_saved(path) == {}

    asyncio.run(scenario())


def test_status_file_errors_do_not_crash(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="cogs.utils.voice_status")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("cogs.utils.voice_status.os.replace", boom)

    async def scenario():
        path = tmp_path / "voice_status.json"
        manager, channel, _socket = _pair(status=None, path=path)
        await manager.set_now_playing(channel, Track("Midnight City", "M83"))
        assert manager.bot_text[10] == channel.edits[0]
        assert channel.edits == [f"{NOTE} Midnight City {DASH} M83"]

    asyncio.run(scenario())
    warnings = _warnings(caplog)
    assert any("Could not save voice statuses" in record.getMessage() for record in warnings)
