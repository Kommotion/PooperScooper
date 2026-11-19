"""Render fake weekly-stats dashboards so the four designs can be compared.

The dataset goes through StatsStore. Nothing here is a real server.
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.utils.stats_card import CardNames
from cogs.utils.stats_designs import DESIGNS, render_design
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore

GUILD = 424242
GUILD_NAME = "FAKE DATA - Preview Server"
PREV_MONDAY = date(2026, 11, 9)
CUR_MONDAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 22, 20, 0, tzinfo=LA)

# id, name, messages this week, messages last week
CHANNELS = (
    (101, "general", 486, 310),
    (102, "off-topic", 352, 240),
    (103, "memes", 274, 180),
    (104, "image-diffusion-showcase", 198, 90),
    (105, "illuminaughty", 166, 140),
    (106, "bot-spam", 121, 70),
    (107, "music-chat", 88, 60),
    (108, "gaming", 67, 55),
    (109, "clips", 49, 40),
    (110, "pets", 36, 22),
    (111, "food", 21, 18),
    (112, "vent", 12, 9),
    (113, "introductions", 7, 6),
    (114, "lfg", 3, 4),
    (115, "announcements", 0, 14),
    (116, "rules", 0, 2),
    (117, "mod-log", 0, 8),
    (118, "welcome", 0, 0),
    (119, "archive", 0, 5),
    (120, "staff-notes", 0, 1),
)

VOICE = (
    (201, "Lounge"),
    (202, "Gaming VC"),
    (203, "Music"),
    (204, "Late Night"),
)

# key, name, custom, count
REACT_CURRENT = (
    ("😂", "😂", False, 180),
    ("❤️", "❤️", False, 110),
    ("🔥", "🔥", False, 74),
    ("👍", "👍", False, 52),
    ("😭", "😭", False, 36),
    ("👀", "👀", False, 24),
    ("💀", "💀", False, 16),
    ("c1", "kekw", True, 14),
    ("🎉", "🎉", False, 9),
    ("c2", "pepega", True, 5),
    ("c3", "monkas", True, 2),
)
REACT_PREVIOUS = (
    ("😂", "😂", False, 260),
    ("❤️", "❤️", False, 190),
    ("🔥", "🔥", False, 120),
    ("👍", "👍", False, 80),
    ("😭", "😭", False, 55),
    ("👀", "👀", False, 40),
    ("💀", "💀", False, 30),
    ("c1", "kekw", True, 48),
    ("🎉", "🎉", False, 22),
    ("c2", "pepega", True, 28),
    ("c3", "monkas", True, 18),
    ("c4", "sadge", True, 6),
    ("c5", "copium", True, 2),
)
CUSTOM_EMOJIS = (
    ("c1", "kekw"),
    ("c2", "pepega"),
    ("c3", "monkas"),
    ("c4", "sadge"),
    ("c5", "copium"),
    ("c6", "weirdchamp"),
)

# name, kind, this week, last week
COMMANDS = (
    ("imagegen", "slash", 92, 48),
    ("play", "slash", 71, 66),
    ("skip", "slash", 44, 39),
    ("queue", "slash", 31, 20),
    ("imagine", "slash", 24, 11),
    ("grok", "slash", 18, 4),
    ("remind", "slash", 11, 9),
    ("candy", "slash", 7, 15),
    ("turkeys", "slash", 4, 2),
    ("stats", "slash", 3, 1),
    ("statscard", "prefix", 2, 6),
    ("sync", "prefix", 1, 3),
)

# model, successful runs, sampler steps. Errors are counted separately.
CURRENT_SUCCESS = (
    ("anima", 26, 28),
    ("wai", 20, 28),
    ("krea2", 16, 8),
    ("krea2_identity_edit", 14, 8),
    ("qwen", 10, 12),
    ("klein", 8, 8),
)
CURRENT_ERRORS = (("krea2", 3), ("qwen", 2), ("anima", 1))
PREVIOUS_SUCCESS = (
    ("anima", 16, 28),
    ("wai", 11, 28),
    ("krea2", 7, 8),
    ("krea2_identity_edit", 4, 8),
    ("qwen", 5, 12),
    ("klein", 3, 8),
)
PREVIOUS_ERRORS = (("krea2", 2), ("anima", 1))

# Pacific hour weights. Quiet 3–9am, building through the afternoon, peak 8–11pm.
_HOUR_WEIGHT = (
    0.50, 0.32, 0.16, 0.07, 0.05, 0.04, 0.05, 0.07,
    0.14, 0.30, 0.48, 0.66, 0.82, 0.95, 1.10, 1.28,
    1.48, 1.70, 1.95, 2.20, 2.55, 2.65, 2.40, 1.55,
)


def _moment(monday: date, day: int, hour: int, seq: int) -> datetime:
    return datetime(monday.year, monday.month, monday.day, hour, seq % 60, min(seq % 50, 59), tzinfo=LA) + timedelta(days=day)


def _activity_weight(day: int, hour: int, rng: random.Random) -> float:
    weight = _HOUR_WEIGHT[hour]
    if day >= 5 and 12 <= hour <= 18:
        weight *= 1.75
    if day in (4, 5) and (hour >= 21 or hour <= 1):
        weight *= 1.85
    if day == 6 and hour >= 22:
        weight *= 0.72
    if day == 0 and hour < 11:
        weight *= 0.82
    return max(0.01, weight * rng.uniform(0.55, 1.50))


def _pick_cells(count: int, rng: random.Random) -> list[tuple[int, int]]:
    if count <= 0:
        return []
    weights = [_activity_weight(day, hour, rng) for day in range(7) for hour in range(24)]
    return [divmod(pick, 24) for pick in rng.choices(range(168), weights=weights, k=count)]


def _spray_messages(store: StatsStore, monday: date, week_index: int, users: list[int], rng: random.Random) -> None:
    user_index = 0
    for channel_id, _name, current, previous in CHANNELS:
        count = current if week_index == 1 else previous
        for step, (day, hour) in enumerate(_pick_cells(count, rng)):
            store.record_message(GUILD, channel_id, users[user_index % len(users)], _moment(monday, day, hour, step + user_index))
            user_index += 1


def _spray_reactions(store: StatsStore, monday: date, rows, users: list[int], rng: random.Random) -> None:
    seq = 0
    for key, name, custom, count in rows:
        for step, (day, hour) in enumerate(_pick_cells(count, rng)):
            store.record_reaction(
                GUILD,
                users[seq % len(users)],
                key,
                name=name,
                is_custom=custom,
                when=_moment(monday, day, hour, seq + step),
            )
            seq += 1


def _spray_commands(store: StatsStore, week_index: int) -> None:
    for name, kind, current, previous in COMMANDS:
        count = current if week_index == 1 else previous
        for _step in range(count):
            store.record_command(GUILD, name, kind)


def _generation_seconds(rng: random.Random) -> float:
    # Triangular 15–45s, mode 26s. Expectation is (15+45+26)/3 ≈ 28.7s.
    return round(min(45.0, max(15.0, rng.triangular(15.0, 45.0, 26.0))), 1)


def _resolution(rng: random.Random, model: str) -> tuple[int, int]:
    roll = rng.random()
    if model == "krea2_identity_edit":
        if roll < 0.72:
            return 1024, 1024
        if roll < 0.90:
            return 832, 1216
        return 1216, 832
    if roll < 0.58:
        return 1024, 1024
    if roll < 0.82:
        return 832, 1216
    if roll < 0.94:
        return 1216, 832
    return 768, 1344


def _spray_images(store: StatsStore, successes, errors, regen: int, edits: int, rng: random.Random) -> None:
    for model, count, steps in successes:
        for _step in range(count):
            width, height = _resolution(rng, model)
            store.record_imagegen(
                GUILD,
                model=model,
                edit_mode="identity" if model == "krea2_identity_edit" else None,
                width=width,
                height=height,
                steps=steps,
                sampler="euler",
                seconds=_generation_seconds(rng),
                success=True,
            )
    for model, count in errors:
        for _step in range(count):
            store.record_imagegen(
                GUILD,
                model=model,
                seconds=_generation_seconds(rng),
                success=False,
            )
    for _step in range(regen):
        store.record_imagegen_followup(GUILD, "regenerate")
    for _step in range(edits):
        store.record_imagegen_followup(GUILD, "edit")


def _voice_session(store: StatsStore, monday: date, channel_id: int, user_id: int, day: int, hour: int, minute: int, duration: int) -> None:
    start = datetime(monday.year, monday.month, monday.day, hour, minute, tzinfo=LA) + timedelta(days=day)
    store.record_voice(GUILD, channel_id, user_id, start, start + timedelta(minutes=duration))


def _spray_voice(store: StatsStore, monday: date, week_index: int, rng: random.Random) -> None:
    """Evenings 7pm–1am, plus weekend afternoons. Previous week is the same shape, shorter."""
    people = (8001, 8002, 8003, 8004, 8005, 8006)
    scale = 1.0 if week_index == 1 else 0.62
    evening_people = 3 if week_index == 1 else 2
    for day in range(7):
        for index in range(evening_people):
            start_hour = rng.choice((19, 19, 20, 20, 21, 22))
            start_min = rng.randrange(0, 50, 10)
            duration = max(25, int(rng.randint(35, 100) * scale))
            channel = 201 if index == 0 else rng.choice((201, 202, 203))
            _voice_session(store, monday, channel, people[index], day, start_hour, start_min, duration)
        if day in (4, 5):
            _voice_session(
                store,
                monday,
                204,
                people[5],
                day,
                rng.choice((22, 23)),
                rng.choice((0, 15, 30)),
                max(30, int(rng.randint(55, 110) * scale)),
            )
        if day >= 5:
            _voice_session(
                store,
                monday,
                202,
                people[1],
                day,
                rng.choice((13, 14)),
                rng.choice((0, 20)),
                max(30, int(rng.randint(50, 100) * scale)),
            )
            _voice_session(
                store,
                monday,
                202,
                people[2],
                day,
                rng.choice((15, 16)),
                rng.choice((0, 15, 30)),
                max(25, int(rng.randint(40, 85) * scale)),
            )


def build_preview(db_path: Path):
    """Return full stats, display names, and an empty guild's stats."""
    store = StatsStore(db_path)
    try:
        rng = random.Random(280)
        previous_users = list(range(8000, 8036))
        current_users = list(range(8000, 8052))
        _spray_messages(store, PREV_MONDAY, 0, previous_users, rng)
        _spray_reactions(store, PREV_MONDAY, REACT_PREVIOUS, previous_users, rng)
        _spray_commands(store, 0)
        _spray_images(store, PREVIOUS_SUCCESS, PREVIOUS_ERRORS, 6, 3, rng)
        _spray_voice(store, PREV_MONDAY, 0, rng)
        closed = store.close_week(GUILD, PREV_MONDAY, datetime(2026, 11, 15, 21, tzinfo=LA))
        if closed is None:
            raise RuntimeError("fake previous week did not snapshot")
        _spray_messages(store, CUR_MONDAY, 1, current_users, rng)
        _spray_reactions(store, CUR_MONDAY, REACT_CURRENT, current_users, rng)
        _spray_commands(store, 1)
        _spray_images(store, CURRENT_SUCCESS, CURRENT_ERRORS, 16, 9, rng)
        _spray_voice(store, CUR_MONDAY, 1, rng)
        stats = store.build_stats(
            GUILD,
            NOW,
            week_start=CUR_MONDAY,
            known_channel_ids=[channel_id for channel_id, *_rest in CHANNELS],
            known_custom_emojis=CUSTOM_EMOJIS,
        )
        names = CardNames(
            {channel_id: name for channel_id, name, *_rest in CHANNELS} | {channel_id: name for channel_id, name in VOICE},
            {user_id: f"LeakUser{user_id}" for user_id in current_users},
            {key: name for key, name in CUSTOM_EMOJIS},
        )
        empty = store.build_stats(999, NOW)
        return stats, names, empty
    finally:
        store.close()


def write_previews(out_dir: Path, stats, names, empty) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key in DESIGNS:
        full = out_dir / f"stats_design_{key}.png"
        blank = out_dir / f"stats_design_empty_{key}.png"
        full.write_bytes(render_design(key, stats, names, GUILD_NAME, watermark="FAKE DATA"))
        blank.write_bytes(render_design(key, empty, CardNames({}, {1: "LeakUser1"}, {}), GUILD_NAME, watermark="FAKE DATA"))
        written.extend((full, blank))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render fake weekly stats dashboard designs.")
    parser.add_argument("--out", default=None, help="Directory for the PNGs. Defaults to the temp pooper-stats-preview folder.")
    args = parser.parse_args(argv)
    out_dir = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "pooper-stats-preview"
    db_dir = Path(tempfile.mkdtemp(prefix="pooperscooper-stats-"))
    stats, names, empty = build_preview(db_dir / "weekly_stats.sqlite3")
    paths = write_previews(out_dir, stats, names, empty)
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
