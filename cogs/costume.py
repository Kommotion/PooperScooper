"""Halloween costume for the bot's per-server profile in Menaces to Sobriety.

The guild avatar and bio go on at midnight PT on Halloween and come off at
midnight PT the next day. Each of those Discord edits is attempted once.
Pure helpers at the top are what the tests drive; the cog only talks to Discord.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.asset import Asset
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.constants import DATA_DIR, PROJECT_ROOT
from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)

# Schedule and target. Change these here; nothing else hard-codes the dates.
TIMEZONE_NAME = "America/Los_Angeles"
PT = ZoneInfo(TIMEZONE_NAME)
MTS_GUILD_ID = 932057681307512922
APPLY_AT = datetime(2026, 10, 31, 0, 0, tzinfo=PT)
REVERT_AT = datetime(2026, 11, 1, 0, 0, tzinfo=PT)
LOOP_SECONDS = 60

AVATAR_ASSET = PROJECT_ROOT / "assets" / "halloween" / "avatar.png"
BIO_ASSET = PROJECT_ROOT / "assets" / "halloween" / "bio.txt"
STATE_PATH = DATA_DIR / "costume.json"
BACKUP_AVATAR = DATA_DIR / "costume_backup_avatar.png"

APPLY_REASON = "Halloween costume"
REVERT_REASON = "Restoring profile after Halloween costume"
STATE_KEYS = (
    "applied_at",
    "apply_attempted",
    "apply_error",
    "reverted_at",
    "revert_attempted",
    "revert_error",
    "had_guild_avatar",
    "had_guild_bio",
    "original_bio",
)


def default_state() -> dict:
    return {
        "applied_at": None,
        "apply_attempted": False,
        "apply_error": None,
        "reverted_at": None,
        "revert_attempted": False,
        "revert_error": None,
        "had_guild_avatar": False,
        "had_guild_bio": False,
        "original_bio": None,
    }


def _opt_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _as_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def normalize_state(raw: object) -> dict:
    base = default_state()
    if not isinstance(raw, dict):
        return base
    for key in ("apply_attempted", "revert_attempted", "had_guild_avatar", "had_guild_bio"):
        base[key] = _as_bool(raw.get(key))
    for key in ("applied_at", "apply_error", "reverted_at", "revert_error", "original_bio"):
        base[key] = _opt_str(raw.get(key))
    return base


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return as_utc(moment).isoformat()


def format_pt(moment: datetime) -> str:
    return as_utc(moment).astimezone(PT).strftime("%Y-%m-%d %H:%M PT")


def apply_is_open(state: dict, *, force: bool) -> bool:
    """True when an apply edit is still allowed."""
    if force:
        return True
    state = normalize_state(state)
    if state["apply_attempted"]:
        return False
    # applied_at without the flag still means the costume is already on.
    if state["applied_at"] and not state["reverted_at"]:
        return False
    return True


def revert_is_open(state: dict, *, force: bool) -> bool:
    """True when a revert edit is still allowed."""
    if force:
        return True
    state = normalize_state(state)
    if state["revert_attempted"] or state["reverted_at"]:
        return False
    if not state["applied_at"]:
        return False
    return True


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

    Automatic checks never pass ``force``, so a recorded attempt cannot run twice.
    Manual commands ignore the clock and use ``force`` to retry a recorded attempt.
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
            return "apply" if apply_is_open(state, force=False) else "none"
        return "revert" if revert_is_open(state, force=False) else "none"
    if manual == "apply":
        return "apply" if apply_is_open(state, force=force) else "none"
    return "revert" if revert_is_open(state, force=force) else "none"


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


def record_apply_success(state: dict, now: datetime, *, revert_at: datetime) -> dict:
    updated = normalize_state(state)
    updated["apply_attempted"] = True
    updated["apply_error"] = None
    updated["applied_at"] = iso(now)
    updated["reverted_at"] = None
    updated["revert_error"] = None
    # A manual apply after the window should stay on. The loop would otherwise
    # revert it on the next tick.
    updated["revert_attempted"] = as_utc(now) >= as_utc(revert_at)
    return updated


def record_revert_success(state: dict, now: datetime) -> dict:
    updated = normalize_state(state)
    updated["revert_attempted"] = True
    updated["revert_error"] = None
    updated["reverted_at"] = iso(now)
    return updated


def load_bio_text(path: str | Path) -> str:
    """Read the costume bio. ``utf-8-sig`` drops a leading BOM."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise FileNotFoundError(f"Costume bio is missing: {file_path}") from None
    text = text.strip()
    if not text:
        raise ValueError(f"Costume bio is empty: {file_path}")
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
    avatar_ready: bool,
    bio_ready: bool,
    backup_ready: bool,
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
        f"revert_attempted: {_shown(current['revert_attempted'])}",
        f"reverted_at: {_shown(current['reverted_at'])}",
        f"revert_error: {_shown(current['revert_error'])}",
        f"had_guild_avatar: {_shown(current['had_guild_avatar'])}",
        f"had_guild_bio: {_shown(current['had_guild_bio'])}",
        f"original_bio: {_shown(current['original_bio'])}",
        "",
        "Assets:",
        f"avatar.png: {'present' if avatar_ready else 'missing'}",
        f"bio.txt: {'present' if bio_ready else 'missing'}",
        f"backup avatar: {'present' if backup_ready else 'absent'}",
    ]
    return "\n".join(lines)


