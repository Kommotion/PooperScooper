"""Pure logic and fakes for the Halloween candy hunt. No network."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from cogs.candyhunt import (
    CANDY_GUILD_IDS,
    AMBIENT_CHANCE,
    AMBIENT_EXTRAS,
    CANDY_HUNT,
    CANDY_TYPES,
    CARD_TIMEOUT,
    CLAIM_TEMPLATE,
    LATE_CLAIM_TEXT,
    MELTED_TEXT,
    MTS_GUILD_ID,
    NICK_ID,
    PT,
    SUPPORT_GUILD_ID,
    SUPPORT_TEST_CHANNEL_ID,
    TEST_DROP_EXPIRY,
    TREATS,
    TRICKS,
    CandyHunt,
    CandyStore,
    ClaimButton,
    ClaimOutcome,
    ChannelActivity,
    ChannelSnapshot,
    _candyadmin_allowed,
    _clock,
    apply_amount,
    apply_candy_hunt_overrides,
    apply_claim,
    as_utc,
    build_candy_card,
    build_claimed_drop_view,
    build_drop_view,
    build_leaderboard_view,
    build_winner_view,
    owner_decoration_dm,
    WINNER_DECORATION_LINE,
    broad_role_ids,
    channel_block_reason,
    component_text,
    compute_next_drop_at,
    default_state,
    effective_interval,
    find_announcement_channel,
    had_activity,
    has_players,
    is_eligible,
    is_in_drop_window,
    iso,
    late_night_factor,
    leaderboard_rows,
    jittered_delay_seconds,
    make_drop,
    make_test_session,
    members_can_chat,
    normalize_state,
    normalize_test_session,
    parse_autostart,
    read_autostart,
    resolve_test_session,
    session_drop_lifetime,
    parse_dt,
    pick_channel_id,
    plain_candy_text,
    claim_public_text,
    public_display_name,
    podium,
    pull_earlier,
    rank_for,
    real_drop_lifetime,
    render_status,
    resolve_schedule,
    role_removal_due,
    roll_candy,
    streak_factor,
    split_stale_drops,
    window_bounds,
    wrap_up_decision,
)
from cogs.utils.server_config import ServerConfig

NOW = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)
BEFORE = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)


class ScriptedRng:
    """random()/choice() pop scripted values. uniform() returns scripted seconds as-is."""

    def __init__(self, randoms=(), uniforms=(), choices=()):
        self.randoms = list(randoms)
        self.uniforms = list(uniforms)
        self.choices = list(choices)

    def random(self) -> float:
        if self.randoms:
            return self.randoms.pop(0)
        return 0.0

    def uniform(self, low: float, high: float) -> float:
        if self.uniforms:
            return self.uniforms.pop(0)
        return low

    def choice(self, seq):
        if self.choices:
            return self.choices.pop(0)
        return seq[0]


def _trick_weight_random(trick) -> float:
    """A scripted random() in the middle of this trick's weight bucket."""
    cursor = 0
    for item in TRICKS:
        if item == trick:
            return (cursor + item.weight / 2) / 100.0
        cursor += item.weight
    raise AssertionError(f"missing trick amount={trick.amount}")


class _HTTPResponse:
    status = 404
    reason = "Not Found"


def _http_error() -> discord.HTTPException:
    return discord.HTTPException(_HTTPResponse(), "missing")


class FakeCommandTree:
    def __init__(self):
        self.guild_syncs: list[int] = []
        self.global_syncs = 0
        self.copy_calls: list[int] = []
        self.fail_guilds: set[int] = set()
        self.synced_commands = [SimpleNamespace(name="candy")]

    async def sync(self, *, guild=None):
        if guild is None:
            self.global_syncs += 1
            return []
        self.guild_syncs.append(int(guild.id))
        if int(guild.id) in self.fail_guilds:
            raise _http_error()
        return list(self.synced_commands)

    def copy_global_to(self, *, guild):
        self.copy_calls.append(int(guild.id))
        raise AssertionError("copy_global_to")


class FakeBot:
    def __init__(self):
        self.dynamic_items: list = []
        self.guilds: dict = {}
        self.channels: dict = {}
        self.tree = FakeCommandTree()
        self.owner_ids: set[int] = set()
        self._before_invoke = None
        self._after_invoke = None

    def add_dynamic_items(self, *items):
        self.dynamic_items.extend(items)

    def remove_dynamic_items(self, *items):
        for item in items:
            if item in self.dynamic_items:
                self.dynamic_items.remove(item)

    async def wait_until_ready(self):
        return None

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(_HTTPResponse(), "missing")
        return channel

    def get_cog(self, name):
        return None

    async def is_owner(self, user):
        return getattr(user, "id", None) in self.owner_ids

    async def can_run(self, ctx, *, call_once=False):
        return True


class FakeTextChannel(discord.TextChannel):
    def __init__(
        self,
        channel_id: int,
        name: str = "lounge",
        *,
        error: BaseException | None = None,
        grants: dict | None = None,
    ):
        self.id = channel_id
        self.name = name
        self._type = 0
        self.nsfw = False
        self.guild = None
        self.sent: list[dict] = []
        self.error = error
        self.grants = grants

    def permissions_for(self, target):
        if self.grants is None:
            return SimpleNamespace(view_channel=True, send_messages=True)
        view, send = self.grants.get(getattr(target, "id", None), (False, False))
        return SimpleNamespace(view_channel=view, send_messages=send)

    async def send(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=self.id + 100)

    async def fetch_message(self, message_id):
        async def delete():
            return None

        return SimpleNamespace(id=message_id, delete=delete)

    def history(self, *, limit=None, after=None, before=None, around=None, oldest_first=None):
        messages = list(getattr(self, "history_messages", []) or [])

        def _stamp(message):
            return getattr(message, "created_at", None)

        if after is not None:
            messages = [
                message for message in messages if _stamp(message) is None or _stamp(message) > after
            ]
        if before is not None:
            messages = [
                message for message in messages if _stamp(message) is None or _stamp(message) < before
            ]

        async def _walk():
            for message in messages:
                yield message

        return _walk()


class FakeThread(discord.Thread):
    def __init__(self, parent):
        self._parent = parent

    @property
    def parent(self):
        return self._parent


def _cog(tmp_path, bot: FakeBot | None = None, rng=None, autostart_path=None) -> tuple[CandyHunt, FakeBot]:
    bot = bot or FakeBot()
    cog = CandyHunt(
        bot,
        path=tmp_path / "candy_hunt.json",
        settings=CANDY_HUNT,
        rng=rng or random.Random(0),
        autostart_path=tmp_path / "no-autostart.json" if autostart_path is None else autostart_path,
    )
    return cog, bot


def _interaction(user_id: int, *, guild_id: int | None = MTS_GUILD_ID, channel=None, client=None, bot: bool = False):
    user = SimpleNamespace(id=user_id, bot=bot, send=AsyncMock())
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        user=user,
        guild_id=guild_id,
        channel=channel,
        client=client or SimpleNamespace(is_owner=AsyncMock(return_value=False), get_cog=lambda name: None),
        message=None,
    )
    return interaction


def _sent(mock: AsyncMock):
    args, kwargs = mock.await_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def _ctx(
    cog: CandyHunt,
    user_id: int = NICK_ID,
    *,
    guild_id: int | None = MTS_GUILD_ID,
    channel=None,
):
    author = SimpleNamespace(id=user_id, bot=False)
    ctx = SimpleNamespace(
        author=author,
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        channel=channel,
        bot=cog.bot,
        cog=cog,
        reply=AsyncMock(),
        send=AsyncMock(),
        command=cog.candyadmin,
        prefix="!",
        invoked_with="candyadmin",
        invoked_parents=[],
        invoked_subcommand=None,
        subcommand_passed=None,
        args=[],
        kwargs={},
    )
    ctx.message = SimpleNamespace(
        attachments=[],
        author=author,
        guild=ctx.guild,
        channel=channel,
        id=1,
    )
    return ctx


def _replied(ctx):
    args, kwargs = ctx.reply.await_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def _assert_silent(ctx) -> None:
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()


def _bind_prefix_commands(cog: CandyHunt) -> None:
    for command in cog.walk_commands():
        command.cog = cog


def _active_drop(drop_id: str = "abcd1234", *, dry_run: bool = False, expires_at: datetime | None = None) -> dict:
    created = NOW - timedelta(minutes=1)
    expires = expires_at if expires_at is not None else NOW + timedelta(minutes=9)
    return make_drop(drop_id, 55, 66, created, expires, dry_run=dry_run)


async def _stop(cog: CandyHunt) -> None:
    if cog.candy_loop.is_running():
        cog.candy_loop.cancel()
        task = cog.candy_loop.get_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass


def test_default_window_runs_through_six_am_the_next_morning():
    start, end = window_bounds(CANDY_HUNT)
    assert CANDY_HUNT.window_end_minute == 30 * 60
    assert CANDY_HUNT.late_ramp_start_minute == 18 * 60
    assert CANDY_HUNT.late_ramp_full_minute == 21 * 60
    assert CANDY_HUNT.late_floor == timedelta(minutes=1)
    assert start.astimezone(PT) == datetime(2026, 10, 31, 10, 0, tzinfo=PT)
    assert end.astimezone(PT) == datetime(2026, 11, 1, 6, 0, tzinfo=PT)
    assert start.tzname() == "PDT"
    assert end.tzname() == "PST"
    # 10:00 PDT to 06:00 PST the next day is 21 absolute hours. The fallback
    # adds one hour that a wall-clock 06:00 still has to land on.
    assert as_utc(end) - as_utc(start) == timedelta(hours=21)
    assert CANDY_HUNT.wrap_up_at.astimezone(PT) == datetime(2026, 11, 1, 10, 0, tzinfo=PT)
    assert is_in_drop_window(CANDY_HUNT, start)
    assert not is_in_drop_window(CANDY_HUNT, start - timedelta(seconds=1))
    assert is_in_drop_window(CANDY_HUNT, end - timedelta(seconds=1))
    assert not is_in_drop_window(CANDY_HUNT, end)


def test_next_drop_stays_inside_the_window():
    start, end = window_bounds(CANDY_HUNT)
    minimum = CANDY_HUNT.min_interval
    maximum = CANDY_HUNT.max_interval
    for seed in range(25):
        rng = random.Random(seed)
        first = compute_next_drop_at(
            start,
            window_end=end,
            min_interval=minimum,
            max_interval=maximum,
            rng=rng,
            first=True,
        )
        assert first is not None
        assert as_utc(start) <= as_utc(first) < as_utc(end)
        following = compute_next_drop_at(
            first,
            window_end=end,
            min_interval=minimum,
            max_interval=maximum,
            rng=rng,
            first=False,
        )
        if following is None:
            assert as_utc(end) - as_utc(first) < minimum
            continue
        gap = as_utc(following) - as_utc(first)
        assert minimum <= gap <= maximum
        assert as_utc(following) < as_utc(end)

    opened = compute_next_drop_at(
        start,
        window_end=end,
        min_interval=minimum,
        max_interval=maximum,
        rng=ScriptedRng(uniforms=[0.0]),
        first=True,
    )
    assert as_utc(opened) == as_utc(start)
    followed = compute_next_drop_at(
        start,
        window_end=end,
        min_interval=minimum,
        max_interval=maximum,
        rng=ScriptedRng(uniforms=[minimum.total_seconds()]),
        first=False,
    )
    assert as_utc(followed) == as_utc(start) + minimum
    late = compute_next_drop_at(
        end - timedelta(minutes=10),
        window_end=end,
        min_interval=minimum,
        max_interval=maximum,
        rng=random.Random(0),
        first=False,
    )
    assert late is None


def test_resolve_schedule_does_not_burst():
    start, end = window_bounds(CANDY_HUNT)
    minimum = CANDY_HUNT.min_interval
    maximum = CANDY_HUNT.max_interval

    def decide(now, persisted, rng=None):
        return resolve_schedule(
            now=now,
            persisted=persisted,
            window_start=start,
            window_end=end,
            min_interval=minimum,
            max_interval=maximum,
            rng=rng or ScriptedRng(uniforms=[minimum.total_seconds()]),
        )

    saved = start + timedelta(hours=2)
    waiting = decide(start - timedelta(hours=5), saved)
    assert waiting.action == "wait"
    assert as_utc(waiting.next_drop_at) == as_utc(saved)

    due = decide(start + timedelta(hours=3), start + timedelta(hours=1))
    assert due.action == "drop"
    assert as_utc(due.next_drop_at) == as_utc(start + timedelta(hours=3)) + minimum

    before = decide(BEFORE, None, ScriptedRng(uniforms=[0.0]))
    assert before.action == "wait"
    assert as_utc(before.next_drop_at) == as_utc(start)

    inside = decide(start + timedelta(hours=1), None)
    assert inside.action == "wait"
    assert as_utc(inside.next_drop_at) == as_utc(start + timedelta(hours=1)) + minimum

    outside = decide(start + timedelta(hours=2), start - timedelta(hours=3))
    assert outside.action == "wait"
    assert as_utc(outside.next_drop_at) == as_utc(start + timedelta(hours=2)) + minimum

    assert decide(end, start + timedelta(hours=1)).action == "idle"
    assert decide(end + timedelta(minutes=1), None).next_drop_at is None


def test_channel_eligibility_skips_quiet_and_staff_rooms():
    def snap(**kwargs) -> ChannelSnapshot:
        base = dict(channel_id=1, name="lounge")
        base.update(kwargs)
        return ChannelSnapshot(**base)

    assert is_eligible(snap())
    assert channel_block_reason(snap(is_text=False)) == "not_text"
    assert channel_block_reason(snap(is_news=True)) == "news"
    assert channel_block_reason(snap(is_nsfw=True)) == "nsfw"
    assert channel_block_reason(snap(is_rules_channel=True)) == "rules"
    assert channel_block_reason(snap(name="server-rules")) == "rules"
    assert channel_block_reason(snap(name="Announcements")) == "announcements"
    assert channel_block_reason(snap(is_announce_channel=True, name="general")) == "announcements"
    assert channel_block_reason(snap(skipped=True)) == "skip_list"
    assert channel_block_reason(snap(everyone_can_send=False)) == "read_only"
    assert channel_block_reason(snap(everyone_can_view=False, everyone_can_send=True)) == "read_only"
    gated = snap(everyone_can_view=False, everyone_can_send=False, broad_role_can_chat=True)
    assert members_can_chat(gated)
    assert is_eligible(gated)
    assert is_eligible(snap(everyone_can_send=False, broad_role_can_chat=True))
    assert channel_block_reason(snap(bot_can_view=False)) == "no_view"
    assert channel_block_reason(snap(bot_can_send=False)) == "no_send"
    assert not is_eligible(snap(name="rules-and-lore"))


def test_broad_role_ids_require_half_the_humans():
    # Member is exactly half. Staff is one person. Boost is managed. @everyone is not a role.
    holders = {1: 4, 2: 2, 3: 1, 4: 4}
    assert broad_role_ids(holders, 4, managed_role_ids={4}, everyone_role_id=1) == frozenset({2})
    assert broad_role_ids({2: 1}, 2) == frozenset({2})
    assert broad_role_ids({3: 1}, 3) == frozenset()
    assert broad_role_ids({2: 2}, 3) == frozenset({2})
    assert broad_role_ids({2: 4}, 0) == frozenset()
    assert broad_role_ids({}, 5) == frozenset()
    assert broad_role_ids({4: 4}, 4, managed_role_ids={4}) == frozenset()
    assert broad_role_ids({1: 4}, 4, everyone_role_id=1) == frozenset()
    assert broad_role_ids({3: 1}, 4, min_share=0.25) == frozenset({3})


class _CountingList(list):
    def __init__(self, items):
        super().__init__(items)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_choose_drop_channel_accepts_member_gates(tmp_path, caplog):
    # 2 of 4 humans hold Member (exactly half). One of them lists Staff twice,
    # which still counts as one person. Five bots must not dilute that share.
    # Boost is managed, so it cannot open a channel by itself.
    everyone = SimpleNamespace(id=1, managed=False)
    member = SimpleNamespace(id=2, managed=False)
    staff = SimpleNamespace(id=3, managed=False)
    boost = SimpleNamespace(id=4, managed=True)
    bot_id = 9000

    def person(member_id, roles, *, bot=False):
        return SimpleNamespace(id=member_id, bot=bot, roles=list(roles))

    members = _CountingList(
        [
            person(101, [member, staff, staff, boost]),
            person(102, [member, boost]),
            person(103, [boost]),
            person(104, [boost]),
            *[person(200 + i, [boost], bot=True) for i in range(5)],
        ]
    )

    def room(channel_id, name, mapping):
        return FakeTextChannel(channel_id, name, grants={bot_id: (True, True), **mapping})

    rules = room(9, "rules", {1: (True, True), 2: (True, True)})
    mods = room(10, "mods", {1: (False, False), 2: (False, False), 3: (True, True)})
    boosters = room(11, "boosters", {1: (False, False), 4: (True, True)})
    whisper = room(12, "whisper", {1: (False, False), 2: (False, True)})
    bulletin = room(13, "bulletin", {1: (False, False), 2: (True, False)})
    general = room(14, "general", {1: (False, False), 2: (True, True), 3: (True, True)})
    lounge = room(15, "lounge", {1: (True, True)})
    guild = SimpleNamespace(
        id=MTS_GUILD_ID,
        text_channels=[rules, mods, boosters, whisper, bulletin, general, lounge],
        me=SimpleNamespace(id=bot_id),
        default_role=everyone,
        rules_channel=None,
        members=members,
    )
    bot = FakeBot()
    bot.guilds[MTS_GUILD_ID] = guild
    cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(randoms=[0.0]))

    def eligible_logs():
        return [record for record in caplog.records if record.message.startswith("candy channels eligible=")]

    with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
        chosen = cog.choose_drop_channel(NOW)
        assert chosen is general
        assert members.iterations == 1
        logs = eligible_logs()
        assert len(logs) == 1
        assert logs[0].levelno == logging.INFO
        assert logs[0].message == "candy channels eligible=2 total=7"

        caplog.clear()
        guild.text_channels = [channel for channel in guild.text_channels if channel is not general]
        assert cog.choose_drop_channel(NOW) is lounge
        assert members.iterations == 2
        assert [record.message for record in eligible_logs()] == ["candy channels eligible=1 total=6"]

        caplog.clear()
        guild.text_channels = [channel for channel in guild.text_channels if channel is not lounge]
        assert cog.choose_drop_channel(NOW) is None
        assert members.iterations == 3
        assert [record.message for record in eligible_logs()] == ["candy channels eligible=0 total=5"]


def test_weighted_channel_pick_prefers_recent_rooms():
    window = timedelta(hours=2)
    fresh = ChannelActivity(1, NOW)
    edge = ChannelActivity(2, NOW - window)
    quiet_a = ChannelActivity(5, None)
    quiet_b = ChannelActivity(3, None)

    assert pick_channel_id([quiet_a, fresh], NOW, ScriptedRng(randoms=[0.99]), activity_window=window) == 1
    # Sorted by id: weight 2 then weight 1. 0.0 lands on the first, 0.9 on the second.
    pair = [edge, fresh]
    assert pick_channel_id(pair, NOW, ScriptedRng(randoms=[0.0]), activity_window=window) == 1
    assert pick_channel_id(pair, NOW, ScriptedRng(randoms=[0.9]), activity_window=window) == 2
    assert pick_channel_id([quiet_a, quiet_b], NOW, ScriptedRng(randoms=[0.0]), activity_window=window) == 3
    assert pick_channel_id([], NOW, ScriptedRng(), activity_window=window) is None


def test_find_announcement_channel_prefers_configured_id():
    rooms = [(1, "general"), (2, "announcements"), (3, "staff-announcements")]
    assert find_announcement_channel(rooms, channel_id=3, name="announcements") == 3
    assert find_announcement_channel(rooms, channel_id=99, name="announcements") == 2
    assert find_announcement_channel([(4, "weekly-announcements")], channel_id=None, name="announcements") == 4
    assert find_announcement_channel([(4, "general")], channel_id=None, name="announcements") is None


def test_candy_roll_and_floor_at_zero():
    assert CANDY_HUNT.trick_chance == 0.20
    trick = roll_candy(ScriptedRng(randoms=[0.0, 0.0]), trick_chance=0.125)
    assert trick.kind == "trick"
    assert trick.blurb == TRICKS[0].blurb
    assert trick.amount == TRICKS[0].amount

    treat = roll_candy(ScriptedRng(randoms=[0.125, 0.0]), trick_chance=0.125)
    assert treat.kind == "treat"
    assert treat.name == TREATS[0].name

    forced = roll_candy(ScriptedRng(randoms=[0.0, 0.0]), trick_chance=0)
    assert forced.name == "Candy Corn"
    assert forced.amount == 1

    jackpot = roll_candy(ScriptedRng(randoms=[0.5, 0.999]), trick_chance=0)
    assert jackpot.name == "Pumpkin Jackpot"
    assert jackpot.amount == 25

    assert apply_amount(0, -2) == 0
    assert apply_amount(1, -2) == 0
    assert apply_amount(5, -1) == 4
    assert apply_amount(10, -25) == 0
    assert apply_amount(25, -25) == 0
    assert apply_amount(26, -25) == 1

    raisins = next(item for item in TRICKS if item.amount == -2)
    state = {
        "totals": {},
        "claim_counts": {},
        "claims_total": 0,
        "active_drops": [_active_drop()],
    }
    updated, outcome = apply_claim(
        state,
        "abcd1234",
        7,
        NOW,
        ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)]),
        is_bot=False,
        trick_chance=1,
    )
    assert outcome.status == "won"
    assert outcome.roll is not None and outcome.roll.amount == -2
    assert outcome.total == 0
    assert updated["totals"]["7"] == 0


def test_trick_weights_sum_to_100_and_the_worst_loss_is_25():
    assert sum(trick.weight for trick in TRICKS) == 100
    assert all(isinstance(trick.weight, int) and trick.weight > 0 for trick in TRICKS)
    assert all(2 <= len(trick.blurbs) <= 3 for trick in TRICKS)
    assert max(-trick.amount for trick in TRICKS) == 25
    assert min(trick.amount for trick in TRICKS) == -25
    curse = next(trick for trick in TRICKS if trick.amount == -25)
    assert curse.weight == 4
    assert "Witch Curse" in curse.blurb


def test_weighted_trick_selection_follows_the_scripted_roll():
    cursor = 0
    tricks = list(TRICKS)
    for index, trick in enumerate(tricks):
        for point in (cursor + 0.25, cursor + trick.weight / 2, cursor + trick.weight - 0.25):
            roll = roll_candy(ScriptedRng(randoms=[0.0, point / 100.0]), trick_chance=1)
            assert roll.kind == "trick"
            assert roll.amount == trick.amount
            assert roll.blurb == trick.blurb
        if index + 1 < len(tricks):
            nxt = tricks[index + 1]
            below = roll_candy(
                ScriptedRng(randoms=[0.0, (cursor + trick.weight - 0.25) / 100.0]),
                trick_chance=1,
            )
            above = roll_candy(
                ScriptedRng(randoms=[0.0, (cursor + trick.weight + 0.25) / 100.0]),
                trick_chance=1,
            )
            assert below.blurb == trick.blurb and below.amount == trick.amount
            assert above.blurb == nxt.blurb and above.amount == nxt.amount
        cursor += trick.weight
    assert cursor == 100
    last = roll_candy(ScriptedRng(randoms=[0.0, 0.999]), trick_chance=1)
    assert last.amount == -25
    assert last.blurb == tricks[-1].blurb


def test_curse_on_a_small_total_floors_at_zero_and_reports_removed():
    curse = next(trick for trick in TRICKS if trick.amount == -25)
    state = _claim_state(7, 9)
    state["candy_types"] = {"7": {"candy_corn": 4, "lollipop": 5}}
    updated, outcome = apply_claim(
        state,
        "abcd1234",
        7,
        NOW,
        ScriptedRng(randoms=[0.0, _trick_weight_random(curse)]),
        is_bot=False,
        trick_chance=1,
    )
    assert outcome.status == "won"
    assert outcome.roll is not None and outcome.roll.kind == "trick"
    assert outcome.roll.amount == -25
    assert outcome.total == 0
    assert outcome.removed == 9
    assert updated["totals"]["7"] == 0
    assert updated["candy_types"] == {"7": {"candy_corn": 4, "lollipop": 5}}
    assert state["totals"]["7"] == 9
    assert state["candy_types"]["7"]["candy_corn"] == 4
    assert claim_public_text(outcome, "Ada") == "🎃 TRICK! Ada lost 9 candy"


def test_ranks_use_competition_order():
    totals = {"1": 10, "2": 10, "3": 4, "9": 0}
    assert rank_for(totals, 1) == (1, 10, 4)
    assert rank_for(totals, 2) == (1, 10, 4)
    assert rank_for(totals, 3) == (3, 4, 4)
    assert rank_for(totals, 9) == (4, 0, 4)
    assert rank_for(totals, 8) == (4, 0, 4)
    assert leaderboard_rows(totals) == [(1, "1", 10), (1, "2", 10), (3, "3", 4)]
    assert podium(totals) == [("1", 10), ("2", 10), ("3", 4)]
    assert "haven't found any" in component_text(build_leaderboard_view(totals, 8))


def test_activity_flags():
    state = default_state()
    assert had_activity(state) is False
    assert has_players(state) is False
    state["drops_posted"] = 1
    assert had_activity(state) is True
    assert has_players(state) is False
    state["totals"]["4"] = 2
    assert has_players(state) is True


