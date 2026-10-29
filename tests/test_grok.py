"""Owner-only /grok webhook command."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from cogs.grok import Grok, _PUBLIC_FAILURE, app_command_is_owner
from cogs.utils.config import BotConfig

CREATED_AT = datetime(2026, 10, 14, 15, 4, 5, tzinfo=timezone.utc)
WEBHOOK_URL = "https://example.com/grok-webhook"
WEBHOOK_KEY = "test-key"


@pytest.fixture(autouse=True)
def _isolate_grok_refs(tmp_path, monkeypatch):
    """Successful /grok writes a ref file. Keep that out of data/."""
    monkeypatch.setattr("cogs.grok.GROK_REFS_PATH", tmp_path / "grok_refs.json")


def _config(**overrides) -> BotConfig:
    data = {
        "token": "test-token",
        "client_id": "123456789012345678",
        "grok_webhook_url": WEBHOOK_URL,
        "grok_webhook_key": WEBHOOK_KEY,
    }
    data.update(overrides)
    return BotConfig.model_validate(data)


def _interaction(*, guild_id=4242, guild=None, channel=None):
    return SimpleNamespace(
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
            is_done=lambda: False,
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id=111, name="pooper"),
        channel_id=222,
        guild_id=guild_id,
        guild=guild,
        channel=channel,
        application_id=333,
        id=4444,
        token="interaction-token",
        created_at=CREATED_AT,
        client=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
        delete_original_response=AsyncMock(),
        edit_original_response=AsyncMock(),
    )


class _Response:
    def __init__(self, status: int, body: str = ""):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def read(self) -> bytes:
        return self._body.encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_session(monkeypatch, *, status=200, body="", error=None, on_post=None):
    sessions = []

    class Session:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.posts = []
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            if on_post is not None:
                on_post()
            if error is not None:
                raise error
            return _Response(status, body)

    monkeypatch.setattr("cogs.grok.aiohttp.ClientSession", Session)
    return sessions


def _use_config(monkeypatch, config: BotConfig) -> None:
    monkeypatch.setattr("cogs.grok.load_config", lambda: config)


async def _invoke(cog: Grok, interaction, prompt: str = "hello from discord", attachment=None) -> None:
    await cog.grok.callback(cog, interaction, prompt, attachment)


def _file(*, filename, url, content_type, size):
    return SimpleNamespace(filename=filename, url=url, content_type=content_type, size=size)


def _chat_message(
    *,
    message_id,
    author_name,
    author_id,
    created_at,
    content,
    attachments=None,
    reply_to_id=None,
    jump_url=None,
    embeds=None,
):
    reference = None if reply_to_id is None else SimpleNamespace(message_id=reply_to_id)
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(name=author_name, id=author_id),
        created_at=created_at,
        content=content,
        attachments=list(attachments or []),
        reference=reference,
        jump_url=jump_url or f"https://discord.com/channels/4242/222/{message_id}",
        embeds=list(embeds or []),
    )


def _history(messages, calls, *, error=None, fail_during_iteration=False):
    """Fake Discord history. `messages` is newest-first, like the default API.

    Yields at most `limit` messages. `oldest_first=True` walks from the oldest
    message instead, which is the discord.py behavior this cog avoids.
    """

    def history(*args, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error

        async def generate():
            if fail_during_iteration:
                raise RuntimeError("Missing Access")
            selected = list(messages)
            if kwargs.get("oldest_first") is True:
                selected.reverse()
            limit = kwargs.get("limit")
            if isinstance(limit, int):
                selected = selected[:limit]
            for message in selected:
                yield message

        return generate()

    return history


def _modal_text_input(modal):
    children = list(modal.children)
    assert len(children) == 1
    child = children[0]
    assert isinstance(child, discord.ui.TextInput)
    assert not any(isinstance(item, discord.ui.Label) for item in children)
    assert child._underlying.label == "What should I do with this?"
    return child


async def _open_ask_grok(cog: Grok, message):
    interaction = _interaction()
    await cog.ask_grok_menu._invoke(interaction, message)
    modal = interaction.response.send_modal.await_args.args[0]
    return interaction, modal


def _posted(sessions):
    assert len(sessions) == 1
    assert len(sessions[0].posts) == 1
    url, kwargs = sessions[0].posts[0]
    return sessions[0], url, kwargs


def test_grok_command_is_global_and_owner_only():
    cog = Grok(bot=None)
    assert cog.grok.name == "grok"
    assert cog.grok.guild_only is False
    assert cog.grok._guild_ids is None
    assert app_command_is_owner in cog.grok.checks
    assert cog.has_app_command_error_handler()
    prompt = cog.grok.get_parameter("prompt")
    assert prompt is not None
    assert prompt.required is True

    import PooperScooper

    assert "cogs.grok" in PooperScooper.initial_extensions


def test_grok_key_stays_out_of_legacy_credentials():
    config = _config(grok_webhook_key="super-secret-value")
    credentials = config.to_credentials_dict()
    assert "grok_webhook_key" not in credentials
    assert "super-secret-value" not in json.dumps(credentials)


def test_grok_success_posts_payload_and_leaves_response(monkeypatch):
    async def scenario(header_name: str):
        overrides = {}
        if header_name:
            overrides["grok_webhook_key_header"] = header_name
        _use_config(monkeypatch, _config(**overrides))
        sessions = _patch_session(monkeypatch, status=204, body="accepted")
        interaction = _interaction(guild_id=None)
        cog = Grok(bot=None)

        await _invoke(cog, interaction, "ship the feature")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        assert "ephemeral" not in interaction.response.defer.await_args.kwargs
        interaction.delete_original_response.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        interaction.response.send_message.assert_not_awaited()

        session, url, kwargs = _posted(sessions)
        assert url == WEBHOOK_URL
        assert session.kwargs["timeout"].total == 30
        assert kwargs["headers"] == {"Authorization": f"Bearer {WEBHOOK_KEY}"}
        assert kwargs["json"] == {
            "prompt": "ship the feature",
            "user_id": "111",
            "username": "pooper",
            "channel_id": "222",
            "guild_id": None,
            "guild_name": None,
            "channel_name": None,
            "application_id": "333",
            "interaction_id": "4444",
            "interaction_token": "interaction-token",
            "created_at": "2026-10-14T15:04:05+00:00",
            "attachments": [],
            "recent_messages": [],
        }
        assert WEBHOOK_KEY not in json.dumps(kwargs["json"])

    asyncio.run(scenario(""))
    asyncio.run(scenario("Authorization"))


def test_grok_records_interaction_ref(monkeypatch):
    async def scenario():
        _use_config(monkeypatch, _config())
        _patch_session(monkeypatch, status=204)
        interaction = _interaction()
        interaction.created_at = datetime.now(timezone.utc)
        await _invoke(Grok(bot=None), interaction, "remember this")

        from cogs import grok as grok_mod

        stored = json.loads(grok_mod.GROK_REFS_PATH.read_text(encoding="utf-8"))
        ref = stored["refs"]["4444"]
        assert ref["user_id"] == 111
        assert ref["channel_id"] == 222
        assert ref["guild_id"] == 4242
        assert ref["consumed"] is False

    asyncio.run(scenario())


def test_grok_ref_record_failure_leaves_the_response(monkeypatch, caplog):
    async def scenario():
        def boom(self, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("cogs.grok.GrokRefs.record", boom)
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        interaction = _interaction()
        with caplog.at_level("WARNING", logger="cogs.grok"):
            await _invoke(Grok(bot=None), interaction, "still delivered")

        assert any("Failed to record Grok reminder ref" in record.message for record in caplog.records)
        _session, url, _kwargs = _posted(sessions)
        assert url == WEBHOOK_URL
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.delete_original_response.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    asyncio.run(scenario())


def test_grok_records_ref_before_post_and_failed_post_keeps_it(monkeypatch):
    async def scenario():
        from cogs import grok as grok_mod

        _use_config(monkeypatch, _config())
        seen = {}

        def on_post():
            stored = json.loads(grok_mod.GROK_REFS_PATH.read_text(encoding="utf-8"))
            seen["during_post"] = stored["refs"]["4444"]

        _patch_session(monkeypatch, status=503, body="upstream down", on_post=on_post)
        interaction = _interaction()
        interaction.created_at = datetime.now(timezone.utc)
        await _invoke(Grok(bot=None), interaction, "remember this")

        assert seen["during_post"]["user_id"] == 111
        assert seen["during_post"]["channel_id"] == 222
        assert seen["during_post"]["guild_id"] == 4242
        assert seen["during_post"]["consumed"] is False
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.delete_original_response.assert_awaited_once_with()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "HTTP 503" in args[0]
        assert "upstream down" in args[0]
        stored = json.loads(grok_mod.GROK_REFS_PATH.read_text(encoding="utf-8"))
        assert stored["refs"]["4444"] == seen["during_post"]

    asyncio.run(scenario())


def test_grok_custom_header_sends_raw_key(monkeypatch):
    async def scenario():
        _use_config(monkeypatch, _config(grok_webhook_key_header="X-Grok-Key"))
        sessions = _patch_session(monkeypatch)
        interaction = _interaction(guild_id=4242)
        await _invoke(Grok(bot=None), interaction)

        _session, url, kwargs = _posted(sessions)
        assert url == WEBHOOK_URL
        assert kwargs["headers"] == {"X-Grok-Key": WEBHOOK_KEY}
        assert kwargs["json"]["guild_id"] == "4242"
        assert "Bearer" not in kwargs["headers"]["X-Grok-Key"]
        interaction.delete_original_response.assert_not_awaited()

    asyncio.run(scenario())


def test_grok_missing_config_reports_ephemeral_error(monkeypatch):
    async def scenario(overrides):
        _use_config(monkeypatch, _config(**overrides))
        sessions = _patch_session(monkeypatch)
        interaction = _interaction()
        await _invoke(Grok(bot=None), interaction, "unused")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        assert sessions == []
        interaction.delete_original_response.assert_awaited_once_with()
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "not configured" in args[0]
        assert WEBHOOK_KEY not in args[0]
        assert WEBHOOK_URL not in args[0]

    asyncio.run(scenario({"grok_webhook_url": "", "grok_webhook_key": ""}))
    asyncio.run(scenario({"grok_webhook_url": "  ", "grok_webhook_key": WEBHOOK_KEY}))
    asyncio.run(scenario({"grok_webhook_key": ""}))


def test_grok_post_failure_removes_deferred_response(monkeypatch):
    async def scenario():
        _use_config(monkeypatch, _config())
        _patch_session(monkeypatch, status=503, body="upstream down")
        interaction = _interaction()
        await _invoke(Grok(bot=None), interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.delete_original_response.assert_awaited_once_with()
        interaction.edit_original_response.assert_not_awaited()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "HTTP 503" in args[0]
        assert "upstream down" in args[0]
        assert WEBHOOK_KEY not in args[0]

    asyncio.run(scenario())


def test_grok_post_exception_removes_deferred_response(monkeypatch):
    async def scenario():
        _use_config(monkeypatch, _config())
        _patch_session(monkeypatch, error=ConnectionError(f"connection refused {WEBHOOK_KEY}"))
        interaction = _interaction()
        await _invoke(Grok(bot=None), interaction)

        interaction.delete_original_response.assert_awaited_once_with()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "connection refused" in args[0]
        assert WEBHOOK_KEY not in args[0]
        assert "***" in args[0]

    asyncio.run(scenario())


def test_grok_delete_failure_falls_back_to_edit(monkeypatch):
    async def scenario():
        _use_config(monkeypatch, _config())
        _patch_session(monkeypatch, status=500, body="nope")
        interaction = _interaction()
        interaction.delete_original_response.side_effect = RuntimeError("delete failed")
        await _invoke(Grok(bot=None), interaction)

        interaction.delete_original_response.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once_with(content=_PUBLIC_FAILURE)
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "HTTP 500" in args[0]
        assert "nope" in args[0]
        assert "nope" not in interaction.edit_original_response.await_args.kwargs["content"]

    asyncio.run(scenario())


def test_grok_non_owner_gets_ephemeral_error():
    async def scenario():
        cog = Grok(bot=None)
        denied = _interaction()
        denied.client.is_owner = AsyncMock(return_value=False)
        assert await app_command_is_owner(denied) is False

        await cog.cog_app_command_error(denied, app_commands.CheckFailure())
        denied.response.defer.assert_not_awaited()
        denied.response.send_message.assert_awaited_once()
        args, kwargs = denied.response.send_message.await_args
        assert kwargs["ephemeral"] is True
        assert "owner" in args[0].lower()
        denied.followup.send.assert_not_awaited()

        allowed = _interaction()
        assert await app_command_is_owner(allowed) is True

        done = _interaction()
        done.response.is_done = lambda: True
        await cog.cog_app_command_error(done, app_commands.CheckFailure())
        done.response.send_message.assert_not_awaited()
        _args, kwargs = done.followup.send.await_args
        assert kwargs["ephemeral"] is True

    asyncio.run(scenario())


def test_grok_other_errors_are_logged_and_reported(caplog):
    async def scenario():
        cog = Grok(bot=None)
        error = app_commands.AppCommandError("boom")

        pending = _interaction()
        pending.command = SimpleNamespace(name="grok")
        with caplog.at_level("ERROR", logger="cogs.grok"):
            await cog.cog_app_command_error(pending, error)
        pending.response.send_message.assert_awaited_once()
        args, kwargs = pending.response.send_message.await_args
        assert kwargs["ephemeral"] is True
        assert args[0] == "Something went wrong running that command."
        pending.followup.send.assert_not_awaited()
        assert any(record.getMessage() == "Unhandled grok error" for record in caplog.records)

        caplog.clear()
        done = _interaction()
        done.response.is_done = lambda: True
        done.command = SimpleNamespace(name="Ask Grok")
        with caplog.at_level("ERROR", logger="cogs.grok"):
            await cog.cog_app_command_error(done, error)
        done.response.send_message.assert_not_awaited()
        args, kwargs = done.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert args[0] == "Something went wrong running that command."
        assert any(record.getMessage() == "Unhandled Ask Grok error" for record in caplog.records)

        caplog.clear()
        missing = _interaction()
        with caplog.at_level("ERROR", logger="cogs.grok"):
            await cog.cog_app_command_error(missing, error)
        assert any(record.getMessage() == "Unhandled unknown error" for record in caplog.records)

        broken = _interaction()
        broken.command = SimpleNamespace(name="grok")
        broken.response.send_message.side_effect = RuntimeError("send failed")
        await cog.cog_app_command_error(broken, error)

        done_broken = _interaction()
        done_broken.response.is_done = lambda: True
        done_broken.command = SimpleNamespace(name="grok")
        done_broken.followup.send.side_effect = RuntimeError("followup failed")
        await cog.cog_app_command_error(done_broken, error)

    asyncio.run(scenario())


def test_grok_attachment_option_is_included_in_payload(monkeypatch):
    cog = Grok(bot=None)
    parameter = cog.grok.get_parameter("attachment")
    assert parameter is not None
    assert parameter.required is False
    assert parameter.type is discord.AppCommandOptionType.attachment

    attachment = _file(
        filename="note.txt",
        url="https://cdn.example/note.txt",
        content_type="text/plain",
        size=12,
    )

    async def scenario(passed, expected):
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        interaction = _interaction()
        await _invoke(Grok(bot=None), interaction, "see this", passed)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        _session, url, kwargs = _posted(sessions)
        assert url == WEBHOOK_URL
        assert kwargs["json"]["prompt"] == "see this"
        assert kwargs["json"]["attachments"] == expected
        assert "target_message" not in kwargs["json"]

    asyncio.run(scenario(None, []))
    asyncio.run(
        scenario(
            attachment,
            [
                {
                    "filename": "note.txt",
                    "url": "https://cdn.example/note.txt",
                    "content_type": "text/plain",
                    "size": 12,
                }
            ],
        )
    )


def test_grok_payload_includes_guild_and_channel_names(monkeypatch):
    async def scenario(guild_id, guild, channel, expected_guild_name, expected_channel_name):
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        interaction = _interaction(guild_id=guild_id, guild=guild, channel=channel)
        await _invoke(Grok(bot=None), interaction, "where am I")

        payload = _posted(sessions)[2]["json"]
        assert payload["guild_id"] == (None if guild_id is None else str(guild_id))
        assert payload["guild_name"] == expected_guild_name
        assert payload["channel_name"] == expected_channel_name

    dm_channel = SimpleNamespace(id=222)
    named_channel = SimpleNamespace(name="general")
    asyncio.run(scenario(None, None, dm_channel, None, None))
    asyncio.run(
        scenario(
            4242,
            SimpleNamespace(id=4242, name="Pooper Guild"),
            named_channel,
            "Pooper Guild",
            "general",
        )
    )
    asyncio.run(scenario(4242, None, named_channel, None, "general"))


def test_grok_recent_messages_are_oldest_first_and_truncated(monkeypatch):
    oldest_at = datetime(2026, 10, 14, 15, 0, 0, tzinfo=timezone.utc)
    middle_at = datetime(2026, 10, 14, 15, 1, 0, tzinfo=timezone.utc)
    newest_at = datetime(2026, 10, 14, 15, 2, 0, tzinfo=timezone.utc)
    oldest = _chat_message(
        message_id=10,
        author_name="ada",
        author_id=501,
        created_at=oldest_at,
        content="a" * 1000,
        attachments=[
            _file(
                filename="log.txt",
                url="https://cdn.example/log.txt",
                content_type="text/plain",
                size=99,
            )
        ],
    )
    middle = _chat_message(
        message_id=20,
        author_name="grace",
        author_id=502,
        created_at=middle_at,
        content="b" * 1001,
    )
    newest = _chat_message(
        message_id=30,
        author_name="linus",
        author_id=503,
        created_at=newest_at,
        content="latest",
        reply_to_id=10,
    )
    calls = []
    events = []

    async def scenario():
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        channel = SimpleNamespace(
            name="general",
            history=_history([newest, oldest, middle], calls),
        )
        interaction = _interaction(
            guild_id=4242,
            guild=SimpleNamespace(id=4242, name="Pooper Guild"),
            channel=channel,
        )

        def mark_defer(*args, **kwargs):
            events.append("defer")

        interaction.response.defer.side_effect = mark_defer
        original_history = channel.history

        def history(*args, **kwargs):
            events.append("history")
            return original_history(*args, **kwargs)

        channel.history = history
        await _invoke(Grok(bot=None), interaction, "with context")

        assert events == ["defer", "history"]
        assert calls == [
            {"limit": 20, "before": interaction},
        ]
        assert calls[0].get("oldest_first") is not True
        payload = _posted(sessions)[2]["json"]
        assert payload["recent_messages"] == [
            {
                "id": "10",
                "author_name": "ada",
                "author_id": "501",
                "created_at": "2026-10-14T15:00:00+00:00",
                "content": "a" * 1000,
                "attachments": [
                    {
                        "filename": "log.txt",
                        "url": "https://cdn.example/log.txt",
                        "content_type": "text/plain",
                    }
                ],
                "reply_to_id": None,
            },
            {
                "id": "20",
                "author_name": "grace",
                "author_id": "502",
                "created_at": "2026-10-14T15:01:00+00:00",
                "content": "b" * 1000,
                "attachments": [],
                "reply_to_id": None,
            },
            {
                "id": "30",
                "author_name": "linus",
                "author_id": "503",
                "created_at": "2026-10-14T15:02:00+00:00",
                "content": "latest",
                "attachments": [],
                "reply_to_id": "10",
            },
        ]
        assert "size" not in payload["recent_messages"][0]["attachments"][0]
        interaction.delete_original_response.assert_not_awaited()

    asyncio.run(scenario())


def test_grok_recent_messages_keeps_the_newest_window(monkeypatch):
    messages = [
        _chat_message(
            message_id=number,
            author_name="ada",
            author_id=501,
            created_at=datetime(2026, 10, 14, 15, 0, number, tzinfo=timezone.utc),
            content=f"body-{number}",
        )
        for number in range(25, 0, -1)
    ]
    calls = []

    async def scenario():
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        channel = SimpleNamespace(name="general", history=_history(messages, calls))
        interaction = _interaction(channel=channel)
        await _invoke(Grok(bot=None), interaction, "with a long channel")

        assert calls == [{"limit": 20, "before": interaction}]
        assert calls[0].get("oldest_first") is not True
        ids = [item["id"] for item in _posted(sessions)[2]["json"]["recent_messages"]]
        assert ids == [str(number) for number in range(6, 26)]

    asyncio.run(scenario())


def test_grok_recent_messages_fall_back_to_empty_list(monkeypatch):
    async def scenario(channel):
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        interaction = _interaction(channel=channel)
        await _invoke(Grok(bot=None), interaction, "keep going")

        payload = _posted(sessions)[2]["json"]
        assert payload["recent_messages"] == []
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.delete_original_response.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    calls = []
    denied = SimpleNamespace(name="general", history=_history([], calls, error=PermissionError("Missing Access")))
    broken = SimpleNamespace(history=_history([], calls, fail_during_iteration=True))
    dm_channel = SimpleNamespace(id=222, history=_history([], calls, error=RuntimeError("DM history unavailable")))
    asyncio.run(scenario(denied))
    asyncio.run(scenario(broken))
    asyncio.run(scenario(dm_channel))
    asyncio.run(scenario(None))


def test_grok_webhook_post_is_logged_without_secrets(monkeypatch, caplog):
    prompt = "super-secret-prompt-value"
    token = "super-secret-interaction-token"
    content = "super-secret-message-body"
    attachment_url = "https://cdn.example/secret-file.txt"

    def info_lines():
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "cogs.grok" and record.levelname == "INFO"
        ]

    def assert_safe(message, payload, command_name, *, has_target, recent_count):
        assert f"command={command_name}" in message
        assert f"keys={sorted(payload)}" in message
        assert f"target_message={has_target}" in message
        assert f"recent_messages={recent_count}" in message
        assert "\n" not in message
        assert token not in message
        assert prompt not in message
        assert content not in message
        assert attachment_url not in message
        assert WEBHOOK_URL not in message
        assert WEBHOOK_KEY not in message
        assert "https://" not in message

    async def slash():
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        calls = []
        channel = SimpleNamespace(
            name="general",
            history=_history(
                [
                    _chat_message(
                        message_id=1,
                        author_name="ada",
                        author_id=5,
                        created_at=CREATED_AT,
                        content=content,
                        attachments=[
                            _file(
                                filename="secret.txt",
                                url=attachment_url,
                                content_type="text/plain",
                                size=4,
                            )
                        ],
                    )
                ],
                calls,
            ),
        )
        interaction = _interaction(channel=channel)
        interaction.token = token
        interaction.command = SimpleNamespace(name="grok")
        caplog.clear()
        with caplog.at_level("INFO", logger="cogs.grok"):
            await _invoke(
                Grok(bot=None),
                interaction,
                prompt,
                _file(
                    filename="note.txt",
                    url=attachment_url,
                    content_type="text/plain",
                    size=4,
                ),
            )
        lines = info_lines()
        assert len(lines) == 1
        payload = _posted(sessions)[2]["json"]
        assert_safe(lines[0], payload, "grok", has_target=False, recent_count=1)
        assert payload["prompt"] == prompt
        assert payload["interaction_token"] == token

    async def modal():
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        target = _chat_message(
            message_id=42,
            author_name="ada",
            author_id=5,
            created_at=CREATED_AT,
            content=content,
            jump_url="https://discord.com/channels/4242/222/42",
            attachments=[
                _file(
                    filename="secret.txt",
                    url=attachment_url,
                    content_type="text/plain",
                    size=4,
                )
            ],
        )
        cog = Grok(bot=None)
        _opener, modal_view = await _open_ask_grok(cog, target)
        _modal_text_input(modal_view)._value = prompt
        submit = _interaction()
        submit.token = token
        caplog.clear()
        with caplog.at_level("INFO", logger="cogs.grok"):
            await modal_view.on_submit(submit)
        lines = info_lines()
        assert len(lines) == 1
        payload = _posted(sessions)[2]["json"]
        assert_safe(lines[0], payload, "Ask Grok", has_target=True, recent_count=0)
        assert payload["prompt"] == prompt
        assert payload["interaction_token"] == token
        assert payload["target_message"]["content"] == content
        submit.response.defer.assert_awaited_once_with(thinking=True)
        assert "ephemeral" not in submit.response.defer.await_args.kwargs

    async def unknown():
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        interaction = _interaction()
        interaction.token = token
        caplog.clear()
        with caplog.at_level("INFO", logger="cogs.grok"):
            await _invoke(Grok(bot=None), interaction, prompt)
        lines = info_lines()
        assert len(lines) == 1
        payload = _posted(sessions)[2]["json"]
        assert_safe(lines[0], payload, "unknown", has_target=False, recent_count=0)
        assert payload["prompt"] == prompt
        assert payload["interaction_token"] == token

    asyncio.run(slash())
    asyncio.run(modal())
    asyncio.run(unknown())


def test_ask_grok_menu_is_global_and_owner_only():
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = Grok(bot)
        try:
            menu = cog.ask_grok_menu
            assert menu.name == "Ask Grok"
            assert menu.type is discord.AppCommandType.message
            assert menu.guild_only is False
            assert menu._guild_ids is None
            assert app_command_is_owner in menu.checks
            registered = bot.tree.get_command("Ask Grok", type=discord.AppCommandType.message)
            assert registered is menu
            assert bot.tree.get_command("Ask Grok", guild=None, type=discord.AppCommandType.message) is menu

            owner = _interaction()
            message = _chat_message(
                message_id=7,
                author_name="ada",
                author_id=5,
                created_at=CREATED_AT,
                content="hello",
            )
            await menu._invoke(owner, message)
            owner.response.send_modal.assert_awaited_once()
            owner.response.defer.assert_not_awaited()
            modal = owner.response.send_modal.await_args.args[0]
            assert isinstance(modal, discord.ui.Modal)
            text_input = _modal_text_input(modal)
            assert text_input.style is discord.TextStyle.paragraph
            assert text_input.required is False

            cog.cog_unload()
            assert bot.tree.get_command("Ask Grok", type=discord.AppCommandType.message) is None
        finally:
            await bot.close()

    asyncio.run(scenario())

    bare = Grok(bot=None)
    assert bare.ask_grok_menu.name == "Ask Grok"
    bare.cog_unload()


def test_ask_grok_non_owner_gets_ephemeral_error():
    async def scenario():
        cog = Grok(bot=None)
        denied = _interaction()
        denied.client.is_owner = AsyncMock(return_value=False)
        message = _chat_message(
            message_id=7,
            author_name="ada",
            author_id=5,
            created_at=CREATED_AT,
            content="hello",
        )
        assert await app_command_is_owner(denied) is False
        try:
            await cog.ask_grok_menu._invoke(denied, message)
        except app_commands.AppCommandError as exc:
            await cog.ask_grok_menu.on_error(denied, exc)
        else:
            raise AssertionError("expected the owner check to fail")

        denied.response.send_modal.assert_not_awaited()
        denied.response.defer.assert_not_awaited()
        denied.response.send_message.assert_awaited_once()
        args, kwargs = denied.response.send_message.await_args
        assert kwargs["ephemeral"] is True
        assert "owner" in args[0].lower()
        denied.followup.send.assert_not_awaited()

        done = _interaction()
        done.response.is_done = lambda: True
        await cog.ask_grok_menu.on_error(done, app_commands.CheckFailure())
        done.response.send_message.assert_not_awaited()
        _args, kwargs = done.followup.send.await_args
        assert kwargs["ephemeral"] is True

    asyncio.run(scenario())


def test_ask_grok_modal_default_prompt_and_target_message(monkeypatch):
    target = _chat_message(
        message_id=9001,
        author_name="ada",
        author_id=555,
        created_at=datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc),
        content="c" * 1200,
        attachments=[
            _file(
                filename="shot.png",
                url="https://cdn.example/shot.png",
                content_type="image/png",
                size=2048,
            )
        ],
        jump_url="https://discord.com/channels/4242/222/9001",
        embeds=[
            SimpleNamespace(title="Trace", description="line 1"),
            SimpleNamespace(title=None, description=None),
        ],
    )
    expected_target = {
        "id": "9001",
        "author_name": "ada",
        "author_id": "555",
        "created_at": "2026-10-14T12:00:00+00:00",
        "content": "c" * 1200,
        "jump_url": "https://discord.com/channels/4242/222/9001",
        "attachments": [
            {
                "filename": "shot.png",
                "url": "https://cdn.example/shot.png",
                "content_type": "image/png",
                "size": 2048,
            }
        ],
        "embeds": [
            {"title": "Trace", "description": "line 1"},
            {"title": None, "description": None},
        ],
    }

    async def scenario(modal_text, expected_prompt):
        _use_config(monkeypatch, _config())
        sessions = _patch_session(monkeypatch, status=204)
        cog = Grok(bot=None)
        _opener, modal = await _open_ask_grok(cog, target)
        text_input = _modal_text_input(modal)
        text_input._value = modal_text
        calls = []
        channel = SimpleNamespace(name="general", history=_history([], calls))
        submit = _interaction(
            guild_id=4242,
            guild=SimpleNamespace(id=4242, name="Pooper Guild"),
            channel=channel,
        )
        await modal.on_submit(submit)

        submit.response.defer.assert_awaited_once_with(thinking=True)
        assert "ephemeral" not in submit.response.defer.await_args.kwargs
        submit.delete_original_response.assert_not_awaited()
        submit.followup.send.assert_not_awaited()
        assert calls == [{"limit": 20, "before": submit}]
        assert calls[0].get("oldest_first") is not True
        payload = _posted(sessions)[2]["json"]
        assert payload["prompt"] == expected_prompt
        assert payload["attachments"] == []
        assert payload["recent_messages"] == []
        assert payload["guild_name"] == "Pooper Guild"
        assert payload["channel_name"] == "general"
        assert payload["guild_id"] == "4242"
        assert payload["user_id"] == "111"
        assert payload["target_message"] == expected_target
        assert WEBHOOK_KEY not in json.dumps(payload)

    asyncio.run(scenario("", "Help me with this message."))
    asyncio.run(scenario("   ", "Help me with this message."))
    asyncio.run(scenario("Explain the bug", "Explain the bug"))


def test_ask_grok_modal_errors(monkeypatch):
    target = _chat_message(
        message_id=4,
        author_name="ada",
        author_id=5,
        created_at=CREATED_AT,
        content="help",
    )

    async def submit(cog_config, **session_kwargs):
        _use_config(monkeypatch, cog_config)
        sessions = _patch_session(monkeypatch, **session_kwargs)
        cog = Grok(bot=None)
        _opener, modal = await _open_ask_grok(cog, target)
        text_input = _modal_text_input(modal)
        text_input._value = "look at this"
        interaction = _interaction()
        await modal.on_submit(interaction)
        return interaction, sessions

    async def http_failure():
        interaction, _sessions = await submit(_config(), status=502, body="bad gateway")
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.delete_original_response.assert_awaited_once_with()
        interaction.edit_original_response.assert_not_awaited()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "HTTP 502" in args[0]
        assert "bad gateway" in args[0]
        assert WEBHOOK_KEY not in args[0]

    async def redacted_exception():
        interaction, _sessions = await submit(
            _config(),
            error=ConnectionError(f"connection refused {WEBHOOK_KEY}"),
        )
        interaction.delete_original_response.assert_awaited_once_with()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "connection refused" in args[0]
        assert WEBHOOK_KEY not in args[0]
        assert "***" in args[0]

    async def delete_fallback_fresh():
        _use_config(monkeypatch, _config())
        _patch_session(monkeypatch, status=500, body="nope")
        cog = Grok(bot=None)
        _opener, modal = await _open_ask_grok(cog, target)
        text_input = _modal_text_input(modal)
        text_input._value = "look at this"
        interaction = _interaction()
        interaction.delete_original_response.side_effect = RuntimeError("delete failed")
        await modal.on_submit(interaction)
        interaction.delete_original_response.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once_with(content=_PUBLIC_FAILURE)
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "HTTP 500" in args[0]
        assert "nope" in args[0]
        assert "nope" not in interaction.edit_original_response.await_args.kwargs["content"]

    async def missing_config():
        interaction, sessions = await submit(_config(grok_webhook_url="", grok_webhook_key=""))
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        assert sessions == []
        interaction.delete_original_response.assert_awaited_once_with()
        args, kwargs = interaction.followup.send.await_args
        assert kwargs["ephemeral"] is True
        assert "not configured" in args[0]

    asyncio.run(http_failure())
    asyncio.run(redacted_exception())
    asyncio.run(delete_fallback_fresh())
    asyncio.run(missing_config())
