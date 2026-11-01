"""Halloween costume for the bot's per-server profile in Menaces to Sobriety.

The guild avatar and bio go on at 10:00 AM PT on 31 Oct 2026, when the candy
hunt starts, and come off at 6:00 AM PT on 1 Nov 2026, when the hunt ends.
Only the Menaces to Sobriety guild profile changes. The account username and
the global avatar stay as they are. Restoring puts back the snapshotted guild
avatar and bio, or clears them when there was nothing to snapshot.

If the bot is down at the start, the first loop tick inside the window applies
the costume and posts the announcement. If it is already past the end and the
costume was never applied, both are skipped. A failed apply or restore is
retried with backoff. A successful restore is never followed by another apply.

The announcement is posted once in the candy hunt's announcement channel.
``!costume apply`` does not post it unless ``announce`` is included. The
scheduled post still happens once during the window if it has not been posted.

Avatar file
-----------
``COSTUME_AVATAR_FILE`` is the default image name in ``assets/halloween``
(``gsd_costume_1.png``). Change that constant to pick a different default.

To pick a file without editing code, add ``data/costume_settings.json`` and
reload the cog. It is read once, at load::

    {"avatar": "gsd_costume_3.png"}

The value is a file name in ``assets/halloween``, not a path. A missing
settings file uses the default. Bad JSON, path separators, and names that
are not an existing image in that folder are logged and ignored.

Owner commands are the hidden prefix group ``!costume`` (Menaces to Sobriety,
the support server, or DMs)::

    !costume status
    !costume preview
    !costume apply [force] [announce]
    !costume restore
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.asset import Asset
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.candyhunt import CANDY_HUNT, find_announcement_channel, load_settings
from cogs.utils.constants import DATA_DIR, PROJECT_ROOT
from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)

# Schedule and target. Change the clock here; nothing else hard-codes the dates.
TIMEZONE_NAME = "America/Los_Angeles"
PT = ZoneInfo(TIMEZONE_NAME)
MTS_GUILD_ID = 932057681307512922
NICK_ID = 87324314084331520
SUPPORT_GUILD_ID = 1045144253627637830
COSTUME_GUILD_IDS = (MTS_GUILD_ID, SUPPORT_GUILD_ID)
APPLY_AT = datetime(2026, 10, 31, 10, 0, tzinfo=PT)
REVERT_AT = datetime(2026, 11, 1, 6, 0, tzinfo=PT)
LOOP_SECONDS = 60
RETRY_BACKOFF_MINUTES = (5, 15, 30, 60)

HALLOWEEN_DIR = PROJECT_ROOT / "assets" / "halloween"
# One name to change when the owner picks gsd_costume_2.png, 3, or 4.
COSTUME_AVATAR_FILE = "gsd_costume_1.png"
AVATAR_ASSET = HALLOWEEN_DIR / COSTUME_AVATAR_FILE
BIO_ASSET = HALLOWEEN_DIR / "bio.txt"
ANNOUNCEMENT_ASSET = HALLOWEEN_DIR / "announcement.md"
STATE_PATH = DATA_DIR / "costume.json"
SETTINGS_PATH = DATA_DIR / "costume_settings.json"
BACKUP_AVATAR = DATA_DIR / "costume_backup_avatar.png"

APPLY_REASON = "Halloween costume"
REVERT_REASON = "Restoring profile after Halloween costume"
BIO_LIMIT = 190
ANNOUNCEMENT_LIMIT = 2000
_AVATAR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(?:png|jpg|jpeg|webp)$")

COSTUME_USAGE = (
    "Usage:\n"
    "!costume status\n"
    "!costume preview\n"
    "!costume apply [force] [announce]\n"
    "!costume restore"
)

STATE_KEYS = (
    "applied_at",
    "apply_attempted",
    "apply_error",
    "apply_failures",
    "apply_retry_at",
    "reverted_at",
    "revert_attempted",
    "revert_error",
    "revert_failures",
    "revert_retry_at",
    "had_guild_avatar",
    "had_guild_bio",
    "original_bio",
    "announced_at",
    "announce_attempted",
    "announce_error",
    "announce_message_id",
    "announce_retry_at",
)


class _Unset:
    """Marks a constructor argument the caller did not pass."""


_UNSET = _Unset()


def default_state() -> dict:
    return {
        "applied_at": None,
        "apply_attempted": False,
        "apply_error": None,
        "apply_failures": 0,
        "apply_retry_at": None,
        "reverted_at": None,
        "revert_attempted": False,
        "revert_error": None,
        "revert_failures": 0,
        "revert_retry_at": None,
        "had_guild_avatar": False,
        "had_guild_bio": False,
        "original_bio": None,
        "announced_at": None,
        "announce_attempted": False,
        "announce_error": None,
        "announce_message_id": None,
        "announce_retry_at": None,
    }


def _opt_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _opt_id(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return None


def _as_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _as_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def normalize_state(raw: object) -> dict:
    base = default_state()
    if not isinstance(raw, dict):
        return base
    for key in ("apply_attempted", "revert_attempted", "had_guild_avatar", "had_guild_bio", "announce_attempted"):
        base[key] = _as_bool(raw.get(key))
    for key in ("apply_failures", "revert_failures"):
        base[key] = _as_count(raw.get(key))
    for key in (
        "applied_at",
        "apply_error",
        "apply_retry_at",
        "reverted_at",
        "revert_error",
        "revert_retry_at",
        "original_bio",
        "announced_at",
        "announce_error",
        "announce_retry_at",
    ):
        base[key] = _opt_str(raw.get(key))
    base["announce_message_id"] = _opt_id(raw.get("announce_message_id"))
    return base


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return as_utc(moment).isoformat()


def format_pt(moment: datetime) -> str:
    return as_utc(moment).astimezone(PT).strftime("%Y-%m-%d %H:%M PT")


def _parse_moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def _retry_ready(state: dict, kind: str, now: datetime) -> bool:
    moment = _parse_moment(state.get(f"{kind}_retry_at"))
    if moment is None:
        return True
    return as_utc(now) >= as_utc(moment)


def backoff_delay(failures: int, retry_after: float | None = None) -> timedelta:
    """Wait for the backoff step, or longer when Discord says so."""
    try:
        count = int(failures)
    except (TypeError, ValueError):
        count = 1
    if count < 1:
        count = 1
    index = min(count, len(RETRY_BACKOFF_MINUTES)) - 1
    delay = timedelta(minutes=RETRY_BACKOFF_MINUTES[index])
    if retry_after is None:
        return delay
    try:
        extra = float(retry_after)
    except (TypeError, ValueError):
        return delay
    if extra <= 0:
        return delay
    rate_delay = timedelta(seconds=extra)
    if rate_delay > delay:
        return rate_delay
    return delay


def rate_limit_retry_after(exc: BaseException) -> float | None:
    """Seconds Discord asked us to wait, for HTTP 429 and ``RateLimited``.

    ``discord.py`` sleeps through ordinary 429s and raises ``RateLimited``
    when the wait is longer than its cap. A raw ``HTTPException`` with
    status 429 is handled the same way when ``retry_after`` or a
    ``Retry-After`` header is present.
    """
    status = getattr(exc, "status", None)
    if not isinstance(exc, discord.RateLimited) and status != 429:
        return None
    value = getattr(exc, "retry_after", None)
    if value is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        if callable(getter):
            value = getter("Retry-After")
            if value is None:
                value = getter("X-RateLimit-Reset-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def apply_is_open(state: dict, *, force: bool = False, now: datetime | None = None, ignore_backoff: bool = False) -> bool:
    """True when an apply edit is still allowed.

    A recorded restore closes apply for good, including ``force``.
    """
    state = normalize_state(state)
    if state["reverted_at"]:
        return False
    if state["applied_at"] and not force:
        return False
    if force or ignore_backoff:
        return True
    if now is not None and not _retry_ready(state, "apply", now):
        return False
    return True


def revert_is_open(state: dict, *, now: datetime | None = None, ignore_backoff: bool = False) -> bool:
    """True when a restore is still allowed."""
    state = normalize_state(state)
    if state["reverted_at"] or not state["applied_at"]:
        return False
    if ignore_backoff:
        return True
    if now is not None and not _retry_ready(state, "revert", now):
        return False
    return True


def announce_is_due(
    now: datetime,
    state: dict,
    *,
    apply_at: datetime = APPLY_AT,
    revert_at: datetime = REVERT_AT,
) -> bool:
    """True when the scheduled announcement still needs its one post."""
    now_utc = as_utc(now)
    if now_utc < as_utc(apply_at) or now_utc >= as_utc(revert_at):
        return False
    state = normalize_state(state)
    if state["reverted_at"]:
        return False
    if state["announced_at"] or state["announce_message_id"] or state["announce_attempted"]:
        return False
    return _retry_ready(state, "announce", now)


def due_action(
    now: datetime,
    state: dict,
    *,
    apply_at: datetime = APPLY_AT,
    revert_at: datetime = REVERT_AT,
    manual: str | None = None,
    force: bool = False,
) -> str:
    """Return ``apply``, ``revert``, or ``none``.

    Automatic apply runs only inside the window, and only until it succeeds
    or the profile has been restored. Automatic restore runs at and after
    the end while the costume is still on. Manual commands ignore the clock
    and the backoff timer. ``force`` reapplies a costume that is already on.
    Nothing reapplies after a successful restore.
    """
    if manual not in (None, "apply", "revert"):
        raise ValueError(f"unknown costume action: {manual}")
    now_utc = as_utc(now)
    apply_utc = as_utc(apply_at)
    revert_utc = as_utc(revert_at)
    if apply_utc >= revert_utc:
        raise ValueError("apply time must be before revert time")
    state = normalize_state(state)

    if manual is None:
        if now_utc < apply_utc:
            return "none"
        if now_utc < revert_utc:
            return "apply" if apply_is_open(state, now=now) else "none"
        return "revert" if revert_is_open(state, now=now) else "none"
    if manual == "apply":
        return "apply" if apply_is_open(state, force=force, ignore_backoff=True) else "none"
    return "revert" if revert_is_open(state, ignore_backoff=True) else "none"


def should_snapshot(state: dict) -> bool:
    """Snapshot the current profile once, before the first successful apply.

    A later retry must not overwrite the backup with the costume itself.
    """
    state = normalize_state(state)
    if state["applied_at"]:
        return False
    if state["apply_attempted"] and (
        state["had_guild_avatar"] or state["had_guild_bio"] or state["original_bio"]
    ):
        return False
    return True


def record_apply_success(state: dict, now: datetime) -> dict:
    updated = normalize_state(state)
    updated["apply_attempted"] = True
    updated["apply_error"] = None
    updated["apply_failures"] = 0
    updated["apply_retry_at"] = None
    updated["applied_at"] = iso(now)
    updated["reverted_at"] = None
    updated["revert_attempted"] = False
    updated["revert_error"] = None
    updated["revert_failures"] = 0
    updated["revert_retry_at"] = None
    return updated


def record_revert_success(state: dict, now: datetime) -> dict:
    updated = normalize_state(state)
    updated["revert_attempted"] = True
    updated["revert_error"] = None
    updated["revert_failures"] = 0
    updated["revert_retry_at"] = None
    updated["reverted_at"] = iso(now)
    return updated


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise
    except OSError:
        raise
    except UnicodeError as exc:
        raise ValueError(f"Could not read {path.name} as UTF-8: {exc}") from exc


def load_bio_text(path: str | Path) -> str:
    """Read the costume bio. ``utf-8-sig`` drops a leading BOM."""
    file_path = Path(path)
    try:
        text = _read_text(file_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"Costume bio is missing: {file_path}") from None
    text = text.strip()
    if not text:
        raise ValueError(f"Costume bio is empty: {file_path}")
    if len(text) > BIO_LIMIT:
        raise ValueError(f"Costume bio is {len(text)} characters; the limit is {BIO_LIMIT}")
    return text


def load_announcement_text(path: str | Path) -> str:
    """Read the announcement at post time. Refuse anything over 2000 characters."""
    file_path = Path(path)
    try:
        text = _read_text(file_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"Costume announcement is missing: {file_path}") from None
    text = text.strip()
    if not text:
        raise ValueError(f"Costume announcement is empty: {file_path}")
    if len(text) > ANNOUNCEMENT_LIMIT:
        raise ValueError(
            f"Costume announcement is {len(text)} characters; the limit is {ANNOUNCEMENT_LIMIT}"
        )
    return text


def load_image_bytes(path: str | Path, what: str) -> bytes:
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(f"{what} is missing: {file_path}") from None
    if not data:
        raise ValueError(f"{what} is empty: {file_path}")
    return data


def load_avatar_bytes(path: str | Path) -> bytes:
    return load_image_bytes(path, "Costume avatar")


def resolve_avatar_file(settings_path: str | Path, halloween_dir: str | Path, default_name: str) -> str:
    """Return the avatar file name to load.

    A missing settings file, bad JSON, or an invalid name keeps ``default_name``.
    """
    path = Path(settings_path)
    folder = Path(halloween_dir)
    if not path.is_file():
        return default_name
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log.warning("Costume settings ignored (%s); using the default avatar", exc)
        return default_name
    if not isinstance(raw, dict):
        log.warning("Costume settings ignored (not an object); using the default avatar")
        return default_name
    if "avatar" not in raw or raw.get("avatar") is None:
        return default_name
    avatar = raw.get("avatar")
    if not isinstance(avatar, str) or _AVATAR_NAME.fullmatch(avatar.strip()) is None:
        log.warning("Costume settings avatar %r is invalid; using the default avatar", avatar)
        return default_name
    name = avatar.strip()
    try:
        root = folder.resolve()
        candidate = (folder / name).resolve()
    except OSError as exc:
        log.warning("Costume settings avatar %s could not be resolved (%s); using the default", name, exc)
        return default_name
    if candidate.parent != root:
        log.warning("Costume settings avatar %r escapes the halloween folder; using the default", name)
        return default_name
    if not candidate.is_file():
        log.warning("Costume settings avatar %s is missing; using the default avatar", name)
        return default_name
    return name


def announcement_settings(bot) -> tuple[int | None, str]:
    """Candy-hunt announcement channel id and name, with safe fallbacks."""
    settings = None
    getter = getattr(bot, "get_cog", None)
    if callable(getter):
        try:
            cog = getter("CandyHunt")
        except Exception:
            log.warning("Costume could not read the CandyHunt cog", exc_info=True)
            cog = None
        settings = getattr(cog, "settings", None)
    if settings is None:
        try:
            settings = load_settings()
        except Exception:
            log.warning("Costume could not read candy hunt settings; using defaults", exc_info=True)
            settings = CANDY_HUNT
    channel_id = getattr(settings, "announcement_channel_id", None)
    name = getattr(settings, "announcement_channel_name", None) or CANDY_HUNT.announcement_channel_name
    if channel_id is not None:
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            log.warning("Costume ignored candy hunt announcement_channel_id %r", channel_id)
            channel_id = None
    if not isinstance(name, str) or not name.strip():
        name = "announcements"
    else:
        name = name.strip()
    return channel_id, name


def bio_from_member_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    bio = payload.get("bio")
    if not isinstance(bio, str):
        return None
    text = bio.strip()
    return text or None


def avatar_hash_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    avatar = payload.get("avatar")
    if isinstance(avatar, str) and avatar:
        return avatar
    return None


def select_guild_avatar_asset(member, payload: object):
    """Asset to back up, or None when the member has no guild avatar.

    A payload that includes ``avatar: null`` wins over a stale cache entry.
    """
    if isinstance(payload, dict) and "avatar" in payload:
        avatar_hash = avatar_hash_from_payload(payload)
        if not avatar_hash:
            return None
        cached = getattr(member, "guild_avatar", None)
        cached_key = getattr(cached, "key", None)
        if cached is not None and (cached_key is None or cached_key == avatar_hash):
            return cached
        state = getattr(member, "_state", None)
        guild = getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        member_id = getattr(member, "id", None)
        if state is None or guild_id is None or member_id is None:
            if cached is not None:
                return cached
            raise RuntimeError("Guild avatar is set but I can't download it")
        return Asset._from_guild_avatar(state, guild_id, member_id, avatar_hash)
    return getattr(member, "guild_avatar", None)


def revert_payload(state: dict, backup_path: str | Path) -> tuple[bytes | None, str | None]:
    """Bytes and bio to send. None clears that per-server field."""
    current = normalize_state(state)
    if current["had_guild_avatar"]:
        avatar: bytes | None = load_image_bytes(backup_path, "Backup guild avatar")
    else:
        avatar = None
    if current["had_guild_bio"]:
        bio = current["original_bio"]
        if not bio:
            raise ValueError("Original guild bio is missing from costume state")
    else:
        bio = None
    return avatar, bio


def error_text(exc: BaseException, limit: int = 400) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text or type(exc).__name__


def parse_apply_spec(spec: str | None) -> tuple[bool, bool] | None:
    """Return ``(force, announce)``. Unknown tokens return None."""
    if spec is None:
        return False, False
    if not isinstance(spec, str):
        return None
    force = False
    announce = False
    for token in spec.split():
        key = token.casefold()
        if key == "force":
            force = True
        elif key == "announce":
            announce = True
        else:
            return None
    return force, announce


def preview_message(bio: str, announcement: str) -> str | None:
    """One Discord message containing the bio and the full announcement.

    Returns None when the pair does not fit in 2000 characters. The caller
    sends them separately instead of cutting the announcement short.
    """
    text = f"**Bio**\n{bio}\n\n**Announcement**\n{announcement}"
    if len(text) <= ANNOUNCEMENT_LIMIT:
        return text
    return None


def _shown(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_status(
    state: dict,
    *,
    now: datetime,
    apply_at: datetime = APPLY_AT,
    revert_at: datetime = REVERT_AT,
    avatar_name: str,
    avatar_ready: bool,
    avatar_note: str,
    bio_ready: bool,
    announcement_ready: bool,
    backup_ready: bool,
    announcement_channel: str,
    guild_id: int = MTS_GUILD_ID,
) -> str:
    current = normalize_state(state)
    due = due_action(now, current, apply_at=apply_at, revert_at=revert_at)
    lines = [
        f"Halloween costume — Menaces to Sobriety ({guild_id})",
        f"Apply at: {format_pt(apply_at)}",
        f"Revert at: {format_pt(revert_at)}",
        f"Now: {format_pt(now)}",
        f"Due now: {due}",
        "",
        "State:",
        f"apply_attempted: {_shown(current['apply_attempted'])}",
        f"applied_at: {_shown(current['applied_at'])}",
        f"apply_error: {_shown(current['apply_error'])}",
        f"apply_failures: {_shown(current['apply_failures'])}",
        f"apply_retry_at: {_shown(current['apply_retry_at'])}",
        f"revert_attempted: {_shown(current['revert_attempted'])}",
        f"reverted_at: {_shown(current['reverted_at'])}",
        f"revert_error: {_shown(current['revert_error'])}",
        f"revert_failures: {_shown(current['revert_failures'])}",
        f"revert_retry_at: {_shown(current['revert_retry_at'])}",
        f"had_guild_avatar: {_shown(current['had_guild_avatar'])}",
        f"had_guild_bio: {_shown(current['had_guild_bio'])}",
        f"original_bio: {_shown(current['original_bio'])}",
        f"announce_attempted: {_shown(current['announce_attempted'])}",
        f"announced_at: {_shown(current['announced_at'])}",
        f"announce_error: {_shown(current['announce_error'])}",
        f"announce_message_id: {_shown(current['announce_message_id'])}",
        f"announce_retry_at: {_shown(current['announce_retry_at'])}",
        "",
        "Assets:",
        f"avatar file: {avatar_name} ({'present' if avatar_ready else 'missing'}, {avatar_note})",
        f"bio.txt: {'present' if bio_ready else 'missing'}",
        f"announcement.md: {'present' if announcement_ready else 'missing'}",
        f"backup avatar: {'present' if backup_ready else 'absent'}",
        f"Announcement channel: {announcement_channel}",
    ]
    text = "\n".join(lines)
    if len(text) > 1900:
        return text[:1890] + "\n…(truncated)"
    return text


def block_message(state: dict, *, manual: str, force: bool) -> str:
    current = normalize_state(state)
    if manual == "apply":
        if current["reverted_at"]:
            return "The costume was already restored. I won't apply it again."
        if current["applied_at"] and not force:
            return "The costume is already applied. Use !costume apply force to run it again."
    if manual == "revert":
        if current["reverted_at"]:
            return "The per-server profile is already restored."
        if not current["applied_at"]:
            return "The costume was never applied, so there's nothing to restore."
    return "Nothing to do."


def result_message(action: str, status: str, error: str | None = None) -> str:
    if status == "applied":
        return "Halloween costume is on in Menaces to Sobriety."
    if status == "reverted":
        return "Per-server profile restored in Menaces to Sobriety."
    if status == "unavailable":
        return "I can't see Menaces to Sobriety right now, so I didn't change the profile."
    if status == "error":
        verb = "Apply" if action == "apply" else "Restore"
        detail = error or "unknown error"
        return f"{verb} failed: {detail} I'll try again on backoff."
    return "Nothing to do."


@dataclass(frozen=True)
class StepResult:
    status: str
    error: str | None = None


@dataclass(frozen=True)
class ProfileSnapshot:
    had_avatar: bool
    avatar_bytes: bytes | None
    bio: str | None


class CostumeStore:
    """JSON book for the costume. The path is injectable so tests stay out of data/."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = LockedJsonFile(self.path, default=default_state, indent=2)
        self.state = normalize_state(self._file.read())

    def save(self) -> None:
        normalized = normalize_state(self.state)
        # Update in place so the cog and the store keep the same dict.
        self.state.clear()
        self.state.update(normalized)
        self._file.write(dict(self.state))


