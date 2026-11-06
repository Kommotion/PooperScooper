"""Confirmed-preview-failure fallback. Simulated responses only.

These tests never call Discord, vx, or Fx. Nothing here is a live broken
video. Temporary files are the cog's own short-lived directories.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import discord
import pytest

import cogs.malfoyd as malfoyd_module
from cogs.malfoyd import (
    HEADING,
    build_content,
    make_view,
    valid_candidates,
)
from cogs.malfoyd_media import (
    API_TIMEOUT,
    API_UA,
    DISCORD_UA,
    HTML_CAP,
    JSON_CAP,
    MAX_CANDIDATE_ATTEMPTS,
    MAX_MEDIA,
    MEDIA_TIMEOUT,
    OPERATION_SECONDS,
    PREVIEW_ATTEMPTS,
    PREVIEW_TIMEOUT,
    AiohttpExchange,
    AiohttpOpened,
    Budget,
    DownloadResult,
    Inspection,
    MediaClient,
    PreviewStatus,
    PublicResolver,
    UnsafeAddress,
    api_urls,
    assert_public_resolution,
    classify_html,
    content_length,
    ip_is_public,
    media_urls_from_payload,
    mp4_compatible,
    sniff_media,
    validate_public_https,
)
from tests.test_malfoyd import (
    TODAY,
    VX,
    FakeGuild,
    FakeMember,
    FakeRole,
    FakeThread,
    _button,
    _candidate,
    _cog,
    _http,
    _interaction,
    _queue,
    _saved,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4
FAIL_HTML = b"""<!doctype html><html><head>
<meta property="og:description" content="Failed to scan your link!">
</head><body>Failed to scan your link!</body></html>"""
OK_HTML = b"""<!doctype html><html><head>
<meta property="og:description" content="a normal tweet">
<meta property="og:video" content="https://video.twimg.com/amplify_video/ok.mp4">
<meta property="og:image" content="https://pbs.twimg.com/media/ok.jpg">
</head></html>"""
AGE_HTML = b"""<!doctype html><html><head>
<meta property="og:description" content="Age-restricted adult content. This Tweet might include sensitive content.">
</head><body>age-restricted</body></html>"""
UNAVAILABLE = (
    "Failed to scan your link!",
    "Tweet not found.",
    "This Tweet is from a suspended account.",
    "This post is unavailable :(",
    "This Tweet is unavailable.",
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _mp4(*extra: bytes) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x00\x00" + b"isom" + b"avc1")
    moov = _box(b"moov", b"avc1")
    mdat = _box(b"mdat", b"\x00\x00")
    return ftyp + moov + mdat + b"".join(extra)


MP4 = _mp4()
JPEG_URL = "https://pbs.twimg.com/media/one.jpg"
PNG_URL = "https://pbs.twimg.com/media/two.png"
MP4_URL = "https://video.twimg.com/ext_tw_video/one.mp4"


def _preview(number: int) -> str:
    return f"{VX}{number}"


def _vx(number: int) -> str:
    return f"https://api.vxtwitter.com/user/status/{number}"


def _fx(number: int) -> str:
    return f"https://api.fxtwitter.com/user/status/{number}"


def _vx_body(items: list[tuple[str, str]]) -> bytes:
    return json.dumps(
        {
            "media_extended": [
                {
                    "type": kind,
                    "url": url,
                    "thumbnail_url": "https://evil.example/thumb.jpg",
                    "altText": "keep",
                }
                for kind, url in items
            ],
            "mediaURLs": [url for _, url in items],
        }
    ).encode()


class _Pairs:
    def __init__(self, pairs):
        self._pairs = list(pairs)

    def items(self):
        return iter(self._pairs)


class Opened:
    def __init__(self, body: bytes, headers, status: int = 200):
        self.body = body
        self.headers = headers
        self.status = status
        self.url = ""
        self.reads = 0
        self.streams = 0
        self.read_caps: list[int] = []
        self.stream_caps: list[int] = []
        self.released = False

    async def read(self, cap: int):
        self.reads += 1
        self.read_caps.append(cap)
        if len(self.body) > cap:
            return self.body[:cap], True
        return self.body, False

    async def stream_to(self, path, cap: int):
        self.streams += 1
        self.stream_caps.append(cap)
        if len(self.body) > cap:
            return 0, True
        Path(path).write_bytes(self.body)
        return len(self.body), False

    async def release(self):
        self.released = True


def opened(body: bytes = b"", *, status: int = 200, headers=None, length="auto") -> Opened:
    if headers is not None and type(headers) is not dict:
        return Opened(body, headers, status)
    hdrs = dict(headers or {})
    if length == "omit":
        pass
    elif length == "auto":
        if not any(str(key).lower() == "content-length" for key in hdrs):
            hdrs["Content-Length"] = str(len(body))
    else:
        hdrs["Content-Length"] = str(length)
    return Opened(body, hdrs, status)


def redirect(location: str, status: int = 302) -> Opened:
    return Opened(b"", {"Location": location}, status)


class Exchange:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []
        self.opened: list[Opened] = []

    async def open(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        item = self.plan(url, headers, timeout) if callable(self.plan) else self._lookup(url)
        if isinstance(item, Exception):
            raise item
        item.url = url
        self.opened.append(item)
        return item

    def _lookup(self, url):
        if url not in self.plan:
            raise AssertionError(f"unexpected URL {url}")
        item = self.plan[url]
        if isinstance(item, list):
            if not item:
                raise AssertionError(f"no scripted response left for {url}")
            return item.pop(0)
        return item

    def urls(self) -> list[str]:
        return [item["url"] for item in self.calls]

    def assert_released(self):
        leaked = [item.url for item in self.opened if not item.released]
        assert leaked == []


def _budget(**kwargs) -> Budget:
    now = kwargs.pop("now", None) or (lambda: 0.0)
    requests = kwargs.pop("requests", 48)
    seconds = kwargs.pop("seconds", 60)
    return Budget(now, requests=requests, seconds=seconds)


def _track_temp(monkeypatch) -> list[str]:
    created: list[str] = []

    class Track(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    monkeypatch.setattr(malfoyd_module.tempfile, "TemporaryDirectory", Track)
    return created


def _assert_gone(created: list[str]):
    assert created
    assert all(not Path(path).exists() for path in created)


def _controls(view, remaining: int):
    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        "malfoyd:notify",
        "malfoyd:reroll",
    ]
    button = _button(view, "malfoyd:reroll")
    assert button.label == f"Reroll ({remaining} left)"
    assert button.disabled is (remaining <= 0)


def _build(
    tmp_path,
    queue,
    *,
    plan=None,
    media=None,
    limit=8 * 1024 * 1024,
    role=77,
    monotonic=None,
    thread=None,
    guild=None,
    settings=None,
):
    exchange = None
    if media is None:
        exchange = Exchange(plan if plan is not None else {})
        media = MediaClient(exchange)
    if guild is None:
        guild = FakeGuild()
        if limit is not None:
            guild.filesize_limit = limit
        if role is not None:
            guild.roles.append(FakeRole(role))
    if settings is None and role is not None:
        settings = {"role_id": role}
    if thread is None:
        thread = FakeThread()
    cog = _cog(
        tmp_path,
        queue,
        settings,
        guild=guild,
        thread=thread,
        monotonic=monotonic,
        media=media,
    )
    return cog, thread, exchange


async def _force(cog):
    async with cog._lock:
        return await cog._post_locked(force=True)


def _http_code(status: int, reason: str, message):
    return discord.HTTPException(SimpleNamespace(status=status, reason=reason), message)


def test_bracket_wrapper_is_only_for_attachment_posts():
    url = _preview(1)
    candidate = {"vx_url": url}
    assert build_content(candidate, 77, True) == f"<@&77>\n{HEADING}\n{url}"
    wrapped = build_content(candidate, 77, True, embed=False)
    assert wrapped == f"<@&77>\n{HEADING}\n<{url}>"
    spaced = {"vx_url": url + " x"}
    assert build_content(spaced, None, False, embed=False).splitlines()[-1] == url + " x"
    assert DISCORD_UA == "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"


@pytest.mark.parametrize("phrase", UNAVAILABLE)
def test_explicit_unavailable_pages_without_media_are_confirmed(phrase):
    body = f"<html><body>{phrase}</body></html>".encode()
    assert classify_html(body) is PreviewStatus.CONFIRMED_FAILURE
    with_media = body.replace(
        b"</body>",
        b'<meta property="og:image" content="https://pbs.twimg.com/media/x.jpg"></body>',
    )
    assert classify_html(with_media) is PreviewStatus.OK


def test_age_gate_unknown_error_and_empty_media_are_not_a_failure_class():
    assert classify_html(AGE_HTML) is PreviewStatus.OK
    assert classify_html(b"<html>Unknown Error</html>") is PreviewStatus.OK
    page = b'<html>Failed to scan your link!<meta property="og:image" content=""></html>'
    assert classify_html(page) is PreviewStatus.CONFIRMED_FAILURE
    tagged = b"<html>Failed to scan your link!<video></video></html>"
    assert classify_html(tagged) is PreviewStatus.OK


def test_image_and_mp4_signatures_reject_incompatible_bytes():
    assert sniff_media(JPEG) == "jpg"
    assert sniff_media(PNG) == "png"
    assert sniff_media(GIF) == "gif"
    assert sniff_media(WEBP) == "webp"
    assert sniff_media(MP4) == "mp4"
    assert mp4_compatible(MP4) is True
    assert sniff_media(b"<html>nope</html>") is None
    assert sniff_media(b"MZ") is None
    assert mp4_compatible(_mp4(b"hvc1")) is False
    assert mp4_compatible(_mp4(b"hev1")) is False
    assert mp4_compatible(_mp4(b"av01")) is False
    assert mp4_compatible(_box(b"ftyp", b"isom" + b"\x00\x00\x00\x00" + b"avc1") + _box(b"moov", b"avc1")) is False
    huge = bytearray(MP4)
    huge[0:4] = len(huge).to_bytes(4, "big")
    huge[0] = 0x7F
    assert mp4_compatible(bytes(huge)) is False


def test_content_length_is_strict_and_urls_stay_on_the_allowlist():
    assert content_length({}) is None
    assert content_length({"Content-Length": "12"}) == 12
    assert content_length({"content-length": 12}) == 12
    assert content_length({"Content-Length": "0"}) is None
    assert content_length({"Content-Length": "12.5"}) is None
    assert content_length({"Content-Length": " 12"}) is None
    assert content_length({"Content-Length": True}) is None
    pairs = _Pairs([("Content-Length", "10"), ("Content-Length", "10")])
    assert content_length(pairs) is None
    assert validate_public_https("https://PBS.twimg.com./a.jpg", "media") is None
    assert validate_public_https("https://vxtwitter.com/user/status/1", "preview") is None
    assert validate_public_https("https://api.vxtwitter.com/user/status/1", "preview") == "blocked host"
    assert validate_public_https("https://api.fxtwitter.com/x/status/1", "api") is None
    assert validate_public_https("https://video.twimg.com/a.mp4", "media") is None
    assert validate_public_https("http://pbs.twimg.com/a.jpg", "media") == "blocked url"
    assert validate_public_https("https://user:pass@pbs.twimg.com/a.jpg", "media") == "blocked credentials"
    assert validate_public_https("https://pbs.twimg.com:8443/a.jpg", "media") == "blocked port"
    assert validate_public_https("https://pbs.twimg.com:443/a.jpg", "media") is None
    assert validate_public_https("https://pbs.twimg.com.evil.example/a.jpg", "media") == "blocked host"
    assert validate_public_https("https://127.0.0.1/a.jpg", "media") == "blocked host"
    assert validate_public_https("https://pbs.twimg.com/a.jpg\nSet-Cookie: a", "media") == "blocked url"
    assert api_urls("User_Name", "99") == (
        "https://api.vxtwitter.com/User_Name/status/99",
        "https://api.fxtwitter.com/User_Name/status/99",
    )


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2001:4860:4860::8888", True),
        ("::ffff:8.8.8.8", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("172.16.0.1", False),
        ("169.254.1.1", False),
        ("100.64.0.1", False),
        ("224.0.0.1", False),
        ("0.0.0.0", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_only_global_unicast_addresses_are_public(address, public):
    assert ip_is_public(address) is public


def test_payload_helpers_ignore_thumbnails_empty_fx_all_and_cap_media():
    urls = [f"https://pbs.twimg.com/media/{index}.jpg" for index in range(5)]
    payload = {
        "media_extended": [{"type": "image", "url": url, "thumbnail_url": "https://evil.example/t.jpg"} for url in urls]
    }
    assert media_urls_from_payload(payload) == urls[:MAX_MEDIA]
    assert MAX_MEDIA == 4
    empty_all = {
        "tweet": {
            "media": {
                "all": [],
                "photos": [{"type": "photo", "url": JPEG_URL, "thumbnail_url": "https://evil.example/t.jpg"}],
            }
        }
    }
    assert media_urls_from_payload(empty_all) == [JPEG_URL]
    assert media_urls_from_payload({"media_extended": [{"type": "video", "thumbnail_url": JPEG_URL}]}) == []
    duplicated = {"mediaURLs": [JPEG_URL, JPEG_URL]}
    assert media_urls_from_payload(duplicated) == [JPEG_URL]


def test_stream_reader_stops_at_the_byte_cap_and_drops_the_partial(tmp_path):
    async def run():
        chunks = _Chunks([b"a" * 80, b"b" * 80])
        response = SimpleNamespace(content=chunks, status=200, headers={}, url=JPEG_URL, release=lambda: None)
        session = SimpleNamespace(close=_closed())
        opened_response = AiohttpOpened(session, response)
        target = tmp_path / "partial.bin"
        total, overflow = await opened_response.stream_to(target, 100)
        assert overflow is True
        assert total == 80
        assert not target.exists()
        small = AiohttpOpened(
            SimpleNamespace(close=_closed()),
            SimpleNamespace(content=_Chunks([b"abcdef"]), status=200, headers={}, url=JPEG_URL, release=lambda: None),
        )
        body, truncated = await small.read(3)
        assert truncated is True
        assert len(body) <= 3

    asyncio.run(run())


class _Chunks:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        del size
        for chunk in self.chunks:
            yield chunk


def _closed():
    async def close():
        return None

    return close


def test_resolution_and_exchange_reject_private_dns_before_connect():
    async def run():
        loop = asyncio.get_running_loop()

        async def check(address, connects):
            called = []

            def factory(timeout):
                del timeout
                called.append(address)
                raise RuntimeError("session")

            async def gai(*args, **kwargs):
                del args, kwargs
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

            loop.getaddrinfo = gai
            exchange = AiohttpExchange(session_factory=factory)
            if connects:
                with pytest.raises(RuntimeError, match="session"):
                    await exchange.open(JPEG_URL, {"User-Agent": API_UA}, 5)
                assert called == [address]
            else:
                with pytest.raises(UnsafeAddress):
                    await exchange.open(JPEG_URL, {"User-Agent": API_UA}, 5)
                assert called == []

        await check("8.8.8.8", True)
        await check("::ffff:8.8.8.8", True)
        for private in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "224.0.0.1", "::ffff:127.0.0.1"):
            await check(private, False)

        async def empty(*args, **kwargs):
            del args, kwargs
            return []

        loop.getaddrinfo = empty
        with pytest.raises(OSError):
            await assert_public_resolution("pbs.twimg.com")

        async def missing(*args, **kwargs):
            del args, kwargs
            raise socket.gaierror("nope")

        loop.getaddrinfo = missing
        with pytest.raises(OSError):
            await assert_public_resolution("pbs.twimg.com")

    asyncio.run(run())


def test_default_session_disables_env_and_redirects(monkeypatch):
    created = {}

    class Session:
        def __init__(self, *, timeout, connector, trust_env):
            del timeout
            created["trust_env"] = trust_env
            created["connector"] = connector

        async def get(self, url, *, headers, allow_redirects):
            created["url"] = url
            created["headers"] = headers
            created["allow_redirects"] = allow_redirects
            raise RuntimeError("stop")

        async def close(self):
            created["closed"] = True

    class Connector:
        def __init__(self, *, resolver, ssl, force_close):
            created["resolver"] = resolver
            created["ssl"] = ssl
            created["force_close"] = force_close

    monkeypatch.setattr(malfoyd_module_aiohttp(), "ClientSession", Session)
    monkeypatch.setattr(malfoyd_module_aiohttp(), "TCPConnector", Connector)

    async def run():
        loop = asyncio.get_running_loop()

        async def gai(*args, **kwargs):
            del args, kwargs
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        loop.getaddrinfo = gai
        exchange = AiohttpExchange()
        with pytest.raises(RuntimeError, match="stop"):
            await exchange.open(JPEG_URL, {"User-Agent": API_UA, "Accept": "*/*"}, MEDIA_TIMEOUT)

    asyncio.run(run())
    assert created["trust_env"] is False
    assert created["allow_redirects"] is False
    assert created["force_close"] is True
    assert created["ssl"] is True
    assert isinstance(created["resolver"], PublicResolver)
    assert created["closed"] is True
    assert created["headers"]["User-Agent"] == API_UA
    assert "Authorization" not in created["headers"]


def malfoyd_module_aiohttp():
    from cogs import malfoyd_media

    return malfoyd_media.aiohttp


def test_public_resolver_rejects_any_non_public_answer(monkeypatch):
    async def run(answers, fails):
        async def fake(self, hostname, port=0, family=socket.AF_INET):
            del self, hostname
            return [
                {"host": host, "port": port, "family": family, "proto": 0, "flags": 0}
                for host in answers
            ]

        monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", fake)
        resolver = PublicResolver()
        if fails:
            with pytest.raises(UnsafeAddress):
                await resolver.resolve("pbs.twimg.com", 443)
        else:
            infos = await resolver.resolve("pbs.twimg.com", 443)
            assert [item["host"] for item in infos] == list(answers)

    asyncio.run(run(["8.8.8.8", "1.1.1.1"], False))
    asyncio.run(run(["8.8.8.8", "10.0.0.1"], True))
    asyncio.run(run(["224.0.0.1"], True))
    asyncio.run(run([], True))


def test_normal_preview_uses_the_discord_crawler_and_skips_the_api(tmp_path):
    async def run():
        def route(url, headers, timeout):
            assert url == _preview(1)
            assert headers == {"User-Agent": DISCORD_UA, "Accept": "text/html"}
            assert timeout == PREVIEW_TIMEOUT
            assert "Authorization" not in headers
            return opened(OK_HTML)

        cog, thread, exchange = _build(tmp_path, _queue(candidates=[_candidate(1, campaign="keep")], source="cron"), plan=route)
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        content, kwargs = thread.sent[0]
        assert content == build_content(_candidate(1, campaign="keep"), 77, True)
        assert f"<{_preview(1)}>" not in content
        assert "files" not in kwargs
        assert "embed" not in kwargs and "embeds" not in kwargs
        assert thread.uploads == []
        _controls(kwargs["view"], 5)
        assert [item.id for item in kwargs["allowed_mentions"].roles] == [77]
        saved = _saved(cog)
        assert saved["posted"] is True
        assert saved["index"] == 0
        assert saved["rerolls_used"] == 0
        assert saved["source"] == "cron"
        assert saved["candidates"][0]["campaign"] == "keep"
        assert exchange.urls() == [_preview(1)]
        exchange.assert_released()
        assert not cog._lock.locked()

    asyncio.run(run())


def test_age_gate_and_media_tags_stay_on_the_link_path(tmp_path):
    async def run():
        pages = {1: AGE_HTML, 2: FAIL_HTML.replace(b"</head>", b'<meta property="og:video" content="https://video.twimg.com/x.mp4"></head>')}

        def route(url, headers, timeout):
            del headers, timeout
            for number, body in pages.items():
                if url == _preview(number):
                    return opened(body)
            raise AssertionError(url)

        for number in (1, 2):
            cog, thread, exchange = _build(
                tmp_path / str(number),
                _queue(candidates=[_candidate(number)], message_id=None),
                plan=route,
                role=None,
            )
            assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
            assert thread.sent[0][0] == build_content(_candidate(number), None, False)
            assert thread.uploads == []
            assert exchange.urls() == [_preview(number)]

    asyncio.run(run())


def test_confirmed_failure_uploads_images_and_mp4_in_order(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)

    async def run():
        def route(url, headers, timeout):
            assert set(headers) == {"User-Agent", "Accept"}
            assert "Authorization" not in headers and "Cookie" not in headers
            if url == _preview(1):
                assert headers["User-Agent"] == DISCORD_UA
                assert timeout == PREVIEW_TIMEOUT
                return opened(FAIL_HTML)
            if url == _vx(1):
                assert headers["User-Agent"] == API_UA
                assert timeout == API_TIMEOUT
                return opened(_vx_body([("image", JPEG_URL), ("video", MP4_URL)]))
            if url == JPEG_URL:
                assert headers["User-Agent"] == API_UA
                assert timeout == MEDIA_TIMEOUT
                return opened(JPEG)
            if url == MP4_URL:
                return opened(MP4)
            raise AssertionError(url)

        exchange = Exchange(route)
        holder = {}

        class Wrap:
            def __init__(self):
                self.inner = MediaClient(exchange)

            async def inspect(self, url, budget):
                assert holder["cog"]._lock.locked()
                return await self.inner.inspect(url, budget)

            async def download(self, candidate, limit, directory, budget):
                assert holder["cog"]._lock.locked()
                return await self.inner.download(candidate, limit, directory, budget)

        cog, thread, _exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1, campaign="keep")], source="cron", message_id=None),
            media=Wrap(),
        )
        holder["cog"] = cog
        original = thread.send

        async def send(*args, **kwargs):
            assert cog._lock.locked()
            return await original(*args, **kwargs)

        thread.send = send
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        content, kwargs = thread.sent[0]
        url = _preview(1)
        assert content == build_content(_candidate(1, campaign="keep"), 77, True, embed=False)
        assert content.splitlines()[0] == "<@&77>"
        assert HEADING in content.splitlines()
        assert content.splitlines()[-1] == f"<{url}>"
        _controls(kwargs["view"], 5)
        assert [item.id for item in kwargs["allowed_mentions"].roles] == [77]
        assert "embed" not in kwargs and "embeds" not in kwargs
        assert thread.uploads == [[("malfoyd-1.jpg", JPEG), ("malfoyd-2.mp4", MP4)]]
        saved = _saved(cog)
        assert saved["posted"] is True
        assert saved["message_id"] == 5001
        assert saved["index"] == 0
        assert saved["rerolls_used"] == 0
        assert saved["candidates"][0]["campaign"] == "keep"
        assert _fx(1) not in exchange.urls()
        assert all("evil.example" not in item for item in exchange.urls())
        exchange.assert_released()
        assert not cog._lock.locked()

    asyncio.run(run())
    _assert_gone(created)


def test_comparable_phrase_uploads_png_and_ignores_the_url_suffix(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)
    body = "<html><body>This post is unavailable :(</body></html>".encode()
    weird = "https://pbs.twimg.com/media/pic.gif"

    async def run():
        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(4):
                return opened(body)
            if url == _vx(4):
                return opened(_vx_body([("photo", weird)]))
            if url == weird:
                return opened(PNG)
            raise AssertionError(url)

        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(4)], message_id=None),
            plan=route,
            role=None,
        )
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert thread.sent[0][0] == build_content(_candidate(4), None, False, embed=False)
        assert thread.uploads == [[("malfoyd-1.png", PNG)]]
        exchange.assert_released()

    asyncio.run(run())
    _assert_gone(created)


def test_vx_miss_uses_fx_photos_when_all_is_empty(tmp_path):
    async def run():
        payload = {
            "tweet": {
                "media": {
                    "all": [],
                    "photos": [{"type": "photo", "url": JPEG_URL, "thumbnail_url": "https://evil.example/t.jpg"}],
                }
            }
        }

        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(6):
                return opened(b"<html>Tweet not found.</html>")
            if url == _vx(6):
                return opened(b"{}")
            if url == _fx(6):
                return opened(json.dumps(payload).encode())
            if url == JPEG_URL:
                return opened(JPEG)
            raise AssertionError(url)

        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(6)], message_id=None),
            plan=route,
            role=None,
        )
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert thread.uploads == [[("malfoyd-1.jpg", JPEG)]]
        assert exchange.urls() == [_preview(6), _vx(6), _fx(6), JPEG_URL]

    asyncio.run(run())


def test_oversize_stream_cap_and_unknown_length_do_not_upload(tmp_path):
    async def run():
        huge = opened(JPEG, length=50_000_000)
        capped = opened(JPEG + b"EXTRA", length=len(JPEG))
        unknown = opened(JPEG, length="omit")
        duplicated = Opened(JPEG, _Pairs([("Content-Length", "8"), ("Content-Length", "8")]), 200)

        async def scenario(number, media, limit, reason, streams):
            def route(url, headers, timeout):
                del headers, timeout
                if url == _preview(number):
                    return opened(FAIL_HTML)
                if url == _vx(number):
                    return opened(_vx_body([("image", JPEG_URL)]))
                if url == _fx(number):
                    return opened(b"", status=404)
                if url == JPEG_URL:
                    return media
                if url == _preview(number + 1):
                    return opened(OK_HTML)
                raise AssertionError(url)

            cog, thread, exchange = _build(
                tmp_path / str(number),
                _queue(candidates=[_candidate(number), _candidate(number + 1)], message_id=None),
                plan=route,
                limit=limit,
                role=None,
            )
            before = cog.queue_path.read_bytes()
            assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
            assert thread.uploads == []
            assert thread.sent[0][0] == build_content(_candidate(number + 1), None, False)
            assert _saved(cog)["index"] == 1
            assert _saved(cog)["rerolls_used"] == 0
            assert _saved(cog)["candidates"][0] == _candidate(number)
            assert media.streams == streams
            assert media.reads == 0
            if streams:
                assert media.stream_caps == [len(JPEG)]
            text = cog.queue_path.read_bytes()
            assert text != before
            assert reason in ("oversize", "stream cap", "size unknown")
            exchange.assert_released()

        await scenario(10, huge, 1024, "oversize", 0)
        await scenario(20, capped, len(JPEG) + 100, "stream cap", 1)
        await scenario(30, unknown, 1024, "size unknown", 0)
        await scenario(40, duplicated, 1024, "size unknown", 0)

        exact = opened(JPEG)
        cog, thread, exchange = _build(
            tmp_path / "exact",
            _queue(candidates=[_candidate(50)], message_id=None),
            plan=lambda url, headers, timeout: (
                opened(FAIL_HTML)
                if url == _preview(50)
                else opened(_vx_body([("image", JPEG_URL)]))
                if url == _vx(50)
                else exact
                if url == JPEG_URL
                else (_ for _ in ()).throw(AssertionError(url))
            ),
            limit=len(JPEG),
            role=None,
        )
        client = MediaClient(exchange)
        with tempfile.TemporaryDirectory() as directory:
            result = await client.download({"vx_url": _preview(50)}, len(JPEG), directory, _budget())
        assert result.reason == ""
        assert exact.streams == 1
        assert exact.stream_caps == [len(JPEG)]

    asyncio.run(run())


def test_destination_guild_limit_beats_a_larger_bot_guild(tmp_path):
    async def run():
        media = opened(JPEG, length=200)

        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(1):
                return opened(FAIL_HTML)
            if url in {_vx(1), _fx(1)}:
                return opened(_vx_body([("image", JPEG_URL)])) if url == _vx(1) else opened(b"", status=404)
            if url == JPEG_URL:
                return media
            if url == _preview(2):
                return opened(OK_HTML)
            raise AssertionError(url)

        thread = FakeThread()
        thread.guild = SimpleNamespace(filesize_limit=64)
        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1), _candidate(2, campaign="next")], message_id=None),
            plan=route,
            thread=thread,
            limit=5_000_000,
        )
        assert cog.bot.guild.filesize_limit == 5_000_000
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        content, kwargs = thread.sent[0]
        assert content == build_content(_candidate(2, campaign="next"), 77, True)
        assert [item.id for item in kwargs["allowed_mentions"].roles] == [77]
        assert media.streams == 0 and media.reads == 0
        assert _saved(cog)["index"] == 1
        assert _saved(cog)["rerolls_used"] == 0
        exchange.assert_released()

    asyncio.run(run())


def test_unknown_upload_limit_skips_without_fetching_media(tmp_path, caplog):
    async def run():
        class Exploding:
            def __init__(self):
                self.downloads = 0
                self.seen = []

            async def inspect(self, url, budget):
                del budget
                self.seen.append(url)
                if url == _preview(1):
                    return Inspection(PreviewStatus.CONFIRMED_FAILURE, "html")
                return Inspection(PreviewStatus.OK, "html")

            async def download(self, candidate, limit, directory, budget):
                del candidate, limit, directory, budget
                self.downloads += 1
                raise AssertionError("unknown limit must not fetch media")

        class Boom(FakeGuild):
            @property
            def filesize_limit(self):
                raise RuntimeError("unavailable")

        guilds = [FakeGuild(), Boom(), FakeGuild(), FakeGuild()]
        guilds[2].filesize_limit = 0
        guilds[3].filesize_limit = True
        for index, guild in enumerate(guilds):
            media = Exploding()
            cog, thread, _exchange = _build(
                tmp_path / str(index),
                _queue(candidates=[_candidate(1), _candidate(2)], message_id=None),
                media=media,
                guild=guild,
                limit=None,
                role=None,
            )
            before = cog.queue_path.read_bytes()
            with caplog.at_level(logging.WARNING, logger="cogs.malfoyd"):
                assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
            assert media.downloads == 0
            assert media.seen == [_preview(1), _preview(2)]
            assert thread.uploads == []
            assert _saved(cog)["index"] == 1
            assert _saved(cog)["rerolls_used"] == 0
            assert cog.queue_path.read_bytes() != before
        lone = Exploding()
        cog, thread, _exchange = _build(
            tmp_path / "lone",
            _queue(candidates=[_candidate(1)], message_id=None),
            media=lone,
            guild=FakeGuild(),
            limit=None,
            role=None,
        )
        before = cog.queue_path.read_bytes()
        assert await cog.poll_once() is None
        assert await _force(cog) == "No usable picks left in the queue."
        assert lone.downloads == 0
        assert thread.sent == []
        assert cog.queue_path.read_bytes() == before
        assert "upload limit is unknown" in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 500, 404])
def test_preview_http_errors_stay_unknown_and_do_not_skip(status, tmp_path):
    async def run():
        def route(url, headers, timeout):
            del timeout
            assert headers["User-Agent"] == DISCORD_UA
            if url != _preview(1):
                raise AssertionError(url)
            return opened(b"hidden", status=status)

        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=None),
            plan=route,
            role=None,
        )
        before = cog.queue_path.read_bytes()
        assert await cog.poll_once() is None
        assert await _force(cog) == f"Couldn't check that preview (HTTP {status}). Nothing was changed."
        assert thread.sent == []
        assert cog.queue_path.read_bytes() == before
        assert exchange.urls() == [_preview(1)] * (PREVIEW_ATTEMPTS * 2)
        assert all(item.reads == 0 for item in exchange.opened)
        exchange.assert_released()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("kind", "detail"),
    [("timeout", "timed out"), ("os", "connection error"), ("client", "connection error")],
)
def test_preview_timeouts_retry_then_report_unknown(kind, detail, tmp_path):
    async def run():
        def failure():
            if kind == "timeout":
                return TimeoutError("slow")
            if kind == "os":
                return OSError("reset")
            return aiohttp.ClientError("boom")

        def route(url, headers, timeout):
            del headers, timeout
            if url != _preview(1):
                raise AssertionError(url)
            return failure()

        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=None),
            plan=route,
            role=None,
        )
        before = cog.queue_path.read_bytes()
        assert await _force(cog) == f"Couldn't check that preview ({detail}). Nothing was changed."
        assert thread.sent == []
        assert cog.queue_path.read_bytes() == before
        assert exchange.urls() == [_preview(1), _preview(1)]

    asyncio.run(run())


def test_preview_retry_then_success_posts_the_bare_link(tmp_path):
    async def run():
        state = {"n": 0}

        def route(url, headers, timeout):
            del headers, timeout
            if url != _preview(1):
                raise AssertionError(url)
            state["n"] += 1
            if state["n"] == 1:
                return TimeoutError("slow")
            return opened(OK_HTML)

        cog, thread, exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1)], message_id=None),
            plan=route,
            role=None,
        )
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert thread.sent[0][0] == build_content(_candidate(1), None, False)
        assert thread.uploads == []
        assert exchange.urls() == [_preview(1), _preview(1)]

    asyncio.run(run())


def test_redirects_are_checked_and_private_targets_are_not_fetched():
    async def run():
        cases = [
            (redirect("https://user:pass@vxtwitter.com/user/status/1"), "blocked credentials"),
            (redirect("http://169.254.169.254/latest/meta-data"), "blocked url"),
            (redirect("https://evil.example/a"), "blocked host"),
            (redirect("https://api.vxtwitter.com/user/status/1"), "blocked host"),
            (UnsafeAddress("nope"), "blocked address"),
        ]
        for item, detail in cases:
            def route(url, headers, timeout, item=item):
                del headers, timeout
                if url != _preview(1):
                    raise AssertionError(url)
                return item

            client = MediaClient(Exchange(route))
            result = await client.inspect(_preview(1), _budget())
            assert result.status is PreviewStatus.UNKNOWN
            assert result.detail == detail

        first = "https://vxtwitter.com/user/status/1"
        second = "https://www.vxtwitter.com/user/status/1"
        third = "https://vxtwitter.com/user/status/2"
        fourth = "https://www.vxtwitter.com/user/status/2"
        chain = {first: redirect(second), second: redirect(third), third: redirect(fourth)}
        exchange = Exchange(chain)
        result = await MediaClient(exchange).inspect(first, _budget())
        assert result.detail == "too many redirects"
        assert exchange.urls() == [first, second, third]
        exchange.assert_released()

        loop_plan = {first: redirect(second), second: redirect(first)}
        loop_exchange = Exchange(loop_plan)
        looped = await MediaClient(loop_exchange).inspect(first, _budget())
        assert looped.detail == "redirect loop"
        assert loop_exchange.urls() == [first, second]

        old = "https://pbs.twimg.com/media/old.jpg"
        new = "https://pbs.twimg.com/media/new.jpg"

        def download_route(url, headers, timeout):
            del headers, timeout
            if url == _vx(8):
                return opened(_vx_body([("image", old)]))
            if url == old:
                return redirect("  /media/new.jpg")
            if url == new:
                return opened(JPEG)
            raise AssertionError(url)

        exchange = Exchange(download_route)
        with tempfile.TemporaryDirectory() as directory:
            downloaded = await MediaClient(exchange).download(
                {"vx_url": _preview(8)},
                len(JPEG) + 10,
                directory,
                _budget(),
            )
            assert downloaded.reason == ""
            assert Path(downloaded.files[0].path).read_bytes() == JPEG
        assert exchange.urls() == [_vx(8), old, new]
        hostile = Exchange(
            lambda url, headers, timeout: (
                opened(_vx_body([("image", old)]))
                if url == _vx(8)
                else redirect("//evil.example/a.jpg")
                if url == old
                else (_ for _ in ()).throw(AssertionError(url))
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            blocked = await MediaClient(hostile).download({"vx_url": _preview(8)}, 1000, directory, _budget())
        assert blocked.reason == "blocked host"
        assert all("evil.example" not in url for url in hostile.urls())

    asyncio.run(run())


def test_bad_signature_and_failed_download_advance_without_uploading(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)

    async def run():
        bad = opened(b"not-an-image-or-mp4")
        hevc = opened(_mp4(b"hvc1"))

        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(1):
                return opened(FAIL_HTML)
            if url == _vx(1):
                return opened(_vx_body([("image", JPEG_URL), ("video", MP4_URL)]))
            if url == JPEG_URL:
                return bad
            if url == MP4_URL:
                return hevc
            if url == _preview(2):
                return opened(FAIL_HTML)
            if url == _vx(2):
                return opened(_vx_body([("image", PNG_URL)]))
            if url == PNG_URL:
                return opened(b"", status=404)
            if url == _fx(2):
                return opened(b"", status=404)
            if url == _preview(3):
                return opened(OK_HTML)
            raise AssertionError(url)

        cog, thread, exchange = _build(
            tmp_path,
            _queue(
                candidates=[_candidate(1), _candidate(2), _candidate(3)],
                message_id=None,
                source="cron",
            ),
            plan=route,
            role=None,
        )
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert thread.uploads == []
        assert thread.sent[0][0] == build_content(_candidate(3), None, False)
        assert _saved(cog)["index"] == 2
        assert _saved(cog)["rerolls_used"] == 0
        assert _saved(cog)["candidates"][0]["vx_url"] == _preview(1)
        assert MP4_URL not in exchange.urls()
        exchange.assert_released()

    asyncio.run(run())
    _assert_gone(created)


def test_discord_413_and_40005_advance_but_other_http_errors_stop(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)

    async def upload(number, exc, advances):
        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(number):
                return opened(FAIL_HTML)
            if url == _vx(number):
                return opened(_vx_body([("image", JPEG_URL)]))
            if url == JPEG_URL:
                return opened(JPEG)
            if url == _preview(number + 1):
                return opened(OK_HTML)
            raise AssertionError(url)

        cog, thread, exchange = _build(
            tmp_path / str(number),
            _queue(candidates=[_candidate(number), _candidate(number + 1)], message_id=None),
            plan=route,
            role=None,
        )
        thread.fail_send = [exc] if advances else exc
        before = cog.queue_path.read_bytes()
        if advances:
            assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
            assert thread.uploads == [[("malfoyd-1.jpg", JPEG)]]
            assert thread.sent[0][0] == build_content(_candidate(number + 1), None, False)
            assert _saved(cog)["index"] == 1
            assert _saved(cog)["rerolls_used"] == 0
        else:
            assert await cog.poll_once() is None
            assert await _force(cog) == "Couldn't post Malfoyd of the day."
            assert thread.sent == []
            assert _preview(number + 1) not in exchange.urls()
            assert cog.queue_path.read_bytes() == before
        exchange.assert_released()

    async def run():
        await upload(1, _http(413, "Payload Too Large", "nope"), True)
        await upload(
            3,
            _http_code(400, "Bad Request", {"code": 40005, "message": "nope"}),
            True,
        )
        await upload(5, _http(500, "Internal Server Error", "nope"), False)
        await upload(7, _http(403, "Forbidden", "nope"), False)

    asyncio.run(run())
    _assert_gone(created)


def test_exhaustion_keeps_the_queue_and_attempt_cap_does_not_wrap(tmp_path, caplog):
    async def run():
        def failing(url, headers, timeout):
            del headers, timeout
            if url.startswith("https://vxtwitter.com") or url.startswith("https://www.vxtwitter.com"):
                return opened(FAIL_HTML)
            if url.startswith("https://api."):
                return opened(b"", status=404)
            raise AssertionError(url)

        count = MAX_CANDIDATE_ATTEMPTS
        exact = [_candidate(number) for number in range(1, count + 1)]
        cog, thread, exchange = _build(
            tmp_path / "exact",
            _queue(candidates=exact, message_id=None, source="cron"),
            plan=failing,
            role=None,
        )
        before = cog.queue_path.read_bytes()
        with caplog.at_level(logging.WARNING, logger="cogs.malfoyd"):
            assert await cog.poll_once() is None
            assert await _force(cog) == "No usable picks left in the queue."
        assert thread.sent == []
        assert cog.queue_path.read_bytes() == before
        assert "Malfoyd found no usable pick" in caplog.text
        previews = [url for url in exchange.urls() if "/api." not in url]
        assert previews == [_preview(number) for number in range(1, count + 1)] * 2

        extra = [_candidate(number) for number in range(1, count + 2)]
        stopped, stopped_thread, stopped_exchange = _build(
            tmp_path / "cap",
            _queue(candidates=extra, message_id=None),
            plan=failing,
            role=None,
        )
        snapshot = stopped.queue_path.read_bytes()
        assert await _force(stopped) == "Couldn't find a usable pick to post. Nothing was changed."
        assert stopped_thread.sent == []
        assert stopped.queue_path.read_bytes() == snapshot
        assert _preview(count + 1) not in stopped_exchange.urls()
        assert "Malfoyd stopped without a usable pick" in caplog.text

        reroll_candidates = [_candidate(number) for number in range(1, count + 3)]
        reroll, _thread, reroll_exchange = _build(
            tmp_path / "reroll",
            _queue(candidates=reroll_candidates, message_id=5001),
            plan=failing,
            role=None,
        )
        kept = reroll.queue_path.read_bytes()
        interaction = _interaction(reroll, user=FakeMember(4, reroll.bot.guild), message_id=5001)
        await reroll.handle_reroll(interaction)
        assert interaction.response.sent[0][0] == "Couldn't find a usable pick to post. Nothing was changed."
        assert interaction.response.edits == []
        assert reroll.queue_path.read_bytes() == kept
        assert _preview(1) not in reroll_exchange.urls()
        assert _preview(count + 2) not in reroll_exchange.urls()
        assert _saved(reroll)["index"] == 0
        assert _saved(reroll)["rerolls_used"] == 0

    asyncio.run(run())


def test_auto_skips_do_not_spend_a_reroll_and_the_owner_is_exempt(tmp_path):
    async def run():
        original = [
            _candidate(1),
            _candidate(2, campaign="skip-me"),
            {"vx_url": "https://x.com/no", "id": "bad"},
            _candidate(3, campaign="skip-too"),
            _candidate(4, campaign="post-me"),
        ]

        def route(url, headers, timeout):
            del headers, timeout
            if url in {_preview(2), _preview(3)}:
                return opened(FAIL_HTML)
            if url in {_vx(2), _fx(2), _vx(3), _fx(3)}:
                return opened(b"", status=404)
            if url == _preview(4):
                return opened(OK_HTML)
            raise AssertionError(url)

        cog, _thread, exchange = _build(tmp_path, _queue(candidates=original, message_id=5001), plan=route)
        interaction = _interaction(cog, user=FakeMember(4, cog.bot.guild), message_id=5001)
        await _button(make_view(5), "malfoyd:reroll").callback(interaction)
        edit = interaction.response.edits[0]
        assert edit["content"] == build_content(_candidate(4, campaign="post-me"), None, False)
        assert "<@&" not in edit["content"]
        assert edit["allowed_mentions"].roles is False
        assert "attachments" not in edit
        _controls(edit["view"], 4)
        saved = _saved(cog)
        assert saved["rerolls_used"] == 1
        assert saved["index"] == 3
        assert saved["message_id"] == 5001
        assert saved["candidates"] == original
        assert _preview(1) not in exchange.urls()
        assert "https://x.com/no" not in exchange.urls()

        owner_media_skipped = [
            _candidate(1),
            _candidate(2),
            _candidate(3),
        ]

        def owner_route(url, headers, timeout):
            del headers, timeout
            if url == _preview(2):
                return opened(FAIL_HTML)
            if url in {_vx(2), _fx(2)}:
                return opened(b"", status=404)
            if url == _preview(3):
                return opened(OK_HTML)
            raise AssertionError(url)

        owner, _owner_thread, _owner_exchange = _build(
            tmp_path / "owner",
            _queue(candidates=owner_media_skipped, message_id=5001, rerolls_used=0),
            plan=owner_route,
        )
        owner.bot.is_owner = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=True)
        hit = _interaction(owner, user=FakeMember(9, owner.bot.guild), message_id=5001)
        await owner.handle_reroll(hit)
        _controls(hit.response.edits[0]["view"], 5)
        assert _saved(owner)["rerolls_used"] == 0
        assert _saved(owner)["index"] == 2

        capped, _capped_thread, capped_exchange = _build(
            tmp_path / "capped",
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=5001, rerolls_used=5),
            plan=lambda url, headers, timeout: opened(OK_HTML),
        )
        blocked = _interaction(capped, user=FakeMember(3, capped.bot.guild), message_id=5001)
        await capped.handle_reroll(blocked)
        assert blocked.response.sent[0][0] == "No rerolls left today"
        assert capped_exchange.urls() == []
        assert _saved(capped)["rerolls_used"] == 5

        exempt, _exempt_thread, _exempt_exchange = _build(
            tmp_path / "exempt",
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=5001, rerolls_used=5),
            plan=lambda url, headers, timeout: (_ for _ in ()).throw(AssertionError(url))
            if url != _preview(2)
            else opened(OK_HTML),
        )
        exempt.bot.is_owner = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=True)
        allowed = _interaction(exempt, user=FakeMember(9, exempt.bot.guild), message_id=5001)
        await exempt.handle_reroll(allowed)
        assert _preview(2) in allowed.response.edits[0]["content"]
        assert _saved(exempt)["rerolls_used"] == 5
        assert _saved(exempt)["index"] == 1

    asyncio.run(run())


def test_daily_skip_keeps_rerolls_and_the_dedupe_slice(tmp_path):
    async def run():
        original = [
            _candidate(1, campaign="unusable"),
            {"vx_url": "nope"},
            _candidate(2, campaign="posted"),
            _candidate(3, campaign="later"),
        ]

        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(1):
                return opened(FAIL_HTML)
            if url.startswith("https://api.") and url.endswith("/1"):
                return opened(b"", status=404)
            if url == _preview(2):
                return opened(OK_HTML)
            raise AssertionError(url)

        cog, thread, _exchange = _build(tmp_path, _queue(candidates=original, message_id=None), plan=route)
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        _controls(thread.sent[0][1]["view"], 5)
        saved = _saved(cog)
        assert saved["index"] == 1
        assert saved["rerolls_used"] == 0
        assert saved["candidates"] == original
        chosen = valid_candidates(saved["candidates"])
        assert [item["vx_url"] for item in chosen[: saved["index"] + 1]] == [_preview(1), _preview(2)]
        assert [item["campaign"] for item in chosen[saved["index"] + 1 :]] == ["later"]
        assert _preview(3) not in _exchange_urls(cog)

    asyncio.run(run())


def _exchange_urls(cog) -> list[str]:
    return cog._media._exchange.urls()


def test_reroll_replaces_with_attachments_after_the_edit(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)

    async def run():
        def images(number):
            def route(url, headers, timeout):
                del headers, timeout
                if url == _preview(number):
                    return opened(FAIL_HTML)
                if url == _vx(number):
                    return opened(_vx_body([("image", JPEG_URL), ("video", MP4_URL)]))
                if url == JPEG_URL:
                    return opened(JPEG)
                if url == MP4_URL:
                    return opened(MP4)
                raise AssertionError(url)

            return route

        reroll_events = []
        reroll, _reroll_thread, reroll_exchange = _build(
            tmp_path / "reroll",
            _queue(message_id=5001, candidates=[_candidate(1), _candidate(8)]),
            plan=images(8),
        )
        hit = _interaction(reroll, user=FakeMember(4, reroll.bot.guild), message_id=5001)
        original_edit = hit.response.edit_message

        async def edit_message(**kwargs):
            reroll_events.append("edit")
            assert cog_not_saved(reroll)
            return await original_edit(**kwargs)

        hit.response.edit_message = edit_message
        real_reroll_save = reroll._save_queue

        def reroll_save(queue):
            reroll_events.append("save")
            assert hit.response.edits
            return real_reroll_save(queue)

        reroll._save_queue = reroll_save
        await _button(make_view(5), "malfoyd:reroll").callback(hit)
        edit = hit.response.edits[0]
        assert edit["content"] == build_content(_candidate(8), None, False, embed=False)
        assert "<@&" not in edit["content"]
        assert edit["allowed_mentions"].roles is False
        assert hit.response.uploads == [[("malfoyd-1.jpg", JPEG), ("malfoyd-2.mp4", MP4)]]
        _controls(edit["view"], 4)
        assert reroll_events == ["edit", "save"]
        assert _saved(reroll)["index"] == 1
        assert _saved(reroll)["rerolls_used"] == 1
        assert _saved(reroll)["message_id"] == 5001
        reroll_exchange.assert_released()

    asyncio.run(run())
    _assert_gone(created)


def cog_not_saved(cog) -> bool:
    return _saved(cog)["rerolls_used"] == 0


def test_deadline_does_not_commit_skips(tmp_path, caplog):
    async def run():
        now = {"t": 0.0}
        seen = []

        class Media:
            async def inspect(self, url, budget):
                del budget
                seen.append(url)
                now["t"] += OPERATION_SECONDS
                return Inspection(PreviewStatus.CONFIRMED_FAILURE, "html")

            async def download(self, candidate, limit, directory, budget):
                del candidate, limit, directory, budget
                return DownloadResult((), "timed out")

        cog, thread, _exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=None),
            media=Media(),
            monotonic=lambda: now["t"],
            role=None,
        )
        before = cog.queue_path.read_bytes()
        with caplog.at_level(logging.WARNING, logger="cogs.malfoyd"):
            assert await cog.poll_once() is None
        assert seen == [_preview(1)]
        assert await _force(cog) == "Couldn't check that preview (timed out). Nothing was changed."
        assert seen == [_preview(1), _preview(1)]
        assert thread.sent == []
        assert cog.queue_path.read_bytes() == before
        assert "Malfoyd preview unknown: timed out" in caplog.text

    asyncio.run(run())


def test_request_and_json_bounds(tmp_path):
    async def run():
        client = MediaClient(Exchange(lambda url, headers, timeout: opened(_vx_body([("image", JPEG_URL)]))))
        limited = await client.download({"vx_url": _preview(1)}, 1000, tmp_path, _budget(requests=1))
        assert limited.reason == "request limit"

        now = {"t": 0.0}
        clock = MediaClient(Exchange(lambda url, headers, timeout: opened(OK_HTML)))
        budget = Budget(lambda: now["t"], requests=4, seconds=5)
        now["t"] = 5
        inspection = await clock.inspect(_preview(1), budget)
        assert inspection.status is PreviewStatus.UNKNOWN
        assert inspection.detail == "timed out"

        huge = b"x" * (HTML_CAP + 1)
        exchange = Exchange({_preview(1): opened(huge)})
        truncated = await MediaClient(exchange).inspect(_preview(1), _budget())
        assert truncated == Inspection(PreviewStatus.UNKNOWN, "preview truncated")
        assert exchange.opened[0].read_caps == [HTML_CAP]

        fat = b'{"a":"' + (b"b" * JSON_CAP)
        media = opened(JPEG)

        def route(url, headers, timeout):
            del headers, timeout
            if url == _vx(3):
                return opened(fat)
            if url == _fx(3):
                return opened(_vx_body([("image", JPEG_URL)]))
            if url == JPEG_URL:
                return media
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory:
            downloaded = await MediaClient(Exchange(route)).download(
                {"vx_url": _preview(3)},
                len(JPEG) + 5,
                directory,
                _budget(),
            )
            assert downloaded.reason == ""
            assert Path(downloaded.files[0].path).read_bytes() == JPEG

        urls = [f"https://pbs.twimg.com/media/{index}.jpg" for index in range(5)]

        def many(url, headers, timeout):
            del headers, timeout
            if url == _vx(4):
                return opened(_vx_body([("image", item) for item in urls]))
            if url in urls[:4]:
                return opened(JPEG)
            raise AssertionError(url)

        many_exchange = Exchange(many)
        with tempfile.TemporaryDirectory() as directory:
            four = await MediaClient(many_exchange).download({"vx_url": _preview(4)}, 1000, directory, _budget())
        assert [item.filename for item in four.files] == [f"malfoyd-{index}.jpg" for index in range(1, 5)]
        assert urls[4] not in many_exchange.urls()

    asyncio.run(run())


def test_cleanup_on_success_error_and_cancellation(tmp_path, monkeypatch):
    created = _track_temp(monkeypatch)

    async def run():
        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(1):
                return opened(FAIL_HTML)
            if url == _vx(1):
                return opened(_vx_body([("image", JPEG_URL)]))
            if url == JPEG_URL:
                return opened(JPEG)
            raise AssertionError(url)

        failed, failed_thread, _failed_exchange = _build(
            tmp_path / "error",
            _queue(candidates=[_candidate(1), _candidate(2)], message_id=None),
            plan=route,
            role=None,
        )
        failed_thread.fail_send = _http(500, "Internal Server Error", "nope")
        before = failed.queue_path.read_bytes()
        assert await failed.poll_once() is None
        assert failed_thread.sent == []
        assert failed.queue_path.read_bytes() == before

        cancelled, cancelled_thread, _cancelled_exchange = _build(
            tmp_path / "cancel",
            _queue(candidates=[_candidate(1)], message_id=None),
            plan=route,
            role=None,
        )
        cancelled_thread.fail_send = asyncio.CancelledError()
        snapshot = cancelled.queue_path.read_bytes()
        with pytest.raises(asyncio.CancelledError):
            await cancelled.poll_once()
        assert cancelled.queue_path.read_bytes() == snapshot
        assert not cancelled._lock.locked()

        class CancelMedia:
            async def inspect(self, url, budget):
                del url, budget
                return Inspection(PreviewStatus.CONFIRMED_FAILURE, "html")

            async def download(self, candidate, limit, directory, budget):
                del candidate, limit, budget
                Path(directory, "junk.bin").write_bytes(b"junk")
                raise asyncio.CancelledError()

        download_cancel, _thread, _exchange = _build(
            tmp_path / "download-cancel",
            _queue(candidates=[_candidate(1)], message_id=None),
            media=CancelMedia(),
            role=None,
        )
        kept = download_cancel.queue_path.read_bytes()
        with pytest.raises(asyncio.CancelledError):
            await download_cancel.poll_once()
        assert download_cancel.queue_path.read_bytes() == kept
        assert not download_cancel._lock.locked()

    asyncio.run(run())
    _assert_gone(created)


def test_persist_failure_deletes_the_new_post_and_does_not_ping_twice(tmp_path, caplog):
    async def run():
        state = {"n": 0}

        def route(url, headers, timeout):
            del headers, timeout
            if url != _preview(1):
                raise AssertionError(url)
            return opened(OK_HTML)

        cog, thread, _exchange = _build(
            tmp_path,
            _queue(candidates=[_candidate(1)], message_id=None),
            plan=route,
        )
        real = cog._save_queue

        def save(queue):
            state["n"] += 1
            if state["n"] == 1:
                return False
            return real(queue)

        cog._save_queue = save
        before = cog.queue_path.read_bytes()
        assert await cog.poll_once() is None
        assert thread.sent[0][0].startswith("<@&77>")
        assert thread.deleted_ids == [5001]
        assert cog.queue_path.read_bytes() == before
        assert cog._unpersisted_mention_date == TODAY
        assert await cog.poll_once() == "Posted Malfoyd of the day (5002)."
        assert "<@&" not in thread.sent[1][0]
        assert thread.sent[1][1]["allowed_mentions"].roles is False
        assert _saved(cog)["posted"] is True
        assert _saved(cog)["message_id"] == 5002

        lost, lost_thread, _lost_exchange = _build(
            tmp_path / "lost",
            _queue(candidates=[_candidate(1)], message_id=None),
            plan=route,
        )
        lost._save_queue = lambda queue: False

        async def fetch(message_id):
            lost_thread.fetched_ids.append(message_id)
            raise _http(403, "Forbidden")

        lost_thread.fetch_message = fetch
        with caplog.at_level(logging.ERROR, logger="cogs.malfoyd"):
            assert await lost.poll_once() is None
        assert lost_thread.deleted_ids == []
        assert len(lost_thread.sent) == 1
        assert _saved(lost)["posted"] is False
        assert "could not remove the unrecorded post 5001" in caplog.text

    asyncio.run(run())


def test_reroll_save_failure_restores_the_previous_text_and_does_not_charge(tmp_path):
    async def run():
        def route(url, headers, timeout):
            del headers, timeout
            if url == _preview(2):
                return opened(FAIL_HTML)
            if url == _vx(2):
                return opened(_vx_body([("image", JPEG_URL)]))
            if url == JPEG_URL:
                return opened(JPEG)
            raise AssertionError(url)

        cog, _thread, _exchange = _build(
            tmp_path,
            _queue(message_id=5001, candidates=[_candidate(1, campaign="old"), _candidate(2)]),
            plan=route,
            role=None,
        )
        before = cog.queue_path.read_bytes()
        interaction = _interaction(cog, user=FakeMember(4, cog.bot.guild), message_id=5001)
        restored = []

        async def edit(**kwargs):
            restored.append(kwargs)

        interaction.message.edit = edit
        cog._save_queue = lambda queue: False
        await cog.handle_reroll(interaction)
        assert interaction.response.uploads == [[("malfoyd-1.jpg", JPEG)]]
        assert interaction.followup.sent[0][0] == "Couldn't update that pick."
        assert restored[0]["content"] == build_content(_candidate(1, campaign="old"), None, False)
        assert restored[0]["attachments"] == []
        _controls(restored[0]["view"], 5)
        assert cog.queue_path.read_bytes() == before

    asyncio.run(run())


def test_lock_serializes_post_and_reroll(tmp_path):
    async def run():
        holder = {}
        started = asyncio.Event()
        release = asyncio.Event()
        seen = []

        class Media:
            async def inspect(self, url, budget):
                del budget
                assert holder["cog"]._lock.locked()
                seen.append(url)
                if len(seen) == 1:
                    started.set()
                    await release.wait()
                return Inspection(PreviewStatus.OK, "html")

            async def download(self, candidate, limit, directory, budget):
                del candidate, limit, directory, budget
                raise AssertionError("valid previews are not downloaded")

        cog, thread, _exchange = _build(
            tmp_path,
            _queue(message_id=5001, candidates=[_candidate(1), _candidate(2)]),
            media=Media(),
            role=None,
        )
        holder["cog"] = cog
        first = asyncio.create_task(cog.poll_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        interaction = _interaction(cog, user=FakeMember(2, cog.bot.guild), message_id=5001)
        second = asyncio.create_task(cog.handle_reroll(interaction))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not second.done()
        assert _saved(cog)["posted"] is False
        assert seen == [_preview(1)]
        release.set()
        await asyncio.wait_for(first, timeout=2)
        await asyncio.wait_for(second, timeout=2)
        assert _saved(cog)["posted"] is True
        assert _saved(cog)["index"] == 1
        assert _saved(cog)["rerolls_used"] == 1
        assert len(thread.sent) == 1
        assert _preview(2) in interaction.response.edits[0]["content"]
        assert not cog._lock.locked()

    asyncio.run(run())
