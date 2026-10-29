"""Halloween candy hunt for Menaces to Sobriety.

Drops land in active text channels from Halloween morning through 6 AM the
next day, the first click wins, and November 1 crowns a Pumpkin God. A
support-server test session can post dry-run drops into the test tally
without touching the MTS totals.
Pure helpers at the top are what the tests drive; the cog only talks to Discord.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import secrets
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.constants import DATA_DIR
from cogs.utils.json_store import LockedJsonFile
from cogs.utils.server_config import get_guild_config

log = logging.getLogger(__name__)

MTS_GUILD_ID = 932057681307512922
NICK_ID = 87324314084331520
PT = ZoneInfo("America/Los_Angeles")
DEFAULT_PATH = DATA_DIR / "candy_hunt.json"
# One-shot flag. Read at cog load, consumed once the bot is ready, then deleted.
AUTOSTART_PATH = DATA_DIR / "candy_test_session_autostart.json"
LOOP_SECONDS = 30
DROP_ID_RE_TEXT = r"[0-9a-f]{8}"
CLAIM_TEMPLATE = rf"candy:claim:(?P<drop_id>{DROP_ID_RE_TEXT})"
PUMPKIN_COLOUR = discord.Colour(0xF47B20)
TRICK_COLOUR = discord.Colour(0x6C3483)
PUMPKIN_IMAGE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f383.png"
CANDY_IMAGE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f36c.png"
LATE_CLAIM_TEXT = "Someone already grabbed this one!"
MELTED_TEXT = "This one melted before anyone grabbed it."
MEDALS = ("🥇", "🥈", "🥉")
# Static cards have no buttons. A finite timeout keeps them out of the persistent
# view store. The drop view stays timeout=None so its claim button survives restart.
CARD_TIMEOUT = 180.0


def _as_blurbs(value: tuple[str, ...] | str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class Treat:
    name: str
    amount: int
    weight: int
    blurbs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "blurbs", _as_blurbs(self.blurbs))

    @property
    def blurb(self) -> str:
        return self.blurbs[0] if self.blurbs else ""


@dataclass(frozen=True)
class Trick:
    amount: int
    blurbs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "blurbs", _as_blurbs(self.blurbs))

    @property
    def blurb(self) -> str:
        return self.blurbs[0] if self.blurbs else ""


# Common handfuls, a few generous bars, and a rare jackpot. Weights sum to 100.
TREATS: tuple[Treat, ...] = (
    Treat(
        "Candy Corn",
        1,
        28,
        (
            "One sticky kernel. The porch light hums.",
            "A single kernel. You hold it like a coin.",
            "One piece. The jack-o'-lantern looks pleased.",
            "Candy corn, singular. The night takes notes.",
            "Just one sticky bite. Somewhere a gate squeaks.",
        ),
    ),
    Treat(
        "Lollipop",
        2,
        22,
        (
            "Cherry. Or possibly blood. It's cherry.",
            "A red disc on a white stick. You accept the risk.",
            "The wrapper crinkles. The street goes quiet.",
            "Sweet, round, and a little too shiny.",
            "You raise it to the porch light. Still cherry.",
        ),
    ),
    Treat(
        "Chocolate Bar",
        3,
        16,
        (
            "A full bar. You are the main character tonight.",
            "Squares of chocolate. The wrapper is warm.",
            "Someone gave you the good stuff.",
            "It snaps clean. The night approves.",
        ),
    ),
    Treat(
        "Gummy Worms",
        4,
        12,
        (
            "They wiggle. That's normal. Probably.",
            "A handful of worms. They do not blink.",
            "Sour, stretchy, and a little too lively.",
            "You count four. One of them shifts.",
            "The bag rustles. The worms stay put. Mostly.",
        ),
    ),
    Treat(
        "Caramel Apple",
        6,
        8,
        (
            "Sticky, shiny, and slightly cursed.",
            "A whole apple on a stick. Ambitious.",
            "Caramel tugs at your teeth. Worth it.",
            "The stick is sturdy. Your courage is average.",
            "It gleams. Something in the yard gleams back.",
        ),
    ),
    Treat(
        "Full-Size Snickers",
        10,
        8,
        (
            "The holy grail of the suburban porch.",
            "Full size. The neighbors will hear about this.",
            "Nougat, caramel, and a small gasp.",
            "You check the wrapper twice. It is real.",
            "A king-size moment on a fun-size street.",
        ),
    ),
    Treat(
        "Pumpkin Jackpot",
        25,
        6,
        (
            "The pumpkin splits open and candy rains out.",
            "Twenty-five pieces. The porch goes quiet.",
            "The jackpot clatters into your bucket.",
            "Every kernel and bar you hoped for, at once.",
            "The pumpkin winks. Your bucket gets heavy.",
        ),
    ),
)

TRICKS: tuple[Trick, ...] = (
    Trick(
        -1,
        (
            "A porch ghost swipes one piece and cackles.",
            "Cold fingers lift one piece and vanish.",
            "A ghost taxes you one piece, then laughs.",
        ),
    ),
    Trick(
        0,
        (
            "You bite down. It's an onion. You put it back.",
            "An onion. You return it with what dignity you have.",
            "Your teeth meet onion. The score stays put.",
        ),
    ),
    Trick(
        0,
        (
            "A plastic spider. Festive, and completely inedible.",
            "Plastic legs and zero candy. Your total holds.",
        ),
    ),
    Trick(
        -2,
        (
            "Raisins. The ancient betrayal. Two pieces walk off in shame.",
            "Raisins. Two pieces leave in protest.",
            "The raisin box costs you two pieces.",
        ),
    ),
    Trick(
        -1,
        (
            "Your bucket has a hole. One piece hits the sidewalk.",
            "A hole in the bucket. One piece escapes.",
            "You lose one piece through a hole in the bucket.",
        ),
    ),
    Trick(
        0,
        (
            "The house is dark. You get a 'boo' and an empty hand.",
            "Nobody's home. A 'boo', and your candy stays put.",
            "Dark windows. Empty hand. You keep what you had.",
        ),
    ),
)

# A treat line sometimes picks up one of these. Tricks stay as written.
AMBIENT_EXTRAS: tuple[str, ...] = (
    "Somewhere, a porch light hums.",
    "A distant howl.",
    "Leaves skitter across the sidewalk.",
    "A door creaks shut behind you.",
    "Something giggles in the hedge.",
    "A loose shutter taps once.",
)
AMBIENT_CHANCE = 0.05


@dataclass(frozen=True)
class CandyHuntSettings:
    """Schedule and tunables. Override pieces from server_configs candy_hunt."""

    guild_id: int = MTS_GUILD_ID
    owner_id: int = NICK_ID
    timezone_name: str = "America/Los_Angeles"
    event_date: date = date(2026, 10, 31)
    window_start_minute: int = 10 * 60
    # Minutes after local midnight of event_date. 30*60 is 06:00 the next day.
    window_end_minute: int = 30 * 60
    min_interval: timedelta = timedelta(minutes=30)
    max_interval: timedelta = timedelta(minutes=60)
    drop_expiry: timedelta = timedelta(minutes=10)
    wrap_up_at: datetime = field(default_factory=lambda: datetime(2026, 11, 1, 10, 0, tzinfo=PT))
    pumpkin_god_duration: timedelta = timedelta(days=7)
    wrap_up_late_limit: timedelta = timedelta(days=3)
    announcement_channel_name: str = "announcements"
    announcement_channel_id: int | None = None
    skip_channel_ids: frozenset[int] = field(default_factory=frozenset)
    activity_window: timedelta = timedelta(hours=2)
    trick_chance: float = 0.12
    pumpkin_god_role_name: str = "Pumpkin God"
    # Each claimed drop multiplies the gap by this. 1.0 disables the shrink.
    streak_factor: float = 0.6
    streak_floor_low: timedelta = timedelta(minutes=12)
    streak_floor_high: timedelta = timedelta(minutes=15)
    # From late_ramp_start onward both ends of the gap use this floor.
    # It never raises an interval above the configured base min/max.
    late_floor: timedelta = timedelta(minutes=1)
    # 18:00 local. x1.0 here, and the 1-minute floor starts at this same clock.
    late_ramp_start_minute: int = 18 * 60
    # 21:00 local. x0.5 from here through the window end (06:00 the next day).
    late_ramp_full_minute: int = 21 * 60
    late_ramp_factor: float = 0.5

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


# /candy is the only candy slash command, guild-scoped to these servers.
# Owner controls are the hidden prefix group !candyadmin, not a slash command.
SUPPORT_GUILD_ID = 1045144253627637830
SUPPORT_TEST_CHANNEL_ID = 1045149015756521493
CANDY_GUILD_IDS = (MTS_GUILD_ID, SUPPORT_GUILD_ID)
# Test-session drops expire after min(drop_expiry, interval, TEST_DROP_EXPIRY)
# so a 2-minute support cadence does not leave a 10-minute live pumpkin up.
TEST_DROP_EXPIRY = timedelta(minutes=2)
TEST_INTERVAL_JITTER = 0.20
# A real claim may pull the saved next drop forward, but not inside this lead
# and not past the window. 60 seconds is the 1-minute late floor: the next drop
# is never placed sooner than 60 seconds after the claim, even when the streak
# has already shrunk the gap to one minute. Test sessions use a shorter lead
# and the session end.
REAL_CLAIM_LEAD = timedelta(seconds=60)
TEST_CLAIM_LEAD = timedelta(seconds=30)
TEST_SESSION_MIN_MINUTES = 1
TEST_SESSION_MAX_MINUTES = 240
TEST_SESSION_MIN_INTERVAL = 1
TEST_SESSION_MAX_INTERVAL = 60
TEST_SESSION_SOURCES = frozenset({"command", "autostart"})
CANDYADMIN_USAGE = (
    "Usage:\n"
    "!candyadmin status\n"
    "!candyadmin power on|off\n"
    "!candyadmin test_session [minutes=15] [interval=2] [#channel]\n"
    "!candyadmin test_session_stop\n"
    "!candyadmin wrapup [true|false]  (false posts the real Nov 1 announcement, MTS only)"
)
CANDYADMIN_POWER_USAGE = "Usage: !candyadmin power on|off"
CANDYADMIN_TEST_USAGE = (
    "Usage: !candyadmin test_session [minutes=15] [interval=2] [#channel] "
    "(minutes 1-240, interval 1-60)"
)
CANDYADMIN_WRAPUP_USAGE = (
    "Usage: !candyadmin wrapup [true|false]  "
    "(false posts the real Nov 1 announcement, MTS only)"
)
CANDYADMIN_TEST_DM = "Test sessions need a server channel. Pass a channel mention or ID."
CANDYADMIN_TEST_CHANNEL = "Pick a text channel for the test session."
CANDYADMIN_TEST_MTS = "Test sessions stay out of Menaces to Sobriety."
CANDYADMIN_TEST_BUSY = "A test session is already running. Use !candyadmin test_session_stop first."
# Real wrap-up is irreversible. Only an explicit false posts it.
# off/no/0 read like "turn wrap-up off" and must show usage instead.
_DRY_RUN_TRUE = frozenset({"true", "dry_run=true", "preview"})
_DRY_RUN_FALSE = frozenset({"false", "dry_run=false"})

CANDY_HUNT = CandyHuntSettings()


@dataclass(frozen=True)
class CandyRoll:
    kind: str
    name: str
    amount: int
    blurb: str


@dataclass(frozen=True)
class ClaimOutcome:
    status: str
    mutated: bool
    drop: dict | None = None
    roll: CandyRoll | None = None
    total: int = 0
    rank: int = 0
    players: int = 0
    dry_run: bool = False
    user_id: int = 0


@dataclass(frozen=True)
class ScheduleDecision:
    action: str
    next_drop_at: datetime | None


@dataclass(frozen=True)
class ChannelSnapshot:
    channel_id: int
    name: str
    is_text: bool = True
    is_news: bool = False
    is_nsfw: bool = False
    bot_can_view: bool = True
    bot_can_send: bool = True
    everyone_can_view: bool = True
    everyone_can_send: bool = True
    # A broad member role can view and send. Staff-sized roles do not set this.
    broad_role_can_chat: bool = False
    is_rules_channel: bool = False
    is_announce_channel: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class ChannelActivity:
    channel_id: int
    last_human_at: datetime | None = None


@dataclass(frozen=True)
class WrapResult:
    status: str
    view: discord.ui.LayoutView | None = None
    winner_id: int | None = None


def _log_info(message: str) -> None:
    log.info("%s", message.replace("\r", " ").replace("\n", " "))


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return as_utc(moment).isoformat()


def parse_dt(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return as_utc(moment)


def parse_optional_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_dt(value)
    except ValueError:
        return None


def local_wall_time(day: date, minutes: int, zone: ZoneInfo) -> datetime:
    """Wall clock `minutes` after local midnight of `day`.

    The calendar day and clock are built in `zone`. Adding a timedelta to
    midnight would measure a duration across a DST change instead of a
    clock time. 24:00 is the next midnight. 30:00 is 06:00 the next day.
    An ambiguous fall-back hour uses the first occurrence. A spring-forward
    gap is returned as the real instant that zone maps that clock to.
    """
    if minutes < 0:
        raise ValueError("minutes must be non-negative")
    day_offset, minute_of_day = divmod(int(minutes), 24 * 60)
    hour, minute = divmod(minute_of_day, 60)
    target = day + timedelta(days=day_offset)
    moment = datetime(target.year, target.month, target.day, hour, minute, tzinfo=zone)
    normalized = moment.astimezone(timezone.utc).astimezone(zone)
    if (
        normalized.year == moment.year
        and normalized.month == moment.month
        and normalized.day == moment.day
        and normalized.hour == hour
        and normalized.minute == minute
    ):
        return moment
    return normalized


def window_bounds(settings: CandyHuntSettings) -> tuple[datetime, datetime]:
    zone = settings.zone
    start = local_wall_time(settings.event_date, settings.window_start_minute, zone)
    end = local_wall_time(settings.event_date, settings.window_end_minute, zone)
    return start, end


def is_in_drop_window(settings: CandyHuntSettings, now: datetime) -> bool:
    start, end = window_bounds(settings)
    moment = as_utc(now)
    return as_utc(start) <= moment < as_utc(end)


def minutes_from_event_midnight(settings: CandyHuntSettings, origin: datetime) -> float:
    """Wall-clock minutes from local midnight of the event date.

    The next calendar day is 24*60 plus that day's clock, so a time after
    midnight stays past 24*60 across a DST change. With the default full
    ramp at 21:00, those minutes are already at the late factor.
    """
    local = as_utc(origin).astimezone(settings.zone)
    day_delta = (local.date() - settings.event_date).days
    return (
        day_delta * 24 * 60
        + local.hour * 60
        + local.minute
        + local.second / 60.0
        + local.microsecond / 60_000_000.0
    )


def _streak_value(value: object) -> int:
    """Non-negative streak. Missing and garbage values become 0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value < 0:
            return 0
        if value != int(value):
            return 0
        return int(value)
    if isinstance(value, str):
        try:
            number = int(value)
        except ValueError:
            return 0
        return number if number > 0 else 0
    return 0