def test_apply_claim_awards_once_and_keeps_namespaces_apart():
    real = _active_drop("aaaa1111")
    state = {
        "totals": {"3": 4},
        "claim_counts": {},
        "claims_total": 0,
        "active_drops": [real, _active_drop("bbbb2222", dry_run=True)],
    }
    won_state, won = apply_claim(
        state,
        "aaaa1111",
        3,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0]),
        is_bot=False,
        trick_chance=0,
    )
    assert won.status == "won" and won.mutated
    assert won_state["totals"]["3"] == 5
    assert won_state["claim_counts"]["3"] == 1
    assert won_state["claims_total"] == 1
    assert won.rank == 1
    assert state["totals"] == {"3": 4}

    again_state, again = apply_claim(
        won_state,
        "aaaa1111",
        8,
        NOW,
        ScriptedRng(),
        is_bot=False,
        trick_chance=0,
    )
    assert again.status == "already" and not again.mutated
    assert again_state["totals"] == won_state["totals"]
    assert again_state["claims_total"] == 1

    partial = {"active_drops": [_active_drop("bbbb2222", dry_run=True)], "totals": {"3": 4}, "claims_total": 2}
    dry_state, dry = apply_claim(
        partial,
        "bbbb2222",
        9,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0]),
        is_bot=False,
        trick_chance=0,
    )
    assert dry.status == "won" and dry.dry_run
    assert dry_state["totals"] == {"3": 4}
    assert dry_state["claims_total"] == 2
    assert dry_state["test"]["totals"]["9"] == 1
    assert dry_state["test"]["claims_total"] == 1
    assert partial.get("test") is None

    untouched, bot = apply_claim(dry_state, "bbbb2222", 1, NOW, ScriptedRng(), is_bot=True, trick_chance=0)
    assert bot.status == "bot" and not bot.mutated
    assert untouched is dry_state

    expired_drop = _active_drop("cccc3333", expires_at=NOW)
    expired_state = {"totals": {}, "claim_counts": {}, "claims_total": 0, "active_drops": [expired_drop]}
    marked, expired = apply_claim(
        expired_state,
        "cccc3333",
        1,
        NOW,
        ScriptedRng(),
        is_bot=False,
        trick_chance=0,
    )
    assert expired.status == "expired" and expired.mutated
    assert marked["active_drops"][0]["status"] == "expired"
    assert marked["claims_total"] == 0
    missing, gone = apply_claim(marked, "dddd4444", 1, NOW, ScriptedRng(), is_bot=False, trick_chance=0)
    assert gone.status == "missing"
    assert missing is marked


def test_concurrent_claims_award_exactly_once(tmp_path):
    async def race():
        store = CandyStore(tmp_path / "candy_hunt.json")
        store.state["active_drops"] = [_active_drop("abcd1234")]
        store.save()
        rng = random.Random(1)
        results = await asyncio.gather(
            store.claim("abcd1234", 10, NOW, rng, is_bot=False, trick_chance=0),
            store.claim("abcd1234", 11, NOW, rng, is_bot=False, trick_chance=0),
            store.claim("abcd1234", 12, NOW, rng, is_bot=False, trick_chance=0),
        )
        statuses = [item.status for item in results]
        assert statuses.count("won") == 1
        assert statuses.count("already") == 2
        assert store.state["claims_total"] == 1
        assert len(store.state["totals"]) == 1
        reloaded = CandyStore(tmp_path / "candy_hunt.json")
        assert reloaded.state["claims_total"] == 1
        assert reloaded.state["totals"] == store.state["totals"]

    asyncio.run(race())


def test_split_stale_drops_and_role_removal():
    future = _active_drop("aaaa1111", expires_at=NOW + timedelta(minutes=5))
    due = _active_drop("bbbb2222", expires_at=NOW)
    claimed = _active_drop("cccc3333")
    claimed["status"] = "claimed"
    keep, expired, finished = split_stale_drops([future, due, claimed], NOW)
    assert [item["drop_id"] for item in keep] == ["aaaa1111"]
    assert expired[0]["drop_id"] == "bbbb2222" and expired[0]["status"] == "expired"
    assert finished[0]["drop_id"] == "cccc3333"
    assert future["status"] == "active"

    assert role_removal_due(None, NOW) is False
    assert role_removal_due({"remove_at": iso(NOW - timedelta(seconds=1))}, NOW) is True
    assert role_removal_due({"remove_at": iso(NOW)}, NOW) is True
    assert role_removal_due({"remove_at": iso(NOW + timedelta(minutes=1))}, NOW) is False


def test_wrap_up_decision_matrix():
    due = CANDY_HUNT.wrap_up_at
    limit = CANDY_HUNT.wrap_up_late_limit

    def decide(**overrides):
        params = dict(
            now=due,
            wrap_up_at=due,
            done=False,
            had_activity=True,
            catching_up=False,
            late_limit=limit,
            has_players=True,
        )
        params.update(overrides)
        return wrap_up_decision(**params)

    assert decide(now=due - timedelta(seconds=1)) == "waiting"
    assert decide(done=True) == "done"
    assert decide(now=due + limit + timedelta(seconds=1)) == "too_late"
    assert decide(now=due + limit) == "announce"
    assert decide(has_players=False, had_activity=False) == "nobody"
    assert decide(catching_up=True, had_activity=False, has_players=False) == "skip_quiet"
    assert decide(catching_up=True, had_activity=True, has_players=False) == "nobody"
    assert decide(catching_up=True, now=due + timedelta(days=1)) == "announce"


def test_config_overrides_and_json_roundtrip(tmp_path):
    blank = ServerConfig(1, {})
    assert blank.candy_hunt == {}
    assert ServerConfig(1, {"candy_hunt": "nope"}).candy_hunt == {}
    section = {"trick_chance": 0.0, "announcement_channel_id": 12}
    assert ServerConfig(MTS_GUILD_ID, {"candy_hunt": section}).candy_hunt == section

    updated = apply_candy_hunt_overrides(
        CANDY_HUNT,
        {
            "announcement_channel_id": 12,
            "announcement_channel_name": "bulletin",
            "skip_channel_ids": [9],
            "min_interval_minutes": 20,
            "max_interval_minutes": 40,
            "drop_expiry_minutes": 5,
            "event_date": "2026-10-30",
            "window_start": "09:30",
            "window_end": "23:00",
            "wrap_up_at": "2026-11-01T09:00:00",
            "pumpkin_god_days": 3,
            "trick_chance": 0.5,
            "activity_window_hours": 1,
            "pumpkin_god_role_name": "Gourd",
            "ignored": True,
        },
    )
    assert updated.announcement_channel_id == 12
    assert updated.min_interval == timedelta(minutes=20)
    assert updated.drop_expiry == timedelta(minutes=5)
    assert updated.event_date.isoformat() == "2026-10-30"
    assert updated.window_start_minute == 9 * 60 + 30
    assert updated.pumpkin_god_duration == timedelta(days=3)
    assert updated.wrap_up_at.tzinfo is not None
    assert updated.wrap_up_at.astimezone(PT).hour == 9
    assert apply_candy_hunt_overrides(CANDY_HUNT, {"trick_chance": "nope"}) is CANDY_HUNT
    assert (
        apply_candy_hunt_overrides(CANDY_HUNT, {"window_start": "18:00", "window_end": "10:00"}).window_start_minute
        == CANDY_HUNT.window_start_minute
    )

    raw = {
        "event_disabled": "false",
        "wrap_up_done": "true",
        "totals": {1: 3, "nope": "x"},
        "next_drop_at": iso(NOW),
    }
    cleaned = normalize_state(raw)
    assert cleaned["event_disabled"] is False
    assert cleaned["wrap_up_done"] is False
    assert cleaned["totals"] == {"1": 3}
    assert cleaned["test"]["drops_posted"] == 0

    store = CandyStore(tmp_path / "candy_hunt.json")
    store.state["totals"]["42"] = 7
    store.state["next_drop_at"] = iso(NOW)
    store.state["test"]["drops_posted"] = 2
    store.save()
    loaded = CandyStore(tmp_path / "candy_hunt.json")
    assert loaded.state["totals"] == {"42": 7}
    assert loaded.state["next_drop_at"] == iso(NOW)
    assert loaded.state["test"]["drops_posted"] == 2
    assert len(render_status(loaded.state, CANDY_HUNT, NOW)) < 2000


def test_components_v2_cards_and_persistent_claim_button():
    pattern = ClaimButton.__discord_ui_compiled_template__
    assert pattern.fullmatch("candy:claim:abcd1234")
    assert pattern.pattern == CLAIM_TEMPLATE
    assert pattern.fullmatch("candy:claim:ABCD1234") is None
    assert ClaimButton("abcd1234").template.fullmatch("candy:claim:abcd1234")

    drop = build_drop_view("abcd1234", expires_at=NOW + timedelta(minutes=10), dry_run=True)
    card = build_candy_card(
        ClaimOutcome(
            status="won",
            mutated=True,
            roll=roll_candy(ScriptedRng(randoms=[0.9, 0.0]), trick_chance=0),
            total=3,
            rank=1,
            players=1,
            dry_run=True,
            user_id=5,
        )
    )
    board = build_leaderboard_view({"5": 3}, 5)
    winners = build_winner_view(top=[("5", 3), ("6", 1)], drops_posted=4, claims_total=2, pumpkin_days=7)
    preview = build_winner_view(top=[], drops_posted=0, claims_total=0, pumpkin_days=7, preview=True)
    for view in (drop, card, board, winners, preview):
        assert isinstance(view, discord.ui.LayoutView)
        payload = view.to_components()
        assert payload and payload[0]["type"] == 17
        kinds = {type(item) for item in view.walk_children()}
        assert discord.ui.Container in kinds
        assert discord.ui.Section in kinds
        assert discord.ui.TextDisplay in kinds
        assert discord.ui.Separator in kinds
        assert discord.ui.Thumbnail in kinds

    drop_kinds = {type(item) for item in drop.walk_children()}
    assert discord.ui.ActionRow in drop_kinds
    assert ClaimButton in drop_kinds
    assert drop.is_persistent()
    ids = [getattr(item, "custom_id", None) for item in drop.walk_children()]
    assert "candy:claim:abcd1234" in ids
    assert "TEST" in component_text(drop)

    for view in (card, board, winners, preview):
        assert view.timeout == CARD_TIMEOUT
        assert not view.is_persistent()
        assert discord.ui.MediaGallery in {type(item) for item in view.walk_children()} or view is board

    assert discord.ui.MediaGallery in {type(item) for item in card.walk_children()}
    assert "TEST" in component_text(card)
    assert "nobody found any candy" in component_text(preview).casefold()
    assert "PREVIEW" in component_text(preview)
    assert "🥇" in component_text(winners)
    fallback = plain_candy_text(
        ClaimOutcome(
            status="won",
            mutated=True,
            roll=roll_candy(ScriptedRng(randoms=[0.9, 0.0]), trick_chance=0),
            total=3,
            rank=1,
            players=1,
            dry_run=True,
        )
    )
    assert fallback.startswith("TEST.")


def test_commands_are_registered_for_mts():
    import PooperScooper

    assert "cogs.candyhunt" in PooperScooper.initial_extensions
    app_commands = list(CandyHunt.__cog_app_commands__)
    assert [command.name for command in app_commands] == ["candy"]
    assert not any(isinstance(command, discord.app_commands.Group) for command in app_commands)
    assert not any(command.name == "candyadmin" for command in app_commands)
    assert CandyHunt.candy.guild_only is True
    assert list(CandyHunt.candy._guild_ids) == list(CANDY_GUILD_IDS)

    group = CandyHunt.candyadmin
    assert isinstance(group, commands.Group)
    assert not isinstance(group, discord.app_commands.Group)
    assert group.name == "candyadmin"
    assert group.hidden is True
    assert group.invoke_without_command is True
    names = {command.name for command in group.commands}
    assert names == {"status", "power", "test_session", "test_session_stop", "wrapup", "cleanup"}
    assert all(command.hidden for command in group.commands)
    assert list(group.clean_params) == []
    assert list(CandyHunt.admin_status.clean_params) == []
    assert list(CandyHunt.admin_power.clean_params) == ["state"]
    assert list(CandyHunt.admin_test_session.clean_params) == ["minutes", "interval", "channel"]
    assert list(CandyHunt.admin_test_session_stop.clean_params) == []
    assert list(CandyHunt.admin_wrapup.clean_params) == ["dry_run"]
    predicate = group.checks[0]
    assert predicate is _candyadmin_allowed
    assert all(command.checks[0] is predicate for command in group.commands)


def test_owner_gate_is_silent_for_everyone_else(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        _bind_prefix_commands(cog)
        owner_mock = AsyncMock(return_value=False)
        cog.bot.is_owner = owner_mock

        nick = _ctx(cog, NICK_ID, guild_id=SUPPORT_GUILD_ID)
        assert await _candyadmin_allowed(nick) is True
        assert await cog.candyadmin.can_run(nick) is True
        owner_mock.assert_not_awaited()

        cog.bot.is_owner = AsyncMock(return_value=True)
        bot_owner = _ctx(cog, 9, guild_id=MTS_GUILD_ID)
        assert await cog.admin_status.can_run(bot_owner) is True

        cog.bot.is_owner = AsyncMock(return_value=False)
        stranger = _ctx(cog, 5, guild_id=SUPPORT_GUILD_ID)
        assert await cog.admin_power.can_run(stranger) is False
        await cog.admin_status.callback(cog, stranger)
        await cog.admin_power.callback(cog, stranger, "on")
        await cog.candyadmin.callback(cog, stranger)
        await cog.cog_command_error(stranger, commands.CheckFailure("no"))
        _assert_silent(stranger)

        elsewhere = _ctx(cog, NICK_ID, guild_id=1)
        assert await cog.candyadmin.can_run(elsewhere) is False
        await cog.admin_status.callback(cog, elsewhere)
        _assert_silent(elsewhere)

        dm = _ctx(cog, NICK_ID, guild_id=None)
        assert await cog.admin_status.can_run(dm) is True

    asyncio.run(run())


def test_candyadmin_usage_parsing_dms_and_invoke(tmp_path):
    async def run():
        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        bot = FakeBot()
        bot.channels[channel.id] = channel
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(uniforms=[120.0, 120.0]))
        _bind_prefix_commands(cog)

        bare = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        bare.view = StringView("")
        await cog.candyadmin.invoke(bare)
        text, kwargs = _replied(bare)
        assert kwargs["mention_author"] is False
        for piece in ("!candyadmin status", "power on|off", "test_session", "test_session_stop", "wrapup"):
            assert piece in text
        assert "!candyadmin wrapup [true|false]  (false posts the real Nov 1 announcement, MTS only)" in text

        unknown = _ctx(cog, guild_id=MTS_GUILD_ID)
        unknown.view = StringView("nope")
        await cog.candyadmin.invoke(unknown)
        text, _kwargs = _replied(unknown)
        assert "Usage:" in text
        assert "nope" not in text

        cog.bot.is_owner = AsyncMock(return_value=False)
        stranger = _ctx(cog, 5, guild_id=SUPPORT_GUILD_ID)
        stranger.view = StringView("power on")
        try:
            await cog.candyadmin.invoke(stranger)
        except commands.CheckFailure as exc:
            await cog.cog_command_error(stranger, exc)
        else:
            raise AssertionError("non-owner invoke should fail the owner check")
        _assert_silent(stranger)
        assert cog.store.state["event_disabled"] is False

        async def _is_owner(user):
            return getattr(user, "id", None) in cog.bot.owner_ids

        cog.bot.is_owner = _is_owner
        cog.store.state["event_disabled"] = True
        power = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        power.view = StringView("power ON")
        await cog.candyadmin.invoke(power)
        text, power_kwargs = _replied(power)
        assert text == "Candy hunt enabled."
        assert power_kwargs["mention_author"] is False
        assert cog.store.state["event_disabled"] is False

        off = _ctx(cog, guild_id=MTS_GUILD_ID)
        off.view = StringView("power off")
        await cog.candyadmin.invoke(off)
        text, _kwargs = _replied(off)
        assert text == "Candy hunt disabled. Scheduled drops and the automatic wrap-up are paused."
        assert cog.store.state["event_disabled"] is True

        usage_ctx = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        usage_ctx.command = cog.admin_test_session
        await cog.cog_command_error(usage_ctx, commands.BadArgument("nope"))
        usage, usage_kwargs = _replied(usage_ctx)
        assert "!candyadmin status" in usage
        assert usage_kwargs["mention_author"] is False

        wrapup_usage = (
            "Usage: !candyadmin wrapup [true|false]  "
            "(false posts the real Nov 1 announcement, MTS only)"
        )
        bad_wrap = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_wrapup.callback(cog, bad_wrap, "maybe")
        text, _kwargs = _replied(bad_wrap)
        assert text == wrapup_usage

        for token in ("true", "TRUE", "dry_run=true", "preview", "Preview", None):
            preview = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
            await cog.admin_wrapup.callback(cog, preview, token)
            view = preview.reply.await_args.kwargs["view"]
            assert "content" not in preview.reply.await_args.kwargs
            assert "PREVIEW" in component_text(view)
            assert cog.store.state["wrap_up_done"] is False

        for token in ("false", "FALSE", "dry_run=false", "Dry_Run=False"):
            refused = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
            await cog.admin_wrapup.callback(cog, refused, token)
            text, _kwargs = _replied(refused)
            assert "will not post" in text
            assert cog.store.state["wrap_up_done"] is False

        for token in ("no", "off", "0", "on", "yes", "1"):
            ignored = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
            await cog.admin_wrapup.callback(cog, ignored, token)
            text, _kwargs = _replied(ignored)
            assert text == wrapup_usage
            assert "view" not in ignored.reply.await_args.kwargs
            assert cog.store.state["wrap_up_done"] is False

        dm = _ctx(cog, guild_id=None)
        await cog.admin_test_session.callback(cog, dm, "15", "2")
        text, _kwargs = _replied(dm)
        assert "server channel" in text.casefold()
        assert cog.store.state["test"]["session"] is None

        ranged = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        await cog.admin_test_session.callback(cog, ranged, "241", "2")
        text, _kwargs = _replied(ranged)
        assert "minutes 1-240" in text
        wide = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        await cog.admin_test_session.callback(cog, wide, "15", "61")
        text, _kwargs = _replied(wide)
        assert "interval 1-60" in text
        assert cog.store.state["test"]["session"] is None

        started = _ctx(cog, guild_id=None)
        await cog.admin_test_session.callback(cog, started, "10", "3", f"<#{channel.id}>")
        text, kwargs = _replied(started)
        assert kwargs["mention_author"] is False
        assert "previous test tally was cleared" in text.casefold()
        session = cog.store.state["test"]["session"]
        assert session["minutes"] == 10
        assert session["interval"] == 3
        assert session["guild_id"] == SUPPORT_GUILD_ID
        assert session["channel_id"] == channel.id
        assert session["source"] == "command"

    asyncio.run(run())


def test_candy_command_is_mts_only_and_does_not_ping(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        cog.store.state["totals"] = {"8": 6, "2": 4}
        other = _interaction(2, guild_id=999)
        await cog.candy.callback(cog, other)
        message, kwargs = _sent(other.response.send_message)
        assert message == "The candy hunt is only running in Menaces to Sobriety."
        assert kwargs["ephemeral"] is True

        home = _interaction(3, guild_id=MTS_GUILD_ID)
        await cog.candy.callback(cog, home)
        args, kwargs = home.response.send_message.await_args
        assert args == ()
        assert "content" not in kwargs
        assert kwargs["allowed_mentions"].users is False
        assert kwargs["allowed_mentions"].everyone is False
        text = component_text(kwargs["view"])
        assert "<@8>" in text
        assert "haven't found any" in text

    asyncio.run(run())


def test_post_drop_send_failure_rolls_the_count_back(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        broken = FakeTextChannel(88, error=_http_error())
        with pytest.raises(discord.HTTPException):
            await cog.post_drop(broken, dry_run=False)
        assert cog.store.state["drops_posted"] == 0
        assert cog.store.state["active_drops"] == []

        with pytest.raises(discord.HTTPException):
            await cog.post_drop(broken, dry_run=True)
        assert cog.store.state["drops_posted"] == 0
        assert cog.store.state["test"]["drops_posted"] == 0
        assert cog.store.state["active_drops"] == []

    asyncio.run(run())


def test_schedule_posts_one_catchup_drop(tmp_path, caplog):
    start, _end = window_bounds(CANDY_HUNT)
    now = start + timedelta(hours=3)

    async def run():
        channel = FakeTextChannel(42, "spooky")
        news = FakeTextChannel(43, "headlines")
        news._type = 5
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[news],
            me=SimpleNamespace(id=1),
            default_role=SimpleNamespace(id=0),
            rules_channel=None,
        )
        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = guild
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(uniforms=[CANDY_HUNT.min_interval.total_seconds()]))
        cog.store.state["next_drop_at"] = iso(start + timedelta(hours=1))
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_drop(now)
        assert channel.sent == []
        assert cog.store.state["drops_posted"] == 0
        assert any("candy channels eligible=0 total=1" in record.message for record in caplog.records)
        assert any("candy drop skipped reason=no_channel" in record.message for record in caplog.records)
        advanced = cog.store.state["next_drop_at"]

        guild.text_channels = [channel]
        cog.store.state["next_drop_at"] = iso(start + timedelta(hours=1))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_drop(now)
        assert any("candy channels eligible=1 total=1" in record.message for record in caplog.records)
        assert len(channel.sent) == 1
        assert cog.store.state["drops_posted"] == 1
        assert parse_dt(cog.store.state["next_drop_at"]) == as_utc(now) + CANDY_HUNT.min_interval
        await cog.maybe_drop(now)
        assert len(channel.sent) == 1

        cog.store.state["event_disabled"] = True
        cog.store.state["next_drop_at"] = iso(start)
        await cog.maybe_drop(now)
        assert cog.store.state["drops_posted"] == 1
        assert cog.store.state["next_drop_at"] == iso(start)
        assert advanced != iso(start)

    asyncio.run(run())


def test_claims_ack_and_dm_failure_stays_ephemeral(tmp_path, caplog):
    async def run():
        cog, _bot = _cog(tmp_path, rng=ScriptedRng(randoms=[0.9, 0.0]))
        cog.store.state["active_drops"] = [_active_drop("abcd1234")]

        bot_click = _interaction(5, bot=True)
        await cog.handle_claim(bot_click, "abcd1234")
        message, kwargs = _sent(bot_click.response.send_message)
        assert message == "Bots can't grab candy."
        assert kwargs["ephemeral"] is True
        assert cog.store.state["claims_total"] == 0

        missing = _interaction(6)
        await cog.handle_claim(missing, "ffff9999")
        message, kwargs = _sent(missing.response.send_message)
        assert message == LATE_CLAIM_TEXT

        winner = _interaction(6)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.handle_claim(winner, "abcd1234")
        winner.response.defer.assert_awaited()
        winner.user.send.assert_awaited()
        sent_view = winner.user.send.await_args.kwargs["view"]
        assert "content" not in winner.user.send.await_args.kwargs
        assert "TEST" not in component_text(sent_view)
        assert any(record.message.startswith("candy claimed ") for record in caplog.records)

        late = _interaction(7)
        cog.store.state["active_drops"] = [_active_drop("abcd1234")]
        cog.store.state["active_drops"][0]["status"] = "claimed"
        await cog.handle_claim(late, "abcd1234")
        message, _kwargs = _sent(late.response.send_message)
        assert message == LATE_CLAIM_TEXT

        melted = _interaction(8)
        cog.store.state["active_drops"] = [_active_drop("bbbb2222", expires_at=NOW)]
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.handle_claim(melted, "bbbb2222")
        message, kwargs = _sent(melted.response.send_message)
        assert message == MELTED_TEXT
        assert kwargs["ephemeral"] is True
        assert any("candy drop expired" in record.message for record in caplog.records)

        dm_fail = _interaction(9)
        dm_fail.user.send = AsyncMock(side_effect=_http_error())
        cog.store.state["active_drops"] = [_active_drop("cccc3333", dry_run=True)]
        cog.rng = ScriptedRng(randoms=[0.9, 0.0])
        await cog.handle_claim(dm_fail, "cccc3333")
        view_kwargs = dm_fail.followup.send.await_args_list[0].kwargs
        assert "content" not in view_kwargs
        assert view_kwargs["ephemeral"] is True
        assert "TEST" in component_text(view_kwargs["view"])
        assert "9" in cog.store.state["test"]["totals"]
        assert "9" not in cog.store.state["totals"]
        assert cog.store.state["test"]["claims_total"] == 1

    # handle_claim reads discord.utils.utcnow, so freeze it around the run.
    original = discord.utils.utcnow
    discord.utils.utcnow = lambda: NOW
    try:
        asyncio.run(run())
    finally:
        discord.utils.utcnow = original


