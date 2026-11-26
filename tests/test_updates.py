"""Updates announcements, the notify button, and owner prefix commands. No network."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from cogs.updates import (
    ANNOUNCEMENTS_CHANNEL_ID,
    CONTENT_LIMIT,
    EMBED_LIMIT,
    GUILD_ID,
    NOTIFY_CUSTOM_ID,
    ROLE_NAME,
    SETTINGS_PATH,
    TEST_CHANNEL_ID,
    Updates,
    default_settings,
    make_view,
    write_json,
)
from cogs.utils.constants import DATA_DIR
from PooperScooper import _HALLOWEEN_EXTENSIONS, initial_extensions


def _http(status: int, reason: str, text: str = "nope"):
    response = SimpleNamespace(status=status, reason=reason)
    if status == 403:
        return discord.Forbidden(response, text)
    return discord.HTTPException(response, text)


class FakeRole:
    def __init__(self, role_id: int, name: str = ROLE_NAME):
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(self, user_id: int, guild, roles=None):
        self.id = user_id
        self.guild = guild
        self.roles = list(roles or [])
        self.added = []
        self.removed = []
        self.fail = None

    async def add_roles(self, *roles, reason=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.added.append((roles, reason))
        for role in roles:
            if not any(getattr(item, "id", None) == role.id for item in self.roles):
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.removed.append((roles, reason))
        remove_ids = {role.id for role in roles}
        self.roles = [item for item in self.roles if getattr(item, "id", None) not in remove_ids]


class FakeMessage:
    def __init__(self, message_id: int):
        self.id = message_id


class FakeTextChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent = []
        self._next = 1000

    async def send(self, content=None, **kwargs):
        self._next += 1
        message = FakeMessage(self._next)
        self.sent.append((content, kwargs))
        return message


class FakeAttachment:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self):
        if isinstance(self._data, BaseException):
            raise self._data
        return self._data


class FakeGuild:
    def __init__(self, guild_id: int = 932057681307512922):
        self.id = guild_id
        self.roles = []
        self.members = {}
        self.created = []
        self.fail_create = None

    def get_role(self, role_id: int):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int):
        member = self.members.get(user_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        return member

    async def create_role(self, **kwargs):
        if self.fail_create is not None:
            raise self.fail_create
        role = FakeRole(8000 + len(self.created), kwargs.get("name", ROLE_NAME))
        self.created.append(kwargs)
        self.roles.append(role)
        return role


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))


class FakeBot:
    def __init__(self, guild=None, channels=None, extra_guilds=None):
        self.guild = guild
        self.channels = dict(channels or {})
        self.views = []
        self.is_owner = AsyncMock(return_value=False)
        self._before_invoke = None
        self._after_invoke = None
        self._cog = None
        self._guilds = {}
        if guild is not None:
            self._guilds[guild.id] = guild
        for item in extra_guilds or []:
            self._guilds[item.id] = item

    def get_guild(self, guild_id: int):
        return self._guilds.get(guild_id)

    async def fetch_guild(self, guild_id: int):
        found = self.get_guild(guild_id)
        if found is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown guild")
        return found

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown channel")
        return channel

    def add_view(self, view, *, message_id=None):
        self.views.append((view, message_id))

    def get_cog(self, name):
        if name == "Updates":
            return self._cog
        return None

    async def can_run(self, ctx, *, call_once=False):
        return True


def _cog(tmp_path, settings=None, *, guild=None, channels=None, extra_guilds=None):
    settings_path = tmp_path / "updates_settings.json"
    if settings is not None:
        write_json(settings_path, settings)
    if guild is None:
        guild = FakeGuild()
    bot = FakeBot(guild, channels, extra_guilds)
    cog = Updates(bot, settings_path=settings_path)
    bot._cog = cog
    for command in cog.walk_commands():
        command.cog = cog
    return cog


def _interaction(cog, *, user, guild=None):
    return SimpleNamespace(
        user=user,
        guild=guild if guild is not None else cog.bot.guild,
        client=cog.bot,
        message=SimpleNamespace(id=1, attachments=[]),
        response=FakeResponse(),
    )


def _ctx(cog, user_id: int, *, owner: bool, command=None, attachments=None, channel=None):
    cog.bot.is_owner = AsyncMock(return_value=owner)
    author = SimpleNamespace(id=user_id, bot=False)
    invoked = channel if channel is not None else FakeTextChannel(42)
    ctx = SimpleNamespace(
        author=author,
        guild=cog.bot.guild,
        channel=invoked,
        bot=cog.bot,
        cog=cog,
        reply=AsyncMock(),
        send=AsyncMock(),
        prefix="!",
        view=StringView(""),
        command=command or cog.announce_setup,
        invoked_parents=[],
        invoked_with="announce",
    )
    ctx.message = SimpleNamespace(
        id=1,
        author=author,
        guild=ctx.guild,
        channel=invoked,
        attachments=list(attachments or []),
    )
    return ctx


def _button(view, custom_id: str):
    return next(item for item in view.children if item.custom_id == custom_id)


def _pair():
    announcements = FakeTextChannel(ANNOUNCEMENTS_CHANNEL_ID)
    test = FakeTextChannel(TEST_CHANNEL_ID)
    return announcements, test, {
        ANNOUNCEMENTS_CHANNEL_ID: announcements,
        TEST_CHANNEL_ID: test,
    }


def test_notify_toggles_role_by_id_and_by_name(tmp_path):
    async def run():
        role = FakeRole(55, ROLE_NAME)
        guild = FakeGuild()
        guild.roles.append(role)
        member = FakeMember(9, guild)
        cog = _cog(tmp_path, settings={"role_id": 55}, guild=guild)
        interaction = _interaction(cog, user=member)
        await _button(make_view(), NOTIFY_CUSTOM_ID).callback(interaction)
        assert interaction.response.sent[0][0] == "You'll be pinged for PooperScooper updates 🐶💩"
        assert interaction.response.sent[0][1]["ephemeral"] is True
        assert member.added[0][0] == (role,)
        assert member.added[0][1] == "Updates notify on"

        again = _interaction(cog, user=member)
        await cog.handle_notify(again)
        assert again.response.sent[0][0] == "You won't be pinged anymore."
        assert again.response.sent[0][1]["ephemeral"] is True
        assert member.removed[0][0] == (role,)
        assert member.removed[0][1] == "Updates notify off"
        assert role not in member.roles

        named = FakeRole(70, ROLE_NAME)
        other = FakeRole(71, "Someone Else")
        name_guild = FakeGuild()
        name_guild.roles.extend([other, named])
        person = FakeMember(3, name_guild)
        name_cog = _cog(tmp_path / "name", guild=name_guild)
        name_hit = _interaction(name_cog, user=person)
        await name_cog.handle_notify(name_hit)
        assert person.added[0][0] == (named,)
        assert person.added[0][1] == "Updates notify on"

        stale = FakeRole(80, ROLE_NAME)
        stale_guild = FakeGuild()
        stale_guild.roles.append(stale)
        stale_member = FakeMember(4, stale_guild)
        stale_cog = _cog(tmp_path / "stale", settings={"role_id": 12345}, guild=stale_guild)
        stale_hit = _interaction(stale_cog, user=stale_member)
        await stale_cog.handle_notify(stale_hit)
        assert stale_member.added[0][0] == (stale,)

    asyncio.run(run())


def test_notify_unset_forbidden_and_http_exception(tmp_path):
    async def run():
        cog = _cog(tmp_path)
        member = FakeMember(9, cog.bot.guild)
        missing = _interaction(cog, user=member)
        await cog.handle_notify(missing)
        assert missing.response.sent[0][0] == "Signups aren't set up yet."
        assert missing.response.sent[0][1]["ephemeral"] is True
        assert member.added == []

        role = FakeRole(55)
        cog.bot.guild.roles.append(role)
        write_json(cog.settings_path, {"role_id": 55})
        for status, reason in ((403, "Forbidden"), (500, "Server Error")):
            person = FakeMember(status, cog.bot.guild)
            person.fail = _http(status, reason)
            interaction = _interaction(cog, user=person)
            await cog.handle_notify(interaction)
            assert interaction.response.sent[0][0] == "Couldn't update your notify role."
            assert interaction.response.sent[0][1]["ephemeral"] is True
            assert person.roles == []
            assert person.added == []
            assert person.removed == []

    asyncio.run(run())


def test_cog_load_registers_persistent_view(tmp_path):
    async def run():
        cog = _cog(tmp_path / "load")
        await cog.cog_load()
        view, message_id = cog.bot.views[0]
        assert message_id is None
        assert view.timeout is None
        assert view.is_persistent() is True
        assert [item.custom_id for item in view.children] == [NOTIFY_CUSTOM_ID]

    asyncio.run(run())


def test_owner_commands_reply_and_non_owners_stay_silent(tmp_path, caplog):
    async def run():
        announcements, test, channels = _pair()
        cog = _cog(tmp_path, channels=channels)
        preview = cog.announce.get_command("preview")
        for command in (cog.announce_setup, cog.announce, preview):
            stranger = _ctx(cog, 5, owner=False, command=command)
            with pytest.raises(commands.NotOwner):
                await command.can_run(stranger)
            await command.callback(cog, stranger)
            with caplog.at_level(logging.ERROR, logger="cogs.updates"):
                await cog.cog_command_error(stranger, commands.NotOwner("You do not own this bot."))
            try:
                await command.invoke(stranger)
            except commands.CheckFailure as exc:
                await cog.cog_command_error(stranger, exc)
            else:
                raise AssertionError("non-owner invoke should fail the owner check")
            stranger.reply.assert_not_awaited()
            stranger.send.assert_not_awaited()
        assert "Updates command failed" not in caplog.text
        assert cog.bot.guild.created == []
        assert announcements.sent == []
        assert test.sent == []

        owner = _ctx(cog, 9, owner=True, command=cog.announce_setup)
        write_json(cog.settings_path, {"note": "keep"})
        await cog.announce_setup.callback(cog, owner)
        created = cog.bot.guild.created[0]
        assert created["name"] == ROLE_NAME
        assert created["mentionable"] is True
        assert created["permissions"].value == 0
        assert created["reason"] == "PooperScooper updates signups"
        role_id = cog.bot.guild.roles[-1].id
        assert owner.reply.await_args.args[0] == str(role_id)
        assert owner.reply.await_args.kwargs["mention_author"] is False
        stored = json.loads(cog.settings_path.read_text(encoding="utf-8-sig"))
        assert stored["role_id"] == role_id
        assert stored["guild_id"] == GUILD_ID
        assert stored["channel_id"] == ANNOUNCEMENTS_CHANNEL_ID
        assert stored["note"] == "keep"
        assert not cog.settings_path.read_bytes().startswith(b"\xef\xbb\xbf")

        existing = FakeRole(4242, ROLE_NAME)
        reuse_guild = FakeGuild()
        reuse_guild.roles.append(existing)
        reuse = _cog(tmp_path / "reuse", guild=reuse_guild)
        reuse_ctx = _ctx(reuse, 9, owner=True, command=reuse.announce_setup)
        await reuse.announce_setup.callback(reuse, reuse_ctx)
        assert reuse_guild.created == []
        assert reuse_ctx.reply.await_args.args[0] == "4242"
        saved = json.loads(reuse.settings_path.read_text(encoding="utf-8"))
        assert saved["role_id"] == 4242
        assert saved["guild_id"] == reuse_guild.id

        source = Path(__file__).resolve().parents[1].joinpath("cogs", "updates.py").read_text(encoding="utf-8")
        assert "@commands.is_owner()" in source
        assert Updates.announce_setup.hidden is True
        assert Updates.announce.hidden is True
        assert Updates.announce.invoke_without_command is True
        assert preview.hidden is True
        assert preview.qualified_name == "announce preview"
        assert list(Updates.__cog_app_commands__) == []

    asyncio.run(run())


def test_announce_posts_to_announcements_with_ping_and_button(tmp_path):
    async def run():
        role = FakeRole(55, ROLE_NAME)
        guild = FakeGuild()
        guild.roles.append(role)
        announcements, _test, channels = _pair()
        cog = _cog(tmp_path, settings={"role_id": 55}, guild=guild, channels=channels)
        invoked = FakeTextChannel(42)
        ctx = _ctx(cog, 9, owner=True, command=cog.announce, channel=invoked)
        ctx.guild = FakeGuild(guild_id=12345)
        await cog.announce.callback(cog, ctx, text="Patch notes are up.")
        assert invoked.sent == []
        assert len(announcements.sent) == 1
        content, kwargs = announcements.sent[0]
        assert content == f"<@&{role.id}>\nPatch notes are up."
        assert kwargs["allowed_mentions"].roles == [role]
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].users is False
        assert [item.custom_id for item in kwargs["view"].children] == [NOTIFY_CUSTOM_ID]
        assert ctx.reply.await_args.args[0] == f"Posted the announcement ({1001})."
        assert ctx.reply.await_args.kwargs["mention_author"] is False

    asyncio.run(run())


def test_announce_preview_posts_to_test_channel_without_ping(tmp_path):
    async def run():
        announcements, test, channels = _pair()
        cog = _cog(tmp_path, guild=FakeGuild(), channels=channels)
        assert cog.bot.guild.roles == []
        invoked = FakeTextChannel(42)
        preview = cog.announce.get_command("preview")
        ctx = _ctx(cog, 9, owner=True, command=preview, channel=invoked)
        await preview.callback(cog, ctx, text="Preview text")
        assert announcements.sent == []
        assert invoked.sent == []
        assert len(test.sent) == 1
        content, kwargs = test.sent[0]
        assert content == "Preview text"
        assert "<@&" not in content
        mentions = kwargs["allowed_mentions"]
        assert mentions.roles is False
        assert mentions.everyone is False
        assert mentions.users is False
        assert [item.custom_id for item in kwargs["view"].children] == [NOTIFY_CUSTOM_ID]
        assert ctx.reply.await_args.args[0] == "Posted the preview (1001)."
        assert ctx.reply.await_args.kwargs["mention_author"] is False

    asyncio.run(run())


def test_announce_split_and_attachment(tmp_path):
    async def run():
        role = FakeRole(55, ROLE_NAME)

        def prepared(folder: str):
            guild = FakeGuild()
            guild.roles.append(role)
            announcements, test, channels = _pair()
            cog = _cog(
                tmp_path / folder,
                settings={"role_id": role.id},
                guild=guild,
                channels=channels,
            )
            return cog, announcements, test

        short_cog, short_channel, _short_test = prepared("short")
        short_ctx = _ctx(short_cog, 9, owner=True, command=short_cog.announce)
        await short_cog.announce.callback(short_cog, short_ctx, text="hello")
        content, kwargs = short_channel.sent[0]
        assert content == f"<@&{role.id}>\nhello"
        assert kwargs.get("embed") is None
        assert len(short_channel.sent) == 1

        embed_body = "b" * CONTENT_LIMIT
        embed_cog, embed_channel, _embed_test = prepared("embed")
        embed_ctx = _ctx(embed_cog, 9, owner=True, command=embed_cog.announce)
        await embed_cog.announce.callback(embed_cog, embed_ctx, text=embed_body)
        content, kwargs = embed_channel.sent[0]
        assert content == f"<@&{role.id}>"
        assert kwargs["embed"].description == embed_body
        assert [item.custom_id for item in kwargs["view"].children] == [NOTIFY_CUSTOM_ID]
        assert len(embed_channel.sent) == 1

        long_body = "c" * (EMBED_LIMIT + CONTENT_LIMIT + 50)
        long_cog, long_channel, _long_test = prepared("long")
        long_ctx = _ctx(long_cog, 9, owner=True, command=long_cog.announce)
        await long_cog.announce.callback(long_cog, long_ctx, text=long_body)
        first, kwargs = long_channel.sent[0]
        assert first == f"<@&{role.id}>"
        assert kwargs["embed"].description == long_body[:EMBED_LIMIT]
        assert [item.custom_id for item in kwargs["view"].children] == [NOTIFY_CUSTOM_ID]
        followups = long_channel.sent[1:]
        assert [len(piece[0]) for piece in followups] == [CONTENT_LIMIT, 50]
        assert "".join(piece[0] for piece in followups) == long_body[EMBED_LIMIT:]
        for piece, follow_kwargs in followups:
            assert "view" not in follow_kwargs
            assert follow_kwargs.get("embed") is None
            assert "<@&" not in piece
            assert follow_kwargs["allowed_mentions"].roles is False
            assert follow_kwargs["allowed_mentions"].everyone is False
            assert follow_kwargs["allowed_mentions"].users is False

        file_cog, file_channel, _file_test = prepared("file")
        attachment = FakeAttachment("notes.TXT", b"\xef\xbb\xbffrom-file")
        file_ctx = _ctx(
            file_cog,
            9,
            owner=True,
            command=file_cog.announce,
            attachments=[attachment],
        )
        await file_cog.announce.callback(file_cog, file_ctx, text="command text should lose")
        assert file_channel.sent[0][0] == f"<@&{role.id}>\nfrom-file"

        bare = FakeGuild()
        announcements, _test, channels = _pair()
        bare_cog = _cog(tmp_path / "bare", guild=bare, channels=channels)
        bare_ctx = _ctx(bare_cog, 9, owner=True, command=bare_cog.announce)
        await bare_cog.announce.callback(bare_cog, bare_ctx, text="no role yet")
        assert announcements.sent == []
        assert bare_ctx.reply.await_args.args[0] == "Run !announce_setup first."

        empty_cog, empty_channel, _empty_test = prepared("empty")
        empty_ctx = _ctx(empty_cog, 9, owner=True, command=empty_cog.announce)
        await empty_cog.announce.callback(empty_cog, empty_ctx, text="   ")
        assert empty_channel.sent == []
        assert empty_ctx.reply.await_args.args[0] == "Need some announcement text, or a .txt file."

        bad = FakeAttachment("notes.txt", _http(400, "Bad Request"))
        bad_cog, bad_channel, _bad_test = prepared("bad")
        bad_ctx = _ctx(bad_cog, 9, owner=True, command=bad_cog.announce, attachments=[bad])
        await bad_cog.announce.callback(bad_cog, bad_ctx, text="fallback text")
        assert bad_channel.sent == []
        assert bad_ctx.reply.await_args.args[0] == "Couldn't read that .txt file."

    asyncio.run(run())


def test_paths_and_registration():
    assert SETTINGS_PATH == DATA_DIR / "updates_settings.json"
    assert ROLE_NAME == "Pooperscooper Updates"
    assert NOTIFY_CUSTOM_ID == "updates:notify"
    assert default_settings() == {"guild_id": GUILD_ID, "channel_id": ANNOUNCEMENTS_CHANNEL_ID}
    assert "cogs.updates" in initial_extensions
    assert initial_extensions[-1] == "cogs.updates"
    assert "cogs.updates" not in _HALLOWEEN_EXTENSIONS


def test_settings_retarget_guild_and_notify_uses_click_guild(tmp_path):
    async def run():
        support_id = 1045144253627637830
        mts = FakeGuild()
        support = FakeGuild(support_id)
        announcements, test, channels = _pair()
        cog = _cog(
            tmp_path,
            settings={"guild_id": support_id, "channel_id": TEST_CHANNEL_ID},
            guild=mts,
            channels=channels,
            extra_guilds=[support],
        )
        ctx = _ctx(cog, 9, owner=True, command=cog.announce_setup)
        ctx.guild = mts
        await cog.announce_setup.callback(cog, ctx)
        assert mts.created == []
        assert support.created[0]["name"] == ROLE_NAME
        assert support.created[0]["mentionable"] is True
        assert support.created[0]["permissions"].value == 0
        role_id = support.roles[-1].id
        assert ctx.reply.await_args.args[0] == str(role_id)
        stored = json.loads(cog.settings_path.read_text(encoding="utf-8"))
        assert stored["role_id"] == role_id
        assert stored["guild_id"] == support_id
        assert stored["channel_id"] == TEST_CHANNEL_ID

        post = _ctx(cog, 9, owner=True, command=cog.announce, channel=FakeTextChannel(42))
        await cog.announce.callback(cog, post, text="Support-server preview notes.")
        assert announcements.sent == []
        assert len(test.sent) == 1
        content, kwargs = test.sent[0]
        assert content == f"<@&{role_id}>\nSupport-server preview notes."
        assert kwargs["allowed_mentions"].roles == [support.roles[-1]]
        assert [item.custom_id for item in kwargs["view"].children] == [NOTIFY_CUSTOM_ID]

        mts_role = FakeRole(55, ROLE_NAME)
        mts.roles.append(mts_role)
        click_role = FakeRole(99, ROLE_NAME)
        click_guild = FakeGuild(222)
        click_guild.roles.append(click_role)
        member = FakeMember(4, click_guild)
        other = _cog(
            tmp_path / "click",
            settings={"guild_id": GUILD_ID, "role_id": 55, "channel_id": ANNOUNCEMENTS_CHANNEL_ID},
            guild=mts,
        )
        interaction = _interaction(other, user=member, guild=click_guild)
        await other.handle_notify(interaction)
        assert member.added[0][0] == (click_role,)
        assert member.added[0][1] == "Updates notify on"
        assert mts_role not in member.roles

        by_id = FakeRole(77, ROLE_NAME)
        id_guild = FakeGuild(333)
        id_guild.roles.append(by_id)
        person = FakeMember(6, id_guild)
        id_cog = _cog(
            tmp_path / "click-id",
            settings={"guild_id": GUILD_ID, "role_id": 77},
            guild=mts,
        )
        hit = _interaction(id_cog, user=person, guild=id_guild)
        await id_cog.handle_notify(hit)
        assert person.added[0][0] == (by_id,)

    asyncio.run(run())
