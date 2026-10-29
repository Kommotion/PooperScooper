"""Grok PS-REMIND blocks become normal reminders."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.reminders import (
    GROK_REMIND_CREATORS,
    WEBHOOK_UNTRUSTED_SECONDS,
    CapError,
    CancelButton,
    CancelView,
    NotYourReminder,
    RemindMeButton,
    RemindMeView,
    Reminders,
    SnoozeButton,
    SnoozeView,
    allowed_mentions_for,
    render_confirmation,
    render_grok_private_confirmation,
)
from cogs.utils.grok_refs import (
    BLOCK_FIELD_NAME,
    BlockError,
    GrokRefs,
    parse_remind_block,
)
from cogs.utils.remind_parse import DEFAULT_TIMEZONE, ParsedWhen, parse_when

NICK = 87324314084331520
BOT_ID = 999
APP_ID = 333
CHANNEL_ID = 555
GUILD_ID = 42
SECRET = "xylophone-secret-phrase"


class _HTTPResponse:
    status = 404
    reason = "Not Found"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _field(name: str, value: str):
    return SimpleNamespace(name=name, value=value)


def _embeds(*fields):
    return [SimpleNamespace(fields=list(fields))]


def _base_block(**overrides) -> dict:
    data = {
        "ref": "4444",
        "creator_id": str(NICK),
        "guild_id": "42",
        "channel_id": "555",
        "when_utc": "2026-10-26T16:00:00Z",
        "text": "hello",
    }
    data.update(overrides)
    return data


def _parse(value):
    return parse_remind_block(_embeds(_field(BLOCK_FIELD_NAME, value)))


def _rejects(value: str, reason: str) -> None:
    with pytest.raises(BlockError) as caught:
        _parse(value)
    assert caught.value.reason == reason


class Chan:
    def __init__(self, channel_id: int = CHANNEL_ID):
        self.id = channel_id
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class Guild:
    def __init__(self, guild_id: int = GUILD_ID):
        self.id = guild_id
        self.cached: dict[int, object] = {}
        self.remote: dict[int, object] = {}
        self.fetch_calls: list[int] = []

    def get_member(self, user_id: int):
        return self.cached.get(user_id)

    async def fetch_member(self, user_id: int):
        self.fetch_calls.append(user_id)
        member = self.remote.get(user_id)
        if member is None:
            raise discord.NotFound(_HTTPResponse(), "missing")
        return member


class Hook:
    def __init__(self, *, token="hook-token", application_id=APP_ID):
        self.token = token
        if application_id is not None:
            self.application_id = application_id
        self.edits: list[tuple] = []

    async def edit_message(self, message_id, **kwargs):
        self.edits.append((message_id, kwargs))


class Bot:
    def __init__(self):
        self.user = SimpleNamespace(id=BOT_ID)
        self.application_id = APP_ID
        self.hooks: dict[int, Hook] = {}
        self.fetch_calls: list[int] = []
        self.fetch_error: BaseException | None = None
        self.raw_calls: list[int] = []
        self.raw_webhook: dict | None = None
        self.dynamic_items: list = []
        self.cogs: dict = {}

    async def fetch_webhook(self, webhook_id):
        self.fetch_calls.append(webhook_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.hooks[webhook_id]

    async def get_webhook(self, webhook_id):
        self.raw_calls.append(webhook_id)
        return self.raw_webhook

    @property
    def http(self):
        return self

    async def wait_until_ready(self):
        return None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *items):
        self.dynamic_items.extend(items)

    def remove_dynamic_items(self, *items):
        for item in items:
            if item in self.dynamic_items:
                self.dynamic_items.remove(item)


class FakeMessage:
    def __init__(
        self,
        *,
        embeds,
        author_id: int,
        channel: Chan,
        guild: Guild | None,
        message_id: int = 100,
        webhook_id: int | None = None,
        metadata=None,
        edit_error: BaseException | None = None,
    ):
        self.embeds = embeds
        self.author = SimpleNamespace(id=author_id)
        self.id = message_id
        self.channel = channel
        self.guild = guild
        self.guild_id = None if guild is None else guild.id
        self.channel_id = channel.id
        self.webhook_id = webhook_id
        self.interaction_metadata = metadata
        self.edits: list[dict] = []
        self.edit_error = edit_error

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.edit_error is not None:
            raise self.edit_error

    def to_reference(self, *, fail_if_not_exists: bool = True):
        return discord.MessageReference(
            message_id=self.id,
            channel_id=self.channel.id,
            guild_id=self.guild_id,
            fail_if_not_exists=fail_if_not_exists,
        )


def _world(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(discord.utils, "utcnow", lambda: moment)
    bot = Bot()
    cog = Reminders(bot, path=tmp_path / "reminders.json", refs_path=tmp_path / "grok_refs.json")
    return SimpleNamespace(
        moment=moment,
        bot=bot,
        cog=cog,
        channel=Chan(),
        guild=Guild(),
        refs_path=tmp_path / "grok_refs.json",
    )


def _record(world, ref: str = "4444", *, created_at=None, now=None, user_id=NICK, channel_id=CHANNEL_ID, guild_id=GUILD_ID):
    created = world.moment if created_at is None else created_at
    GrokRefs(world.refs_path).record(
        ref,
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        created_at=created,
        now=world.moment if now is None else now,
    )


def _payload(world, **overrides) -> dict:
    data = {
        "ref": "4444",
        "creator_id": str(NICK),
        "guild_id": str(GUILD_ID),
        "channel_id": str(CHANNEL_ID),
        "when_utc": (world.moment + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "private": False,
        "text": "take out the trash",
    }
    data.update(overrides)
    return data


def _block_embed(payload: dict, *, fence: bool = False) -> discord.Embed:
    body = json.dumps(payload)
    if fence:
        body = f"```json\n{body}\n```"
    embed = discord.Embed(title="Grok", description="answer")
    embed.add_field(name=BLOCK_FIELD_NAME, value=body, inline=False)
    return embed


def _message(world, payload: dict, **kwargs) -> FakeMessage:
    metadata = kwargs.pop("metadata", "nick")
    if metadata == "nick":
        metadata = SimpleNamespace(id=1, user=SimpleNamespace(id=NICK))
    guild = kwargs.pop("guild", world.guild)
    channel = kwargs.pop("channel", world.channel)
    return FakeMessage(
        embeds=[_block_embed(payload, fence=kwargs.pop("fence", False))],
        author_id=kwargs.pop("author_id", BOT_ID),
        channel=channel,
        guild=guild,
        metadata=metadata,
        **kwargs,
    )


def _info(caplog, prefix: str) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name == "cogs.reminders" and record.message.startswith(prefix)
    ]


def _button_interaction(user_id: int, client):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        user=SimpleNamespace(id=user_id, name="nick"),
        channel_id=CHANNEL_ID,
        guild_id=GUILD_ID,
        client=client,
    )


def test_allowlist_is_nick():
    assert GROK_REMIND_CREATORS == {NICK}


def test_parse_public_private_fence_and_missing_field():
    public = _parse(json.dumps(_base_block(creator_id=NICK, mention_ids=["8", 9])))
    assert public is not None
    assert public.ref == "4444"
    assert public.creator_id == NICK
    assert public.guild_id == 42
    assert public.channel_id == 555
    assert public.when_utc == datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
    assert public.repeat is None
    assert public.private is False
    assert public.text == "hello"
    assert public.mention_ids == (8, 9)
    assert public.exposed_private_text is False

    private = _parse(json.dumps(_base_block(
        guild_id=None,
        private=True,
        text=None,
        text_b64=_b64("hidden note"),
    )))
    assert private is not None
    assert private.private is True
    assert private.text == "hidden note"
    assert private.guild_id is None
    assert private.exposed_private_text is False
    assert private.mention_ids == ()

    fenced = "```json\n" + json.dumps(_base_block(text="fenced")) + "\n```"
    fenced_block = _parse(fenced)
    assert fenced_block is not None
    assert fenced_block.text == "fenced"

    assert parse_remind_block(_embeds(_field("Note", "nope"))) is None
    assert parse_remind_block([]) is None
    assert parse_remind_block(None) is None

    exposed = _parse(json.dumps(_base_block(private=True, text="visible secret")))
    assert exposed is not None
    assert exposed.exposed_private_text is True
    assert exposed.text == "visible secret"


def test_parse_rejects_malformed_blocks():
    _rejects("{", "bad JSON")
    _rejects(json.dumps([1, 2]), "bad JSON")
    missing = _base_block()
    del missing["creator_id"]
    _rejects(json.dumps(missing), "missing keys")
    no_time = _base_block()
    del no_time["when_utc"]
    _rejects(json.dumps(no_time), "missing keys")
    _rejects(json.dumps(_base_block(ref="nope")), "bad id")
    _rejects(json.dumps(_base_block(creator_id=True)), "bad id")
    _rejects(json.dumps(_base_block(channel_id=0)), "bad id")
    _rejects(json.dumps(_base_block(guild_id=-1)), "bad id")
    _rejects(json.dumps(_base_block(text="hi", text_b64=_b64("hi"))), "both text fields")
    neither = _base_block()
    del neither["text"]
    _rejects(json.dumps(neither), "neither text field")
    _rejects(json.dumps(_base_block(text=None, text_b64="!!!")), "bad text_b64")
    _rejects(json.dumps(_base_block(private="yes")), "bad private")
    _rejects(json.dumps(_base_block(mention_ids="8")), "bad mention_ids")


def test_parse_repeat_ignores_when_utc():
    body = _base_block(repeat="every Monday at 9am", when_utc="not-a-time")
    block = _parse(json.dumps(body))
    assert block is not None
    assert block.repeat == "every Monday at 9am"
    assert block.when_utc is None

    blank = _base_block(repeat="  ")
    assert _parse(json.dumps(blank)).repeat is None
    _rejects(json.dumps(_base_block(repeat=3)), "bad time")


def test_consumed_ref_survives_reload_and_rerecord(tmp_path):
    moment = datetime.now(timezone.utc)
    path = tmp_path / "grok_refs.json"
    refs = GrokRefs(path)
    refs.record("4444", user_id=NICK, channel_id=CHANNEL_ID, guild_id=GUILD_ID, created_at=moment, now=moment)
    assert refs.mark_consumed("4444", now=moment) is True
    assert refs.mark_consumed("4444", now=moment) is False

    again = GrokRefs(path)
    assert again.refs["4444"].consumed is True
    assert again.prepare("4444", user_id=NICK, channel_id=CHANNEL_ID, now=moment) == "consumed"
    again.record("4444", user_id=1, channel_id=2, guild_id=3, created_at=moment, now=moment)
    kept = GrokRefs(path)
    assert kept.refs["4444"].consumed is True
    assert kept.refs["4444"].user_id == NICK
    assert kept.refs["4444"].channel_id == CHANNEL_ID

    old = moment - timedelta(hours=25)
    refs.record("9", user_id=1, channel_id=2, guild_id=None, created_at=old, now=old)
    assert "9" in json.loads(path.read_text(encoding="utf-8"))["refs"]
    assert "9" not in GrokRefs(path).refs


def test_trust_checks(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        _record(world, "1001")
        _record(world, "1002")
        _record(world, "1003")
        _record(world, "1004")
        _record(world, "1005")
        _record(world, "1007")

        stranger = _message(
            world,
            _payload(world, ref="1001"),
            author_id=5,
            message_id=1,
            metadata=None,
        )
        await world.cog.on_message(stranger)

        foreign = Hook(application_id=111)
        world.bot.hooks[888] = foreign
        foreign_message = _message(
            world,
            _payload(world, ref="1002"),
            author_id=50,
            webhook_id=888,
            message_id=2,
            metadata=None,
        )
        await world.cog.on_message(foreign_message)

        not_nick = _message(
            world,
            _payload(world, ref="1003", creator_id="5"),
            message_id=3,
            metadata=SimpleNamespace(id=1, user=SimpleNamespace(id=5)),
        )
        await world.cog.on_message(not_nick)

        mismatch = _message(
            world,
            _payload(world, ref="1004"),
            message_id=4,
            metadata=SimpleNamespace(id=1, user=SimpleNamespace(id=8)),
        )
        await world.cog.on_message(mismatch)

        wrong_channel = _message(
            world,
            _payload(world, ref="1005", channel_id=str(CHANNEL_ID + 1)),
            message_id=5,
        )
        await world.cog.on_message(wrong_channel)

        unknown = _message(world, _payload(world, ref="1006"), message_id=6)
        await world.cog.on_message(unknown)

        later = world.moment + timedelta(hours=25)
        monkeypatch.setattr(discord.utils, "utcnow", lambda: later)
        expired = _message(
            world,
            _payload(
                world,
                ref="1007",
                when_utc=(later + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
            message_id=7,
        )
        await world.cog.on_message(expired)

        assert world.cog.store.active_for(NICK) == []
        assert stranger.edits == []
        assert foreign_message.edits == []
        assert foreign.edits == []
        texts = [text for text, _kwargs in world.channel.sent]
        assert texts == [
            "Couldn't set that reminder: creator not allowed",
            "Couldn't set that reminder: interaction user mismatch",
            "Couldn't set that reminder: channel mismatch",
            "Couldn't set that reminder: This reminder request expired or was already used.",
            "Couldn't set that reminder: This reminder request expired or was already used.",
        ]

        reasons = [line.split("reason=", 1)[1] for line in _info(caplog, "grok remind rejected ")]
        assert "untrusted source" in reasons
        assert reasons.count("untrusted source") == 2
        assert "creator not allowed" in reasons
        assert "interaction user mismatch" in reasons
        assert "channel mismatch" in reasons
        assert "unknown ref" in reasons
        assert "expired ref" in reasons
        assert all(kwargs["allowed_mentions"].to_dict().get("users", []) == [] for _text, kwargs in world.channel.sent)

    with caplog.at_level(logging.INFO, logger="cogs.reminders"):
        asyncio.run(scenario())


def test_same_message_and_ref_are_idempotent(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        _record(world)
        message = _message(world, _payload(world), message_id=100, fence=True)
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            await world.cog.on_message(message)
            await world.cog.on_raw_message_edit(SimpleNamespace(message=message))
            await world.cog.on_raw_message_edit(SimpleNamespace(message=None))

        stored = world.cog.store.active_for(NICK)
        assert len(stored) == 1
        reminder = stored[0]
        assert reminder.channel_id == CHANNEL_ID
        assert reminder.guild_id == GUILD_ID
        assert reminder.creator_id == NICK
        assert reminder.text == "take out the trash"
        assert reminder.private is False
        assert reminder.next_fire_at == world.moment + timedelta(hours=2)
        assert reminder.timezone == DEFAULT_TIMEZONE
        assert len(world.channel.sent) == 1
        content, kwargs = world.channel.sent[0]
        assert content == render_confirmation(reminder)
        assert kwargs["mention_author"] is False
        assert kwargs["reference"].fail_if_not_exists is False
        assert kwargs["reference"].message_id == 100
        assert isinstance(kwargs["view"], RemindMeView)
        custom_ids = [child.custom_id for child in kwargs["view"].children]
        assert custom_ids == [f"remind:join:{reminder.id}", f"remind:cancel:{reminder.id}"]
        assert kwargs["allowed_mentions"].to_dict()["users"] == [NICK]
        assert len(message.edits) == 1
        edited = message.edits[0]["embeds"]
        assert edited[0].description == "answer"
        assert all(field.name != BLOCK_FIELD_NAME for field in edited[0].fields)
        assert any(field.name == BLOCK_FIELD_NAME for field in message.embeds[0].fields)

        set_line = _info(caplog, "grok remind set ")[0]
        assert f"id={reminder.id} user={NICK} guild={GUILD_ID} channel={CHANNEL_ID} message=100 ref=4444 source=interaction" in set_line
        assert "private=False" in set_line
        assert "participants=0" in set_line
        assert "text='take out the trash'" in set_line
        assert _info(caplog, "grok remind rejected ") == [
            f"grok remind rejected user={NICK} channel={CHANNEL_ID} message=100 ref=4444 reason=duplicate"
        ]

        other = _message(world, _payload(world), message_id=101)
        await world.cog.on_message(other)
        assert len(world.cog.store.active_for(NICK)) == 1
        assert len(world.channel.sent) == 2
        assert world.channel.sent[1][0] == "Couldn't set that reminder: This reminder request expired or was already used."
        assert GrokRefs(world.refs_path).refs["4444"].consumed is True

    asyncio.run(scenario())


def test_repeat_phrase_uses_creator_timezone(tmp_path, monkeypatch):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        world.cog.store.set_timezone(NICK, "America/New_York")
        _record(world, "2001")
        payload = _payload(world, ref="2001", repeat="every Monday at 9am")
        del payload["when_utc"]
        await world.cog.on_message(_message(world, payload, message_id=20))
        reminder = world.cog.store.active_for(NICK)[0]
        expected = parse_when("every Monday at 9am", now=world.moment, timezone_name="America/New_York")
        assert reminder.next_fire_at == expected.next_fire_at
        assert reminder.repeat == expected.repeat
        assert reminder.timezone == "America/New_York"

    asyncio.run(scenario())


def test_cap_and_past_time_are_visible(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        for index in range(25):
            world.cog.store.create(
                creator_id=NICK,
                channel_id=CHANNEL_ID,
                guild_id=GUILD_ID,
                text=f"filler {index}",
                parsed=ParsedWhen(next_fire_at=world.moment + timedelta(hours=1), repeat=None, timezone=DEFAULT_TIMEZONE),
                reminder_id=f"{index:08x}",
            )
        _record(world, "3001")
        _record(world, "3002")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            await world.cog.on_message(_message(world, _payload(world, ref="3001", text="one more"), message_id=30))
            past_payload = _payload(
                world,
                ref="3002",
                when_utc=world.moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                text="too late",
            )
            await world.cog.on_message(_message(world, past_payload, message_id=31))

        assert len(world.cog.store.active_for(NICK)) == 25
        assert world.channel.sent[0][0] == f"Couldn't set that reminder: {CapError.message}"
        assert world.channel.sent[1][0] == "Couldn't set that reminder: That time is already in the past."
        assert "reason=cap reached" in _info(caplog, "grok remind rejected ")[0]
        assert "reason=past time" in _info(caplog, "grok remind rejected ")[1]
        refs = GrokRefs(world.refs_path)
        assert refs.refs["3001"].consumed is False
        assert refs.refs["3002"].consumed is False

    asyncio.run(scenario())


def test_private_hides_text_from_confirmation_errors_and_logs(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        _record(world, "4001")
        _record(world, "4002")
        _record(world, "4003")
        _record(world, "4004")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            payload = _payload(world, ref="4001", private=True, text=None, text_b64=_b64(SECRET))
            await world.cog.on_message(_message(world, payload, message_id=40))
            past = _payload(
                world,
                ref="4002",
                private=True,
                text=None,
                text_b64=_b64(SECRET),
                when_utc=world.moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            await world.cog.on_message(_message(world, past, message_id=41))
            mentioned = _payload(
                world,
                ref="4003",
                private=True,
                text=None,
                text_b64=_b64(SECRET),
                mention_ids=["200"],
            )
            await world.cog.on_message(_message(world, mentioned, message_id=42))
            plain = _payload(world, ref="4004", private=True, text=SECRET)
            await world.cog.on_message(_message(world, plain, message_id=43))

        stored = world.cog.store.active_for(NICK)
        assert [item.text for item in stored] == [SECRET, SECRET]
        assert all(item.private for item in stored)
        first, _first_kwargs = world.channel.sent[0]
        assert first == render_grok_private_confirmation(stored[0])
        assert SECRET not in first
        assert isinstance(world.channel.sent[0][1]["view"], CancelView)
        assert not isinstance(world.channel.sent[0][1]["view"], RemindMeView)
        assert [child.custom_id for child in world.channel.sent[0][1]["view"].children] == [
            f"remind:cancel:{stored[0].id}"
        ]
        assert world.channel.sent[1][0] == "Couldn't set that reminder: That time is already in the past."
        assert world.channel.sent[2][0] == "Couldn't set that reminder: private reminders can't include other people"
        assert SECRET not in world.channel.sent[1][0]
        assert SECRET not in world.channel.sent[2][0]
        logged = "\n".join(record.message for record in caplog.records if record.name == "cogs.reminders")
        assert SECRET not in logged
        assert "private=True" in logged
        assert "text_len=" in logged
        assert "exposed=yes" in logged
        assert "reason=private reminders can't include other people" in logged

    asyncio.run(scenario())


def test_cancel_button_only_the_creator(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        world.bot.cogs["Reminders"] = world.cog
        public = world.cog.store.create(
            creator_id=NICK,
            channel_id=CHANNEL_ID,
            guild_id=GUILD_ID,
            text="movie night",
            parsed=ParsedWhen(next_fire_at=world.moment + timedelta(hours=1), repeat=None, timezone=DEFAULT_TIMEZONE),
            reminder_id="abcd1234",
        )
        private = world.cog.store.create(
            creator_id=NICK,
            channel_id=CHANNEL_ID,
            guild_id=GUILD_ID,
            text=SECRET,
            parsed=ParsedWhen(next_fire_at=world.moment + timedelta(hours=2), repeat=None, timezone=DEFAULT_TIMEZONE),
            reminder_id="abcd1235",
            private=True,
        )
        match = CancelButton(public.id).template.fullmatch(f"remind:cancel:{public.id}")
        stranger = _button_interaction(8, world.bot)
        button = await CancelButton.from_custom_id(stranger, None, match)
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            await button.callback(stranger)
            owner = _button_interaction(NICK, world.bot)
            await CancelButton(public.id).callback(owner)
            await CancelButton(private.id).callback(owner)

        stranger_text, stranger_kwargs = stranger.response.send_message.await_args.args[0], stranger.response.send_message.await_args.kwargs
        assert stranger_kwargs["ephemeral"] is True
        assert stranger_text == NotYourReminder.message
        assert world.cog.store.get(public.id) is None
        owner_calls = owner.response.send_message.await_args_list
        public_text = owner_calls[0].args[0]
        private_text = owner_calls[1].args[0]
        assert owner_calls[0].kwargs["ephemeral"] is True
        assert public_text == f"Cancelled reminder `{public.id}`: movie night"
        assert private_text == f"Cancelled reminder `{private.id}`."
        assert SECRET not in private_text
        assert world.cog.store.get(private.id) is None
        logged = "\n".join(record.message for record in caplog.records)
        assert "remind cancel button" in logged
        assert "result=not yours" in logged
        assert "result=cancelled" in logged
        assert SECRET not in logged
        assert button.item.label == "Cancel"
        assert button.item.style is discord.ButtonStyle.danger

    asyncio.run(scenario())


def test_mention_ids_become_participants(tmp_path, monkeypatch):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        world.guild.cached[200] = SimpleNamespace(id=200)
        world.guild.remote[201] = SimpleNamespace(id=201)
        _record(world, "5001")
        _record(world, "5002")
        _record(world, "5003")
        _record(world, "5004")
        payload = _payload(world, ref="5001", mention_ids=[str(NICK), "200", 201, "200"])
        await world.cog.on_message(_message(world, payload, message_id=50))
        reminder = world.cog.store.active_for(NICK)[0]
        assert reminder.participants == [200, 201]
        assert world.guild.fetch_calls == [201]
        allowed = world.channel.sent[0][1]["allowed_mentions"]
        assert allowed.to_dict()["users"] == [NICK, 200, 201]
        assert allowed.to_dict()["users"] == [user.id for user in allowed_mentions_for(reminder).users]

        missing = _payload(world, ref="5002", mention_ids=["404"])
        await world.cog.on_message(_message(world, missing, message_id=51))
        assert world.channel.sent[1][0] == "Couldn't set that reminder: user 404 is not in this server"
        assert len(world.cog.store.active_for(NICK)) == 1

        too_many = _payload(world, ref="5003", mention_ids=[str(300 + index) for index in range(11)])
        await world.cog.on_message(_message(world, too_many, message_id=52))
        assert "at most 10" in world.channel.sent[2][0]

        dm_channel = Chan()
        dm = _payload(world, ref="5004", guild_id=None, mention_ids=["200"])
        await world.cog.on_message(_message(world, dm, message_id=53, guild=None, channel=dm_channel))
        assert dm_channel.sent[0][0] == "Couldn't set that reminder: mention_ids require a server"

    asyncio.run(scenario())


def test_webhook_trust_strip_and_failures(tmp_path, monkeypatch, caplog):
    async def scenario():
        world = _world(tmp_path, monkeypatch)
        for ref in ("6001", "6002", "6003", "6004"):
            _record(world, ref)

        owned = Hook(application_id=APP_ID, token="yes")
        world.bot.hooks[701] = owned
        owned_message = _message(
            world,
            _payload(world, ref="6001"),
            author_id=50,
            webhook_id=701,
            message_id=60,
            metadata=None,
        )

        bare = Hook(application_id=None, token="yes")
        assert not hasattr(bare, "application_id")
        world.bot.hooks[702] = bare
        world.bot.raw_webhook = {"application_id": str(APP_ID)}
        bare_message = _message(
            world,
            _payload(world, ref="6002", text="from the raw webhook"),
            author_id=50,
            webhook_id=702,
            message_id=61,
            metadata=None,
        )

        tokenless = Hook(application_id=APP_ID, token=None)
        world.bot.hooks[703] = tokenless
        tokenless_message = _message(
            world,
            _payload(world, ref="6003", text="no token"),
            author_id=50,
            webhook_id=703,
            message_id=62,
            metadata=None,
        )

        world.bot.fetch_error = RuntimeError("down")
        shortcut = _message(
            world,
            _payload(world, ref="6004", text="app webhook"),
            author_id=50,
            webhook_id=APP_ID,
            message_id=63,
            metadata=None,
        )
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            await world.cog.on_message(shortcut)
            world.bot.fetch_error = None
            await world.cog.on_message(owned_message)
            await world.cog.on_message(bare_message)
            await world.cog.on_message(tokenless_message)
            _record(world, "6005")
            failing = _message(
                world,
                _payload(world, ref="6005", text="edit blows up"),
                message_id=64,
                edit_error=RuntimeError("edit failed"),
            )
            await world.cog.on_message(failing)

        texts = [item.text for item in world.cog.store.active_for(NICK)]
        assert texts == ["app webhook", "take out the trash", "from the raw webhook", "no token", "edit blows up"]
        assert owned_message.edits == []
        assert owned.edits and owned.edits[0][0] == 60
        assert all(field.name != BLOCK_FIELD_NAME for field in owned.edits[0][1]["embeds"][0].fields)
        assert 702 in world.bot.raw_calls
        assert 701 not in world.bot.raw_calls
        assert tokenless.edits == []
        assert failing.edits
        assert any("source=webhook" in line for line in _info(caplog, "grok remind set "))
        assert any("source=interaction" in line for line in _info(caplog, "grok remind set "))
        warnings = [record.message for record in caplog.records if record.levelno >= logging.WARNING]
        assert any("webhook has no token" in message for message in warnings)
        assert any("edit failed" in message for message in warnings)
        assert len(world.channel.sent) == 5

    asyncio.run(scenario())


def test_failed_webhook_fetch_is_retried_after_cache_window(tmp_path, monkeypatch):
    async def scenario():
        assert WEBHOOK_UNTRUSTED_SECONDS == 600
        world = _world(tmp_path, monkeypatch)
        clock = {"now": 1_000.0}
        monkeypatch.setattr("cogs.reminders.time.monotonic", lambda: clock["now"])

        world.bot.fetch_error = RuntimeError("down")
        trusted, webhook = await world.cog._trusted_webhook(801)
        assert trusted is False
        assert webhook is None
        assert world.bot.fetch_calls == [801]

        clock["now"] = 1_000.0 + WEBHOOK_UNTRUSTED_SECONDS - 1
        trusted, webhook = await world.cog._trusted_webhook(801)
        assert trusted is False
        assert webhook is None
        assert world.bot.fetch_calls == [801]

        clock["now"] = 1_000.0 + WEBHOOK_UNTRUSTED_SECONDS
        world.bot.fetch_error = None
        owned = Hook(application_id=APP_ID, token="yes")
        world.bot.hooks[801] = owned
        trusted, webhook = await world.cog._trusted_webhook(801)
        assert trusted is True
        assert webhook is owned
        assert world.bot.fetch_calls == [801, 801]

        clock["now"] = 1_000.0 + WEBHOOK_UNTRUSTED_SECONDS + 86_400
        trusted, webhook = await world.cog._trusted_webhook(801)
        assert trusted is True
        assert webhook is owned
        assert world.bot.fetch_calls == [801, 801]

        foreign = Hook(application_id=111, token="no")
        world.bot.hooks[802] = foreign
        trusted, webhook = await world.cog._trusted_webhook(802)
        assert trusted is False
        assert webhook is None
        assert world.bot.fetch_calls == [801, 801, 802]

        clock["now"] += WEBHOOK_UNTRUSTED_SECONDS - 1
        trusted, webhook = await world.cog._trusted_webhook(802)
        assert trusted is False
        assert world.bot.fetch_calls == [801, 801, 802]

        clock["now"] += 1
        trusted, webhook = await world.cog._trusted_webhook(802)
        assert trusted is False
        assert webhook is None
        assert world.bot.fetch_calls == [801, 801, 802, 802]

    asyncio.run(scenario())


def test_confirmation_views_and_cancel_registration(tmp_path):
    async def scenario():
        view = RemindMeView("abcd1234")
        assert [child.custom_id for child in view.children] == [
            "remind:join:abcd1234",
            "remind:cancel:abcd1234",
        ]
        assert view.children[1].item.label == "Cancel"
        snooze = SnoozeView("abcd1234")
        assert all(not child.custom_id.startswith("remind:cancel:") for child in snooze.children)
        cancel = CancelView("abcd1234")
        assert [child.custom_id for child in cancel.children] == ["remind:cancel:abcd1234"]

        cog, bot = Reminders(Bot(), path=tmp_path / "reminders.json", refs_path=tmp_path / "refs.json"), None
        bot = cog.bot
        await cog.cog_load()
        assert CancelButton in bot.dynamic_items
        assert RemindMeButton in bot.dynamic_items
        assert SnoozeButton in bot.dynamic_items
        await cog.cog_unload()
        assert CancelButton not in bot.dynamic_items
        task = cog.reminder_loop.get_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