def test_wrap_up_preview_posts_once_and_survives_a_failed_post(tmp_path, caplog):
    async def run():
        channel = FakeTextChannel(8, "announcements")
        member = SimpleNamespace(id=10, add_roles=AsyncMock(), remove_roles=AsyncMock())
        role_holder = {}

        class Guild:
            id = MTS_GUILD_ID
            roles = []
            text_channels = [channel]

            def get_member(self, user_id):
                return member if int(user_id) == 10 else None

            async def fetch_member(self, user_id):
                raise discord.NotFound(_HTTPResponse(), "missing")

            async def create_role(self, **kwargs):
                role = SimpleNamespace(id=77, name=kwargs["name"])
                self.roles.append(role)
                role_holder["role"] = role
                return role

            def get_role(self, role_id):
                return role_holder.get("role") if role_id == 77 else None

        guild = Guild()
        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = guild
        cog, _bot = _cog(tmp_path, bot=bot)
        cog.store.state["totals"] = {"20": 9, "10": 9}
        cog.store.state["drops_posted"] = 3
        cog.store.state["claims_total"] = 2

        preview_ctx = _ctx(cog, guild_id=MTS_GUILD_ID)
        await cog.admin_wrapup.callback(cog, preview_ctx, True)
        args, kwargs = preview_ctx.reply.await_args
        assert args == ()
        assert "content" not in kwargs
        assert kwargs["mention_author"] is False
        preview_text = component_text(kwargs["view"])
        assert "PREVIEW" in preview_text
        assert "<@" not in preview_text
        assert "Pumpkin God" in preview_text
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].users is False
        assert kwargs["allowed_mentions"].roles is False
        assert cog.store.state["wrap_up_done"] is False
        assert channel.sent == []
        member.add_roles.assert_not_awaited()

        channel.error = _http_error()
        failed = _ctx(cog, guild_id=MTS_GUILD_ID)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.admin_wrapup.callback(cog, failed, False)
        message, kwargs = _replied(failed)
        assert "not marked done" in message
        assert "irreversible" not in message.casefold()
        assert kwargs["mention_author"] is False
        assert cog.store.state["wrap_up_done"] is False
        assert cog.store.state["pumpkin_god"]["user_id"] == 10
        member.add_roles.assert_awaited()

        channel.error = None
        channel.sent.clear()
        done = _ctx(cog, guild_id=MTS_GUILD_ID)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.admin_wrapup.callback(cog, done, False)
        message, _kwargs = _replied(done)
        assert "irreversible" in message.casefold()
        assert cog.store.state["wrap_up_done"] is True
        assert cog.store.state["next_drop_at"] is None
        assert channel.sent[0]["args"] == ()
        assert "content" not in channel.sent[0]["kwargs"]
        posted_view = component_text(channel.sent[0]["kwargs"]["view"])
        assert "🥇" in posted_view
        assert "<@" not in posted_view
        mentions = channel.sent[0]["kwargs"]["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert any("candy wrapup posted" in record.message for record in caplog.records)

        repeat = _ctx(cog, guild_id=MTS_GUILD_ID)
        await cog.admin_wrapup.callback(cog, repeat, False)
        message, _kwargs = _replied(repeat)
        assert "already happened" in message.casefold()
        assert len(channel.sent) == 1

    original = discord.utils.utcnow
    discord.utils.utcnow = lambda: CANDY_HUNT.wrap_up_at
    try:
        asyncio.run(run())
    finally:
        discord.utils.utcnow = original


def test_automatic_wrap_up_and_role_removal(tmp_path, caplog):
    async def run():
        cog, bot = _cog(tmp_path)
        cog.store.state["drops_posted"] = 1
        cog._catching_up = True
        due = CANDY_HUNT.wrap_up_at + timedelta(days=3, seconds=5)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_wrap_up(due)
        assert cog.store.state["wrap_up_done"] is True
        assert any("candy wrapup skipped reason=too_late" in record.message for record in caplog.records)

        cog.store.state["wrap_up_done"] = False
        cog.store.state["event_disabled"] = True
        cog.store.state["drops_posted"] = 0
        cog.store.state["totals"] = {"1": 5}
        await cog.maybe_wrap_up(CANDY_HUNT.wrap_up_at)
        assert cog.store.state["wrap_up_done"] is False

        cog.store.state["event_disabled"] = False
        cog.store.state["totals"] = {}
        cog.store.state["drops_posted"] = 0
        cog._catching_up = True
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_wrap_up(CANDY_HUNT.wrap_up_at + timedelta(hours=2))
        assert cog.store.state["wrap_up_done"] is True
        assert any("candy wrapup skipped reason=no_activity" in record.message for record in caplog.records)

        remove_at = NOW - timedelta(minutes=1)
        member = SimpleNamespace(remove_roles=AsyncMock())
        role = SimpleNamespace(id=77)
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            get_member=lambda user_id: member,
            get_role=lambda role_id: role,
        )
        bot.guilds[MTS_GUILD_ID] = guild
        cog.store.state["pumpkin_god"] = {
            "user_id": 10,
            "role_id": 77,
            "guild_id": MTS_GUILD_ID,
            "remove_at": iso(remove_at),
        }
        await cog.remove_pumpkin_god_if_due(NOW)
        member.remove_roles.assert_awaited()
        assert cog.store.state["pumpkin_god"] is None

    asyncio.run(run())


def test_loop_load_schedules_without_posting_before_halloween(tmp_path, monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: BEFORE)

    async def run():
        cog, bot = _cog(tmp_path, rng=ScriptedRng(uniforms=[0.0]))
        await cog.cog_load()
        try:
            assert ClaimButton in bot.dynamic_items
            assert cog.candy_loop.is_running()
            for _ in range(50):
                if cog.store.state.get("next_drop_at"):
                    break
                await asyncio.sleep(0.01)
            start, end = window_bounds(CANDY_HUNT)
            scheduled = parse_dt(cog.store.state["next_drop_at"])
            assert as_utc(start) <= scheduled < as_utc(end)
            assert cog.store.state["drops_posted"] == 0
            assert cog._catching_up is False
        finally:
            await _stop(cog)
            await cog.cog_unload()

    asyncio.run(run())


def test_on_message_tracks_human_activity_in_mts(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        parent = FakeTextChannel(4, "chat")
        message = SimpleNamespace(
            guild=SimpleNamespace(id=MTS_GUILD_ID),
            author=SimpleNamespace(bot=False),
            webhook_id=None,
            channel=FakeThread(parent),
            created_at=NOW,
        )
        await cog.on_message(message)
        assert cog.activity[4] == NOW

        bot_message = SimpleNamespace(
            guild=SimpleNamespace(id=MTS_GUILD_ID),
            author=SimpleNamespace(bot=True),
            webhook_id=None,
            channel=parent,
            created_at=NOW + timedelta(minutes=1),
        )
        await cog.on_message(bot_message)
        assert cog.activity[4] == NOW

        elsewhere = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(bot=False),
            webhook_id=None,
            channel=FakeTextChannel(9, "other"),
            created_at=NOW,
        )
        await cog.on_message(elsewhere)
        assert 9 not in cog.activity

    asyncio.run(run())


def test_admin_status_and_power(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        cog.store.state["totals"]["1"] = 4
        status = _ctx(cog)
        await cog.admin_status.callback(cog, status)
        message, kwargs = _replied(status)
        assert kwargs["mention_author"] is False
        assert "content" not in kwargs or kwargs.get("content") == message
        assert "Disabled: False" in message
        assert len(message) < 2000
        status.send.assert_not_awaited()

        off = _ctx(cog)
        await cog.admin_power.callback(cog, off, "OFF")
        text, off_kwargs = _replied(off)
        assert off_kwargs["mention_author"] is False
        assert text == "Candy hunt disabled. Scheduled drops and the automatic wrap-up are paused."
        assert cog.store.state["event_disabled"] is True

        on = _ctx(cog)
        await cog.admin_power.callback(cog, on, "on")
        text, on_kwargs = _replied(on)
        assert on_kwargs["mention_author"] is False
        assert text == "Candy hunt enabled."
        assert cog.store.state["event_disabled"] is False

        cog.store.state["wrap_up_done"] = True
        again = _ctx(cog)
        await cog.admin_power.callback(cog, again, "On")
        text, again_kwargs = _replied(again)
        assert again_kwargs["mention_author"] is False
        assert text == "Candy hunt enabled. Wrap-up already ran, so scheduled drops stay off."
        assert cog.store.state["event_disabled"] is False
        assert cog.store.state["wrap_up_done"] is True

        bad = _ctx(cog)
        await cog.admin_power.callback(cog, bad, "maybe")
        usage, usage_kwargs = _replied(bad)
        assert usage == "Usage: !candyadmin power on|off"
        assert usage_kwargs["mention_author"] is False
        assert cog.store.state["event_disabled"] is False

        missing = _ctx(cog)
        await cog.admin_power.callback(cog, missing)
        usage, _kwargs = _replied(missing)
        assert usage == "Usage: !candyadmin power on|off"

    asyncio.run(run())


def _command_tree():
    connection = SimpleNamespace(_command_tree=None)
    client = SimpleNamespace(http=SimpleNamespace(), _connection=connection)
    return discord.app_commands.CommandTree(client)


def test_candy_commands_register_on_the_guild_trees_only(tmp_path):
    cog, _bot = _cog(tmp_path)
    assert [command.name for command in cog.get_app_commands()] == ["candy"]
    assert not any(command.name == "candyadmin" for command in cog.walk_app_commands())
    assert not any(isinstance(command, discord.app_commands.Group) for command in cog.walk_app_commands())
    assert [command.name for command in cog.get_commands()] == ["candyadmin"]
    assert {command.name for command in cog.candyadmin.commands} == {
        "status",
        "power",
        "test_session",
        "test_session_stop",
        "wrapup",
        "cleanup",
    }
    assert cog.candyadmin.hidden is True
    assert all(command.hidden for command in cog.candyadmin.commands)
    assert list(cog.candy._guild_ids) == list(CANDY_GUILD_IDS)
    tree = _command_tree()
    tree.add_command(cog.candy)
    assert [command.name for command in tree.get_commands()] == []
    for gid in CANDY_GUILD_IDS:
        names = [command.name for command in tree.get_commands(guild=discord.Object(id=gid))]
        assert names == ["candy"]
    assert tree.get_commands(guild=discord.Object(id=1)) == []


def test_guild_sync_is_once_per_load_and_not_global(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: BEFORE)

    async def run():
        cog, bot = _cog(tmp_path)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.sync_candy_guild_commands()
        assert bot.tree.guild_syncs == list(CANDY_GUILD_IDS)
        assert bot.tree.global_syncs == 0
        assert bot.tree.copy_calls == []
        text = "\n".join(record.message for record in caplog.records)
        assert f"candy commands synced guild={MTS_GUILD_ID} count=1 names=candy" in text
        assert f"candy commands synced guild={SUPPORT_GUILD_ID} count=1 names=candy" in text
        assert "copy_global_to" not in text

        bot.tree.fail_guilds.add(SUPPORT_GUILD_ID)
        bot.tree.guild_syncs.clear()
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="cogs.candyhunt"):
            await cog.sync_candy_guild_commands()
        assert bot.tree.guild_syncs == list(CANDY_GUILD_IDS)
        assert bot.tree.global_syncs == 0
        assert any(
            f"candy commands synced guild={SUPPORT_GUILD_ID} failed" in record.message
            for record in caplog.records
        )

        bot.tree.fail_guilds.clear()
        bot.tree.guild_syncs.clear()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.cog_load()
            await cog._startup_task
        try:
            assert bot.tree.guild_syncs == list(CANDY_GUILD_IDS)
            assert bot.tree.global_syncs == 0
            assert bot.tree.copy_calls == []
            bot.tree.guild_syncs.clear()
            cog._ensure_startup_task()
            await asyncio.sleep(0)
            assert bot.tree.guild_syncs == []
            assert cog._guilds_synced is True
        finally:
            await _stop(cog)
            await cog.cog_unload()

    asyncio.run(run())


class _LowRng:
    def uniform(self, low, high):
        return low

    def choice(self, seq):
        return seq[0]


def test_test_drop_timing_stays_inside_jitter_and_expiry():
    rng = random.Random(1)
    samples = [jittered_delay_seconds(2, rng) for _ in range(40)]
    assert all(96 <= sample <= 144 for sample in samples)
    assert min(samples) < 120 < max(samples)
    assert jittered_delay_seconds(1, _LowRng()) == 60
    assert jittered_delay_seconds(2, _LowRng()) == 96

    assert session_drop_lifetime(timedelta(minutes=10), timedelta(minutes=2)) == timedelta(minutes=2)
    assert session_drop_lifetime(timedelta(minutes=10), timedelta(minutes=1)) == timedelta(minutes=1)
    assert session_drop_lifetime(timedelta(minutes=10), timedelta(minutes=30)) == TEST_DROP_EXPIRY
    assert session_drop_lifetime(timedelta(minutes=1), timedelta(minutes=2)) == timedelta(minutes=1)

    session = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=NOW,
        source="command",
    )
    assert resolve_test_session(session, NOW) == "drop"
    assert resolve_test_session(session, NOW - timedelta(seconds=1)) == "wait"
    assert resolve_test_session(None, NOW) == "idle"
    ended = dict(session)
    ended["ends_at"] = iso(NOW)
    assert resolve_test_session(ended, NOW) == "end"
    mts = dict(session, guild_id=MTS_GUILD_ID)
    assert resolve_test_session(mts, NOW) == "end"

    cleaned = normalize_state({"test": {"session": session, "totals": {"4": 2}}})
    assert cleaned["test"]["session"]["channel_id"] == SUPPORT_TEST_CHANNEL_ID
    assert cleaned["test"]["totals"] == {"4": 2}
    assert cleaned["totals"] == {}
    assert normalize_test_session(dict(session, guild_id=MTS_GUILD_ID)) is None
    assert parse_autostart({"guild_id": SUPPORT_GUILD_ID, "channel_id": SUPPORT_TEST_CHANNEL_ID}) == {
        "guild_id": SUPPORT_GUILD_ID,
        "channel_id": SUPPORT_TEST_CHANNEL_ID,
        "minutes": 15,
        "interval": 2,
    }
    assert parse_autostart({"guild_id": MTS_GUILD_ID, "channel_id": 1})["guild_id"] == MTS_GUILD_ID
    assert parse_autostart([]) is None


def test_test_session_resumes_and_ends_without_touching_mts(tmp_path, caplog):
    async def run():
        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        bot = FakeBot()
        bot.channels[channel.id] = channel
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(uniforms=[120.0]))
        saved_next = iso(NOW + timedelta(hours=2))
        cog.store.state["next_drop_at"] = saved_next
        cog.store.state["totals"]["8"] = 6
        session = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=channel.id,
            minutes=15,
            interval=2,
            now=NOW - timedelta(minutes=30),
            source="autostart",
        )
        cog.store.state["test"]["session"] = session
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_test_session(NOW)
        assert channel.sent == []
        assert cog.store.state["test"]["session"] is None
        assert cog.store.state["next_drop_at"] == saved_next
        assert cog.store.state["totals"] == {"8": 6}
        assert cog.store.state["drops_posted"] == 0
        assert any("reason=elapsed" in record.message for record in caplog.records)
        assert not any("candy channels eligible=" in record.message for record in caplog.records)

        waiting = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=channel.id,
            minutes=15,
            interval=2,
            now=NOW,
            source="command",
        )
        waiting["next_drop_at"] = iso(NOW + timedelta(minutes=4))
        cog.store.state["test"]["session"] = waiting
        caplog.clear()
        await cog.maybe_test_session(NOW)
        assert channel.sent == []
        assert cog.store.state["test"]["session"]["drops_posted"] == 0
        assert cog.store.state["next_drop_at"] == saved_next

        cog.store.state["test"]["session"]["next_drop_at"] = iso(NOW)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_test_session(NOW)
        assert len(channel.sent) == 1
        assert "TEST" in component_text(channel.sent[0]["kwargs"]["view"])
        posted = next(drop for drop in cog.store.state["active_drops"] if drop["dry_run"])
        assert parse_dt(posted["expires_at"]) - as_utc(NOW) == timedelta(minutes=2)
        assert cog.store.state["test"]["drops_posted"] == 1
        assert cog.store.state["test"]["session"]["drops_posted"] == 1
        assert parse_dt(cog.store.state["test"]["session"]["next_drop_at"]) == as_utc(NOW) + timedelta(seconds=120)
        assert cog.store.state["drops_posted"] == 0
        assert cog.store.state["totals"] == {"8": 6}
        assert cog.store.state["next_drop_at"] == saved_next
        assert any("candy drop posted" in record.message and "dry_run=true" in record.message for record in caplog.records)
        assert not any("candy channels eligible=" in record.message for record in caplog.records)

    asyncio.run(run())


def test_test_session_command_refuses_mts_and_posts_a_dry_run(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: NOW)

    async def run():
        cog, bot = _cog(tmp_path, rng=ScriptedRng(uniforms=[120.0, 120.0]))
        saved_next = iso(NOW + timedelta(hours=3))
        cog.store.state["next_drop_at"] = saved_next
        cog.store.state["totals"]["8"] = 3
        home = FakeTextChannel(4, "general")
        cog.store.state["test"]["totals"]["4"] = 9
        cog.store.state["streak"] = 2
        cog.store.state["active_drops"] = [_active_drop("ffff6666", dry_run=True)]
        refused = _ctx(cog, guild_id=MTS_GUILD_ID, channel=home)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.admin_test_session.callback(cog, refused, "15", "2", None)
        message, kwargs = _replied(refused)
        assert "stay out of Menaces to Sobriety" in message
        assert kwargs["mention_author"] is False
        assert cog.store.state["test"]["session"] is None
        assert cog.store.state["test"]["totals"]["4"] == 9
        assert cog.store.state["streak"] == 2
        assert cog.store.state["active_drops"][0]["drop_id"] == "ffff6666"
        assert cog.store.state["next_drop_at"] == saved_next
        assert cog.store.state["totals"] == {"8": 3}
        assert any("reason=mts source=command" in record.message for record in caplog.records)

        mts_channel = FakeTextChannel(5, "lounge")
        mts_channel.guild = SimpleNamespace(id=MTS_GUILD_ID)
        crossed = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=mts_channel)
        await cog.admin_test_session.callback(cog, crossed, 15, 2, mts_channel)
        message, _kwargs = _replied(crossed)
        assert "stay out of Menaces to Sobriety" in message
        assert cog.store.state["test"]["session"] is None

        bad = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=home)
        await cog.admin_test_session.callback(cog, bad, 0, 2, None)
        message, kwargs = _replied(bad)
        assert "minutes" in message
        assert "test_session" in message
        assert kwargs["mention_author"] is False
        assert cog.store.state["test"]["totals"]["4"] == 9
        assert cog.store.state["streak"] == 2
        assert cog.store.state["active_drops"][0]["drop_id"] == "ffff6666"

        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        bot.channels[channel.id] = channel
        started = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.admin_test_session.callback(cog, started)
        message, kwargs = _replied(started)
        assert kwargs["mention_author"] is False
        assert f"<#{channel.id}>" in message
        assert "test tally" in message
        assert "previous test tally was cleared" in message.casefold()
        assert not any(drop["drop_id"] == "ffff6666" for drop in cog.store.state["active_drops"])
        assert cog.store.state["test"]["totals"] == {}
        assert cog.store.state["streak"] == 2
        session = cog.store.state["test"]["session"]
        assert session["minutes"] == 15
        assert session["interval"] == 2
        assert session["channel_id"] == channel.id
        assert session["source"] == "command"
        assert session["drops_posted"] == 1
        assert len(channel.sent) == 1
        assert "TEST" in component_text(channel.sent[0]["kwargs"]["view"])
        assert cog.store.state["drops_posted"] == 0
        assert cog.store.state["totals"] == {"8": 3}
        assert cog.store.state["next_drop_at"] == saved_next
        assert any(
            "candy test session started "
            f"guild={SUPPORT_GUILD_ID} channel={channel.id} minutes=15 interval=2 source=command"
            in record.message
            for record in caplog.records
        )
        assert any("dry_run=true" in record.message for record in caplog.records)

        status = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_status.callback(cog, status)
        text, status_kwargs = _replied(status)
        assert status_kwargs["mention_author"] is False
        assert f"channel={channel.id}" in text
        assert f"ends={_clock(parse_dt(session['ends_at']), PT)}" in text
        assert f"next={_clock(parse_dt(session['next_drop_at']), PT)}" in text
        assert "drops=1" in text

        cog.store.state["test"]["totals"]["4"] = 2
        cog.store.state["test"]["session"]["streak"] = 3
        again = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        await cog.admin_test_session.callback(cog, again, 15, 2, channel)
        message, _kwargs = _replied(again)
        assert "already running" in message
        assert "!candyadmin test_session_stop" in message
        assert cog.store.state["test"]["session"]["started_at"] == session["started_at"]
        assert cog.store.state["test"]["totals"]["4"] == 2
        assert cog.store.state["test"]["session"]["streak"] == 3
        assert cog.store.state["streak"] == 2
        assert cog.store.state["totals"] == {"8": 3}
        assert cog.store.state["active_drops"]

        stop = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_test_session_stop.callback(cog, stop)
        message, kwargs = _replied(stop)
        assert "stopped" in message.casefold()
        assert kwargs["mention_author"] is False
        assert cog.store.state["test"]["session"] is None
        assert cog.store.state["totals"] == {"8": 3}
        assert cog.store.state["next_drop_at"] == saved_next

        missing = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_test_session_stop.callback(cog, missing)
        message, _kwargs = _replied(missing)
        assert message == "No test session is running."

    asyncio.run(run())


def test_test_session_start_clears_test_state_and_busy_does_not(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: NOW)

    async def run():
        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        deleted: list[int] = []

        async def fetch_message(message_id):
            async def delete():
                deleted.append(int(message_id))

            return SimpleNamespace(id=message_id, delete=delete)

        channel.fetch_message = fetch_message
        bot = FakeBot()
        bot.channels[channel.id] = channel
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(uniforms=[120.0]))
        saved_next = iso(NOW + timedelta(hours=2))
        cog.store.state["next_drop_at"] = saved_next
        cog.store.state["totals"]["8"] = 5
        cog.store.state["streak"] = 4
        cog.store.state["drops_posted"] = 3
        cog.store.state["claims_total"] = 3
        real = _active_drop("aaaa1111", dry_run=False)
        leftover = _active_drop("bbbb2222", dry_run=True)
        leftover["channel_id"] = channel.id
        leftover["message_id"] = 4242
        cog.store.state["active_drops"] = [real, leftover]
        ended = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=channel.id,
            minutes=15,
            interval=2,
            now=NOW - timedelta(hours=1),
            source="command",
        )
        ended["ends_at"] = iso(NOW - timedelta(seconds=1))
        ended["streak"] = 7
        cog.store.state["test"]["totals"]["4"] = 9
        cog.store.state["test"]["claim_counts"]["4"] = 2
        cog.store.state["test"]["claims_total"] = 2
        cog.store.state["test"]["drops_posted"] = 2
        cog.store.state["test"]["session"] = ended

        started = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        await cog.admin_test_session.callback(cog, started)
        message, kwargs = _replied(started)
        assert kwargs["mention_author"] is False
        assert "previous test tally was cleared" in message.casefold()
        session = cog.store.state["test"]["session"]
        assert session["streak"] == 0
        assert session["source"] == "command"
        assert session["started_at"] == iso(NOW)
        assert session["started_at"] != ended["started_at"]
        assert cog.store.state["test"]["totals"] == {}
        assert cog.store.state["test"]["claim_counts"] == {}
        assert cog.store.state["test"]["claims_total"] == 0
        assert cog.store.state["test"]["drops_posted"] == 1
        assert not any(drop["drop_id"] == "bbbb2222" for drop in cog.store.state["active_drops"])
        assert any(drop["drop_id"] == "aaaa1111" and not drop["dry_run"] for drop in cog.store.state["active_drops"])
        assert 4242 in deleted
        assert real["message_id"] not in deleted
        assert cog.store.state["totals"] == {"8": 5}
        assert cog.store.state["streak"] == 4
        assert cog.store.state["drops_posted"] == 3
        assert cog.store.state["claims_total"] == 3
        assert cog.store.state["next_drop_at"] == saved_next

        cog.store.state["test"]["totals"]["4"] = 9
        cog.store.state["test"]["session"]["streak"] = 3
        busy_drop = _active_drop("cccc3333", dry_run=True)
        busy_drop["channel_id"] = channel.id
        busy_drop["message_id"] = 5252
        cog.store.state["active_drops"].append(busy_drop)
        deleted.clear()
        drops_before = [drop["drop_id"] for drop in cog.store.state["active_drops"]]
        again = _ctx(cog, guild_id=SUPPORT_GUILD_ID, channel=channel)
        await cog.admin_test_session.callback(cog, again, 15, 2, channel)
        message, kwargs = _replied(again)
        assert kwargs["mention_author"] is False
        assert "already running" in message
        assert cog.store.state["test"]["session"]["started_at"] == session["started_at"]
        assert cog.store.state["test"]["totals"]["4"] == 9
        assert cog.store.state["test"]["session"]["streak"] == 3
        assert [drop["drop_id"] for drop in cog.store.state["active_drops"]] == drops_before
        assert deleted == []
        assert cog.store.state["totals"] == {"8": 5}
        assert cog.store.state["streak"] == 4
        assert cog.store.state["next_drop_at"] == saved_next

        await cog.stop_test_session(reason="stopped")
        cog.store.state["test"]["totals"]["4"] = 11
        cog.store.state["test"]["claims_total"] = 1
        rerun = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=channel.id,
            minutes=15,
            interval=2,
            now=NOW - timedelta(hours=2),
            source="command",
        )
        rerun["ends_at"] = iso(NOW - timedelta(seconds=1))
        rerun["streak"] = 6
        cog.store.state["test"]["session"] = rerun
        another = _active_drop("dddd4444", dry_run=True)
        another["channel_id"] = channel.id
        another["message_id"] = 6262
        cog.store.state["active_drops"].append(another)
        deleted.clear()
        path = tmp_path / "candy_test_session_autostart.json"
        path.write_text(
            json.dumps(
                {
                    "guild_id": SUPPORT_GUILD_ID,
                    "channel_id": SUPPORT_TEST_CHANNEL_ID,
                    "minutes": 15,
                    "interval": 2,
                }
            ),
            encoding="utf-8",
        )
        cog.autostart_path = path
        cog._autostart = read_autostart(path)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog._consume_autostart()
        assert not path.exists()
        auto = cog.store.state["test"]["session"]
        assert auto["source"] == "autostart"
        assert auto["streak"] == 0
        assert auto["started_at"] == iso(NOW)
        assert cog.store.state["test"]["totals"] == {}
        assert cog.store.state["test"]["claims_total"] == 0
        assert cog.store.state["test"]["drops_posted"] == 1
        assert not any(drop["drop_id"] == "dddd4444" for drop in cog.store.state["active_drops"])
        assert not any(drop["drop_id"] == "cccc3333" for drop in cog.store.state["active_drops"])
        assert any(drop["drop_id"] == "aaaa1111" and not drop["dry_run"] for drop in cog.store.state["active_drops"])
        assert 6262 in deleted
        assert 5252 in deleted
        assert real["message_id"] not in deleted
        assert cog.store.state["totals"] == {"8": 5}
        assert cog.store.state["streak"] == 4
        assert cog.store.state["drops_posted"] == 3
        assert cog.store.state["next_drop_at"] == saved_next
        assert any(
            "candy test session started " in record.message and "source=autostart" in record.message
            for record in caplog.records
        )

        cog.store.state["test"]["totals"]["4"] = 12
        cog.store.state["test"]["session"]["streak"] = 2
        kept = [drop["drop_id"] for drop in cog.store.state["active_drops"]]
        deleted.clear()
        busy_path = tmp_path / "busy.json"
        busy_path.write_text(
            json.dumps(
                {
                    "guild_id": SUPPORT_GUILD_ID,
                    "channel_id": SUPPORT_TEST_CHANNEL_ID,
                    "minutes": 15,
                    "interval": 2,
                }
            ),
            encoding="utf-8",
        )
        cog.autostart_path = busy_path
        cog._autostart = read_autostart(busy_path)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog._consume_autostart()
        assert not busy_path.exists()
        assert cog.store.state["test"]["totals"]["4"] == 12
        assert cog.store.state["test"]["session"]["streak"] == 2
        assert cog.store.state["test"]["session"]["source"] == "autostart"
        assert cog.store.state["test"]["session"]["started_at"] == auto["started_at"]
        assert [drop["drop_id"] for drop in cog.store.state["active_drops"]] == kept
        assert deleted == []
        assert cog.store.state["streak"] == 4
        assert cog.store.state["totals"] == {"8": 5}
        assert cog.store.state["next_drop_at"] == saved_next
        assert any("reason=already_running source=autostart" in record.message for record in caplog.records)

    asyncio.run(run())


