"""Interaction ids from /grok, so a later reminder block can be tied to that command.

The JSON file is the source of truth. Reminders and Grok each open it, and every
read reloads from disk. The path is injectable so tests never touch data/.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

GROK_REFS_PATH = DATA_DIR / "grok_refs.json"
REF_TTL = timedelta(hours=24)
BLOCK_FIELD_NAME = "PS-REMIND v1"
_SNOWFLAKE_MAX = (1 << 64) - 1
_FENCE_RE = re.compile(
    r"^\s*```(?:[ \t]*json)?[ \t]*\r?\n?(.*?)\r?\n?```\s*$",
    re.IGNORECASE | re.DOTALL,
)


class BlockError(Exception):
    """A PS-REMIND v1 field was present but not a usable reminder block."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RemindBlock:
    ref: str
    creator_id: int
    guild_id: int | None
    channel_id: int
    when_utc: datetime | None
    repeat: str | None
    private: bool
    text: str
    mention_ids: tuple[int, ...]
    exposed_private_text: bool


@dataclass
class GrokRef:
    user_id: int
    channel_id: int
    guild_id: int | None
    created_at: datetime
    consumed: bool = False


def parse_remind_block(embeds) -> RemindBlock | None:
    """Return the PS-REMIND v1 block, or None when that field is absent.

    Raises BlockError with a short reason when the field is present but malformed.
    """
    field = _find_block_field(embeds)
    if field is None:
        return None
    value = getattr(field, "value", None)
    if not isinstance(value, str):
        raise BlockError("bad JSON")
    try:
        data = json.loads(_strip_fence(value))
    except json.JSONDecodeError as exc:
        raise BlockError("bad JSON") from exc
    if not isinstance(data, dict):
        raise BlockError("bad JSON")
    for key in ("ref", "creator_id", "guild_id", "channel_id"):
        if key not in data:
            raise BlockError("missing keys")
    repeat = _optional_repeat(data)
    if repeat is None and "when_utc" not in data:
        raise BlockError("missing keys")
    ref = str(_parse_snowflake(data.get("ref"), reason="bad id"))
    creator_id = _parse_snowflake(data.get("creator_id"), reason="bad id")
    guild_raw = data.get("guild_id")
    guild_id = None if guild_raw is None else _parse_snowflake(guild_raw, reason="bad id")
    channel_id = _parse_snowflake(data.get("channel_id"), reason="bad id")
    when_utc = None if repeat is not None else _parse_when_utc(data.get("when_utc"))
    private = data.get("private", False)
    if not isinstance(private, bool):
        raise BlockError("bad private")
    text, exposed = _parse_text(data, private=private)
    mention_ids = _parse_mentions(data)
    return RemindBlock(
        ref=ref,
        creator_id=creator_id,
        guild_id=guild_id,
        channel_id=channel_id,
        when_utc=when_utc,
        repeat=repeat,
        private=private,
        text=text,
        mention_ids=mention_ids,
        exposed_private_text=exposed,
    )