def streak_factor(streak: int, settings: CandyHuntSettings) -> float:
    """Compounding shrink for consecutive claims. Zero claims is 1."""
    base = float(settings.streak_factor)
    if not 0.0 < base <= 1.0:
        base = 0.6
    return base ** _streak_value(streak)


def late_night_factor(settings: CandyHuntSettings, origin: datetime) -> float:
    """1.0 before the ramp, then a straight line down to the late factor.

    Minutes are wall-clock minutes from local midnight of the event date.
    The default line is x1.0 at 18:00 and x0.5 at 21:00. A clock at or
    after the full-ramp time uses the late factor. Minutes past 24*60
    (after midnight) are past the default full ramp at 21:00, so they stay
    on that factor through the end of the window. A configured full ramp
    after midnight keeps the line going until that clock.
    """
    minutes = minutes_from_event_midnight(settings, origin)
    start = float(settings.late_ramp_start_minute)
    full = float(settings.late_ramp_full_minute)
    floor = float(settings.late_ramp_factor)
    if not 0.0 < floor <= 1.0:
        floor = 0.5
    if full <= start or minutes < start:
        return 1.0
    if minutes >= full:
        return floor
    progress = (minutes - start) / (full - start)
    return 1.0 + (floor - 1.0) * progress


def _active_floors(settings: CandyHuntSettings, origin: datetime) -> tuple[timedelta, timedelta]:
    """Daytime floors before the late ramp, then `late_floor` for both ends.

    The switch is `late_ramp_start` (18:00 by default) through the window
    end. Before that the 12 and 15 minute streak floors apply. A floor is
    capped by the configured base so it never raises a gap.
    """
    if minutes_from_event_midnight(settings, origin) >= float(settings.late_ramp_start_minute):
        late = settings.late_floor
        return min(late, settings.min_interval), min(late, settings.max_interval)
    return (
        min(settings.streak_floor_low, settings.min_interval),
        min(settings.streak_floor_high, settings.max_interval),
    )


def effective_interval(
    settings: CandyHuntSettings,
    streak: int,
    origin: datetime,
) -> tuple[timedelta, timedelta]:
    """Shrunk min/max gap. Floors never raise an interval above the base."""
    shrink = streak_factor(streak, settings) * late_night_factor(settings, origin)
    if shrink >= 1.0:
        low = settings.min_interval
        high = settings.max_interval
    else:
        floor_low, floor_high = _active_floors(settings, origin)
        low = max(floor_low, settings.min_interval * shrink)
        high = max(floor_high, settings.max_interval * shrink)
    if high < low:
        high = low
    return low, high


def real_drop_lifetime(settings: CandyHuntSettings, streak: int, now: datetime) -> timedelta:
    """How long one scheduled real drop stays claimable.

    ``min(drop_expiry, max(2 minutes, twice the high end of the effective
    interval))``, from the streak and the post time. The lifetime is not
    cut off at the hunt window: a drop posted at 05:59 may expire after
    06:00 and can still be claimed until it expires.
    """
    _low, high = effective_interval(settings, streak, now)
    scaled = high * 2
    shortest = timedelta(minutes=2)
    if scaled < shortest:
        scaled = shortest
    if scaled > settings.drop_expiry:
        return settings.drop_expiry
    return scaled


def compute_next_drop_at(
    origin: datetime,
    *,
    window_end: datetime,
    min_interval: timedelta,
    max_interval: timedelta,
    rng: random.Random,
    first: bool,
) -> datetime | None:
    """Next instant inside [origin, window_end). `first` may land on origin."""
    origin_utc = as_utc(origin)
    end = as_utc(window_end)
    latest = end - timedelta(seconds=1)
    if latest <= origin_utc:
        return None
    if first:
        delay_low = 0.0
        high = min(origin_utc + max_interval, latest)
    else:
        earliest = origin_utc + min_interval
        if earliest > latest:
            return None
        delay_low = min_interval.total_seconds()
        high = min(origin_utc + max_interval, latest)
        if high < earliest:
            return None
    delay_high = (high - origin_utc).total_seconds()
    if delay_high < delay_low:
        return None
    delay = delay_low if delay_high == delay_low else rng.uniform(delay_low, delay_high)
    candidate = origin_utc + timedelta(seconds=delay)
    if candidate >= end:
        return None
    return candidate


def pull_earlier(
    existing: datetime | None,
    candidate: datetime | None,
    now: datetime,
    deadline: datetime | None,
    *,
    lead: timedelta,
) -> datetime | None:
    """Move a pending instant earlier. None stays None.

    The result is never before now+lead, never later than a pending time that
    is already at least `lead` ahead, and never at or after `deadline`.
    """
    if existing is None:
        return None
    moment = as_utc(now)
    current = as_utc(existing)
    earliest = moment + lead
    end = None if deadline is None else as_utc(deadline)
    chosen = current
    if candidate is not None and as_utc(candidate) < chosen:
        chosen = as_utc(candidate)
    if chosen < earliest:
        chosen = earliest
    if current >= earliest and chosen > current:
        chosen = current
    if chosen < moment:
        return None
    if end is not None and chosen >= end:
        if moment <= current < end:
            return current
        return None
    return chosen


def resolve_schedule(
    *,
    now: datetime,
    persisted: datetime | None,
    window_start: datetime,
    window_end: datetime,
    min_interval: timedelta,
    max_interval: timedelta,
    rng: random.Random,
    settings: CandyHuntSettings | None = None,
    streak: int = 0,
) -> ScheduleDecision:
    """One drop when a saved time is due. A restart keeps a future time as-is."""
    moment = as_utc(now)
    start = as_utc(window_start)
    end = as_utc(window_end)
    saved = None if persisted is None else as_utc(persisted)

    def bounds(origin: datetime) -> tuple[timedelta, timedelta]:
        if settings is None:
            return min_interval, max_interval
        return effective_interval(settings, streak, origin)

    if moment >= end or end <= start:
        return ScheduleDecision("idle", None)

    if saved is not None and start <= saved < end:
        if saved > moment:
            return ScheduleDecision("wait", saved)
        if start <= moment < end:
            low, high = bounds(moment)
            following = compute_next_drop_at(
                moment,
                window_end=end,
                min_interval=low,
                max_interval=high,
                rng=rng,
                first=False,
            )
            return ScheduleDecision("drop", following)

    if moment < start:
        low, high = bounds(start)
        first = compute_next_drop_at(
            start,
            window_end=end,
            min_interval=low,
            max_interval=high,
            rng=rng,
            first=True,
        )
        if first is None:
            return ScheduleDecision("idle", None)
        return ScheduleDecision("wait", first)

    low, high = bounds(moment)
    nxt = compute_next_drop_at(
        moment,
        window_end=end,
        min_interval=low,
        max_interval=high,
        rng=rng,
        first=False,
    )
    if nxt is None:
        return ScheduleDecision("idle", None)
    return ScheduleDecision("wait", nxt)


# Share of human members a role needs before it counts as "people can chat here".
# Smaller roles (staff, mods) stay excluded so those rooms never get drops.
BROAD_ROLE_MIN_SHARE = 0.5


def broad_role_ids(
    holders_by_role: dict[int, int],
    human_count: int,
    *,
    managed_role_ids: set[int] | frozenset[int] | None = None,
    everyone_role_id: int | None = None,
    min_share: float = BROAD_ROLE_MIN_SHARE,
) -> frozenset[int]:
    """Non-managed roles, other than @everyone, held by at least `min_share` of humans.

    `holders_by_role` counts human members only. No humans means no broad role.
    """
    if human_count <= 0:
        return frozenset()
    managed = frozenset(managed_role_ids or ())
    cutoff = human_count * min_share
    chosen: set[int] = set()
    for role_id, holders in holders_by_role.items():
        if role_id == everyone_role_id or role_id in managed or holders <= 0:
            continue
        if holders >= cutoff:
            chosen.add(role_id)
    return frozenset(chosen)


def members_can_chat(channel: ChannelSnapshot) -> bool:
    """True when members can see the channel and post in it.

    @everyone qualifies on its own. Otherwise a broad role (see `broad_role_ids`)
    must be able to view and send. A channel only a small role can post in does not.
    """
    if channel.everyone_can_view and channel.everyone_can_send:
        return True
    return channel.broad_role_can_chat


def channel_block_reason(channel: ChannelSnapshot) -> str | None:
    if not channel.is_text:
        return "not_text"
    if channel.is_news:
        return "news"
    if channel.is_nsfw:
        return "nsfw"
    name = channel.name.casefold()
    if channel.is_rules_channel or "rules" in name:
        return "rules"
    if channel.is_announce_channel or "announcement" in name:
        return "announcements"
    if channel.skipped:
        return "skip_list"
    if not members_can_chat(channel):
        return "read_only"
    if not channel.bot_can_view:
        return "no_view"
    if not channel.bot_can_send:
        return "no_send"
    return None


def is_eligible(channel: ChannelSnapshot) -> bool:
    return channel_block_reason(channel) is None


def weighted_choice(items: list, weights: list[float], rng: random.Random):
    if not items:
        raise ValueError("nothing to choose")
    total = float(sum(weights))
    if total <= 0:
        return items[-1]
    roll = float(rng.random()) * total
    acc = 0.0
    for item, weight in zip(items, weights):
        acc += float(weight)
        if roll < acc:
            return item
    return items[-1]


def pick_channel_id(
    channels: list[ChannelActivity],
    now: datetime,
    rng: random.Random,
    *,
    activity_window: timedelta,
) -> int | None:
    """Prefer channels humans used inside the window. Otherwise any candidate."""
    if not channels:
        return None
    moment = as_utc(now)
    window_s = activity_window.total_seconds()
    active: list[ChannelActivity] = []
    for channel in channels:
        if channel.last_human_at is None:
            continue
        age = (moment - as_utc(channel.last_human_at)).total_seconds()
        if 0 <= age <= window_s:
            active.append(channel)
    pool = active if active else list(channels)
    pool = sorted(pool, key=lambda channel: channel.channel_id)
    weights: list[float] = []
    for channel in pool:
        if channel.last_human_at is None or window_s <= 0:
            weights.append(1.0)
            continue
        age = (moment - as_utc(channel.last_human_at)).total_seconds()
        if age < 0 or age > window_s:
            weights.append(1.0)
        else:
            weights.append(1.0 + (1.0 - age / window_s))
    return int(weighted_choice([channel.channel_id for channel in pool], weights, rng))


def find_announcement_channel(
    channels: list[tuple[int, str]],
    *,
    channel_id: int | None,
    name: str,
) -> int | None:
    by_id = {cid: cname for cid, cname in channels}
    if channel_id is not None and channel_id in by_id:
        return channel_id
    wanted = name.casefold()
    exact = [cid for cid, cname in channels if cname.casefold() == wanted]
    if exact:
        return exact[0]
    partial = [cid for cid, cname in channels if wanted in cname.casefold()]
    if partial:
        return partial[0]
    return None


def _line_overlaps(line: str, extra: str) -> bool:
    left = " ".join(line.casefold().split())
    right = " ".join(extra.casefold().split())
    if not right or right in left or left in right:
        return True
    # The porch-light hum is one joke. Don't tell it twice.
    return "porch light hum" in left and "porch light hum" in right


def pick_blurb(rng: random.Random, blurbs: tuple[str, ...]) -> str:
    if not blurbs:
        return ""
    if len(blurbs) == 1:
        return blurbs[0]
    return str(rng.choice(blurbs))


def with_ambient(rng: random.Random, line: str) -> str:
    """Maybe append one ambient extra to a treat line. Tricks do not call this."""
    if not AMBIENT_EXTRAS or rng.random() >= AMBIENT_CHANCE:
        return line
    extra = str(rng.choice(AMBIENT_EXTRAS))
    if _line_overlaps(line, extra):
        return line
    return f"{line} {extra}"


