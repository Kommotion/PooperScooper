"""Weekly card schedule in America/Los_Angeles, including DST weeks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from cogs.utils.stats_schedule import LA, due_week, post_moment_for, split_voice_minutes, week_start_for


def test_week_start_is_the_pacific_monday():
    wednesday = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
    assert wednesday.weekday() == 2
    assert week_start_for(wednesday) == date(2026, 11, 16)

    # Sunday 21:30 PT is already Monday in UTC. The card week stays on the Pacific Monday.
    sunday_night = datetime(2026, 11, 1, 21, 30, tzinfo=LA)
    assert sunday_night.weekday() == 6
    utc = sunday_night.astimezone(timezone.utc)
    assert utc.date() == date(2026, 11, 2)
    assert week_start_for(sunday_night) == date(2026, 10, 26)
    assert week_start_for(utc) == date(2026, 10, 26)


def test_naive_datetimes_are_utc():
    # 07:30 UTC on Monday Nov 16 is still Sunday evening in Pacific time.
    naive = datetime(2026, 11, 16, 7, 30)
    assert week_start_for(naive) == date(2026, 11, 9)


def test_no_post_before_sunday_21():
    previous = date(2026, 11, 2)
    early = datetime(2026, 11, 15, 20, 59, tzinfo=LA)
    assert early.weekday() == 6
    assert due_week(early, previous) is None
    on_time = datetime(2026, 11, 15, 21, 0, tzinfo=LA)
    assert due_week(on_time, previous) == date(2026, 11, 9)


def test_deploy_window_before_sunday_is_still_the_previous_week():
    # Sun Nov 15 21:00 PT through just before Sun Nov 22 21:00 PT.
    # Nothing has been posted, so the due week is Monday Nov 9.
    # Sunday Nov 22 then posts Monday Nov 16, and does not repeat.
    assert due_week(datetime(2026, 11, 15, 21, 0, tzinfo=LA), None) == date(2026, 11, 9)
    assert due_week(datetime(2026, 11, 19, 10, 0, tzinfo=LA), None) == date(2026, 11, 9)
    assert due_week(datetime(2026, 11, 22, 20, 59, tzinfo=LA), None) == date(2026, 11, 9)
    assert due_week(datetime(2026, 11, 22, 20, 59, tzinfo=LA), date(2026, 11, 9)) is None
    on_sunday = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
    assert due_week(on_sunday, date(2026, 11, 9)) == date(2026, 11, 16)
    assert due_week(on_sunday + timedelta(minutes=1), date(2026, 11, 16)) is None
    assert due_week(datetime(2026, 11, 29, 20, 59, tzinfo=LA), date(2026, 11, 16)) is None
    assert due_week(datetime(2026, 11, 29, 21, 0, tzinfo=LA), date(2026, 11, 16)) == date(2026, 11, 23)


def test_missed_sunday_catches_up_once_and_does_not_repeat():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LA)
    assert now.weekday() == 2
    # Several older weeks were missed. Only the latest due Sunday is posted.
    assert due_week(now, None) == date(2026, 11, 9)
    assert due_week(now, date(2026, 10, 26)) == date(2026, 11, 9)
    assert due_week(now, date(2026, 11, 2)) == date(2026, 11, 9)
    assert due_week(now, date(2026, 11, 9)) is None


def test_fall_dst_week_posts_at_21_local():
    # Week of Monday 2026-10-26 posts Sunday 2026-11-01, the day Pacific time falls back.
    assert date(2026, 11, 1).weekday() == 6
    moment = post_moment_for(date(2026, 10, 26))
    local = moment.astimezone(LA)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 11, 1, 21, 0)
    assert local.utcoffset() == timedelta(hours=-8)
    assert moment.astimezone(timezone.utc).date() == date(2026, 11, 2)
    assert week_start_for(moment) == date(2026, 10, 26)
    assert due_week(moment, date(2026, 10, 19)) == date(2026, 10, 26)
    assert due_week(moment - timedelta(minutes=1), date(2026, 10, 19)) is None


def test_spring_dst_week_posts_at_21_local():
    # Week of Monday 2027-03-08 posts Sunday 2027-03-14, the day Pacific time springs forward.
    assert date(2027, 3, 14).weekday() == 6
    moment = post_moment_for(date(2027, 3, 8))
    local = moment.astimezone(LA)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2027, 3, 14, 21, 0)
    assert local.utcoffset() == timedelta(hours=-7)
    assert week_start_for(moment.astimezone(timezone.utc)) == date(2027, 3, 8)
    assert due_week(moment, date(2027, 3, 1)) == date(2027, 3, 8)
    assert due_week(moment - timedelta(minutes=1), date(2027, 3, 1)) is None
    assert due_week(moment, date(2027, 3, 8)) is None


def test_voice_minutes_follow_real_elapsed_time_across_dst():
    start = datetime(2026, 11, 16, 10, 0, tzinfo=LA)
    end = datetime(2026, 11, 16, 11, 30, tzinfo=LA)
    parts = {(weekday, hour): minutes for weekday, hour, minutes in split_voice_minutes(start, end)}
    assert parts == {(0, 10): 60, (0, 11): 30}

    spring_start = datetime(2027, 3, 14, 1, 30, tzinfo=LA)
    spring_end = datetime(2027, 3, 14, 3, 30, tzinfo=LA)
    spring = {hour: minutes for _, hour, minutes in split_voice_minutes(spring_start, spring_end)}
    assert spring == {1: 30, 3: 30}

    # First 1:00 is still PDT. 2:30 is PST. The repeated 1:00 shares one bucket.
    fall_start = datetime(2026, 11, 1, 1, 0, tzinfo=LA)
    fall_end = datetime(2026, 11, 1, 2, 30, tzinfo=LA)
    fall = {hour: minutes for _, hour, minutes in split_voice_minutes(fall_start, fall_end)}
    assert fall == {1: 120, 2: 30}
    assert sum(fall.values()) == 150

    short = start + timedelta(seconds=59)
    assert split_voice_minutes(start, short) == []
