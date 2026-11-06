"""Preview check and attachment download for Malfoyd of the day.

Discord's crawler is the preview. A normal vx page is left as a link.
A confirmed failure is an HTTP 200 page whose HTML says the vx/Fx
unavailable text and has no media tags. HTTP errors, 403s, and timeouts
stay unknown. The public vx/Fx status APIs are used only after that
confirmation; a successful API response is not a successful preview.

Downloads stay on allowlisted HTTPS hosts. Redirects are checked the
same way. Sizes are bounded by Content-Length and by the bytes actually
read. Nothing is cached: callers put files in a temporary directory and
delete it themselves.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import aiohttp

DISCORD_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
API_UA = "PooperScooper Malfoyd (discord bot)"

PREVIEW_ATTEMPTS = 2
MAX_REDIRECTS = 2
MAX_MEDIA = 4
MAX_CANDIDATE_ATTEMPTS = 5
MAX_REQUESTS = 48
OPERATION_SECONDS = 45.0
PREVIEW_TIMEOUT = 8.0
API_TIMEOUT = 8.0
MEDIA_TIMEOUT = 20.0
HTML_CAP = 256 * 1024
JSON_CAP = 512 * 1024

_PREVIEW_HOSTS = frozenset({"vxtwitter.com", "www.vxtwitter.com"})
_API_HOSTS = frozenset({"api.vxtwitter.com", "api.fxtwitter.com"})
_MEDIA_HOSTS = frozenset(
    {
        "video.twimg.com",
        "pbs.twimg.com",
        "vxtwitter.com",
        "www.vxtwitter.com",
    }
)
_HOSTS = {
    "preview": _PREVIEW_HOSTS,
    "api": _API_HOSTS,
    "media": _MEDIA_HOSTS,
}
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# vx/Fx pages use these strings when the post itself is unavailable.
# Age-gate copy is intentionally not in this list.
_UNAVAILABLE_PHRASES = (
    "failed to scan your link",
    "tweet not found",
    "this tweet is from a suspended account",
    "this post is unavailable",
    "this tweet is unavailable",
)
_MEDIA_META = frozenset(
    {
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src",
        "twitter:player",
        "twitter:player:stream",
    }
)
_MP4_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV"}
)
_META_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(r"<source\b[^>]*\bsrc\s*=\s*['\"]https?://", re.IGNORECASE)
_VIDEO_RE = re.compile(r"<video\b", re.IGNORECASE)
_STATUS_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/([0-9]{1,20})/?$")


class PreviewStatus(Enum):
    OK = "ok"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNKNOWN = "unknown"


class UnsafeAddress(Exception):
    """Resolved address is not a public unicast address."""


@dataclass(frozen=True)
class Inspection:
    status: PreviewStatus
    detail: str


@dataclass(frozen=True)
class StoredFile:
    path: str
    filename: str


@dataclass(frozen=True)
class DownloadResult:
    files: tuple[StoredFile, ...]
    reason: str


@dataclass(frozen=True)
class _Fetch:
    opened: object | None
    detail: str
    retry: bool


class Budget:
    """Shared request and time ceiling for one post or reroll."""

    def __init__(
        self,
        now: Callable[[], float],
        *,
        requests: int = MAX_REQUESTS,
        seconds: float = OPERATION_SECONDS,
    ):
        self._now = now
        self.requests = requests
        self.deadline = now() + seconds
        self.used = 0

    def expired(self) -> bool:
        return self._now() >= self.deadline

    def allow_request(self) -> str | None:
        if self.expired():
            return "timed out"
        if self.used >= self.requests:
            return "request limit"
        self.used += 1
        return None


def ip_is_public(value: str) -> bool:
    """True for a globally routable unicast address. Mapped IPv4 is unwrapped."""
    text = value.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
        or not parsed.is_global
    ):
        return False
    if isinstance(parsed, ipaddress.IPv4Address) and parsed in _CGNAT:
        return False
    return True


def validate_public_https(url: str, policy: str) -> str | None:
    """Reject anything that is not HTTPS on the allowlist for this hop.

    The return value is a short reason. None means the URL may be requested.
    Credentials, IP literals, odd ports, and non-allowlisted hosts fail.
    """
    if not isinstance(url, str) or not url or any(ch in url for ch in "\\\n\r\t"):
        return "blocked url"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return "blocked url"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "blocked credentials"
    host = parts.hostname
    if host is None:
        return "blocked url"
    try:
        host = host.lower().rstrip(".").encode("ascii").decode("ascii")
    except UnicodeError:
        return "blocked host"
    allowed = _HOSTS.get(policy)
    if allowed is None or host not in allowed:
        return "blocked host"
    if parts.port not in (None, 443):
        return "blocked port"
    return None


def classify_html(body: bytes) -> PreviewStatus:
    """Confirmed failure needs the explicit unavailable text and no media.

    Any other 200 body, including an age-gate page, is a normal preview.
    This function does not treat the vx/Fx API as a preview.
    """
    text = body.decode("utf-8", errors="replace")
    if _explicit_unavailable(text) and not _html_has_media(text):
        return PreviewStatus.CONFIRMED_FAILURE
    return PreviewStatus.OK


def status_parts(url: object) -> tuple[str, str] | None:
    """Return (handle, status id) from a vxtwitter status URL."""
    if not isinstance(url, str):
        return None
    if validate_public_https(url, "preview") is not None:
        return None
    path = urllib.parse.urlsplit(url).path
    match = _STATUS_RE.match(path)
    if match is None:
        return None
    return match.group(1), match.group(2)


def media_urls_from_payload(payload: object) -> list[str]:
    """Pull photo and MP4 URLs from a documented vx or Fx status payload."""
    tweet = _tweet_object(payload)
    if tweet is None:
        return []
    urls = _fx_urls(tweet)
    if not urls:
        urls = _vx_urls(tweet)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= MAX_MEDIA:
            break
    return deduped


def content_length(headers: Mapping[str, object]) -> int | None:
    """Strict positive length. Missing, duplicate, or junk values are unknown."""
    found: object | None = None
    for key, value in headers.items():
        if str(key).lower() != "content-length":
            continue
        if found is not None:
            return None
        found = value
    if isinstance(found, bool) or found is None:
        return None
    if isinstance(found, int):
        return found if found > 0 else None
    if isinstance(found, str) and found.isdecimal():
        number = int(found)
        return number if number > 0 else None
    return None


def sniff_media(data: bytes) -> str | None:
    """Return a file extension when the bytes are a real image or a playable MP4."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if mp4_compatible(data):
        return "mp4"
    return None


