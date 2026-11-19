"""Prompt detail buttons: timeout and expired interactions. No network."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.imagediffusion import ImageGenPrefView, PromptDetailButton
from cogs.utils.image_models import ImageCreation, Models


def _run(coro):
    asyncio.run(coro)


def _not_found(code: int, message: str) -> discord.NotFound:
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": code, "message": message},
    )


def _http(status: int, reason: str, message: str) -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason=reason), message)


def _view(*, author_id: int = 7, guild_id: int = 10) -> PromptDetailButton:
    image = ImageCreation("a cat in a hat", Models.ANIME_WAI_ILLUSTRIOUS)
    return PromptDetailButton(
        "a cat in a hat",
        "blurry",
        "positive cat",
        "negative cat",
        image,
        author_id,
        1.5,
        guild_id,
    )


def _button(view, label: str):
    return next(child for child in view.children if getattr(child, "label", None) == label)


def _click(*, user_id: int, roles=None, cog=None, send_message=None, defer=None, delete=None):
    response = SimpleNamespace(
        send_message=send_message if send_message is not None else AsyncMock(),
        defer=defer if defer is not None else AsyncMock(),
        is_done=lambda: False,
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, mention=f"<@{user_id}>", roles=list(roles or [])),
        response=response,
        message=SimpleNamespace(delete=delete if delete is not None else AsyncMock()),
        client=SimpleNamespace(get_cog=lambda name: cog if name == "ImageDiffusion" else None),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _mod_role(monkeypatch, role_id: int = 42):
    def fake_config(guild_id):
        return SimpleNamespace(image_diffusion_moderator_role_id=role_id)

    monkeypatch.setattr("cogs.imagediffusion.get_guild_config", fake_config)


def _assert_disabled(view) -> None:
    assert view.children
    assert all(child.disabled for child in view.children)


def test_on_timeout_ignores_unknown_message(caplog):
    unknown = _not_found(10008, "Unknown Message")
    assert unknown.code == 10008

    async def edit(**kwargs):
        raise unknown

    async def run():
        view = _view()
        assert all(not child.disabled for child in view.children)
        view.message = SimpleNamespace(edit=edit)
        with caplog.at_level(logging.DEBUG, logger="cogs.imagediffusion"):
            await view.on_timeout()
        _assert_disabled(view)

    _run(run())
    assert "image message was deleted" in caplog.text
    assert not any(
        record.levelno >= logging.ERROR and record.name == "cogs.imagediffusion"
        for record in caplog.records
    )


def test_on_timeout_ignores_a_missing_message():
    async def run():
        missing = _view()
        assert getattr(missing, "message", None) is None
        await missing.on_timeout()
        _assert_disabled(missing)

        cleared = _view()
        cleared.message = None
        await cleared.on_timeout()
        _assert_disabled(cleared)

    _run(run())


def test_on_timeout_ignores_http_exception():
    async def edit(**kwargs):
        raise _http(500, "Internal Server Error", "edit failed")

    async def run():
        view = _view()
        view.message = SimpleNamespace(edit=edit)
        await view.on_timeout()
        _assert_disabled(view)

    _run(run())


def test_on_timeout_disables_buttons_on_the_message_that_is_still_there():
    async def run():
        view = _view()
        seen = {}

        async def edit(**kwargs):
            seen["view"] = kwargs["view"]
            seen["disabled"] = [child.disabled for child in kwargs["view"].children]

        view.message = SimpleNamespace(edit=edit)
        await view.on_timeout()
        assert seen["view"] is view
        assert seen["disabled"] == [True, True, True, True]

    _run(run())


def test_prompt_details_still_send_the_ephemeral_embed():
    async def run():
        view = _view()
        interaction = _click(user_id=8)
        await _button(view, "See prompt details").callback(interaction)
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        embed = kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert embed.title == "🖼️ Image Generation Details"
        assert "a cat in a hat" in fields["User Prompt"]
        assert fields["Positive Prompt"] == "positive cat"
        assert fields["Generation time"] == "1.50 seconds"
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    _run(run())


def test_prompt_details_ignore_unknown_interaction(caplog):
    expired = _not_found(10062, "Unknown interaction")
    assert expired.code == 10062

    async def send_message(*args, **kwargs):
        raise expired

    async def run():
        view = _view()
        interaction = _click(user_id=8, send_message=send_message)
        with caplog.at_level(logging.DEBUG, logger="cogs.imagediffusion"):
            await _button(view, "See prompt details").callback(interaction)

    _run(run())
    assert "interaction expired" in caplog.text
    assert not any(
        record.levelno >= logging.ERROR and record.name == "cogs.imagediffusion"
        for record in caplog.records
    )


def test_edit_and_regenerate_still_opens_the_pref_view_without_deferring():
    async def run():
        view = _view()
        cog = SimpleNamespace()
        interaction = _click(user_id=3, cog=cog)
        await _button(view, "Edit and Regenerate").callback(interaction)
        interaction.response.defer.assert_not_awaited()
        args = interaction.response.send_message.await_args
        assert args.args[0] == "Edit your options before regenerating:"
        assert args.kwargs["ephemeral"] is True
        assert isinstance(args.kwargs["view"], ImageGenPrefView)
        assert args.kwargs["view"].cog is cog
        assert args.kwargs["view"].initial_prompt == "a cat in a hat"
        interaction.followup.send.assert_not_awaited()

    _run(run())


def test_regenerate_still_queues_the_same_ephemeral_message():
    async def run():
        view = _view()
        queued = []

        async def enqueue_image(entry, interaction, **kwargs):
            queued.append((entry, kwargs))
            await interaction.response.send_message(kwargs["queued_message"], ephemeral=kwargs["ephemeral"])
            return True

        interaction = _click(user_id=7, cog=SimpleNamespace(enqueue_image=enqueue_image))
        await _button(view, "Regenerate").callback(interaction)
        assert len(queued) == 1
        entry, kwargs = queued[0]
        assert entry.prompt == "a cat in a hat"
        assert entry.negative_prompt == "blurry"
        assert entry.model is Models.ANIME_WAI_ILLUSTRIOUS
        assert kwargs["queued_message"] == "Queued a new generation with the same settings!"
        assert kwargs["ephemeral"] is True
        sent = interaction.response.send_message.await_args
        assert sent.args[0] == "Queued a new generation with the same settings!"
        assert sent.kwargs["ephemeral"] is True
        interaction.response.defer.assert_not_awaited()

    _run(run())


def test_regenerate_ignores_unknown_interaction():
    expired = _not_found(10062, "Unknown interaction")

    async def send_message(*args, **kwargs):
        raise expired

    async def run():
        view = _view()
        queued = []

        async def enqueue_image(entry, interaction, **kwargs):
            queued.append(entry)
            await interaction.response.send_message(kwargs["queued_message"], ephemeral=kwargs["ephemeral"])

        interaction = _click(
            user_id=7,
            cog=SimpleNamespace(enqueue_image=enqueue_image),
            send_message=send_message,
        )
        await _button(view, "Regenerate").callback(interaction)
        assert len(queued) == 1
        assert queued[0].prompt == "a cat in a hat"

    _run(run())


def test_delete_acknowledges_then_deletes_for_the_author(monkeypatch):
    _mod_role(monkeypatch)

    async def run():
        view = _view(author_id=7)
        calls = []

        async def defer(*args, **kwargs):
            calls.append("defer")

        async def delete(*args, **kwargs):
            calls.append("delete")

        interaction = _click(
            user_id=7,
            roles=[SimpleNamespace(id=1)],
            defer=defer,
            delete=delete,
        )
        await _button(view, "Delete").callback(interaction)
        assert calls == ["defer", "delete"]
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    _run(run())


def test_delete_acknowledges_then_deletes_for_a_moderator(monkeypatch):
    _mod_role(monkeypatch, 42)

    async def run():
        view = _view(author_id=7)
        calls = []

        async def defer(*args, **kwargs):
            calls.append("defer")

        async def delete(*args, **kwargs):
            calls.append("delete")

        interaction = _click(
            user_id=99,
            roles=[SimpleNamespace(id=42)],
            defer=defer,
            delete=delete,
        )
        await _button(view, "Delete").callback(interaction)
        assert calls == ["defer", "delete"]
        interaction.response.send_message.assert_not_awaited()

    _run(run())


def test_delete_rejects_someone_who_is_not_the_author_or_a_moderator(monkeypatch):
    _mod_role(monkeypatch, 42)

    async def run():
        view = _view(author_id=7)
        interaction = _click(user_id=99, roles=[SimpleNamespace(id=1)])
        await _button(view, "Delete").callback(interaction)
        interaction.message.delete.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        sent = interaction.response.send_message.await_args
        assert sent.args[0] == "You don't have permission to delete this message."
        assert sent.kwargs["ephemeral"] is True

    _run(run())


def test_delete_ignores_unknown_interaction(caplog):
    expired = _not_found(10062, "Unknown interaction")
    assert expired.code == 10062

    async def defer(*args, **kwargs):
        raise expired

    async def run():
        view = _view(author_id=7)
        interaction = _click(user_id=7, defer=defer)
        with caplog.at_level(logging.DEBUG, logger="cogs.imagediffusion"):
            await _button(view, "Delete").callback(interaction)
        interaction.message.delete.assert_awaited()
        interaction.response.send_message.assert_not_awaited()

    _run(run())
    assert "expired before it could be acknowledged" in caplog.text


def test_delete_ignores_an_unknown_interaction_on_the_rejection(monkeypatch):
    _mod_role(monkeypatch, 42)

    async def send_message(*args, **kwargs):
        raise _not_found(10062, "Unknown interaction")

    async def run():
        view = _view(author_id=7)
        interaction = _click(user_id=99, roles=[SimpleNamespace(id=1)], send_message=send_message)
        await _button(view, "Delete").callback(interaction)
        interaction.message.delete.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()

    _run(run())


def test_delete_ignores_a_message_that_is_already_gone(caplog):
    async def run():
        view = _view(author_id=7)
        calls = []

        async def defer(*args, **kwargs):
            calls.append("defer")

        async def delete(*args, **kwargs):
            calls.append("delete")
            raise _not_found(10008, "Unknown Message")

        interaction = _click(user_id=7, defer=defer, delete=delete)
        with caplog.at_level(logging.DEBUG, logger="cogs.imagediffusion"):
            await _button(view, "Delete").callback(interaction)
        assert calls == ["defer", "delete"]

    _run(run())
    assert "already deleted" in caplog.text


def test_delete_ignores_http_exception_from_the_response_and_the_delete():
    async def run():
        view = _view(author_id=7)

        async def defer(*args, **kwargs):
            raise _http(500, "Internal Server Error", "ack failed")

        interaction = _click(user_id=7, defer=defer)
        await _button(view, "Delete").callback(interaction)
        interaction.message.delete.assert_awaited()

        async def delete(*args, **kwargs):
            raise _http(403, "Forbidden", "missing permission")

        again = _view(author_id=7)
        second = _click(user_id=7, delete=delete)
        await _button(again, "Delete").callback(second)
        second.response.defer.assert_awaited()

    _run(run())