class GrokRefs:
    """JSON map of interaction id -> ref. Consumed refs stay until they expire."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.refs: dict[str, GrokRef] = {}
        self.load()

    def load(self, *, now: datetime | None = None) -> None:
        self._read()
        removed = self._drop_expired(_as_utc(now or datetime.now(timezone.utc)))
        if removed:
            try:
                self.save()
            except OSError as exc:
                log.warning("Couldn't prune Grok refs in %s: %s", self.path, exc)

    def record(
        self,
        interaction_id: str,
        *,
        user_id: int,
        channel_id: int,
        guild_id: int | None,
        created_at: datetime,
        now: datetime | None = None,
    ) -> None:
        """Store a new ref. A second record for the same id does not reset consumed."""
        self._read()
        moment = _as_utc(now or datetime.now(timezone.utc))
        self._drop_expired(moment)
        created = _as_utc(created_at)
        key = str(interaction_id)
        if _age_expired(created, moment):
            self.save()
            return
        if key not in self.refs:
            self.refs[key] = GrokRef(
                user_id=int(user_id),
                channel_id=int(channel_id),
                guild_id=None if guild_id is None else int(guild_id),
                created_at=created,
                consumed=False,
            )
        self.save()

    def prepare(
        self,
        interaction_id: str,
        *,
        user_id: int,
        channel_id: int,
        now: datetime,
    ) -> str:
        """Classify a ref without consuming it.

        Returns ready, missing, expired, consumed, user mismatch, or channel mismatch.
        """
        self._read()
        moment = _as_utc(now)
        ref = self.refs.get(str(interaction_id))
        if ref is None:
            return "missing"
        if _is_expired(ref, moment):
            self._drop_expired(moment)
            self.save()
            return "expired"
        if ref.consumed:
            return "consumed"
        if ref.user_id != int(user_id):
            return "user mismatch"
        if ref.channel_id != int(channel_id):
            return "channel mismatch"
        return "ready"

    def mark_consumed(self, interaction_id: str, *, now: datetime) -> bool:
        """Persist consumed=True. False when the ref is missing, expired, or already used."""
        self._read()
        moment = _as_utc(now)
        ref = self.refs.get(str(interaction_id))
        if ref is None or ref.consumed or _is_expired(ref, moment):
            return False
        ref.consumed = True
        self._drop_expired(moment)
        self.save()
        return True

    def unconsume(self, interaction_id: str) -> None:
        """Undo mark_consumed when creating the reminder failed."""
        self._read()
        ref = self.refs.get(str(interaction_id))
        if ref is None or not ref.consumed:
            return
        ref.consumed = False
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "refs": {
                key: {
                    "user_id": ref.user_id,
                    "channel_id": ref.channel_id,
                    "guild_id": ref.guild_id,
                    "created_at": _as_utc(ref.created_at).isoformat(),
                    "consumed": bool(ref.consumed),
                }
                for key, ref in self.refs.items()
            }
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _read(self) -> None:
        self.refs = {}
        if not self.path.is_file():
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Grok ref file %s is corrupt (%s); starting empty.", self.path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("Grok ref file %s is corrupt; starting empty.", self.path)
            return
        body = raw.get("refs", {})
        if not isinstance(body, dict):
            log.warning("Grok ref list in %s is corrupt; starting empty.", self.path)
            return
        for key, value in body.items():
            try:
                self.refs[str(key)] = _ref_from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping grok ref %s in %s: %s", key, self.path, exc)

    def _drop_expired(self, now: datetime) -> int:
        expired = [key for key, ref in self.refs.items() if _is_expired(ref, now)]
        for key in expired:
            del self.refs[key]
        return len(expired)


def _find_block_field(embeds):
    if not embeds:
        return None
    for embed in embeds:
        fields = getattr(embed, "fields", None) or []
        for field in fields:
            if getattr(field, "name", None) == BLOCK_FIELD_NAME:
                return field
    return None


def _strip_fence(value: str) -> str:
    text = value.strip()
    match = _FENCE_RE.fullmatch(text)
    if match:
        return match.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, count=1, flags=re.IGNORECASE).strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def _optional_repeat(data: dict) -> str | None:
    if "repeat" not in data or data["repeat"] is None:
        return None
    repeat = data["repeat"]
    if not isinstance(repeat, str):
        raise BlockError("bad time")
    cleaned = " ".join(repeat.split())
    if not cleaned:
        return None
    return cleaned


def _parse_when_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise BlockError("bad time")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BlockError("bad time") from exc
    if parsed.tzinfo is None:
        raise BlockError("bad time")
    return _as_utc(parsed)


def _parse_text(data: dict, *, private: bool) -> tuple[str, bool]:
    has_text = "text" in data and data["text"] is not None
    has_b64 = "text_b64" in data and data["text_b64"] is not None
    if has_text and has_b64:
        raise BlockError("both text fields")
    if not has_text and not has_b64:
        raise BlockError("neither text field")
    if has_b64:
        encoded = data["text_b64"]
        if not isinstance(encoded, str) or not encoded.strip():
            raise BlockError("bad text_b64")
        return _decode_b64url(encoded.strip()), False
    text = data["text"]
    if not isinstance(text, str):
        raise BlockError("bad text")
    return text, private


def _decode_b64url(value: str) -> str:
    padded = value + ("=" * (-len(value) % 4))
    try:
        # validate=True rejects characters outside the alphabet. The default
        # decoder drops them and would accept "!!!". altchars keeps base64url.
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except Exception as exc:
        raise BlockError("bad text_b64") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlockError("bad text_b64") from exc


def _parse_mentions(data: dict) -> tuple[int, ...]:
    if "mention_ids" not in data or data["mention_ids"] is None:
        return ()
    values = data["mention_ids"]
    if not isinstance(values, list):
        raise BlockError("bad mention_ids")
    return tuple(_parse_snowflake(item, reason="bad id") for item in values)


def _parse_snowflake(value: object, *, reason: str) -> int:
    if isinstance(value, bool):
        raise BlockError(reason)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        raise BlockError(reason)
    if number < 1 or number > _SNOWFLAKE_MAX:
        raise BlockError(reason)
    return number


def _ref_from_dict(value: object) -> GrokRef:
    if not isinstance(value, dict):
        raise ValueError("ref must be an object")
    return GrokRef(
        user_id=int(value["user_id"]),
        channel_id=int(value["channel_id"]),
        guild_id=None if value.get("guild_id") is None else int(value["guild_id"]),
        created_at=_stored_time(value["created_at"]),
        consumed=value.get("consumed") is True,
    )


def _stored_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_at")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _as_utc(parsed)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _is_expired(ref: GrokRef, now: datetime) -> bool:
    return _age_expired(ref.created_at, now)


def _age_expired(created_at: datetime, now: datetime) -> bool:
    return _as_utc(now) - _as_utc(created_at) >= REF_TTL