def roll_candy(
    rng: random.Random,
    *,
    trick_chance: float,
    treats: tuple[Treat, ...] = TREATS,
    tricks: tuple[Trick, ...] = TRICKS,
) -> CandyRoll:
    if tricks and rng.random() < trick_chance:
        trick = rng.choice(tricks)
        return CandyRoll("trick", "Trick", trick.amount, pick_blurb(rng, trick.blurbs))
    treat = weighted_choice(list(treats), [item.weight for item in treats], rng)
    line = with_ambient(rng, pick_blurb(rng, treat.blurbs))
    return CandyRoll("treat", treat.name, treat.amount, line)


def apply_amount(total: int, amount: int) -> int:
    return max(0, int(total) + int(amount))


def rank_for(totals: dict, user_id: int) -> tuple[int, int, int]:
    """Competition rank, this user's total, and how many totals exist.

    Ties share the better rank: 10, 10, 4 is ranks 1, 1, 3.
    """
    scores = {str(key): int(value) for key, value in totals.items()}
    total = scores.get(str(user_id), 0)
    better = sum(1 for value in scores.values() if value > total)
    return better + 1, total, len(scores)


def leaderboard_rows(totals: dict, limit: int = 10) -> list[tuple[int, str, int]]:
    ordered = sorted(
        ((str(uid), int(total)) for uid, total in totals.items() if int(total) > 0),
        key=lambda item: (-item[1], int(item[0])),
    )
    rows: list[tuple[int, str, int]] = []
    for uid, _total in ordered[:limit]:
        rank, total, _players = rank_for(totals, int(uid))
        rows.append((rank, uid, total))
    return rows


def podium(totals: dict, limit: int = 3) -> list[tuple[str, int]]:
    ordered = sorted(
        ((str(uid), int(total)) for uid, total in totals.items() if int(total) > 0),
        key=lambda item: (-item[1], int(item[0])),
    )
    return ordered[:limit]


def has_players(state: dict) -> bool:
    return any(int(value) > 0 for value in (state.get("totals") or {}).values())


def had_activity(state: dict) -> bool:
    if int(state.get("drops_posted") or 0) > 0:
        return True
    if int(state.get("claims_total") or 0) > 0:
        return True
    return has_players(state)


def wrap_up_decision(
    *,
    now: datetime,
    wrap_up_at: datetime,
    done: bool,
    had_activity: bool,
    catching_up: bool,
    late_limit: timedelta,
    has_players: bool,
) -> str:
    """waiting, done, too_late, skip_quiet, nobody, or announce.

    A process that wakes up after the deadline only announces when the event
    actually happened, and never when it is more than `late_limit` late.
    An on-time empty event still gets the short nobody note.
    """
    if done:
        return "done"
    moment = as_utc(now)
    due = as_utc(wrap_up_at)
    if moment < due:
        return "waiting"
    if moment - due > late_limit:
        return "too_late"
    if catching_up and not had_activity:
        return "skip_quiet"
    if not has_players:
        return "nobody"
    return "announce"


def role_removal_due(pumpkin_god: dict | None, now: datetime) -> bool:
    if not pumpkin_god:
        return False
    remove_at = parse_optional_dt(pumpkin_god.get("remove_at"))
    if remove_at is None:
        return False
    return as_utc(now) >= remove_at


def split_stale_drops(drops: list[dict], now: datetime) -> tuple[list[dict], list[dict], list[dict]]:
    """Still-active drops, ones that just expired, and claimed/expired leftovers."""
    keep: list[dict] = []
    expired: list[dict] = []
    finished: list[dict] = []
    moment = as_utc(now)
    for drop in drops:
        status = drop.get("status")
        if status != "active":
            finished.append(dict(drop))
            continue
        expires = parse_optional_dt(drop.get("expires_at"))
        if expires is None or expires <= moment:
            updated = dict(drop)
            updated["status"] = "expired"
            expired.append(updated)
        else:
            keep.append(drop)
    return keep, expired, finished


def reset_streaks_for_expiry(state: dict, drops: list[dict]) -> None:
    """Clear the streak that an unclaimed expiry belongs to. Mutates `state`.

    A real drop clears the event streak. A dry-run drop clears the test
    session streak and leaves the event streak alone.
    """
    saw_real = False
    saw_test = False
    for drop in drops:
        if not isinstance(drop, dict):
            continue
        if drop.get("dry_run"):
            saw_test = True
        else:
            saw_real = True
    if saw_real:
        state["streak"] = 0
    if saw_test:
        test = state.get("test")
        session = test.get("session") if isinstance(test, dict) else None
        if isinstance(session, dict):
            session["streak"] = 0


def _bump_real_streak(
    state: dict,
    drop: dict,
    now: datetime,
    rng: random.Random,
    settings: CandyHuntSettings | None,
) -> None:
    """Count a real claim and pull the saved next drop earlier when we can.

    ``pull_earlier`` keeps the result at least ``REAL_CLAIM_LEAD`` (60s) after
    the claim. That matches the 1-minute late floor, so a claim cannot schedule
    the next drop sooner than 60 seconds after itself.
    """
    state["streak"] = _streak_value(state.get("streak")) + 1
    if settings is None:
        return
    existing = parse_optional_dt(state.get("next_drop_at"))
    if existing is None:
        return
    origin = parse_optional_dt(drop.get("created_at")) or now
    _start, end = window_bounds(settings)
    low, high = effective_interval(settings, state["streak"], origin)
    candidate = compute_next_drop_at(
        origin,
        window_end=end,
        min_interval=low,
        max_interval=high,
        rng=rng,
        first=False,
    )
    pulled = pull_earlier(existing, candidate, now, end, lead=REAL_CLAIM_LEAD)
    state["next_drop_at"] = None if pulled is None else iso(pulled)


def _bump_test_streak(
    state: dict,
    drop: dict,
    now: datetime,
    rng: random.Random,
    settings: CandyHuntSettings | None,
) -> None:
    """Count a test-session claim and pull that session's next drop earlier."""
    test = state.get("test")
    if not isinstance(test, dict):
        return
    session = test.get("session")
    if not isinstance(session, dict):
        return
    session["streak"] = _streak_value(session.get("streak")) + 1
    existing = parse_optional_dt(session.get("next_drop_at"))
    if existing is None:
        return
    try:
        interval = int(session.get("interval"))
    except (TypeError, ValueError):
        interval = TEST_SESSION_MIN_INTERVAL
    if isinstance(session.get("interval"), bool):
        interval = TEST_SESSION_MIN_INTERVAL
    origin = parse_optional_dt(drop.get("created_at")) or now
    pace = settings if settings is not None else CANDY_HUNT
    delay = jittered_delay_seconds(interval, rng, factor=streak_factor(session["streak"], pace))
    candidate = as_utc(origin) + timedelta(seconds=delay)
    ends = parse_optional_dt(session.get("ends_at"))
    pulled = pull_earlier(existing, candidate, now, ends, lead=TEST_CLAIM_LEAD)
    session["next_drop_at"] = None if pulled is None else iso(pulled)


def apply_claim(
    state: dict,
    drop_id: str,
    user_id: int,
    now: datetime,
    rng: random.Random,
    *,
    is_bot: bool,
    trick_chance: float,
    settings: CandyHuntSettings | None = None,
) -> tuple[dict, ClaimOutcome]:
    """Check-and-set for one drop. The input state is not mutated."""

    def outcome(status: str, **kwargs) -> ClaimOutcome:
        return ClaimOutcome(status=status, user_id=user_id, **kwargs)

    if is_bot:
        return state, outcome("bot", mutated=False)

    current = next((drop for drop in state.get("active_drops") or [] if drop.get("drop_id") == drop_id), None)
    if current is None:
        return state, outcome("missing", mutated=False)

    dry_run = bool(current.get("dry_run"))
    status = current.get("status")
    if status == "expired":
        return state, outcome("expired", mutated=False, drop=dict(current), dry_run=dry_run)
    if status != "active":
        return state, outcome("already", mutated=False, drop=dict(current), dry_run=dry_run)

    expires = parse_optional_dt(current.get("expires_at"))
    state = copy.deepcopy(state)
    drop = next(item for item in state["active_drops"] if item["drop_id"] == drop_id)
    if expires is None or expires <= as_utc(now):
        drop["status"] = "expired"
        reset_streaks_for_expiry(state, [drop])
        return state, outcome("expired", mutated=True, drop=drop, dry_run=dry_run)

    roll = roll_candy(rng, trick_chance=trick_chance)
    if dry_run:
        bucket = state.setdefault("test", default_test())
    else:
        bucket = state
    bucket.setdefault("totals", {})
    bucket.setdefault("claim_counts", {})
    key = str(user_id)
    old = int(bucket["totals"].get(key, 0))
    bucket["totals"][key] = apply_amount(old, roll.amount)
    bucket["claim_counts"][key] = int(bucket["claim_counts"].get(key, 0)) + 1
    bucket["claims_total"] = int(bucket.get("claims_total", 0)) + 1
    drop["status"] = "claimed"
    drop["claimed_by"] = int(user_id)
    drop["claimed_at"] = iso(now)
    if dry_run:
        _bump_test_streak(state, drop, now, rng, settings)
    else:
        _bump_real_streak(state, drop, now, rng, settings)
    rank, total, players = rank_for(bucket["totals"], user_id)
    return state, outcome(
        "won",
        mutated=True,
        drop=drop,
        roll=roll,
        total=total,
        rank=rank,
        players=players,
        dry_run=dry_run,
    )