def test_support_leaderboard_and_wrapup_use_the_test_tally(tmp_path):
    async def run():
        announce = FakeTextChannel(8, "announcements")
        member = SimpleNamespace(id=10, add_roles=AsyncMock())

        class Guild:
            def __init__(self, guild_id, channels):
                self.id = guild_id
                self.text_channels = channels
                self.roles = []

            def get_member(self, user_id):
                return member if int(user_id) == 10 else None

            async def create_role(self, **kwargs):
                raise AssertionError("roles stay off during a support wrap-up")

        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = Guild(MTS_GUILD_ID, [announce])
        bot.guilds[SUPPORT_GUILD_ID] = Guild(SUPPORT_GUILD_ID, [FakeTextChannel(9, "announcements")])
        cog, _bot = _cog(tmp_path, bot=bot)
        cog.store.state["totals"] = {"10": 9}
        cog.store.state["drops_posted"] = 4
        cog.store.state["claims_total"] = 3
        cog.store.state["test"]["totals"] = {"4": 5}
        cog.store.state["test"]["drops_posted"] = 2
        cog.store.state["test"]["claims_total"] = 2

        home = _interaction(3, guild_id=MTS_GUILD_ID)
        await cog.candy.callback(cog, home)
        text = component_text(home.response.send_message.await_args.kwargs["view"])
        assert "<@10>" in text
        assert "<@4>" not in text
        assert "Test leaderboard" not in text

        support = _interaction(4, guild_id=SUPPORT_GUILD_ID)
        await cog.candy.callback(cog, support)
        kwargs = support.response.send_message.await_args.kwargs
        text = component_text(kwargs["view"])
        assert "Test leaderboard" in text
        assert "<@4>" in text
        assert "<@10>" not in text
        assert kwargs["allowed_mentions"].users is False

        preview = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_wrapup.callback(cog, preview, True)
        view = preview.reply.await_args.kwargs["view"]
        preview_text = component_text(view)
        assert "PREVIEW" in preview_text
        assert "TEST" in preview_text
        assert "<@" not in preview_text
        assert "4" in preview_text
        assert "10" not in preview_text
        assert "content" not in preview.reply.await_args.kwargs
        assert preview.reply.await_args.kwargs["mention_author"] is False
        assert announce.sent == []
        member.add_roles.assert_not_awaited()
        assert cog.store.state["wrap_up_done"] is False

        refused = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_wrapup.callback(cog, refused, "false")
        message, kwargs = _replied(refused)
        assert "will not post" in message
        assert "roles" in message
        assert kwargs["mention_author"] is False
        assert announce.sent == []
        member.add_roles.assert_not_awaited()
        assert cog.store.state["wrap_up_done"] is False
        assert cog.store.state["totals"]["10"] == 9

    asyncio.run(run())


def test_mts_schedule_and_test_session_post_independently(tmp_path, caplog):
    start, _end = window_bounds(CANDY_HUNT)

    async def run():
        lounge = FakeTextChannel(42, "lounge")
        test_channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        test_channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[lounge],
            me=SimpleNamespace(id=1),
            default_role=SimpleNamespace(id=0),
            rules_channel=None,
            members=[],
        )
        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = guild
        bot.channels[test_channel.id] = test_channel
        cog, _bot = _cog(
            tmp_path,
            bot=bot,
            rng=ScriptedRng(uniforms=[CANDY_HUNT.min_interval.total_seconds(), 120.0]),
        )
        cog.store.state["next_drop_at"] = iso(start)
        cog.store.state["totals"]["8"] = 4
        cog.store.state["test"]["session"] = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=test_channel.id,
            minutes=15,
            interval=2,
            now=NOW,
            source="command",
        )
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.tick(now=NOW)
        assert len(lounge.sent) == 1
        assert "TEST DROP" not in component_text(lounge.sent[0]["kwargs"]["view"])
        assert len(test_channel.sent) == 1
        assert "TEST" in component_text(test_channel.sent[0]["kwargs"]["view"])
        assert cog.store.state["drops_posted"] == 1
        assert cog.store.state["test"]["drops_posted"] == 1
        assert cog.store.state["test"]["session"]["drops_posted"] == 1
        assert cog.store.state["totals"] == {"8": 4}
        assert parse_dt(cog.store.state["next_drop_at"]) == as_utc(NOW) + CANDY_HUNT.min_interval
        assert parse_dt(cog.store.state["test"]["session"]["next_drop_at"]) == as_utc(NOW) + timedelta(seconds=120)
        real = next(drop for drop in cog.store.state["active_drops"] if not drop["dry_run"])
        test_drop = next(drop for drop in cog.store.state["active_drops"] if drop["dry_run"])
        assert parse_dt(real["expires_at"]) - as_utc(NOW) == CANDY_HUNT.drop_expiry
        assert parse_dt(test_drop["expires_at"]) - as_utc(NOW) == timedelta(minutes=2)
        eligible = [record for record in caplog.records if "candy channels eligible=" in record.message]
        assert len(eligible) == 1
        assert any("dry_run=false" in record.message for record in caplog.records)
        assert any("dry_run=true" in record.message for record in caplog.records)

    asyncio.run(run())


def test_autostart_flag_fires_once_and_refuses_mts(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(discord.utils, "utcnow", lambda: BEFORE)

    async def run():
        path = tmp_path / "candy_test_session_autostart.json"
        path.write_text(
            json.dumps(
                {
                    "guild_id": SUPPORT_GUILD_ID,
                    "channel_id": SUPPORT_TEST_CHANNEL_ID,
                    "minutes": 15,
                    "interval": 2,
                }
            ),
            encoding="utf-8",
        )
        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        bot = FakeBot()
        bot.channels[channel.id] = channel
        cog, _bot = _cog(tmp_path, bot=bot, autostart_path=path)
        saved_next = iso(NOW)
        cog.store.state["next_drop_at"] = saved_next
        cog.store.state["totals"]["8"] = 5
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.cog_load()
            await cog._startup_task
        try:
            assert not path.exists()
            session = cog.store.state["test"]["session"]
            assert session["guild_id"] == SUPPORT_GUILD_ID
            assert session["channel_id"] == SUPPORT_TEST_CHANNEL_ID
            assert session["minutes"] == 15
            assert session["interval"] == 2
            assert session["source"] == "autostart"
            assert session["drops_posted"] == 1
            assert len(channel.sent) == 1
            assert "TEST" in component_text(channel.sent[0]["kwargs"]["view"])
            assert cog.store.state["totals"] == {"8": 5}
            assert cog.store.state["drops_posted"] == 0
            assert cog.store.state["next_drop_at"] == saved_next
            assert any(
                "candy test session started "
                f"guild={SUPPORT_GUILD_ID} channel={SUPPORT_TEST_CHANNEL_ID} minutes=15 interval=2 source=autostart"
                in record.message
                for record in caplog.records
            )
            assert any("dry_run=true" in record.message for record in caplog.records)
        finally:
            await _stop(cog)
            await cog.cog_unload()

        mts_path = tmp_path / "mts.json"
        mts_path.write_text(
            json.dumps({"guild_id": MTS_GUILD_ID, "channel_id": 1, "minutes": 15, "interval": 2}),
            encoding="utf-8",
        )
        other_dir = tmp_path / "fresh"
        other_dir.mkdir()
        other, _bot = _cog(other_dir, autostart_path=mts_path)
        other._autostart = read_autostart(mts_path)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await other._consume_autostart()
        assert not mts_path.exists()
        assert other.store.state["test"]["session"] is None
        assert any("reason=mts source=autostart" in record.message for record in caplog.records)
        assert not any("candy test session started" in record.message for record in caplog.records)

        bad_dir = tmp_path / "bad-flag"
        bad_dir.mkdir()
        bad = bad_dir / "candy_test_session_autostart.json"
        bad.write_text("{", encoding="utf-8")
        assert read_autostart(bad) == {"error": "unreadable"}
        broken, _bot = _cog(bad_dir, autostart_path=bad)
        broken._autostart = read_autostart(bad)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await broken._consume_autostart()
        assert not bad.exists()
        assert any("reason=unreadable source=autostart" in record.message for record in caplog.records)
        assert read_autostart(tmp_path / "missing.json") is None

    asyncio.run(run())


def _pt(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 31, hour, minute, second, tzinfo=PT)


def _split_ambient(blurb: str, lines: tuple[str, ...]) -> tuple[str, bool]:
    for extra in AMBIENT_EXTRAS:
        suffix = " " + extra
        if blurb.endswith(suffix):
            head = blurb[: -len(suffix)]
            if head in lines:
                return head, True
    return blurb, False


def test_streak_shrinks_claims_and_resets_when_a_drop_expires():
    assert streak_factor(0, CANDY_HUNT) == 1
    assert streak_factor(1, CANDY_HUNT) == pytest.approx(0.6)
    assert streak_factor(2, CANDY_HUNT) == pytest.approx(0.36)
    assert streak_factor(3, CANDY_HUNT) == pytest.approx(0.216)

    state = default_state()
    for index, drop_id in enumerate(("aaaa1111", "bbbb2222", "cccc3333"), start=1):
        state["active_drops"] = [_active_drop(drop_id)]
        state, outcome = apply_claim(
            state,
            drop_id,
            7,
            NOW,
            ScriptedRng(randoms=[0.9, 0.0, 1.0]),
            is_bot=False,
            trick_chance=0,
            settings=CANDY_HUNT,
        )
        assert outcome.status == "won" and not outcome.dry_run
        assert state["streak"] == index

    held = state["next_drop_at"]
    state["active_drops"] = [_active_drop("dddd4444", dry_run=True)]
    state, dry = apply_claim(
        state,
        "dddd4444",
        9,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0, 1.0]),
        is_bot=False,
        trick_chance=0,
        settings=CANDY_HUNT,
    )
    assert dry.status == "won" and dry.dry_run
    assert state["streak"] == 3
    assert state["next_drop_at"] == held
    assert state["test"]["totals"]["9"] == 1

    state["active_drops"] = [_active_drop("eeee5555", expires_at=NOW)]
    state, expired = apply_claim(
        state,
        "eeee5555",
        7,
        NOW,
        ScriptedRng(),
        is_bot=False,
        trick_chance=0,
        settings=CANDY_HUNT,
    )
    assert expired.status == "expired"
    assert state["streak"] == 0


def test_effective_interval_floors_ramp_and_status_line():
    afternoon = _pt(14, 0)
    assert late_night_factor(CANDY_HUNT, _pt(17, 59)) == pytest.approx(1.0)
    assert late_night_factor(CANDY_HUNT, _pt(18, 0)) == pytest.approx(1.0)
    assert late_night_factor(CANDY_HUNT, _pt(19, 30)) == pytest.approx(0.75)
    assert late_night_factor(CANDY_HUNT, _pt(21, 0)) == pytest.approx(0.5)
    assert late_night_factor(CANDY_HUNT, datetime(2026, 11, 1, 2, 0, tzinfo=PT)) == pytest.approx(0.5)

    low, high = effective_interval(CANDY_HUNT, 0, afternoon)
    assert (low, high) == (timedelta(minutes=30), timedelta(minutes=60))
    low, high = effective_interval(CANDY_HUNT, 2, afternoon)
    assert low == timedelta(minutes=12)
    assert high.total_seconds() / 60 == pytest.approx(21.6)

    low, high = effective_interval(CANDY_HUNT, 2, _pt(22, 0))
    assert low.total_seconds() / 60 == pytest.approx(5.4)
    assert high.total_seconds() / 60 == pytest.approx(10.8)
    low, high = effective_interval(CANDY_HUNT, 12, afternoon)
    assert (low, high) == (timedelta(minutes=12), timedelta(minutes=15))
    assert effective_interval(CANDY_HUNT, 12, _pt(17, 30)) == (
        timedelta(minutes=12),
        timedelta(minutes=15),
    )
    assert effective_interval(CANDY_HUNT, 12, _pt(18, 0)) == (timedelta(minutes=1), timedelta(minutes=1))

    tiny = replace(CANDY_HUNT, min_interval=timedelta(minutes=5), max_interval=timedelta(minutes=8))
    assert effective_interval(tiny, 8, afternoon) == (timedelta(minutes=5), timedelta(minutes=8))
    odd = replace(
        CANDY_HUNT,
        streak_floor_low=timedelta(minutes=20),
        streak_floor_high=timedelta(minutes=10),
    )
    assert effective_interval(odd, 8, afternoon) == (timedelta(minutes=20), timedelta(minutes=20))

    state = default_state()
    state["streak"] = 2
    text = render_status(state, CANDY_HUNT, _pt(22, 0))
    assert "Window: Oct 31 10:00 AM - Nov 1 6:00 AM PDT/PST" in text
    assert "Late ramp: 6:00 PM → 9:00 PM" in text
    assert "Floor: 1 min" in text
    assert "Drop lifetime: 10 min" in text
    assert "Streak: 2 claimed in a row (x0.36) | Late ramp: x0.50 | Next interval: 5.4-10.8 min" in text
    text = render_status(state, CANDY_HUNT, afternoon)
    assert "Floor: 12-15 min" in text
    assert "Streak: 2 claimed in a row (x0.36) | Late ramp: x1.00 | Next interval: 12-21.6 min" in text


def test_next_drop_stays_before_window_end_and_claim_only_pulls_earlier():
    _start, end = window_bounds(CANDY_HUNT)
    origin = end - timedelta(minutes=5)
    for streak in (0, 2):
        low, high = effective_interval(CANDY_HUNT, streak, origin)
        assert low > timedelta(minutes=5)
        for seed in range(12):
            nxt = compute_next_drop_at(
                origin,
                window_end=end,
                min_interval=low,
                max_interval=high,
                rng=random.Random(seed),
                first=False,
            )
            assert nxt is None

    tight = replace(CANDY_HUNT, min_interval=timedelta(minutes=1), max_interval=timedelta(minutes=2))
    low, high = effective_interval(tight, 0, origin)
    for seed in range(12):
        nxt = compute_next_drop_at(
            origin,
            window_end=end,
            min_interval=low,
            max_interval=high,
            rng=random.Random(seed),
            first=False,
        )
        assert nxt is not None
        assert as_utc(origin) + low <= as_utc(nxt) <= as_utc(origin) + high
        assert as_utc(nxt) < as_utc(end)

    past_end = pull_earlier(
        end + timedelta(seconds=5),
        end + timedelta(minutes=1),
        end - timedelta(seconds=10),
        end,
        lead=timedelta(seconds=60),
    )
    assert past_end is None

    now = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    posted = now - timedelta(minutes=1)
    existing = now + timedelta(minutes=40)
    low, _high = effective_interval(CANDY_HUNT, 1, posted)

    def claim(state, drop_id, rng):
        return apply_claim(
            state,
            drop_id,
            7,
            now,
            rng,
            is_bot=False,
            trick_chance=0,
            settings=CANDY_HUNT,
        )

    state = default_state()
    state["next_drop_at"] = iso(existing)
    state["active_drops"] = [make_drop("abcd1234", 55, 66, posted, now + timedelta(minutes=9), dry_run=False)]
    updated, outcome = claim(
        state,
        "abcd1234",
        ScriptedRng(randoms=[0.9, 0.0, 1.0], uniforms=[low.total_seconds()]),
    )
    assert outcome.status == "won"
    assert updated["streak"] == 1
    nxt = parse_dt(updated["next_drop_at"])
    assert as_utc(nxt) == as_utc(posted) + low
    assert as_utc(now) + timedelta(seconds=60) <= as_utc(nxt) < as_utc(existing)
    assert as_utc(nxt) < as_utc(end)

    soon = now + timedelta(minutes=8)
    state = default_state()
    state["next_drop_at"] = iso(soon)
    state["active_drops"] = [make_drop("bbbb2222", 55, 66, posted, now + timedelta(minutes=9), dry_run=False)]
    updated, _outcome = claim(
        state,
        "bbbb2222",
        ScriptedRng(randoms=[0.9, 0.0, 1.0], uniforms=[999999.0]),
    )
    assert parse_dt(updated["next_drop_at"]) == as_utc(soon)

    state = default_state()
    state["next_drop_at"] = iso(existing)
    state["active_drops"] = [make_drop("cccc3333", 55, 66, posted, now + timedelta(minutes=9), dry_run=False)]
    updated, _outcome = claim(
        state,
        "cccc3333",
        ScriptedRng(randoms=[0.9, 0.0, 1.0], uniforms=[1.0]),
    )
    assert parse_dt(updated["next_drop_at"]) == as_utc(now) + timedelta(seconds=60)

    state = default_state()
    state["active_drops"] = [make_drop("dddd4444", 55, 66, posted, now + timedelta(minutes=9), dry_run=False)]
    updated, _outcome = claim(state, "dddd4444", ScriptedRng(randoms=[0.9, 0.0, 1.0]))
    assert updated["next_drop_at"] is None
    assert updated["streak"] == 1


def test_streak_state_roundtrip_accepts_old_files(tmp_path):
    assert default_state()["streak"] == 0
    assert normalize_state({"totals": {"1": 2}})["streak"] == 0
    assert normalize_state({"streak": "nope"})["streak"] == 0
    assert normalize_state({"streak": -4})["streak"] == 0
    assert normalize_state({"streak": True})["streak"] == 0
    assert normalize_state({"streak": 2.5})["streak"] == 0
    assert normalize_state({"streak": 4})["streak"] == 4

    session = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=NOW,
        source="command",
    )
    assert session["streak"] == 0
    legacy = dict(session)
    del legacy["streak"]
    assert normalize_test_session(legacy)["streak"] == 0
    assert normalize_test_session(dict(legacy, streak="bogus"))["streak"] == 0
    assert normalize_test_session(dict(legacy, streak=-1))["streak"] == 0
    assert normalize_test_session(dict(legacy, streak=3))["streak"] == 3
    cleaned = normalize_state({"test": {"session": legacy}, "streak": "x"})
    assert cleaned["streak"] == 0
    assert cleaned["test"]["session"]["streak"] == 0

    store = CandyStore(tmp_path / "candy_hunt.json")
    store.state["streak"] = 6
    store.save()
    assert CandyStore(tmp_path / "candy_hunt.json").state["streak"] == 6


def test_expiry_keeps_the_two_streaks_apart(tmp_path):
    async def run():
        cog, _bot = _cog(tmp_path)
        session = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=SUPPORT_TEST_CHANNEL_ID,
            minutes=15,
            interval=2,
            now=NOW,
            source="command",
        )
        session["streak"] = 2
        cog.store.state["streak"] = 4
        cog.store.state["test"]["session"] = session
        cog.store.state["active_drops"] = [_active_drop("aaaa1111", expires_at=NOW)]
        await cog.expire_drops(NOW)
        assert cog.store.state["streak"] == 0
        assert cog.store.state["test"]["session"]["streak"] == 2
        assert cog.store.state["active_drops"] == []

        cog.store.state["streak"] = 4
        cog.store.state["test"]["session"]["streak"] = 2
        cog.store.state["active_drops"] = [_active_drop("bbbb2222", dry_run=True, expires_at=NOW)]
        await cog.expire_drops(NOW)
        assert cog.store.state["streak"] == 4
        assert cog.store.state["test"]["session"]["streak"] == 0

        # A claimed drop cleaned up later must not wipe the streak it just earned.
        cog.store.state["streak"] = 2
        claimed = _active_drop("cccc3333")
        claimed["status"] = "claimed"
        cog.store.state["active_drops"] = [claimed]
        await cog.expire_drops(NOW)
        assert cog.store.state["streak"] == 2

    asyncio.run(run())


def test_maybe_drop_uses_the_shrunk_interval(tmp_path):
    now = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    low, _high = effective_interval(CANDY_HUNT, 2, now)

    async def run():
        cog, _bot = _cog(tmp_path, rng=ScriptedRng(uniforms=[low.total_seconds()]))
        cog.store.state["streak"] = 2
        cog.store.state["next_drop_at"] = iso(now - timedelta(minutes=1))
        await cog.maybe_drop(now)
        assert parse_dt(cog.store.state["next_drop_at"]) == as_utc(now) + low
        await cog.maybe_drop(now)
        assert parse_dt(cog.store.state["next_drop_at"]) == as_utc(now) + low

    asyncio.run(run())