def block_message(state: dict, *, manual: str, force: bool) -> str:
    current = normalize_state(state)
    if manual == "apply":
        if current["apply_error"] and not force:
            return "Apply already failed, so I won't try again. Set force to True to retry once."
        if current["applied_at"] and not current["reverted_at"] and not force:
            return "The costume is already applied. Set force to True to run it again."
        if current["apply_attempted"] and not force:
            return "Apply was already attempted. Set force to True to run it again."
    if manual == "revert":
        if not current["applied_at"] and not current["reverted_at"] and not force:
            return (
                "The costume was never applied, so there's nothing to revert. "
                "Set force to True to clear the per-server avatar and bio anyway."
            )
        if current["revert_error"] and not force:
            return "Revert already failed, so I won't try again. Set force to True to retry once."
        if (current["revert_attempted"] or current["reverted_at"]) and not force:
            return "Revert was already attempted. Set force to True to run it again."
    return "Nothing to do."


def result_message(action: str, status: str, error: str | None = None) -> str:
    if status == "applied":
        return "Halloween costume is on in Menaces to Sobriety."
    if status == "reverted":
        return "Per-server profile restored in Menaces to Sobriety."
    if status == "unavailable":
        return "I can't see Menaces to Sobriety right now, so I didn't change the profile."
    if status == "error":
        verb = "Apply" if action == "apply" else "Revert"
        detail = error or "unknown error"
        return (
            f"{verb} failed: {detail} "
            "I won't retry automatically. Use force on the command to try once more."
        )
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


async def _costume_interaction_check(interaction: discord.Interaction) -> bool:
    is_owner = getattr(getattr(interaction, "client", None), "is_owner", None)
    allowed = False
    if callable(is_owner):
        try:
            allowed = await is_owner(getattr(interaction, "user", None))
        except Exception:
            allowed = False
    if allowed:
        return True
    response = getattr(interaction, "response", None)
    is_done = getattr(response, "is_done", None)
    done = bool(is_done()) if callable(is_done) else False
    if response is not None and not done:
        await response.send_message(
            "Only the bot owner can use costume commands.",
            ephemeral=True,
        )
    return False


async def _costume_on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)
        done = bool(is_done()) if callable(is_done) else False
        if response is not None and not done:
            await response.send_message(
                "Only the bot owner can use costume commands.",
                ephemeral=True,
            )
        return
    log.exception("Costume command failed")


