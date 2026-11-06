"""Malfoyd of the day.

An external job writes ``data/malfoyd_queue.json``. Once a minute this cog
posts today's pick into the Malfoyd thread when that file is for today and
has not been posted yet. A normal vx preview is plain text plus the
vxtwitter link so Discord unfurls it. No embeds.

The preview is the HTML Discord's crawler would get. An explicit unavailable
page with no media is a confirmed failure: the public vx/Fx API supplies the
image or MP4, and those files are uploaded with the original link in angle
brackets. HTTP errors and timeouts are not failures. The candidate list is
not rewritten. ``index`` stays the valid-candidate high-water mark so the
next-day final-index dedupe still covers everything up through the posted pick.

``index`` counts valid candidates only. A candidate is valid when ``vx_url``
is a non-empty string starting with ``https://vxtwitter.com/``. Invalid
entries are skipped and do not take a slot. Extra JSON fields are kept.

Notify toggles the Malfoyd Ping role. Reroll moves to the next valid
candidate. The whole server shares five rerolls a day. The bot owner can
reroll without using one. An automatic skip does not use a reroll. A
requested reroll uses one only after the replacement is posted.
``!malfoyd_setup`` and ``!malfoyd_post`` are owner-only prefix commands;
anyone else gets no reply.

Older posts may still include a Refresh button whose custom id is
``malfoyd:refresh``. That button is not on new posts. A listener replies
that Refresh is no longer available and does not change the queue.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from cogs.malfoyd_media import MAX_CANDIDATE_ATTEMPTS, MAX_REQUESTS, OPERATION_SECONDS, Budget, MediaClient, PreviewStatus
from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

GUILD_ID = 932057681307512922
THREAD_ID = 1565459865134960811
ROLE_NAME = "Malfoyd Ping"
QUEUE_PATH = DATA_DIR / "malfoyd_queue.json"
SETTINGS_PATH = DATA_DIR / "malfoyd_settings.json"
MAX_REROLLS_PER_DAY = 5
POLL_SECONDS = 60
TZ = ZoneInfo("America/Los_Angeles")

VX_PREFIX = "https://vxtwitter.com/"
HEADING = "**Malfoyd of the day** 🐍"
NOTIFY_CUSTOM_ID = "malfoyd:notify"
REROLL_CUSTOM_ID = "malfoyd:reroll"
# Older messages still send this custom_id. New posts do not include the button.
REFRESH_CUSTOM_ID = "malfoyd:refresh"
RETIRED_REFRESH_MESSAGE = "Refresh is no longer available."

_missing_logged: set[str] = set()


def today_pt(now: datetime | None = None) -> str:
    """YYYY-MM-DD in America/Los_Angeles. Naive datetimes are read as Pacific."""
    moment = datetime.now(TZ) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    return moment.astimezone(TZ).date().isoformat()


def default_queue() -> dict:
    return {
        "date": None,
        "posted": False,
        "message_id": None,
        "index": 0,
        "rerolls_used": 0,
        "candidates": [],
    }


def default_settings() -> dict:
    return {}


@dataclass(frozen=True)
class JsonRead:
    data: dict
    status: str


def read_json(path: str | Path, default_factory: Callable[[], dict]) -> JsonRead:
    """Load a JSON object. BOM is accepted. Missing and corrupt files do not raise."""
    target = Path(path)
    if not target.is_file():
        key = str(target)
        if key not in _missing_logged:
            _missing_logged.add(key)
            log.warning("Malfoyd file %s is missing; using defaults.", target)
        return JsonRead(default_factory(), "missing")
    try:
        parsed = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        log.error("Malfoyd file %s is corrupt: %s", target, exc)
        return JsonRead(default_factory(), "corrupt")
    if not isinstance(parsed, dict):
        log.error("Malfoyd file %s is corrupt: expected an object.", target)
        return JsonRead(default_factory(), "corrupt")
    return JsonRead(parsed, "ok")


def write_json(path: str | Path, data: dict) -> None:
    """Atomic UTF-8 write with no BOM. A temp file is replaced into place."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if not payload.endswith("\n"):
        payload += "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def is_valid_candidate(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    url = candidate.get("vx_url")
    return isinstance(url, str) and url.startswith(VX_PREFIX)


