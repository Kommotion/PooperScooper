"""Parsing, storage, and delivery for /remind."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from cogs.reminders import (
    MAX_ACTIVE,
    LOOP_SECONDS,
    CapError,
    NotYourReminder,
    Reminder,
    ReminderNotFound,
    ReminderStore,
    RemindMeButton,
    RemindMeView,
    Reminders,
    SnoozeButton,
    SnoozeView,
    allowed_mentions_for,
)
from cogs.utils.remind_parse import (
    DEFAULT_TIMEZONE,
    EXAMPLES,
    ParsedWhen,
    ParseError,
    RepeatRule,
    describe_repeat,
    parse_when,
)

NOW = datetime(2026, 10, 19, 22, 0, tzinfo=timezone.utc)  # Monday 3:00pm Pacific
LA = DEFAULT_TIMEZONE
NY = "America/New_York"


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _store(tmp_path) -> ReminderStore:
    return ReminderStore(tmp_path / "reminders.json")


def _add(
    store: ReminderStore,
    *,
    creator: int = 1,
    text: str = "feed the cat",
    when: datetime | None = None,
    channel: int = 555,
    guild: int | None = 42,
    participants: list[int] | None = None,
    repeat: RepeatRule | None = None,
    timezone_name: str = LA,
    reminder_id: str | None = None,
    snooze_of: str | None = None,
    private: bool = False,
) -> Reminder:
    return store.create(
        creator_id=creator,
        channel_id=channel,
        guild_id=guild,
        text=text,
        parsed=ParsedWhen(next_fire_at=when or (NOW + timedelta(hours=1)), repeat=repeat, timezone=timezone_name),
        participants=participants,
        reminder_id=reminder_id,
        snooze_of=snooze_of,
        created_at=NOW,
        private=private,
    )


class _HTTPResponse:
    status = 404
    reason = "Not Found"


def _http_error() -> discord.HTTPException:
    return discord.HTTPException(_HTTPResponse(), "missing")


class FakeChannel:
    def __init__(self, *, fail: bool = False, error: BaseException | None = None):
        self.fail = fail
        self.error = error
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content, **kwargs):
        if self.error is not None:
            raise self.error
        if self.fail:
            raise _http_error()
        self.sent.append((content, kwargs))


class FakeBot:
    def __init__(self):
        self.channels: dict[int, FakeChannel] = {}
        self.users: dict[int, FakeChannel] = {}
        self.dynamic_items: list = []
        self.cogs: dict = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise _http_error()
        return channel

    async def fetch_user(self, user_id):
        user = self.users.get(user_id)
        if user is None:
            raise _http_error()
        return user

    def add_dynamic_items(self, *items):
        self.dynamic_items.extend(items)

    def remove_dynamic_items(self, *items):
        for item in items:
            if item in self.dynamic_items:
                self.dynamic_items.remove(item)

    async def wait_until_ready(self):
        return None

    def get_cog(self, name):
        return self.cogs.get(name)


def _interaction(
    user_id: int,
    *,
    channel_id: int = 555,
    guild_id: int | None = 42,
    client=None,
    username: str | None = None,
    guild_name: str | None = None,
    channel_name: str | None = None,
):
    user = SimpleNamespace(id=user_id)
    if username is not None:
        user.name = username
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        user=user,
        channel_id=channel_id,
        guild_id=guild_id,
        client=client or SimpleNamespace(get_cog=lambda name: None),
    )
    if guild_name is not None:
        interaction.guild = SimpleNamespace(id=guild_id, name=guild_name)
    if channel_name is not None:
        interaction.channel = SimpleNamespace(id=channel_id, name=channel_name)
    return interaction


def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(discord.utils, "utcnow", lambda: moment)


def _cog(tmp_path, bot: FakeBot | None = None) -> tuple[Reminders, FakeBot]:
    bot = bot or FakeBot()
    return Reminders(bot, path=tmp_path / "reminders.json"), bot


def _sent(mock: AsyncMock):
    args, kwargs = mock.await_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def test_parse_relative_weekday_tomorrow_and_calendar():
    assert parse_when("in 2h", now=NOW, timezone_name=LA).next_fire_at == _utc(2026, 10, 20, 0)
    assert parse_when("in 2 hours", now=NOW, timezone_name=LA).next_fire_at == _utc(2026, 10, 20, 0)
    assert parse_when("in 30 minutes", now=NOW, timezone_name=LA).next_fire_at == _utc(2026, 10, 19, 22, 30)
    assert parse_when("in 2h30m", now=NOW, timezone_name=LA).next_fire_at == _utc(2026, 10, 20, 0, 30)

    friday = parse_when("Friday 8pm", now=NOW, timezone_name=LA)
    assert friday.repeat is None
    assert friday.next_fire_at == _utc(2026, 10, 24, 3)

    # Friday noon Pacific is still before 8pm, so it stays on that Friday.
    friday_noon = _utc(2026, 10, 23, 19)
    assert parse_when("Friday 8pm", now=friday_noon, timezone_name=LA).next_fire_at == _utc(2026, 10, 24, 3)
    # Friday 9pm Pacific has passed 8pm, so the next Friday is used.
    friday_night = _utc(2026, 10, 24, 4)
    assert parse_when("friday 8PM", now=friday_night, timezone_name=LA).next_fire_at == _utc(2026, 10, 31, 3)

    tomorrow = parse_when("tomorrow at 7", now=NOW, timezone_name=LA)
    assert tomorrow.next_fire_at == _utc(2026, 10, 20, 14)

    october = parse_when("Oct 30 at noon", now=NOW, timezone_name=LA)
    assert october.next_fire_at == _utc(2026, 10, 30, 19)
    # Same calendar day, later clock: don't jump a year.
    assert parse_when("Oct 19 at 8pm", now=NOW, timezone_name=LA).next_fire_at == _utc(2026, 10, 20, 3)


def test_parse_repeats():
    monday = parse_when("every Monday at 9am", now=NOW, timezone_name=LA)
    assert monday.repeat == RepeatRule(kind="weekly", weekday=0, hour=9, minute=0)
    assert monday.next_fire_at == _utc(2026, 10, 26, 16)
    assert describe_repeat(monday.repeat) == "every Monday at 9am"

    daily = parse_when("every day at 8pm", now=NOW, timezone_name=LA)
    assert daily.repeat == RepeatRule(kind="daily", hour=20, minute=0)
    assert daily.next_fire_at == _utc(2026, 10, 20, 3)

    weekly = parse_when("every week", now=NOW, timezone_name=LA)
    assert weekly.repeat == RepeatRule(kind="interval", interval_seconds=7 * 24 * 3600)
    assert weekly.next_fire_at == NOW + timedelta(days=7)

    hours = parse_when("every 2 hours", now=NOW, timezone_name=LA)
    assert hours.repeat is not None
    assert hours.repeat.interval_seconds == 7200
    assert hours.next_fire_at == NOW + timedelta(hours=2)


def test_parse_uses_the_callers_timezone():
    la = parse_when("tomorrow at 7", now=NOW, timezone_name=LA)
    ny = parse_when("tomorrow at 7", now=NOW, timezone_name=NY)
    assert la.next_fire_at == _utc(2026, 10, 20, 14)
    assert ny.next_fire_at == _utc(2026, 10, 20, 11)
    assert la.next_fire_at != ny.next_fire_at


def test_parse_rejects_past_and_nonsense():
    for phrase in ("today at 2pm", "yesterday", "yesterday at 3pm"):
        with pytest.raises(ParseError) as caught:
            parse_when(phrase, now=NOW, timezone_name=LA)
        assert "past" in caught.value.message.lower()
        assert "Friday 8pm" in caught.value.message
        assert EXAMPLES.split("Examples: ", 1)[1].split(",")[0].strip("`") in caught.value.message

    with pytest.raises(ParseError) as bad:
        parse_when("banana smoothie", now=NOW, timezone_name=LA)
    assert "couldn't understand" in bad.value.message
    for example in ("in 2h", "in 30 minutes", "every Monday at 9am", "every day at 8pm", "every week"):
        assert example in bad.value.message


def test_store_round_trip_and_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "reminders.json"
    replaced = {}
    real_replace = os.replace

    def spy(src, dst):
        replaced["src"] = src
        replaced["dst"] = dst
        real_replace(src, dst)

    monkeypatch.setattr("cogs.reminders.os.replace", spy)
    store = _store(tmp_path)
    store.set_timezone(7, "America/New_York")
    repeat = RepeatRule(kind="weekly", weekday=0, hour=9, minute=0)
    created = _add(
        store,
        creator=7,
        text="stand up",
        when=_utc(2026, 10, 26, 13),
        participants=[8, 7, 8],
        repeat=repeat,
        timezone_name=NY,
        reminder_id="abcd1234",
    )
    assert created.participants == [8]
    assert Path(replaced["dst"]) == path
    assert str(replaced["src"]).endswith(".tmp")
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    loaded = ReminderStore(path)
    assert loaded.timezone_for(7) == NY
    assert loaded.timezone_for(1) == LA
    again = loaded.get("abcd1234")
    assert again is not None
    assert again.to_dict() == created.to_dict()
    assert again.repeat == repeat
    assert json.loads(path.read_text(encoding="utf-8"))["reminders"][0]["text"] == "stand up"


def test_missing_and_corrupt_files_start_empty(tmp_path, caplog):
    missing = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING, logger="cogs.reminders"):
        store = ReminderStore(missing)
    assert store.reminders == []
    assert store.timezones == {}
    assert not missing.exists()
    assert any("missing" in record.message.lower() for record in caplog.records)

    caplog.clear()
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cogs.reminders"):
        broken = ReminderStore(corrupt)
    assert broken.reminders == []
    assert any("corrupt" in record.message.lower() for record in caplog.records)
    assert corrupt.read_text(encoding="utf-8") == "{not json"

    array_file = tmp_path / "array.json"
    array_file.write_text("[]", encoding="utf-8")
    assert ReminderStore(array_file).reminders == []

    mixed = tmp_path / "mixed.json"
    kept = Reminder(
        id="abcd1234",
        creator_id=3,
        participants=[4],
        channel_id=555,
        guild_id=42,
        text="keep me",
        next_fire_at=_utc(2026, 10, 20, 1),
        timezone=LA,
        repeat=None,
        created_at=NOW,
    )
    mixed.write_text(
        json.dumps({"timezones": {"3": LA}, "reminders": [{"id": "nope"}, kept.to_dict()]}),
        encoding="utf-8",
    )
    reloaded = ReminderStore(mixed)
    assert [item.id for item in reloaded.reminders] == ["abcd1234"]
    assert reloaded.timezone_for(3) == LA


def test_due_and_overdue_firing(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        channel = FakeChannel()
        bot.channels[555] = channel
        _add(
            cog.store,
            text="on time @everyone <@&55> <@999>",
            when=NOW - timedelta(seconds=30),
            participants=[2],
            reminder_id="aaaa1111",
        )
        _add(
            cog.store,
            text="overdue ping",
            when=NOW - timedelta(hours=2),
            participants=[2],
            reminder_id="bbbb2222",
        )

        fired = await cog.dispatch_due(now=NOW)

        assert {item.reminder.id for item in fired} == {"aaaa1111", "bbbb2222"}
        assert len(channel.sent) == 2
        by_text = {content: kwargs for content, kwargs in channel.sent}
        on_time = next(content for content in by_text if "on time" in content)
        overdue = next(content for content in by_text if "overdue ping" in content)
        assert "This reminder is late." not in on_time
        assert "This reminder is late." in overdue
        assert "<@1>" in overdue and "<@2>" in overdue
        assert cog.store.get("aaaa1111").status == "fired"
        assert cog.store.get("bbbb2222").status == "fired"
        assert await cog.dispatch_due(now=NOW) == []
        assert len(channel.sent) == 2

    asyncio.run(scenario())


def test_repeat_reschedules_once_without_a_burst(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        channel = FakeChannel()
        bot.channels[555] = channel
        daily = RepeatRule(kind="daily", hour=20, minute=0)
        _add(
            cog.store,
            text="evening meds",
            when=_utc(2026, 10, 17, 3),
            repeat=daily,
            reminder_id="cccc3333",
        )
        moment = _utc(2026, 10, 21, 20)
        fired = await cog.dispatch_due(now=moment)
        assert len(fired) == 1
        assert len(channel.sent) == 1
        assert "This reminder is late." in channel.sent[0][0]
        reminder = cog.store.get("cccc3333")
        assert reminder.status == "active"
        assert reminder.next_fire_at == _utc(2026, 10, 22, 3)
        assert await cog.dispatch_due(now=moment) == []
        assert len(channel.sent) == 1

        interval = RepeatRule(kind="interval", interval_seconds=2 * 3600)
        scheduled = _utc(2026, 10, 21, 15)
        _add(
            cog.store,
            text="stretch",
            when=scheduled,
            repeat=interval,
            reminder_id="dddd4444",
        )
        moment = _utc(2026, 10, 21, 20)
        await cog.dispatch_due(now=moment)
        stretch = cog.store.get("dddd4444")
        assert stretch.next_fire_at == scheduled + timedelta(hours=6)
        assert sum(1 for content, _kwargs in channel.sent if "stretch" in content) == 1

    asyncio.run(scenario())


def test_startup_fires_overdue_immediately(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        channel = FakeChannel()
        bot.channels[555] = channel
        overdue_at = discord.utils.utcnow() - timedelta(hours=3)
        _add(cog.store, text="while the bot was off", when=overdue_at, reminder_id="eeee5555")
        await cog.before_reminder_loop()
        assert len(channel.sent) == 1
        assert "This reminder is late." in channel.sent[0][0]
        assert 30 <= LOOP_SECONDS <= 60

    asyncio.run(scenario())


def test_channel_failure_dms_then_drops(tmp_path, caplog):
    async def scenario():
        cog, bot = _cog(tmp_path)
        user = FakeChannel()
        bot.users[1] = user
        _add(cog.store, text="dm me", when=NOW - timedelta(minutes=5), reminder_id="ffff6666")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            await cog.dispatch_due(now=NOW)
        assert len(user.sent) == 1
        assert "DMing you" in user.sent[0][0]
        assert cog.store.get("ffff6666") is not None

        blocked = FakeChannel(fail=True)
        bot.channels[555] = blocked
        bot.users.clear()
        _add(cog.store, text="nowhere to go", when=NOW - timedelta(minutes=5), reminder_id="aaaabbbb")
        with caplog.at_level(logging.WARNING, logger="cogs.reminders"):
            await cog.dispatch_due(now=NOW)
        assert cog.store.get("aaaabbbb") is None
        assert any("Dropping reminder aaaabbbb" in record.message for record in caplog.records)

    asyncio.run(scenario())


def test_per_user_cap(tmp_path):
    store = _store(tmp_path)
    for index in range(MAX_ACTIVE):
        _add(store, reminder_id=f"{index:08x}", text=f"item {index}")
    try:
        _add(store, reminder_id="00abcdef", text="one too many")
        raise AssertionError("cap should have been enforced")
    except CapError as exc:
        assert "25" in exc.message
        assert "/remind cancel" in exc.message
    assert store.active_count(1) == MAX_ACTIVE

    async def command():
        cog, _bot = _cog(tmp_path)
        cog.store = store
        interaction = _interaction(1)
        await cog.remind_set.callback(cog, interaction, "in 2h", "another one")
        content, kwargs = _sent(interaction.response.send_message)
        assert kwargs["ephemeral"] is True
        assert "25" in content
        assert store.active_count(1) == MAX_ACTIVE

    asyncio.run(command())


def test_list_and_cancel_are_limited_to_the_owner(tmp_path):
    async def scenario():
        cog, _bot = _cog(tmp_path)
        _add(
            cog.store,
            creator=1,
            text="my secret task",
            reminder_id="1111aaaa",
            repeat=RepeatRule(kind="daily", hour=20, minute=0),
        )
        _add(cog.store, creator=2, text="their secret task", reminder_id="2222bbbb")

        mine = _interaction(1)
        await cog.remind_list.callback(cog, mine)
        content, kwargs = _sent(mine.response.send_message)
        assert kwargs["ephemeral"] is True
        assert "1111aaaa" in content
        assert "my secret task" in content
        assert "<t:" in content and ":F>" in content and ":R>" in content
        assert "every day at 8pm" in content
        assert "their secret task" not in content
        assert "2222bbbb" not in content

        choices = await cog.remind_cancel_autocomplete(mine, "secret")
        assert [choice.value for choice in choices] == ["1111aaaa"]
        assert await cog.remind_cancel_autocomplete(mine, "their") == []

        stolen = _interaction(2)
        await cog.remind_cancel.callback(cog, stolen, "1111aaaa")
        denial, denial_kwargs = _sent(stolen.response.send_message)
        assert denial_kwargs["ephemeral"] is True
        assert denial == NotYourReminder.message
        assert cog.store.get("1111aaaa") is not None

        await cog.remind_cancel.callback(cog, mine, "1111aaaa")
        cancelled, cancelled_kwargs = _sent(mine.response.send_message)
        assert cancelled_kwargs["ephemeral"] is True
        assert "1111aaaa" in cancelled
        assert cog.store.get("1111aaaa") is None
        assert cog.store.get("2222bbbb") is not None

        missing = _interaction(1)
        await cog.remind_cancel.callback(cog, missing, "deadbeef")
        missing_text, _missing_kwargs = _sent(missing.response.send_message)
        assert missing_text == ReminderNotFound.message

    asyncio.run(scenario())


def test_allowed_mentions_are_only_creator_and_participants(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        channel = FakeChannel()
        bot.channels[555] = channel
        reminder = _add(
            cog.store,
            text="bring snacks @everyone <@&55> <@999>",
            when=NOW - timedelta(minutes=1),
            participants=[2, 2],
            reminder_id="abc12345",
        )
        mentions = allowed_mentions_for(reminder)
        payload = mentions.to_dict()
        assert mentions.everyone is False
        assert mentions.roles is False
        assert payload["users"] == [1, 2]
        assert "everyone" not in payload["parse"]
        assert "roles" not in payload["parse"]
        assert 999 not in payload["users"]

        await cog.dispatch_due(now=NOW)
        content, kwargs = channel.sent[0]
        sent_payload = kwargs["allowed_mentions"].to_dict()
        assert sent_payload["users"] == [1, 2]
        assert "everyone" not in sent_payload["parse"]
        assert "roles" not in sent_payload["parse"]
        assert "@everyone" in content
        assert "<@&55>" in content
        assert "<@999>" in content
        assert "<@1>" in content and "<@2>" in content
        custom_ids = {child.custom_id for child in kwargs["view"].children}
        assert custom_ids == {
            "remind:snooze:abc12345:10m",
            "remind:snooze:abc12345:1h",
            "remind:snooze:abc12345:tomorrow",
        }
        assert kwargs["view"].timeout is None
        assert kwargs["view"].is_persistent()

    asyncio.run(scenario())


def test_set_command_is_public_and_rejects_bad_times(tmp_path, monkeypatch):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, _bot = _cog(tmp_path)
        bad = _interaction(1)
        await cog.remind_set.callback(cog, bad, "not a time", "hello")
        content, kwargs = _sent(bad.response.send_message)
        assert kwargs["ephemeral"] is True
        assert "Friday 8pm" in content and "in 2h" in content
        assert cog.store.reminders == []

        past = _interaction(1)
        await cog.remind_set.callback(cog, past, "today at 2pm", "hello")
        past_text, past_kwargs = _sent(past.response.send_message)
        assert past_kwargs["ephemeral"] is True
        assert "past" in past_text.lower()
        assert "Oct 30 at noon" in past_text

        long_text = _interaction(1)
        await cog.remind_set.callback(cog, long_text, "in 2h", "x" * 501)
        long_message, long_kwargs = _sent(long_text.response.send_message)
        assert long_kwargs["ephemeral"] is True
        assert "500" in long_message

        ok = _interaction(1)
        await cog.remind_set.callback(cog, ok, "in 2h", "  hello @everyone <@&9>  ")
        confirmation, confirm_kwargs = _sent(ok.response.send_message)
        assert confirm_kwargs["ephemeral"] is False
        reminder = cog.store.active_for(1)[0]
        assert reminder.text == "hello @everyone <@&9>"
        assert reminder.private is False
        assert reminder.next_fire_at == NOW + timedelta(hours=2)
        assert reminder.timezone == LA
        assert f"`{reminder.id}`" in confirmation
        assert "<t:" in confirmation
        view = confirm_kwargs["view"]
        assert isinstance(view, RemindMeView)
        assert view.timeout is None
        assert view.is_persistent()
        assert view.children[0].custom_id == f"remind:join:{reminder.id}"
        allowed = confirm_kwargs["allowed_mentions"].to_dict()
        assert allowed["users"] == [1]
        assert "everyone" not in allowed["parse"]
        assert "roles" not in allowed["parse"]

    asyncio.run(scenario())


def test_timezone_command_changes_later_parsing(tmp_path, monkeypatch):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, _bot = _cog(tmp_path)
        bad = _interaction(5)
        await cog.remind_timezone.callback(cog, bad, "Not/AZone")
        message, kwargs = _sent(bad.response.send_message)
        assert kwargs["ephemeral"] is True
        assert "America/Los_Angeles" in message
        assert cog.store.timezone_for(5) == LA

        good = _interaction(5)
        await cog.remind_timezone.callback(cog, good, "america/new_york")
        updated, updated_kwargs = _sent(good.response.send_message)
        assert updated_kwargs["ephemeral"] is True
        assert "America/New_York" in updated
        assert cog.store.timezone_for(5) == NY

        created = _interaction(5)
        await cog.remind_set.callback(cog, created, "tomorrow at 7", "wake up")
        assert cog.store.active_for(5)[0].next_fire_at == _utc(2026, 10, 20, 11)
        assert cog.store.active_for(5)[0].timezone == NY

    asyncio.run(scenario())


def test_remind_me_too_toggles_and_survives_reload(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        reminder = _add(cog.store, reminder_id="abcd1234", text="movie night")
        bot.cogs["Reminders"] = cog

        friend = _interaction(8, client=bot)
        button = await RemindMeButton.from_custom_id(
            friend,
            None,
            RemindMeButton("abcd1234").template.fullmatch("remind:join:abcd1234"),
        )
        await button.callback(friend)
        added, added_kwargs = _sent(friend.response.send_message)
        assert added_kwargs["ephemeral"] is True
        assert "ping you too" in added
        assert cog.store.get("abcd1234").participants == [8]

        reloaded, reloaded_bot = _cog(tmp_path)
        reloaded_bot.cogs["Reminders"] = reloaded
        again = _interaction(8, client=reloaded_bot)
        restored = await RemindMeButton.from_custom_id(
            again,
            None,
            button.template.fullmatch(button.custom_id),
        )
        await restored.callback(again)
        removed, removed_kwargs = _sent(again.response.send_message)
        assert removed_kwargs["ephemeral"] is True
        assert "off this reminder" in removed
        assert reloaded.store.get("abcd1234").participants == []

        owner = _interaction(1, client=reloaded_bot)
        await RemindMeButton(reminder.id).callback(owner)
        stayed, _stayed_kwargs = _sent(owner.response.send_message)
        assert "you stay on it" in stayed.lower()
        assert reloaded.store.get("abcd1234").participants == []
        assert reloaded.store.get("abcd1234").creator_id == 1

        view = RemindMeView("abcd1234")
        assert view.timeout is None
        assert view.is_persistent()
        assert view.children[0].custom_id == "remind:join:abcd1234"

    asyncio.run(scenario())


def test_snooze_creates_or_reschedules_a_personal_one_off(tmp_path):
    source_id = "abc12345"
    store = _store(tmp_path)
    repeat = RepeatRule(kind="daily", hour=20, minute=0)
    original_when = _utc(2026, 10, 20, 3)
    _add(
        store,
        text="stretch",
        when=original_when,
        repeat=repeat,
        participants=[8],
        reminder_id=source_id,
    )

    try:
        store.snooze(source_id, 99, "10m", NOW)
        raise AssertionError("outsider should not snooze")
    except Exception as exc:
        assert "Only people on this reminder" in str(exc) or getattr(exc, "message", "")
    assert len(store.reminders) == 1

    action, child = store.snooze(source_id, 8, "10m", NOW)
    assert action == "created"
    assert child.id != source_id
    assert child.creator_id == 8
    assert child.participants == []
    assert child.repeat is None
    assert child.snooze_of == source_id
    assert child.next_fire_at == NOW + timedelta(minutes=10)
    assert store.get(source_id).next_fire_at == original_when
    assert store.get(source_id).repeat == repeat

    action, updated = store.snooze(source_id, 8, "1h", NOW)
    assert action == "rescheduled"
    assert updated.id == child.id
    assert updated.next_fire_at == NOW + timedelta(hours=1)
    assert sum(1 for item in store.reminders if item.snooze_of == source_id) == 1

    one_off = _add(store, creator=4, text="once", when=NOW + timedelta(hours=3), reminder_id="4444cccc")
    one_off.status = "fired"
    store.save()
    action, rescheduled = store.snooze("4444cccc", 4, "tomorrow", NOW)
    assert action == "rescheduled"
    assert rescheduled.id == "4444cccc"
    assert rescheduled.status == "active"
    assert rescheduled.repeat is None
    # Tomorrow 9:00am Pacific.
    assert rescheduled.next_fire_at == _utc(2026, 10, 20, 16)

    async def button_path():
        cog, bot = _cog(tmp_path)
        cog.store = store
        bot.cogs["Reminders"] = cog
        interaction = _interaction(8, client=bot)
        _freeze_local = NOW
        # The handler reads discord.utils.utcnow, so pin it for this call.
        original = discord.utils.utcnow
        discord.utils.utcnow = lambda: _freeze_local
        try:
            button = await SnoozeButton.from_custom_id(
                interaction,
                None,
                SnoozeButton("abc12345", "10m").template.fullmatch("remind:snooze:abc12345:10m"),
            )
            await button.callback(interaction)
        finally:
            discord.utils.utcnow = original
        message, kwargs = _sent(interaction.response.send_message)
        assert kwargs["ephemeral"] is True
        assert message.startswith("Snooze updated")
        assert store.get(child.id).next_fire_at == NOW + timedelta(minutes=10)
        view = SnoozeView(source_id)
        assert view.timeout is None
        assert view.is_persistent()

    asyncio.run(button_path())


def test_commands_are_a_global_remind_group():
    group = Reminders.remind_group
    assert group.name == "remind"
    assert group.guild_only is False
    assert group.default_permissions is None
    assert group._guild_ids is None
    assert sorted(command.name for command in group.commands) == ["cancel", "list", "set", "timezone"]
    assert 30 <= Reminders.reminder_loop.seconds <= 60

    import PooperScooper

    assert "cogs.reminders" in PooperScooper.initial_extensions


def test_cog_load_registers_persistent_items(tmp_path):
    async def scenario():
        cog, bot = _cog(tmp_path)
        await cog.cog_load()
        try:
            assert RemindMeButton in bot.dynamic_items
            assert SnoozeButton in bot.dynamic_items
            assert cog.reminder_loop.is_running()
        finally:
            cog.reminder_loop.cancel()
            task = cog.reminder_loop.get_task()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_private_confirmation_is_ephemeral_without_button(tmp_path, monkeypatch):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, _bot = _cog(tmp_path)
        private = _interaction(1)
        await cog.remind_set.callback(cog, private, "in 2h", "hidden task", True)
        content, kwargs = _sent(private.response.send_message)
        assert kwargs["ephemeral"] is True
        assert "view" not in kwargs
        assert "hidden task" in content
        stored = cog.store.active_for(1)[0]
        assert stored.private is True
        assert stored.text == "hidden task"

        public = _interaction(1)
        await cog.remind_set.callback(cog, public, "in 3h", "visible task")
        _public_text, public_kwargs = _sent(public.response.send_message)
        assert public_kwargs["ephemeral"] is False
        assert isinstance(public_kwargs["view"], RemindMeView)
        assert cog.store.get(cog.store.active_for(1)[1].id).private is False

        listed = _interaction(1)
        await cog.remind_list.callback(cog, listed)
        listing, list_kwargs = _sent(listed.response.send_message)
        assert list_kwargs["ephemeral"] is True
        hidden = next(line for line in listing.splitlines() if "hidden task" in line)
        visible = next(line for line in listing.splitlines() if "visible task" in line)
        assert "once (private)" in hidden
        assert "missed" not in hidden
        assert "(private)" not in visible

    asyncio.run(scenario())


def test_private_flag_round_trips_and_defaults(tmp_path):
    store = _store(tmp_path)
    created = _add(store, private=True, text="quiet", participants=[8], reminder_id="abcd1234")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["reminders"][0]["private"] is True
    loaded = ReminderStore(store.path).get("abcd1234")
    assert loaded is not None
    assert loaded.private is True
    assert loaded.missed_ids == []
    assert loaded.missed_at is None
    assert loaded.to_dict() == created.to_dict()

    public = _add(store, private=False, text="loud", reminder_id="bbbb2222")
    assert public.private is False
    again = ReminderStore(store.path).get("bbbb2222")
    assert again is not None
    assert again.private is False
    assert again.to_dict()["private"] is False

    legacy_path = tmp_path / "legacy.json"
    payload = created.to_dict()
    del payload["private"]
    payload["id"] = "abcd1235"
    payload.pop("missed_ids", None)
    payload.pop("missed_at", None)
    legacy_path.write_text(json.dumps({"reminders": [payload]}), encoding="utf-8")
    legacy = ReminderStore(legacy_path).get("abcd1235")
    assert legacy is not None
    assert legacy.private is False
    assert legacy.missed_ids == []
    assert legacy.missed_at is None

    action, child = store.snooze("abcd1234", 8, "1h", NOW)
    assert action == "created"
    assert child.private is True
    assert child.id != "abcd1234"
    assert ReminderStore(store.path).get(child.id).private is True
    assert store.get("abcd1234").private is True


def test_private_reminder_dms_each_person_with_snooze(tmp_path, monkeypatch):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, bot = _cog(tmp_path)
        creator = FakeChannel()
        friend = FakeChannel()
        channel = FakeChannel()
        bot.users[1] = creator
        bot.users[8] = friend
        bot.channels[555] = channel
        bot.cogs["Reminders"] = cog
        _add(
            cog.store,
            text="unique-secret-phrase",
            when=NOW - timedelta(minutes=1),
            participants=[8],
            private=True,
            reminder_id="abc12345",
        )

        await cog.dispatch_due(now=NOW)

        assert channel.sent == []
        assert len(creator.sent) == 1
        assert len(friend.sent) == 1
        expected_buttons = {
            "remind:snooze:abc12345:10m",
            "remind:snooze:abc12345:1h",
            "remind:snooze:abc12345:tomorrow",
        }
        for target in (creator, friend):
            content, kwargs = target.sent[0]
            assert "unique-secret-phrase" in content
            assert kwargs["view"].timeout is None
            assert kwargs["view"].is_persistent()
            assert {child.custom_id for child in kwargs["view"].children} == expected_buttons
        stored = cog.store.get("abc12345")
        assert stored is not None
        assert stored.status == "fired"
        assert stored.private is True
        assert stored.missed_ids == []

        interaction = _interaction(1, client=bot)
        button = await SnoozeButton.from_custom_id(
            interaction,
            None,
            SnoozeButton("abc12345", "10m").template.fullmatch("remind:snooze:abc12345:10m"),
        )
        await button.callback(interaction)
        message, snooze_kwargs = _sent(interaction.response.send_message)
        assert snooze_kwargs["ephemeral"] is True
        assert message.startswith("Snooze updated")
        rescheduled = cog.store.get("abc12345")
        assert rescheduled.private is True
        assert rescheduled.status == "active"

        await cog.dispatch_due(now=NOW + timedelta(minutes=10))
        assert len(creator.sent) == 2
        assert "unique-secret-phrase" in creator.sent[1][0]
        assert {child.custom_id for child in creator.sent[1][1]["view"].children} == expected_buttons
        assert channel.sent == []

    asyncio.run(scenario())


def test_private_dm_failure_falls_back_without_reminder_text(tmp_path, monkeypatch):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, bot = _cog(tmp_path)
        creator = FakeChannel(error=discord.Forbidden(_HTTPResponse(), "DMs closed"))
        broken = FakeChannel(error=_http_error())
        friend = FakeChannel()
        channel = FakeChannel()
        bot.users[1] = creator
        bot.users[2] = broken
        bot.users[3] = friend
        bot.channels[555] = channel
        secret = "unique-secret-phrase-" + ("n" * 100)
        _add(
            cog.store,
            text=secret,
            when=NOW - timedelta(minutes=1),
            participants=[2, 3],
            private=True,
            reminder_id="abcd9999",
        )

        await cog.dispatch_due(now=NOW)

        assert creator.sent == []
        assert broken.sent == []
        assert len(friend.sent) == 1
        assert secret in friend.sent[0][0]
        assert {child.custom_id for child in friend.sent[0][1]["view"].children} == {
            "remind:snooze:abcd9999:10m",
            "remind:snooze:abcd9999:1h",
            "remind:snooze:abcd9999:tomorrow",
        }
        assert len(channel.sent) == 1
        fallback, kwargs = channel.sent[0]
        assert fallback == (
            "<@1> <@2>, I couldn't DM you your private reminder. "
            "Turn on DMs, or use `/remind list` to see it."
        )
        assert secret not in fallback
        assert "unique-secret-phrase" not in fallback
        assert "<@3>" not in fallback
        assert "view" not in kwargs
        payload = kwargs["allowed_mentions"].to_dict()
        assert payload["users"] == [1, 2]
        assert 3 not in payload["users"]
        assert "everyone" not in payload["parse"]
        assert "roles" not in payload["parse"]

        stored = cog.store.get("abcd9999")
        assert stored is not None
        assert stored.status == "fired"
        assert stored.missed_ids == [1, 2]

        for user_id in (1, 2):
            interaction = _interaction(user_id)
            await cog.remind_list.callback(cog, interaction)
            content, list_kwargs = _sent(interaction.response.send_message)
            assert list_kwargs["ephemeral"] is True
            assert secret in content
            assert "missed (private)" in content
            assert "abcd9999" in content

        spared = _interaction(3)
        await cog.remind_list.callback(cog, spared)
        spared_content, _spared_kwargs = _sent(spared.response.send_message)
        assert secret not in spared_content
        assert "abcd9999" not in spared_content

        _freeze(monkeypatch, NOW + timedelta(hours=24) - timedelta(seconds=1))
        still = _interaction(1)
        await cog.remind_list.callback(cog, still)
        still_content, _still_kwargs = _sent(still.response.send_message)
        assert secret in still_content
        assert "missed (private)" in still_content
        assert cog.store.get("abcd9999") is not None

        _freeze(monkeypatch, NOW + timedelta(hours=24))
        gone = _interaction(1)
        await cog.remind_list.callback(cog, gone)
        gone_content, _gone_kwargs = _sent(gone.response.send_message)
        assert secret not in gone_content
        assert "abcd9999" not in gone_content
        assert "missed (private)" not in gone_content
        assert cog.store.get("abcd9999") is None

    asyncio.run(scenario())


def test_private_dm_and_channel_failure_drops(tmp_path, caplog):
    async def scenario():
        cog, bot = _cog(tmp_path)
        bot.users[1] = FakeChannel(error=discord.Forbidden(_HTTPResponse(), "DMs closed"))
        _add(
            cog.store,
            text="nowhere-secret",
            when=NOW - timedelta(minutes=1),
            private=True,
            reminder_id="ffff7777",
        )
        with caplog.at_level(logging.WARNING, logger="cogs.reminders"):
            await cog.dispatch_due(now=NOW)
        assert cog.store.get("ffff7777") is None
        assert any("Dropping reminder ffff7777" in record.message for record in caplog.records)

        caplog.clear()
        bot.users[1] = FakeChannel(fail=True)
        bot.channels[777] = FakeChannel(fail=True)
        _add(
            cog.store,
            text="still-nowhere",
            when=NOW - timedelta(minutes=1),
            channel=777,
            private=True,
            reminder_id="ffff7778",
        )
        with caplog.at_level(logging.WARNING, logger="cogs.reminders"):
            await cog.dispatch_due(now=NOW)
        assert cog.store.get("ffff7778") is None
        assert bot.channels[777].sent == []
        assert any("Dropping reminder ffff7778" in record.message for record in caplog.records)

    asyncio.run(scenario())


class _NamedTarget(FakeChannel):
    def __init__(self, *, name: str, message_id: int, fail: bool = False, error: BaseException | None = None):
        super().__init__(fail=fail, error=error)
        self.name = name
        self.message_id = message_id

    async def send(self, content, **kwargs):
        if self.error is not None:
            raise self.error
        if self.fail:
            raise _http_error()
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=self.message_id)


def _logged(caplog, *, level: int | None = None) -> str:
    chunks: list[str] = []
    for record in caplog.records:
        if record.name != "cogs.reminders":
            continue
        if level is not None and record.levelno != level:
            continue
        chunks.append(record.getMessage())
        chunks.append(repr(record.args))
        chunks.append(str(record.msg))
    return "\n".join(chunks)


def test_remind_set_is_logged_and_private_text_is_hidden(tmp_path, monkeypatch, caplog):
    async def scenario():
        _freeze(monkeypatch, NOW)
        cog, _bot = _cog(tmp_path)
        secret = "private-secret-do-not-log"
        interaction = _interaction(7, username="ada", guild_name="Pooper Guild", channel_name="general")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.remind_set.callback(cog, interaction, "in 2h", secret, True)
        blob = _logged(caplog)
        assert secret not in blob
        set_records = [
            record
            for record in caplog.records
            if record.name == "cogs.reminders" and record.getMessage().startswith("remind set ")
        ]
        assert len(set_records) == 1
        assert set_records[0].levelno == logging.INFO
        line = set_records[0].getMessage()
        assert "\n" not in line
        reminder = cog.store.active_for(7)[0]
        assert f"id={reminder.id}" in line
        assert "user=7" in line
        assert "username=ada" in line
        assert "guild=42" in line
        assert "guild_name=Pooper Guild" in line
        assert "channel=555" in line
        assert "channel_name=general" in line
        assert "when='in 2h'" in line
        assert "private=True" in line
        assert f"text_len={len(secret)}" in line
        assert "text=" not in line
        assert "utc=2026-10-20T00:00:00+00:00" in line
        assert "local=2026-10-19T17:00:00-07:00" in line
        assert "repeat=once" in line

        public = ("P" * 80) + "TAIL"
        public_interaction = _interaction(8, username="bea", guild_name="Pooper Guild", channel_name="general")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.remind_set.callback(cog, public_interaction, "in 3h", public, False)
        public_records = [
            record
            for record in caplog.records
            if record.name == "cogs.reminders"
            and record.getMessage().startswith("remind set ")
            and "rejected" not in record.getMessage()
        ]
        assert len(public_records) == 1
        public_line = public_records[0].getMessage()
        assert "\n" not in public_line
        assert ("P" * 80) in public_line
        assert "TAIL" not in public_line
        assert "text_len=84" in public_line
        assert "private=False" in public_line

        past_secret = "past-secret-do-not-log"
        past = _interaction(8, username="bea", guild_name="Pooper Guild", channel_name="general")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.remind_set.callback(cog, past, "today at 2pm", past_secret, True)
        past_blob = _logged(caplog)
        assert past_secret not in past_blob
        assert "remind set rejected" in past_blob
        assert "reason=past time" in past_blob
        assert "user=8" in past_blob
        assert "username=bea" in past_blob

        listed = _interaction(7, username="ada", guild_name="Pooper Guild", channel_name="general")
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.remind_list.callback(cog, listed)
            await cog.remind_cancel.callback(cog, listed, reminder.id)
        follow_blob = _logged(caplog)
        assert secret not in follow_blob
        assert "remind list" in follow_blob
        assert "count=1" in follow_blob
        assert f"id={reminder.id}" in follow_blob
        assert "result=cancelled" in follow_blob
        assert "text=" not in follow_blob

    asyncio.run(scenario())


def test_delivery_is_logged_and_private_text_is_hidden(tmp_path, caplog):
    async def scenario():
        cog, bot = _cog(tmp_path)
        channel = _NamedTarget(name="general", message_id=9001)
        creator = _NamedTarget(name="ada", message_id=111)
        bot.channels[555] = channel
        bot.users[1] = creator
        bot.get_guild = lambda guild_id: SimpleNamespace(id=guild_id, name="Pooper Guild")
        _add(
            cog.store,
            text="public feed the cat",
            when=NOW - timedelta(seconds=30),
            participants=[2],
            reminder_id="aaaa1111",
        )
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.dispatch_due(now=NOW)
        fired = [
            record
            for record in caplog.records
            if record.name == "cogs.reminders" and record.getMessage().startswith("Fired in channel ")
        ]
        assert len(fired) == 1
        assert fired[0].levelno == logging.INFO
        line = fired[0].getMessage()
        assert "\n" not in line
        assert "id=aaaa1111" in line
        assert "user=1" in line
        assert "username=ada" in line
        assert "guild=42" in line
        assert "guild_name=Pooper Guild" in line
        assert "channel=555" in line
        assert "channel_name=general" in line
        assert "message_id=9001" in line
        assert "mentions=1,2" in line
        assert "timing=on time" in line
        assert "seconds_late=30" in line
        assert "public feed the cat" in line
        assert len(channel.sent) == 1

        secret = "dm-secret-do-not-log"
        dm = _NamedTarget(name="ada", message_id=4242)
        quiet = _NamedTarget(name="general", message_id=7)
        bot.users[1] = dm
        bot.channels[555] = quiet
        _add(
            cog.store,
            text=secret,
            when=NOW - timedelta(seconds=90),
            private=True,
            reminder_id="bbbb2222",
        )
        with caplog.at_level(logging.INFO, logger="cogs.reminders"):
            caplog.clear()
            await cog.dispatch_due(now=NOW)
        blob = _logged(caplog)
        assert secret not in blob
        assert quiet.sent == []
        assert secret in dm.sent[0][0]
        delivered = [
            record.getMessage()
            for record in caplog.records
            if record.name == "cogs.reminders" and record.getMessage().startswith("DM delivered ")
        ]
        assert len(delivered) == 1
        assert "\n" not in delivered[0]
        assert "id=bbbb2222" in delivered[0]
        assert "user=1" in delivered[0]
        assert "username=ada" in delivered[0]
        assert "dm_user=1" in delivered[0]
        assert "dm_username=ada" in delivered[0]
        assert "message_id=4242" in delivered[0]
        assert "timing=late" in delivered[0]
        assert "seconds_late=90" in delivered[0]
        assert f"text_len={len(secret)}" in delivered[0]
        assert "text=" not in delivered[0]

    asyncio.run(scenario())


def test_reminders_logger_emits_info_under_bot_logging(tmp_path, monkeypatch):
    import PooperScooper

    log_path = tmp_path / "pooperscooper.log"
    monkeypatch.setattr(PooperScooper, "LOG_FILE", str(log_path))
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configured = PooperScooper.configure_logging(debug=False)
        assert configured is root
        assert root.level == logging.INFO
        logger = logging.getLogger("cogs.reminders")
        assert logger.name == "cogs.reminders"
        assert logger.propagate is True
        assert logger.isEnabledFor(logging.INFO)
        logger.info("reminders-info-visible")
        for handler in root.handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "reminders-info-visible" in text
        assert "cogs.reminders" in text
        assert "INFO" in text
    finally:
        for handler in root.handlers[:]:
            if handler not in saved_handlers:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(saved_level)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
