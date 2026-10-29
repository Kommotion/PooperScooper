"""Halloweenify builds one Identity Edit of the caller's avatar. No network."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.asset import Asset

from cogs.imagediffusion import (
    HALLOWEENIFY_ENABLED,
    HALLOWEENIFY_PROMPT,
    HALLOWEENIFY_SIZE,
    ImageDiffusion,
    PromptDetailButton,
    app_commands_for_tree,
    build_halloweenify_image,
    generation_caption,
    release_source_bytes,
    static_png_avatar,
)
from cogs.utils.image_models import ImageCreation, Models, NsfwLevel


class FakeAvatar:
    def __init__(self, data: bytes, *, animated: bool = False, fail: bool = False):
        self.data = data
        self.animated = animated
        self.fail = fail
        self.kwargs = None

    def is_animated(self) -> bool:
        return self.animated

    def replace(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def read(self) -> bytes:
        if self.fail:
            raise RuntimeError("cdn down")
        return self.data


class FakeDiffusion:
    def __init__(self, *, allowed: bool = True, enqueue_ok: bool = True):
        self.allowed = allowed
        self.enqueue_ok = enqueue_ok
        self.entries = []
        self.kwargs = None
        self.seen = None

    def _is_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        self.seen = (guild_id, channel_id)
        return self.allowed

    async def enqueue_image(self, entry, interaction, **kwargs):
        self.entries.append(entry)
        self.kwargs = kwargs
        if not self.enqueue_ok:
            await interaction.response.send_message(
                "Queue is full! Please wait and try again.",
                ephemeral=True,
            )
            return False
        await interaction.response.send_message(kwargs["queued_message"], ephemeral=False)
        return True


def _user(data: bytes, *, animated: bool = False, fail: bool = False, user_id: int = 7):
    return SimpleNamespace(
        id=user_id,
        mention=f"<@{user_id}>",
        display_avatar=FakeAvatar(data, animated=animated, fail=fail),
    )


def _interaction(user, *, guild_id=10, channel_id=20):
    return SimpleNamespace(
        user=user,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user=SimpleNamespace(display_avatar=FakeAvatar(b"someone-else")),
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False, defer=AsyncMock()),
    )


def test_prompt_is_fixed_and_spooky():
    assert "Halloween" in HALLOWEENIFY_PROMPT
    assert "jack-o'-lantern" in HALLOWEENIFY_PROMPT
    assert "bats" in HALLOWEENIFY_PROMPT
    assert "full moon" in HALLOWEENIFY_PROMPT
    assert "not gory" in HALLOWEENIFY_PROMPT
    assert "identity" in HALLOWEENIFY_PROMPT
    assert HALLOWEENIFY_SIZE == 1024


def test_command_has_no_user_or_prompt_option():
    command = ImageDiffusion.halloweenify
    assert command.name == "halloweenify"
    assert command.guild_only is True
    assert command.parent is None
    assert [param.name for param in command.parameters] == []


def test_static_png_avatar_requests_1024_and_a_still_frame():
    still = Asset(
        state=None,
        url="https://cdn.discordapp.com/avatars/9/abc.webp",
        key="abc",
        animated=False,
    )
    still_out = static_png_avatar(still)
    assert still_out.url.split("?")[0].endswith("/abc.png")
    assert "size=1024" in still_out.url

    moving = Asset(
        state=None,
        url="https://cdn.discordapp.com/avatars/9/a_abc.gif",
        key="a_abc",
        animated=True,
    )
    moving_out = static_png_avatar(moving)
    assert moving_out.url.split("?")[0].endswith("/a_abc.png")
    assert "size=1024" in moving_out.url
    assert not moving_out.url.split("?")[0].endswith(".gif")


def test_build_uses_identity_edit_and_caller_bytes():
    caller = b"caller-avatar"
    interaction = SimpleNamespace(user=SimpleNamespace(mention="<@7>", id=7))
    image = build_halloweenify_image(caller, interaction)
    assert isinstance(image, ImageCreation)
    assert image.prompt == HALLOWEENIFY_PROMPT
    assert image.model is Models.KREA2_IDENTITY_EDIT
    assert image.source_image_bytes is caller
    assert image.source_image_filename == "halloweenify_avatar.png"
    assert image.edit_width == 1024
    assert image.edit_height == 1024
    assert image.retain_source is True
    assert image.llm_prompt_enhance is False
    assert image.use_default_positive is False
    assert image.use_default_negative is False
    assert image.nsfw is NsfwLevel.NOT_SPECIFIED
    assert image.interaction is interaction

    plain = ImageCreation(
        "edit this",
        Models.KREA2_IDENTITY_EDIT,
        interaction=SimpleNamespace(user=SimpleNamespace(mention="<@3>")),
        source_image_bytes=b"upload",
    )
    assert plain.retain_source is False
    release_source_bytes(plain)
    assert plain.source_image_bytes is None
    release_source_bytes(image)
    assert image.source_image_bytes is caller

    assert generation_caption(image, "preview") == "**Halloweenify** — <@7>"
    assert "jack-o'-lantern" not in generation_caption(image, "preview")
    plain_caption = generation_caption(plain, "red jacket")
    assert plain_caption.startswith("**Edit:** red jacket")


def test_empty_avatar_is_rejected():
    try:
        build_halloweenify_image(b"", SimpleNamespace())
    except ValueError as exc:
        assert "avatar" in str(exc)
    else:
        raise AssertionError("empty avatar should be rejected")


def test_halloweenify_queues_only_the_caller():
    async def run():
        caller = _user(b"caller-png")
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction)

        assert cog.seen == (10, 20)
        assert len(cog.entries) == 1
        entry = cog.entries[0]
        assert entry.source_image_bytes == b"caller-png"
        assert entry.source_image_bytes != b"someone-else"
        assert entry.prompt == HALLOWEENIFY_PROMPT
        assert entry.model is Models.KREA2_IDENTITY_EDIT
        assert entry.edit_width == 1024 and entry.edit_height == 1024
        assert entry.retain_source is True
        assert caller.display_avatar.kwargs == {"size": 1024, "static_format": "png"}
        assert cog.kwargs["queued_message"] == "Queued a Halloween version of your avatar (1024×1024)."
        assert interaction.response.send_message.await_count == 1

    asyncio.run(run())


def test_animated_avatar_uses_png_frame():
    async def run():
        caller = _user(b"frame", animated=True)
        interaction = _interaction(caller)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, interaction)
        assert caller.display_avatar.kwargs == {"size": 1024, "format": "png"}
        assert cog.entries[0].source_image_bytes == b"frame"

    asyncio.run(run())


def test_disallowed_channel_is_ephemeral_and_not_queued():
    async def run():
        cog = FakeDiffusion(allowed=False)
        interaction = _interaction(_user(b"caller-png"))
        await ImageDiffusion.halloweenify.callback(cog, interaction)
        assert cog.entries == []
        args = interaction.response.send_message.await_args
        assert args.args[0] == "This command is not allowed in this channel!"
        assert args.kwargs["ephemeral"] is True

        missing_guild = _interaction(_user(b"caller-png"), guild_id=None)
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, missing_guild)
        assert cog.entries == []

    asyncio.run(run())


def test_avatar_fetch_failure_is_ephemeral():
    async def run():
        cog = FakeDiffusion()
        interaction = _interaction(_user(b"caller-png", fail=True))
        await ImageDiffusion.halloweenify.callback(cog, interaction)
        assert cog.entries == []
        args = interaction.response.send_message.await_args
        assert "couldn't fetch your avatar" in args.args[0]
        assert args.kwargs["ephemeral"] is True

        empty = _interaction(_user(b""))
        cog = FakeDiffusion()
        await ImageDiffusion.halloweenify.callback(cog, empty)
        assert cog.entries == []

    asyncio.run(run())


def test_queue_full_uses_the_shared_enqueue_message():
    async def run():
        cog = FakeDiffusion(enqueue_ok=False)
        interaction = _interaction(_user(b"caller-png"))
        await ImageDiffusion.halloweenify.callback(cog, interaction)
        assert len(cog.entries) == 1
        args = interaction.response.send_message.await_args
        assert args.args[0] == "Queue is full! Please wait and try again."
        assert interaction.response.send_message.await_count == 1

    asyncio.run(run())


def test_result_view_keeps_caller_bytes_for_regenerate():
    async def run():
        image = build_halloweenify_image(b"caller-png", SimpleNamespace(user=SimpleNamespace(id=7, mention="<@7>")))
        view = PromptDetailButton(
            image.prompt,
            "",
            image.prompt,
            "",
            image,
            7,
            1.5,
            10,
        )
        assert view.retain_source is True
        assert view.source_image_bytes == b"caller-png"
        assert view.model is Models.KREA2_IDENTITY_EDIT
        assert view.edit_width == 1024

        view.source_image_bytes = None
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(send_message=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: None),
        )
        button = next(child for child in view.children if getattr(child, "label", None) == "Regenerate")
        await button.callback(interaction)
        message = interaction.response.send_message.await_args.args[0]
        assert "/halloweenify" in message
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True

    asyncio.run(run())


def _command_tree():
    connection = SimpleNamespace(_command_tree=None)
    client = SimpleNamespace(http=SimpleNamespace(), _connection=connection)
    return discord.app_commands.CommandTree(client)


def test_halloweenify_is_kept_off_the_tree_when_disabled():
    assert HALLOWEENIFY_ENABLED is False
    assert ImageDiffusion.halloweenify.name == "halloweenify"
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == ["imagegen"]

    asyncio.run(run())

    tree = _command_tree()
    for command in app_commands_for_tree([ImageDiffusion.halloweenify]):
        tree.add_command(command)
    assert tree.get_command("halloweenify") is None
    assert all(getattr(command, "name", None) != "halloweenify" for command in tree.get_commands())


def test_halloweenify_registers_when_the_flag_is_on(monkeypatch):
    monkeypatch.setattr("cogs.imagediffusion.HALLOWEENIFY_ENABLED", True)
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen", "halloweenify"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == ["imagegen", "halloweenify"]

    asyncio.run(run())

    tree = _command_tree()
    tree.add_command(ImageDiffusion.halloweenify)
    assert tree.get_command("halloweenify").name == "halloweenify"
