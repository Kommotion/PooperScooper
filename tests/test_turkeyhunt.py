"""Thanksgiving turkey hunt. Fake clocks, no Discord connection, no live avatar."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView

from PooperScooper import _HALLOWEEN_EXTENSIONS, extensions_to_load, initial_extensions
from cogs.candyhunt import (
    MAIN_DROP_CHANNEL_ID,
    MTS_GUILD_ID,
    NICK_ID,
    SUPPORT_GUILD_ID,
    SUPPORT_TEST_CHANNEL_ID,
)
from cogs.hunt import load_theme
from cogs.hunt.book import DeleteRef, HuntBook, closed_shell_keeps_winner
from cogs.hunt.copy import (
    catch_dm,
    drop_text,
    is_deletable_hunt_message,
    kickoff_text,
    recap_text,
    slip_text,
    winner_text,
)
from cogs.hunt.ctl import (
    MAX_COMMANDS_PER_TICK,
    default_ctl_root,
    inbox_file_is_local,
    new_request_id,
    wait_for_result,
    write_command,
    write_result,
)
from cogs.hunt.engine import HuntEngine
from cogs.hunt.halloween import HalloweenTheme
from cogs.hunt.rolls import choose_item, slipped
from cogs.hunt.schedule import build_schedule, draw_counts, raw_bounds
from cogs.hunt.thanksgiving import (
    DOCUMENTED_RAW_TOTAL_BOUND,
    DOCUMENTED_TARGET_TOTAL,
    TurkeySettings,
    apply_turkey_hunt_overrides,
    load_turkey_settings,
)
from cogs.hunt.theme import contains_role_ping
from cogs.turkeyhunt import (
    CLAIM_TEMPLATE,
    TURKEYADMIN_USAGE,
    TurkeyClaimButton,
    TurkeyHunt,
    _turkeyadmin_allowed,
    build_drop_view,
)
from cogs.utils.config import BotConfig
from cogs.utils.constants import DATA_DIR, PROJECT_ROOT
from cogs.utils.server_config import ServerConfig, get_server_config_manager

PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parents[1]
GENERAL_ID = 932057681307512924
TIE_WORD = re.compile(r"\btie\b", re.IGNORECASE)
SUPPORT_CHANNEL_ID = 1045149015756521493


class _HTTPResponse:
    status = 500
    reason = "Boom"


class ScriptedRng(random.Random):
    def __init__(self, values: list[float]):
        super().__init__(0)
        self._values = list(values)
        self._index = 0

    def random(self) -> float:
        if self._index >= len(self._values):
            return 0.99
        value = self._values[self._index]
        self._index += 1
        return float(value)


class FakeBot:
    def __init__(self):
        self.dynamic_items: list = []
        self.channels: dict = {}
        self.tree = SimpleNamespace(sync=AsyncMock(return_value=[]))
        self.owner_ids: set[int] = set()
        self.user = SimpleNamespace(id=4242, avatar=None, edit=AsyncMock())

    def add_dynamic_items(self, *items):
        self.dynamic_items.extend(items)

    def remove_dynamic_items(self, *items):
        for item in items:
            if item in self.dynamic_items:
                self.dynamic_items.remove(item)

    async def wait_until_ready(self):
        return None

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(_HTTPResponse(), "missing")
        return channel

    def get_cog(self, name):
        return getattr(self, "_cog", None) if name == "TurkeyHunt" else None

    async def is_owner(self, user):
        return getattr(user, "id", None) in self.owner_ids

    async def can_run(self, ctx, *, call_once=False):
        return True


class HuntChannel(discord.TextChannel):
    def __init__(self, channel_id: int, name: str, guild_id: int | None):
        self.id = channel_id
        self.name = name
        self.guild = None if guild_id is None else SimpleNamespace(id=guild_id)
        self.speaker = SimpleNamespace(id=1)
        self.messages: dict[int, SimpleNamespace] = {}
        self.sent: list[dict] = []
        self.fail_send = False
        self.fail_delete: set[int] = set()
        self._seq = 1000

    async def send(self, content=None, **kwargs):
        if self.fail_send:
            raise discord.HTTPException(_HTTPResponse(), "send failed")
        self._seq += 1
        mid = self._seq
        text = content or ""
        view = kwargs.get("view")
        if view is not None:
            extra = []
            for item in view.walk_children():
                bit = getattr(item, "content", None)
                if isinstance(bit, str):
                    extra.append(bit)
            if extra:
                text = (text + "\n" + "\n".join(extra)).strip()
        return self._store(mid, text, self.speaker, kwargs)

    def preseed(self, message_id: int, content: str, author) -> SimpleNamespace:
        return self._store(message_id, content, author, {})

    def _store(self, message_id: int, content: str, author, kwargs: dict) -> SimpleNamespace:
        channel = self

        async def delete():
            if message_id in channel.fail_delete:
                raise discord.HTTPException(_HTTPResponse(), "delete failed")
            channel.messages.pop(message_id, None)

        message = SimpleNamespace(id=message_id, content=content, author=author, delete=delete, components=[])
        self.messages[message_id] = message
        self.sent.append({"id": message_id, "content": content, "kwargs": kwargs})
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(_HTTPResponse(), "missing")
        return message


def _settings(**changes) -> TurkeySettings:
    base = TurkeySettings()
    if not changes:
        return base
    return replace(base, **changes)


def _book(tmp_path, rng=None, settings: TurkeySettings | None = None) -> HuntBook:
    settings = settings or TurkeySettings()
    tmp_path.mkdir(parents=True, exist_ok=True)
    return HuntBook(
        settings,
        tmp_path / "turkey_hunt.json",
        rng=rng or random.Random(0),
        archive_path=tmp_path / "turkey_hunt_archive_2026.json",
    )


def _cog(
    tmp_path,
    *,
    enabled: bool = False,
    rng=None,
    settings: TurkeySettings | None = None,
    clock=None,
    path: Path | None = None,
    ctl_dir: Path | None = None,
) -> tuple[TurkeyHunt, FakeBot]:
    bot = FakeBot()
    state_path = path or (tmp_path / "turkey_hunt.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    cog = TurkeyHunt(
        bot,
        path=state_path,
        settings=settings or TurkeySettings(),
        rng=rng or random.Random(0),
        enabled=enabled,
        clock=clock or (lambda: datetime(2026, 11, 10, 12, tzinfo=PT)),
        ctl_dir=ctl_dir or (tmp_path / "turkey_hunt_ctl"),
    )
    bot._cog = cog
    return cog, bot


def _wire(cog: TurkeyHunt, bot: FakeBot, *, name: str = "general", guild_id: int | None = None) -> HuntChannel:
    guild_id = cog.settings.guild_id if guild_id is None else guild_id
    channel = HuntChannel(cog.settings.drop_channel_id, name, guild_id)
    channel.speaker = bot.user
    bot.channels[channel.id] = channel
    return channel


def _quiet_avatar(cog: TurkeyHunt) -> None:
    """Skip the pilgrim swap so a live tick never reads the real avatar file."""
    cog.book.state["avatar_apply_attempted"] = True
    cog.book.save()


def _slot(when: datetime, drop_id: str, *, lifetime_min: int = 45, guarantee: str | None = None) -> dict:
    local = when.astimezone(PT)
    return {
        "id": drop_id,
        "at": when.isoformat(),
        "day": local.date().isoformat(),
        "lifetime_seconds": lifetime_min * 60,
        "status": "pending",
        "guarantee_key": guarantee,
    }


def _arm(cog: TurkeyHunt, slots: list[dict], *, kickoff_done: bool = True) -> None:
    cog.book.state["schedule_built"] = True
    cog.book.state["schedule"] = slots
    cog.book.state["kickoff_done"] = kickoff_done
    cog.book.state["avatar_apply_attempted"] = True
    for day in cog.settings.event_dates():
        cog.book.state["recap_done"][day.isoformat()] = True
    cog.book.save()


def _interaction(cog: TurkeyHunt, user_id: int, *, guild_id: int = MTS_GUILD_ID, name: str = "Ada", bot: bool = False, send=None):
    user = SimpleNamespace(id=user_id, bot=bot, display_name=name, send=send or AsyncMock())
    return SimpleNamespace(
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            is_done=lambda: False,
        ),
        user=user,
        guild_id=guild_id,
        client=cog.bot,
    )


def _ctx(cog: TurkeyHunt, user_id: int = NICK_ID, *, guild_id: int | None = MTS_GUILD_ID, channel=None):
    author = SimpleNamespace(id=user_id, bot=False)
    ctx = SimpleNamespace(
        author=author,
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        channel=channel,
        bot=cog.bot,
        cog=cog,
        reply=AsyncMock(),
        send=AsyncMock(),
        command=cog.turkeyadmin,
        prefix="!",
        invoked_with="turkeyadmin",
        invoked_parents=[],
        invoked_subcommand=None,
        subcommand_passed=None,
        args=[],
        kwargs={},
        view=StringView(""),
    )
    ctx.message = SimpleNamespace(author=author, guild=ctx.guild, channel=channel, id=1)
    return ctx


def _replied(ctx) -> tuple[str, dict]:
    args, kwargs = ctx.reply.await_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def _assert_silent(ctx) -> None:
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()


def _bind(cog: TurkeyHunt) -> None:
    for command in cog.walk_commands():
        command.cog = cog


def _no_pings(kwargs: dict) -> None:
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


def _open_drop(cog: TurkeyHunt, drop_id: str, when: datetime, *, lifetime_min: int, message_id: int, type_key: str = "regular"):
    item = cog.settings.item_by_key(type_key)
    assert item is not None
    expires = when + timedelta(minutes=lifetime_min)
    cog.book.state["drops"][drop_id] = {
        "id": drop_id,
        "channel_id": cog.settings.drop_channel_id,
        "message_id": message_id,
        "type_key": item.key,
        "name": item.name,
        "emoji": item.emoji,
        "points": item.points,
        "grease_chance": item.grease_chance,
        "posted_at": when.isoformat(),
        "scheduled_at": when.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "open",
        "caught_by": None,
        "day": when.astimezone(PT).date().isoformat(),
        "test": False,
    }
    cog.book.save()


def _minutes(moment: datetime) -> int:
    local = moment.astimezone(PT)
    return local.hour * 60 + local.minute


def _gaps(moments: list[datetime]) -> list[int]:
    ordered = sorted(moments)
    return [int((ordered[index] - ordered[index - 1]).total_seconds() // 60) for index in range(1, len(ordered))]


def test_general_channel_id_comes_from_candyhunt_constant():
    source = (ROOT / "cogs" / "candyhunt.py").read_text(encoding="utf-8")
    assert "MAIN_DROP_CHANNEL_ID = 932057681307512924  # MTS #general" in source
    assert "cogs.hunt" not in source
    assert "turkeyhunt" not in source
    assert MAIN_DROP_CHANNEL_ID == GENERAL_ID
    assert SUPPORT_CHANNEL_ID == SUPPORT_TEST_CHANNEL_ID
    assert TurkeySettings().drop_channel_id == GENERAL_ID
    assert TurkeySettings().guild_id == 932057681307512922


def test_switches_and_extension_list():
    blank = BotConfig.model_validate({"token": "test-token", "client_id": "123456789012345678"})
    assert blank.halloween_enabled is False
    assert blank.thanksgiving_enabled is False
    assert "cogs.turkeyhunt" in initial_extensions
    assert "cogs.turkeyhunt" not in _HALLOWEEN_EXTENSIONS
    loaded = extensions_to_load(blank)
    assert "cogs.turkeyhunt" in loaded
    assert "cogs.candyhunt" not in loaded
    assert "cogs.costume" not in loaded
    assert extensions_to_load(SimpleNamespace()) == [name for name in initial_extensions if name not in _HALLOWEEN_EXTENSIONS]
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["halloween_enabled"] is False
    assert example["thanksgiving_enabled"] is False
    docs = (ROOT / "docs" / "SERVER_CONFIGS.md").read_text(encoding="utf-8")
    readme = (ROOT / "docs" / "README_turkeyhunt.md").read_text(encoding="utf-8")
    assert "thanksgiving_enabled" in docs
    assert "932057681307512924" in docs
    assert "932057681307512924" in readme
    assert "!sync_commands ~" in readme
    mts = json.loads((ROOT / "server_configs" / "932057681307512922.json").read_text(encoding="utf-8"))
    sample = json.loads((ROOT / "server_configs" / "example.json").read_text(encoding="utf-8"))
    assert mts["turkey_hunt"]["drop_channel_id"] == GENERAL_ID
    assert sample["turkey_hunt"]["drop_channel_id"] == GENERAL_ID
    assert mts["turkey_hunt"]["prize_line"] == ""
    assert ServerConfig(1, {}).turkey_hunt == {}
    assert ServerConfig(1, {"turkey_hunt": "nope"}).turkey_hunt == {}
    pilgrim = PROJECT_ROOT / "assets" / "avatars" / "pilgrim_shepherd.png"
    assert pilgrim.is_file()
    assert TurkeySettings().avatar_path == "assets/avatars/pilgrim_shepherd.png"


def test_overrides_ignore_guild_and_role_pings():
    base = TurkeySettings()
    assert apply_turkey_hunt_overrides(base, {}) is base
    assert apply_turkey_hunt_overrides(base, {"guild_id": 5}) is base
    assert apply_turkey_hunt_overrides(base, {"prize_line": "hello @everyone"}) is base
    updated = apply_turkey_hunt_overrides(base, {"prize_line": "a pie"})
    assert updated.prize_line == "a pie"
    assert updated.guild_id == base.guild_id
    assert updated.drop_channel_id == GENERAL_ID


def test_halloween_theme_does_not_run_the_live_engine(tmp_path):
    theme = load_theme("halloween")
    assert isinstance(theme, HalloweenTheme)
    assert theme.handles_live_event() is False
    assert theme.drop_channel_id == GENERAL_ID
    book = _book(tmp_path)
    book.prepare_live = AsyncMock(side_effect=AssertionError("halloween must not prepare"))
    engine = HuntEngine(theme, book=book)

    async def run():
        assert await engine.tick(datetime(2026, 11, 20, 5, tzinfo=PT), avatar_exists=True) is None

    asyncio.run(run())
    assert book.state["schedule_built"] is False


def test_schedule_hits_ranges_windows_gaps_and_clusters():
    settings = TurkeySettings()
    plans = settings.day_plans()
    assert raw_bounds(plans) == DOCUMENTED_RAW_TOTAL_BOUND == (74, 112)
    assert settings.target_total == DOCUMENTED_TARGET_TOTAL == 100
    by_day = {plan.day.isoformat(): plan for plan in plans}
    assert by_day["2026-11-22"].window_end.hour == 22
    assert by_day["2026-11-29"].window_end.hour == 21
    assert by_day["2026-11-29"].window_end.minute == 30
    assert by_day["2026-11-26"].lifetime == timedelta(minutes=30)
    assert by_day["2026-11-22"].lifetime == timedelta(minutes=45)
    nov22_after = False
    seen = []
    for seed in range(12):
        slots = build_schedule(plans, random.Random(seed), zone=PT, target_total=100)
        seen.append(tuple(slot["at"] for slot in slots))
        grouped: dict[str, list[datetime]] = {}
        for slot in slots:
            grouped.setdefault(slot["day"], []).append(datetime.fromisoformat(slot["at"]))
        assert sum(len(moments) for moments in grouped.values()) == 100
        for plan in plans:
            moments = sorted(grouped[plan.day.isoformat()])
            assert plan.low <= len(moments) <= plan.high
            gaps = _gaps(moments)
            assert gaps
            assert min(gaps) >= 20
            assert max(gaps) <= 180
            assert any(gap <= 30 for gap in gaps)
            assert any(gap < 180 for gap in gaps)
            assert all((moments[index] - moments[index - 1]).total_seconds() % 60 == 0 for index in range(1, len(moments)))
            assert 5 * 60 <= _minutes(moments[0]) <= 8 * 60
            late_end = 21 * 60 + 30 if plan.day.isoformat() == "2026-11-29" else 22 * 60
            assert 20 * 60 <= _minutes(moments[-1]) <= late_end
            assert all(5 * 60 <= _minutes(moment) <= late_end for moment in moments)
            life = 30 if plan.day.isoformat() == "2026-11-26" else 45
            assert all(
                slot["lifetime_seconds"] == life * 60
                for slot in slots
                if slot["day"] == plan.day.isoformat()
            )
        assert _minutes(grouped["2026-11-29"][-1]) <= 21 * 60 + 30
        if _minutes(grouped["2026-11-22"][-1]) > 21 * 60 + 30:
            nov22_after = True
        last_thanks = [slot for slot in slots if slot["day"] == "2026-11-26"][-1]
        assert last_thanks["guarantee_key"] == "golden"
        assert all(slot["guarantee_key"] is None for slot in slots if slot["id"] != last_thanks["id"] or slot["day"] != "2026-11-26")
    assert nov22_after
    assert len(set(seen)) == len(seen)


def test_unadjusted_draw_lands_near_one_hundred():
    plans = TurkeySettings().day_plans()
    totals = [sum(draw_counts(plans, random.Random(seed))) for seed in range(200)]
    assert min(totals) >= 74
    assert max(totals) <= 112
    assert 95 <= (sum(totals) / len(totals)) <= 102


def test_kickoff_lists_odds_lifetime_grease_and_the_clocks():
    settings = TurkeySettings()
    assert (settings.regular_points, settings.plump_points, settings.golden_points) == (10, 30, 100)
    text = kickoff_text(settings)
    for piece in (
        "Regular",
        "Plump",
        "Golden",
        "75%",
        "20%",
        "5%",
        "45",
        "30",
        "1 in 10",
        "10:00 PM",
        "10:00 AM",
        "10 points",
        "30 points",
        "100 points",
    ):
        assert piece in text
    assert "Regular — 75% — 10 points." in text
    assert "Plump — 20% — 30 points." in text
    assert "Golden — 5% — 100 points." in text
    assert "— 1 point." not in text
    assert "— 3 points." not in text
    assert not contains_role_ping(text)
    assert "@everyone" not in text and "@here" not in text and "<@&" not in text


def test_odds_and_thanksgiving_golden_guarantee(tmp_path):
    settings = TurkeySettings()
    counts = {"regular": 0, "plump": 0, "golden": 0}
    rng = random.Random(7)
    for _ in range(4000):
        counts[choose_item(settings.items(), rng).key] += 1
    assert 2800 <= counts["regular"] <= 3200
    assert 600 <= counts["plump"] <= 1000
    assert 120 <= counts["golden"] <= 280

    book = _book(tmp_path, rng=ScriptedRng([0.0]))
    first = datetime(2026, 11, 26, 20, 0, tzinfo=PT)
    second = datetime(2026, 11, 26, 20, 20, tzinfo=PT)
    book.state["schedule_built"] = True
    book.state["schedule"] = [
        _slot(first, "aaaa1111", lifetime_min=30),
        _slot(second, "bbbb2222", lifetime_min=30, guarantee="golden"),
    ]
    book.state["kickoff_done"] = True
    book.state["avatar_apply_attempted"] = True

    async def run():
        plan = await book.prepare_live(datetime(2026, 11, 26, 20, 25, tzinfo=PT), avatar_exists=False)
        return plan

    plan = asyncio.run(run())
    posted = {post.drop_id: post.item_key for post in plan.posts}
    assert posted["aaaa1111"] == "regular"
    assert posted["bbbb2222"] == "golden"
    assert book.state["drops"]["bbbb2222"]["expires_at"].startswith("2026-11-26T20:50")


def test_golden_is_not_forced_when_one_already_appeared_and_misses_are_not_replaced(tmp_path):
    book = _book(tmp_path, rng=ScriptedRng([0.0]))
    second = datetime(2026, 11, 26, 20, 20, tzinfo=PT)
    book.state["schedule_built"] = True
    book.state["kickoff_done"] = True
    book.state["avatar_apply_attempted"] = True
    book.state["schedule"] = [_slot(second, "bbbb2222", lifetime_min=30, guarantee="golden")]
    book.state["drops"]["prevgold"] = {
        "id": "prevgold",
        "type_key": "golden",
        "day": "2026-11-26",
        "status": "caught",
        "channel_id": GENERAL_ID,
        "message_id": 1,
    }

    async def already():
        plan = await book.prepare_live(datetime(2026, 11, 26, 20, 25, tzinfo=PT), avatar_exists=False)
        assert [post.item_key for post in plan.posts] == ["regular"]

    asyncio.run(already())

    missed = _book(tmp_path / "miss", rng=ScriptedRng([0.0]))
    missed.state["schedule_built"] = True
    missed.state["kickoff_done"] = True
    missed.state["avatar_apply_attempted"] = True
    missed.state["schedule"] = [
        _slot(datetime(2026, 11, 26, 6, 0, tzinfo=PT), "cccc3333", lifetime_min=30),
        _slot(second, "dddd4444", lifetime_min=30, guarantee="golden"),
    ]

    async def only_last():
        plan = await missed.prepare_live(datetime(2026, 11, 26, 20, 25, tzinfo=PT), avatar_exists=False)
        assert [post.drop_id for post in plan.posts] == ["dddd4444"]
        assert plan.posts[0].item_key == "golden"
        assert missed.state["schedule"][0]["status"] == "missed"
        assert len(missed.state["schedule"]) == 2

    asyncio.run(only_last())


def test_grease_slips_regulars_only_and_does_not_change_the_score(tmp_path):
    book = _book(tmp_path)
    when = datetime(2026, 11, 21, 12, 0, tzinfo=PT)
    item = book.settings.item_by_key("regular")
    plump = book.settings.item_by_key("plump")
    golden = book.settings.item_by_key("golden")
    assert slipped(item.grease_chance, ScriptedRng([0.099])) is True
    assert slipped(item.grease_chance, ScriptedRng([0.1])) is False
    assert slipped(plump.grease_chance, ScriptedRng([0.0])) is False
    assert slipped(golden.grease_chance, ScriptedRng([0.0])) is False
    text = slip_text(item)
    folded = text.casefold()
    for banned in ("lost", "penalty", "minus", "deduct"):
        assert banned not in folded
    book.state["drops"]["abcd1234"] = {
        "id": "abcd1234",
        "channel_id": GENERAL_ID,
        "message_id": 9,
        "type_key": "regular",
        "name": "Regular",
        "emoji": "🦃",
        "points": 10,
        "grease_chance": item.grease_chance,
        "posted_at": when.isoformat(),
        "scheduled_at": when.isoformat(),
        "expires_at": (when + timedelta(minutes=45)).isoformat(),
        "status": "open",
        "caught_by": None,
        "day": "2026-11-21",
        "test": False,
    }
    book.state["drops"]["abcd1235"] = dict(book.state["drops"]["abcd1234"])
    book.state["drops"]["abcd1235"]["id"] = "abcd1235"
    book.state["drops"]["abcd1235"]["type_key"] = "plump"
    book.state["drops"]["abcd1235"]["name"] = "Plump"
    book.state["drops"]["abcd1235"]["points"] = 30
    book.state["drops"]["abcd1235"]["grease_chance"] = 0.0

    async def run():
        slip = await book.claim("abcd1234", 5, when, ScriptedRng([0.0]), name="@everyone Ada", is_bot=False)
        catch = await book.claim("abcd1235", 5, when, ScriptedRng([0.0]), name="Ada", is_bot=False)
        return slip, catch

    slip, catch = asyncio.run(run())
    assert slip.status == "slip"
    assert book.state["drops"]["abcd1234"]["status"] == "open"
    assert book.state["scores"] == {"5": 30}
    assert catch.total == 30
    assert "30 points" in catch.dm_text
    assert book.state["slips"] == 1
    assert "Greased feathers" in slip.text
    assert catch.status == "caught"
    assert "@everyone" not in book.state["names"]["5"]
    assert not contains_role_ping(catch.text)
    assert not contains_role_ping(catch.dm_text)


def test_restart_skips_posted_slots_and_posts_a_still_living_one(tmp_path):
    path = tmp_path / "turkey_hunt.json"
    archive = tmp_path / "turkey_hunt_archive_2026.json"
    when = datetime(2026, 11, 21, 12, 0, tzinfo=PT)
    book = HuntBook(TurkeySettings(), path, rng=random.Random(0), archive_path=archive)
    book.state["schedule_built"] = True
    book.state["kickoff_done"] = True
    book.state["avatar_apply_attempted"] = True
    book.state["schedule"] = [_slot(when, "abcd1234")]
    book.save()

    async def first():
        plan = await book.prepare_live(when + timedelta(minutes=10), avatar_exists=False)
        assert [post.drop_id for post in plan.posts] == ["abcd1234"]
        assert book.state["drops"]["abcd1234"]["message_id"] is None
        assert book.state["drops"]["abcd1234"]["status"] == "open"
        assert book.state["drops"]["abcd1234"]["expires_at"].startswith("2026-11-21T12:45")

    asyncio.run(first())
    reloaded = HuntBook(TurkeySettings(), path, rng=random.Random(1), archive_path=archive)

    async def second():
        plan = await reloaded.prepare_live(when + timedelta(minutes=10), avatar_exists=False)
        assert plan.posts == []
        assert reloaded.state["schedule"][0]["status"] == "posted"

    asyncio.run(second())

    expired = HuntBook(TurkeySettings(), tmp_path / "late.json", rng=random.Random(0), archive_path=archive)
    expired.state["schedule_built"] = True
    expired.state["kickoff_done"] = True
    expired.state["avatar_apply_attempted"] = True
    expired.state["schedule"] = [_slot(when, "abcd9999")]

    async def too_late():
        plan = await expired.prepare_live(when + timedelta(hours=2), avatar_exists=False)
        assert plan.posts == []
        assert expired.state["schedule"][0]["status"] == "missed"
        assert expired.state["drops"] == {}

    asyncio.run(too_late())


def test_sunday_close_blocks_catches_and_monday_archives_the_tie(tmp_path):
    book = _book(tmp_path)
    settings = book.settings
    end = settings.drops_end
    scheduled = end - timedelta(minutes=30)
    book.state["schedule_built"] = True
    book.state["kickoff_done"] = True
    book.state["avatar_apply_attempted"] = True
    book.state["avatar_applied"] = True
    book.state["drops"]["abcd1234"] = {
        "id": "abcd1234",
        "channel_id": GENERAL_ID,
        "message_id": 44,
        "type_key": "regular",
        "name": "Regular",
        "emoji": "🦃",
        "points": 10,
        "grease_chance": 0.1,
        "posted_at": scheduled.isoformat(),
        "scheduled_at": scheduled.isoformat(),
        "expires_at": (scheduled + timedelta(minutes=45)).isoformat(),
        "status": "open",
        "caught_by": None,
        "day": "2026-11-29",
        "test": False,
    }
    book.state["schedule"] = [_slot(end - timedelta(minutes=5), "abcd5555", lifetime_min=45)]
    book.state["scores"] = {"5": 10, "6": 10}
    book.state["names"] = {"5": "Ada", "6": "Bea"}
    book.state["catches"] = [{"user_id": "5", "type_key": "golden", "name": "Golden", "points": 100, "day": "2026-11-29"}]
    book.state["recaps"]["2026-11-29"] = {"channel_id": GENERAL_ID, "message_id": 70}
    book.state["kickoff_message"] = {"channel_id": GENERAL_ID, "message_id": 71}

    async def close_then_final():
        plan = await book.prepare_live(end, avatar_exists=False)
        assert plan.posts == []
        assert book.state["closed"] is True
        assert book.state["drops"]["abcd1234"]["status"] == "closed"
        assert book.state["schedule"][0]["status"] == "missed"
        claim = await book.claim("abcd1234", 8, end, ScriptedRng([0.99]), name="Cam", is_bot=False)
        assert claim.status == "closed"
        assert book.state["scores"] == {"5": 10, "6": 10}
        final_plan = await book.prepare_live(settings.final_at, avatar_exists=False)
        assert final_plan.final_text
        assert "It's a tie!" in final_plan.final_text
        deleted_ids = {ref.message_id for ref in final_plan.deletes}
        assert 44 in deleted_ids
        assert 70 in deleted_ids
        assert 71 not in deleted_ids
        should_revert = book.commit_final(90, GENERAL_ID, settings.final_at)
        assert should_revert is True
        assert book.state["avatar_revert_attempted"] is False
        assert closed_shell_keeps_winner(book.state)
        assert book.state["scores"] == {}
        assert book.state["event_disabled"] is True
        assert book.state["schedule_built"] is True
        archive = json.loads((tmp_path / "turkey_hunt_archive_2026.json").read_text(encoding="utf-8"))
        assert archive["scores"] == {"5": 10, "6": 10}
        quiet = await book.prepare_live(settings.final_at + timedelta(hours=1), avatar_exists=True)
        assert quiet.final_text is None
        assert quiet.posts == []

    asyncio.run(close_then_final())


def test_winner_copy_names_ties_and_leaves_the_prize_unset():
    settings = TurkeySettings()
    sole = winner_text(settings, {"5": 9, "6": 4}, {"5": "Ada", "6": "Bea"}, [], 0)
    assert "Ada wins the turkey hunt" in sole
    assert TIE_WORD.search(sole) is None
    assert "Prize: (not set yet)" in sole
    assert not is_deletable_hunt_message(sole)
    tied = winner_text(settings, {"5": 9, "6": 9}, {"5": "Ada", "6": "Bea"}, [], 2)
    assert "It's a tie!" in tied
    assert "Tie for 1st" in tied
    second = winner_text(settings, {"5": 9, "6": 4, "7": 4}, {"5": "Ada", "6": "Bea", "7": "Cam"}, [], 0)
    assert "It's a tie!" not in second
    assert "Tie for 2nd" in second
    empty = winner_text(settings, {}, {}, [], 0)
    assert "The flock wins" in empty
    prized = winner_text(replace(settings, prize_line="a pie"), {"5": 1}, {"5": "Ada"}, [], 0)
    assert "Prize: a pie" in prized
    assert not contains_role_ping(tied)


def test_live_switch_off_skips_kickoff_drops_and_avatar(tmp_path):
    png = tmp_path / "pilgrim.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    settings = _settings(avatar_path=str(png))
    cog, bot = _cog(tmp_path, enabled=False, settings=settings)
    channel = _wire(cog, bot)
    path = cog.book.path

    async def run():
        await cog.tick(settings.event_start)
        await cog.apply_avatar()
        await cog.revert_avatar()

    asyncio.run(run())
    assert channel.sent == []
    assert not path.exists()
    bot.user.edit.assert_not_called()
    assert cog.book.state["avatar_apply_attempted"] is False
    assert cog.book.state["kickoff_done"] is False


def test_missing_avatar_logs_and_still_posts_kickoff(tmp_path, caplog):
    settings = _settings(avatar_path=str(tmp_path / "missing-pilgrim.png"))
    cog, bot = _cog(tmp_path, enabled=True, settings=settings)
    channel = _wire(cog, bot)
    cog.book.state["schedule_built"] = True
    cog.book.state["schedule"] = []
    cog.book.save()

    async def run():
        with caplog.at_level("WARNING"):
            await cog.tick(settings.event_start)

    asyncio.run(run())
    assert channel.sent
    assert "The turkey hunt is on." in channel.sent[0]["content"]
    _no_pings(channel.sent[0]["kwargs"])
    bot.user.edit.assert_not_called()
    assert cog.book.state["kickoff_done"] is True
    assert cog.book.state["avatar_missing_logged"] is True
    assert cog.book.state["avatar_apply_attempted"] is False
    assert any("avatar" in record.message.casefold() for record in caplog.records)


def test_avatar_apply_and_revert_happen_once_on_a_temp_file(tmp_path):
    png = tmp_path / "pilgrim.png"
    png.write_bytes(b"pilgrim-bytes")
    settings = _settings(avatar_path=str(png))
    cog, bot = _cog(tmp_path, enabled=True, settings=settings)

    class Asset:
        def __init__(self, payload: bytes):
            self.payload = payload

        async def read(self):
            return self.payload

    bot.user.avatar = Asset(b"original-bytes")

    async def run():
        await cog.apply_avatar()
        await cog.apply_avatar()
        await cog.revert_avatar()
        await cog.revert_avatar()

    asyncio.run(run())
    assert bot.user.edit.await_count == 2
    assert bot.user.edit.await_args_list[0].kwargs["avatar"] == b"pilgrim-bytes"
    assert bot.user.edit.await_args_list[1].kwargs["avatar"] == b"original-bytes"
    assert cog.book.state["avatar_apply_attempted"] is True
    assert cog.book.state["avatar_applied"] is True
    assert cog.book.state["avatar_revert_attempted"] is True
    backup = Path(cog.book.state["avatar_backup"])
    assert backup.is_file()
    assert backup.read_bytes() == b"original-bytes"
    assert backup != PROJECT_ROOT / "assets" / "avatars" / "pilgrim_shepherd.png"


def test_drop_post_restart_expiry_and_mentions(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True, rng=ScriptedRng([0.0]))
    channel = _wire(cog, bot)
    when = datetime(2026, 11, 21, 12, 0, tzinfo=PT)
    _arm(cog, [_slot(when, "abcd1234")])
    path = cog.book.path

    async def post_once():
        await cog.tick(when)
        assert len(channel.sent) == 1
        assert "A turkey waddled into the channel." in channel.sent[0]["content"]
        assert channel.sent[0]["kwargs"]["view"] is not None
        _no_pings(channel.sent[0]["kwargs"])
        assert "content" not in channel.sent[0]["kwargs"] or channel.sent[0]["kwargs"].get("content") in (None, "")
        assert cog.book.state["drops"]["abcd1234"]["message_id"] == channel.sent[0]["id"]
        again, again_bot = _cog(tmp_path, enabled=True, path=path)
        _wire(again, again_bot)
        await again.tick(when)
        assert again_bot.channels[GENERAL_ID].sent == []
        assert again.book.state["drops"]["abcd1234"]["message_id"] == channel.sent[0]["id"]

    asyncio.run(post_once())

    expiry_cog, expiry_bot = _cog(tmp_path / "expire", enabled=True)
    expiry_channel = _wire(expiry_cog, expiry_bot)
    _arm(expiry_cog, [])
    expiry_channel.preseed(81, "A turkey waddled into the channel.", expiry_bot.user)
    _open_drop(expiry_cog, "abcd1234", when, lifetime_min=45, message_id=81)

    async def expire():
        await expiry_cog.tick(when + timedelta(minutes=45))
        assert 81 not in expiry_channel.messages
        assert expiry_cog.book.state["drops"]["abcd1234"]["status"] == "expired"

    asyncio.run(expire())


def test_send_failure_reverts_the_slot_and_a_later_tick_posts_once(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True, rng=ScriptedRng([0.99]))
    channel = _wire(cog, bot)
    when = datetime(2026, 11, 21, 15, 0, tzinfo=PT)
    _arm(cog, [_slot(when, "abcd1234")])
    channel.fail_send = True

    async def run():
        await cog.tick(when)
        assert channel.sent == []
        assert cog.book.state["schedule"][0]["status"] == "pending"
        assert "abcd1234" not in cog.book.state["drops"]
        channel.fail_send = False
        await cog.tick(when)
        assert len(channel.sent) == 1
        assert cog.book.state["schedule"][0]["status"] == "posted"

    asyncio.run(run())


def test_sunday_close_deletes_open_turkeys_and_refuses_later_catches(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    settings = cog.settings
    end = settings.drops_end
    scheduled = end - timedelta(minutes=30)
    _arm(cog, [_slot(end + timedelta(minutes=10), "abcd5555")])
    channel.preseed(44, "A turkey waddled into the channel.", bot.user)
    _open_drop(cog, "abcd1234", scheduled, lifetime_min=45, message_id=44)
    cog._clock = lambda: end

    async def run():
        await cog.tick(end)
        assert 44 not in channel.messages
        assert cog.book.state["drops"]["abcd1234"]["status"] == "closed"
        assert cog.book.state["schedule"][0]["status"] == "missed"
        assert all("abcd5555" not in message["content"] for message in channel.sent)
        interaction = _interaction(cog, 8)
        await cog.handle_claim(interaction, "abcd1234")
        assert cog.book.state["scores"] == {}
        interaction.response.send_message.assert_awaited()
        assert "closed" in interaction.response.send_message.await_args.args[0].casefold()

    asyncio.run(run())


def test_claims_grease_dm_failure_and_first_click(tmp_path):
    cog, bot = _cog(tmp_path, enabled=False, rng=ScriptedRng([0.0, 0.99]))
    channel = _wire(cog, bot)
    when = datetime(2026, 11, 21, 12, 0, tzinfo=PT)
    cog._clock = lambda: when
    _arm(cog, [])
    channel.preseed(50, "A turkey waddled into the channel. Regular", bot.user)
    channel.preseed(51, "A turkey waddled into the channel. Plump", bot.user)
    _open_drop(cog, "abcd1234", when, lifetime_min=45, message_id=50, type_key="regular")
    _open_drop(cog, "abcd1235", when, lifetime_min=45, message_id=51, type_key="plump")
    before = channel.messages[50].content

    async def slip_then_catch():
        slip = _interaction(cog, 5, name="@everyone Ada")
        await cog.handle_claim(slip, "abcd1234")
        assert cog.book.state["scores"] == {}
        assert cog.book.state["drops"]["abcd1234"]["status"] == "open"
        assert cog.book.state["slips"] == 1
        slip.response.edit_message.assert_not_awaited()
        slip_text_sent = slip.response.send_message.await_args.args[0]
        assert "Greased" in slip_text_sent
        assert slip.response.send_message.await_args.kwargs["ephemeral"] is True
        _no_pings(slip.response.send_message.await_args.kwargs)
        assert channel.messages[50].content == before
        closed = AsyncMock(side_effect=discord.Forbidden(_HTTPResponse(), "closed"))
        catch = _interaction(cog, 5, name="Ada", send=closed)
        await cog.handle_claim(catch, "abcd1235")
        assert cog.book.state["scores"] == {"5": 30}
        assert "@everyone" not in cog.book.state["names"]["5"]
        catch.response.edit_message.assert_awaited()
        _no_pings(catch.response.edit_message.await_args.kwargs)
        closed.assert_awaited()
        assert "30 points" in closed.await_args.args[0]
        taken = _interaction(cog, 6)
        await cog.handle_claim(taken, "abcd1235")
        assert cog.book.state["scores"] == {"5": 30}
        assert "already" in taken.response.send_message.await_args.args[0].casefold()

    asyncio.run(slip_then_catch())


def test_two_clicks_at_once_award_one_catch(tmp_path):
    cog, _bot = _cog(tmp_path, enabled=False)
    when = datetime(2026, 11, 21, 12, 0, tzinfo=PT)
    cog._clock = lambda: when
    _arm(cog, [])
    _open_drop(cog, "abcd1234", when, lifetime_min=45, message_id=50)

    async def run():
        first = _interaction(cog, 5)
        second = _interaction(cog, 6)
        await asyncio.gather(
            cog.handle_claim(first, "abcd1234"),
            cog.handle_claim(second, "abcd1234"),
        )
        assert sum(cog.book.state["scores"].values()) == TurkeySettings().regular_points == 10
        edits = int(first.response.edit_message.await_count) + int(second.response.edit_message.await_count)
        assert edits == 1

    asyncio.run(run())


def test_recap_posts_after_sunday_close_and_monday_skips_a_late_recap(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    settings = cog.settings
    _arm(cog, [])
    cog.book.state["recap_done"].pop("2026-11-29", None)
    cog.book.state["scores"] = {"5": 100}
    cog.book.state["names"] = {"5": "Ada"}
    cog.book.state["catches"] = [
        {"user_id": "5", "type_key": "golden", "name": "Golden", "points": 100, "day": "2026-11-29"}
    ]
    cog.book.save()

    async def recap():
        await cog.tick(datetime(2026, 11, 29, 22, 30, tzinfo=PT))
        recaps = [row for row in channel.sent if "Turkey tally" in row["content"]]
        assert len(recaps) == 1
        assert "Ada" in recaps[0]["content"]
        assert "Golden" in recaps[0]["content"]
        _no_pings(recaps[0]["kwargs"])
        assert cog.book.state["closed"] is True

    asyncio.run(recap())

    late, late_bot = _cog(tmp_path / "late", enabled=True)
    late_channel = _wire(late, late_bot)
    _arm(late, [])
    late.book.state["recap_done"] = {}
    late.book.state["scores"] = {"5": 4}
    late.book.state["names"] = {"5": "Ada"}
    late.book.save()

    async def final_only():
        await late.tick(settings.final_at)
        assert not any("Turkey tally" in row["content"] for row in late_channel.sent)
        assert any("Turkey hunt champion" in row["content"] for row in late_channel.sent)

    asyncio.run(final_only())


def test_monday_final_keeps_the_winner_deletes_recaps_and_restores_avatar(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    settings = cog.settings
    backup = tmp_path / "backup.png"
    backup.write_bytes(b"original-bytes")
    _arm(cog, [])
    cog.book.state["scores"] = {"5": 8, "6": 8}
    cog.book.state["names"] = {"5": "Ada", "6": "Bea"}
    cog.book.state["catches"] = []
    cog.book.state["slips"] = 2
    cog.book.state["avatar_applied"] = True
    cog.book.state["had_global_avatar"] = True
    cog.book.state["avatar_backup"] = str(backup)
    cog.book.state["kickoff_message"] = {"channel_id": channel.id, "message_id": 71}
    channel.preseed(71, "The turkey hunt is on. Kickoff stays.", bot.user)
    channel.preseed(70, "Turkey tally for Sunday.", bot.user)
    channel.preseed(44, "A turkey waddled into the channel.", bot.user)
    channel.preseed(80, "A turkey waddled into the channel.", SimpleNamespace(id=5))
    cog.book.state["recaps"]["2026-11-29"] = {"channel_id": channel.id, "message_id": 70}
    _open_drop(cog, "abcd1234", settings.drops_end - timedelta(minutes=20), lifetime_min=45, message_id=44)
    cog.book.state["drops"]["userdrop"] = dict(cog.book.state["drops"]["abcd1234"])
    cog.book.state["drops"]["userdrop"]["id"] = "userdrop"
    cog.book.state["drops"]["userdrop"]["message_id"] = 80
    announce = HuntChannel(cog.settings.cleanup_skip_channel_ids[0], "announcements", cog.settings.guild_id)
    announce.speaker = bot.user
    bot.channels[announce.id] = announce
    announce.preseed(90, "Turkey tally should stay because this is announcements.", bot.user)
    cog.book.state["recaps"]["2026-11-28"] = {"channel_id": announce.id, "message_id": 90}
    named = HuntChannel(2222, "town-announcements", cog.settings.guild_id)
    named.speaker = bot.user
    bot.channels[named.id] = named
    named.preseed(91, "Turkey tally by name.", bot.user)
    cog.book.save()

    async def run():
        await cog.safe_delete(DeleteRef(named.id, 91, "recap"))
        assert 91 in named.messages
        channel.fail_delete.add(70)
        await cog.tick(settings.final_at)
        winners = [row for row in channel.sent if "Turkey hunt champion" in row["content"]]
        assert len(winners) == 1
        assert "It's a tie!" in winners[0]["content"]
        assert "Prize: (not set yet)" in winners[0]["content"]
        _no_pings(winners[0]["kwargs"])
        assert winners[0]["id"] in channel.messages
        assert 71 in channel.messages
        assert 80 in channel.messages
        assert 44 not in channel.messages
        assert 70 in channel.messages
        assert 90 in announce.messages
        assert cog.book.state["final_done"] is True
        assert cog.book.state["scores"] == {}
        assert cog.book.state["event_disabled"] is True
        assert cog.book.state["winner_message"]["message_id"] == winners[0]["id"]
        archive = json.loads((tmp_path / "turkey_hunt_archive_2026.json").read_text(encoding="utf-8"))
        assert archive["scores"] == {"5": 8, "6": 8}
        bot.user.edit.assert_awaited()
        assert bot.user.edit.await_args.kwargs["avatar"] == b"original-bytes"
        sent_before = len(channel.sent)
        await cog.tick(settings.final_at + timedelta(minutes=5))
        assert len(channel.sent) == sent_before
        assert bot.user.edit.await_count == 1

    asyncio.run(run())


def test_delete_failure_does_not_stop_the_next_delete(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    channel.preseed(1, "A turkey waddled into the channel.", bot.user)
    channel.preseed(2, "A turkey waddled into the channel.", bot.user)
    channel.fail_delete.add(1)

    async def run():
        first = await cog.safe_delete(DeleteRef(channel.id, 1, "drop"))
        second = await cog.safe_delete(DeleteRef(channel.id, 2, "drop"))
        assert first is False
        assert second is True
        assert 1 in channel.messages
        assert 2 not in channel.messages

    asyncio.run(run())


def test_commands_exist_and_are_not_synced(tmp_path):
    source = inspect.getsource(__import__("cogs.turkeyhunt", fromlist=["*"]))
    assert "sync_commands" not in source
    assert "tree.sync" not in source
    assert ".sync(" not in source
    assert "utils.get" not in source
    hunt_source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "cogs" / "hunt").glob("*.py"))
    assert "utils.get" not in hunt_source
    names = [command.name for command in TurkeyHunt.__cog_app_commands__]
    assert names == ["turkeys"]
    assert TurkeyHunt.turkeys.guild_only is True
    assert list(TurkeyHunt.turkeys._guild_ids) == [MTS_GUILD_ID, SUPPORT_GUILD_ID]
    group = TurkeyHunt.turkeyadmin
    assert isinstance(group, commands.Group)
    assert not isinstance(group, discord.app_commands.Group)
    assert group.hidden is True
    assert group.case_insensitive is True
    assert {command.name for command in group.commands} == {
        "status",
        "schedule",
        "cleanup",
        "testdrop",
        "test_session",
        "test_session_stop",
        "simulate",
        "power",
    }
    assert all(command.hidden for command in group.commands)
    view = build_drop_view("abcd1234", "A turkey waddled into the channel.")
    ids = [getattr(item, "custom_id", None) for item in view.walk_children()]
    assert "turkey:claim:abcd1234" in ids
    assert TurkeyClaimButton("abcd1234").template.fullmatch("turkey:claim:abcd1234")
    assert CLAIM_TEMPLATE.startswith("turkey:claim:")

    cog, bot = _cog(tmp_path, enabled=False)

    async def load():
        await cog.cog_load()
        try:
            bot.tree.sync.assert_not_called()
            assert TurkeyClaimButton in bot.dynamic_items
        finally:
            cog.turkey_loop.cancel()
            task = cog.turkey_loop.get_task()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(load())


def test_turkeys_leaderboard_is_guild_scoped(tmp_path):
    cog, _bot = _cog(tmp_path, enabled=False)
    cog.book.state["scores"] = {"5": 4}
    cog.book.state["names"] = {"5": "Ada"}
    cog.book.state["test_scores"] = {"5": 9}
    cog.book.state["test_names"] = {"5": "Ada"}

    async def run():
        home = _interaction(cog, 5, guild_id=MTS_GUILD_ID)
        await cog.turkeys.callback(cog, home)
        text = home.response.send_message.await_args.args[0]
        assert "Your score: 4" in text
        assert "Test turkeys" not in text
        _no_pings(home.response.send_message.await_args.kwargs)
        support = _interaction(cog, 5, guild_id=SUPPORT_GUILD_ID)
        await cog.turkeys.callback(cog, support)
        assert "Test turkeys" in support.response.send_message.await_args.args[0]
        assert "Your score: 9" in support.response.send_message.await_args.args[0]
        other = _interaction(cog, 5, guild_id=1)
        await cog.turkeys.callback(cog, other)
        assert other.response.send_message.await_args.kwargs["ephemeral"] is True

    asyncio.run(run())


def test_admin_is_silent_for_everyone_else(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path, enabled=False)
        _bind(cog)
        cog.bot.is_owner = AsyncMock(return_value=False)
        stranger = _ctx(cog, 5, guild_id=SUPPORT_GUILD_ID)
        assert await _turkeyadmin_allowed(stranger) is False
        await cog.admin_status.callback(cog, stranger)
        await cog.admin_power.callback(cog, stranger, "on")
        await cog.turkeyadmin.callback(cog, stranger)
        await cog.cog_command_error(stranger, commands.CheckFailure("no"))
        await cog.cog_command_error(stranger, commands.BadArgument("nope"))
        _assert_silent(stranger)
        stranger.view = StringView("power on")
        try:
            await cog.turkeyadmin.invoke(stranger)
        except commands.CheckFailure as exc:
            await cog.cog_command_error(stranger, exc)
        else:
            raise AssertionError("a stranger should fail the owner check")
        _assert_silent(stranger)
        assert cog.book.state["event_disabled"] is False

        nick = _ctx(cog, NICK_ID, guild_id=SUPPORT_GUILD_ID)
        assert await cog.turkeyadmin.can_run(nick) is True
        elsewhere = _ctx(cog, NICK_ID, guild_id=1)
        assert await cog.turkeyadmin.can_run(elsewhere) is False
        dm = _ctx(cog, NICK_ID, guild_id=None)
        assert await cog.admin_status.can_run(dm) is True

        owner = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        owner.command = cog.admin_schedule
        await cog.cog_command_error(owner, commands.BadArgument("nope"))
        usage, kwargs = _replied(owner)
        assert usage == TURKEYADMIN_USAGE
        assert kwargs["mention_author"] is False

    asyncio.run(run())


def test_admin_schedule_simulate_power_and_test_drops(tmp_path):
    cog, bot = _cog(tmp_path, enabled=False, rng=random.Random(1))
    _bind(cog)
    support = HuntChannel(SUPPORT_CHANNEL_ID, "bot-test", SUPPORT_GUILD_ID)
    support.speaker = bot.user
    bot.channels[support.id] = support
    general = _wire(cog, bot)

    async def run():
        preview = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        before = cog.book.path.exists()
        await cog.admin_schedule.callback(cog, preview)
        text, _kwargs = _replied(preview)
        assert "Unsaved preview." in text
        assert cog.book.path.exists() is before
        save = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_schedule.callback(cog, save, "save")
        assert "Schedule saved." in _replied(save)[0]
        assert cog.book.state["schedule_built"] is True
        saved = cog.book.path.read_bytes()
        sim = _ctx(cog, guild_id=None)
        await cog.admin_simulate.callback(cog, sim, "2026-11-20T05:00:00-08:00")
        body, _kwargs = _replied(sim)
        assert "Simulation only" in body
        assert cog.book.path.read_bytes() == saved

        power = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_power.callback(cog, power, "off")
        assert cog.book.state["event_disabled"] is True
        await cog.admin_power.callback(cog, power, "on")
        assert cog.book.state["event_disabled"] is False
        assert "thanksgiving_enabled" in _replied(power)[0]

        refused = _ctx(cog, guild_id=MTS_GUILD_ID, channel=general)
        await cog.admin_testdrop.callback(cog, refused, str(GENERAL_ID))
        assert "stay out" in _replied(refused)[0].casefold()
        assert cog.book.state["test_drops"] == {}
        assert general.sent == []

        posted = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=support)
        cog._clock = lambda: datetime(2026, 11, 21, 12, tzinfo=PT)
        await cog.admin_testdrop.callback(cog, posted, f"<#{SUPPORT_CHANNEL_ID}>")
        assert support.sent
        assert "TEST TURKEY" in support.sent[-1]["content"]
        _no_pings(support.sent[-1]["kwargs"])
        assert cog.book.state["scores"] == {}
        drop_id = next(iter(cog.book.state["test_drops"]))
        claim = _interaction(cog, 5, guild_id=SUPPORT_GUILD_ID)
        cog.rng = ScriptedRng([0.99])
        await cog.handle_claim(claim, drop_id)
        assert cog.book.state["scores"] == {}
        assert cog.book.state["test_scores"]

        session = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=support)
        await cog.admin_test_session.callback(cog, session, "15", "5", str(SUPPORT_CHANNEL_ID))
        assert "Test session started" in _replied(session)[0]
        assert any(row.get("test") for row in cog.book.state["test_drops"].values())
        mts_session = _ctx(cog, guild_id=MTS_GUILD_ID, channel=general)
        await cog.admin_test_session.callback(cog, mts_session, "15", "5")
        assert "Menaces" in _replied(mts_session)[0]

        status = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_status.callback(cog, status)
        assert "live switch: off" in _replied(status)[0]

    asyncio.run(run())


def test_cleanup_defaults_to_dry_run_and_real_deletes_only_our_hunt_messages(tmp_path):
    cog, bot = _cog(tmp_path, enabled=False)
    _bind(cog)
    channel = _wire(cog, bot)
    channel.preseed(44, "A turkey waddled into the channel.", bot.user)
    channel.preseed(71, "The turkey hunt is on.", bot.user)
    _arm(cog, [])
    cog.book.state["kickoff_message"] = {"channel_id": channel.id, "message_id": 71}
    _open_drop(cog, "abcd1234", datetime(2026, 11, 21, 12, tzinfo=PT), lifetime_min=45, message_id=44)

    async def run():
        dry = _ctx(cog, guild_id=MTS_GUILD_ID, channel=channel)
        await cog.admin_cleanup.callback(cog, dry)
        report, _kwargs = _replied(dry)
        assert report.startswith("Dry run.")
        assert 44 in channel.messages
        support = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_cleanup.callback(cog, support, "false")
        assert "only runs in Menaces" in _replied(support)[0]
        assert 44 in channel.messages
        real = _ctx(cog, guild_id=MTS_GUILD_ID, channel=channel)
        await cog.admin_cleanup.callback(cog, real, "false")
        assert 44 not in channel.messages
        assert 71 in channel.messages

    asyncio.run(run())


def test_local_pause_blocks_the_live_tick_even_when_the_switch_is_on(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    when = datetime(2026, 11, 21, 12, tzinfo=PT)
    _arm(cog, [_slot(when, "abcd1234")])
    cog.book.state["event_disabled"] = True
    cog.book.save()

    async def run():
        await cog.tick(when)
        assert channel.sent == []
        assert cog.book.state["schedule"][0]["status"] == "pending"

    asyncio.run(run())


def test_bots_cannot_catch(tmp_path):
    cog, _bot = _cog(tmp_path, enabled=False)
    when = datetime(2026, 11, 21, 12, tzinfo=PT)
    cog._clock = lambda: when
    _arm(cog, [])
    _open_drop(cog, "abcd1234", when, lifetime_min=45, message_id=50)

    async def run():
        interaction = _interaction(cog, 9, bot=True)
        await cog.handle_claim(interaction, "abcd1234")
        assert cog.book.state["scores"] == {}
        assert cog.book.state["drops"]["abcd1234"]["status"] == "open"

    asyncio.run(run())


def test_winner_send_failure_leaves_the_final_unfinished(tmp_path):
    cog, bot = _cog(tmp_path, enabled=True)
    channel = _wire(cog, bot)
    _arm(cog, [])
    cog.book.state["scores"] = {"5": 6}
    cog.book.state["names"] = {"5": "Ada"}
    cog.book.save()
    channel.fail_send = True

    async def run():
        await cog.tick(cog.settings.final_at)
        assert cog.book.state["final_done"] is False
        assert not (tmp_path / "turkey_hunt_archive_2026.json").exists()
        assert channel.sent == []
        channel.fail_send = False
        await cog.tick(cog.settings.final_at)
        winners = [row for row in channel.sent if "Turkey hunt champion" in row["content"]]
        assert len(winners) == 1
        assert "Ada wins the turkey hunt" in winners[0]["content"]
        assert TIE_WORD.search(winners[0]["content"]) is None
        assert cog.book.state["final_done"] is True
        await cog.tick(cog.settings.final_at)
        assert sum(1 for row in channel.sent if "Turkey hunt champion" in row["content"]) == 1

    asyncio.run(run())


def test_deletable_markers_leave_kickoff_and_winner_alone():
    settings = TurkeySettings()
    assert is_deletable_hunt_message(kickoff_text(settings)) is False
    assert is_deletable_hunt_message(winner_text(settings, {"1": 1}, {"1": "Ada"}, [], 0)) is False
    assert is_deletable_hunt_message("A turkey waddled into the channel.") is True
    assert is_deletable_hunt_message("Turkey tally for Sunday") is True
    assert not contains_role_ping(kickoff_text(settings))


def _load_ctl_script():
    path = ROOT / "scripts" / "turkeyhunt_ctl.py"
    spec = importlib.util.spec_from_file_location("turkeyhunt_ctl_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(cog: TurkeyHunt, request_id: str) -> dict:
    path = cog.ctl_root / "results" / f"{request_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_inbox(cog: TurkeyHunt, name: str, text: str) -> Path:
    inbox = cog.ctl_root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(text, encoding="utf-8")
    return path


def _watch_fetch(bot: FakeBot) -> list:
    fetched: list = []
    original = bot.fetch_channel

    async def watch(channel_id):
        fetched.append(channel_id)
        return await original(channel_id)

    bot.fetch_channel = watch
    return fetched


def test_turkey_point_defaults_are_10_30_100(tmp_path):
    """Old 1/3/10 defaults, docs, or copy would fail this without banning '1 in 10'."""
    settings = TurkeySettings()
    assert (settings.regular_points, settings.plump_points, settings.golden_points) == (10, 30, 100)
    items = {item.key: item.points for item in settings.items()}
    assert items == {"regular": 10, "plump": 30, "golden": 100}
    get_server_config_manager().clear_cache(MTS_GUILD_ID)
    loaded = load_turkey_settings()
    assert (loaded.regular_points, loaded.plump_points, loaded.golden_points) == (10, 30, 100)

    for name in ("example.json", "932057681307512922.json"):
        raw = json.loads((ROOT / "server_configs" / name).read_text(encoding="utf-8"))
        hunt = raw["turkey_hunt"]
        assert (hunt["regular_points"], hunt["plump_points"], hunt["golden_points"]) == (10, 30, 100)
        applied = apply_turkey_hunt_overrides(TurkeySettings(), hunt)
        assert (applied.regular_points, applied.plump_points, applied.golden_points) == (10, 30, 100)

    readme = (ROOT / "docs" / "README_turkeyhunt.md").read_text(encoding="utf-8")
    configs = (ROOT / "docs" / "SERVER_CONFIGS.md").read_text(encoding="utf-8")
    assert "Regular 75% / 10 points, Plump 20% / 30 points, Golden 5% / 100 points." in readme
    assert "Regular 75% / 1 point" not in readme
    assert "Defaults: `10`, `30`, `100`." in configs
    assert "Defaults: `1`, `3`, `10`." not in configs
    copy_source = (ROOT / "cogs" / "hunt" / "copy.py").read_text(encoding="utf-8")
    assert "1 point" not in copy_source
    assert "3 points" not in copy_source
    assert "10 points" not in copy_source

    kickoff = kickoff_text(settings)
    assert "Regular — 75% — 10 points." in kickoff
    assert "Plump — 20% — 30 points." in kickoff
    assert "Golden — 5% — 100 points." in kickoff
    assert "1 in 10" in kickoff
    assert "10:00 PM" in kickoff
    assert "10:00 AM" in kickoff
    when = datetime(2026, 11, 21, 12, tzinfo=PT)
    for item in settings.items():
        posted = drop_text(item, when + timedelta(minutes=45), test=False, drops_end=settings.drops_end)
        assert f"{item.points} points" in posted
        note = catch_dm("Ada", item, item.points, 1, test=False)
        assert f"That's {item.points} points" in note

    book = _book(tmp_path)
    day = "2026-11-21"
    catches = []
    total = 0
    for item in settings.items():
        drop_id = f"{item.key[:4]}1234"
        book.state["drops"][drop_id] = {
            "id": drop_id,
            "channel_id": GENERAL_ID,
            "message_id": 9,
            "type_key": item.key,
            "name": item.name,
            "emoji": item.emoji,
            "points": item.points,
            "grease_chance": 0.0,
            "posted_at": when.isoformat(),
            "scheduled_at": when.isoformat(),
            "expires_at": (when + timedelta(minutes=45)).isoformat(),
            "status": "open",
            "caught_by": None,
            "day": day,
            "test": False,
        }

        async def claim(drop_id=drop_id):
            return await book.claim(drop_id, 5, when, ScriptedRng([0.99]), name="Ada", is_bot=False)

        result = asyncio.run(claim())
        total += item.points
        assert result.status == "caught"
        assert result.total == total
        assert f"That's {item.points} points" in result.dm_text
        catches.append(
            {"user_id": "5", "type_key": item.key, "name": item.name, "points": item.points, "day": day}
        )
    assert book.state["scores"] == {"5": 140}
    assert book.state["test_scores"] == {}
    recap = recap_text(settings, when.date(), book.state["scores"], {"5": "Ada"}, catches)
    assert "Ada — Regular — 10" in recap
    assert "Ada — Plump — 30" in recap
    assert "Ada — Golden — 100" in recap
    final = winner_text(settings, book.state["scores"], {"5": "Ada"}, catches, 0)
    assert "140 points" in final
    assert "Ada wins the turkey hunt" in final


def test_ctl_cli_writes_a_command_and_prints_the_result(tmp_path, capsys):
    script = _load_ctl_script()
    source = (ROOT / "scripts" / "turkeyhunt_ctl.py").read_text(encoding="utf-8")
    ctl_source = (ROOT / "cogs" / "hunt" / "ctl.py").read_text(encoding="utf-8")
    hunt_source = (ROOT / "cogs" / "turkeyhunt.py").read_text(encoding="utf-8")
    assert "from cogs.candyhunt import SUPPORT_TEST_CHANNEL_ID" in source
    assert "from cogs.candyhunt import SUPPORT_TEST_CHANNEL_ID" in ctl_source
    for text in (source, ctl_source, hunt_source):
        assert "1045149015756521493" not in text
        assert "HTTPServer" not in text
        assert "socket.socket" not in text
        assert "asyncio.start_server" not in text
    assert "import discord" not in source
    assert "load_config" not in source
    assert "discord.Client" not in source
    assert default_ctl_root() == DATA_DIR / "turkey_hunt_ctl"
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/turkey_hunt_ctl/" in ignore
    assert "data/*.json" in ignore
    assert "data/*.png" not in ignore

    cases = (
        (["testdrop"], "testdrop", SUPPORT_TEST_CHANNEL_ID, None, None),
        (["testdrop", "--channel", "5"], "testdrop", 5, None, None),
        (["test_session", "start"], "test_session_start", SUPPORT_TEST_CHANNEL_ID, 15, 2),
        (["test_session", "stop"], "test_session_stop", None, None, None),
        (["status"], "status", None, None, None),
        (["schedule"], "schedule", None, None, None),
    )
    for argv, action, channel_id, minutes, interval in cases:
        payload = script.command_from_args(script.build_parser().parse_args(argv), request_id="a" * 32)
        assert payload["action"] == action
        assert payload["id"] == "a" * 32
        assert payload.get("channel_id") == channel_id
        assert payload.get("minutes") == minutes
        assert payload.get("interval") == interval

    root = tmp_path / "cli"
    request_id = "b" * 32
    payload = script.command_from_args(script.build_parser().parse_args(["status"]), request_id=request_id)
    path = write_command(root, payload)
    assert path.parent == root / "inbox"
    assert path.is_file()
    assert list(path.parent.glob("*.tmp")) == []
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["action"] == "status"
    write_result(
        root,
        {"ok": True, "action": "status", "id": request_id, "text": "live switch: off", "channel_id": None, "error": None},
    )
    found = wait_for_result(root, request_id, 1)
    assert found is not None
    assert found["ok"] is True
    assert found["text"] == "live switch: off"

    def serve_failure():
        deadline = time.time() + 10
        inbox = root / "fail" / "inbox"
        while time.time() < deadline:
            files = list(inbox.glob("*.json")) if inbox.exists() else []
            if files:
                body = json.loads(files[0].read_text(encoding="utf-8"))
                write_result(
                    root / "fail",
                    {
                        "ok": False,
                        "action": body["action"],
                        "id": body["id"],
                        "text": "Test drops stay out of Menaces to Sobriety and the live drop channel.",
                        "channel_id": body.get("channel_id"),
                        "error": "refused",
                    },
                )
                return
            time.sleep(0.05)

    worker = threading.Thread(target=serve_failure)
    worker.start()
    code = script.main(["--root", str(root / "fail"), "testdrop", "--timeout", "8"])
    worker.join(timeout=5)
    captured = capsys.readouterr()
    assert code == 1
    assert "stay out" in captured.out.casefold()

    code = script.main(["--root", str(tmp_path / "empty"), "--timeout", "0.3", "schedule"])
    captured = capsys.readouterr()
    assert code == 1
    assert "Timed out waiting for the turkey hunt cog." in captured.out

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "turkeyhunt_ctl.py"), "--root", str(root / "sub"), "--timeout", "20", "status"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 30
    body = None
    inbox = root / "sub" / "inbox"
    while time.time() < deadline:
        files = list(inbox.glob("*.json")) if inbox.exists() else []
        if files:
            body = json.loads(files[0].read_text(encoding="utf-8"))
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert body is not None
    assert body["action"] == "status"
    write_result(
        root / "sub",
        {"ok": True, "action": "status", "id": body["id"], "text": "status from the cog", "channel_id": None, "error": None},
    )
    stdout, stderr = proc.communicate(timeout=20)
    assert proc.returncode == 0
    assert stdout.strip() == "status from the cog"
    assert "Traceback" not in stderr


def test_ctl_testdrop_refuses_live_targets_and_keeps_the_test_tally(tmp_path):
    when = datetime(2026, 11, 21, 12, tzinfo=PT)
    # 0.0 rolls Regular. The next draw is the grab, and 0.0 would slip on a 1-in-10 Regular.
    cog, bot = _cog(tmp_path, enabled=False, rng=ScriptedRng([0.0, 0.99]), clock=lambda: when)
    assert cog.live_enabled() is False
    general = _wire(cog, bot)
    support = HuntChannel(SUPPORT_CHANNEL_ID, "bot-test", SUPPORT_GUILD_ID)
    support.speaker = bot.user
    side = HuntChannel(424242424242, "side-room", MTS_GUILD_ID)
    side.speaker = bot.user
    announce_id = cog.settings.cleanup_skip_channel_ids[0]
    announce = HuntChannel(announce_id, "announcements", SUPPORT_GUILD_ID)
    announce.speaker = bot.user
    bot.channels[support.id] = support
    bot.channels[side.id] = side
    bot.channels[announce.id] = announce
    fetched = _watch_fetch(bot)
    _arm(cog, [_slot(when, "abcd1234")])

    async def run():
        refused = (
            ("testdrop", GENERAL_ID),
            ("testdrop", announce_id),
            ("testdrop", side.id),
            ("test_session_start", GENERAL_ID),
            ("test_session_start", announce_id),
            ("test_session_start", side.id),
        )
        for action, channel_id in refused:
            request_id = new_request_id()
            fields = {"id": request_id, "action": action, "channel_id": channel_id}
            if action == "test_session_start":
                fields["minutes"] = 15
                fields["interval"] = 2
            write_command(cog.ctl_root, fields)
            await cog.tick(when)
            result = _result(cog, request_id)
            assert result["ok"] is False
            assert result["error"] == "refused"
            assert "Menaces" in result["text"] or "live drop" in result["text"]
        assert general.sent == []
        assert support.sent == []
        assert side.sent == []
        assert announce.sent == []
        assert cog.book.state["test_drops"] == {}
        assert cog.book.state["test_session"] is None
        assert cog.book.state["scores"] == {}
        assert GENERAL_ID not in fetched
        assert announce_id not in fetched
        assert cog.book.state["schedule"][0]["status"] == "pending"

        missing = new_request_id()
        write_command(cog.ctl_root, {"id": missing, "action": "testdrop", "channel_id": 888888888888888888})
        await cog.tick(when)
        missed = _result(cog, missing)
        assert missed["ok"] is False
        assert missed["error"] == "unresolved"
        assert general.sent == []
        assert support.sent == []

        omitted = new_request_id()
        write_command(cog.ctl_root, {"id": omitted, "action": "testdrop"})
        await cog.tick(when)
        posted = _result(cog, omitted)
        assert posted["ok"] is True
        assert posted["channel_id"] == SUPPORT_TEST_CHANNEL_ID
        assert "does not count on the live board" in posted["text"]
        assert len(support.sent) == 1
        assert "TEST TURKEY" in support.sent[0]["content"]
        assert "10 points" in support.sent[0]["content"]
        assert general.sent == []
        assert cog.book.state["scores"] == {}
        drop_id = next(iter(cog.book.state["test_drops"]))
        claim = _interaction(cog, 5, guild_id=SUPPORT_GUILD_ID)
        await cog.handle_claim(claim, drop_id)
        assert cog.book.state["scores"] == {}
        assert cog.book.state["test_scores"] == {"5": 10}
        assert "10 points" in claim.user.send.await_args.args[0]
        assert "test tally" in claim.user.send.await_args.args[0]
        assert cog.book.state["schedule"][0]["status"] == "pending"

        before = len(support.sent)
        write_command(cog.ctl_root, {"id": omitted, "action": "testdrop"})
        await cog.tick(when)
        assert len(support.sent) == before
        assert _result(cog, omitted)["ok"] is True

        support.fail_send = True
        failed = new_request_id()
        write_command(cog.ctl_root, {"id": failed, "action": "testdrop", "channel_id": SUPPORT_CHANNEL_ID})
        await cog.tick(when)
        failure = _result(cog, failed)
        assert failure["ok"] is False
        assert failure["error"] == "send_failed"
        assert len(support.sent) == before
        assert all(row.get("status") != "open" for row in cog.book.state["test_drops"].values())
        assert cog.book.state["scores"] == {}
        support.fail_send = False

    asyncio.run(run())


def test_ctl_session_status_schedule_and_bad_files_do_not_crash_or_save(tmp_path):
    when = datetime(2026, 11, 21, 12, tzinfo=PT)
    cog, bot = _cog(tmp_path, enabled=False, rng=ScriptedRng([0.0] * 8), clock=lambda: when)
    support = HuntChannel(SUPPORT_CHANNEL_ID, "bot-test", SUPPORT_GUILD_ID)
    support.speaker = bot.user
    bot.channels[support.id] = support
    general = _wire(cog, bot)
    cog.book.start_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=when,
    )
    bad_id = "m" * 16
    _raw_inbox(cog, f"{bad_id}.json", "{")

    async def run():
        await cog.tick(when)
        assert len(support.sent) == 1
        assert "TEST TURKEY" in support.sent[0]["content"]
        bad = _result(cog, bad_id)
        assert bad["ok"] is False
        assert bad["error"] == "malformed"
        assert not (cog.ctl_root / "inbox" / f"{bad_id}.json").exists()
        assert general.sent == []

        stop_id = new_request_id()
        write_command(cog.ctl_root, {"id": stop_id, "action": "test_session_stop"})
        await cog.tick(when)
        assert _result(cog, stop_id)["ok"] is True
        assert "stopped" in _result(cog, stop_id)["text"].casefold()
        assert cog.book.state["test_session"] is None

        start_id = new_request_id()
        write_command(cog.ctl_root, {"id": start_id, "action": "test_session_start"})
        await cog.tick(when)
        started = _result(cog, start_id)
        assert started["ok"] is True
        assert started["channel_id"] == SUPPORT_TEST_CHANNEL_ID
        assert "for 15 minutes" in started["text"]
        assert "every 2 minutes" in started["text"]
        assert len(support.sent) == 2
        assert cog.book.state["scores"] == {}
        assert cog.book.state["test_session"]["channel_id"] == SUPPORT_CHANNEL_ID
        ends_at = cog.book.state["test_session"]["ends_at"]

        busy_id = new_request_id()
        write_command(
            cog.ctl_root,
            {"id": busy_id, "action": "test_session_start", "minutes": 5, "interval": 1},
        )
        await cog.tick(when)
        busy = _result(cog, busy_id)
        assert busy["ok"] is False
        assert busy["error"] == "busy"
        assert cog.book.state["test_session"]["ends_at"] == ends_at
        assert len(support.sent) == 2

        wide_id = new_request_id()
        write_command(
            cog.ctl_root,
            {
                "id": wide_id,
                "action": "test_session_start",
                "channel_id": SUPPORT_CHANNEL_ID,
                "minutes": 9999,
                "interval": 2,
            },
        )
        await cog.tick(when)
        assert _result(cog, wide_id)["error"] == "invalid"
        assert cog.book.state["test_session"]["ends_at"] == ends_at

        again = new_request_id()
        write_command(cog.ctl_root, {"id": again, "action": "test_session_stop"})
        await cog.tick(when)
        assert cog.book.state["test_session"] is None
        idle = new_request_id()
        write_command(cog.ctl_root, {"id": idle, "action": "test_session_stop"})
        await cog.tick(when)
        idle_result = _result(cog, idle)
        assert idle_result["ok"] is True
        assert "No test session is running." in idle_result["text"]

        cog.book.state["schedule_built"] = True
        sentinel = [{"id": "keep", "at": when.isoformat(), "day": "2026-11-21", "status": "pending"}]
        cog.book.state["schedule"] = sentinel
        cog.book.save()
        raw = cog.book.path.read_bytes()
        status_id = new_request_id()
        schedule_id = new_request_id()
        write_command(cog.ctl_root, {"id": status_id, "action": "status"})
        write_command(cog.ctl_root, {"id": schedule_id, "action": "schedule"})
        sent_before = len(support.sent)
        await cog.tick(when)
        status = _result(cog, status_id)
        preview = _result(cog, schedule_id)
        assert status["ok"] is True
        assert "live switch: off" in status["text"]
        assert preview["ok"] is True
        assert preview["text"].startswith("Unsaved preview.")
        assert "turkeys planned" in preview["text"]
        assert "keep" not in preview["text"]
        assert cog.book.state["schedule"] == sentinel
        assert cog.book.state["schedule_built"] is True
        assert cog.book.path.read_bytes() == raw
        assert len(support.sent) == sent_before
        assert general.sent == []

        unknown = "u" * 16
        _raw_inbox(cog, f"{unknown}.json", json.dumps({"id": unknown, "action": "power"}))
        outer = "a" * 16
        inner = "b" * 16
        _raw_inbox(cog, f"{outer}.json", json.dumps({"id": inner, "action": "status"}))
        huge = "c" * 16
        _raw_inbox(cog, f"{huge}.json", "x" * 9000)
        nested_id = "d" * 16
        nested = cog.ctl_root / "inbox" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / f"{nested_id}.json").write_text(
            json.dumps({"id": nested_id, "action": "status"}),
            encoding="utf-8",
        )
        escaped_id = "e" * 16
        (cog.ctl_root / "escaped.json").write_text(
            json.dumps({"id": escaped_id, "action": "status"}),
            encoding="utf-8",
        )
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        inbox = cog.ctl_root / "inbox"
        assert inbox_file_is_local(inbox, outside) is False
        assert inbox_file_is_local(inbox, inbox / ".." / outside.name) is False
        await cog.tick(when)
        assert _result(cog, unknown)["error"] == "unknown_action"
        assert _result(cog, outer)["error"] == "id_mismatch"
        assert not (cog.ctl_root / "results" / f"{inner}.json").exists()
        assert _result(cog, huge)["error"] == "too_large"
        assert (nested / f"{nested_id}.json").is_file()
        assert not (cog.ctl_root / "results" / f"{nested_id}.json").exists()
        assert not (cog.ctl_root / "results" / f"{escaped_id}.json").exists()
        assert len(support.sent) == sent_before
        assert cog.book.path.read_bytes() == raw

        ids = [new_request_id() for _ in range(MAX_COMMANDS_PER_TICK + 2)]
        for request_id in ids:
            write_command(cog.ctl_root, {"id": request_id, "action": "status"})
        await cog.tick(when)
        done = [request_id for request_id in ids if (cog.ctl_root / "results" / f"{request_id}.json").is_file()]
        left = [request_id for request_id in ids if (cog.ctl_root / "inbox" / f"{request_id}.json").is_file()]
        assert len(done) == MAX_COMMANDS_PER_TICK
        assert len(left) == 2
        assert len(support.sent) == sent_before
        await cog.tick(when)
        assert all((cog.ctl_root / "results" / f"{request_id}.json").is_file() for request_id in ids)
        assert list((cog.ctl_root / "inbox").glob("*.json")) == []

    asyncio.run(run())