def mp4_compatible(data: bytes) -> bool:
    """H.264 MP4 that Discord can play. No transcoding and no HEVC/AV1."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    size = int.from_bytes(data[0:4], "big")
    if size < 16 or size > len(data):
        return False
    brands = {data[8:12]}
    offset = 16
    while offset + 4 <= size:
        brands.add(data[offset : offset + 4])
        offset += 4
    if brands.isdisjoint(_MP4_BRANDS):
        return False
    if b"avc1" not in data and b"avc3" not in data:
        return False
    if any(marker in data for marker in (b"hvc1", b"hev1", b"dvh1", b"av01")):
        return False
    return b"moov" in data and b"mdat" in data


def _explicit_unavailable(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _UNAVAILABLE_PHRASES)


def _html_has_media(text: str) -> bool:
    for match in _META_RE.finditer(text):
        attrs: dict[str, str] = {}
        for attr in _ATTR_RE.finditer(match.group(1)):
            key = attr.group(1).lower()
            value = next((group for group in attr.groups()[1:] if group is not None), "")
            attrs[key] = value.strip()
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content") or attrs.get("value") or ""
        if name in _MEDIA_META and content.lower().startswith(("http://", "https://")):
            return True
    if _VIDEO_RE.search(text) or _SOURCE_RE.search(text):
        return True
    return False


def _tweet_object(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    tweet = payload.get("tweet")
    if isinstance(tweet, dict):
        return tweet
    return payload


def _fx_urls(tweet: dict) -> list[str]:
    media = tweet.get("media")
    if not isinstance(media, dict):
        return []
    pool = media.get("all")
    if not isinstance(pool, list) or not pool:
        pool = []
        for key in ("videos", "photos"):
            items = media.get(key)
            if isinstance(items, list):
                pool.extend(items)
    urls: list[str] = []
    for item in pool:
        url = _item_url(item)
        if url is not None:
            urls.append(url)
    return urls


def _vx_urls(tweet: dict) -> list[str]:
    extended = tweet.get("media_extended")
    urls: list[str] = []
    if isinstance(extended, list):
        for item in extended:
            url = _item_url(item)
            if url is not None:
                urls.append(url)
    if urls:
        return urls
    raw = tweet.get("mediaURLs")
    if isinstance(raw, list):
        for url in raw:
            if isinstance(url, str) and url.startswith("https://"):
                urls.append(url)
    return urls


def _item_url(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "").lower()
    url = item.get("url")
    if kind in {"video", "gif", "image", "photo"} and isinstance(url, str):
        return url
    return None


def api_urls(handle: str, status_id: str) -> tuple[str, str]:
    """Documented vx and Fx status endpoints. The handle is ignored by both APIs."""
    return (
        f"https://api.vxtwitter.com/{handle}/status/{status_id}",
        f"https://api.fxtwitter.com/{handle}/status/{status_id}",
    )


class PublicResolver(aiohttp.ThreadedResolver):
    """Drop the connection when any resolved address is not public."""

    async def resolve(self, hostname: str, port: int = 0, family: int = socket.AF_INET):
        infos = await super().resolve(hostname, port, family)
        if not infos:
            raise UnsafeAddress(hostname)
        for item in infos:
            if not ip_is_public(str(item.get("host", ""))):
                raise UnsafeAddress(hostname)
        return infos


async def assert_public_resolution(host: str) -> None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(str(exc)) from exc
    if not infos:
        raise OSError("no addresses")
    for info in infos:
        address = info[4][0]
        if not ip_is_public(str(address)):
            raise UnsafeAddress(host)


class AiohttpOpened:
    def __init__(self, session: aiohttp.ClientSession, response: aiohttp.ClientResponse):
        self.status = response.status
        self.headers = response.headers
        self.url = str(response.url)
        self._session = session
        self._response = response
        self._released = False

    async def read(self, cap: int) -> tuple[bytes, bool]:
        buf = bytearray()
        truncated = False
        async for chunk in self._response.content.iter_chunked(16 * 1024):
            if not chunk:
                continue
            if len(buf) + len(chunk) > cap:
                truncated = True
                break
            buf.extend(chunk)
        return bytes(buf), truncated

    async def stream_to(self, path: Path, cap: int) -> tuple[int, bool]:
        total = 0
        overflow = False
        with open(path, "wb") as handle:
            async for chunk in self._response.content.iter_chunked(64 * 1024):
                if not chunk:
                    continue
                if total + len(chunk) > cap:
                    overflow = True
                    break
                handle.write(chunk)
                total += len(chunk)
        if overflow:
            path.unlink(missing_ok=True)
        return total, overflow

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._response.release()
        await self._session.close()


class AiohttpExchange:
    """One HTTPS GET. Redirects are not followed. Private DNS never connects."""

    def __init__(self, session_factory: Callable[[aiohttp.ClientTimeout], aiohttp.ClientSession] | None = None):
        self._session_factory = session_factory

    async def open(self, url: str, headers: Mapping[str, str], timeout: float) -> AiohttpOpened:
        host = urllib.parse.urlsplit(url).hostname
        if not host:
            raise UnsafeAddress(url)
        await assert_public_resolution(host)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        if self._session_factory is not None:
            session = self._session_factory(client_timeout)
        else:
            connector = aiohttp.TCPConnector(resolver=PublicResolver(), ssl=True, force_close=True)
            session = aiohttp.ClientSession(
                timeout=client_timeout,
                connector=connector,
                trust_env=False,
            )
        try:
            response = await session.get(
                url,
                headers=dict(headers),
                allow_redirects=False,
            )
        except UnsafeAddress:
            await session.close()
            raise
        except Exception as exc:
            await session.close()
            cause = exc.__cause__
            if isinstance(exc, UnsafeAddress) or isinstance(cause, UnsafeAddress):
                raise UnsafeAddress(str(exc)) from exc
            raise
        return AiohttpOpened(session, response)


class MediaClient:
    """Fetches the preview HTML and, only for a confirmed failure, the media files."""

    def __init__(self, exchange: object | None = None):
        self._exchange = exchange if exchange is not None else AiohttpExchange()

    async def inspect(self, url: str, budget: Budget) -> Inspection:
        detail = "unknown"
        for attempt in range(PREVIEW_ATTEMPTS):
            fetched = await self._fetch(
                url,
                policy="preview",
                headers={"User-Agent": DISCORD_UA, "Accept": "text/html"},
                timeout=PREVIEW_TIMEOUT,
                budget=budget,
            )
            if fetched.opened is None:
                detail = fetched.detail
                if not fetched.retry or attempt + 1 == PREVIEW_ATTEMPTS:
                    return Inspection(PreviewStatus.UNKNOWN, detail)
                continue
            try:
                body, truncated = await fetched.opened.read(HTML_CAP)
            finally:
                await fetched.opened.release()
            if truncated:
                return Inspection(PreviewStatus.UNKNOWN, "preview truncated")
            return Inspection(classify_html(body), "html")
        return Inspection(PreviewStatus.UNKNOWN, detail)

    async def download(self, candidate: dict, limit: int, directory: str, budget: Budget) -> DownloadResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return DownloadResult((), "upload limit unknown")
        parts = status_parts(candidate.get("vx_url") if isinstance(candidate, dict) else None)
        if parts is None:
            return DownloadResult((), "no status id")
        urls, detail = await self._media_urls(parts[0], parts[1], budget)
        if not urls:
            return DownloadResult((), detail or "no media")
        stored: list[StoredFile] = []
        root = Path(directory)
        for number, media_url in enumerate(urls, start=1):
            saved = await self._download_one(media_url, limit, root, number, budget)
            if isinstance(saved, str):
                return DownloadResult((), saved)
            stored.append(saved)
        return DownloadResult(tuple(stored), "")

    async def _media_urls(self, handle: str, status_id: str, budget: Budget) -> tuple[list[str], str]:
        detail = "no media"
        for api_url in api_urls(handle, status_id):
            fetched = await self._fetch(
                api_url,
                policy="api",
                headers={"User-Agent": API_UA, "Accept": "application/json"},
                timeout=API_TIMEOUT,
                budget=budget,
            )
            if fetched.opened is None:
                detail = fetched.detail
                continue
            try:
                body, truncated = await fetched.opened.read(JSON_CAP)
            finally:
                await fetched.opened.release()
            if truncated:
                detail = "api truncated"
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, ValueError):
                detail = "api invalid"
                continue
            urls = media_urls_from_payload(payload)
            if urls:
                return urls, ""
            detail = "no media"
        return [], detail

    async def _download_one(
        self,
        url: str,
        limit: int,
        root: Path,
        number: int,
        budget: Budget,
    ) -> StoredFile | str:
        fetched = await self._fetch(
            url,
            policy="media",
            headers={"User-Agent": API_UA, "Accept": "*/*"},
            timeout=MEDIA_TIMEOUT,
            budget=budget,
        )
        if fetched.opened is None:
            return fetched.detail or "download failed"
        opened = fetched.opened
        partial = root / f"part-{number}"
        try:
            declared = content_length(opened.headers)
            if declared is None:
                return "size unknown"
            if declared > limit:
                return "oversize"
            _total, overflow = await opened.stream_to(partial, declared)
            if overflow or not partial.is_file():
                return "stream cap"
            data = partial.read_bytes()
        finally:
            await opened.release()
        kind = sniff_media(data)
        if kind is None:
            partial.unlink(missing_ok=True)
            return "signature"
        final = root / f"malfoyd-{number}.{kind}"
        partial.replace(final)
        return StoredFile(str(final), final.name)

    async def _fetch(
        self,
        url: str,
        *,
        policy: str,
        headers: Mapping[str, str],
        timeout: float,
        budget: Budget,
    ) -> _Fetch:
        current = url
        seen: set[str] = set()
        for hop in range(MAX_REDIRECTS + 1):
            blocked = validate_public_https(current, policy)
            if blocked is not None:
                return _Fetch(None, blocked, False)
            if current in seen:
                return _Fetch(None, "redirect loop", False)
            seen.add(current)
            limited = budget.allow_request()
            if limited is not None:
                return _Fetch(None, limited, False)
            try:
                opened = await self._exchange.open(current, headers, timeout)
            except UnsafeAddress:
                return _Fetch(None, "blocked address", False)
            except (TimeoutError, asyncio.TimeoutError):
                return _Fetch(None, "timed out", True)
            except (aiohttp.ClientError, OSError):
                return _Fetch(None, "connection error", True)
            if opened.status in _REDIRECTS:
                location = _header(opened.headers, "location")
                await opened.release()
                if hop == MAX_REDIRECTS:
                    return _Fetch(None, "too many redirects", False)
                if not location:
                    return _Fetch(None, f"HTTP {opened.status}", True)
                current = urllib.parse.urljoin(current, location.strip())
                continue
            if opened.status != 200:
                status = opened.status
                await opened.release()
                return _Fetch(None, f"HTTP {status}", True)
            return _Fetch(opened, "", False)
        return _Fetch(None, "too many redirects", False)


def _header(headers: Mapping[str, object], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name and isinstance(value, str) and value:
            return value
    return None