def test_test_session_streak_floors_delay_and_pulls_earlier(tmp_path):
    assert jittered_delay_seconds(2, _LowRng(), factor=0.6**2) == 60
    assert jittered_delay_seconds(0.72, _LowRng()) == 60
    stream = random.Random(1)
    samples = [jittered_delay_seconds(2, stream, factor=0.6**2) for _ in range(40)]
    assert all(60 <= sample <= 72 for sample in samples)
    assert max(samples) > 60

    now = NOW
    posted = now - timedelta(seconds=20)
    existing = now + timedelta(minutes=5)
    session = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=now,
        source="command",
    )
    session["streak"] = 1
    session["next_drop_at"] = iso(existing)
    state = default_state()
    state["streak"] = 5
    saved_next = iso(now + timedelta(hours=2))
    state["next_drop_at"] = saved_next
    state["test"]["session"] = session
    state["active_drops"] = [make_drop("abcd1234", 9, 8, posted, now + timedelta(minutes=2), dry_run=True)]
    updated, outcome = apply_claim(
        state,
        "abcd1234",
        3,
        now,
        ScriptedRng(randoms=[0.9, 0.0, 1.0], uniforms=[48.0]),
        is_bot=False,
        trick_chance=0,
        settings=CANDY_HUNT,
    )
    assert outcome.dry_run and outcome.status == "won"
    assert updated["streak"] == 5
    assert updated["next_drop_at"] == saved_next
    assert updated["test"]["session"]["streak"] == 2
    nxt = parse_dt(updated["test"]["session"]["next_drop_at"])
    assert as_utc(nxt) == as_utc(posted) + timedelta(seconds=60)
    assert as_utc(now) + timedelta(seconds=30) <= as_utc(nxt) < as_utc(existing)
    ends = parse_dt(updated["test"]["session"]["ends_at"])
    assert as_utc(nxt) < as_utc(ends)

    later = default_state()
    later["streak"] = 5
    later["next_drop_at"] = saved_next
    held = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=now,
        source="command",
    )
    held["next_drop_at"] = iso(now + timedelta(seconds=35))
    later["test"]["session"] = held
    later["active_drops"] = [make_drop("bbbb2222", 9, 8, posted, now + timedelta(minutes=2), dry_run=True)]
    updated, _outcome = apply_claim(
        later,
        "bbbb2222",
        3,
        now,
        ScriptedRng(randoms=[0.9, 0.0, 1.0], uniforms=[600.0]),
        is_bot=False,
        trick_chance=0,
        settings=CANDY_HUNT,
    )
    assert updated["test"]["session"]["streak"] == 1
    assert parse_dt(updated["test"]["session"]["next_drop_at"]) == as_utc(now) + timedelta(seconds=35)
    assert updated["streak"] == 5

    quiet = default_state()
    quiet["streak"] = 5
    quiet_session = dict(held)
    quiet_session["streak"] = 4
    quiet_session["next_drop_at"] = None
    quiet["test"]["session"] = quiet_session
    quiet["active_drops"] = [make_drop("cccc3333", 9, 8, posted, now + timedelta(minutes=2), dry_run=True)]
    updated, _outcome = apply_claim(
        quiet,
        "cccc3333",
        3,
        now,
        ScriptedRng(randoms=[0.9, 0.0, 1.0]),
        is_bot=False,
        trick_chance=0,
    )
    assert updated["test"]["session"]["next_drop_at"] is None
    assert updated["test"]["session"]["streak"] == 5
    assert updated["streak"] == 5

    melted = default_state()
    melted["streak"] = 4
    melted_session = dict(session)
    melted_session["streak"] = 3
    melted["test"]["session"] = melted_session
    melted["active_drops"] = [make_drop("dddd4444", 9, 8, posted, now, dry_run=True)]
    updated, outcome = apply_claim(
        melted,
        "dddd4444",
        3,
        now,
        ScriptedRng(),
        is_bot=False,
        trick_chance=0,
    )
    assert outcome.status == "expired"
    assert updated["streak"] == 4
    assert updated["test"]["session"]["streak"] == 0

    async def run():
        channel = FakeTextChannel(SUPPORT_TEST_CHANNEL_ID, "test-channel")
        channel.guild = SimpleNamespace(id=SUPPORT_GUILD_ID)
        bot = FakeBot()
        bot.channels[channel.id] = channel
        cog, _bot = _cog(tmp_path, bot=bot, rng=_LowRng())
        running = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=channel.id,
            minutes=15,
            interval=2,
            now=now - timedelta(minutes=1),
            source="command",
        )
        running["streak"] = 2
        running["next_drop_at"] = iso(now)
        cog.store.state["test"]["session"] = running
        cog.store.state["streak"] = 9
        await cog.maybe_test_session(now)
        assert parse_dt(cog.store.state["test"]["session"]["next_drop_at"]) == as_utc(now) + timedelta(seconds=60)
        assert cog.store.state["streak"] == 9
        posted_drop = next(drop for drop in cog.store.state["active_drops"] if drop["dry_run"])
        assert parse_dt(posted_drop["expires_at"]) - as_utc(now) == timedelta(minutes=2)
        text = render_status(cog.store.state, CANDY_HUNT, now)
        assert "streak=2" in text

    asyncio.run(run())


def test_pacing_config_keys_ignore_bad_values(caplog):
    updated = apply_candy_hunt_overrides(
        CANDY_HUNT,
        {
            "streak_factor": 0.5,
            "streak_floor_low_minutes": 10,
            "streak_floor_high_minutes": 14,
            "late_ramp_start": "19:00",
            "late_ramp_full": "22:30",
            "late_ramp_factor": 0.4,
        },
    )
    assert updated.streak_factor == 0.5
    assert updated.streak_floor_low == timedelta(minutes=10)
    assert updated.streak_floor_high == timedelta(minutes=14)
    assert updated.late_ramp_start_minute == 19 * 60
    assert updated.late_ramp_full_minute == 22 * 60 + 30
    assert updated.late_ramp_factor == 0.4

    with caplog.at_level(logging.WARNING, logger="cogs.candyhunt"):
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"streak_factor": 0}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"streak_factor": 2}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_factor": "nope"}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"streak_floor_low_minutes": 0}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_start": "25:99"}) is CANDY_HUNT
        assert (
            apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_start": "23:00", "late_ramp_full": "20:00"})
            is CANDY_HUNT
        )
    assert any("streak_factor" in record.message for record in caplog.records)


def test_overnight_window_stays_open_until_six_am():
    start, end = window_bounds(CANDY_HUNT)
    assert is_in_drop_window(CANDY_HUNT, datetime(2026, 10, 31, 23, 59, tzinfo=PT))
    assert is_in_drop_window(CANDY_HUNT, datetime(2026, 11, 1, 0, 30, tzinfo=PT))
    assert is_in_drop_window(CANDY_HUNT, datetime(2026, 11, 1, 5, 59, tzinfo=PT))
    assert not is_in_drop_window(CANDY_HUNT, datetime(2026, 11, 1, 6, 0, tzinfo=PT))
    assert not is_in_drop_window(CANDY_HUNT, datetime(2026, 11, 1, 10, 0, tzinfo=PT))
    assert not is_in_drop_window(CANDY_HUNT, datetime(2026, 10, 31, 9, 59, tzinfo=PT))

    # A spring-forward gap is stored as the real later instant, not a phantom 2:30.
    gap = replace(
        CANDY_HUNT,
        event_date=date(2026, 3, 8),
        window_start_minute=0,
        window_end_minute=2 * 60 + 30,
    )
    _gap_start, gap_end = window_bounds(gap)
    assert (gap_end.hour, gap_end.minute) == (3, 30)
    assert gap_end.utcoffset() == timedelta(hours=-7)

    evening = datetime(2026, 10, 31, 23, 50, tzinfo=PT)
    scheduled = resolve_schedule(
        now=evening,
        persisted=None,
        window_start=start,
        window_end=end,
        min_interval=CANDY_HUNT.min_interval,
        max_interval=CANDY_HUNT.max_interval,
        rng=ScriptedRng(uniforms=[15 * 60]),
        settings=CANDY_HUNT,
        streak=0,
    )
    assert scheduled.action == "wait"
    landed = scheduled.next_drop_at.astimezone(PT)
    assert landed == datetime(2026, 11, 1, 0, 5, tzinfo=PT)
    assert as_utc(landed) < as_utc(end)

    resumed = resolve_schedule(
        now=datetime(2026, 11, 1, 3, 0, tzinfo=PT),
        persisted=datetime(2026, 11, 1, 4, 0, tzinfo=PT),
        window_start=start,
        window_end=end,
        min_interval=CANDY_HUNT.min_interval,
        max_interval=CANDY_HUNT.max_interval,
        rng=ScriptedRng(),
        settings=CANDY_HUNT,
        streak=0,
    )
    assert resumed.action == "wait"
    assert resumed.next_drop_at.astimezone(PT) == datetime(2026, 11, 1, 4, 0, tzinfo=PT)

    closed = resolve_schedule(
        now=end,
        persisted=datetime(2026, 11, 1, 5, 0, tzinfo=PT),
        window_start=start,
        window_end=end,
        min_interval=timedelta(minutes=1),
        max_interval=timedelta(minutes=1),
        rng=ScriptedRng(),
        settings=CANDY_HUNT,
    )
    assert closed.action == "idle"
    assert closed.next_drop_at is None

    for seed in range(20):
        nxt = compute_next_drop_at(
            datetime(2026, 11, 1, 4, 0, tzinfo=PT),
            window_end=end,
            min_interval=timedelta(minutes=1),
            max_interval=timedelta(minutes=30),
            rng=random.Random(seed),
            first=False,
        )
        assert nxt is not None
        assert nxt.astimezone(PT).date() == date(2026, 11, 1)
        assert as_utc(nxt) < as_utc(end)
    assert (
        compute_next_drop_at(
            end - timedelta(seconds=30),
            window_end=end,
            min_interval=timedelta(minutes=1),
            max_interval=timedelta(minutes=1),
            rng=random.Random(0),
            first=False,
        )
        is None
    )


def test_late_floor_reaches_one_minute_and_claim_keeps_a_minute_lead():
    # A 22:00 claim with an exhausted scripted random() returns 0.0, which would
    # start a frenzy and overwrite next_drop_at. Chance 0 keeps the gap math.
    quiet_frenzy = replace(CANDY_HUNT, frenzy_chance=0.0)
    assert effective_interval(CANDY_HUNT, 12, _pt(17, 30)) == (
        timedelta(minutes=12),
        timedelta(minutes=15),
    )
    for moment in (_pt(18, 0), _pt(21, 0), datetime(2026, 11, 1, 2, 0, tzinfo=PT)):
        assert effective_interval(CANDY_HUNT, 12, moment) == (
            timedelta(minutes=1),
            timedelta(minutes=1),
        )

    # After 21:00 the ramp is x0.5, so 7 claims reach 1 minute on both ends.
    night = _pt(22, 0)
    low6, high6 = effective_interval(CANDY_HUNT, 6, night)
    low7, high7 = effective_interval(CANDY_HUNT, 7, night)
    assert low6 == timedelta(minutes=1)
    assert high6 > timedelta(minutes=1)
    assert (low7, high7) == (timedelta(minutes=1), timedelta(minutes=1))

    capped = replace(
        CANDY_HUNT,
        min_interval=timedelta(minutes=1),
        max_interval=timedelta(minutes=2),
        late_floor=timedelta(minutes=5),
    )
    assert effective_interval(capped, 8, _pt(22, 0)) == (timedelta(minutes=1), timedelta(minutes=2))

    afternoon = _pt(14, 0)
    assert real_drop_lifetime(CANDY_HUNT, 0, afternoon) == timedelta(minutes=10)
    assert real_drop_lifetime(CANDY_HUNT, 3, _pt(22, 0)) == timedelta(minutes=10)
    five = real_drop_lifetime(CANDY_HUNT, 5, _pt(22, 0))
    assert five.total_seconds() / 60 == pytest.approx(2 * 60 * 0.5 * (0.6**5))
    assert timedelta(minutes=4) < five < timedelta(minutes=5)
    assert real_drop_lifetime(CANDY_HUNT, 8, _pt(22, 0)) == timedelta(minutes=2)

    now = _pt(22, 0)
    posted = now - timedelta(seconds=10)
    existing = now + timedelta(minutes=30)
    state = default_state()
    state["streak"] = 7
    state["next_drop_at"] = iso(existing)
    state["active_drops"] = [make_drop("abcd1234", 55, 66, posted, now + timedelta(minutes=2), dry_run=False)]
    updated, outcome = apply_claim(
        state,
        "abcd1234",
        7,
        now,
        ScriptedRng(randoms=[0.9, 0.0], uniforms=[60.0]),
        is_bot=False,
        trick_chance=0,
        settings=quiet_frenzy,
    )
    assert outcome.status == "won"
    assert updated["streak"] == 8
    nxt = parse_dt(updated["next_drop_at"])
    assert as_utc(nxt) == as_utc(now) + timedelta(seconds=60)
    assert as_utc(nxt) < as_utc(existing)

    state["active_drops"] = [make_drop("bbbb2222", 55, 66, posted, now + timedelta(minutes=2), dry_run=False)]
    state["streak"] = 7
    state["next_drop_at"] = iso(now + timedelta(seconds=20))
    updated, _outcome = apply_claim(
        state,
        "bbbb2222",
        7,
        now,
        ScriptedRng(randoms=[0.9, 0.0], uniforms=[1.0]),
        is_bot=False,
        trick_chance=0,
        settings=quiet_frenzy,
    )
    assert parse_dt(updated["next_drop_at"]) == as_utc(now) + timedelta(seconds=60)


def test_scheduled_real_drop_lifetime_shrinks_and_plain_post_drop_does_not(tmp_path):
    async def run():
        channel = FakeTextChannel(42, "lounge")
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[channel],
            me=SimpleNamespace(id=1),
            default_role=SimpleNamespace(id=0),
            rules_channel=None,
            members=[],
        )
        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = guild
        night = _pt(22, 0)
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(uniforms=[60.0]))
        cog.store.state["streak"] = 8
        cog.store.state["next_drop_at"] = iso(night)
        await cog.maybe_drop(night)
        real = next(drop for drop in cog.store.state["active_drops"] if not drop["dry_run"])
        assert parse_dt(real["expires_at"]) - as_utc(night) == timedelta(minutes=2)

        almost = datetime(2026, 11, 1, 5, 59, tzinfo=PT)
        cog.store.state["streak"] = 8
        cog.store.state["next_drop_at"] = iso(almost)
        await cog.maybe_drop(almost)
        late = next(drop for drop in cog.store.state["active_drops"] if drop["created_at"] == iso(almost))
        _start, end = window_bounds(CANDY_HUNT)
        expires = parse_dt(late["expires_at"])
        assert expires == as_utc(almost) + timedelta(minutes=2)
        assert expires > as_utc(end)
        assert cog.store.state["next_drop_at"] is None

        before = len(cog.store.state["active_drops"])
        await cog.post_drop(channel, dry_run=False, now=night)
        posted = cog.store.state["active_drops"][-1]
        assert len(cog.store.state["active_drops"]) == before + 1
        assert parse_dt(posted["expires_at"]) - as_utc(night) == CANDY_HUNT.drop_expiry

    asyncio.run(run())


def test_wrap_up_counts_candy_claimed_through_and_after_six_am(tmp_path):
    async def run():
        channel = FakeTextChannel(8, "announcements")
        member = SimpleNamespace(id=7, add_roles=AsyncMock())

        class Guild:
            id = MTS_GUILD_ID
            roles = []
            text_channels = [channel]

            def get_member(self, user_id):
                return member if int(user_id) == 7 else None

            async def fetch_member(self, user_id):
                return member

            async def create_role(self, **kwargs):
                role = SimpleNamespace(id=77, name=kwargs["name"])
                self.roles.append(role)
                return role

        bot = FakeBot()
        bot.guilds[MTS_GUILD_ID] = Guild()
        cog, _bot = _cog(tmp_path, bot=bot, rng=ScriptedRng(randoms=[0.9, 0.0, 0.9, 0.0]))
        claim_at = datetime(2026, 11, 1, 5, 30, tzinfo=PT)
        assert is_in_drop_window(CANDY_HUNT, claim_at)
        assert CANDY_HUNT.wrap_up_at.astimezone(PT) == datetime(2026, 11, 1, 10, 0, tzinfo=PT)
        cog.store.state["active_drops"] = [
            make_drop(
                "aaaa1111",
                1,
                2,
                claim_at - timedelta(minutes=1),
                claim_at + timedelta(minutes=5),
                dry_run=False,
            )
        ]
        cog.store.state["next_drop_at"] = iso(claim_at + timedelta(minutes=20))
        first = await cog.store.claim(
            "aaaa1111",
            7,
            claim_at,
            cog.rng,
            is_bot=False,
            trick_chance=0,
            settings=CANDY_HUNT,
        )
        assert first.status == "won"
        assert cog.store.state["totals"]["7"] > 0

        posted = datetime(2026, 11, 1, 5, 59, tzinfo=PT)
        later = datetime(2026, 11, 1, 6, 0, 30, tzinfo=PT)
        assert not is_in_drop_window(CANDY_HUNT, later)
        cog.store.state["active_drops"] = [
            make_drop("bbbb2222", 1, 3, posted, posted + timedelta(minutes=2), dry_run=False)
        ]
        second = await cog.store.claim(
            "bbbb2222",
            7,
            later,
            cog.rng,
            is_bot=False,
            trick_chance=0,
            settings=CANDY_HUNT,
        )
        assert second.status == "won"
        assert cog.store.state["claims_total"] == 2
        total = cog.store.state["totals"]["7"]

        await cog.maybe_wrap_up(claim_at)
        assert cog.store.state["wrap_up_done"] is False
        await cog.maybe_wrap_up(CANDY_HUNT.wrap_up_at - timedelta(seconds=1))
        assert cog.store.state["wrap_up_done"] is False
        await cog.maybe_wrap_up(CANDY_HUNT.wrap_up_at)
        assert cog.store.state["wrap_up_done"] is True
        assert channel.sent
        text = component_text(channel.sent[0]["kwargs"]["view"])
        assert "<@" not in text
        assert "Pumpkin God" in text
        assert f"**{total}**" in text
        assert "Claims: **2**" in text

    asyncio.run(run())


def test_clock_config_accepts_an_overnight_end_and_old_state(tmp_path, caplog):
    classic = apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "24:00"})
    assert classic.window_end_minute == 24 * 60
    _start, classic_end = window_bounds(classic)
    assert classic_end.astimezone(PT) == datetime(2026, 11, 1, 0, 0, tzinfo=PT)
    assert not is_in_drop_window(classic, datetime(2026, 11, 1, 0, 30, tzinfo=PT))

    overnight = apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "30:00"})
    assert overnight.window_end_minute == 30 * 60
    assert window_bounds(overnight)[1].astimezone(PT) == datetime(2026, 11, 1, 6, 0, tzinfo=PT)

    extended = apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_full": "26:00"})
    assert extended.late_ramp_full_minute == 26 * 60
    one_am = datetime(2026, 11, 1, 1, 0, tzinfo=PT)
    assert 0.5 < late_night_factor(extended, one_am) < 1.0
    assert late_night_factor(extended, datetime(2026, 11, 1, 2, 0, tzinfo=PT)) == pytest.approx(0.5)

    floored = apply_candy_hunt_overrides(CANDY_HUNT, {"late_floor_minutes": 4})
    assert floored.late_floor == timedelta(minutes=4)
    at_end = apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_full": "30:00"})
    assert at_end.late_ramp_full_minute == 30 * 60

    with caplog.at_level(logging.WARNING, logger="cogs.candyhunt"):
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "48:01"}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "25:60"}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "nope"}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"late_ramp_full": "31:00"}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"late_floor_minutes": 0}) is CANDY_HUNT
        assert apply_candy_hunt_overrides(CANDY_HUNT, {"late_floor_minutes": 61}) is CANDY_HUNT
        early = apply_candy_hunt_overrides(CANDY_HUNT, {"wrap_up_at": "2026-11-01T04:00:00"})
        far = apply_candy_hunt_overrides(CANDY_HUNT, {"window_end": "48:00"})
    assert early.wrap_up_at.astimezone(PT).hour == 4
    assert far.window_end_minute == 48 * 60
    assert any("wrap_up_at" in record.message and "before the window end" in record.message for record in caplog.records)

    path = tmp_path / "candy_hunt.json"
    path.write_text(json.dumps({"totals": {"4": 2}, "drops_posted": 1}), encoding="utf-8")
    loaded = CandyStore(path)
    assert loaded.state["totals"] == {"4": 2}
    assert loaded.state["drops_posted"] == 1
    assert loaded.state["streak"] == 0
    assert loaded.state["next_drop_at"] is None
    assert loaded.state["active_drops"] == []


def test_flavor_lines_rotate_and_ambient_stays_on_treats():
    originals = {
        "Candy Corn": "One sticky kernel. The porch light hums.",
        "Lollipop": "Cherry. Or possibly blood. It's cherry.",
        "Chocolate Bar": "A full bar. You are the main character tonight.",
        "Gummy Worms": "They wiggle. That's normal. Probably.",
        "Caramel Apple": "Sticky, shiny, and slightly cursed.",
        "Full-Size Snickers": "The holy grail of the suburban porch.",
        "Pumpkin Jackpot": "The pumpkin splits open and candy rains out.",
    }
    trick_originals = (
        "A porch ghost swipes one piece and cackles.",
        "Raisins. The ancient betrayal. Two pieces walk off in shame.",
        "Your bucket has a hole. One piece hits the sidewalk.",
    )
    assert [treat.blurb for treat in TREATS] == list(originals.values())
    for treat in TREATS:
        assert 4 <= len(treat.blurbs) <= 5
        assert originals[treat.name] in treat.blurbs
        assert treat.blurb == treat.blurbs[0]
    assert len({line for treat in TREATS for line in treat.blurbs}) == sum(len(treat.blurbs) for treat in TREATS)
    for trick, original in zip(TRICKS, trick_originals):
        assert original in trick.blurbs
        if trick.amount == -2:
            assert all("two" in line.casefold() for line in trick.blurbs)
        if trick.amount == -1:
            assert all("one" in line.casefold() for line in trick.blurbs)
    for trick in TRICKS:
        assert 2 <= len(trick.blurbs) <= 3
        assert trick.blurb == trick.blurbs[0]
    assert len({line for trick in TRICKS for line in trick.blurbs}) == sum(len(trick.blurbs) for trick in TRICKS)
    assert len(AMBIENT_EXTRAS) >= 6
    assert AMBIENT_CHANCE == 0.05
    corn = next(treat for treat in TREATS if treat.name == "Candy Corn")
    assert "One sticky kernel. The porch light hums." in corn.blurbs

    seen = {treat.name: set() for treat in TREATS}
    ambient = 0
    porch = "One sticky kernel. The porch light hums."
    hum = "Somewhere, a porch light hums."
    seeded = roll_candy(random.Random(4), trick_chance=0)
    again = roll_candy(random.Random(4), trick_chance=0)
    assert seeded.blurb == again.blurb
    treat = next(item for item in TREATS if item.name == seeded.name)
    base, _had = _split_ambient(seeded.blurb, treat.blurbs)
    assert base in treat.blurbs
    card = ClaimOutcome(
        status="won",
        mutated=True,
        roll=seeded,
        total=seeded.amount,
        rank=1,
        players=1,
    )
    assert seeded.blurb in plain_candy_text(card)
    assert seeded.blurb in component_text(build_candy_card(card))

    rolls = 4000
    for seed in range(rolls):
        roll = roll_candy(random.Random(seed), trick_chance=0)
        lines = next(item.blurbs for item in TREATS if item.name == roll.name)
        base, had = _split_ambient(roll.blurb, lines)
        assert base in lines
        seen[roll.name].add(base)
        if had:
            ambient += 1
        if roll.blurb.startswith(porch):
            assert hum not in roll.blurb
    assert all(seen[treat.name] == set(treat.blurbs) for treat in TREATS)
    rate = ambient / rolls
    assert 0.03 <= rate <= 0.08

    trick_seen = {trick.blurbs: set() for trick in TRICKS}
    for seed in range(2000):
        roll = roll_candy(random.Random(seed), trick_chance=1)
        assert roll.kind == "trick"
        matched = next(trick for trick in TRICKS if roll.blurb in trick.blurbs)
        assert roll.amount == matched.amount
        trick_seen[matched.blurbs].add(roll.blurb)
        assert _split_ambient(roll.blurb, matched.blurbs) == (roll.blurb, False)
    assert all(trick_seen[trick.blurbs] == set(trick.blurbs) for trick in TRICKS)


def test_winner_announcement_adds_one_decoration_line():
    view = build_winner_view(
        top=[("5", 3), ("6", 1)],
        drops_posted=4,
        claims_total=2,
        pumpkin_days=7,
    )
    text = component_text(view)
    assert WINNER_DECORATION_LINE == (
        "The #1 winner also gets a gifted Discord profile decoration from Nick, coming soon."
    )
    assert text.count(WINNER_DECORATION_LINE) == 1
    assert f"The Pumpkin God reigns for 7 days.\n{WINNER_DECORATION_LINE}" in text

    quiet = component_text(
        build_winner_view(top=[], drops_posted=0, claims_total=0, pumpkin_days=7)
    )
    assert WINNER_DECORATION_LINE not in quiet
    assert "the ghosts kept the bowl" in quiet.casefold()
    assert CANDY_HUNT.pumpkin_god_role_name == "Pumpkin God"

    reminder = owner_decoration_dm(
        display_name="Candy Champ",
        username="champ",
        user_id=5,
        candy=3,
    )
    assert reminder == (
        "Display name: Candy Champ\n"
        "Username: champ\n"
        "User ID: 5\n"
        "Candy: 3\n"
        "Reminder: gift them a profile decoration from the Discord Shop."
    )
    assert owner_decoration_dm(
        display_name="Candy Champ",
        username="champ",
        user_id=5,
        candy=3,
        dry_run=True,
    ) == f"[DRY RUN] {reminder}"


def _wrap_stage(tmp_path, name, *, owner_id, winner_id, candy, send=None):
    """MTS guild, owner user, and one winner for a wrap-up DM test."""
    member = SimpleNamespace(
        id=winner_id or 0,
        display_name="Candy Champ",
        name="champ",
        add_roles=AsyncMock(),
    )
    owner = SimpleNamespace(id=owner_id, send=send if send is not None else AsyncMock())
    channel = FakeTextChannel(8, "announcements")
    lookups: list[int] = []

    class Guild:
        id = MTS_GUILD_ID

        def __init__(self):
            self.roles = []
            self.text_channels = [channel]

        def get_member(self, user_id):
            if winner_id is None or int(user_id) != int(winner_id):
                return None
            return member

        async def fetch_member(self, user_id):
            found = self.get_member(user_id)
            if found is None:
                raise discord.NotFound(_HTTPResponse(), "missing")
            return found

        async def create_role(self, **kwargs):
            role = SimpleNamespace(id=77, name=kwargs["name"])
            self.roles.append(role)
            return role

    guild = Guild()
    bot = FakeBot()
    bot.guilds[MTS_GUILD_ID] = guild

    def get_user(user_id):
        lookups.append(int(user_id))
        if int(user_id) == int(owner_id):
            return owner
        return None

    async def fetch_user(user_id):
        lookups.append(int(user_id))
        if int(user_id) == int(owner_id):
            return owner
        raise discord.NotFound(_HTTPResponse(), "missing")

    bot.get_user = get_user
    bot.fetch_user = fetch_user
    folder = tmp_path / name
    folder.mkdir()
    cog, _bot = _cog(folder, bot=bot)
    cog.settings = replace(CANDY_HUNT, owner_id=owner_id)
    if winner_id is None:
        cog.store.state["totals"] = {}
        cog.store.state["claims_total"] = 0
    else:
        cog.store.state["totals"] = {str(winner_id): candy}
        cog.store.state["claims_total"] = 2
    cog.store.state["drops_posted"] = 3
    return SimpleNamespace(
        cog=cog,
        channel=channel,
        owner=owner,
        member=member,
        lookups=lookups,
        get_user=get_user,
        fetch_user=fetch_user,
    )