def _nonneg_int(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _int_map(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for key, value in raw.items():
        try:
            user_id = int(key)
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if user_id < 0:
            continue
        cleaned[str(user_id)] = max(0, amount)
    return cleaned


def _drop_record(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    drop_id = raw.get("drop_id")
    if not isinstance(drop_id, str) or len(drop_id) != 8 or any(ch not in "0123456789abcdef" for ch in drop_id):
        return None
    status = raw.get("status")
    if status not in {"active", "claimed", "expired"}:
        return None
    created = parse_optional_dt(raw.get("created_at"))
    expires = parse_optional_dt(raw.get("expires_at"))
    if created is None or expires is None:
        return None
    try:
        channel_id = int(raw["channel_id"])
    except (KeyError, TypeError, ValueError):
        return None
    message_id = raw.get("message_id")
    if message_id is not None:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            message_id = None
    claimed_by = raw.get("claimed_by")
    if claimed_by is not None:
        try:
            claimed_by = int(claimed_by)
        except (TypeError, ValueError):
            claimed_by = None
    record = {
        "drop_id": drop_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "created_at": iso(created),
        "expires_at": iso(expires),
        "status": status,
        "dry_run": _as_bool(raw.get("dry_run")),
        "claimed_by": claimed_by,
    }
    claimed_at = parse_optional_dt(raw.get("claimed_at"))
    if claimed_at is not None:
        record["claimed_at"] = iso(claimed_at)
    return record


def default_test() -> dict:
    return {
        "totals": {},
        "claim_counts": {},
        "claims_total": 0,
        "drops_posted": 0,
        "session": None,
    }


def default_state() -> dict:
    return {
        "totals": {},
        "claim_counts": {},
        "claims_total": 0,
        "active_drops": [],
        "next_drop_at": None,
        "drops_posted": 0,
        "wrap_up_done": False,
        "pumpkin_god": None,
        "event_disabled": False,
        "streak": 0,
        "test": default_test(),
    }


def _pumpkin(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    remove_at = parse_optional_dt(raw.get("remove_at"))
    if remove_at is None:
        return None
    try:
        return {
            "user_id": int(raw["user_id"]),
            "role_id": int(raw["role_id"]),
            "guild_id": int(raw["guild_id"]),
            "remove_at": iso(remove_at),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _test_bucket(raw: object) -> dict:
    bucket = default_test()
    if not isinstance(raw, dict):
        return bucket
    bucket["totals"] = _int_map(raw.get("totals"))
    bucket["claim_counts"] = _int_map(raw.get("claim_counts"))
    bucket["claims_total"] = _nonneg_int(raw.get("claims_total"))
    bucket["drops_posted"] = _nonneg_int(raw.get("drops_posted"))
    bucket["session"] = normalize_test_session(raw.get("session"))
    return bucket


def validate_test_session_args(minutes: object, interval: object) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not (
        TEST_SESSION_MIN_MINUTES <= minutes <= TEST_SESSION_MAX_MINUTES
    ):
        return f"Minutes must be between {TEST_SESSION_MIN_MINUTES} and {TEST_SESSION_MAX_MINUTES}."
    if isinstance(interval, bool) or not isinstance(interval, int) or not (
        TEST_SESSION_MIN_INTERVAL <= interval <= TEST_SESSION_MAX_INTERVAL
    ):
        return (
            f"Interval must be between {TEST_SESSION_MIN_INTERVAL} "
            f"and {TEST_SESSION_MAX_INTERVAL} minutes."
        )
    return None


def _parse_session_int(value: object, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        return int(text)
    return None


def _parse_channel_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("<#") and text.endswith(">"):
        text = text[2:-1].strip()
    if not text.isdigit():
        return None
    parsed = int(text)
    return parsed if parsed > 0 else None


def _parse_power(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    if token in {"on", "off"}:
        return token
    return None


def _parse_dry_run(value: object) -> bool | None:
    """Preview flag. Omitted means preview. Only false runs for real.

    ``true``, ``dry_run=true``, and ``preview`` preview. ``false`` and
    ``dry_run=false`` post the real announcement. Any other token returns
    None so the caller shows usage and does nothing.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _DRY_RUN_TRUE:
            return True
        if token in _DRY_RUN_FALSE:
            return False
    return None


def _command_is_candyadmin(command) -> bool:
    seen: set[int] = set()
    current = command
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "parent", None) is None:
            return getattr(current, "name", None) == "candyadmin"
        current = getattr(current, "parent", None)
    return False


async def _candyadmin_allowed(ctx) -> bool:
    """Owner, and only in a candy guild or a DM with the bot."""
    author = getattr(ctx, "author", None)
    author_id = getattr(author, "id", None)
    cog = getattr(ctx, "cog", None)
    if cog is None:
        cog = getattr(getattr(ctx, "command", None), "cog", None)
    settings = getattr(cog, "settings", None)
    owner_id = getattr(settings, "owner_id", NICK_ID)
    if author_id != owner_id:
        is_owner = getattr(getattr(ctx, "bot", None), "is_owner", None)
        allowed = False
        if callable(is_owner):
            try:
                allowed = bool(await is_owner(author))
            except Exception:
                allowed = False
        if not allowed:
            return False
    guild = getattr(ctx, "guild", None)
    if guild is None:
        return True
    return getattr(guild, "id", None) in CANDY_GUILD_IDS


_candyadmin_check = commands.check(_candyadmin_allowed)


def jittered_delay_seconds(
    interval_minutes: float,
    rng: random.Random,
    *,
    jitter: float = TEST_INTERVAL_JITTER,
    factor: float = 1.0,
) -> float:
    """Interval with +/- jitter, never under one minute.

    A 2-minute setting lands between 96s and 144s. A 1-minute setting is
    clamped up to 60s when the low side would dip under a minute.
    `factor` scales the interval first (a test streak uses 0.6**streak).
    The one-minute floor applies to that scaled length, before the jitter.
    """
    try:
        scaled = float(interval_minutes) * float(factor)
    except (TypeError, ValueError):
        scaled = float(TEST_SESSION_MIN_INTERVAL)
    if scaled < TEST_SESSION_MIN_INTERVAL:
        scaled = float(TEST_SESSION_MIN_INTERVAL)
    base = scaled * 60.0
    low = base * (1.0 - jitter)
    high = base * (1.0 + jitter)
    rolled = low if high <= low else float(rng.uniform(low, high))
    return max(60.0, rolled)


def session_drop_lifetime(drop_expiry: timedelta, interval: timedelta) -> timedelta:
    """How long a test-session drop stays up.

    Lifetime is min(the live drop expiry, the session interval, TEST_DROP_EXPIRY).
    TEST_DROP_EXPIRY is 2 minutes.
    """
    return min(drop_expiry, interval, TEST_DROP_EXPIRY)


def make_test_session(
    *,
    guild_id: int,
    channel_id: int,
    minutes: int,
    interval: int,
    now: datetime,
    source: str,
) -> dict:
    moment = as_utc(now)
    return {
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "minutes": int(minutes),
        "interval": int(interval),
        "started_at": iso(moment),
        "ends_at": iso(moment + timedelta(minutes=int(minutes))),
        "next_drop_at": iso(moment),
        "drops_posted": 0,
        "source": source if source in TEST_SESSION_SOURCES else "command",
        "active": True,
        "streak": 0,
    }


def resolve_test_session(session: dict | None, now: datetime) -> str:
    """idle, wait, drop, or end.

    A missing session is idle. Menaces to Sobriety, or a passed ends_at, ends
    it. A future next_drop_at waits. A due next_drop_at drops. next_drop_at of
    None means the last drop already went out and the session is running down.
    """
    if not isinstance(session, dict) or not session.get("active", True):
        return "idle"
    try:
        guild_id = int(session.get("guild_id") or 0)
    except (TypeError, ValueError):
        return "end"
    if guild_id == MTS_GUILD_ID:
        return "end"
    ends = parse_optional_dt(session.get("ends_at"))
    if ends is None or as_utc(now) >= ends:
        return "end"
    nxt = parse_optional_dt(session.get("next_drop_at"))
    if nxt is None or as_utc(now) < nxt:
        return "wait"
    return "drop"


def normalize_test_session(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        guild_id = int(raw["guild_id"])
        channel_id = int(raw["channel_id"])
        minutes = int(raw["minutes"])
        interval = int(raw["interval"])
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(raw.get("minutes"), bool) or isinstance(raw.get("interval"), bool):
        return None
    if guild_id == MTS_GUILD_ID:
        log.warning("candy test session dropped reason=mts guild=%s", guild_id)
        return None
    if guild_id <= 0 or channel_id <= 0:
        return None
    if validate_test_session_args(minutes, interval):
        return None
    started = parse_optional_dt(raw.get("started_at"))
    ends = parse_optional_dt(raw.get("ends_at"))
    if started is None or ends is None or ends <= started:
        return None
    nxt = parse_optional_dt(raw.get("next_drop_at"))
    source = raw.get("source")
    if source not in TEST_SESSION_SOURCES:
        source = "command"
    if raw.get("active") is False:
        return None
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "minutes": minutes,
        "interval": interval,
        "started_at": iso(started),
        "ends_at": iso(ends),
        "next_drop_at": None if nxt is None else iso(nxt),
        "drops_posted": _nonneg_int(raw.get("drops_posted")),
        "source": source,
        "active": True,
        "streak": _streak_value(raw.get("streak")),
    }


def parse_autostart(raw: object) -> dict | None:
    """Validated flag-file body. MTS is left for the caller to refuse."""
    if not isinstance(raw, dict):
        return None
    try:
        guild_id = int(raw["guild_id"])
        channel_id = int(raw["channel_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(raw.get("guild_id"), bool) or isinstance(raw.get("channel_id"), bool):
        return None
    minutes = raw.get("minutes", 15)
    interval = raw.get("interval", 2)
    if isinstance(minutes, bool) or isinstance(interval, bool):
        return None
    try:
        minutes_i = int(minutes)
        interval_i = int(interval)
    except (TypeError, ValueError):
        return None
    if guild_id <= 0 or channel_id <= 0:
        return None
    if validate_test_session_args(minutes_i, interval_i):
        return None
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "minutes": minutes_i,
        "interval": interval_i,
    }


def read_autostart(path: Path) -> dict | None:
    """Payload from the one-shot flag file, or None when the file is absent.

    A present but unusable file returns ``{"error": "..."}`` so the caller can
    log it and delete the file. Menaces to Sobriety is still a payload; the
    caller refuses that guild and deletes the file.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"error": "unreadable"}
    parsed = parse_autostart(raw)
    if parsed is None:
        return {"error": "invalid"}
    return parsed


def delete_flag_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("candy test session autostart delete failed path=%s", path)


def log_session_ended(session: dict, reason: str) -> None:
    _log_info(
        "candy test session ended "
        f"guild={session.get('guild_id')} channel={session.get('channel_id')} "
        f"drops={int(session.get('drops_posted') or 0)} reason={reason}"
    )


def normalize_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return default_state()
    state = default_state()
    state["totals"] = _int_map(raw.get("totals"))
    state["claim_counts"] = _int_map(raw.get("claim_counts"))
    state["claims_total"] = _nonneg_int(raw.get("claims_total"))
    drops = []
    for item in raw.get("active_drops") or []:
        record = _drop_record(item)
        if record is not None:
            drops.append(record)
    state["active_drops"] = drops
    nxt = parse_optional_dt(raw.get("next_drop_at"))
    state["next_drop_at"] = None if nxt is None else iso(nxt)
    state["drops_posted"] = _nonneg_int(raw.get("drops_posted"))
    state["wrap_up_done"] = _as_bool(raw.get("wrap_up_done"))
    state["event_disabled"] = _as_bool(raw.get("event_disabled"))
    state["pumpkin_god"] = _pumpkin(raw.get("pumpkin_god"))
    state["streak"] = _streak_value(raw.get("streak"))
    state["test"] = _test_bucket(raw.get("test"))
    return state


def make_drop(
    drop_id: str,
    channel_id: int,
    message_id: int | None,
    created_at: datetime,
    expires_at: datetime,
    *,
    dry_run: bool,
) -> dict:
    return {
        "drop_id": drop_id,
        "channel_id": int(channel_id),
        "message_id": None if message_id is None else int(message_id),
        "created_at": iso(created_at),
        "expires_at": iso(expires_at),
        "status": "active",
        "dry_run": bool(dry_run),
        "claimed_by": None,
    }


def new_drop_id(existing: set[str]) -> str:
    for _ in range(8):
        candidate = secrets.token_hex(4)
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not allocate a drop id")


def parse_clock_minutes(value: object) -> int | None:
    """Parse ``HH:MM`` into minutes from local midnight.

    ``00:00``-``23:59`` are the event date. ``24:00`` is the next midnight.
    ``30:00`` is 06:00 the next day. Hours run through ``48:00`` exactly;
    ``48:01`` and a minute of 60 are rejected. Callers that need a same-day
    clock still reject a result at or after ``24:00``.
    """
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    hour_text, minute_text = value.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if hour < 0 or hour > 48 or minute < 0 or minute > 59:
        return None
    if hour == 48 and minute != 0:
        return None
    return hour * 60 + minute


def _bounded_unit(value: object) -> float | None:
    """A factor in (0, 1]. Booleans and non-numbers are rejected."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if 0.0 < number <= 1.0:
        return number
    return None


def _bounded_minutes(value: object, *, high: int = 24 * 60) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != int(value):
            return None
        number = int(value)
    elif isinstance(value, int):
        number = value
    else:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if 1 <= number <= high:
        return number
    return None


def apply_candy_hunt_overrides(base: CandyHuntSettings, raw: object) -> CandyHuntSettings:
    if not isinstance(raw, dict) or not raw:
        return base
    changes: dict = {}
    zone = base.zone

    if "event_date" in raw:
        try:
            changes["event_date"] = date.fromisoformat(str(raw["event_date"]))
        except ValueError:
            log.warning("candy hunt config ignored key=event_date")

    if "window_start" in raw:
        minutes = parse_clock_minutes(raw["window_start"])
        if minutes is None or minutes >= 24 * 60:
            log.warning("candy hunt config ignored key=window_start")
        else:
            changes["window_start_minute"] = minutes

    if "window_end" in raw:
        minutes = parse_clock_minutes(raw["window_end"])
        if minutes is None:
            log.warning("candy hunt config ignored key=window_end")
        else:
            changes["window_end_minute"] = minutes

    start_minute = changes.get("window_start_minute", base.window_start_minute)
    end_minute = changes.get("window_end_minute", base.window_end_minute)
    if end_minute <= start_minute:
        changes.pop("window_start_minute", None)
        changes.pop("window_end_minute", None)
        log.warning("candy hunt config ignored window because the end is not after the start")

    if "min_interval_minutes" in raw or "max_interval_minutes" in raw:
        try:
            minimum = int(raw.get("min_interval_minutes", int(base.min_interval.total_seconds() // 60)))
            maximum = int(raw.get("max_interval_minutes", int(base.max_interval.total_seconds() // 60)))
        except (TypeError, ValueError):
            minimum = maximum = -1
        if 1 <= minimum <= maximum <= 24 * 60:
            changes["min_interval"] = timedelta(minutes=minimum)
            changes["max_interval"] = timedelta(minutes=maximum)
        else:
            log.warning("candy hunt config ignored interval")

    if "drop_expiry_minutes" in raw:
        try:
            minutes = int(raw["drop_expiry_minutes"])
        except (TypeError, ValueError):
            minutes = -1
        if 1 <= minutes <= 180:
            changes["drop_expiry"] = timedelta(minutes=minutes)
        else:
            log.warning("candy hunt config ignored key=drop_expiry_minutes")

    if "wrap_up_at" in raw:
        try:
            moment = datetime.fromisoformat(str(raw["wrap_up_at"]))
        except ValueError:
            moment = None
        if moment is None:
            log.warning("candy hunt config ignored key=wrap_up_at")
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=zone)
            changes["wrap_up_at"] = moment

    if "pumpkin_god_days" in raw:
        try:
            days = int(raw["pumpkin_god_days"])
        except (TypeError, ValueError):
            days = -1
        if 1 <= days <= 30:
            changes["pumpkin_god_duration"] = timedelta(days=days)
        else:
            log.warning("candy hunt config ignored key=pumpkin_god_days")

    if "announcement_channel_id" in raw:
        value = raw["announcement_channel_id"]
        if value is None:
            changes["announcement_channel_id"] = None
        else:
            try:
                changes["announcement_channel_id"] = int(value)
            except (TypeError, ValueError):
                log.warning("candy hunt config ignored key=announcement_channel_id")

    if "announcement_channel_name" in raw:
        name = raw["announcement_channel_name"]
        if isinstance(name, str) and name.strip():
            changes["announcement_channel_name"] = name.strip()
        else:
            log.warning("candy hunt config ignored key=announcement_channel_name")

    if "skip_channel_ids" in raw:
        values = raw["skip_channel_ids"]
        if isinstance(values, list):
            skipped: set[int] = set()
            valid = True
            for item in values:
                try:
                    skipped.add(int(item))
                except (TypeError, ValueError):
                    valid = False
            if valid:
                changes["skip_channel_ids"] = frozenset(skipped)
            else:
                log.warning("candy hunt config ignored key=skip_channel_ids")
        else:
            log.warning("candy hunt config ignored key=skip_channel_ids")

    if "trick_chance" in raw:
        try:
            chance = float(raw["trick_chance"])
        except (TypeError, ValueError):
            chance = -1.0
        if 0.0 <= chance <= 1.0:
            changes["trick_chance"] = chance
        else:
            log.warning("candy hunt config ignored key=trick_chance")

    if "activity_window_hours" in raw:
        try:
            hours = float(raw["activity_window_hours"])
        except (TypeError, ValueError):
            hours = -1.0
        if 0 < hours <= 24:
            changes["activity_window"] = timedelta(hours=hours)
        else:
            log.warning("candy hunt config ignored key=activity_window_hours")

    if "pumpkin_god_role_name" in raw:
        name = raw["pumpkin_god_role_name"]
        if isinstance(name, str) and name.strip() and len(name.strip()) <= 100:
            changes["pumpkin_god_role_name"] = name.strip()
        else:
            log.warning("candy hunt config ignored key=pumpkin_god_role_name")

    if "streak_factor" in raw:
        factor = _bounded_unit(raw["streak_factor"])
        if factor is None:
            log.warning("candy hunt config ignored key=streak_factor")
        else:
            changes["streak_factor"] = factor

    if "late_ramp_factor" in raw:
        factor = _bounded_unit(raw["late_ramp_factor"])
        if factor is None:
            log.warning("candy hunt config ignored key=late_ramp_factor")
        else:
            changes["late_ramp_factor"] = factor

    if "streak_floor_low_minutes" in raw:
        minutes = _bounded_minutes(raw["streak_floor_low_minutes"])
        if minutes is None:
            log.warning("candy hunt config ignored key=streak_floor_low_minutes")
        else:
            changes["streak_floor_low"] = timedelta(minutes=minutes)

    if "streak_floor_high_minutes" in raw:
        minutes = _bounded_minutes(raw["streak_floor_high_minutes"])
        if minutes is None:
            log.warning("candy hunt config ignored key=streak_floor_high_minutes")
        else:
            changes["streak_floor_high"] = timedelta(minutes=minutes)

    if "late_ramp_start" in raw or "late_ramp_full" in raw:
        start_minute = base.late_ramp_start_minute
        full_minute = base.late_ramp_full_minute
        got_start = False
        got_full = False
        if "late_ramp_start" in raw:
            parsed = parse_clock_minutes(raw["late_ramp_start"])
            if parsed is None or parsed >= 24 * 60:
                log.warning("candy hunt config ignored key=late_ramp_start")
            else:
                start_minute = parsed
                got_start = True
        if "late_ramp_full" in raw:
            parsed = parse_clock_minutes(raw["late_ramp_full"])
            # A rejected window pair was popped, so this is the end that will apply.
            end_limit = changes.get("window_end_minute", base.window_end_minute)
            if parsed is None or parsed > end_limit:
                log.warning("candy hunt config ignored key=late_ramp_full")
            else:
                full_minute = parsed
                got_full = True
        if got_start or got_full:
            if full_minute > start_minute:
                if got_start:
                    changes["late_ramp_start_minute"] = start_minute
                if got_full:
                    changes["late_ramp_full_minute"] = full_minute
            else:
                log.warning("candy hunt config ignored late ramp because the full time is not after the start")

    if "late_floor_minutes" in raw:
        minutes = _bounded_minutes(raw["late_floor_minutes"], high=60)
        if minutes is None:
            log.warning("candy hunt config ignored key=late_floor_minutes")
        else:
            changes["late_floor"] = timedelta(minutes=minutes)

    if not changes:
        return base
    updated = replace(base, **changes)
    _warn_if_wrap_up_before_window(updated)
    return updated


def _warn_if_wrap_up_before_window(settings: CandyHuntSettings) -> None:
    """Log when wrap-up is configured earlier than the last drop. Do not raise."""
    try:
        _start, end = window_bounds(settings)
    except (OverflowError, OSError, ValueError):
        log.warning("candy hunt wrap_up_at could not be compared with the window end")
        return
    if as_utc(settings.wrap_up_at) < as_utc(end):
        log.warning(
            "candy hunt wrap_up_at %s is before the window end %s",
            iso(settings.wrap_up_at),
            iso(end),
        )


def load_settings() -> CandyHuntSettings:
    try:
        config = get_guild_config(MTS_GUILD_ID)
    except Exception:
        log.exception("candy hunt config load failed")
        return CANDY_HUNT
    if config is None:
        return CANDY_HUNT
    return apply_candy_hunt_overrides(CANDY_HUNT, config.candy_hunt)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _twelve_hour(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {suffix}"


def _month_day(moment: datetime) -> str:
    return f"{_MONTHS[moment.month - 1]} {moment.day}"


def _window_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    left = as_utc(start).astimezone(zone)
    right = as_utc(end).astimezone(zone)
    left_zone = left.tzname() or ""
    right_zone = right.tzname() or ""
    zone_label = left_zone if left_zone == right_zone else f"{left_zone}/{right_zone}"
    return f"{_month_day(left)} {_twelve_hour(left)} - {_month_day(right)} {_twelve_hour(right)} {zone_label}"


def _ramp_clock(settings: CandyHuntSettings, minutes: int) -> str:
    moment = local_wall_time(settings.event_date, int(minutes), settings.zone)
    local = as_utc(moment).astimezone(settings.zone)
    clock = _twelve_hour(local)
    if minutes < 24 * 60 and local.date() == settings.event_date:
        return clock
    return f"{_month_day(local)} {clock}"


def _floor_line(settings: CandyHuntSettings, now: datetime) -> str:
    low, high = _active_floors(settings, now)
    if low == high:
        return f"Floor: {_format_minutes(low)} min"
    return f"Floor: {_format_minutes(low)}-{_format_minutes(high)} min"


def render_status(state: dict, settings: CandyHuntSettings, now: datetime) -> str:
    start, end = window_bounds(settings)
    zone = settings.zone
    nxt = parse_optional_dt(state.get("next_drop_at"))
    pumpkin = state.get("pumpkin_god")
    if pumpkin:
        pumpkin_line = (
            f"user={pumpkin['user_id']} role={pumpkin['role_id']} "
            f"remove={_clock(parse_dt(pumpkin['remove_at']), zone)}"
        )
    else:
        pumpkin_line = "none"
    lifetime = real_drop_lifetime(settings, _streak_value(state.get("streak")), now)
    lines = [
        "**Candy hunt**",
        f"Server: {settings.guild_id}",
        f"Disabled: {bool(state.get('event_disabled'))}",
        f"Wrap-up done: {bool(state.get('wrap_up_done'))}",
        f"Window: {_window_text(start, end, zone)}",
        (
            "Interval: "
            f"{int(settings.min_interval.total_seconds() // 60)}-"
            f"{int(settings.max_interval.total_seconds() // 60)} min"
        ),
        (
            "Late ramp: "
            f"{_ramp_clock(settings, settings.late_ramp_start_minute)} → "
            f"{_ramp_clock(settings, settings.late_ramp_full_minute)}"
        ),
        _floor_line(settings, now),
        _pace_line(state, settings, now),
        f"Drop lifetime: {_format_minutes(lifetime)} min",
        f"Wrap-up: {_clock(settings.wrap_up_at, zone)}",
        f"Next drop: {_clock(nxt, zone) if nxt else 'not scheduled'}",
        f"Drops posted: {int(state.get('drops_posted') or 0)}",
        f"Claims: {int(state.get('claims_total') or 0)}",
        f"Players: {len(state.get('totals') or {})}",
        f"Pumpkin God: {pumpkin_line}",
        (
            "Test bucket: "
            f"players={len((state.get('test') or {}).get('totals') or {})} "
            f"drops={int((state.get('test') or {}).get('drops_posted') or 0)}"
        ),
        _test_session_line(state, zone),
        f"Now: {_clock(now, zone)}",
    ]
    active = state.get("active_drops") or []
    lines.append(f"Active drops: {len(active)}")
    for drop in active[:8]:
        expires = parse_optional_dt(drop.get("expires_at"))
        when = _clock(expires, zone) if expires else "?"
        lines.append(
            f"- `{drop.get('drop_id')}` channel={drop.get('channel_id')} "
            f"expires={when} dry_run={str(bool(drop.get('dry_run'))).lower()} "
            f"status={drop.get('status')}"
        )
    if len(active) > 8:
        lines.append(f"- … {len(active) - 8} more")
    text = "\n".join(lines)
    if len(text) > 1900:
        return text[:1899] + "…"
    return text


def _clock(moment: datetime | None, zone: ZoneInfo) -> str:
    if moment is None:
        return "none"
    return as_utc(moment).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _format_minutes(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60.0
    if abs(minutes - round(minutes)) < 1e-6:
        return str(int(round(minutes)))
    return f"{minutes:.1f}"


def _factor_text(value: float) -> str:
    return f"x{value:.2f}"


def _pace_line(state: dict, settings: CandyHuntSettings, now: datetime) -> str:
    streak = _streak_value(state.get("streak"))
    shrink = streak_factor(streak, settings)
    ramp = late_night_factor(settings, now)
    low, high = effective_interval(settings, streak, now)
    return (
        f"Streak: {streak} claimed in a row ({_factor_text(shrink)}) | "
        f"Late ramp: {_factor_text(ramp)} | "
        f"Next interval: {_format_minutes(low)}-{_format_minutes(high)} min"
    )


def _test_session_line(state: dict, zone: ZoneInfo) -> str:
    session = (state.get("test") or {}).get("session")
    if not isinstance(session, dict):
        return "Test session: none"
    ends = parse_optional_dt(session.get("ends_at"))
    nxt = parse_optional_dt(session.get("next_drop_at"))
    return (
        "Test session: "
        f"channel={session.get('channel_id')} "
        f"ends={_clock(ends, zone)} "
        f"next={_clock(nxt, zone)} "
        f"drops={int(session.get('drops_posted') or 0)} "
        f"streak={_streak_value(session.get('streak'))}"
    )


def component_text(view: discord.ui.LayoutView) -> str:
    parts: list[str] = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def plain_candy_text(outcome: ClaimOutcome) -> str:
    prefix = "TEST. " if outcome.dry_run else ""
    roll = outcome.roll
    if roll is None:
        return f"{prefix}You got the candy. Total {outcome.total}. Rank #{outcome.rank} of {outcome.players}."
    change = f"{roll.amount:+d}"
    return (
        f"{prefix}{roll.blurb} {roll.name} ({change}). "
        f"Total {outcome.total}. Rank #{outcome.rank} of {outcome.players}."
    )


def _section(eyebrow: str, title: str, image: str, description: str) -> discord.ui.Section:
    return discord.ui.Section(
        eyebrow,
        title,
        accessory=discord.ui.Thumbnail(image, description=description),
    )


def build_drop_view(drop_id: str, *, expires_at: datetime, dry_run: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    accent = TRICK_COLOUR if dry_run else PUMPKIN_COLOUR
    container = discord.ui.Container(accent_colour=accent)
    eyebrow = "-# TEST DROP" if dry_run else "-# TRICK OR TREAT"
    container.add_item(_section(eyebrow, "### 🎃 Trick or treat!", PUMPKIN_IMAGE, "Jack-o'-lantern"))
    container.add_item(discord.ui.Separator())
    stamp = int(as_utc(expires_at).timestamp())
    container.add_item(
        discord.ui.TextDisplay(
            "A pumpkin bucket just clattered into the channel.\n"
            "First hand in the bowl takes the candy. Bots get coal.\n"
            f"It melts <t:{stamp}:R>."
        )
    )
    row = discord.ui.ActionRow()
    row.add_item(ClaimButton(drop_id))
    container.add_item(discord.ui.Separator())
    container.add_item(row)
    view.add_item(container)
    return view


def build_candy_card(outcome: ClaimOutcome) -> discord.ui.LayoutView:
    roll = outcome.roll
    view = discord.ui.LayoutView(timeout=CARD_TIMEOUT)
    trick = roll is not None and roll.kind == "trick"
    container = discord.ui.Container(accent_colour=TRICK_COLOUR if trick else PUMPKIN_COLOUR)
    eyebrow = "-# TEST CANDY CARD" if outcome.dry_run else "-# CANDY CARD"
    title = "### 👻 A trick!" if trick else f"### 🍬 {roll.name if roll else 'Candy'}"
    container.add_item(_section(eyebrow, title, PUMPKIN_IMAGE, "Pumpkin"))
    container.add_item(discord.ui.Separator())
    if roll is None:
        body = "The bucket rattles."
        amount_line = "Candy change: **0**"
    elif trick:
        body = roll.blurb
        amount_line = f"Candy change: **{roll.amount:+d}**"
    else:
        body = roll.blurb
        amount_line = f"You got **{roll.amount}** {roll.name}."
    players = outcome.players or 1
    container.add_item(
        discord.ui.TextDisplay(
            f"{body}\n{amount_line}\n"
            f"Your total: **{outcome.total}**\n"
            f"Rank: **#{outcome.rank}** of {players}"
        )
    )
    container.add_item(discord.ui.Separator())
    label = roll.name if roll is not None else "Candy"
    container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(CANDY_IMAGE, description=label)))
    view.add_item(container)
    return view


def build_leaderboard_view(totals: dict, user_id: int, *, test: bool = False) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=CARD_TIMEOUT)
    container = discord.ui.Container(accent_colour=PUMPKIN_COLOUR)
    if test:
        eyebrow, title = "-# TEST LEADERBOARD", "### 🎃 Test leaderboard"
    else:
        eyebrow, title = "-# HALLOWEEN", "### 🎃 Candy hunt"
    container.add_item(_section(eyebrow, title, PUMPKIN_IMAGE, "Pumpkin"))
    container.add_item(discord.ui.Separator())
    rows = leaderboard_rows(totals, limit=10)
    if rows:
        lines = [f"**#{rank}** <@{uid}> — **{total}**" for rank, uid, total in rows]
        board = "\n".join(lines)
    else:
        board = "No candy has been found yet."
    known = str(user_id) in {str(key) for key in totals}
    if known:
        rank, total, players = rank_for(totals, user_id)
        you = f"You have **{total}** · rank **#{rank}** of {players}."
    else:
        you = "You haven't found any candy yet."
    container.add_item(discord.ui.TextDisplay(f"{board}\n\n{you}"))
    view.add_item(container)
    return view


def build_winner_view(
    *,
    top: list[tuple[str, int]],
    drops_posted: int,
    claims_total: int,
    pumpkin_days: int,
    preview: bool = False,
    test: bool = False,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=CARD_TIMEOUT)
    container = discord.ui.Container(accent_colour=PUMPKIN_COLOUR)
    if test:
        eyebrow = "-# TEST PREVIEW — NOTHING POSTED" if preview else "-# TEST"
    elif preview:
        eyebrow = "-# PREVIEW — NOTHING POSTED"
    else:
        eyebrow = "-# HALLOWEEN CANDY HUNT"
    container.add_item(
        _section(eyebrow, "### 🎃 The pumpkin bucket is empty", PUMPKIN_IMAGE, "Pumpkin")
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    if not top:
        body = "Nobody found any candy. The ghosts kept the bowl."
    else:
        lines = []
        for index, (uid, total) in enumerate(top):
            medal = MEDALS[index] if index < len(MEDALS) else "🍬"
            lines.append(f"{medal} <@{uid}> — **{total}** candy")
        reign = f"The Pumpkin God reigns for {pumpkin_days} days."
        body = "\n".join(lines) + f"\n\n{reign}"
    container.add_item(discord.ui.TextDisplay(body))
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(f"Drops posted: **{drops_posted}**\nClaims: **{claims_total}**")
    )
    container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(CANDY_IMAGE, description="Candy")))
    view.add_item(container)
    return view


class CandyStore:
    """JSON book for the hunt. The path is injectable so tests stay out of data/."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = LockedJsonFile(self.path, default=default_state, indent=2)
        self._lock = asyncio.Lock()
        self.state = normalize_state(self._file.read())

    def save(self) -> None:
        self._file.write(self.state)

    async def claim(
        self,
        drop_id: str,
        user_id: int,
        now: datetime,
        rng: random.Random,
        *,
        is_bot: bool,
        trick_chance: float,
        settings: CandyHuntSettings | None = None,
    ) -> ClaimOutcome:
        async with self._lock:
            # Yield while holding the lock so overlapping clicks serialize on the
            # check-and-set instead of both observing an active drop.
            await asyncio.sleep(0)
            new_state, outcome = apply_claim(
                self.state,
                drop_id,
                user_id,
                now,
                rng,
                is_bot=is_bot,
                trick_chance=trick_chance,
                settings=settings,
            )
            if outcome.mutated:
                self.state = new_state
                self.save()
            return outcome


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=CLAIM_TEMPLATE):
    def __init__(self, drop_id: str):
        super().__init__(
            discord.ui.Button(
                label="Grab the candy",
                emoji="🍬",
                style=discord.ButtonStyle.primary,
                custom_id=f"candy:claim:{drop_id}",
            )
        )
        self.drop_id = drop_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match,
    ) -> ClaimButton:
        return cls(match.group("drop_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("CandyHunt")
        if not isinstance(cog, CandyHunt):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The candy hunt isn't available right now.",
                    ephemeral=True,
                )
            return
        await cog.handle_claim(interaction, self.drop_id)


def _perm(channel, target, name: str) -> bool:
    if channel is None or target is None:
        return False
    try:
        perms = channel.permissions_for(target)
    except Exception:
        return False
    return bool(getattr(perms, name, False))


def _view_and_send(channel, target) -> tuple[bool, bool]:
    if channel is None or target is None:
        return False, False
    try:
        perms = channel.permissions_for(target)
    except Exception:
        return False, False
    return (
        bool(getattr(perms, "view_channel", False)),
        bool(getattr(perms, "send_messages", False)),
    )


class CandyHunt(Cog):
    """Halloween candy drops, claims, and the November 1 wrap-up."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        path: str | Path | None = None,
        settings: CandyHuntSettings | None = None,
        rng: random.Random | None = None,
        autostart_path: str | Path | None = None,
    ):
        self.bot = bot
        self.settings = settings if settings is not None else load_settings()
        self.store = CandyStore(path if path is not None else DEFAULT_PATH)
        self.rng = rng if rng is not None else random.Random()
        self.autostart_path = Path(autostart_path) if autostart_path is not None else AUTOSTART_PATH
        self.activity: dict[int, datetime] = {}
        self._catching_up = False
        self._wrap_channel_warned = False
        self._autostart: dict | None = None
        self._startup_task: asyncio.Task | None = None
        self._startup_started = False
        self._guilds_synced = False

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ClaimButton)
        now = discord.utils.utcnow()
        self._catching_up = as_utc(now) >= as_utc(self.settings.wrap_up_at)
        _log_info(
            "candy hunt loaded "
            f"players={len(self.store.state['totals'])} "
            f"drops_posted={self.store.state['drops_posted']} "
            f"active={len(self.store.state['active_drops'])} "
            f"next={self.store.state.get('next_drop_at') or 'none'} "
            f"disabled={str(bool(self.store.state['event_disabled'])).lower()} "
            f"wrap_up_done={str(bool(self.store.state['wrap_up_done'])).lower()}"
        )
        # Snapshot the flag now. It is acted on after ready, then deleted.
        self._autostart = read_autostart(self.autostart_path)
        if not self.candy_loop.is_running():
            self.candy_loop.start()
        self._ensure_startup_task()

    async def cog_unload(self) -> None:
        self.candy_loop.cancel()
        task = self._startup_task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        remove = getattr(self.bot, "remove_dynamic_items", None)
        if remove is not None:
            remove(ClaimButton)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # CheckFailure for !candyadmin is an unauthorized caller. Swallow it so
        # the default command error logger stays quiet. Bad args get usage text.
        if not _command_is_candyadmin(getattr(ctx, "command", None)):
            log.error("candy hunt command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.UserInputError):
            try:
                await ctx.reply(CANDYADMIN_USAGE, mention_author=False)
            except Exception:
                log.error("candy admin usage reply failed", exc_info=True)
            return
        log.error("candy admin command failed", exc_info=error)

    def _ensure_startup_task(self) -> None:
        if self._startup_started:
            return
        self._startup_started = True
        self._startup_task = asyncio.create_task(self._startup_after_ready())

    async def _startup_after_ready(self) -> None:
        """Guild-sync candy commands once, then consume the autostart flag.

        ``on_ready`` can fire again after a reconnect. This task is started
        once per cog load, so a reconnect does not sync or autostart again.
        """
        try:
            await self.bot.wait_until_ready()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("candy commands synced failed")
            return
        if not self._guilds_synced:
            self._guilds_synced = True
            try:
                await self.sync_candy_guild_commands()
            except Exception:
                log.exception("candy commands synced failed")
        try:
            await self._consume_autostart()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("candy test session autostart failed")

    async def sync_candy_guild_commands(self) -> None:
        """Publish the guild command set for each candy guild.

        ``tree.sync(guild=...)`` replaces that guild's guild-scoped commands
        with the guild commands already on the tree. No other cog registers
        guild-scoped commands for these guilds, so the set is the candy
        commands (plus anything else already guild-scoped on the tree).
        This does not call global ``tree.sync()`` or ``copy_global_to``.
        """
        tree = getattr(self.bot, "tree", None)
        sync = getattr(tree, "sync", None)
        if not callable(sync):
            log.error("candy commands synced failed error=no_tree")
            return
        for gid in CANDY_GUILD_IDS:
            try:
                synced = await sync(guild=discord.Object(id=gid))
            except Exception:
                log.exception("candy commands synced guild=%s failed", gid)
                continue
            synced = list(synced or [])
            names = ",".join(str(getattr(command, "name", "")) for command in synced)
            _log_info(f"candy commands synced guild={gid} count={len(synced)} names={names}")

    async def _consume_autostart(self) -> None:
        payload = self._autostart
        self._autostart = None
        if payload is None:
            return
        path = self.autostart_path
        try:
            if payload.get("error"):
                _log_info(f"candy test session refused reason={payload['error']} source=autostart")
                return
            guild_id = int(payload["guild_id"])
            if guild_id == MTS_GUILD_ID:
                _log_info(f"candy test session refused guild={guild_id} reason=mts source=autostart")
                return
            status = await self.begin_test_session(
                guild_id=guild_id,
                channel_id=int(payload["channel_id"]),
                minutes=int(payload["minutes"]),
                interval=int(payload["interval"]),
                source="autostart",
            )
            if status == "started":
                await self.maybe_test_session(discord.utils.utcnow())
            elif status == "busy":
                _log_info("candy test session refused reason=already_running source=autostart")
            elif status == "mts":
                _log_info(f"candy test session refused guild={guild_id} reason=mts source=autostart")
            else:
                _log_info(f"candy test session refused reason={status} source=autostart")
        finally:
            delete_flag_file(path)

    @tasks.loop(seconds=LOOP_SECONDS)
    async def candy_loop(self) -> None:
        try:
            await self.tick()
        except Exception:
            log.exception("Candy hunt loop failed")

    @candy_loop.before_loop
    async def before_candy_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, *, now: datetime | None = None) -> None:
        moment = now if now is not None else discord.utils.utcnow()
        await self.remove_pumpkin_god_if_due(moment)
        await self.expire_drops(moment)
        await self.maybe_wrap_up(moment)
        await self.maybe_drop(moment)
        await self.maybe_test_session(moment)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or guild.id != self.settings.guild_id:
            return
        author = message.author
        if author is None or author.bot or message.webhook_id is not None:
            return
        channel = message.channel
        if isinstance(channel, discord.Thread):
            channel = channel.parent
        if not isinstance(channel, discord.TextChannel):
            return
        self.activity[channel.id] = message.created_at

    def _guild(self) -> discord.Guild | None:
        getter = getattr(self.bot, "get_guild", None)
        if not callable(getter):
            return None
        guild = getter(self.settings.guild_id)
        if isinstance(guild, discord.Guild) or guild is None:
            return guild
        return guild

    def _broad_roles(self, guild) -> list:
        """Roles held by at least half the cached human members. One pass.

        Bots are ignored. Managed roles and @everyone never qualify, so a
        staff role held by a few people does not open a channel.
        """
        holders: dict[int, int] = {}
        roles_by_id: dict[int, object] = {}
        managed: set[int] = set()
        humans = 0
        everyone_id = getattr(getattr(guild, "default_role", None), "id", None)
        for member in getattr(guild, "members", None) or ():
            if getattr(member, "bot", False):
                continue
            humans += 1
            seen: set[int] = set()
            for role in getattr(member, "roles", None) or ():
                role_id = getattr(role, "id", None)
                if role_id is None or role_id in seen:
                    continue
                seen.add(role_id)
                roles_by_id[role_id] = role
                if getattr(role, "managed", False):
                    managed.add(role_id)
                holders[role_id] = holders.get(role_id, 0) + 1
        chosen = broad_role_ids(
            holders,
            humans,
            managed_role_ids=managed,
            everyone_role_id=everyone_id,
        )
        return [roles_by_id[role_id] for role_id in chosen]

    def _channel_snapshot(self, guild, channel: discord.TextChannel, broad_roles: list) -> ChannelSnapshot:
        me = guild.me
        if me is None:
            user = getattr(self.bot, "user", None)
            if user is not None:
                me = guild.get_member(user.id)
        rules = getattr(guild, "rules_channel", None)
        rules_id = getattr(rules, "id", None)
        announce_id = self.settings.announcement_channel_id
        everyone = getattr(guild, "default_role", None)
        everyone_can_view, everyone_can_send = _view_and_send(channel, everyone)
        broad_role_can_chat = False
        if not (everyone_can_view and everyone_can_send):
            for role in broad_roles:
                can_view, can_send = _view_and_send(channel, role)
                if can_view and can_send:
                    broad_role_can_chat = True
                    break
        return ChannelSnapshot(
            channel_id=channel.id,
            name=str(channel.name),
            is_text=True,
            is_news=bool(channel.is_news()) if hasattr(channel, "is_news") else False,
            is_nsfw=bool(getattr(channel, "nsfw", False)),
            bot_can_view=_perm(channel, me, "view_channel"),
            bot_can_send=_perm(channel, me, "send_messages"),
            everyone_can_view=everyone_can_view,
            everyone_can_send=everyone_can_send,
            broad_role_can_chat=broad_role_can_chat,
            is_rules_channel=rules_id is not None and channel.id == rules_id,
            is_announce_channel=announce_id is not None and channel.id == announce_id,
            skipped=channel.id in self.settings.skip_channel_ids,
        )

    def choose_drop_channel(self, now: datetime) -> discord.TextChannel | None:
        guild = self._guild()
        if guild is None:
            _log_info("candy channels eligible=0 total=0")
            return None
        broad_roles = self._broad_roles(guild)
        eligible: dict[int, discord.TextChannel] = {}
        activities: list[ChannelActivity] = []
        total = 0
        for channel in getattr(guild, "text_channels", []):
            if not isinstance(channel, discord.TextChannel):
                continue
            total += 1
            snapshot = self._channel_snapshot(guild, channel, broad_roles)
            if not is_eligible(snapshot):
                continue
            eligible[channel.id] = channel
            activities.append(ChannelActivity(channel.id, self.activity.get(channel.id)))
        _log_info(f"candy channels eligible={len(eligible)} total={total}")
        chosen = pick_channel_id(
            activities,
            now,
            self.rng,
            activity_window=self.settings.activity_window,
        )
        if chosen is None:
            return None
        return eligible.get(chosen)

    def _announcement_channel(self, guild) -> discord.TextChannel | None:
        rows: list[tuple[int, str]] = []
        lookup: dict[int, discord.TextChannel] = {}
        for channel in getattr(guild, "text_channels", []):
            if isinstance(channel, discord.TextChannel):
                rows.append((channel.id, channel.name))
                lookup[channel.id] = channel
        found = find_announcement_channel(
            rows,
            channel_id=self.settings.announcement_channel_id,
            name=self.settings.announcement_channel_name,
        )
        if found is None:
            return None
        return lookup.get(found)

    async def post_drop(
        self,
        channel,
        *,
        dry_run: bool,
        now: datetime | None = None,
        expiry: timedelta | None = None,
    ) -> dict:
        moment = now if now is not None else discord.utils.utcnow()
        lifetime = self.settings.drop_expiry if expiry is None else expiry
        expires = moment + lifetime
        async with self.store._lock:
            drop_id = new_drop_id({drop["drop_id"] for drop in self.store.state["active_drops"]})
            record = make_drop(drop_id, channel.id, None, moment, expires, dry_run=dry_run)
            self.store.state["active_drops"].append(record)
            bucket = self.store.state["test"] if dry_run else self.store.state
            bucket["drops_posted"] = int(bucket.get("drops_posted", 0)) + 1
            self.store.save()
        view = build_drop_view(drop_id, expires_at=expires, dry_run=dry_run)
        try:
            message = await channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException):
            async with self.store._lock:
                self.store.state["active_drops"] = [
                    drop for drop in self.store.state["active_drops"] if drop["drop_id"] != drop_id
                ]
                bucket = self.store.state["test"] if dry_run else self.store.state
                bucket["drops_posted"] = max(0, int(bucket.get("drops_posted", 0)) - 1)
                self.store.save()
            raise
        async with self.store._lock:
            for drop in self.store.state["active_drops"]:
                if drop["drop_id"] == drop_id:
                    drop["message_id"] = getattr(message, "id", None)
                    record = drop
                    break
            self.store.save()
        _log_info(
            f"candy drop posted channel={channel.id} drop={drop_id} "
            f"expires={iso(expires)} dry_run={str(dry_run).lower()}"
        )
        return record

    async def maybe_drop(self, now: datetime) -> None:
        if self.store.state.get("event_disabled") or self.store.state.get("wrap_up_done"):
            return
        start, end = window_bounds(self.settings)
        async with self.store._lock:
            # The 30s loop reads next_drop_at on every pass. A claim that pulls
            # that timestamp earlier is picked up on the next pass, and the new
            # time is always at least REAL_CLAIM_LEAD ahead, so this pass cannot
            # also post it.
            streak = _streak_value(self.store.state.get("streak"))
            decision = resolve_schedule(
                now=now,
                persisted=parse_optional_dt(self.store.state.get("next_drop_at")),
                window_start=start,
                window_end=end,
                min_interval=self.settings.min_interval,
                max_interval=self.settings.max_interval,
                rng=self.rng,
                settings=self.settings,
                streak=streak,
            )
            new_iso = None if decision.next_drop_at is None else iso(decision.next_drop_at)
            if decision.action != "drop":
                if self.store.state.get("next_drop_at") != new_iso:
                    self.store.state["next_drop_at"] = new_iso
                    self.store.save()
                return
            if not is_in_drop_window(self.settings, now):
                self.store.state["next_drop_at"] = new_iso
                self.store.save()
                _log_info("candy drop skipped reason=outside_window")
                return
            self.store.state["next_drop_at"] = new_iso
            self.store.save()
        channel = self.choose_drop_channel(now)
        if channel is None:
            _log_info("candy drop skipped reason=no_channel")
            return
        # Scheduled real drops use the streak lifetime. Test sessions pass
        # their own expiry and do not use this value.
        lifetime = real_drop_lifetime(self.settings, streak, now)
        try:
            await self.post_drop(channel, dry_run=False, now=now, expiry=lifetime)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy drop failed channel={getattr(channel, 'id', None)} error={type(exc).__name__}")

    async def handle_claim(self, interaction: discord.Interaction, drop_id: str) -> None:
        user = interaction.user
        now = discord.utils.utcnow()
        outcome = await self.store.claim(
            drop_id,
            int(user.id),
            now,
            self.rng,
            is_bot=bool(getattr(user, "bot", False)),
            trick_chance=self.settings.trick_chance,
            settings=self.settings,
        )
        if outcome.status == "bot":
            await interaction.response.send_message("Bots can't grab candy.", ephemeral=True)
            return
        if outcome.status == "expired":
            await interaction.response.send_message(MELTED_TEXT, ephemeral=True)
            await self._cleanup_drop(outcome.drop, interaction)
            if outcome.drop is not None:
                _log_info(
                    f"candy drop expired channel={outcome.drop.get('channel_id')} drop={outcome.drop.get('drop_id')}"
                )
            return
        if outcome.status != "won":
            await interaction.response.send_message(LATE_CLAIM_TEXT, ephemeral=True)
            return
        roll = outcome.roll
        _log_info(
            "candy claimed "
            f"user={user.id} drop={drop_id} kind={roll.kind if roll else 'none'} "
            f"amount={roll.amount if roll else 0} total={outcome.total} rank={outcome.rank} "
            f"dry_run={str(outcome.dry_run).lower()}"
        )
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await self._cleanup_drop(outcome.drop, interaction)
        await self._deliver_card(interaction, user, outcome)

    async def _deliver_card(self, interaction: discord.Interaction, user, outcome: ClaimOutcome) -> None:
        view = build_candy_card(outcome)
        try:
            await user.send(view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy dm failed user={getattr(user, 'id', None)} error={type(exc).__name__}")
            try:
                await interaction.followup.send(view=view, ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(plain_candy_text(outcome), ephemeral=True)
            return
        await interaction.followup.send("You got it. The candy card is in your DMs.", ephemeral=True)

    async def _cleanup_drop(self, drop: dict | None, interaction: discord.Interaction | None = None) -> None:
        if drop is None:
            return
        await self._delete_drop_message(drop, interaction)
        drop_id = drop.get("drop_id")
        if not drop_id:
            return
        async with self.store._lock:
            self.store.state["active_drops"] = [
                item for item in self.store.state["active_drops"] if item.get("drop_id") != drop_id
            ]
            self.store.save()

    async def _delete_drop_message(self, drop: dict, interaction: discord.Interaction | None = None) -> bool:
        if interaction is not None and getattr(interaction, "message", None) is not None:
            try:
                await interaction.message.delete()
                return True
            except discord.NotFound:
                return True
            except discord.HTTPException as exc:
                _log_info(
                    f"candy drop delete failed channel={drop.get('channel_id')} "
                    f"drop={drop.get('drop_id')} error={type(exc).__name__}"
                )
                return False
        message_id = drop.get("message_id")
        channel_id = drop.get("channel_id")
        if not message_id or not channel_id:
            return False
        channel = None
        getter = getattr(self.bot, "get_channel", None)
        if callable(getter):
            channel = getter(int(channel_id))
        if channel is None:
            fetch = getattr(self.bot, "fetch_channel", None)
            if not callable(fetch):
                return False
            try:
                channel = await fetch(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(
                f"candy drop delete failed channel={channel_id} drop={drop.get('drop_id')} "
                f"error={type(exc).__name__}"
            )
            return False

    async def expire_drops(self, now: datetime) -> None:
        async with self.store._lock:
            keep, expired, finished = split_stale_drops(self.store.state["active_drops"], now)
            if not expired and not finished:
                return
            self.store.state["active_drops"] = keep
            if expired:
                reset_streaks_for_expiry(self.store.state, expired)
            self.store.save()
        for drop in expired:
            await self._delete_drop_message(drop)
            _log_info(f"candy drop expired channel={drop.get('channel_id')} drop={drop.get('drop_id')}")
        for drop in finished:
            await self._delete_drop_message(drop)

    async def maybe_wrap_up(self, now: datetime) -> None:
        if self.store.state.get("event_disabled"):
            return
        decision = wrap_up_decision(
            now=now,
            wrap_up_at=self.settings.wrap_up_at,
            done=bool(self.store.state.get("wrap_up_done")),
            had_activity=had_activity(self.store.state),
            catching_up=self._catching_up,
            late_limit=self.settings.wrap_up_late_limit,
            has_players=has_players(self.store.state),
        )
        if decision in {"waiting", "done"}:
            return
        if decision == "too_late":
            await self._mark_wrap_done()
            _log_info("candy wrapup skipped reason=too_late")
            return
        if decision == "skip_quiet":
            await self._mark_wrap_done()
            _log_info("candy wrapup skipped reason=no_activity")
            return
        result = await self.run_wrap_up(now, dry_run=False)
        if result.status == "no_channel" and not self._wrap_channel_warned:
            self._wrap_channel_warned = True
            _log_info("candy wrapup skipped reason=no_announcement_channel")

    async def _mark_wrap_done(self) -> None:
        async with self.store._lock:
            if self.store.state.get("wrap_up_done"):
                return
            self.store.state["wrap_up_done"] = True
            self.store.state["next_drop_at"] = None
            self.store.save()

    async def run_wrap_up(self, now: datetime, *, dry_run: bool, test_tally: bool = False) -> WrapResult:
        # The test tally never posts an announcement and never grants a role.
        if test_tally:
            dry_run = True
        async with self.store._lock:
            if not dry_run and self.store.state.get("wrap_up_done"):
                return WrapResult("already")
            if test_tally:
                bucket = self.store.state.get("test") or {}
                totals = dict(bucket.get("totals") or {})
                drops_posted = int(bucket.get("drops_posted") or 0)
                claims_total = int(bucket.get("claims_total") or 0)
            else:
                totals = dict(self.store.state["totals"])
                drops_posted = int(self.store.state.get("drops_posted") or 0)
                claims_total = int(self.store.state.get("claims_total") or 0)
        top = podium(totals, 3)
        winner_id = int(top[0][0]) if top else None
        days = max(1, int(self.settings.pumpkin_god_duration.total_seconds() // 86400))
        view = build_winner_view(
            top=top,
            drops_posted=drops_posted,
            claims_total=claims_total,
            pumpkin_days=days,
            preview=dry_run,
            test=test_tally,
        )
        if dry_run:
            return WrapResult("preview", view=view, winner_id=winner_id)

        guild = self._guild()
        if guild is None:
            _log_info("candy wrapup skipped reason=guild_unavailable")
            return WrapResult("no_guild", view=view, winner_id=winner_id)
        channel = self._announcement_channel(guild)
        if channel is None:
            return WrapResult("no_channel", view=view, winner_id=winner_id)

        async with self.store._lock:
            if self.store.state.get("wrap_up_done"):
                return WrapResult("already", view=view, winner_id=winner_id)
            self.store.state["wrap_up_done"] = True
            self.store.state["next_drop_at"] = None
            self.store.save()

        if winner_id is not None:
            record = await self._grant_pumpkin_god(guild, winner_id, now)
            if record is not None:
                async with self.store._lock:
                    self.store.state["pumpkin_god"] = record
                    self.store.save()

        try:
            await channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy wrapup failed channel={channel.id} error={type(exc).__name__}")
            async with self.store._lock:
                self.store.state["wrap_up_done"] = False
                self.store.save()
            return WrapResult("post_failed", view=view, winner_id=winner_id)

        _log_info(
            f"candy wrapup posted guild={guild.id} channel={channel.id} "
            f"winner={winner_id if winner_id is not None else 'none'} "
            f"drops={drops_posted} claims={claims_total}"
        )
        return WrapResult("posted", view=view, winner_id=winner_id)

    async def _grant_pumpkin_god(self, guild, user_id: int, now: datetime) -> dict | None:
        name = self.settings.pumpkin_god_role_name
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            try:
                role = await guild.create_role(
                    name=name,
                    colour=PUMPKIN_COLOUR,
                    hoist=True,
                    mentionable=False,
                    reason="Candy hunt Pumpkin God",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                _log_info(f"candy wrapup role failed user={user_id} error={type(exc).__name__}")
                return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                _log_info(f"candy wrapup role failed user={user_id} error={type(exc).__name__}")
                return None
        try:
            await member.add_roles(role, reason="Candy hunt Pumpkin God")
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy wrapup role failed user={user_id} error={type(exc).__name__}")
            return None
        remove_at = as_utc(now) + self.settings.pumpkin_god_duration
        _log_info(f"candy wrapup role added user={user_id} role={role.id} guild={guild.id}")
        return {
            "user_id": int(user_id),
            "role_id": int(role.id),
            "guild_id": int(guild.id),
            "remove_at": iso(remove_at),
        }

    async def remove_pumpkin_god_if_due(self, now: datetime) -> None:
        record = self.store.state.get("pumpkin_god")
        if not role_removal_due(record, now):
            return
        user_id = int(record["user_id"])
        role_id = int(record["role_id"])
        guild_id = int(record["guild_id"])
        getter = getattr(self.bot, "get_guild", None)
        guild = getter(guild_id) if callable(getter) else None
        if guild is None:
            _log_info(f"candy pumpkin god remove failed user={user_id} error=guild_unavailable")
            return
        role = guild.get_role(role_id)
        if role is None:
            _log_info(f"candy pumpkin god remove skipped user={user_id} role={role_id} reason=role_missing")
            await self._clear_pumpkin_god()
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                _log_info(f"candy pumpkin god remove skipped user={user_id} reason=member_missing")
                await self._clear_pumpkin_god()
                return
            except (discord.Forbidden, discord.HTTPException) as exc:
                _log_info(f"candy pumpkin god remove failed user={user_id} error={type(exc).__name__}")
                return
        try:
            await member.remove_roles(role, reason="Candy hunt Pumpkin God expired")
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy pumpkin god remove failed user={user_id} error={type(exc).__name__}")
            return
        _log_info(f"candy pumpkin god removed user={user_id} role={role_id} guild={guild_id}")
        await self._clear_pumpkin_god()

    async def _clear_pumpkin_god(self) -> None:
        async with self.store._lock:
            self.store.state["pumpkin_god"] = None
            self.store.save()

    async def _set_disabled(self, disabled: bool) -> None:
        async with self.store._lock:
            self.store.state["event_disabled"] = disabled
            self.store.save()

    async def begin_test_session(
        self,
        *,
        guild_id: int,
        channel_id: int,
        minutes: int,
        interval: int,
        source: str,
        now: datetime | None = None,
    ) -> str:
        """Persist a support-server session.

        Does not touch MTS totals, the real streak, or next_drop_at. A start
        clears the test tally, deletes leftover dry-run drops, and opens a new
        session at streak 0. A session that is already running is refused and
        left untouched.
        """
        if int(guild_id) == MTS_GUILD_ID:
            return "mts"
        if validate_test_session_args(minutes, interval):
            return "invalid"
        moment = now if now is not None else discord.utils.utcnow()
        async with self.store._lock:
            test = self.store.state.setdefault("test", default_test())
            current = test.get("session")
            if isinstance(current, dict) and resolve_test_session(current, moment) != "end":
                return "busy"
            removed = [drop for drop in self.store.state["active_drops"] if drop.get("dry_run")]
            self.store.state["active_drops"] = [
                drop for drop in self.store.state["active_drops"] if not drop.get("dry_run")
            ]
            # Fresh bucket. make_test_session starts the test streak at 0.
            # Real totals and the real streak live on the parent state.
            self.store.state["test"] = default_test()
            self.store.state["test"]["session"] = make_test_session(
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                minutes=int(minutes),
                interval=int(interval),
                now=moment,
                source=source,
            )
            self.store.save()
        for drop in removed:
            await self._delete_drop_message(drop)
        _log_info(
            "candy test session started "
            f"guild={guild_id} channel={channel_id} minutes={minutes} interval={interval} source={source}"
        )
        return "started"

    async def stop_test_session(self, *, reason: str) -> dict | None:
        async with self.store._lock:
            test = self.store.state.setdefault("test", default_test())
            session = test.get("session")
            if not isinstance(session, dict):
                return None
            test["session"] = None
            self.store.save()
        log_session_ended(session, reason)
        return session

    def _cached_channel(self, channel_id: int):
        getter = getattr(self.bot, "get_channel", None)
        if not callable(getter):
            return None
        return getter(int(channel_id))

    async def _fetch_channel(self, channel_id: int):
        fetch = getattr(self.bot, "fetch_channel", None)
        if not callable(fetch):
            return None
        try:
            return await fetch(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def maybe_test_session(self, now: datetime) -> None:
        """Post one dry-run drop for an active support session.

        The MTS schedule is ``maybe_drop``. This path never calls
        ``choose_drop_channel``, so the ``candy channels eligible=...`` line
        stays the MTS selector. Test posts go through ``post_drop`` and log
        ``candy drop posted ... dry_run=true``.
        """
        async with self.store._lock:
            test = self.store.state.setdefault("test", default_test())
            session = test.get("session")
            action = resolve_test_session(session, now)
            if action == "idle":
                return
            if action == "end":
                test["session"] = None
                self.store.save()
                reason = "mts" if int(session.get("guild_id") or 0) == MTS_GUILD_ID else "elapsed"
                ended = session
            else:
                ended = None
            if action != "drop":
                if ended is not None:
                    log_session_ended(ended, reason)
                return
            if int(session.get("guild_id") or 0) == MTS_GUILD_ID:
                test["session"] = None
                self.store.save()
                log_session_ended(session, "mts")
                return
            interval = int(session["interval"])
            channel_id = int(session["channel_id"])
            started_at = session.get("started_at")
            delay = jittered_delay_seconds(
                interval,
                self.rng,
                factor=streak_factor(_streak_value(session.get("streak")), self.settings),
            )
            nxt = as_utc(now) + timedelta(seconds=delay)
            ends = parse_dt(session["ends_at"])
            session["next_drop_at"] = None if nxt >= as_utc(ends) else iso(nxt)
            self.store.save()
            lifetime = session_drop_lifetime(self.settings.drop_expiry, timedelta(minutes=interval))

        channel = self._cached_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            channel = await self._fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            _log_info(f"candy test drop skipped reason=no_channel channel={channel_id}")
            return
        channel_guild = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild == MTS_GUILD_ID:
            await self.stop_test_session(reason="mts")
            return
        try:
            await self.post_drop(channel, dry_run=True, now=now, expiry=lifetime)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log_info(f"candy test drop failed channel={channel_id} error={type(exc).__name__}")
            return
        async with self.store._lock:
            current = (self.store.state.get("test") or {}).get("session")
            if isinstance(current, dict) and current.get("started_at") == started_at:
                current["drops_posted"] = int(current.get("drops_posted") or 0) + 1
                self.store.save()

    @app_commands.command(name="candy", description="Show the Halloween candy hunt leaderboard.")
    @app_commands.guilds(*CANDY_GUILD_IDS)
    @app_commands.guild_only()
    async def candy(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id == self.settings.guild_id:
            view = build_leaderboard_view(self.store.state["totals"], interaction.user.id)
        elif interaction.guild_id == SUPPORT_GUILD_ID:
            totals = (self.store.state.get("test") or {}).get("totals") or {}
            view = build_leaderboard_view(totals, interaction.user.id, test=True)
        else:
            await interaction.response.send_message(
                "The candy hunt is only running in Menaces to Sobriety.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _admin_reply(self, ctx, content: str | None = None, *, view=None) -> None:
        if view is not None:
            # Components v2 rejects a content body. The preview card already says PREVIEW.
            await ctx.reply(view=view, mention_author=False)
            return
        await ctx.reply(content, mention_author=False)

    async def _admin_text_channel(self, ctx, token: object):
        """Resolve a test-session channel. Returns (channel, error_code)."""
        if isinstance(token, discord.TextChannel):
            return token, None
        if token is None:
            if getattr(ctx, "guild", None) is None:
                return None, "dm"
            current = getattr(ctx, "channel", None)
            if isinstance(current, discord.TextChannel):
                return current, None
            return None, "not_text"
        channel_id = _parse_channel_id(token)
        if channel_id is None:
            return None, "usage"
        found = self._cached_channel(channel_id)
        if not isinstance(found, discord.TextChannel):
            found = await self._fetch_channel(channel_id)
        if not isinstance(found, discord.TextChannel):
            return None, "not_text"
        return found, None

    @commands.group(name="candyadmin", invoke_without_command=True, hidden=True, case_insensitive=True)
    @_candyadmin_check
    async def candyadmin(self, ctx: commands.Context) -> None:
        """Owner controls for the candy hunt. Hidden from help."""
        if not await _candyadmin_allowed(ctx):
            return
        await self._admin_reply(ctx, CANDYADMIN_USAGE)

    @candyadmin.command(name="status", hidden=True)
    @_candyadmin_check
    async def admin_status(self, ctx: commands.Context) -> None:
        """Show the candy hunt schedule and totals."""
        if not await _candyadmin_allowed(ctx):
            return
        text = render_status(self.store.state, self.settings, discord.utils.utcnow())
        await self._admin_reply(ctx, text)

    @candyadmin.command(name="power", hidden=True)
    @_candyadmin_check
    async def admin_power(self, ctx: commands.Context, state: str | None = None) -> None:
        """Turn scheduled drops and the automatic wrap-up on or off."""
        if not await _candyadmin_allowed(ctx):
            return
        mode = _parse_power(state)
        if mode is None:
            await self._admin_reply(ctx, CANDYADMIN_POWER_USAGE)
            return
        if mode == "on":
            await self._set_disabled(False)
            extra = ""
            if self.store.state.get("wrap_up_done"):
                extra = " Wrap-up already ran, so scheduled drops stay off."
            await self._admin_reply(ctx, f"Candy hunt enabled.{extra}")
            return
        await self._set_disabled(True)
        await self._admin_reply(
            ctx,
            "Candy hunt disabled. Scheduled drops and the automatic wrap-up are paused.",
        )

    @candyadmin.command(name="test_session", hidden=True)
    @_candyadmin_check
    async def admin_test_session(
        self,
        ctx: commands.Context,
        minutes: str | None = None,
        interval: str | None = None,
        channel: str | None = None,
    ) -> None:
        """Start a dry-run candy session outside Menaces to Sobriety."""
        if not await _candyadmin_allowed(ctx):
            return
        minutes_i = _parse_session_int(minutes, 15)
        interval_i = _parse_session_int(interval, 2)
        if minutes_i is None or interval_i is None or validate_test_session_args(minutes_i, interval_i):
            await self._admin_reply(ctx, CANDYADMIN_TEST_USAGE)
            return
        guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        if guild_id == MTS_GUILD_ID:
            _log_info(f"candy test session refused guild={guild_id} reason=mts source=command")
            await self._admin_reply(ctx, CANDYADMIN_TEST_MTS)
            return
        target, problem = await self._admin_text_channel(ctx, channel)
        if problem == "dm":
            await self._admin_reply(ctx, CANDYADMIN_TEST_DM)
            return
        if problem == "usage":
            await self._admin_reply(ctx, CANDYADMIN_TEST_USAGE)
            return
        if problem == "not_text" or target is None:
            await self._admin_reply(ctx, CANDYADMIN_TEST_CHANNEL)
            return
        channel_guild = getattr(getattr(target, "guild", None), "id", None)
        if channel_guild == MTS_GUILD_ID:
            _log_info(f"candy test session refused guild={MTS_GUILD_ID} reason=mts source=command")
            await self._admin_reply(ctx, CANDYADMIN_TEST_MTS)
            return
        if guild_id is None:
            if channel_guild is None:
                await self._admin_reply(ctx, CANDYADMIN_TEST_DM)
                return
            guild_id = channel_guild
        status = await self.begin_test_session(
            guild_id=int(guild_id),
            channel_id=int(target.id),
            minutes=int(minutes_i),
            interval=int(interval_i),
            source="command",
        )
        if status == "busy":
            await self._admin_reply(ctx, CANDYADMIN_TEST_BUSY)
            return
        if status != "started":
            await self._admin_reply(ctx, CANDYADMIN_TEST_MTS)
            return
        await self._admin_reply(
            ctx,
            f"Test session started in <#{target.id}> for {minutes_i} minutes, "
            f"about every {interval_i} minutes. Claims go to the test tally. "
            "The previous test tally was cleared.",
        )
        await self.maybe_test_session(discord.utils.utcnow())

    @candyadmin.command(name="test_session_stop", hidden=True)
    @_candyadmin_check
    async def admin_test_session_stop(self, ctx: commands.Context) -> None:
        """Stop the dry-run candy test session."""
        if not await _candyadmin_allowed(ctx):
            return
        stopped = await self.stop_test_session(reason="stopped")
        if stopped is None:
            await self._admin_reply(ctx, "No test session is running.")
            return
        await self._admin_reply(ctx, "Test session stopped. Real totals were left alone.")

    @candyadmin.command(name="wrapup", hidden=True)
    @_candyadmin_check
    async def admin_wrapup(self, ctx: commands.Context, dry_run: str | None = None) -> None:
        """Preview or run the November wrap-up. Only false posts the real announcement."""
        if not await _candyadmin_allowed(ctx):
            return
        preview = _parse_dry_run(dry_run)
        if preview is None:
            await self._admin_reply(ctx, CANDYADMIN_WRAPUP_USAGE)
            return
        guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        real_guild = self.settings.guild_id
        in_support = guild_id == SUPPORT_GUILD_ID and guild_id != real_guild
        if preview:
            result = await self.run_wrap_up(
                discord.utils.utcnow(),
                dry_run=True,
                test_tally=in_support,
            )
            await self._admin_reply(ctx, view=result.view)
            return
        if guild_id != real_guild:
            await self._admin_reply(
                ctx,
                "The real wrap-up only runs in Menaces to Sobriety. It will not post or give roles here.",
            )
            return
        result = await self.run_wrap_up(discord.utils.utcnow(), dry_run=False)
        if result.status == "already":
            await self._admin_reply(ctx, "Wrap-up already happened. It will not run again.")
            return
        if result.status == "posted":
            winner = f"<@{result.winner_id}>" if result.winner_id is not None else "nobody"
            await self._admin_reply(
                ctx,
                "Wrap-up finished. This is irreversible: the candy hunt is off, "
                f"and the winner announcement was posted in Menaces to Sobriety. Winner: {winner}.",
            )
            return
        if result.status == "post_failed":
            await self._admin_reply(ctx, "The announcement failed to post, so wrap-up was not marked done.")
            return
        await self._admin_reply(
            ctx,
            "Wrap-up did not post. I couldn't find Menaces to Sobriety or its announcements channel.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CandyHunt(bot))
