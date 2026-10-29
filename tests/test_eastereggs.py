"""Phrase triggers for the Menaces easter-egg cog."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from cogs.eastereggs import (
    channel_or_parent_in,
    is_about_that_time,
    is_help_request,
    is_saturday_party_reminder,
    is_thread_opener,
    mentions_hooty,
    pick_party_emojis,
    EasterEggs,
)
from cogs.utils.server_config import ServerConfig

MENACES = {
    "enabled": True,
    "easter_eggs": {
        "hooty_hoo": {
            "enabled": True,
            "emoji": "<:salute:1213661793700937728>",
            "skip_channel_ids": [1019352642977267743],
        },
        "do_not_redeem": {
            "enabled": True,
            "channel_id": 1457177076820869412,
            "reply": "DO NOT REDEEM",
        },
        "announcement_party": {
            "enabled": True,
            "channel_ids": [1019352642977267743],
            "timezone": "America/Chicago",
            "reaction_count": 5,
            "emojis": [
                "<:beer_cat:1>",
                "<a:Beer:2>",
                "<a:Beer_girl:3>",
                "<:wine_drunk:4>",
                "<a:Drunk:5>",
                "<a:dance:6>",
                "<a:DogJam:7>",
                "<:salute:8>",
            ],
        },
    },
}
CHICAGO = ZoneInfo("America/Chicago")
FORUM_ID = 1457177076820869412
ANNOUNCEMENTS_ID = 1019352642977267743


def test_hooty_phrases():
    assert mentions_hooty("hooty hoo")
    assert mentions_hooty("HOOTY HOOs")
    assert mentions_hooty("time to hooty fucking hoot")
    assert not mentions_hooty("hooty and a ha")
    assert not mentions_hooty("shooty hoo")
    assert not mentions_hooty("")


def test_help_request_uses_title_or_body():
    assert is_help_request("my pc won't boot")
    assert is_help_request("", "help")
    assert is_help_request("it is not working")
    assert not is_help_request("just saying hi", "photos")
    assert not is_help_request("", "")


def test_thread_opener_is_only_the_starter_in_that_forum():
    assert is_thread_opener(50, 50, FORUM_ID, FORUM_ID)
    assert not is_thread_opener(51, 50, FORUM_ID, FORUM_ID)
    assert not is_thread_opener(50, 50, 9, FORUM_ID)
    assert not is_thread_opener(50, 50, FORUM_ID, None)


def test_announcements_and_its_threads_match_the_channel_set():
    assert channel_or_parent_in(ANNOUNCEMENTS_ID, None, {ANNOUNCEMENTS_ID})
    assert channel_or_parent_in(80, ANNOUNCEMENTS_ID, {ANNOUNCEMENTS_ID})
    assert not channel_or_parent_in(80, None, {ANNOUNCEMENTS_ID})


def test_about_that_time_matches_the_callout():
    assert is_about_that_time("ITS ABOUT THAT TIME")
    assert is_about_that_time("**ITS ABOUT THAT TIME!**")
    assert is_about_that_time("it's about that time")
    assert not is_about_that_time("about time we left")


def test_saturday_reminder_uses_chicago_not_utc():
    saturday_afternoon = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
    friday_night_utc_still_saturday_before = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
    saturday_night_chicago = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)
    text = "reminder to charge your headset and get ready for tonight"
    assert is_saturday_party_reminder(text, saturday_afternoon, CHICAGO)
    assert is_saturday_party_reminder(text, saturday_night_chicago, CHICAGO)
    assert is_saturday_party_reminder("charge your headset", saturday_afternoon, CHICAGO)
    assert is_saturday_party_reminder("get ready for VR", saturday_afternoon, CHICAGO)
    assert is_saturday_party_reminder("see you tonight", saturday_afternoon, CHICAGO)
    assert not is_saturday_party_reminder(text, friday_night_utc_still_saturday_before, CHICAGO)
    assert not is_saturday_party_reminder("new role colors are up", saturday_afternoon, CHICAGO)


def test_party_pick_is_five_unique():
    pool = [f"<:e:{i}>" for i in range(8)]
    chosen = pick_party_emojis(pool, 5, random.Random(1))
    assert len(chosen) == 5
    assert len(set(chosen)) == 5
    assert pick_party_emojis(["<:a:1>", "<:a:1>"], 5, random.Random(1)) == ["<:a:1>"]


def _message(**overrides):
    channel = overrides.pop(
        "channel",
        SimpleNamespace(id=5, parent_id=None, name="general"),
    )
    data = dict(
        id=10,
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(bot=False),
        content="hooty hoo",
        channel=channel,
        add_reaction=AsyncMock(),
        reply=AsyncMock(),
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_salute_reacts_once_and_ignores_commands(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _message()
    asyncio.run(cog.on_easter_egg(message))
    message.add_reaction.assert_awaited()
    message.reply.assert_not_awaited()

    command = _message(content="!play hooty hoo")
    asyncio.run(cog.on_easter_egg(command))
    command.add_reaction.assert_not_awaited()


def test_salute_skips_announcements(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _message(channel=SimpleNamespace(id=ANNOUNCEMENTS_ID, parent_id=None, name="announcements"))
    asyncio.run(cog.on_easter_egg(message))
    message.add_reaction.assert_not_awaited()


def test_redeem_replies_once_on_a_new_help_post(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _message(
        id=77,
        content="headset is broken",
        channel=SimpleNamespace(id=77, parent_id=FORUM_ID, name="mic"),
    )
    asyncio.run(cog.on_easter_egg(message))
    message.reply.assert_awaited_once_with("DO NOT REDEEM", mention_author=False)

    asyncio.run(cog.on_easter_egg(message))
    message.reply.assert_awaited_once()


def test_redeem_ignores_followups_and_chatter(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    followup = _message(
        id=78,
        content="still broken?",
        channel=SimpleNamespace(id=77, parent_id=FORUM_ID, name="mic"),
    )
    chatter = _message(
        id=90,
        content="look at this photo",
        channel=SimpleNamespace(id=90, parent_id=FORUM_ID, name="photos"),
    )
    asyncio.run(cog.on_easter_egg(followup))
    asyncio.run(cog.on_easter_egg(chatter))
    followup.reply.assert_not_awaited()
    chatter.reply.assert_not_awaited()


def _announcement(content: str, created_at: datetime):
    return _message(
        content=content,
        created_at=created_at,
        channel=SimpleNamespace(id=ANNOUNCEMENTS_ID, parent_id=None, name="announcements"),
    )


def test_about_that_time_gets_five_party_reactions_even_on_friday(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _announcement(
        "**ITS ABOUT THAT TIME!**\nHOOTY HOO",
        datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc),
    )
    asyncio.run(cog.on_easter_egg(message))
    assert message.add_reaction.await_count == 5
    message.reply.assert_not_awaited()


def test_saturday_reminder_gets_five_reactions(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _announcement(
        "reminder to charge your headset and get ready for tonight",
        datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc),
    )
    asyncio.run(cog.on_easter_egg(message))
    assert message.add_reaction.await_count == 5


def test_friday_reminder_and_other_channels_stay_quiet(monkeypatch):
    config = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    friday = _announcement(
        "reminder to charge your headset and get ready for tonight",
        datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc),
    )
    elsewhere = _message(
        content="ITS ABOUT THAT TIME",
        created_at=datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc),
    )
    asyncio.run(cog.on_easter_egg(friday))
    asyncio.run(cog.on_easter_egg(elsewhere))
    friday.add_reaction.assert_not_awaited()
    elsewhere.add_reaction.assert_not_awaited()


def test_hooty_allowlist_limits_the_channel(monkeypatch):
    limited = json.loads(json.dumps(MENACES))
    limited["easter_eggs"]["hooty_hoo"]["channel_ids"] = [1045149015756521493]
    config = ServerConfig(1, limited)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    elsewhere = _message(content="hooty hoo")
    there = _message(
        content="hooty hoo",
        channel=SimpleNamespace(id=1045149015756521493, parent_id=None, name="test-channel"),
    )
    asyncio.run(cog.on_easter_egg(elsewhere))
    asyncio.run(cog.on_easter_egg(there))
    elsewhere.add_reaction.assert_not_awaited()
    there.add_reaction.assert_awaited()


def test_webhooks_react_only_when_the_guild_allows_them(monkeypatch):
    blocked = ServerConfig(1, MENACES)
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: blocked)
    cog = EasterEggs(bot=None)
    webhook = _message(author=SimpleNamespace(bot=True), webhook_id=99, content="hooty hoo")
    asyncio.run(cog.on_easter_egg(webhook))
    webhook.add_reaction.assert_not_awaited()

    allowed_data = json.loads(json.dumps(MENACES))
    allowed_data["easter_eggs"]["allow_webhooks"] = True
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: ServerConfig(1, allowed_data))
    asyncio.run(cog.on_easter_egg(webhook))
    webhook.add_reaction.assert_awaited()


def test_disabled_guild_stays_quiet(monkeypatch):
    config = ServerConfig(1, {"enabled": True})
    monkeypatch.setattr("cogs.eastereggs.get_guild_config", lambda guild_id: config)
    cog = EasterEggs(bot=None)
    message = _message()
    asyncio.run(cog.on_easter_egg(message))
    message.add_reaction.assert_not_awaited()
    message.reply.assert_not_awaited()