def test_wrap_up_dry_run_and_real_dm_use_the_candyadmin_owner(tmp_path):
    async def run():
        owner_id = 424242
        winner_id = 7
        candy = 12
        stage = _wrap_stage(
            tmp_path,
            "dm",
            owner_id=owner_id,
            winner_id=winner_id,
            candy=candy,
        )
        assert owner_id != NICK_ID
        assert stage.cog.settings.owner_id == owner_id

        preview_ctx = _ctx(stage.cog, owner_id)
        await stage.cog.admin_wrapup.callback(stage.cog, preview_ctx, "true")
        preview = component_text(preview_ctx.reply.await_args.kwargs["view"])
        assert WINNER_DECORATION_LINE in preview
        assert "The Pumpkin God reigns for 7 days." in preview
        assert "[DRY RUN]" not in preview
        assert "PREVIEW" in preview
        assert stage.channel.sent == []
        stage.member.add_roles.assert_not_awaited()
        assert stage.cog.store.state["wrap_up_done"] is False
        dry_text = stage.owner.send.await_args.args[0]
        assert dry_text == owner_decoration_dm(
            display_name="Candy Champ",
            username="champ",
            user_id=winner_id,
            candy=candy,
            dry_run=True,
        )
        assert dry_text.startswith("[DRY RUN] ")
        assert stage.lookups == [owner_id]

        stage.owner.send.reset_mock()
        stage.lookups.clear()
        stage.cog.bot.get_user = lambda user_id: None
        done = _ctx(stage.cog, owner_id)
        await stage.cog.admin_wrapup.callback(stage.cog, done, "false")
        message, kwargs = _replied(done)
        assert kwargs["mention_author"] is False
        assert "irreversible" in message.casefold()
        posted = component_text(stage.channel.sent[0]["kwargs"]["view"])
        assert WINNER_DECORATION_LINE in posted
        assert "The Pumpkin God reigns for 7 days." in posted
        assert stage.cog.store.state["wrap_up_done"] is True
        assert stage.cog.store.state["pumpkin_god"]["user_id"] == winner_id
        role = stage.member.add_roles.await_args.args[0]
        assert role.name == "Pumpkin God"
        assert stage.member.add_roles.await_args.kwargs["reason"] == "Candy hunt Pumpkin God"
        real_text = stage.owner.send.await_args.args[0]
        assert real_text == owner_decoration_dm(
            display_name="Candy Champ",
            username="champ",
            user_id=winner_id,
            candy=candy,
        )
        assert not real_text.startswith("[DRY RUN]")
        assert "Discord Shop" in real_text
        assert stage.lookups == [owner_id]
        assert NICK_ID not in stage.lookups

        quiet = _wrap_stage(
            tmp_path,
            "quiet",
            owner_id=owner_id,
            winner_id=None,
            candy=0,
        )
        dry_quiet = _ctx(quiet.cog, owner_id)
        await quiet.cog.admin_wrapup.callback(quiet.cog, dry_quiet, "true")
        quiet_preview = component_text(dry_quiet.reply.await_args.kwargs["view"])
        assert "the ghosts kept the bowl" in quiet_preview.casefold()
        assert WINNER_DECORATION_LINE not in quiet_preview
        quiet.owner.send.assert_not_awaited()

        real_quiet = _ctx(quiet.cog, owner_id)
        await quiet.cog.admin_wrapup.callback(quiet.cog, real_quiet, "false")
        assert quiet.cog.store.state["wrap_up_done"] is True
        quiet_posted = component_text(quiet.channel.sent[0]["kwargs"]["view"])
        assert "the ghosts kept the bowl" in quiet_posted.casefold()
        quiet.owner.send.assert_not_awaited()
        quiet.member.add_roles.assert_not_awaited()

    asyncio.run(run())


def test_wrap_up_owner_dm_failure_is_logged_and_wrap_up_still_posts(tmp_path, caplog):
    async def run():
        owner_id = 424242
        winner_id = 7
        forbidden = _wrap_stage(
            tmp_path,
            "forbidden",
            owner_id=owner_id,
            winner_id=winner_id,
            candy=12,
            send=AsyncMock(side_effect=discord.Forbidden(_HTTPResponse(), "closed")),
        )
        failed_preview = _ctx(forbidden.cog, owner_id)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await forbidden.cog.admin_wrapup.callback(forbidden.cog, failed_preview, "true")
        preview = component_text(failed_preview.reply.await_args.kwargs["view"])
        assert WINNER_DECORATION_LINE in preview
        assert forbidden.cog.store.state["wrap_up_done"] is False
        assert any(
            "candy wrapup owner dm failed error=Forbidden" in record.message
            for record in caplog.records
        )

        caplog.clear()
        crashed = _wrap_stage(
            tmp_path,
            "crashed",
            owner_id=owner_id,
            winner_id=winner_id,
            candy=12,
            send=AsyncMock(side_effect=RuntimeError("dm down")),
        )
        done = _ctx(crashed.cog, owner_id)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await crashed.cog.admin_wrapup.callback(crashed.cog, done, "false")
        message, _kwargs = _replied(done)
        assert "irreversible" in message.casefold()
        assert crashed.cog.store.state["wrap_up_done"] is True
        posted = component_text(crashed.channel.sent[0]["kwargs"]["view"])
        assert WINNER_DECORATION_LINE in posted
        assert "The Pumpkin God reigns for 7 days." in posted
        assert crashed.cog.store.state["pumpkin_god"]["user_id"] == winner_id
        assert crashed.member.add_roles.await_args.args[0].name == "Pumpkin God"
        assert any(
            "candy wrapup owner dm failed error=RuntimeError" in record.message
            for record in caplog.records
        )

        caplog.clear()
        unposted = _wrap_stage(
            tmp_path,
            "unposted",
            owner_id=owner_id,
            winner_id=winner_id,
            candy=12,
        )
        unposted.channel.error = _http_error()
        blocked = _ctx(unposted.cog, owner_id)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await unposted.cog.admin_wrapup.callback(unposted.cog, blocked, "false")
        text, _kwargs = _replied(blocked)
        assert "not marked done" in text
        assert unposted.cog.store.state["wrap_up_done"] is False
        unposted.owner.send.assert_not_awaited()
        assert not any("candy wrapup owner dm" in record.message for record in caplog.records)

    asyncio.run(run())


def _claim_state(user_id: int, total: int, *, dry_run: bool = False, drop_id: str = "abcd1234") -> dict:
    state = default_state()
    state["active_drops"] = [_active_drop(drop_id, dry_run=dry_run)]
    bucket = state["test"] if dry_run else state
    if total:
        bucket["totals"][str(user_id)] = total
    return state


def _claim(state, user_id, rng, drop_id="abcd1234"):
    return apply_claim(
        state,
        drop_id,
        user_id,
        NOW,
        rng,
        is_bot=False,
        trick_chance=1.0 if rng.randoms and rng.randoms[0] < 0.5 else 0.0,
    )


def test_claim_public_text_uses_the_floored_trick_and_a_mention_free_name():
    user = SimpleNamespace(display_name="<@99> @everyone Ada", id=7, mention="<@7>")
    assert public_display_name(user) == "Ada"
    assert "<@" not in public_display_name(user)
    assert "@everyone" not in public_display_name(user)

    treat_state = _claim_state(7, 0)
    untouched = treat_state["totals"].get("7")
    _new_state, treat = _claim(treat_state, 7, ScriptedRng(randoms=[0.9, 0.0]))
    assert treat_state["totals"].get("7") == untouched
    assert treat.status == "won"
    assert treat.roll.amount == 1
    assert treat.total == 1
    assert treat.removed == 0
    treat_text = claim_public_text(treat, public_display_name(user))
    assert treat_text == f"🍬 Claimed by Ada for 1 candy · {CANDY_TYPES[0].emoji} {CANDY_TYPES[0].name}"
    assert "<@" not in treat_text and "@everyone" not in treat_text
    board = build_leaderboard_view(_new_state["totals"], 7)
    assert "1" in component_text(board)

    raisins = next(item for item in TRICKS if item.amount == -2)
    small = _claim_state(7, 1)
    _after, lost_some = _claim(small, 7, ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)]))
    assert lost_some.roll.amount == -2
    assert lost_some.removed == 1
    assert lost_some.total == 0
    assert small["totals"]["7"] == 1
    assert claim_public_text(lost_some, "Ada") == "🎃 TRICK! Ada lost 1 candy"

    rich = _claim_state(7, 5)
    _after, lost_two = _claim(rich, 7, ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)]))
    assert lost_two.removed == 2
    assert lost_two.total == 3
    assert claim_public_text(lost_two, "Ada") == "🎃 TRICK! Ada lost 2 candy"

    empty = _claim_state(7, 0)
    _after, lost_none = _claim(empty, 7, ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)]))
    assert lost_none.removed == 0
    assert lost_none.total == 0
    assert claim_public_text(lost_none, "Ada") == "🎃 TRICK! Ada lost 0 candy"


    dry_state = _claim_state(7, 0, dry_run=True)
    dry_new, dry = _claim(dry_state, 7, ScriptedRng(randoms=[0.9, 0.0]))
    assert claim_public_text(dry, "Ada").startswith("TEST. ")
    assert claim_public_text(dry, "Ada") == (
        f"TEST. 🍬 Claimed by Ada for 1 candy · {CANDY_TYPES[0].emoji} {CANDY_TYPES[0].name}"
    )
    assert dry_new["test"]["totals"]["7"] == 1
    assert dry_new["test"]["claims_total"] == 1
    assert "7" not in dry_new["totals"]
    assert dry_state["totals"] == {}


def test_winning_claim_edits_the_public_drop(tmp_path, caplog):
    async def run():
        cog, _bot = _cog(tmp_path, rng=ScriptedRng(randoms=[0.9, 0.0]))
        cog.store.state["active_drops"] = [_active_drop("abcd1234")]

        def named(user_id, display_name="<@99> @everyone Ada"):
            interaction = _interaction(user_id)
            interaction.user.display_name = display_name
            interaction.message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
            return interaction

        winner = named(6)
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.handle_claim(winner, "abcd1234")
        winner.response.defer.assert_awaited()
        winner.user.send.assert_awaited()
        winner.message.delete.assert_not_awaited()
        edit_kwargs = winner.message.edit.await_args.kwargs
        assert "content" not in edit_kwargs
        mentions = edit_kwargs["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert mentions.replied_user is False
        view = edit_kwargs["view"]
        assert isinstance(view, discord.ui.LayoutView)
        assert view.timeout == CARD_TIMEOUT
        assert not view.is_persistent()
        assert discord.ui.TextDisplay in {type(item) for item in view.walk_children()}
        assert ClaimButton not in {type(item) for item in view.walk_children()}
        assert view.to_components()[0]["type"] == 10
        assert component_text(view) == (
            f"🍬 Claimed by Ada for 1 candy · {CANDY_TYPES[0].emoji} {CANDY_TYPES[0].name}"
        )
        assert "<@" not in component_text(view)
        assert "@everyone" not in component_text(view)
        assert cog.store.state["active_drops"] == []
        assert cog.store.state["totals"]["6"] == 1
        assert cog.store.state["claims_total"] == 1
        assert "1" in component_text(build_leaderboard_view(cog.store.state["totals"], 6))
        assert not any("candy drop edit failed" in record.message for record in caplog.records)

        cog.store.state["totals"]["8"] = 5
        cog.store.state["active_drops"] = [_active_drop("trick222")]
        raisins = next(item for item in TRICKS if item.amount == -2)
        cog.rng = ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)])
        tricked = named(8, "Ada")
        await cog.handle_claim(tricked, "trick222")
        assert component_text(tricked.message.edit.await_args.kwargs["view"]) == "🎃 TRICK! Ada lost 2 candy"
        assert cog.store.state["totals"]["8"] == 3
        tricked.message.delete.assert_not_awaited()

        cog.store.state["active_drops"] = [_active_drop("fail3333")]
        cog.rng = ScriptedRng(randoms=[0.9, 0.0])
        broken = named(9, "Ada")
        broken.message.edit = AsyncMock(side_effect=_http_error())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.handle_claim(broken, "fail3333")
        broken.message.delete.assert_awaited()
        assert any(
            "candy drop edit failed channel=55 drop=fail3333 error=HTTPException" in record.message
            for record in caplog.records
        )
        broken.user.send.assert_awaited()
        assert cog.store.state["active_drops"] == []

        cog.store.state["active_drops"] = [_active_drop("bbbb2222", expires_at=NOW)]
        melted = named(10, "Ada")
        await cog.handle_claim(melted, "bbbb2222")
        melted.message.edit.assert_not_awaited()
        melted.message.delete.assert_awaited()
        message, kwargs = _sent(melted.response.send_message)
        assert message == MELTED_TEXT
        assert kwargs["ephemeral"] is True

        before_real = dict(cog.store.state["totals"])
        cog.store.state["active_drops"] = [_active_drop("dry44444", dry_run=True)]
        cog.rng = ScriptedRng(randoms=[0.9, 0.0])
        tester = named(11, "Ada")
        await cog.handle_claim(tester, "dry44444")
        assert component_text(tester.message.edit.await_args.kwargs["view"]) == (
            f"TEST. 🍬 Claimed by Ada for 1 candy · {CANDY_TYPES[0].emoji} {CANDY_TYPES[0].name}"
        )
        assert cog.store.state["test"]["totals"]["11"] == 1
        assert cog.store.state["test"]["claims_total"] == 1
        assert cog.store.state["totals"] == before_real
        assert "11" not in cog.store.state["totals"]

        claimed = build_claimed_drop_view("🍬 Claimed by Ada for 1 candy")
        assert claimed.timeout == CARD_TIMEOUT
        assert not claimed.is_persistent()

    original = discord.utils.utcnow
    discord.utils.utcnow = lambda: NOW
    try:
        asyncio.run(run())
    finally:
        discord.utils.utcnow = original


def test_drop_creation_assigns_a_candy_type(tmp_path):
    async def run():
        chocolate, worms = CANDY_TYPES[1], CANDY_TYPES[3]
        cog, _bot = _cog(tmp_path, rng=ScriptedRng(choices=[chocolate, worms]))
        channel = FakeTextChannel(42)
        await cog.post_drop(channel, dry_run=False, now=NOW)
        await cog.post_drop(channel, dry_run=True, now=NOW)
        real, test_drop = cog.store.state["active_drops"]
        assert real["dry_run"] is False and real["candy_type"] == "chocolate_bar"
        assert test_drop["dry_run"] is True and test_drop["candy_type"] == "gummy_worms"
        cog.store.save()
        reloaded = CandyStore(cog.store.path)
        assert [drop["candy_type"] for drop in reloaded.state["active_drops"]] == [
            "chocolate_bar",
            "gummy_worms",
        ]

    asyncio.run(run())


def test_treat_claim_counts_candy_type_and_trick_does_not():
    lollipop = next(item for item in CANDY_TYPES if item.key == "lollipop")
    state = default_state()
    drop = _active_drop("treat111")
    drop["candy_type"] = lollipop.key
    state["active_drops"] = [drop]
    state["totals"]["7"] = 5
    state["candy_types"] = {"7": {"lollipop": 2, "candy_corn": 1}}
    updated, outcome = apply_claim(
        state,
        "treat111",
        7,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0]),
        is_bot=False,
        trick_chance=0,
    )
    assert outcome.status == "won" and outcome.roll.kind == "treat"
    assert outcome.roll.amount == 1
    assert updated["totals"]["7"] == 6
    assert updated["candy_types"]["7"] == {"lollipop": 3, "candy_corn": 1}
    assert state["candy_types"]["7"]["lollipop"] == 2
    assert claim_public_text(outcome, "Ada") == f"🍬 Claimed by Ada for 1 candy · {lollipop.emoji} {lollipop.name}"

    jackpot_drop = _active_drop("jack2222")
    jackpot_drop["candy_type"] = "chocolate_bar"
    updated["active_drops"] = [jackpot_drop]
    jackpot_state, jackpot = apply_claim(
        updated,
        "jack2222",
        7,
        NOW,
        ScriptedRng(randoms=[0.9, 0.999]),
        is_bot=False,
        trick_chance=0,
    )
    assert jackpot.roll.amount == 25
    assert jackpot_state["totals"]["7"] == 31
    assert jackpot_state["candy_types"]["7"]["chocolate_bar"] == 25
    assert jackpot_state["candy_types"]["7"]["lollipop"] == 3

    dry = _active_drop("dry33333", dry_run=True)
    dry["candy_type"] = "gummy_worms"
    separate = default_state()
    separate["totals"]["7"] = 5
    separate["candy_types"] = {"7": {"candy_corn": 9}}
    separate["active_drops"] = [dry]
    dry_state, dry_outcome = apply_claim(
        separate,
        "dry33333",
        7,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0]),
        is_bot=False,
        trick_chance=0,
    )
    assert dry_outcome.dry_run
    assert dry_state["totals"]["7"] == 5
    assert dry_state["candy_types"]["7"] == {"candy_corn": 9}
    assert dry_state["test"]["totals"]["7"] == 1
    assert dry_state["test"]["candy_types"]["7"] == {"gummy_worms": 1}

    trick = _active_drop("trick444")
    trick["candy_type"] = "chocolate_bar"
    jackpot_state["active_drops"] = [trick]
    before_types = jackpot_state["candy_types"]
    raisins = next(item for item in TRICKS if item.amount == -2)
    after, lost = apply_claim(
        jackpot_state,
        "trick444",
        7,
        NOW,
        ScriptedRng(randoms=[0.0, _trick_weight_random(raisins)]),
        is_bot=False,
        trick_chance=1,
    )
    assert lost.roll.amount == -2
    assert after["totals"]["7"] == 29
    assert after["candy_types"] == before_types
    assert "chocolate_bar" in after["candy_types"]["7"]
    assert claim_public_text(lost, "Ada") == "🎃 TRICK! Ada lost 2 candy"


def test_legacy_drop_without_candy_type_gets_one_at_claim():
    state = default_state()
    drop = _active_drop("legacy11")
    assert "candy_type" not in drop
    state["active_drops"] = [drop]
    state["totals"]["4"] = 2
    caramel = CANDY_TYPES[4]
    updated, outcome = apply_claim(
        state,
        "legacy11",
        4,
        NOW,
        ScriptedRng(randoms=[0.9, 0.0, 1.0], choices=["kept blurb", caramel]),
        is_bot=False,
        trick_chance=0,
    )
    assert outcome.status == "won" and outcome.roll.amount == 1
    assert updated["active_drops"][0]["candy_type"] == caramel.key
    assert updated["totals"]["4"] == 3
    assert updated["candy_types"]["4"] == {caramel.key: 1}
    assert claim_public_text(outcome, "Ada") == (
        f"🍬 Claimed by Ada for 1 candy · {caramel.emoji} {caramel.name}"
    )


def test_legacy_state_keeps_totals_and_candy_types_roundtrip(tmp_path):
    legacy = normalize_state({"totals": {"4": 2, "5": 0}, "drops_posted": 1, "test": {"totals": {"9": 3}}})
    assert legacy["totals"] == {"4": 2, "5": 0}
    assert legacy["drops_posted"] == 1
    assert legacy["candy_types"] == {}
    assert legacy["test"]["totals"] == {"9": 3}
    assert legacy["test"]["candy_types"] == {}

    garbage = normalize_state(
        {
            "totals": {"1": 8},
            "candy_types": "nope",
            "test": {"totals": {"2": 1}, "candy_types": ["bad"]},
        }
    )
    assert garbage["totals"] == {"1": 8}
    assert garbage["candy_types"] == {}
    assert garbage["test"]["totals"] == {"2": 1}
    assert garbage["test"]["candy_types"] == {}

    mixed = normalize_state(
        {
            "totals": {"7": 9},
            "candy_types": {
                "7": {
                    "candy_corn": 4,
                    "mystery": 3,
                    "lollipop": 0,
                    "chocolate_bar": -2,
                    "gummy_worms": True,
                    "caramel_apple": 2,
                },
                "nope": {"candy_corn": 1},
                "-3": {"candy_corn": 1},
                "8": "bad",
            },
        }
    )
    assert mixed["totals"] == {"7": 9}
    assert mixed["candy_types"] == {"7": {"candy_corn": 4, "caramel_apple": 2}}

    path = tmp_path / "candy_hunt.json"
    path.write_text(json.dumps({"totals": {"4": 2}, "drops_posted": 1}), encoding="utf-8")
    loaded = CandyStore(path)
    assert loaded.state["totals"] == {"4": 2}
    assert loaded.state["drops_posted"] == 1
    assert loaded.state["candy_types"] == {}
    assert loaded.state["test"]["candy_types"] == {}

    loaded.state["totals"]["7"] = 9
    loaded.state["candy_types"] = {"7": {"candy_corn": 4, "caramel_apple": 2}}
    loaded.state["test"]["totals"]["2"] = 1
    loaded.state["test"]["candy_types"] = {"2": {"lollipop": 6}}
    loaded.save()
    again = CandyStore(path)
    assert again.state["totals"]["4"] == 2
    assert again.state["totals"]["7"] == 9
    assert again.state["candy_types"] == {"7": {"candy_corn": 4, "caramel_apple": 2}}
    assert again.state["test"]["totals"]["2"] == 1
    assert again.state["test"]["candy_types"] == {"2": {"lollipop": 6}}


def test_candy_stats_breakdown_is_sorted_and_omitted_when_empty(tmp_path):
    totals = {"8": 6, "3": 4}
    counts = {"chocolate_bar": 2, "lollipop": 2, "candy_corn": 1, "gummy_worms": 0, "nope": 9}
    text = component_text(build_leaderboard_view(totals, 3, type_counts=counts))
    assert "<@8>" in text and "**6**" in text
    assert "You have **4**" in text
    bar = text.index("🍫 Chocolate Bar x2")
    pop = text.index("🍭 Lollipop x2")
    corn = text.index("🌽 Candy Corn x1")
    assert bar < pop < corn
    assert "Gummy" not in text and "nope" not in text

    bare = component_text(build_leaderboard_view(totals, 3))
    assert "You have **4**" in bare
    assert "Chocolate Bar" not in bare
    stranger = component_text(build_leaderboard_view(totals, 9))
    assert "haven't found any" in stranger
    assert "x1" not in stranger and "x2" not in stranger

    async def run():
        cog, _bot = _cog(tmp_path)
        cog.store.state["totals"] = {"3": 4, "8": 6}
        cog.store.state["candy_types"] = {
            "3": {"peanut_butter_cup": 2, "lollipop": 5},
            "8": {"candy_corn": 9},
        }
        home = _interaction(3, guild_id=MTS_GUILD_ID)
        await cog.candy.callback(cog, home)
        own = component_text(home.response.send_message.await_args.kwargs["view"])
        assert "🍭 Lollipop x5" in own
        assert "🥜 Peanut Butter Cup x2" in own
        assert own.index("🍭 Lollipop x5") < own.index("🥜 Peanut Butter Cup x2")
        assert "Candy Corn" not in own
        assert "<@8>" in own

        cog.store.state["test"]["totals"] = {"3": 1}
        cog.store.state["test"]["candy_types"] = {"3": {"gummy_worms": 4}}
        support = _interaction(3, guild_id=SUPPORT_GUILD_ID)
        await cog.candy.callback(cog, support)
        test_text = component_text(support.response.send_message.await_args.kwargs["view"])
        assert "🪱 Gummy Worms x4" in test_text
        assert "Lollipop" not in test_text
        assert "TEST LEADERBOARD" in test_text

    asyncio.run(run())


# --- Multi-drops and night tricks (Oct 31 18:00+) ---
from cogs import candyhunt as _ch

_PT = _ch.PT
_S = _ch.CandyHuntSettings()


def _pt(hour: int, minute: int = 0, day: int = 31) -> datetime:
    return datetime(2026, 10 if day == 31 else 11, day, hour, minute, tzinfo=_PT)


def test_trick_chance_day_vs_night():
    assert _ch.trick_chance_at(_S, _pt(17, 59)) == 0.20
    assert _ch.trick_chance_at(_S, _pt(18, 0)) == 0.25
    assert _ch.trick_chance_at(_S, _pt(22, 0)) == 0.25
    assert _ch.trick_chance_at(_S, _pt(3, 0, day=1)) == 0.25


def test_multi_drop_count_only_at_night():
    rng = random.Random(7)
    assert all(_ch.roll_multi_drop_count(_S, _pt(17, 59), rng) == 1 for _ in range(2000))
    rolls = [_ch.roll_multi_drop_count(_S, _pt(18, 30), rng) for _ in range(20000)]
    assert set(rolls) == {1, 2, 3, 4, 5}
    for count, odds in _ch.MULTI_DROP_ODDS:
        assert abs(rolls.count(count) / len(rolls) - odds) < 0.02


def test_pick_multi_drop_channels_distinct_main_first():
    rng = random.Random(1)
    main = _ch.MAIN_DROP_CHANNEL_ID
    for count in range(2, 6):
        ids = _ch.pick_multi_drop_channels(main, [11, 22, 33, 44, main], count, rng)
        assert ids[0] == main and len(ids) == count and len(set(ids)) == count
    assert len(_ch.pick_multi_drop_channels(main, [11], 5, rng)) == 2


