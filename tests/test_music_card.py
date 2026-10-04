"""Now-playing card layout, reuse rules, and per-server playback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord

from cogs.music import (
    DEFAULT_VOLUME,
    ENDED_ACCENT,
    MAX_QUEUE,
    PAUSED_ACCENT,
    PLAYING_ACCENT,
    TRACK_END_GRACE_SECONDS,
    GuildSession,
    Music,
    NowPlayingView,
    discard_queued,
    drain_queue,
    format_track_length,
    linked_track_title,
    message_is_latest,
    end_event_applies,
    fault_event_applies,
    playback_will_continue,
    repeat_target,
    should_reuse_now_playing,
    stale_end_during_startup,
    started_track_is_playing,
    take_for_queue,
    track_end_should_advance,
    track_should_give_up,
    track_wait_timeout,
)


class _Queue:
    def __init__(self, size: int) -> None:
        self._size = size

    def qsize(self) -> int:
        return self._size


class _Cog:
    def __init__(self, *, queue: int = 2, repeat: bool = False, loop: bool = False, shuffle: bool = False) -> None:
        self.music_queue = _Queue(queue)
        self.repeat_enabled = repeat
        self.loop_enabled = loop
        self.shuffle_mode = shuffle


class _Author:
    def __init__(self, name: str) -> None:
        self.display_name = name
        self.name = name


class _Ctx:
    def __init__(self, name: str) -> None:
        self.message = type("Msg", (), {"author": _Author(name)})()


class _Track:
    def __init__(self, **kwargs) -> None:
        self.title = kwargs.get("title", "Midnight City")
        self.author = kwargs.get("author", "M83")
        self.uri = kwargs.get("uri", "https://example.com/midnight")
        self.artwork = kwargs.get("artwork", "https://example.com/art.jpg")
        self.length = kwargs.get("length", 243000)


class _Entry:
    def __init__(self, track: _Track, requester: str = "Jacqu") -> None:
        self.track = track
        self.ctx = _Ctx(requester)


def _card(cog: _Cog | None = None, **track_kwargs) -> NowPlayingView:
    return NowPlayingView(cog or _Cog(), _Entry(_Track(**track_kwargs)))


def _texts(view: NowPlayingView) -> str:
    chunks = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks)


def _buttons(view: NowPlayingView) -> list[discord.ui.Button]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def _walk_payload(components):
    for component in components:
        yield component
        nested = component.get("components")
        if isinstance(nested, list):
            yield from _walk_payload(nested)
        accessory = component.get("accessory")
        if isinstance(accessory, dict):
            yield accessory


def test_track_length_formatting():
    assert format_track_length(None) is None
    assert format_track_length(0) is None
    assert format_track_length(125000) == "2:05"
    assert format_track_length(3661000) == "1:01:01"


def test_title_is_a_safe_link():
    assert linked_track_title("A_B *live*", "https://example.com/a") == r"[A\_B \*live\*](https://example.com/a)"
    assert linked_track_title("Song [Live]", "https://example.com/a(1)") == r"Song \[Live\]"
    assert "http" not in linked_track_title("Plain", None)


def test_reuse_only_when_the_card_is_still_the_newest_message():
    assert message_is_latest(candidate_id=5, newest_id=5)
    assert not message_is_latest(candidate_id=5, newest_id=9)
    assert not message_is_latest(candidate_id=None, newest_id=5)
    assert should_reuse_now_playing(same_channel=True, is_latest=True)
    assert not should_reuse_now_playing(same_channel=True, is_latest=False)
    assert not should_reuse_now_playing(same_channel=False, is_latest=True)


def test_playback_continues_for_repeat_or_a_queued_track():
    assert not playback_will_continue(
        stopping=True, repeat_enabled=True, has_repeated_entry=True, queue_empty=False
    )
    assert playback_will_continue(
        stopping=False, repeat_enabled=True, has_repeated_entry=True, queue_empty=True
    )
    assert not playback_will_continue(
        stopping=False, repeat_enabled=True, has_repeated_entry=False, queue_empty=True
    )
    assert playback_will_continue(
        stopping=False, repeat_enabled=False, has_repeated_entry=False, queue_empty=False
    )
    assert not playback_will_continue(
        stopping=False, repeat_enabled=False, has_repeated_entry=False, queue_empty=True
    )


def test_buttons_sit_inside_the_card_and_the_disc_is_a_small_thumbnail():
    view = _card(_Cog(queue=2, repeat=True))
    assert view.has_components_v2()
    assert len(view.children) == 1
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    assert container.accent_colour == PLAYING_ACCENT
    assert not any(isinstance(child, discord.ui.ActionRow) for child in view.children)
    rows = [child for child in container.children if isinstance(child, discord.ui.ActionRow)]
    assert len(rows) == 2

    payload = view.to_components()
    assert payload[0]["type"] == discord.ComponentType.container.value
    flat = list(_walk_payload(payload))
    assert discord.ComponentType.media_gallery.value not in [item["type"] for item in flat]
    assert discord.ComponentType.file.value not in [item["type"] for item in flat]
    urls = [
        item["media"]["url"]
        for item in flat
        if item.get("type") == discord.ComponentType.thumbnail.value
    ]
    assert urls[0] == Music.disc_attachment_url(is_playing=True)
    assert urls[0].endswith("spinning_disc.gif")
    assert "https://example.com/art.jpg" in urls

    buttons = _buttons(view)
    assert [button.custom_id for button in buttons] == [
        "np_skip",
        "np_stop",
        "np_pause",
        "np_resume",
        "np_repeat",
        "np_loop",
        "np_shuffle",
    ]
    repeat = next(button for button in buttons if button.custom_id == "np_repeat")
    loop = next(button for button in buttons if button.custom_id == "np_loop")
    assert repeat.style == discord.ButtonStyle.primary
    assert loop.style == discord.ButtonStyle.secondary

    text = _texts(view)
    assert "NOW PLAYING" in text
    assert "Midnight City" in text
    assert "https://example.com/midnight" in text
    assert "M83" in text
    assert "4:03" in text
    assert "▶️ **Playing** · **2** in queue" in text
    assert "Requested by **Jacqu**" in text
    assert "🔂 Repeat" in text

    entry = _Entry(_Track())
    entry.started_at = datetime(2026, 10, 4, tzinfo=timezone.utc)
    stamped = NowPlayingView(_Cog(), entry)
    stamp = int(entry.started_at.timestamp())
    assert f"<t:{stamp}:R>" in _texts(stamped)
    assert f"<t:{stamp}:R>" not in _texts(NowPlayingView(_Cog(), entry, ended=True))


def test_paused_and_ended_cards_use_the_static_disc_and_drop_the_banner():
    paused = NowPlayingView(_Cog(queue=0), _Entry(_Track(artwork=None, author="*Jay*")), is_paused=True)
    assert paused.children[0].accent_colour == PAUSED_ACCENT
    paused_text = _texts(paused)
    assert "PAUSED" in paused_text
    assert r"\*Jay\*" in paused_text
    assert "Queue clear" in paused_text
    paused_urls = [
        item.media.url for item in paused.walk_children() if isinstance(item, discord.ui.Thumbnail)
    ]
    assert paused_urls == [Music.disc_attachment_url(is_playing=False)]

    ended = NowPlayingView(_Cog(), _Entry(_Track()), ended=True)
    assert ended.children[0].accent_colour == ENDED_ACCENT
    assert _buttons(ended) == []
    assert "PLAYBACK ENDED" in _texts(ended)
    assert "Queue another track to keep it going" in _texts(ended)
    ended_flat = list(_walk_payload(ended.to_components()))
    assert discord.ComponentType.action_row.value not in [item["type"] for item in ended_flat]
    assert discord.ComponentType.media_gallery.value not in [item["type"] for item in ended_flat]


def test_unknown_length_waits_only_while_audio_is_up():
    assert track_wait_timeout(None) is None
    assert track_wait_timeout(0) is None
    assert track_wait_timeout(10_000) == 10 + TRACK_END_GRACE_SECONDS

    assert not track_should_give_up(
        length_ms=None,
        elapsed_seconds=TRACK_END_GRACE_SECONDS,
        playing=True,
        paused=False,
        voice_dead=False,
    )
    assert not track_should_give_up(
        length_ms=None,
        elapsed_seconds=TRACK_END_GRACE_SECONDS,
        playing=False,
        paused=True,
        voice_dead=False,
    )
    assert track_should_give_up(
        length_ms=None,
        elapsed_seconds=TRACK_END_GRACE_SECONDS,
        playing=False,
        paused=False,
        voice_dead=False,
    )
    assert not track_should_give_up(
        length_ms=None,
        elapsed_seconds=TRACK_END_GRACE_SECONDS - 1,
        playing=False,
        paused=False,
        voice_dead=False,
    )
    assert track_should_give_up(
        length_ms=180_000,
        elapsed_seconds=1,
        playing=True,
        paused=False,
        voice_dead=True,
    )
    assert not track_should_give_up(
        length_ms=10_000,
        elapsed_seconds=29,
        playing=True,
        paused=False,
        voice_dead=False,
    )
    assert track_should_give_up(
        length_ms=10_000,
        elapsed_seconds=30,
        playing=True,
        paused=False,
        voice_dead=False,
    )


class _IdTrack:
    def __init__(self, encoded: str) -> None:
        self.encoded = encoded


class _Player:
    def __init__(self, current, *, playing: bool = False, paused: bool = False) -> None:
        self.current = current
        self.playing = playing
        self.paused = paused


def test_replaced_track_end_does_not_skip_the_song_just_started():
    waiting = _IdTrack("new")
    previous = _IdTrack("old")
    assert track_end_should_advance("finished")
    assert track_end_should_advance("loadFailed")
    assert track_end_should_advance("stopped")
    assert track_end_should_advance("cleanup")
    assert not track_end_should_advance("replaced")

    assert not end_event_applies(reason="replaced", waiting_track=waiting, ended_track=previous)
    assert not end_event_applies(reason="replaced", waiting_track=waiting, ended_track=waiting)
    assert not end_event_applies(reason="loadFailed", waiting_track=waiting, ended_track=previous)
    assert end_event_applies(reason="finished", waiting_track=waiting, ended_track=_IdTrack("new"))
    assert end_event_applies(reason="loadFailed", waiting_track=waiting, ended_track=waiting)
    assert end_event_applies(reason="finished", waiting_track=None, ended_track=previous)
    assert not fault_event_applies(waiting_track=waiting, failed_track=previous)
    assert fault_event_applies(waiting_track=waiting, failed_track=_IdTrack("new"))

    assert not stale_end_during_startup(event_is_set=False, started_track_is_playing=True)
    assert not stale_end_during_startup(event_is_set=True, started_track_is_playing=False)
    assert stale_end_during_startup(event_is_set=True, started_track_is_playing=True)
    assert started_track_is_playing(_Player(waiting, playing=True), waiting)
    assert started_track_is_playing(_Player(waiting, paused=True), waiting)
    assert not started_track_is_playing(_Player(previous, playing=True), waiting)
    assert not started_track_is_playing(_Player(waiting, playing=False), waiting)


def test_queue_cap_leaves_room_for_tracks_already_waiting():
    assert take_for_queue(0, 10) == 10
    assert take_for_queue(MAX_QUEUE - 1, 5) == 1
    assert take_for_queue(MAX_QUEUE, 5) == 0
    assert take_for_queue(0, 0) == 0
    assert take_for_queue(10, 5, limit=12) == 2


def test_repeat_turned_on_mid_song_keeps_the_current_track():
    current = object()
    held = object()
    assert repeat_target(enabled=True, repeated_entry=None, current_entry=current) is current
    assert repeat_target(enabled=True, repeated_entry=held, current_entry=current) is held
    assert repeat_target(enabled=False, repeated_entry=held, current_entry=current) is None


def test_each_server_has_its_own_queue_and_modes():
    one = GuildSession(1)
    two = GuildSession(2)
    one.music_queue.put_nowait("a")
    two.music_queue.put_nowait("b")
    two.music_queue.put_nowait("c")
    one.repeat_enabled = True
    one.volume = 40

    assert one.music_queue.qsize() == 1
    assert two.music_queue.qsize() == 2
    assert not two.repeat_enabled
    assert two.volume == DEFAULT_VOLUME
    assert one.drain() == 1
    assert one.music_queue.qsize() == 0
    assert two.music_queue.qsize() == 2

    one.loop_enabled = True
    one.shuffle_mode = True
    one.repeated_entry = object()
    one.reset_modes()
    assert not one.repeat_enabled
    assert one.repeated_entry is None
    assert not one.loop_enabled
    assert not one.shuffle_mode


def test_discard_removes_one_looped_copy_and_keeps_order():
    queue = asyncio.Queue()
    first = object()
    looped = object()
    last = object()
    for item in (first, looped, last, looped):
        queue.put_nowait(item)
    assert discard_queued(queue, looped)
    assert queue.get_nowait() is first
    assert queue.get_nowait() is last
    assert queue.get_nowait() is looped
    assert queue.empty()


def test_card_reads_the_guild_session_when_one_is_attached():
    cog = _Cog(queue=9, repeat=False, loop=False, shuffle=False)
    session = GuildSession(123)
    session.music_queue.put_nowait(object())
    session.music_queue.put_nowait(object())
    session.repeat_enabled = True
    session.loop_enabled = True
    view = NowPlayingView(cog, _Entry(_Track()), session=session)
    text = _texts(view)
    assert "**2** in queue" in text
    assert "🔂 Repeat" in text
    assert "🔁 Loop" in text
    repeat = next(button for button in _buttons(view) if button.custom_id == "np_repeat")
    assert repeat.style == discord.ButtonStyle.primary


def test_a_waiting_server_does_not_take_another_servers_track():
    async def scenario():
        music = Music.__new__(Music)
        music._sessions = {}
        one = music._session(1)
        two = music._session(2)
        one.put("from-one")
        two.put("from-two")
        assert await music._get_entry(two) == "from-two"
        assert await music._get_entry(one) == "from-one"

        waiting = music._session(3)

        async def later():
            await asyncio.sleep(0)
            async with waiting.lock:
                waiting.put("after")

        asyncio.create_task(later())
        assert await music._get_entry(waiting) == "after"
        assert one.music_queue.empty()
        assert two.music_queue.empty()

    asyncio.run(scenario())


def test_a_track_queued_as_the_player_exits_starts_another_player():
    async def scenario():
        music = Music.__new__(Music)
        music._sessions = {}
        started: list[int] = []

        def fake_ensure(session):
            started.append(session.guild_id)
            session.running = True
            session.task = None

        music._ensure_player_task = fake_ensure
        session = music._session(4)

        async def exiting():
            session.music_queue.put_nowait("late")
            await music._finish_player_task(session)

        session.task = asyncio.create_task(exiting())
        await session.task
        assert started == [4]
        assert session.music_queue.qsize() == 1

        idle = music._session(5)

        async def leaving():
            await music._finish_player_task(idle)

        idle.task = asyncio.create_task(leaving())
        await idle.task
        assert started == [4]
        assert idle.task is None
        assert not idle.running

    asyncio.run(scenario())