def _command_is_costume(command) -> bool:
    seen: set[int] = set()
    current = command
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "parent", None) is None:
            return getattr(current, "name", None) == "costume"
        current = getattr(current, "parent", None)
    return False


async def _costume_allowed(ctx) -> bool:
    """Owner, and only in MTS, the support guild, or a DM with the bot."""
    author = getattr(ctx, "author", None)
    author_id = getattr(author, "id", None)
    if author_id != NICK_ID:
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
    return getattr(guild, "id", None) in COSTUME_GUILD_IDS


_costume_check = commands.check(_costume_allowed)


class Costume(Cog):
    """Puts the Halloween avatar and bio on the MTS per-server profile."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        path: str | Path | None = None,
        avatar_path: str | Path | None = None,
        bio_path: str | Path | None = None,
        announcement_path: str | Path | None = None,
        backup_path: str | Path | None = None,
        settings_path: str | Path | None = None,
        halloween_dir: str | Path | None = None,
        guild_id: int | None = None,
        apply_at: datetime | None = None,
        revert_at: datetime | None = None,
        announcement_channel_id: object = _UNSET,
        announcement_channel_name: object = _UNSET,
    ):
        self.bot = bot
        self.guild_id = MTS_GUILD_ID if guild_id is None else guild_id
        self.apply_at = APPLY_AT if apply_at is None else apply_at
        self.revert_at = REVERT_AT if revert_at is None else revert_at
        self.halloween_dir = Path(HALLOWEEN_DIR if halloween_dir is None else halloween_dir)
        self.settings_path = Path(SETTINGS_PATH if settings_path is None else settings_path)
        self.avatar_from_settings = False
        if avatar_path is None:
            name = resolve_avatar_file(self.settings_path, self.halloween_dir, COSTUME_AVATAR_FILE)
            self.avatar_from_settings = name != COSTUME_AVATAR_FILE
            self.avatar_path = self.halloween_dir / name
        else:
            self.avatar_path = Path(avatar_path)
        self.bio_path = Path(BIO_ASSET if bio_path is None else bio_path)
        self.announcement_path = Path(ANNOUNCEMENT_ASSET if announcement_path is None else announcement_path)
        self.backup_path = Path(BACKUP_AVATAR if backup_path is None else backup_path)
        self._channel_id_override = announcement_channel_id
        self._channel_name_override = announcement_channel_name
        self.store = CostumeStore(STATE_PATH if path is None else path)
        self.state = self.store.state
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        log.info(
            "Costume loaded guild=%s apply_at=%s revert_at=%s avatar=%s "
            "apply_attempted=%s revert_attempted=%s announce_attempted=%s",
            self.guild_id,
            format_pt(self.apply_at),
            format_pt(self.revert_at),
            self.avatar_path.name,
            str(self.state["apply_attempted"]).lower(),
            str(self.state["revert_attempted"]).lower(),
            str(self.state["announce_attempted"]).lower(),
        )
        if not self.costume_loop.is_running():
            self.costume_loop.start()

    async def cog_unload(self) -> None:
        self.costume_loop.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # CheckFailure for !costume is an unauthorized caller. Swallow it so
        # the default command error logger stays quiet. This cog has no other commands.
        if not _command_is_costume(getattr(ctx, "command", None)):
            log.error("Costume command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, (commands.CommandNotFound, commands.UserInputError)):
            try:
                await ctx.reply(COSTUME_USAGE, mention_author=False)
            except Exception:
                log.error("Costume usage reply failed", exc_info=True)
            return
        log.error("Costume command failed", exc_info=error)

    @tasks.loop(seconds=LOOP_SECONDS)
    async def costume_loop(self) -> None:
        try:
            await self.tick()
        except Exception:
            log.exception("Costume loop failed")

    @costume_loop.before_loop
    async def before_costume_loop(self) -> None:
        await self.bot.wait_until_ready()

    def _save(self) -> None:
        self.store.state = self.state
        self.store.save()
        self.state = self.store.state

    def _guild(self):
        getter = getattr(self.bot, "get_guild", None)
        if not callable(getter):
            return None
        return getter(self.guild_id)

    async def _member(self, guild):
        me = getattr(guild, "me", None)
        if me is not None:
            return me
        fetch = getattr(guild, "fetch_member", None)
        user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if not callable(fetch) or user_id is None:
            return None
        return await fetch(user_id)

    def _record_failure(self, kind: str, exc: BaseException, now: datetime) -> StepResult:
        message = error_text(exc)
        retry_after = rate_limit_retry_after(exc)
        failures = _as_count(self.state.get(f"{kind}_failures")) + 1
        delay = backoff_delay(failures, retry_after)
        next_at = as_utc(now) + delay
        self.state[f"{kind}_attempted"] = True
        self.state[f"{kind}_error"] = message
        self.state[f"{kind}_failures"] = failures
        self.state[f"{kind}_retry_at"] = iso(next_at)
        self._save()
        if retry_after is not None:
            log.warning(
                "Costume %s rate limited status=%s retry_after=%s seconds; next attempt %s (failure %s)",
                kind,
                getattr(exc, "status", "rate_limit"),
                retry_after,
                format_pt(next_at),
                failures,
            )
        else:
            log.exception(
                "Costume %s failed: %s; next attempt %s (failure %s, backoff %s seconds)",
                kind,
                message,
                format_pt(next_at),
                failures,
                int(delay.total_seconds()),
            )
        return StepResult("error", message)

    def _mark_attempt(self, kind: str) -> None:
        # Persist before the profile edit so a crash cannot apply twice without a record.
        # applied_at / reverted_at stay empty until the edit succeeds, so a crash retries.
        self.state[f"{kind}_attempted"] = True
        self.state[f"{kind}_error"] = None
        self._save()

    def _note_announce_problem(self, problem: BaseException | str, *, level: str) -> StepResult:
        message = error_text(problem) if isinstance(problem, BaseException) else str(problem)
        if self.state.get("announce_error") != message:
            if level == "error":
                log.error("Costume announcement not posted: %s", message)
            else:
                log.warning("Costume announcement waiting: %s", message)
            self.state["announce_error"] = message
            self._save()
        status = "error" if level == "error" else "unavailable"
        return StepResult(status, message)

    def _record_announce_failure(self, exc: BaseException, now: datetime) -> StepResult:
        message = error_text(exc)
        retry_after = rate_limit_retry_after(exc)
        self.state["announce_error"] = message
        if retry_after is not None:
            # The post did not land. Try once more after Discord's wait.
            self.state["announce_attempted"] = False
            delay = backoff_delay(1, retry_after)
            next_at = as_utc(now) + delay
            self.state["announce_retry_at"] = iso(next_at)
            log.warning(
                "Costume announcement rate limited status=%s retry_after=%s seconds; next attempt %s",
                getattr(exc, "status", "rate_limit"),
                retry_after,
                format_pt(next_at),
            )
        else:
            self.state["announce_attempted"] = True
            self.state["announce_retry_at"] = None
            log.exception("Costume announcement failed: %s", message)
        self._save()
        return StepResult("error", message)

    def _announcement_query(self) -> tuple[int | None, str]:
        if self._channel_id_override is _UNSET and self._channel_name_override is _UNSET:
            return announcement_settings(self.bot)
        channel_id: int | None = None
        name = "announcements"
        if self._channel_id_override is _UNSET or self._channel_name_override is _UNSET:
            channel_id, name = announcement_settings(self.bot)
        if self._channel_id_override is not _UNSET:
            channel_id = self._channel_id_override if isinstance(self._channel_id_override, int) else None
        if self._channel_name_override is not _UNSET:
            override_name = self._channel_name_override
            name = override_name.strip() if isinstance(override_name, str) and override_name.strip() else "announcements"
        return channel_id, name

    def _channel_rows(self):
        guild = self._guild()
        if guild is None:
            return None
        rows: list[tuple[int, str]] = []
        objects: dict[int, object] = {}
        for channel in getattr(guild, "text_channels", None) or []:
            channel_id = getattr(channel, "id", None)
            name = getattr(channel, "name", None)
            if isinstance(channel_id, int) and isinstance(name, str):
                rows.append((channel_id, name))
                objects[channel_id] = channel
        return rows, objects

    def _resolve_announcement_channel(self) -> tuple[object | None, str]:
        channel_id, name = self._announcement_query()
        located = self._channel_rows()
        hint = f"#{name}"
        if channel_id is not None:
            hint = f"{hint}, id {channel_id}"
        if located is None:
            return None, f"not found; Menaces to Sobriety is not cached ({hint})"
        rows, objects = located
        found = find_announcement_channel(rows, channel_id=channel_id, name=name)
        if found is None:
            return None, f"not found ({hint})"
        channel = objects.get(found)
        label_name = getattr(channel, "name", name)
        return channel, f"#{label_name} ({found})"

    async def _member_payload(self, guild_id: int, member_id: int) -> dict:
        getter = getattr(getattr(self.bot, "http", None), "get_member", None)
        if not callable(getter):
            raise RuntimeError("HTTP client cannot read the guild member")
        raw = await getter(guild_id, member_id)
        if not isinstance(raw, dict):
            raise RuntimeError("Guild member payload was not an object")
        return raw

    async def _read_bio(self, guild_id: int, payload: dict) -> str | None:
        if "bio" in payload:
            return bio_from_member_payload(payload)
        # GET /members omits the per-server bio. One empty modify-current-member
        # returns the bot's member object, including bio, and does not change fields.
        probe = getattr(getattr(self.bot, "http", None), "edit_my_member", None)
        if not callable(probe):
            log.warning("Could not read the existing per-server bio; revert will clear it if one was set.")
            return None
        try:
            probed = await probe(guild_id)
        except Exception:
            log.warning(
                "Could not read the existing per-server bio; revert will clear it if one was set.",
                exc_info=True,
            )
            return None
        return bio_from_member_payload(probed)

    async def _snapshot(self, guild, member) -> ProfileSnapshot:
        member_id = getattr(member, "id", None)
        if member_id is None:
            raise RuntimeError("Bot member has no id")
        payload = await self._member_payload(guild.id, member_id)
        bio = await self._read_bio(guild.id, payload)
        asset = select_guild_avatar_asset(member, payload)
        if asset is None:
            return ProfileSnapshot(had_avatar=False, avatar_bytes=None, bio=bio)
        avatar_bytes = await asset.read()
        if not avatar_bytes:
            raise RuntimeError("Guild avatar download was empty")
        return ProfileSnapshot(had_avatar=True, avatar_bytes=avatar_bytes, bio=bio)

    def _store_snapshot(self, snap: ProfileSnapshot) -> None:
        if snap.had_avatar:
            if not snap.avatar_bytes:
                raise RuntimeError("Guild avatar was set but the download was empty")
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            self.backup_path.write_bytes(snap.avatar_bytes)
        self.state["had_guild_avatar"] = snap.had_avatar
        self.state["had_guild_bio"] = snap.bio is not None
        self.state["original_bio"] = snap.bio
        self._save()

    async def _apply(self, now: datetime) -> StepResult:
        if normalize_state(self.state)["reverted_at"]:
            log.warning("Costume apply skipped; the profile was already restored")
            return StepResult("none")
        try:
            avatar_bytes = load_avatar_bytes(self.avatar_path)
            bio = load_bio_text(self.bio_path)
        except Exception as exc:
            return self._record_failure("apply", exc, now)

        guild = self._guild()
        if guild is None:
            log.warning("Costume apply waiting; guild %s is not available", self.guild_id)
            return StepResult("unavailable")
        try:
            member = await self._member(guild)
        except Exception:
            log.exception("Costume apply waiting; could not load the bot member")
            return StepResult("unavailable")
        if member is None:
            log.warning("Costume apply waiting; bot member is missing in guild %s", self.guild_id)
            return StepResult("unavailable")

        try:
            if should_snapshot(self.state):
                snap = await self._snapshot(guild, member)
                self._store_snapshot(snap)
        except Exception as exc:
            return self._record_failure("apply", exc, now)

        self._mark_attempt("apply")
        try:
            # Guild member edit only. This does not change the account avatar.
            await member.edit(avatar=avatar_bytes, bio=bio, reason=APPLY_REASON)
        except Exception as exc:
            return self._record_failure("apply", exc, now)

        self.state = record_apply_success(self.state, now)
        self._save()
        log.info("Applied Halloween costume in guild %s avatar=%s", self.guild_id, self.avatar_path.name)
        return StepResult("applied")

    async def _revert(self, now: datetime) -> StepResult:
        guild = self._guild()
        if guild is None:
            log.warning("Costume revert waiting; guild %s is not available", self.guild_id)
            return StepResult("unavailable")
        try:
            member = await self._member(guild)
        except Exception:
            log.exception("Costume revert waiting; could not load the bot member")
            return StepResult("unavailable")
        if member is None:
            log.warning("Costume revert waiting; bot member is missing in guild %s", self.guild_id)
            return StepResult("unavailable")

        try:
            avatar, bio = revert_payload(self.state, self.backup_path)
        except Exception as exc:
            return self._record_failure("revert", exc, now)

        self._mark_attempt("revert")
        try:
            await member.edit(avatar=avatar, bio=bio, reason=REVERT_REASON)
        except Exception as exc:
            return self._record_failure("revert", exc, now)

        self.state = record_revert_success(self.state, now)
        self._save()
        log.info("Reverted Halloween costume in guild %s", self.guild_id)
        return StepResult("reverted")

    async def _announce(self, now: datetime, *, manual: bool = False) -> StepResult:
        state = normalize_state(self.state)
        if state["announced_at"] or state["announce_message_id"]:
            return StepResult("already")
        if not manual and state["announce_attempted"]:
            return StepResult("already")
        if not manual and not _retry_ready(state, "announce", now):
            return StepResult("waiting")
        if state["reverted_at"] and not manual:
            return StepResult("none")

        try:
            text = load_announcement_text(self.announcement_path)
        except (OSError, UnicodeError, ValueError) as exc:
            # Too long, empty, or unreadable. Do not post a clipped message.
            return self._note_announce_problem(exc, level="error")

        channel, label = self._resolve_announcement_channel()
        if channel is None:
            return self._note_announce_problem(label, level="warning")
        send = getattr(channel, "send", None)
        if not callable(send):
            return self._note_announce_problem(f"Announcement channel cannot send ({label})", level="warning")

        # Persist the attempt before send so a crash cannot post a second copy.
        self.state["announce_attempted"] = True
        self.state["announce_error"] = None
        self.state["announce_retry_at"] = None
        self._save()
        try:
            sent = await send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            return self._record_announce_failure(exc, now)

        message_id = getattr(sent, "id", None)
        self.state["announce_attempted"] = True
        self.state["announced_at"] = iso(now)
        self.state["announce_message_id"] = str(message_id) if isinstance(message_id, int) and message_id > 0 else None
        self.state["announce_error"] = None
        self.state["announce_retry_at"] = None
        self._save()
        log.info(
            "Posted Halloween announcement channel=%s message=%s",
            getattr(channel, "id", "?"),
            self.state["announce_message_id"],
        )
        return StepResult("announced")

    async def _append_manual_announce(self, text: str, now: datetime) -> str:
        state = normalize_state(self.state)
        if state["reverted_at"]:
            return text + " I won't post the announcement after the costume was restored."
        if state["announced_at"] or state["announce_message_id"]:
            return text + " Announcement was already posted."
        result = await self._announce(now, manual=True)
        if result.status == "announced":
            return text + " Announcement posted."
        if result.status == "already":
            return text + " Announcement was already posted."
        detail = result.error or "it was not posted"
        return text + f" Announcement not posted: {detail}"

    async def tick(self, *, now: datetime | None = None) -> str:
        moment = now if now is not None else discord.utils.utcnow()
        async with self._lock:
            action = due_action(
                moment,
                self.state,
                apply_at=self.apply_at,
                revert_at=self.revert_at,
            )
            profile = "none"
            if action == "apply":
                profile = (await self._apply(moment)).status
            elif action == "revert":
                profile = (await self._revert(moment)).status
            if announce_is_due(moment, self.state, apply_at=self.apply_at, revert_at=self.revert_at):
                announced = await self._announce(moment)
                if profile == "none":
                    if announced.status == "already":
                        return "none"
                    return announced.status
            return profile

    async def _manual(
        self,
        kind: str,
        *,
        force: bool = False,
        announce: bool = False,
        now: datetime | None = None,
    ) -> str:
        moment = now if now is not None else discord.utils.utcnow()
        async with self._lock:
            action = due_action(
                moment,
                self.state,
                apply_at=self.apply_at,
                revert_at=self.revert_at,
                manual=kind,
                force=force,
            )
            if action == "none":
                text = block_message(self.state, manual=kind, force=force)
            elif action == "apply":
                result = await self._apply(moment)
                text = result_message("apply", result.status, result.error)
            else:
                result = await self._revert(moment)
                text = result_message("revert", result.status, result.error)
            if kind == "apply" and announce:
                text = await self._append_manual_announce(text, moment)
            return text

    async def _send_preview(self, author, avatar: bytes, bio: str, announcement: str) -> None:
        mentions = discord.AllowedMentions.none()
        costume_file = discord.File(BytesIO(avatar), filename=self.avatar_path.name)
        combined = preview_message(bio, announcement)
        if combined is not None:
            await author.send(combined, file=costume_file, allowed_mentions=mentions)
            return
        await author.send(f"**Bio**\n{bio}", file=costume_file, allowed_mentions=mentions)
        if len(announcement) <= ANNOUNCEMENT_LIMIT:
            await author.send(announcement, allowed_mentions=mentions)
            return
        await author.send(
            (
                f"The announcement is {len(announcement)} characters, "
                f"over the {ANNOUNCEMENT_LIMIT} limit, so I didn't include a cut-off copy."
            ),
            allowed_mentions=mentions,
        )

    @commands.group(name="costume", invoke_without_command=True, hidden=True, case_insensitive=True)
    @_costume_check
    async def costume(self, ctx: commands.Context) -> None:
        """Owner controls for the Halloween bot costume. Hidden from help."""
        if not await _costume_allowed(ctx):
            return
        await ctx.reply(COSTUME_USAGE, mention_author=False)

    @costume.command(name="status", hidden=True)
    @_costume_check
    async def costume_status(self, ctx: commands.Context) -> None:
        """Show the costume schedule, state, assets, and announcement channel."""
        if not await _costume_allowed(ctx):
            return
        _channel, label = self._resolve_announcement_channel()
        text = render_status(
            self.state,
            now=discord.utils.utcnow(),
            apply_at=self.apply_at,
            revert_at=self.revert_at,
            avatar_name=self.avatar_path.name,
            avatar_ready=self.avatar_path.is_file(),
            avatar_note="costume_settings.json" if self.avatar_from_settings else "default",
            bio_ready=self.bio_path.is_file(),
            announcement_ready=self.announcement_path.is_file(),
            backup_ready=self.backup_path.is_file(),
            announcement_channel=label,
            guild_id=self.guild_id,
        )
        await ctx.reply(text, mention_author=False)

    @costume.command(name="preview", hidden=True)
    @_costume_check
    async def costume_preview(self, ctx: commands.Context) -> None:
        """DM the owner the costume avatar, bio, and announcement."""
        if not await _costume_allowed(ctx):
            return
        try:
            bio = load_bio_text(self.bio_path)
            avatar = load_avatar_bytes(self.avatar_path)
            announcement = load_announcement_text(self.announcement_path)
        except Exception as exc:
            await ctx.reply(f"Costume assets aren't ready: {error_text(exc)}", mention_author=False)
            return
        author = getattr(ctx, "author", None)
        send = getattr(author, "send", None)
        if not callable(send):
            await ctx.reply("I couldn't DM you the preview.", mention_author=False)
            return
        try:
            await self._send_preview(author, avatar, bio, announcement)
        except discord.Forbidden:
            await ctx.reply(
                "I couldn't DM you the preview. Check that your DMs are open.",
                mention_author=False,
            )
            return
        except Exception as exc:
            await ctx.reply(f"I couldn't DM you the preview: {error_text(exc)}", mention_author=False)
            return
        await ctx.reply("Sent you a preview in DMs", mention_author=False)

    @costume.command(name="apply", hidden=True)
    @_costume_check
    async def costume_apply(self, ctx: commands.Context, *, spec: str = "") -> None:
        """Apply the costume now. Pass announce to also post the announcement."""
        if not await _costume_allowed(ctx):
            return
        parsed = parse_apply_spec(spec)
        if parsed is None:
            await ctx.reply(COSTUME_USAGE, mention_author=False)
            return
        force, announce = parsed
        message = await self._manual("apply", force=force, announce=announce)
        await ctx.reply(message, mention_author=False)

    @costume.command(name="restore", hidden=True)
    @_costume_check
    async def costume_restore(self, ctx: commands.Context) -> None:
        """Put the snapshotted Menaces to Sobriety profile back now."""
        if not await _costume_allowed(ctx):
            return
        message = await self._manual("revert")
        await ctx.reply(message, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Costume(bot))