def _drop(drop_id: str, *, extra: bool, expires: datetime) -> dict:
    return _ch.make_drop(drop_id, 1, 2, expires - timedelta(minutes=10), expires, dry_run=False, extra=extra)


def test_expired_extra_drop_keeps_streak_but_main_resets():
    state = {"streak": 22}
    _ch.reset_streaks_for_expiry(state, [_drop("aaaa1111", extra=True, expires=_pt(19))])
    assert state["streak"] == 22
    _ch.reset_streaks_for_expiry(state, [_drop("bbbb2222", extra=False, expires=_pt(19))])
    assert state["streak"] == 0


def test_extra_drop_flag_round_trips_and_old_drops_load():
    raw = _drop("cccc3333", extra=True, expires=_pt(19))
    assert _ch._drop_record(raw)["extra"] is True
    old = _drop("dddd4444", extra=False, expires=_pt(19))
    assert "extra" not in old and "extra" not in _ch._drop_record(old)


def test_claiming_extra_drop_does_not_bump_streak():
    now = _pt(19)
    state = _ch.default_state()
    state["streak"] = 22
    state["active_drops"] = [
        _drop("eeee5555", extra=True, expires=now + timedelta(minutes=5)),
        _drop("ffff6666", extra=False, expires=now + timedelta(minutes=5)),
    ]
    after, won = _ch.apply_claim(state, "eeee5555", 42, now, random.Random(3), is_bot=False, trick_chance=0, settings=_S)
    assert won.status == "won" and after["streak"] == 22
    after2, won2 = _ch.apply_claim(after, "ffff6666", 43, now, random.Random(3), is_bot=False, trick_chance=0, settings=_S)
    assert won2.status == "won" and after2["streak"] == 23


@pytest.mark.parametrize("count,word", [(2, "DOUBLE"), (3, "TRIPLE"), (4, "QUADRUPLE"), (5, "QUINTUPLE")])
def test_multi_drop_banner(count, word):
    extras = list(range(100, 100 + count - 1))
    text = _ch.multi_drop_banner(count, extras, random.Random(count))
    assert f"{word} DROP!" in text
    for cid in extras:
        assert f"<#{cid}>" in text
    assert "@" not in text

def test_multi_drop_odds_switch_at_19():
    assert _ch.multi_drop_odds_at(_S, _pt(18, 0)) is _ch.MULTI_DROP_ODDS
    assert _ch.multi_drop_odds_at(_S, _pt(18, 59)) is _ch.MULTI_DROP_ODDS
    assert _ch.multi_drop_odds_at(_S, _pt(19, 0)) is _ch.MULTI_DROP_ODDS_LATE
    assert _ch.multi_drop_odds_at(_S, _pt(2, 0, day=1)) is _ch.MULTI_DROP_ODDS_LATE
    assert _ch.trick_chance_at(_S, _pt(19, 30)) == 0.25


def test_late_multi_drop_distribution():
    rng = random.Random(11)
    rolls = [_ch.roll_multi_drop_count(_S, _pt(19, 0), rng) for _ in range(20000)]
    for count, odds in _ch.MULTI_DROP_ODDS_LATE:
        assert abs(rolls.count(count) / len(rolls) - odds) < 0.02
    early = [_ch.roll_multi_drop_count(_S, _pt(18, 59), rng) for _ in range(20000)]
    assert abs(early.count(1) / len(early) - 0.65) < 0.02


def test_tricks_all_cost_candy_and_sum_100():
    assert sum(t.weight for t in _ch.TRICKS) == 100
    assert all(t.amount < 0 for t in _ch.TRICKS)
    assert sum(t.weight for t in _ch.TRICKS if t.amount == -1) == 46


def test_night_treat_weights_after_6pm():
    assert sum(t.weight for t in _ch.NIGHT_TREATS) == 100
    assert _ch.treats_at(_S, _pt(17, 59)) is _ch.TREATS
    assert _ch.treats_at(_S, _pt(18, 0)) is _ch.NIGHT_TREATS
    assert _ch.treats_at(_S, _pt(3, 0, day=1)) is _ch.NIGHT_TREATS
    night = {t.name: t.weight for t in _ch.NIGHT_TREATS}
    assert night == _ch.NIGHT_TREAT_WEIGHTS
    assert [t.weight for t in _ch.TREATS] == [28, 22, 16, 12, 8, 8, 6]


def test_apply_claim_uses_night_treats(monkeypatch):
    seen = []
    real = _ch.roll_candy

    def spy(rng, **kw):
        seen.append(kw.get("treats"))
        return real(rng, **kw)

    monkeypatch.setattr(_ch, "roll_candy", spy)
    for when, expected in ((_pt(17, 0), _ch.TREATS), (_pt(19, 0), _ch.NIGHT_TREATS)):
        state = _ch.default_state()
        state["active_drops"] = [{"drop_id": "aaaa1111", "status": "active", "expires_at": (when + timedelta(minutes=5)).isoformat()}]
        _ch.apply_claim(state, "aaaa1111", 7, when, random.Random(1), is_bot=False, trick_chance=0, settings=_S)
        assert seen[-1] is expected


def _fr_state(when, ids=("aaaa1111",), statuses=None):
    state = _ch.default_state()
    created = when - timedelta(minutes=1)
    state["active_drops"] = [
        _ch.make_drop(d, _ch.MAIN_DROP_CHANNEL_ID, 1, created, when + timedelta(minutes=5), dry_run=False, extra=i > 0)
        for i, d in enumerate(ids)
    ]
    state["next_drop_at"] = _ch.iso(when + timedelta(minutes=10))
    return state


class _FrRng(random.Random):
    def __init__(self, roll):
        super().__init__(1)
        self._roll = roll

    def random(self):
        return self._roll


def _fr_claim(state, drop_id, when, roll=0.0):
    return _ch.apply_claim(state, drop_id, 1, when, _FrRng(roll), is_bot=False, trick_chance=0, settings=_S)[0]


def test_frenzy_only_after_1930():
    assert _fr_claim(_fr_state(_pt(19, 29)), "aaaa1111", _pt(19, 29))["frenzy"] is None
    after = _fr_claim(_fr_state(_pt(19, 30)), "aaaa1111", _pt(19, 30))
    assert after["frenzy"]["rounds_left"] == 5
    assert _ch.FRENZY_CHANCE == 0.10
    assert _fr_claim(_fr_state(_pt(19, 45)), "aaaa1111", _pt(19, 45), roll=0.10)["frenzy"] is None


def test_frenzy_needs_full_batch_claim_not_expiry():
    when = _pt(20, 0)
    state = _fr_claim(_fr_state(when, ("aaaa1111", "bbbb2222")), "aaaa1111", when)
    assert state["frenzy"] is None
    state = _fr_claim(state, "bbbb2222", when)
    assert state["frenzy"] is not None
    expired = _fr_state(when)
    batch = expired["active_drops"][0]["created_at"]
    assert _ch.on_batch_finished(expired, batch, when, _FrRng(0.0), _S, all_claimed=False) is None
    assert expired["frenzy"] is None


def test_frenzy_five_rounds_short_gap_then_normal_and_no_nesting():
    when = _pt(20, 0)
    state = _fr_claim(_fr_state(when), "aaaa1111", when)
    gap = _ch.parse_dt(state["next_drop_at"]) - _ch.as_utc(when)
    assert timedelta(seconds=0) <= gap <= timedelta(seconds=5)
    for n in range(1, 6):
        moment = _ch.parse_dt(state["next_drop_at"])
        posted = _ch.iso(moment)
        assert _ch.begin_frenzy_round(state, posted) == n
        state["active_drops"] = [
            _ch.make_drop("cccc3333", 1, 1, moment, moment + timedelta(minutes=5), dry_run=False)
        ]
        assert _ch.frenzy_batch_active(state)
        state = _ch.apply_claim(state, "cccc3333", 1, moment, _FrRng(0.0), is_bot=False, trick_chance=0, settings=_S)[0]
        if n < 5:
            assert state["frenzy"]["rounds_left"] == 5 - n
            gap = _ch.parse_dt(state["next_drop_at"]) - moment
            assert timedelta(seconds=0) <= gap <= timedelta(seconds=5)
    assert state["frenzy"] is None  # ended, and the last round (roll 0.0) did not nest a new one
    low, high = _ch.effective_interval(_S, state["streak"], moment)
    gap = _ch.parse_dt(state["next_drop_at"]) - moment
    assert low <= gap <= high


def test_frenzy_survives_normalize():
    state = _ch.default_state()
    state["frenzy"] = {"rounds_left": 3, "round": 2, "batch": None, "announced": True}
    assert _ch.normalize_state(state)["frenzy"]["rounds_left"] == 3
    assert _ch.normalize_state({})["frenzy"] is None
    assert all(_ch.FRENZY_BANNERS) and len(_ch.FRENZY_BANNERS) == 4


def test_leader_tax_terms():
    totals = {"1": 50, "2": 50, "3": 10}
    chance, tricks = _ch.leader_trick_terms(totals, 1, 0.25)
    assert chance == 0.40 and tricks is _ch.LEADER_TRICKS
    assert _ch.leader_trick_terms(totals, 2, 0.25)[0] == 0.40
    assert _ch.leader_trick_terms(totals, 3, 0.25) == (0.25, _ch.TRICKS)
    assert _ch.leader_trick_terms({"1": 0}, 1, 0.2) == (0.2, _ch.TRICKS)
    base = {t.amount: t.weight for t in _ch.TRICKS if t.amount in (-10, -25)}
    tilted = {t.amount: t.weight for t in _ch.LEADER_TRICKS if t.amount in (-10, -25)}
    assert tilted == {k: v * 2 for k, v in base.items()}
    assert [t.weight for t in _ch.LEADER_TRICKS if t.amount not in (-10, -25)] == [
        t.weight for t in _ch.TRICKS if t.amount not in (-10, -25)
    ]


def test_leader_tax_in_claim(monkeypatch):
    seen = []
    real = _ch.roll_candy

    def spy(rng, **kw):
        seen.append((kw["trick_chance"], kw.get("tricks")))
        return real(rng, **kw)

    monkeypatch.setattr(_ch, "roll_candy", spy)
    for uid, expected in ((1, (0.40, _ch.LEADER_TRICKS)), (2, (0.25, _ch.TRICKS))):
        state = _fr_state(_pt(18, 30))
        state["totals"] = {"1": 90, "2": 5}
        _ch.apply_claim(state, "aaaa1111", uid, _pt(18, 30), _FrRng(0.99), is_bot=False, trick_chance=0.25, settings=_S)
        assert seen[-1] == expected


def test_event_overrides_fall_back_and_apply(caplog):
    assert CANDY_HUNT.announcement_channel_id == _ch.ANNOUNCEMENTS_CHANNEL_ID
    assert CANDY_HUNT.lights_out_minute is None
    assert _ch.lights_out_at(CANDY_HUNT) == window_bounds(CANDY_HUNT)[1]
    assert CANDY_HUNT.multi_drop_odds is _ch.MULTI_DROP_ODDS
    assert CANDY_HUNT.night_tricks is _ch.TRICKS
    assert CANDY_HUNT.leader_tricks is _ch.LEADER_TRICKS

    junk = {
        "multi_drop_start": "nope",
        "multi_drop_late": "25:99",
        "frenzy_start": True,
        "treat_weights": {"Candy Corn": 1},
        "night_treat_weights": [1],
        "trick_weights": [1, 2],
        "night_trick_weights": "nope",
        "multi_drop_odds": [[1, 0]],
        "multi_drop_odds_late": "late",
        "main_drop_channel_id": 0,
        "multi_drop_channel_names": ["gaming", "Gaming"],
        "frenzy_chance": 1.5,
        "frenzy_rounds": 0,
        "frenzy_drop_count": True,
        "frenzy_gap_seconds": [3, 1],
        "frenzy_kick_delay": -1,
        "leader_trick_chance": "nope",
        "leader_big_loss_multiplier": 0,
        "leader_big_loss_amounts": [10],
        "lights_out": "99:00",
        "night_shift_enabled": "true",
        "witching_hour_enabled": 1,
        "announcement_grace_minutes": -5,
        "night_trick_chance": 2,
    }
    with caplog.at_level(logging.WARNING, logger="cogs.candyhunt"):
        assert apply_candy_hunt_overrides(CANDY_HUNT, junk) is CANDY_HUNT
    assert any("key=frenzy_rounds" in record.message for record in caplog.records)
    assert any("key=multi_drop_start" in record.message for record in caplog.records)
    caplog.clear()

    same = {
        "multi_drop_start": "18:00",
        "multi_drop_late": "19:00",
        "frenzy_start": "19:30",
        "frenzy_chance": 0.10,
        "frenzy_rounds": 5,
        "frenzy_drop_count": 5,
        "frenzy_gap_seconds": [2, 2],
        "frenzy_kick_delay": 3,
        "announcement_grace_minutes": 30,
        "night_shift_enabled": True,
        "witching_hour_enabled": True,
        "lights_out": None,
        "night_trick_chance": 0.25,
        "leader_trick_chance": 0.40,
        "leader_big_loss_multiplier": 2,
        "leader_big_loss_amounts": [-10, -25],
        "main_drop_channel_id": _ch.MAIN_DROP_CHANNEL_ID,
        "multi_drop_channel_names": list(_ch.MULTI_DROP_CHANNEL_NAMES),
        "treat_weights": [treat.weight for treat in TREATS],
        "night_treat_weights": {treat.name: treat.weight for treat in _ch.NIGHT_TREATS},
        "trick_weights": [trick.weight for trick in TRICKS],
        "night_trick_weights": [trick.weight for trick in TRICKS],
        "multi_drop_odds": [list(pair) for pair in _ch.MULTI_DROP_ODDS],
        "multi_drop_odds_late": [list(pair) for pair in _ch.MULTI_DROP_ODDS_LATE],
    }
    assert apply_candy_hunt_overrides(CANDY_HUNT, same) is CANDY_HUNT

    night_weights = [treat.weight for treat in _ch.NIGHT_TREATS]
    night_weights[0] += 1
    trick_weights = [trick.weight for trick in TRICKS]
    trick_weights[0] += 1
    updated = apply_candy_hunt_overrides(
        CANDY_HUNT,
        {
            "multi_drop_start": "17:00",
            "multi_drop_late": "20:00",
            "frenzy_start": "30:00",
            "treat_weights": {"Candy Corn": 40, **{treat.name: treat.weight for treat in TREATS if treat.name != "Candy Corn"}},
            "night_treat_weights": night_weights,
            "trick_weights": trick_weights,
            "night_trick_weights": [1, 1, 1, 1, 1, 1, 1],
            "multi_drop_odds": [[1, 0.5], [3, 0.5]],
            "multi_drop_odds_late": [[1, 1.0], [2, 1]],
            "main_drop_channel_id": "77",
            "multi_drop_channel_names": ["arcade"],
            "frenzy_chance": 0,
            "frenzy_rounds": 8,
            "frenzy_drop_count": 3,
            "frenzy_gap_seconds": [1, 4],
            "frenzy_kick_delay": 1.5,
            "leader_trick_chance": 0.7,
            "leader_big_loss_multiplier": 3,
            "leader_big_loss_amounts": [-10, -25],
            "lights_out": "29:30",
            "night_shift_enabled": False,
            "witching_hour_enabled": False,
            "announcement_grace_minutes": 0,
            "night_trick_chance": 0.9,
            "frenzy_rounds_extra": "ignored",
        },
    )
    assert updated.multi_drop_start_minute == 17 * 60
    assert updated.multi_drop_late_minute == 20 * 60
    assert updated.frenzy_start_minute == 30 * 60
    assert updated.treats[0].weight == 40 and updated.treats[0].name == "Candy Corn"
    assert updated.night_treats[0].weight == night_weights[0]
    assert updated.treats is not updated.night_treats
    assert [trick.weight for trick in updated.tricks] == trick_weights
    assert [trick.weight for trick in updated.night_tricks] == [1, 1, 1, 1, 1, 1, 1]
    assert updated.multi_drop_odds == ((1, 0.5), (3, 0.5))
    assert updated.multi_drop_odds_late == ((1, 1.0), (2, 1.0))
    assert updated.main_drop_channel_id == 77
    assert updated.multi_drop_channel_names == ("arcade",)
    assert updated.frenzy_chance == 0.0
    assert updated.frenzy_rounds == 8
    assert updated.frenzy_drop_count == 3
    assert updated.frenzy_gap_seconds == (1.0, 4.0)
    assert updated.frenzy_kick_delay == 1.5
    assert updated.leader_trick_chance == 0.7
    assert updated.leader_big_loss_multiplier == 3
    assert updated.leader_big_loss_amounts == (-10, -25)
    assert [trick.weight for trick in updated.leader_tricks if trick.amount == -10] == [7 * 3]
    assert [trick.weight for trick in updated.leader_tricks if trick.amount == -25] == [4 * 3]
    assert updated.leader_tricks[0].weight == trick_weights[0]
    assert updated.lights_out_minute == 29 * 60 + 30
    assert _ch.lights_out_at(updated).astimezone(PT) == datetime(2026, 11, 1, 5, 30, tzinfo=PT)
    assert updated.night_shift_enabled is False
    assert updated.witching_hour_enabled is False
    assert updated.announcement_grace_minutes == 0
    assert updated.night_trick_chance == 0.9
    # A bad key beside a good one is ignored. The day clock stays put.
    mixed = apply_candy_hunt_overrides(
        CANDY_HUNT,
        {"frenzy_rounds": 8, "frenzy_chance": "nope", "multi_drop_start": "nope"},
    )
    assert mixed.frenzy_rounds == 8
    assert mixed.frenzy_chance == CANDY_HUNT.frenzy_chance
    assert mixed.multi_drop_start_minute == CANDY_HUNT.multi_drop_start_minute
    later_multi = replace(CANDY_HUNT, multi_drop_start_minute=20 * 60)
    assert _ch.roll_multi_drop_count(later_multi, _pt(19, 0), random.Random(0)) == 1
    assert _ch.treats_at(later_multi, _pt(18, 0)) is _ch.NIGHT_TREATS


def test_retired_witch_flags_load_and_new_flags_default_off():
    loaded = normalize_state(
        {
            "witch_curse_done": True,
            "witch_refund_done": False,
            "witching_force_done": "yes",
            "totals": {"4": 2},
        }
    )
    assert loaded["witch_curse_done"] is True
    assert loaded["witch_refund_done"] is False
    assert loaded["witching_force_done"] is False
    assert loaded["totals"] == {"4": 2}
    assert loaded["night_shift_done"] is False
    assert loaded["witching_hour_done"] is False
    fresh = normalize_state({})
    assert "witch_curse_done" not in fresh
    assert "witch_refund_done" not in fresh
    assert "witching_force_done" not in fresh
    assert "witch_curse_done" not in default_state()
    assert default_state()["night_shift_done"] is False
    assert default_state()["witching_hour_done"] is False
    assert normalize_state({"night_shift_done": True, "witching_hour_done": "true"})["night_shift_done"] is True
    assert normalize_state({"witching_hour_done": True})["witching_hour_done"] is True
    assert normalize_state({"night_shift_done": "true"})["night_shift_done"] is False


def test_night_shift_and_witching_hour_due():
    state = default_state()
    state.pop("night_shift_done")
    state.pop("witching_hour_done")
    six = _pt(18, 0)
    seven = _pt(19, 30)
    _start, end = window_bounds(CANDY_HUNT)
    late_restart = _pt(2, 0, day=1)

    assert _ch.night_shift_due(CANDY_HUNT, state, six - timedelta(seconds=1)) is False
    assert _ch.night_shift_due(CANDY_HUNT, state, six) is True
    assert _ch.night_shift_due(CANDY_HUNT, default_state(), six) is True
    assert _ch.night_shift_due(CANDY_HUNT, state, six + timedelta(minutes=30)) is True
    assert _ch.night_shift_due(CANDY_HUNT, state, six + timedelta(minutes=30, seconds=1)) is False
    assert _ch.night_shift_due(CANDY_HUNT, state, late_restart) is False
    assert _ch.night_shift_due(CANDY_HUNT, state, end) is False
    assert _ch.night_shift_due(CANDY_HUNT, state, end + timedelta(hours=2)) is False
    done = default_state()
    done["night_shift_done"] = True
    assert _ch.night_shift_due(CANDY_HUNT, done, six) is False
    fuzzy = default_state()
    fuzzy["night_shift_done"] = "true"
    assert _ch.night_shift_due(CANDY_HUNT, fuzzy, six) is True
    assert _ch.night_shift_due(replace(CANDY_HUNT, night_shift_enabled=False), state, six) is False
    tight = replace(CANDY_HUNT, announcement_grace_minutes=0)
    assert _ch.night_shift_due(tight, state, six) is True
    assert _ch.night_shift_due(tight, state, six + timedelta(seconds=1)) is False
    wide = replace(CANDY_HUNT, announcement_grace_minutes=45)
    assert _ch.night_shift_due(wide, state, six + timedelta(minutes=45)) is True
    assert _ch.night_shift_due(wide, state, six + timedelta(minutes=45, seconds=1)) is False
    earlier = replace(CANDY_HUNT, late_ramp_start_minute=17 * 60)
    assert _ch.night_shift_due(earlier, state, _pt(17, 0)) is True
    assert _ch.night_shift_due(earlier, state, six) is False

    assert _ch.witching_hour_due(CANDY_HUNT, state, seven - timedelta(seconds=1)) is False
    assert _ch.witching_hour_due(CANDY_HUNT, state, seven) is True
    assert _ch.witching_hour_due(CANDY_HUNT, {"totals": {}}, seven) is True
    assert _ch.witching_hour_due(CANDY_HUNT, state, seven + timedelta(minutes=30)) is True
    assert _ch.witching_hour_due(CANDY_HUNT, state, seven + timedelta(minutes=30, seconds=1)) is False
    assert _ch.witching_hour_due(CANDY_HUNT, state, late_restart) is False
    assert _ch.witching_hour_due(CANDY_HUNT, state, end) is False
    marked = default_state()
    marked["witching_hour_done"] = True
    assert _ch.witching_hour_due(CANDY_HUNT, marked, seven) is False
    assert _ch.witching_hour_due(replace(CANDY_HUNT, witching_hour_enabled=False), state, seven) is False
    moved = replace(CANDY_HUNT, frenzy_start_minute=20 * 60)
    assert _ch.witching_hour_due(moved, state, seven) is False
    assert _ch.witching_hour_due(moved, state, _pt(20, 0)) is True


def test_night_shift_and_witching_hour_text():
    mentions = ["<#1>", "<#2>", "<#3>", "<#4>", "<#5>"]
    assert _ch.night_shift_announcement(mentions) == (
        "🎃 **HALLOWEEN IS HERE!** 🎃\n"
        "The sun's gone down, the Witch has had three espressos, and she's NOT holding back anymore. 🧹☕\n"
        "From now on, candy can rain down in **up to five channels at once**: "
        "<#1>, <#2>, <#3>, <#4> and <#5>. "
        "Keep your eyes peeled for DOUBLE, TRIPLE… even QUINTUPLE DROPS.\n"
        "But beware: the Witch is feeling *extra* tricky tonight. More tricks are out there, and some of them bite. 🦷\n"
        "Trick or treat, Menaces. It's about to get stupid. 🍬💀"
    )
    assert "the drop channels" in _ch.night_shift_announcement([])
    fewer = replace(CANDY_HUNT, multi_drop_channel_names=("arcade", "lounge"))
    assert "up to three channels" in _ch.night_shift_announcement(["<#1>", "<#2>", "<#3>"], fewer)
    assert _ch.mentions_for_names([(1, "Gaming"), (2, "music")], ("gaming", "nope", "music")) == ["<#1>", "<#2>"]

    assert _ch.witching_hour_announcement("<#9>") == (
        "🦇🔮 **THE WITCHING HOUR HAS BEGUN** 🔮🦇\n"
        "The clock struck 7:30 and the Witch kicked open the cauldron. "
        "From now until the hunt ends (6 AM PT), every time you Menaces claim **every** drop in a batch, "
        "there's a random chance she loses her mind and starts a **FRENZY**: 🧹\n"
        "🍬 **5 drop rounds back to back**, no waiting. Quintuple drops every round.\n"
        "📣 You'll know it's on when the **WITCHING HOUR** banner hits <#9>."
    )
    assert _ch.witching_hour_announcement("").endswith("hits #general.")
    custom = replace(
        CANDY_HUNT,
        frenzy_start_minute=20 * 60,
        frenzy_rounds=3,
        frenzy_drop_count=2,
        window_end_minute=28 * 60,
    )
    custom_text = _ch.witching_hour_announcement("<#9>", custom)
    assert "The clock struck 8 PM" in custom_text
    assert "(4 AM PT)" in custom_text
    assert "**3 drop rounds back to back**" in custom_text
    assert "Double drops every round." in custom_text