def valid_candidates(candidates: object) -> list[dict]:
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if is_valid_candidate(item)]


def _non_negative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _candidate_at(queue: dict, index: int) -> dict | None:
    chosen = valid_candidates(queue.get("candidates"))
    if index < 0 or index >= len(chosen):
        return None
    return chosen[index]


def current_candidate(queue: dict) -> dict | None:
    if not isinstance(queue, dict):
        return None
    return _candidate_at(queue, _non_negative_int(queue.get("index")))


def _settings_role_id(settings: object) -> int | None:
    if not isinstance(settings, dict) or "role_id" not in settings:
        return None
    value = settings.get("role_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def find_role(guild: object, settings: object) -> object | None:
    """Role from settings.role_id, otherwise the first role named Malfoyd Ping."""
    if guild is None:
        return None
    role_id = _settings_role_id(settings)
    if role_id is not None:
        getter = getattr(guild, "get_role", None)
        role = getter(role_id) if callable(getter) else None
        if role is not None:
            return role
    roles = getattr(guild, "roles", None) or []
    for role in roles:
        if getattr(role, "name", None) == ROLE_NAME:
            return role
    return None


def _role_id(role: object) -> int | None:
    value = getattr(role, "id", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_content(candidate: dict, role_id: int | None, mention: bool, *, embed: bool = True) -> str:
    """Plain-text Malfoyd post. The role mention is only the first line when requested.

    ``embed=False`` wraps the vx link in angle brackets so Discord does not
    attach a second, broken preview under an uploaded file.
    """
    url = candidate["vx_url"]
    if not embed:
        url = _suppress_embed(url)
    lines: list[str] = []
    if mention and role_id is not None:
        lines.append(f"<@&{role_id}>")
    lines.append(HEADING)
    lines.append(url)
    return "\n".join(lines)


def _suppress_embed(url: str) -> str:
    if any(char in url for char in "<> \n\r\t"):
        return url
    return f"<{url}>"


def _same_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _as_int_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _rerolls_remaining(queue: dict) -> int:
    return MAX_REROLLS_PER_DAY - _non_negative_int(queue.get("rerolls_used"))


def _positive_limit(guild: object) -> int | None:
    """Destination guild.filesize_limit, or None when that limit is unknown."""
    if guild is None:
        return None
    try:
        value = getattr(guild, "filesize_limit")
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _upload_rejected(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if status == 413 or code == 40005:
        return True
    text = str(exc).lower()
    return "entity too large" in text or "payload too large" in text


@dataclass
class _Delivered:
    message: object | None
    problem: str | None


@dataclass
class _Walk:
    message: object | None
    problem: str | None
    index: int
    detail: str


def _mentions_for(role: object | None) -> discord.AllowedMentions:
    if role is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(everyone=False, users=False, roles=[role])


def _thread_error(problem: str | None) -> str:
    if problem == "unarchive":
        return "Couldn't unarchive the Malfoyd thread."
    return "Couldn't find the Malfoyd thread."


def _member_has_role(member: object, role: object) -> bool:
    role_id = _role_id(role)
    for item in getattr(member, "roles", None) or []:
        if item is role or _role_id(item) == role_id:
            return True
    return False


def _malfoyd_cog(interaction: discord.Interaction) -> Malfoyd | None:
    client = getattr(interaction, "client", None)
    getter = getattr(client, "get_cog", None)
    if not callable(getter):
        return None
    cog = getter("Malfoyd")
    return cog if isinstance(cog, Malfoyd) else None


def _component_custom_id(interaction: object) -> str | None:
    data = getattr(interaction, "data", None)
    if isinstance(data, dict):
        custom_id = data.get("custom_id")
    else:
        custom_id = getattr(data, "custom_id", None)
    if isinstance(custom_id, str):
        return custom_id
    return None


class MalfoydView(discord.ui.View):
    """Persistent notify and reroll buttons. Callbacks read the queue, not the label."""

    def __init__(self, remaining: int):
        super().__init__(timeout=None)
        left = remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else 0
        self.reroll.label = f"Reroll ({left} left)"
        self.reroll.disabled = left <= 0

    @discord.ui.button(label="Notify me", emoji="🔔", custom_id=NOTIFY_CUSTOM_ID)
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        cog = _malfoyd_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Malfoyd isn't available right now.", ephemeral=True)
            return
        await cog.handle_notify(interaction)

    @discord.ui.button(
        label=f"Reroll ({MAX_REROLLS_PER_DAY} left)",
        emoji="🎲",
        custom_id=REROLL_CUSTOM_ID,
    )
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        cog = _malfoyd_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Malfoyd isn't available right now.", ephemeral=True)
            return
        await cog.handle_reroll(interaction)


def make_view(remaining: int) -> MalfoydView:
    return MalfoydView(remaining)


def _command_is_malfoyd(command: object) -> bool:
    return getattr(command, "name", None) in {"malfoyd_post", "malfoyd_setup"}


class Malfoyd(commands.Cog):
    """Posts the daily Malfoyd pick and handles notify and reroll."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        queue_path: str | Path | None = None,
        settings_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        media: object | None = None,
    ):
        self.bot = bot
        self.queue_path = Path(QUEUE_PATH if queue_path is None else queue_path)
        self.settings_path = Path(SETTINGS_PATH if settings_path is None else settings_path)
        self._clock = clock
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._media = media if media is not None else MediaClient()
        self._unpersisted_mention_date: str | None = None
        self._lock = asyncio.Lock()

    def _today(self) -> str:
        if self._clock is None:
            return today_pt()
        return today_pt(self._clock())

    def _guild(self, source: object | None = None):
        if source is not None:
            guild = getattr(source, "guild", None)
            if getattr(guild, "id", None) == GUILD_ID:
                return guild
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter):
            return getter(GUILD_ID)
        return None

    def _settings(self) -> dict:
        loaded = read_json(self.settings_path, default_settings)
        if loaded.status != "ok":
            return {}
        return loaded.data

    async def _user_is_owner(self, user: object) -> bool:
        if user is None:
            return False
        checker = getattr(self.bot, "is_owner", None)
        if not callable(checker):
            return False
        try:
            result = checker(user)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            log.exception("Malfoyd owner check failed")
            return False

    async def _ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd reply failed")

    async def cog_load(self) -> None:
        # Notify and reroll only. Every child has a custom_id and timeout is
        # None, which is what add_view requires. Old refresh clicks are below.
        self.bot.add_view(make_view(MAX_REROLLS_PER_DAY))
        if not self.poll_loop.is_running():
            self.poll_loop.start()
        log.info("Malfoyd loaded guild=%s thread=%s", GUILD_ID, THREAD_ID)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Answer Refresh buttons left on posts from before the button was removed."""
        try:
            if _component_custom_id(interaction) != REFRESH_CUSTOM_ID:
                return
            response = getattr(interaction, "response", None)
            done = getattr(response, "is_done", None)
            if callable(done) and done():
                return
            await self._ephemeral(interaction, RETIRED_REFRESH_MESSAGE)
        except Exception:
            log.exception("Malfoyd retired refresh reply failed")

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # CheckFailure on these commands is someone who is not the owner.
        # Swallow it so Discord and the error logger stay quiet.
        if not _command_is_malfoyd(getattr(ctx, "command", None)):
            log.error("Malfoyd command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        log.error("Malfoyd command failed", exc_info=error)

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_loop(self) -> None:
        try:
            await self.poll_once()
        except Exception:
            log.exception("Malfoyd poll failed")

    @poll_loop.before_loop
    async def before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def poll_once(self) -> str | None:
        async with self._lock:
            return await self._post_locked(force=False)

    async def _open_thread(self) -> tuple[object | None, str | None]:
        thread = None
        guild = self._guild()
        if guild is not None:
            getter = getattr(guild, "get_thread", None)
            if callable(getter):
                thread = getter(THREAD_ID)
        if thread is None:
            getter = getattr(self.bot, "get_channel", None)
            if callable(getter):
                thread = getter(THREAD_ID)
        if thread is None:
            fetcher = getattr(self.bot, "fetch_channel", None)
            if callable(fetcher):
                try:
                    thread = await fetcher(THREAD_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.error("Malfoyd thread %s could not be fetched: %s", THREAD_ID, exc)
                    return None, "missing"
        if thread is None:
            log.error("Malfoyd thread %s was not found.", THREAD_ID)
            return None, "missing"
        if getattr(thread, "archived", False):
            try:
                await thread.edit(archived=False)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to unarchive Malfoyd thread %s", THREAD_ID)
                return None, "unarchive"
        return thread, None

    def _destination_limit(self, thread: object) -> int | None:
        guild = getattr(thread, "guild", None)
        if guild is not None:
            return _positive_limit(guild)
        return _positive_limit(self._guild())

    async def _send_prepared(self, thread, content: str, files: list[discord.File] | None, role, view_remaining: int):
        kwargs: dict = {
            "content": content,
            "view": make_view(view_remaining),
            "allowed_mentions": _mentions_for(role),
        }
        if files:
            kwargs["files"] = files
        try:
            message = await thread.send(**kwargs)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Malfoyd post to thread %s failed", THREAD_ID)
            if _upload_rejected(exc):
                return _Delivered(None, "too-large")
            return _Delivered(None, "failed")
        return _Delivered(message, None)

    async def _edit_prepared(self, interaction, content: str, files: list[discord.File] | None, view_remaining: int):
        kwargs: dict = {
            "content": content,
            "view": make_view(view_remaining),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if files:
            kwargs["attachments"] = files
        try:
            await interaction.response.edit_message(**kwargs)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Malfoyd reroll edit failed")
            if _upload_rejected(exc):
                return _Delivered(None, "too-large")
            return _Delivered(None, "failed")
        message = getattr(interaction, "message", None)
        if getattr(message, "id", None) is None:
            return _Delivered(None, "failed")
        return _Delivered(message, None)

    async def _walk(
        self,
        queue: dict,
        *,
        start: int,
        mention: bool,
        role_id: int | None,
        view_remaining: int,
        limit_of: Callable[[], int | None],
        deliver: Callable[[str, list[discord.File] | None], Awaitable[_Delivered]],
    ) -> _Walk:
        """Try candidates from ``start`` forward. One attempt each. No wrap.

        State is not written here. A normal preview posts the bare link.
        A confirmed failure uploads files or moves on. Unknown stops the walk.
        """
        chosen = valid_candidates(queue.get("candidates"))
        if start < 0 or start >= len(chosen):
            return _Walk(None, "empty", start, "")
        budget = Budget(self._monotonic, requests=MAX_REQUESTS, seconds=OPERATION_SECONDS)
        tried = 0
        index = start
        saw_unusable = False
        while index < len(chosen) and tried < MAX_CANDIDATE_ATTEMPTS:
            if budget.expired():
                # A deadline is not a confirmed failure. Leave the queue alone
                # so this candidate is checked again instead of being burned.
                log.warning("Malfoyd preview unknown: timed out")
                return _Walk(None, "unknown", index, "timed out")
            candidate = chosen[index]
            tried += 1
            inspection = await self._media.inspect(candidate["vx_url"], budget)
            if inspection.status is PreviewStatus.UNKNOWN:
                log.warning("Malfoyd preview unknown: %s", inspection.detail)
                return _Walk(None, "unknown", index, inspection.detail)
            if inspection.status is PreviewStatus.OK:
                delivered = await deliver(build_content(candidate, role_id, mention), None)
                if delivered.problem == "too-large":
                    log.info("Malfoyd skipped unusable candidate index=%s reason=upload rejected", index)
                    saw_unusable = True
                    index += 1
                    continue
                if delivered.problem:
                    return _Walk(None, "deliver", index, delivered.problem)
                return _Walk(delivered.message, None, index, "")
            limit = limit_of()
            if limit is None:
                log.warning("Malfoyd upload limit is unknown; skipped index=%s", index)
                saw_unusable = True
                index += 1
                continue
            delivered = await self._deliver_attachments(
                candidate,
                limit,
                budget,
                mention,
                role_id,
                deliver,
            )
            if delivered is None:
                log.info("Malfoyd skipped unusable candidate index=%s reason=attachment", index)
                saw_unusable = True
                index += 1
                continue
            if delivered.problem == "too-large":
                log.info("Malfoyd upload rejected index=%s", index)
                saw_unusable = True
                index += 1
                continue
            if delivered.problem:
                return _Walk(None, "deliver", index, delivered.problem)
            return _Walk(delivered.message, None, index, "")
        if index >= len(chosen) and saw_unusable:
            log.warning("Malfoyd found no usable pick")
            return _Walk(None, "exhausted", index, "")
        log.warning("Malfoyd stopped without a usable pick")
        return _Walk(None, "stopped", index, "")

    async def _deliver_attachments(
        self,
        candidate: dict,
        limit: int,
        budget: Budget,
        mention: bool,
        role_id: int | None,
        deliver: Callable[[str, list[discord.File] | None], Awaitable[_Delivered]],
    ) -> _Delivered | None:
        """Download into a temp dir, upload, then delete the dir.

        None means this candidate cannot be posted and the walk should move on.
        The directory is removed on success, failure, and cancellation.
        """
        directory = tempfile.TemporaryDirectory(prefix="malfoyd-")
        files: list[discord.File] = []
        try:
            downloaded = await self._media.download(candidate, limit, directory.name, budget)
            if downloaded.reason or not downloaded.files:
                reason = downloaded.reason or "no media"
                log.info("Malfoyd attachment unavailable reason=%s", reason)
                return None
            try:
                for item in downloaded.files:
                    files.append(discord.File(item.path, filename=item.filename))
            except OSError:
                log.exception("Malfoyd attachment could not be opened")
                return None
            content = build_content(candidate, role_id, mention, embed=False)
            return await deliver(content, files)
        except asyncio.CancelledError:
            raise
        finally:
            for handle in files:
                try:
                    handle.close()
                except Exception:
                    log.exception("Malfoyd attachment close failed")
            try:
                directory.cleanup()
            except Exception:
                log.exception("Malfoyd temp cleanup failed")

    def _walk_message(self, outcome: _Walk) -> str | None:
        if outcome.problem == "unknown":
            detail = outcome.detail or "unknown"
            return f"Couldn't check that preview ({detail}). Nothing was changed."
        if outcome.problem == "exhausted":
            return "No usable picks left in the queue."
        if outcome.problem == "stopped":
            return "Couldn't find a usable pick to post. Nothing was changed."
        return None

    def _save_queue(self, queue: dict) -> bool:
        try:
            write_json(self.queue_path, queue)
        except OSError:
            log.exception("Malfoyd queue save failed")
            return False
        return True

    async def _post_locked(self, *, force: bool) -> str | None:
        loaded = read_json(self.queue_path, default_queue)
        if loaded.status == "corrupt":
            return "The queue file couldn't be read." if force else None
        if loaded.status == "missing":
            return "No valid pick in the queue." if force else None
        queue = loaded.data
        if queue.get("date") != self._today():
            return "The queue is not for today." if force else None
        if not force and queue.get("posted") is True:
            return None
        index = _non_negative_int(queue.get("index"))
        if current_candidate(queue) is None:
            return "No valid pick in the queue." if force else None
        thread, problem = await self._open_thread()
        if thread is None:
            if not force:
                return None
            return _thread_error(problem)
        role = find_role(self._guild(), self._settings())
        role_id = _role_id(role)
        mention = role is not None and self._unpersisted_mention_date != self._today()

        async def deliver(content: str, files: list[discord.File] | None) -> _Delivered:
            return await self._send_prepared(thread, content, files, role if mention else None, _rerolls_remaining(queue))

        outcome = await self._walk(
            queue,
            start=index,
            mention=mention,
            role_id=role_id,
            view_remaining=_rerolls_remaining(queue),
            limit_of=lambda: self._destination_limit(thread),
            deliver=deliver,
        )
        if outcome.message is None:
            text = self._walk_message(outcome)
            if text is not None:
                log.warning("Malfoyd %s", text)
                return text if force else None
            return "Couldn't post Malfoyd of the day." if force else None
        previous = (queue.get("posted"), queue.get("message_id"), queue.get("index"))
        queue["posted"] = True
        queue["message_id"] = int(outcome.message.id)
        # Valid-candidate high-water mark. Skipped picks stay at lower indexes
        # so a next-day dedupe of everything through this index still sees them.
        queue["index"] = outcome.index
        if not self._save_queue(queue):
            queue["posted"], queue["message_id"], queue["index"] = previous
            if mention:
                self._unpersisted_mention_date = self._today()
            removed = await self._delete_posted_message(thread, outcome.message.id)
            if removed is not None:
                log.error("Malfoyd could not remove the unrecorded post %s", outcome.message.id)
            return "Couldn't update that pick." if force else None
        log.info("Posted Malfoyd of the day message_id=%s", outcome.message.id)
        return f"Posted Malfoyd of the day ({outcome.message.id})."

    async def handle_notify(self, interaction: discord.Interaction) -> None:
        guild = self._guild(interaction)
        role = find_role(guild, self._settings())
        if guild is None or role is None:
            await self._ephemeral(interaction, "Signups aren't set up yet.")
            return
        member = await self._member_for(guild, interaction.user)
        if member is None:
            log.error("Malfoyd notify could not find member %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        removing = _member_has_role(member, role)
        try:
            if removing:
                await member.remove_roles(role, reason="Malfoyd notify off")
            else:
                await member.add_roles(role, reason="Malfoyd notify on")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd notify failed for user %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        if removing:
            await self._ephemeral(interaction, "You won't be pinged anymore.")
        else:
            await self._ephemeral(interaction, "You'll be pinged for Malfoyd of the day 🐍")

    async def _member_for(self, guild: object, user: object):
        user_guild = getattr(getattr(user, "guild", None), "id", None)
        if user_guild == GUILD_ID and callable(getattr(user, "add_roles", None)):
            return user
        user_id = getattr(user, "id", None)
        getter = getattr(guild, "get_member", None)
        member = getter(user_id) if callable(getter) else None
        if member is not None:
            return member
        fetcher = getattr(guild, "fetch_member", None)
        if not callable(fetcher) or user_id is None:
            return None
        try:
            return await fetcher(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd could not fetch member %s", user_id)
            return None

    async def handle_reroll(self, interaction: discord.Interaction) -> None:
        # Held across the owner lookup. That await would otherwise let two
        # clicks read the same rerolls_used and both write it back.
        async with self._lock:
            await self._reroll_locked(interaction)

    async def _reroll_locked(self, interaction: discord.Interaction) -> None:
        loaded = read_json(self.queue_path, default_queue)
        queue = loaded.data
        message = getattr(interaction, "message", None)
        message_id = getattr(message, "id", None)
        if (
            loaded.status != "ok"
            or not _same_id(message_id, queue.get("message_id"))
            or queue.get("date") != self._today()
        ):
            await self._ephemeral(interaction, "That pick has expired.")
            return

        owner = await self._user_is_owner(getattr(interaction, "user", None))
        index = _non_negative_int(queue.get("index"))
        used = _non_negative_int(queue.get("rerolls_used"))
        if not owner and used >= MAX_REROLLS_PER_DAY:
            await self._ephemeral(interaction, "No rerolls left today")
            return
        if _candidate_at(queue, index + 1) is None:
            await self._ephemeral(interaction, "No more picks today")
            return

        previous_index = queue.get("index", 0)
        previous_used = queue.get("rerolls_used", 0)
        previous = current_candidate(queue)
        old_remaining = _rerolls_remaining(queue)
        view_remaining = old_remaining if owner else max(0, old_remaining - 1)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            guild = self._guild()

        async def deliver(content: str, files: list[discord.File] | None) -> _Delivered:
            return await self._edit_prepared(interaction, content, files, view_remaining)

        outcome = await self._walk(
            queue,
            start=index + 1,
            mention=False,
            role_id=None,
            view_remaining=view_remaining,
            limit_of=lambda: _positive_limit(guild),
            deliver=deliver,
        )
        if outcome.message is None:
            text = self._walk_message(outcome) or "Couldn't update that pick."
            log.warning("Malfoyd %s", text)
            await self._tell(interaction, text)
            return

        queue["index"] = outcome.index
        if not owner:
            queue["rerolls_used"] = used + 1
        if not self._save_queue(queue):
            queue["index"] = previous_index
            queue["rerolls_used"] = previous_used
            if previous is not None:
                await self._restore_reroll(interaction, build_content(previous, None, False), old_remaining)
            else:
                log.error("Malfoyd reroll changed the post but the queue was not saved")
            await self._tell(interaction, "Couldn't update that pick.")
            return
        log.info("Rerolled Malfoyd of the day index=%s", outcome.index)

    async def _followup(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd reply failed")

    async def _restore_reroll(self, interaction, content: str, view_remaining: int) -> None:
        message = getattr(interaction, "message", None)
        editor = getattr(message, "edit", None)
        if not callable(editor):
            log.error("Malfoyd reroll changed the post but the queue was not saved")
            return
        try:
            await editor(
                content=content,
                view=make_view(view_remaining),
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd reroll changed the post but the queue was not saved")

    async def _tell(self, interaction: discord.Interaction, content: str) -> None:
        response = getattr(interaction, "response", None)
        done = getattr(response, "is_done", None)
        if callable(done) and done():
            await self._followup(interaction, content)
            return
        await self._ephemeral(interaction, content)

    async def _delete_posted_message(self, thread, message_id: object) -> str | None:
        target = _as_int_id(message_id)
        if target is None:
            return "Couldn't remove that post."
        try:
            message = await thread.fetch_message(target)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd could not fetch message %s", target)
            return "Couldn't remove that post."
        try:
            await message.delete()
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd could not delete message %s", target)
            return "Couldn't remove that post."
        return None

    @commands.command(name="malfoyd_post", hidden=True)
    @commands.is_owner()
    async def malfoyd_post(self, ctx: commands.Context) -> None:
        """Owner-only. Post the current Malfoyd queue now."""
        if not await self._user_is_owner(getattr(ctx, "author", None)):
            return
        async with self._lock:
            result = await self._post_locked(force=True)
        await ctx.reply(result or "Couldn't post Malfoyd of the day.", mention_author=False)

    @commands.command(name="malfoyd_setup", hidden=True)
    @commands.is_owner()
    async def malfoyd_setup(self, ctx: commands.Context) -> None:
        """Owner-only. Get or create the Malfoyd Ping role and save its id."""
        if not await self._user_is_owner(getattr(ctx, "author", None)):
            return
        guild = self._guild(ctx)
        if guild is None:
            fetcher = getattr(self.bot, "fetch_guild", None)
            if callable(fetcher):
                try:
                    guild = await fetcher(GUILD_ID)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Malfoyd setup could not fetch guild %s", GUILD_ID)
                    guild = None
        if guild is None:
            await ctx.reply("Couldn't find the guild.", mention_author=False)
            return
        role = find_role(guild, self._settings())
        if role is None:
            try:
                role = await guild.create_role(
                    name=ROLE_NAME,
                    mentionable=True,
                    permissions=discord.Permissions.none(),
                    reason="Malfoyd of the day signups",
                )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Malfoyd setup could not create the role")
                await ctx.reply("Couldn't create the Malfoyd Ping role.", mention_author=False)
                return
        loaded = read_json(self.settings_path, default_settings)
        data = dict(loaded.data) if loaded.status == "ok" else {}
        data["role_id"] = int(role.id)
        write_json(self.settings_path, data)
        await ctx.reply(str(role.id), mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Malfoyd(bot))
