"""Playlist encode HTTP 500 recovery. Mocked only: no Lavalink, no yt-dlp, no audio."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from types import SimpleNamespace

import discord
import pytest
import wavelink

from cogs.music import (
    MAX_QUEUE,
    YOUTUBE_PLAYLIST_RECOVERY_LIMIT,
    YTDLP_PLAYLIST_LIST_TIMEOUT_SECONDS,
    Music,
    PlaylistListingError,
    TrackResolve,
    canonical_youtube_url,
    executable_from_ytdlp_wrapper,
    list_youtube_playlist_video_ids,
    loadtracks_http_500,
    parse_ytdlp_playlist,
    queue_result_copy,
    youtube_playlist_id,
    youtube_watch_url,
)

FAILING_URL = (
    "https://www.youtube.com/watch?v=GT28MYh_jlE&list=PLEB9kfPDyYlswXKXJ4nH94stDSq5yeV_q"
)
PLAYLIST_ID = "PLEB9kfPDyYlswXKXJ4nH94stDSq5yeV_q"
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
OPENING = "GT28MYh_jlE"
BROKEN = "a1b2c3d4e5f"
LATER = "Z9y8x7w6v5U"


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("live process or Lavalink call")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(wavelink.Playable, "search", boom)


def http_500(path: str = "/v4/loadtracks", status: int = 500):
    return wavelink.LavalinkException(
        data={
            "timestamp": 0,
            "status": status,
            "error": "Internal Server Error",
            "path": path,
        }
    )


def playable(identifier: str, title: str | None = None):
    return wavelink.Playable(
        data={
            "encoded": identifier,
            "info": {
                "identifier": identifier,
                "isSeekable": True,
                "author": "Artist",
                "length": 180000,
                "isStream": False,
                "position": 0,
                "title": title or identifier,
                "uri": f"https://www.youtube.com/watch?v={identifier}",
                "sourceName": "youtube",
            },
            "pluginInfo": {},
        }
    )


def as_playlist(name: str, tracks: list):
    return wavelink.Playlist(
        data={
            "info": {"name": name, "selectedTrack": -1},
            "pluginInfo": {},
            "tracks": [track.raw_data for track in tracks],
        }
    )


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Ctx:
    def __init__(self) -> None:
        self.sent = []
        self.guild = SimpleNamespace(id=42)

    def typing(self):
        return _Typing()

    async def send(self, *_args, **kwargs):
        self.sent.append(kwargs.get("embed"))


def _music() -> Music:
    music = Music.__new__(Music)
    music._sessions = {}
    music._lavalink_ready = lambda: True
    music._player_starts = []

    def remember(session):
        music._player_starts.append(session.guild_id)

    music._ensure_player_task = remember
    return music


def _queued_ids(music: Music) -> list[str]:
    session = music._session(42)
    found = []
    while not session.music_queue.empty():
        found.append(session.music_queue.get_nowait().track.identifier)
    return found


def test_share_link_still_means_the_whole_playlist():
    assert canonical_youtube_url(FAILING_URL) == PLAYLIST_URL
    assert youtube_playlist_id(FAILING_URL) is None
    assert youtube_playlist_id(PLAYLIST_URL) == PLAYLIST_ID
    assert youtube_playlist_id(canonical_youtube_url(FAILING_URL)) == PLAYLIST_ID
    plain = "https://www.youtube.com/watch?v=GT28MYh_jlE"
    assert canonical_youtube_url(plain) == plain
    assert youtube_playlist_id(plain) is None
    assert youtube_playlist_id("sleep token gods") is None
    assert youtube_playlist_id("https://open.spotify.com/playlist/abc") is None
    radio = "https://www.youtube.com/watch?v=5uhktDv6hSY&list=RD5uhktDv6hSY"
    assert canonical_youtube_url(radio) == "https://www.youtube.com/watch?v=5uhktDv6hSY"
    assert youtube_playlist_id("https://www.youtube.com/playlist?list=RD5uhktDv6hSY") is None
    assert youtube_watch_url(OPENING) == f"https://www.youtube.com/watch?v={OPENING}"
    with pytest.raises(ValueError):
        youtube_watch_url("PL$(whoami)")


def test_only_the_loadtracks_500_is_the_encode_failure():
    failure = http_500()
    assert str(failure) == (
        "Failed to fulfill request to Lavalink: status=500, "
        "reason=Internal Server Error, path=/v4/loadtracks"
    )
    assert loadtracks_http_500(failure)
    assert loadtracks_http_500(http_500(path="/v4/loadtracks?identifier=abc"))
    assert not loadtracks_http_500(http_500(status=503))
    assert not loadtracks_http_500(http_500(path="/v4/sessions/abc"))
    assert not loadtracks_http_500(
        wavelink.LavalinkLoadException(
            data={
                "message": "Something went wrong while looking up the track.",
                "severity": "fault",
                "cause": "java.lang.NullPointerException",
            }
        )
    )
    assert not loadtracks_http_500(wavelink.NodeException("down", status=500))
    assert not loadtracks_http_500(SimpleNamespace(status=500, path="/v4/loadtracks"))


def test_flat_playlist_keeps_a_null_title_and_skips_other_rows():
    ids, malformed, not_attempted = parse_ytdlp_playlist(
        {
            "playlist_count": 6,
            "entries": [
                {"id": OPENING, "ie_key": "Youtube", "title": "Opening"},
                {"id": "not-a-valid-video-id", "ie_key": "Youtube", "title": None},
                {"ie_key": "Youtube", "title": None},
                {"id": BROKEN, "ie_key": "Youtube", "title": None, "uploader": None},
                None,
                {"id": LATER, "ie_key": "Generic", "title": "Elsewhere"},
                {"id": "QWERTYUIOPA", "ie_key": "youtube", "title": "Later"},
            ],
        },
        limit=10,
    )
    assert ids == [OPENING, BROKEN, "QWERTYUIOPA"]
    assert malformed == 2
    assert not_attempted == 0

    capped, _malformed, left = parse_ytdlp_playlist(
        {
            "playlist_count": 5,
            "entries": [
                {"id": OPENING, "ie_key": "Youtube"},
                {"id": LATER, "ie_key": "Youtube"},
            ],
        },
        limit=1,
    )
    assert capped == [OPENING]
    assert left == 4

    ignored, _malformed, left = parse_ytdlp_playlist(
        {"entries": [], "playlist_count": True},
        limit=10,
    )
    assert ignored == []
    assert left == 0
    with pytest.raises(PlaylistListingError):
        parse_ytdlp_playlist({"title": "china EDM"}, limit=10)
    with pytest.raises(PlaylistListingError):
        parse_ytdlp_playlist(["not-an-object"], limit=10)


def test_listing_uses_an_argument_list_and_refuses_shell_fragments(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

        class Result:
            returncode = 0
            stdout = json.dumps(
                {"entries": [{"id": OPENING, "ie_key": "Youtube"}], "playlist_count": 1}
            )
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ids, malformed, left = list_youtube_playlist_video_ids(
        PLAYLIST_ID,
        limit=10,
        executable=r"C:\Program Files\yt-dlp\yt-dlp.exe",
    )
    assert (ids, malformed, left) == ([OPENING], 0, 0)
    assert seen["argv"] == [
        r"C:\Program Files\yt-dlp\yt-dlp.exe",
        "-q",
        "--no-warnings",
        "--flat-playlist",
        "--skip-download",
        "--playlist-items",
        "1:10",
        "-J",
        "--",
        PLAYLIST_URL,
    ]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == YTDLP_PLAYLIST_LIST_TIMEOUT_SECONDS
    assert isinstance(seen["argv"], list)

    def refuse(_argv):
        raise AssertionError("runner should not be called")

    for bad in ("PL$(whoami)", "PLa&calc", "PL id", "RD" + ("a" * 20), "PLabc", "PLEB9\nwhoami"):
        with pytest.raises(PlaylistListingError):
            list_youtube_playlist_video_ids(bad, limit=10, executable="yt-dlp", runner=refuse)
    with pytest.raises(PlaylistListingError):
        list_youtube_playlist_video_ids(
            PLAYLIST_ID,
            limit=YOUTUBE_PLAYLIST_RECOVERY_LIMIT + 1,
            executable="yt-dlp",
            runner=refuse,
        )
    with pytest.raises(PlaylistListingError):
        list_youtube_playlist_video_ids(PLAYLIST_ID, limit=10, executable="yt-dlp", runner=lambda _argv: "nope")


def test_listing_timeout_and_failure_do_not_become_a_shell_command(monkeypatch):
    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(PlaylistListingError, match="timed out"):
        list_youtube_playlist_video_ids(PLAYLIST_ID, limit=2, executable="yt-dlp")

    def failed(_argv, **_kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "ERROR: private playlist"

        return Result()

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(PlaylistListingError, match="private playlist"):
        list_youtube_playlist_video_ids(PLAYLIST_ID, limit=2, executable="yt-dlp")


def test_wrapper_text_yields_the_executable_path_only():
    text = (
        "@echo off\r\n"
        "set PYTHONWARNINGS=ignore\r\n"
        'set LAVALINK_PASSWORD=not-a-real-secret\r\n'
        '"C:\\Program Files\\yt-dlp\\yt-dlp.exe" %*\r\n'
    )
    assert executable_from_ytdlp_wrapper(text) == r"C:\Program Files\yt-dlp\yt-dlp.exe"
    assert executable_from_ytdlp_wrapper("set ONLY=nope\r\n") is None


def test_classic_queue_copy_is_unchanged():
    missed = queue_result_copy("query", 0, 0, TrackResolve([]))
    assert missed.title == "Error"
    assert missed.description == "Could not find any playable tracks for that query."
    assert missed.error

    one = queue_result_copy("https://youtu.be/abc", 1, 0, TrackResolve([]))
    assert one.title == "Queued up"
    assert one.description == "https://youtu.be/abc"
    assert not one.error

    many = queue_result_copy("playlist", 4, 0, TrackResolve([]))
    assert many.description == "4 songs"

    capped = queue_result_copy("playlist", 2, 3, TrackResolve([]))
    assert capped.description == (
        f"2 songs. 3 didn't fit because the queue is capped at {MAX_QUEUE}."
    )
    full = queue_result_copy("playlist", 0, 5, TrackResolve([]))
    assert full.title == "Queued up"
    assert full.description == f"The queue is full ({MAX_QUEUE} tracks). Nothing new was added."
    assert not full.error


def _install_search(monkeypatch, replies: dict):
    calls = []

    async def search(query, source=None, node=None):
        calls.append((query, source))
        reply = replies.get(query, replies.get("*"))
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(query)
        return reply

    monkeypatch.setattr(wavelink.Playable, "search", search)
    return calls


def test_normal_playlist_search_and_plain_video_do_not_split(monkeypatch):
    first = playable(OPENING, title="Opening")
    second = playable(LATER, title="Later")
    calls = _install_search(
        monkeypatch,
        {
            PLAYLIST_URL: as_playlist("china EDM", [first, second]),
            f"https://www.youtube.com/watch?v={OPENING}": [first],
            "https://open.spotify.com/track/abc": [playable("spotify-id", title="Eden")],
        },
    )

    def refuse_list(*_args, **_kwargs):
        raise AssertionError("playlist was split")

    monkeypatch.setattr("cogs.music.list_youtube_playlist_video_ids", refuse_list)
    music = _music()

    async def scenario():
        playlist = await music._resolve_tracks(FAILING_URL)
        video = await music._resolve_tracks(f"https://www.youtube.com/watch?v={OPENING}")
        spotify = await music._resolve_tracks("https://open.spotify.com/track/abc")
        return playlist, video, spotify

    playlist, video, spotify = asyncio.run(scenario())
    assert [track.identifier for track in playlist.tracks] == [OPENING, LATER]
    assert playlist.recovered is False
    assert [track.identifier for track in video.tracks] == [OPENING]
    assert [track.identifier for track in spotify.tracks] == ["spotify-id"]
    assert all(source is None for _query, source in calls)


def test_search_still_tries_spotify_before_youtube_and_picks_one(monkeypatch):
    calls = []

    async def search(query, source=None, node=None):
        calls.append((query, source))
        if source == "spsearch":
            raise wavelink.LavalinkLoadException(
                data={"message": "missing", "severity": "common", "cause": "nope"}
            )
        return [
            playable("otherother1", title="Something else"),
            playable(OPENING, title="Sleep Token Gods"),
            playable("zzzzzzzzzzz", title="Nope"),
        ]

    monkeypatch.setattr(wavelink.Playable, "search", search)
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split")),
    )
    music = _music()

    async def scenario():
        one = await music._resolve_tracks("sleep token gods")
        many = await music._resolve_tracks("sleep token gods", expand_collection=True)
        return one, many

    one, many = asyncio.run(scenario())
    assert [source for _query, source in calls] == ["spsearch", "ytsearch", "spsearch", "ytsearch"]
    assert [track.identifier for track in one.tracks] == [OPENING]
    assert [track.identifier for track in many.tracks] == ["otherother1", OPENING, "zzzzzzzzzzz"]


def test_load_errors_and_non_playlist_500s_are_not_split(monkeypatch):
    calls = []

    async def search(query, source=None, node=None):
        calls.append((query, source))
        if source == "spsearch":
            raise http_500()
        if "spotify" in query:
            raise http_500()
        if query == f"https://www.youtube.com/watch?v={OPENING}":
            raise http_500()
        if query == PLAYLIST_URL:
            raise wavelink.LavalinkLoadException(
                data={"message": "missing", "severity": "fault", "cause": "gone"}
            )
        raise AssertionError(query)

    monkeypatch.setattr(wavelink.Playable, "search", search)
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split")),
    )
    music = _music()

    async def scenario():
        empty = await music._resolve_tracks(PLAYLIST_URL)
        with pytest.raises(wavelink.LavalinkException):
            await music._resolve_tracks(f"https://www.youtube.com/watch?v={OPENING}")
        with pytest.raises(wavelink.LavalinkException):
            await music._resolve_tracks("https://open.spotify.com/playlist/abc")
        with pytest.raises(wavelink.LavalinkException):
            await music._resolve_tracks("sleep token gods")
        return empty

    empty = asyncio.run(scenario())
    assert empty.tracks == []
    assert empty.recovered is False
    assert ("sleep token gods", "ytsearch") not in calls


def test_encode_500_loads_each_video_once_and_skips_bad_rows(monkeypatch):
    listed = []

    def fake_list(playlist_id, *, limit, executable=None, runner=None):
        listed.append((playlist_id, limit))
        return ["nope", OPENING, BROKEN, LATER], 1, 4

    monkeypatch.setattr("cogs.music.list_youtube_playlist_video_ids", fake_list)
    calls = []

    async def search(query, source=None, node=None):
        calls.append(query)
        if query == PLAYLIST_URL:
            raise http_500()
        if query == youtube_watch_url(OPENING):
            return as_playlist("should-not-expand", [playable(OPENING), playable("extraextra11"), playable(LATER)])
        if query == youtube_watch_url(BROKEN):
            raise http_500()
        if query == youtube_watch_url(LATER):
            return [playable(LATER, title="Later")]
        raise AssertionError(query)

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    outcome = asyncio.run(music._resolve_tracks(FAILING_URL))
    assert listed == [(PLAYLIST_ID, YOUTUBE_PLAYLIST_RECOVERY_LIMIT)]
    assert calls == [PLAYLIST_URL, youtube_watch_url(OPENING), youtube_watch_url(BROKEN), youtube_watch_url(LATER)]
    assert [track.identifier for track in outcome.tracks] == [OPENING, LATER]
    assert outcome.recovered is True
    assert outcome.unplayable == 3  # malformed row from the lister, the bad id, and the 500
    assert outcome.not_attempted == 4
    assert all("list=" not in query for query in calls[1:])
    assert calls.count(PLAYLIST_URL) == 1


def test_node_failure_stops_the_walk(monkeypatch):
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda playlist_id, *, limit, executable=None, runner=None: ([OPENING, BROKEN, LATER], 0, 0),
    )
    calls = []

    async def search(query, source=None, node=None):
        calls.append(query)
        if query == PLAYLIST_URL:
            raise http_500()
        if query == youtube_watch_url(OPENING):
            return [playable(OPENING)]
        if query == youtube_watch_url(BROKEN):
            raise wavelink.NodeException("connection failed")
        raise AssertionError(query)

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    outcome = asyncio.run(music._resolve_tracks(FAILING_URL))
    assert [track.identifier for track in outcome.tracks] == [OPENING]
    assert outcome.stopped_early is True
    assert outcome.unplayable == 0
    assert outcome.not_attempted == 2
    assert youtube_watch_url(LATER) not in calls


def test_play_reports_skipped_tracks_and_keeps_queue_order(monkeypatch, caplog):
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda playlist_id, *, limit, executable=None, runner=None: ([OPENING, BROKEN, LATER], 0, 0),
    )

    async def search(query, source=None, node=None):
        if query == PLAYLIST_URL:
            raise http_500()
        if query == youtube_watch_url(BROKEN):
            return None
        identifier = query.rsplit("=", 1)[-1]
        return [playable(identifier)]

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    ctx = _Ctx()
    with caplog.at_level(logging.WARNING, logger="cogs.music"):
        asyncio.run(music._play(ctx, FAILING_URL))
    assert _queued_ids(music) == [OPENING, LATER]
    assert music._player_starts == [42]
    embed = ctx.sent[0]
    assert embed.title == "Queued up"
    assert embed.description == "2 songs. 1 track couldn't be loaded and was skipped."
    assert "didn't fit" not in embed.description
    assert "Could not find" not in embed.description
    assert embed.colour == discord.Colour.blue()
    assert "Failed to resolve tracks" not in caplog.text
    assert PLAYLIST_ID in caplog.text


def test_play_does_not_claim_success_when_every_video_fails(monkeypatch):
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda playlist_id, *, limit, executable=None, runner=None: ([OPENING, BROKEN], 0, 0),
    )

    async def search(query, source=None, node=None):
        if query == PLAYLIST_URL:
            raise http_500()
        raise http_500()

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    ctx = _Ctx()
    asyncio.run(music._play(ctx, FAILING_URL))
    embed = ctx.sent[0]
    assert embed.title == "Error"
    assert embed.description == (
        "Lavalink couldn't encode that playlist (HTTP 500). "
        "None of the 2 tracks I checked could be loaded."
    )
    assert embed.colour == discord.Colour.red()
    assert _queued_ids(music) == []
    assert music._player_starts == []


def test_play_says_when_the_video_list_cannot_be_read(monkeypatch):
    def fail_list(playlist_id, *, limit, executable=None, runner=None):
        raise PlaylistListingError("yt-dlp is not available")

    monkeypatch.setattr("cogs.music.list_youtube_playlist_video_ids", fail_list)

    async def search(query, source=None, node=None):
        if query == PLAYLIST_URL:
            raise http_500()
        raise AssertionError(query)

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    ctx = _Ctx()
    asyncio.run(music._play(ctx, FAILING_URL))
    embed = ctx.sent[0]
    assert embed.title == "Error"
    assert embed.description == (
        "Lavalink couldn't encode that playlist (HTTP 500), "
        "and the video list could not be read. Nothing was queued."
    )
    assert "Could not find any playable tracks" not in embed.description
    assert _queued_ids(music) == []


def test_play_says_when_the_playlist_has_no_videos(monkeypatch):
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda playlist_id, *, limit, executable=None, runner=None: ([], 0, 0),
    )

    async def search(query, source=None, node=None):
        raise http_500()

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    ctx = _Ctx()
    asyncio.run(music._play(ctx, FAILING_URL))
    assert ctx.sent[0].description == (
        "Lavalink couldn't encode that playlist (HTTP 500). "
        "The playlist didn't include any videos. Nothing was queued."
    )


def test_play_keeps_normal_playlist_copy_order_and_shuffle(monkeypatch):
    first = playable(OPENING, title="Opening")
    second = playable(LATER, title="Later")

    async def search(query, source=None, node=None):
        assert "list=" not in query or query == PLAYLIST_URL
        return as_playlist("china EDM", [first, second])

    monkeypatch.setattr(wavelink.Playable, "search", search)
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split")),
    )
    music = _music()
    ctx = _Ctx()
    asyncio.run(music._play(ctx, FAILING_URL))
    assert ctx.sent[0].title == "Queued up"
    assert ctx.sent[0].description == "2 songs"
    assert ctx.sent[0].colour == discord.Colour.blue()
    assert _queued_ids(music) == [OPENING, LATER]

    def reverse(items):
        items.reverse()

    monkeypatch.setattr("cogs.music.random.shuffle", reverse)
    shuffled = _music()
    shuffled_ctx = _Ctx()
    asyncio.run(shuffled._play(shuffled_ctx, FAILING_URL, shuffle=True))
    assert shuffled_ctx.sent[0].description == "2 songs"
    assert _queued_ids(shuffled) == [LATER, OPENING]


def test_plain_video_empty_result_and_spotify_500_keep_the_old_error(monkeypatch, caplog):
    async def search(query, source=None, node=None):
        if source == "spsearch":
            return None
        if "spotify.com" in query:
            raise http_500()
        return []

    monkeypatch.setattr(wavelink.Playable, "search", search)
    monkeypatch.setattr(
        "cogs.music.list_youtube_playlist_video_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split")),
    )
    music = _music()
    missing = _Ctx()
    spotify = _Ctx()
    with caplog.at_level(logging.WARNING, logger="cogs.music"):
        asyncio.run(music._play(missing, f"https://www.youtube.com/watch?v={OPENING}"))
        asyncio.run(music._play(spotify, "https://open.spotify.com/playlist/abc"))
    assert missing.sent[0].description == "Could not find any playable tracks for that query."
    assert spotify.sent[0].title == "Error"
    assert spotify.sent[0].description == "Could not find any playable tracks for that query."
    assert "https://open.spotify.com/playlist/abc" in caplog.text
    assert "loading videos individually" not in caplog.text


def test_queue_cap_wording_stays_on_the_cap(monkeypatch):
    async def search(query, source=None, node=None):
        return [playable(OPENING), playable(LATER)]

    monkeypatch.setattr(wavelink.Playable, "search", search)
    music = _music()
    session = music._session(42)
    for _ in range(MAX_QUEUE - 1):
        session.music_queue.put_nowait(object())
    ctx = _Ctx()
    asyncio.run(music._play(ctx, f"https://www.youtube.com/watch?v={OPENING}"))
    assert ctx.sent[0].description == (
        f"https://www.youtube.com/watch?v={OPENING}. "
        f"1 didn't fit because the queue is capped at {MAX_QUEUE}."
    )
    assert ctx.sent[0].colour == discord.Colour.blue()

    full = _music()
    full_session = full._session(42)
    for _ in range(MAX_QUEUE):
        full_session.music_queue.put_nowait(object())
    full_ctx = _Ctx()
    asyncio.run(full._play(full_ctx, f"https://www.youtube.com/watch?v={OPENING}"))
    assert full_ctx.sent[0].title == "Queued up"
    assert full_ctx.sent[0].description == (
        f"The queue is full ({MAX_QUEUE} tracks). Nothing new was added."
    )
    assert full._player_starts == []


def test_recovery_mentions_the_cap_and_a_stopped_node_separately():
    skipped = queue_result_copy(
        FAILING_URL,
        2,
        0,
        TrackResolve([], unplayable=1, not_attempted=4, recovered=True),
    )
    assert skipped.title == "Queued up"
    assert skipped.description == (
        "2 songs. 1 track couldn't be loaded and was skipped. "
        f"4 more weren't tried because playlist recovery stops at {YOUTUBE_PLAYLIST_RECOVERY_LIMIT}."
    )
    assert "didn't fit" not in skipped.description

    stopped = queue_result_copy(
        FAILING_URL,
        0,
        0,
        TrackResolve([], stopped_early=True, recovered=True),
    )
    assert stopped.error
    assert stopped.description == (
        "Lavalink couldn't encode that playlist (HTTP 500), "
        "then stopped accepting individual track loads. Nothing was queued."
    )