def test_maybe_night_shift_and_witching_hour_post_once(tmp_path, caplog):
    async def run():
        folder = tmp_path / "shift"
        folder.mkdir()
        cog, bot = _cog(folder)
        general = FakeTextChannel(_ch.MAIN_DROP_CHANNEL_ID, "general")
        gaming = FakeTextChannel(2, "gaming")
        sharing = FakeTextChannel(3, "sharing-is-caring")
        food = FakeTextChannel(4, "food-porn")
        music = FakeTextChannel(5, "music")
        announce = FakeTextChannel(8, "announcements")
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[gaming, announce, music, general, food, sharing],
        )
        bot.guilds[MTS_GUILD_ID] = guild
        six = _pt(18, 0)
        seven = _pt(19, 30)
        saved = {}

        async def send(*args, **kwargs):
            saved["flag"] = cog.store.state["night_shift_done"]
            saved["disk"] = json.loads(cog.store.path.read_text(encoding="utf-8"))
            announce.sent.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(id=88)

        announce.send = send
        await cog.maybe_night_shift(six - timedelta(seconds=1))
        assert announce.sent == []
        assert cog.store.state["night_shift_done"] is False

        cog.store.state["event_disabled"] = True
        await cog.maybe_night_shift(six)
        assert announce.sent == []
        assert cog.store.state["night_shift_done"] is False
        cog.store.state["event_disabled"] = False

        lonely = SimpleNamespace(text_channels=[FakeTextChannel(9, "lounge")])
        assert cog._night_shift_mentions(lonely) == []
        assert cog._main_drop_mention(lonely) == "#general"
        assert cog._night_shift_mentions(guild) == [
            f"<#{general.id}>",
            f"<#{gaming.id}>",
            f"<#{sharing.id}>",
            f"<#{food.id}>",
            f"<#{music.id}>",
        ]

        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_night_shift(six)
        assert saved["flag"] is True
        assert saved["disk"]["night_shift_done"] is True
        assert len(announce.sent) == 1
        assert announce.sent[0]["args"][0] == _ch.night_shift_announcement(cog._night_shift_mentions(guild))
        mentions = announce.sent[0]["kwargs"]["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert any("candy night shift posted" in record.message for record in caplog.records)
        await cog.maybe_night_shift(six + timedelta(minutes=1))
        assert len(announce.sent) == 1

        hour_saved = {}

        async def send_hour(*args, **kwargs):
            hour_saved["flag"] = cog.store.state["witching_hour_done"]
            hour_saved["disk"] = json.loads(cog.store.path.read_text(encoding="utf-8"))
            announce.sent.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(id=89)

        announce.send = send_hour
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_witching_hour(seven)
        assert hour_saved["flag"] is True
        assert hour_saved["disk"]["witching_hour_done"] is True
        assert announce.sent[-1]["args"][0] == _ch.witching_hour_announcement(f"<#{general.id}>")
        assert announce.sent[-1]["kwargs"]["allowed_mentions"].everyone is False
        assert any("candy witching hour posted" in record.message for record in caplog.records)
        await cog.maybe_witching_hour(seven + timedelta(minutes=1))
        assert len(announce.sent) == 2

        missing_dir = tmp_path / "no-room"
        missing_dir.mkdir()
        missing, missing_bot = _cog(missing_dir)
        missing_bot.guilds[MTS_GUILD_ID] = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[FakeTextChannel(1, "general")],
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await missing.maybe_night_shift(six)
        assert missing.store.state["night_shift_done"] is False
        assert any("candy night shift skipped reason=no_announcement_channel" in record.message for record in caplog.records)

        late_dir = tmp_path / "late"
        late_dir.mkdir()
        late, late_bot = _cog(late_dir)
        late_channel = FakeTextChannel(8, "announcements")
        late_bot.guilds[MTS_GUILD_ID] = SimpleNamespace(id=MTS_GUILD_ID, text_channels=[late_channel])
        await late.maybe_night_shift(six + timedelta(minutes=31))
        await late.maybe_witching_hour(window_bounds(CANDY_HUNT)[1])
        assert late_channel.sent == []
        assert late.store.state["night_shift_done"] is False
        assert late.store.state["witching_hour_done"] is False

        off_dir = tmp_path / "off"
        off_dir.mkdir()
        off, off_bot = _cog(off_dir)
        off.settings = replace(CANDY_HUNT, night_shift_enabled=False, witching_hour_enabled=False)
        off_channel = FakeTextChannel(8, "announcements")
        off_bot.guilds[MTS_GUILD_ID] = SimpleNamespace(id=MTS_GUILD_ID, text_channels=[off_channel])
        await off.maybe_night_shift(six)
        await off.maybe_witching_hour(seven)
        assert off_channel.sent == []
        assert off.store.state["night_shift_done"] is False
        assert off.store.state["witching_hour_done"] is False

        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        failed, failed_bot = _cog(failed_dir)
        failed_channel = FakeTextChannel(8, "announcements", error=_http_error())
        failed_bot.guilds[MTS_GUILD_ID] = SimpleNamespace(id=MTS_GUILD_ID, text_channels=[failed_channel])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await failed.maybe_witching_hour(seven)
        assert failed_channel.sent == []
        assert failed.store.state["witching_hour_done"] is True
        assert any("candy witching hour failed" in record.message for record in caplog.records)
        failed_channel.error = None
        await failed.maybe_witching_hour(seven + timedelta(minutes=1))
        assert failed_channel.sent == []

    asyncio.run(run())


def test_frenzy_rounds_are_quintuple():
    from cogs import candyhunt as ch
    assert ch.FRENZY_DROP_COUNT == 5
    assert ch.frenzy_drop_count(True, 1) == 5
    assert ch.frenzy_drop_count(False, 2) == 2
    state = {"frenzy": {"rounds_left": 3, "round": 2, "batch": None, "announced": True}}
    assert ch.begin_frenzy_round(state, "x") == 3
    assert ch.begin_frenzy_round({"frenzy": None}, "x") is None


def test_frenzy_gap_is_near_zero():
    import random
    from cogs import candyhunt as ch
    assert ch.frenzy_gap(random.Random(1)).total_seconds() <= 5
    assert ch.FRENZY_KICK_DELAY + max(ch.FRENZY_GAP_SECONDS) < ch.LOOP_SECONDS


def test_lights_out_due_posts_once_between_six_and_ten():
    from cogs import candyhunt as ch

    _start, end = window_bounds(CANDY_HUNT)
    wrap = CANDY_HUNT.wrap_up_at
    quiet = default_state()
    assert ch.lights_out_due(CANDY_HUNT, quiet, end - timedelta(seconds=1)) is False
    assert ch.lights_out_due(CANDY_HUNT, quiet, end) is True
    assert ch.lights_out_due(CANDY_HUNT, quiet, wrap - timedelta(seconds=1)) is True
    assert ch.lights_out_due(CANDY_HUNT, quiet, wrap) is False
    assert ch.lights_out_due(CANDY_HUNT, quiet, wrap + timedelta(hours=1)) is False

    posted = default_state()
    posted["lights_out_done"] = True
    assert ch.lights_out_due(CANDY_HUNT, posted, end + timedelta(minutes=5)) is False
    wrapped = default_state()
    wrapped["wrap_up_done"] = True
    assert ch.lights_out_due(CANDY_HUNT, wrapped, end + timedelta(minutes=5)) is False

    running = default_state()
    running["test"]["session"] = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=end,
        source="command",
    )
    assert ch.lights_out_due(CANDY_HUNT, running, end + timedelta(minutes=1)) is False
    finished = default_state()
    finished["test"]["session"] = make_test_session(
        guild_id=SUPPORT_GUILD_ID,
        channel_id=SUPPORT_TEST_CHANNEL_ID,
        minutes=15,
        interval=2,
        now=end - timedelta(hours=3),
        source="command",
    )
    assert ch.lights_out_due(CANDY_HUNT, finished, end + timedelta(minutes=1)) is True

    moment = end + timedelta(minutes=30)
    dry = default_state()
    dry["active_drops"] = [
        make_drop("abcd1234", 1, 2, moment, moment + timedelta(minutes=5), dry_run=True)
    ]
    assert ch.lights_out_due(CANDY_HUNT, dry, moment) is False

    assert default_state()["lights_out_done"] is False
    assert normalize_state({})["lights_out_done"] is False
    assert normalize_state({"lights_out_done": True})["lights_out_done"] is True
    assert normalize_state({"lights_out_done": "true"})["lights_out_done"] is False

    assert 4 <= len(ch.LIGHTS_OUT_BANNERS) <= 5
    assert len(set(ch.LIGHTS_OUT_BANNERS)) == len(ch.LIGHTS_OUT_BANNERS)
    for banner in ch.LIGHTS_OUT_BANNERS:
        folded = banner.casefold()
        assert "candy hunt is over" in folded
        assert "witch has flown home" in folded
        assert "drops are off" in folded
        assert "thanks for playing" in folded
        assert "10 am wrap-up" in folded
        assert "<@" not in banner
        assert "@everyone" not in folded
        assert "@here" not in folded
        assert ch.is_candy_hunt_message(banner) is False


def test_maybe_lights_out_saves_before_the_single_announcement(tmp_path, caplog):
    from cogs import candyhunt as ch

    _start, end = window_bounds(CANDY_HUNT)

    async def run():
        folder = tmp_path / "lights"
        folder.mkdir()
        cog, bot = _cog(folder)
        announce = FakeTextChannel(8, "announcements")
        guild = SimpleNamespace(id=MTS_GUILD_ID, text_channels=[announce])
        bot.guilds[MTS_GUILD_ID] = guild

        await cog.maybe_lights_out(end - timedelta(seconds=1))
        assert announce.sent == []
        assert cog.store.state["lights_out_done"] is False

        cog.store.state["event_disabled"] = True
        await cog.maybe_lights_out(end)
        assert announce.sent == []
        assert cog.store.state["lights_out_done"] is False
        cog.store.state["event_disabled"] = False

        cog.store.state["test"]["session"] = make_test_session(
            guild_id=SUPPORT_GUILD_ID,
            channel_id=SUPPORT_TEST_CHANNEL_ID,
            minutes=15,
            interval=2,
            now=end,
            source="command",
        )
        await cog.maybe_lights_out(end + timedelta(minutes=1))
        assert announce.sent == []
        assert cog.store.state["lights_out_done"] is False
        cog.store.state["test"]["session"] = None

        saved_during_send = {}

        async def send(*args, **kwargs):
            saved_during_send["flag"] = cog.store.state["lights_out_done"]
            saved_during_send["disk"] = json.loads(cog.store.path.read_text(encoding="utf-8"))
            announce.sent.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(id=88)

        announce.send = send
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await cog.maybe_lights_out(end + timedelta(minutes=1))
        assert saved_during_send["flag"] is True
        assert saved_during_send["disk"]["lights_out_done"] is True
        assert len(announce.sent) == 1
        assert announce.sent[0]["args"][0] in ch.LIGHTS_OUT_BANNERS
        mentions = announce.sent[0]["kwargs"]["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert any("candy lights out posted" in record.message for record in caplog.records)

        await cog.maybe_lights_out(end + timedelta(minutes=2))
        assert len(announce.sent) == 1

        missing_dir = tmp_path / "no-room"
        missing_dir.mkdir()
        missing = _cog(missing_dir)
        missing[1].guilds[MTS_GUILD_ID] = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[FakeTextChannel(1, "general")],
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await missing[0].maybe_lights_out(end)
        assert missing[0].store.state["lights_out_done"] is False
        assert any("no_announcement_channel" in record.message for record in caplog.records)

        late_dir = tmp_path / "late"
        late_dir.mkdir()
        late = _cog(late_dir)
        late_channel = FakeTextChannel(8, "announcements")
        late[1].guilds[MTS_GUILD_ID] = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[late_channel],
        )
        await late[0].maybe_lights_out(CANDY_HUNT.wrap_up_at)
        assert late_channel.sent == []
        assert late[0].store.state["lights_out_done"] is True

        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        failed = _cog(failed_dir)
        failed_channel = FakeTextChannel(8, "announcements", error=_http_error())
        failed[1].guilds[MTS_GUILD_ID] = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[failed_channel],
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cogs.candyhunt"):
            await failed[0].maybe_lights_out(end)
        assert failed_channel.sent == []
        assert failed[0].store.state["lights_out_done"] is True
        assert any("candy lights out failed" in record.message for record in caplog.records)
        failed_channel.error = None
        await failed[0].maybe_lights_out(end + timedelta(minutes=1))
        assert failed_channel.sent == []

    asyncio.run(run())


def test_wrap_up_announcement_is_a_plain_name_leaderboard():
    from cogs import candyhunt as ch

    names = {1: "Ada", 2: "Bo", 3: "<@3>", 4: "Dee @everyone", 5: "Eve", 6: "Finn"}
    state = {
        "totals": {"1": 50, "2": 40, "3": 30, "4": 20, "5": 10, "6": 5},
        "claim_counts": {"1": 4, "2": 9, "7": 3},
        "candy_types": {
            "2": {"candy_corn": 12, "lollipop": 2},
            "1": {"chocolate_bar": 4},
        },
    }
    text = ch.wrap_up_announcement_body(state, names, pumpkin_days=7)
    assert "🥇 **Pumpkin God** — Ada — **50** candy" in text
    assert "Vice Pumpkin — Bo — **40** candy" in text
    assert "Candy Goblin — someone — **30** candy" in text
    assert "Porch Pirate — Dee — **20** candy" in text
    assert "Bucket Understudy — Eve — **10** candy" in text
    assert "Finn" not in text
    assert "<@" not in text
    assert "@everyone" not in text
    assert "@here" not in text
    assert "🖐️ Fastest Hands: Bo grabbed the bowl 9 times." in text
    assert "🌽 Corn Baron: Bo hauled 12 candy out of Candy Corn drops." in text
    assert "stacked" not in text
    assert "Variety Bucket: Bo collected 2 kinds of candy." in text
    assert "Cursed Bucket: 7 (3 grabs) finished on 0." in text
    assert "jackpot" not in text.casefold()
    assert "trick" not in text.casefold()
    assert "biggest loss" not in text.casefold()
    assert text.count(WINNER_DECORATION_LINE) == 1
    assert f"The Pumpkin God reigns for 7 days.\n{WINNER_DECORATION_LINE}" in text

    tied = ch.wrap_up_announcement_body(
        {"totals": {"9": 10, "2": 10, "3": 4}},
        {2: "Ada", 9: "Bo", 3: "Cee"},
        pumpkin_days=7,
    )
    assert "it's a tie!" in tied
    assert "🎃 Ada — **10** candy" in tied
    assert "🎃 Bo — **10** candy" in tied
    assert "The crown goes to Ada." in tied
    assert "Vice Pumpkin — Cee — **4** candy" in tied
    assert "Candy Goblin" not in tied
    assert "Fastest Hands" not in tied

    solo = ch.wrap_up_announcement_body({"totals": {"4": 8}}, {4: "Ada"}, pumpkin_days=7)
    assert "Honorable mentions" not in solo
    assert "Vice Pumpkin" not in solo
    assert "🥇 **Pumpkin God** — Ada — **8** candy" in solo

    empty = ch.wrap_up_announcement_body({"totals": {"4": 0}}, {}, pumpkin_days=7)
    assert "the ghosts kept the bowl" in empty.casefold()
    assert WINNER_DECORATION_LINE not in empty

    shared = ch.wrap_up_superlatives(
        {"claim_counts": {"1": 4, "2": 4}, "candy_types": {"1": {"gummy_worms": 6}, "2": {"gummy_worms": 6}}},
        {1: "Ada", 2: "Bo"},
    )
    assert shared[0] == "🖐️ Fastest Hands: Ada and Bo grabbed the bowl 4 times."
    assert "🪱 Worm Whisperer: Ada hauled 6 candy out of Gummy Worms drops." in shared
    assert "🪱 Worm Whisperer: Bo hauled 6 candy out of Gummy Worms drops." in shared
    assert not any("Variety Bucket" in line for line in shared)

    tied_kinds = ch.wrap_up_superlatives(
        {
            "candy_types": {
                "1": {"candy_corn": 5, "lollipop": 1, "chocolate_bar": 1},
                "2": {"gummy_worms": 4, "caramel_apple": 2, "peanut_butter_cup": 1},
            }
        },
        {1: "Ada", 2: "Bo"},
    )
    assert "🌽 Corn Baron: Ada hauled 5 candy out of Candy Corn drops." in tied_kinds
    assert not any("Variety Bucket" in line for line in tied_kinds)

    one_kind = ch.wrap_up_superlatives(
        {"candy_types": {"1": {"caramel_apple": 3}}},
        {1: "Ada"},
    )
    assert "🍎 Sticky Champion: Ada hauled 3 candy out of Caramel Apple drops." in one_kind
    assert not any("Variety Bucket" in line for line in one_kind)

    view = build_winner_view(
        top=[("5", 3), ("6", 1)],
        drops_posted=4,
        claims_total=2,
        pumpkin_days=7,
        names={"5": "<@5> Ada", "6": "Bo"},
    )
    rendered = component_text(view)
    assert "<@" not in rendered
    assert "Ada" in rendered
    assert "Vice Pumpkin — Bo — **1** candy" in rendered


def test_is_candy_hunt_message_matches_hunt_spam_only():
    from cogs import candyhunt as ch

    drop = ch.build_drop_view("abcd1234", expires_at=NOW + timedelta(minutes=5), dry_run=False)
    test_drop = ch.build_drop_view("abcd1234", expires_at=NOW + timedelta(minutes=5), dry_run=True)
    card = ch.build_candy_card(
        ClaimOutcome(
            status="won",
            mutated=True,
            roll=ch.CandyRoll("treat", "Candy Corn", 1, "One kernel."),
            total=3,
            rank=1,
            players=1,
        )
    )
    assert ch.is_candy_hunt_message("", drop.to_components()) is True
    assert ch.is_candy_hunt_message("", test_drop.to_components()) is True
    assert ch.is_candy_hunt_message("", card.to_components()) is True
    assert ch.is_candy_hunt_message("🍬 Claimed by Ada for 3 candy · 🌽 Candy Corn") is True
    assert ch.is_candy_hunt_message("🎃 TRICK! Ada lost 2 candy") is True
    assert ch.is_candy_hunt_message("TEST. 🍬 Claimed by Ada for 1 candy") is True
    assert ch.is_candy_hunt_message(ch.multi_drop_banner(2, [11], random.Random(0))) is True
    assert ch.is_candy_hunt_message(ch.multi_drop_banner(3, [11], random.Random(1))) is True
    assert ch.is_candy_hunt_message(ch.multi_drop_banner(4, [11], random.Random(2))) is True
    assert ch.is_candy_hunt_message(ch.multi_drop_banner(5, [11, 12, 13, 14], random.Random(3))) is True
    for banner in ch.FRENZY_BANNERS:
        assert ch.is_candy_hunt_message(banner) is True
    assert ch.is_candy_hunt_message("the witch's curse took 2 from Ada") is True
    assert ch.is_candy_hunt_message("the witch had a change of heart and returned 2") is True
    assert ch.is_candy_hunt_message("", [{"custom_id": "candy:claim:abcd1234", "label": "Grab the candy"}]) is True

    assert ch.is_candy_hunt_message("hello from the bot") is False
    assert ch.is_candy_hunt_message("") is False
    assert ch.is_candy_hunt_message(None) is False
    assert ch.is_candy_hunt_message("", [{"custom_id": "music:skip", "label": "Skip"}]) is False
    board = ch.build_leaderboard_view({"1": 3}, 1)
    assert ch.is_candy_hunt_message("", board.to_components()) is False
    winner = ch.build_winner_view(top=[("1", 4)], drops_posted=1, claims_total=1, pumpkin_days=7)
    assert ch.is_candy_hunt_message("", winner.to_components()) is False
    for banner in ch.LIGHTS_OUT_BANNERS:
        assert ch.is_candy_hunt_message(banner) is False

    assert ch.ANNOUNCEMENTS_CHANNEL_ID == 1019352642977267743
    assert ch.is_drop_channel(ch.MAIN_DROP_CHANNEL_ID, "renamed") is True
    assert ch.is_drop_channel(1, "Gaming") is True
    assert ch.is_drop_channel(1, "sharing-is-caring") is True
    assert ch.is_drop_channel(1, "food-porn") is True
    assert ch.is_drop_channel(1, "music") is True
    assert ch.is_drop_channel(1, "general") is False
    assert ch.is_drop_channel(1, "announcements") is False
    assert ch.cleanup_skip_channel(ch.ANNOUNCEMENTS_CHANNEL_ID, "general") is True
    assert ch.cleanup_skip_channel(3, "staff-announcements") is True
    assert ch.cleanup_skip_channel(3, "general", announcement_channel_id=3) is True
    assert ch.cleanup_skip_channel(ch.MAIN_DROP_CHANNEL_ID, "general") is False

    state = ch.default_state()
    state["active_drops"] = [ch.make_drop("abcd1234", 5, 9, NOW, NOW + timedelta(minutes=1), dry_run=False)]
    assert ch.stored_candy_message_refs(state) == [(5, 9)]
    state["active_drops"][0]["message_id"] = None
    assert ch.stored_candy_message_refs(state) == []


def test_candyadmin_cleanup_counts_then_deletes_only_bot_hunt_spam(tmp_path, monkeypatch):
    from cogs import candyhunt as ch

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ch.asyncio, "sleep", fake_sleep)
    start, _end = window_bounds(CANDY_HUNT)
    bot_id = 4242

    def hunt_message(message_id, author_id, content, when):
        deleted: list[int] = []

        async def delete():
            deleted.append(message_id)

        return SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(id=author_id, bot=author_id == bot_id),
            content=content,
            components=[],
            embeds=[],
            created_at=when,
            delete=delete,
            deleted=deleted,
        )

    async def run():
        general = FakeTextChannel(ch.MAIN_DROP_CHANNEL_ID, "general")
        gaming = FakeTextChannel(555, "gaming")
        announcements = FakeTextChannel(ch.ANNOUNCEMENTS_CHANNEL_ID, "announcements")
        lounge = FakeTextChannel(777, "lounge")
        drop = hunt_message(
            11,
            bot_id,
            "A pumpkin bucket just clattered into the channel.",
            start,
        )
        claim = hunt_message(12, bot_id, "🍬 Claimed by Ada for 2 candy", start + timedelta(minutes=5))
        song = hunt_message(13, bot_id, "Now playing: Thriller", start + timedelta(minutes=6))
        stranger = hunt_message(
            14,
            5,
            "A pumpkin bucket just clattered into the channel.",
            start + timedelta(minutes=7),
        )
        old = hunt_message(
            15,
            bot_id,
            "A pumpkin bucket just clattered into the channel.",
            start - timedelta(hours=2),
        )
        frenzy = hunt_message(21, bot_id, ch.FRENZY_BANNERS[0], start + timedelta(minutes=8))
        announce_spam = hunt_message(
            31,
            bot_id,
            "A pumpkin bucket just clattered into the channel.",
            start + timedelta(minutes=9),
        )
        lounge_spam = hunt_message(
            41,
            bot_id,
            "🎃 **DOUBLE DROP!** 🎃 candy in the lounge",
            start + timedelta(minutes=10),
        )
        stored = hunt_message(999, bot_id, "edited beyond recognition", start + timedelta(hours=1))
        general.history_messages = [drop, claim, song, stranger, old]
        gaming.history_messages = [frenzy]
        announcements.history_messages = [announce_spam]
        lounge.history_messages = [lounge_spam]

        async def fetch_message(message_id):
            if int(message_id) == stored.id:
                return stored
            raise discord.NotFound(_HTTPResponse(), "missing")

        general.fetch_message = fetch_message
        guild = SimpleNamespace(
            id=MTS_GUILD_ID,
            text_channels=[announcements, lounge, gaming, general],
        )
        bot = FakeBot()
        bot.user = SimpleNamespace(id=bot_id)
        bot.guilds[MTS_GUILD_ID] = guild
        cog, _bot = _cog(tmp_path, bot=bot)
        cog.store.state["active_drops"] = [
            make_drop(
                "abcd1234",
                general.id,
                stored.id,
                start,
                start + timedelta(minutes=10),
                dry_run=False,
            )
        ]

        bad = _ctx(cog)
        await cog.admin_cleanup.callback(cog, bad, "maybe")
        usage, _kwargs = _replied(bad)
        assert usage == ch.CANDYADMIN_CLEANUP_USAGE

        elsewhere = _ctx(cog, guild_id=SUPPORT_GUILD_ID)
        await cog.admin_cleanup.callback(cog, elsewhere, "false")
        refused, _kwargs = _replied(elsewhere)
        assert "will not delete" in refused
        assert drop.deleted == []

        cog.bot.user = None
        blind = _ctx(cog)
        await cog.admin_cleanup.callback(cog, blind, "false")
        blind_text, _kwargs = _replied(blind)
        assert "didn't delete" in blind_text
        cog.bot.user = SimpleNamespace(id=bot_id)

        preview = _ctx(cog)
        await cog.admin_cleanup.callback(cog, preview, None)
        counted, count_kwargs = _replied(preview)
        assert count_kwargs["mention_author"] is False
        assert "Nothing was deleted" in counted
        assert f"#gaming (555): 1" in counted
        assert f"#general ({general.id}): 3" in counted
        assert "announcements" not in counted.casefold()
        assert "lounge" not in counted.casefold()
        assert drop.deleted == []
        assert stored.deleted == []
        assert announce_spam.deleted == []
        assert slept == []

        real = _ctx(cog)
        await cog.admin_cleanup.callback(cog, real, "false")
        deleted_text, _kwargs = _replied(real)
        assert "deleted 1 of 1" in deleted_text
        assert "deleted 3 of 3" in deleted_text
        assert slept == [ch.CLEANUP_DELETE_PAUSE, ch.CLEANUP_DELETE_PAUSE]
        assert drop.deleted == [11]
        assert claim.deleted == [12]
        assert stored.deleted == [999]
        assert frenzy.deleted == [21]
        assert song.deleted == []
        assert stranger.deleted == []
        assert old.deleted == []
        assert announce_spam.deleted == []
        assert lounge_spam.deleted == []

    asyncio.run(run())