class Costume(Cog):
    """Puts the Halloween avatar and bio on the MTS per-server profile, once."""

    costume = app_commands.Group(
        name="costume",
        description="Owner controls for the Halloween bot costume.",
        guild_only=True,
    )
    costume.interaction_check = _costume_interaction_check  # type: ignore[method-assign]
    costume.on_error = _costume_on_error  # type: ignore[method-assign]

    def __init__(
        self,
        bot: commands.Bot,
        *,
        path: str | Path | None = None,
        avatar_path: str | Path | None = None,
        bio_path: str | Path | None = None,
        backup_path: str | Path | None = None,
        guild_id: int | None = None,
        apply_at: datetime | None = None,
        revert_at: datetime | None = None,
    ):
        self.bot = bot
        self.guild_id = MTS_GUILD_ID if guild_id is None else guild_id
        self.apply_at = APPLY_AT if apply_at is None else apply_at
        self.revert_at = REVERT_AT if revert_at is None else revert_at
        self.avatar_path = Path(AVATAR_ASSET if avatar_path is None else avatar_path)
        self.bio_path = Path(BIO_ASSET if bio_path is None else bio_path)
        self.backup_path = Path(BACKUP_AVATAR if backup_path is None else backup_path)
        self.store = CostumeStore(STATE_PATH if path is None else path)
        self.state = self.store.state
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        log.info(
            "Costume loaded guild=%s apply_at=%s revert_at=%s apply_attempted=%s revert_attempted=%s",
            self.guild_id,
            format_pt(self.apply_at),
            format_pt(self.revert_at),
            str(self.state["apply_attempted"]).lower(),
            str(self.state["revert_attempted"]).lower(),
        )
        if not self.costume_loop.is_running():
            self.costume_loop.start()

    async def cog_unload(self) -> None:
        self.costume_loop.cancel()

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

    def _record_failure(self, kind: str, exc: BaseException) -> StepResult:
        message = error_text(exc)
        # Called from an except block so the traceback is still current.
        log.exception("Costume %s failed", kind)
        if kind == "apply":
            self.state["apply_attempted"] = True
            self.state["apply_error"] = message
        else:
            self.state["revert_attempted"] = True
            self.state["revert_error"] = message
        self._save()
        return StepResult("error", message)

    def _mark_attempt(self, kind: str) -> None:
        if kind == "apply":
            self.state["apply_attempted"] = True
            self.state["apply_error"] = None
        else:
            self.state["revert_attempted"] = True
            self.state["revert_error"] = None
        self._save()

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
        try:
            avatar_bytes = load_avatar_bytes(self.avatar_path)
            bio = load_bio_text(self.bio_path)
        except Exception as exc:
            return self._record_failure("apply", exc)

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
            return self._record_failure("apply", exc)

        # Persist the attempt before the profile edit so a crash cannot apply twice.
        self._mark_attempt("apply")
        try:
            await member.edit(avatar=avatar_bytes, bio=bio, reason=APPLY_REASON)
        except Exception as exc:
            return self._record_failure("apply", exc)

        self.state = record_apply_success(self.state, now, revert_at=self.revert_at)
        self._save()
        log.info("Applied Halloween costume in guild %s", self.guild_id)
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
            return self._record_failure("revert", exc)

        self._mark_attempt("revert")
        try:
            await member.edit(avatar=avatar, bio=bio, reason=REVERT_REASON)
        except Exception as exc:
            return self._record_failure("revert", exc)

        self.state = record_revert_success(self.state, now)
        self._save()
        log.info("Reverted Halloween costume in guild %s", self.guild_id)
        return StepResult("reverted")

    async def tick(self, *, now: datetime | None = None) -> str:
        moment = now if now is not None else discord.utils.utcnow()
        async with self._lock:
            action = due_action(
                moment,
                self.state,
                apply_at=self.apply_at,
                revert_at=self.revert_at,
            )
            if action == "apply":
                result = await self._apply(moment)
                return result.status
            if action == "revert":
                result = await self._revert(moment)
                return result.status
            return "none"

    async def _manual(self, kind: str, *, force: bool) -> str:
        now = discord.utils.utcnow()
        async with self._lock:
            action = due_action(
                now,
                self.state,
                apply_at=self.apply_at,
                revert_at=self.revert_at,
                manual=kind,
                force=force,
            )
            if action == "none":
                return block_message(self.state, manual=kind, force=force)
            if action == "apply":
                result = await self._apply(now)
            else:
                result = await self._revert(now)
            return result_message(action, result.status, result.error)

    @costume.command(name="status", description="Show the costume schedule, state, and assets.")
    @app_commands.guild_only()
    async def costume_status(self, interaction: discord.Interaction) -> None:
        text = render_status(
            self.state,
            now=discord.utils.utcnow(),
            apply_at=self.apply_at,
            revert_at=self.revert_at,
            avatar_ready=self.avatar_path.is_file(),
            bio_ready=self.bio_path.is_file(),
            backup_ready=self.backup_path.is_file(),
            guild_id=self.guild_id,
        )
        await interaction.response.send_message(text, ephemeral=True)

    @costume.command(name="preview", description="Preview the costume avatar and bio. Does not change the profile.")
    @app_commands.guild_only()
    async def costume_preview(self, interaction: discord.Interaction) -> None:
        try:
            bio = load_bio_text(self.bio_path)
            avatar = load_avatar_bytes(self.avatar_path)
        except Exception as exc:
            await interaction.response.send_message(
                f"Costume assets aren't ready: {error_text(exc)}",
                ephemeral=True,
            )
            return
        costume_file = discord.File(BytesIO(avatar), filename="halloween_avatar.png")
        await interaction.response.send_message(content=bio, file=costume_file, ephemeral=True)

    @costume.command(name="apply_now", description="Apply the Halloween costume once.")
    @app_commands.describe(force="Try again after a recorded attempt.")
    @app_commands.guild_only()
    async def costume_apply_now(self, interaction: discord.Interaction, force: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        message = await self._manual("apply", force=force)
        await interaction.followup.send(message, ephemeral=True)

    @costume.command(name="revert_now", description="Restore the per-server profile once.")
    @app_commands.describe(force="Try again after a recorded attempt.")
    @app_commands.guild_only()
    async def costume_revert_now(self, interaction: discord.Interaction, force: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        message = await self._manual("revert", force=force)
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Costume(bot))
