"""Persisted turkey-hunt state.

The schedule is written once. Later ticks never add slots. A drop that is due
and still inside its original lifetime is posted; a drop whose lifetime has
already ended is marked missed. Nothing is posted at or after ``drops_end``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cogs.hunt import copy as hunt_copy
from cogs.hunt.rolls import choose_item, slipped
from cogs.hunt.schedule import build_schedule
from cogs.hunt.theme import HuntItem
from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("hunt times must be timezone-aware")
    return moment.astimezone(timezone.utc)


def parse_dt(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError("stored hunt time is naive")
    return moment


def default_state() -> dict:
    return {
        "version": 1,
        "schedule_built": False,
        "schedule": [],
        "drops": {},
        "scores": {},
        "names": {},
        "catches": [],
        "slips": 0,
        "test_scores": {},
        "test_names": {},
        "test_catches": [],
        "test_slips": 0,
        "test_drops": {},
        "test_session": None,
        "kickoff_done": False,
        "kickoff_message": None,
        "recaps": {},
        "recap_done": {},
        "winner_message": None,
        "final_done": False,
        "closed": False,
        "event_disabled": False,
        "avatar_apply_attempted": False,
        "avatar_revert_attempted": False,
        "avatar_applied": False,
        "avatar_missing_logged": False,
        "had_global_avatar": False,
        "avatar_backup": None,
        "archive_written": False,
    }


def normalize_state(raw: object) -> dict:
    state = default_state()
    if not isinstance(raw, dict):
        return state
    state["version"] = 1
    for key in (
        "schedule_built",
        "kickoff_done",
        "final_done",
        "closed",
        "event_disabled",
        "avatar_apply_attempted",
        "avatar_revert_attempted",
        "avatar_applied",
        "avatar_missing_logged",
        "had_global_avatar",
        "archive_written",
    ):
        state[key] = bool(raw.get(key))
    if isinstance(raw.get("schedule"), list):
        slots = []
        for slot in raw["schedule"]:
            if isinstance(slot, dict) and slot.get("id") and slot.get("at") and slot.get("day"):
                slots.append(dict(slot))
        state["schedule"] = slots
    for bucket in ("drops", "test_drops"):
        raw_drops = raw.get(bucket)
        if isinstance(raw_drops, dict):
            state[bucket] = {str(key): dict(value) for key, value in raw_drops.items() if isinstance(value, dict)}
    for bucket in ("scores", "test_scores", "names", "test_names", "recap_done", "recaps"):
        raw_map = raw.get(bucket)
        if isinstance(raw_map, dict):
            state[bucket] = dict(raw_map)
    for bucket in ("catches", "test_catches"):
        if isinstance(raw.get(bucket), list):
            state[bucket] = [dict(row) for row in raw[bucket] if isinstance(row, dict)]
    for key in ("slips", "test_slips"):
        try:
            state[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            state[key] = 0
    if isinstance(raw.get("test_session"), dict):
        state["test_session"] = dict(raw["test_session"])
    if isinstance(raw.get("kickoff_message"), dict):
        state["kickoff_message"] = dict(raw["kickoff_message"])
    if isinstance(raw.get("winner_message"), dict):
        state["winner_message"] = dict(raw["winner_message"])
    backup = raw.get("avatar_backup")
    state["avatar_backup"] = backup if isinstance(backup, str) else None
    return state


@dataclass
class DeleteRef:
    channel_id: int
    message_id: int
    kind: str


@dataclass
class PostPlan:
    drop_id: str
    channel_id: int
    text: str
    expires_at: datetime
    item_key: str
    test: bool = False


@dataclass
class RecapPlan:
    day: str
    channel_id: int
    text: str


@dataclass
class LivePlan:
    posts: list[PostPlan] = field(default_factory=list)
    deletes: list[DeleteRef] = field(default_factory=list)
    kickoff_text: str | None = None
    kickoff_channel_id: int | None = None
    recaps: list[RecapPlan] = field(default_factory=list)
    final_text: str | None = None
    final_channel_id: int | None = None
    avatar: str = "none"
    missed: int = 0

    @property
    def quiet(self) -> bool:
        return not (
            self.posts
            or self.deletes
            or self.kickoff_text
            or self.recaps
            or self.final_text
            or self.avatar != "none"
        )


@dataclass
class ClaimResult:
    status: str
    text: str
    dm_text: str = ""
    total: int = 0
    test: bool = False
    delete: DeleteRef | None = None


def _message_ref(drop: dict, kind: str) -> DeleteRef | None:
    message_id = drop.get("message_id")
    channel_id = drop.get("channel_id")
    if isinstance(message_id, bool) or isinstance(channel_id, bool):
        return None
    try:
        mid = int(message_id)
        cid = int(channel_id)
    except (TypeError, ValueError):
        return None
    if mid <= 0 or cid <= 0:
        return None
    return DeleteRef(cid, mid, kind)


def _dedupe(refs: list[DeleteRef]) -> list[DeleteRef]:
    seen: set[tuple[int, int]] = set()
    unique: list[DeleteRef] = []
    for ref in refs:
        key = (ref.channel_id, ref.message_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def due_summary(state: dict, settings, now: datetime) -> str:
    """Read-only description. Does not roll types or mark slots."""
    lines: list[str] = []
    if not state.get("schedule_built"):
        lines.append("A schedule would be generated and saved.")
    start = as_utc(settings.event_start)
    end = as_utc(settings.drops_end)
    final = as_utc(settings.final_at)
    moment = as_utc(now)
    if not state.get("kickoff_done") and start <= moment < end:
        lines.append("Kickoff would post.")
    due = 0
    missed = 0
    for slot in state.get("schedule") or []:
        if slot.get("status") != "pending":
            continue
        try:
            at = as_utc(parse_dt(slot["at"]))
            lifetime = timedelta(seconds=int(slot["lifetime_seconds"]))
        except (TypeError, ValueError):
            continue
        if moment < at:
            continue
        if moment >= end or moment >= at + lifetime:
            missed += 1
        else:
            due += 1
    lines.append(f"Drops that would post: {due}.")
    lines.append(f"Slots that would be marked missed: {missed}.")
    if moment >= end and not state.get("closed"):
        lines.append("Open turkeys would close.")
    if moment >= final and not state.get("final_done"):
        lines.append("The winner post would go out, then leftover drops and recaps would be deleted.")
    elif moment < final:
        recaps = 0
        for day in settings.event_dates():
            key = day.isoformat()
            if state.get("recap_done", {}).get(key):
                continue
            recap_at = datetime.combine(day, settings.recap_time, tzinfo=settings.zone)
            if moment >= as_utc(recap_at):
                recaps += 1
        if recaps:
            lines.append(f"Daily recaps that would post: {recaps}.")
    if not lines:
        lines.append("Nothing is due.")
    return "\n".join(lines)


class HuntBook:
    """JSON book for one hunt. The path is injectable so tests stay out of data/."""

    def __init__(
        self,
        settings,
        path: str | Path,
        rng: random.Random | None = None,
        archive_path: str | Path | None = None,
    ):
        self.settings = settings
        self.path = Path(path)
        self.archive_path = Path(archive_path) if archive_path is not None else settings.archive_path()
        self.rng = rng if rng is not None else random.Random()
        self.persist = True
        self._lock = asyncio.Lock()
        self._file = LockedJsonFile(self.path, default=default_state, indent=2)
        self.state = normalize_state(self._file.read())

    def save(self) -> None:
        if not self.persist:
            return
        self._file.write(self.state)

    async def prepare_live(self, now: datetime, *, avatar_exists: bool) -> LivePlan:
        async with self._lock:
            await asyncio.sleep(0)
            plan, mutated = self._prepare(now, avatar_exists=avatar_exists)
            if mutated:
                self.save()
            return plan

    def _prepare(self, now: datetime, *, avatar_exists: bool) -> tuple[LivePlan, bool]:
        plan = LivePlan()
        if self.state.get("final_done") or self.state.get("event_disabled"):
            return plan, False
        mutated = False
        if not self.state.get("schedule_built"):
            self.state["schedule"] = build_schedule(
                self.settings.day_plans(),
                self.rng,
                zone=self.settings.zone,
                target_total=self.settings.target_total,
            )
            self.state["schedule_built"] = True
            mutated = True
            log.info("turkey schedule built slots=%s", len(self.state["schedule"]))
        moment = as_utc(now)
        end = as_utc(self.settings.drops_end)
        if moment >= end:
            if not self.state.get("closed"):
                self._close_live(now, plan)
                mutated = True
        else:
            if self._expire_open(now, plan):
                mutated = True
            if self._maybe_kickoff(now, plan):
                mutated = True
            if self._post_due(now, plan):
                mutated = True
        if self._maybe_avatar(now, plan, avatar_exists=avatar_exists):
            mutated = True
        if self._maybe_recaps(now, plan):
            mutated = True
        if self._maybe_final(now, plan):
            mutated = True
        plan.deletes = _dedupe(plan.deletes)
        return plan, mutated

    def _close_live(self, now: datetime, plan: LivePlan) -> None:
        moment = as_utc(now)
        for drop in self.state["drops"].values():
            if drop.get("status") != "open":
                continue
            try:
                expires = as_utc(parse_dt(drop["expires_at"]))
            except (TypeError, ValueError):
                expires = as_utc(self.settings.drops_end)
            # A turkey whose original lifetime is already over is expired.
            # One that would still be up is closed so it cannot be caught after drops_end.
            drop["status"] = "expired" if moment >= expires else "closed"
            ref = _message_ref(drop, "close")
            if ref is not None:
                plan.deletes.append(ref)
        # Re-evaluate status with the hunt clock already applied by the caller through expires.
        for slot in self.state["schedule"]:
            if slot.get("status") == "pending":
                slot["status"] = "missed"
                plan.missed += 1
                log.info("turkey slot missed id=%s reason=event_closed", slot.get("id"))
        self.state["closed"] = True

    def _expire_open(self, now: datetime, plan: LivePlan) -> bool:
        changed = False
        moment = as_utc(now)
        for drop in self.state["drops"].values():
            if drop.get("status") != "open":
                continue
            try:
                expires = as_utc(parse_dt(drop["expires_at"]))
            except (TypeError, ValueError):
                continue
            if moment < expires:
                continue
            drop["status"] = "expired"
            changed = True
            ref = _message_ref(drop, "expire")
            if ref is not None:
                plan.deletes.append(ref)
            log.info("turkey drop expired id=%s", drop.get("id"))
        return changed

    def _post_due(self, now: datetime, plan: LivePlan) -> bool:
        moment = as_utc(now)
        end = as_utc(self.settings.drops_end)
        if moment >= end or self.state.get("closed"):
            return False
        changed = False
        appeared: dict[str, set[str]] = {}
        for drop in self.state["drops"].values():
            day = str(drop.get("day") or "")
            key = drop.get("type_key")
            if day and isinstance(key, str):
                appeared.setdefault(day, set()).add(key)
        slots = sorted(self.state["schedule"], key=lambda slot: str(slot.get("at")))
        for slot in slots:
            if slot.get("status") != "pending":
                continue
            try:
                at = parse_dt(slot["at"])
                lifetime = timedelta(seconds=int(slot["lifetime_seconds"]))
            except (TypeError, ValueError):
                continue
            if moment < as_utc(at):
                continue
            if moment >= as_utc(at) + lifetime:
                slot["status"] = "missed"
                plan.missed += 1
                changed = True
                log.info("turkey slot missed id=%s reason=expired", slot.get("id"))
                continue
            day = str(slot.get("day"))
            force = None
            guarantee = slot.get("guarantee_key")
            if isinstance(guarantee, str) and guarantee not in appeared.get(day, set()):
                force = guarantee
            item = choose_item(self.settings.items(), self.rng, force_key=force)
            expires = at + lifetime
            drop = self._new_drop(
                slot_id=str(slot["id"]),
                channel_id=int(self.settings.drop_channel_id),
                item=item,
                scheduled_at=at,
                expires_at=expires,
                day=day,
                test=False,
                posted_at=now,
            )
            self.state["drops"][drop["id"]] = drop
            slot["status"] = "posted"
            appeared.setdefault(day, set()).add(item.key)
            plan.posts.append(
                PostPlan(
                    drop_id=drop["id"],
                    channel_id=drop["channel_id"],
                    text=hunt_copy.drop_text(item, expires, test=False, drops_end=self.settings.drops_end),
                    expires_at=expires,
                    item_key=item.key,
                )
            )
            changed = True
            log.info("turkey drop planned id=%s type=%s", drop["id"], item.key)
        return changed

    def _new_drop(
        self,
        *,
        slot_id: str,
        channel_id: int,
        item: HuntItem,
        scheduled_at: datetime,
        expires_at: datetime,
        day: str,
        test: bool,
        posted_at: datetime,
    ) -> dict:
        return {
            "id": slot_id,
            "channel_id": int(channel_id),
            "message_id": None,
            "type_key": item.key,
            "name": item.name,
            "emoji": item.emoji,
            "points": int(item.points),
            "grease_chance": float(item.grease_chance),
            "posted_at": as_utc(posted_at).isoformat(),
            "scheduled_at": scheduled_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "open",
            "caught_by": None,
            "day": day,
            "test": bool(test),
        }

    def _maybe_kickoff(self, now: datetime, plan: LivePlan) -> bool:
        if self.state.get("kickoff_done") or self.state.get("closed"):
            return False
        moment = as_utc(now)
        if moment < as_utc(self.settings.event_start) or moment >= as_utc(self.settings.drops_end):
            return False
        self.state["kickoff_done"] = True
        plan.kickoff_text = hunt_copy.kickoff_text(self.settings)
        plan.kickoff_channel_id = int(self.settings.drop_channel_id)
        return True

    def _maybe_avatar(self, now: datetime, plan: LivePlan, *, avatar_exists: bool) -> bool:
        moment = as_utc(now)
        if not self.state.get("kickoff_done"):
            return False
        if self.state.get("avatar_apply_attempted") or self.state.get("avatar_applied"):
            return False
        if self.state.get("avatar_missing_logged"):
            return False
        if moment < as_utc(self.settings.event_start) or moment >= as_utc(self.settings.drops_end):
            return False
        if avatar_exists:
            plan.avatar = "apply"
            return False
        self.state["avatar_missing_logged"] = True
        plan.avatar = "skip_missing"
        log.warning("turkey avatar missing; kickoff will continue without an avatar change")
        return True

    def _maybe_recaps(self, now: datetime, plan: LivePlan) -> bool:
        moment = as_utc(now)
        if moment >= as_utc(self.settings.final_at):
            return False
        changed = False
        for day in self.settings.event_dates():
            key = day.isoformat()
            if self.state["recap_done"].get(key):
                continue
            recap_at = datetime.combine(day, self.settings.recap_time, tzinfo=self.settings.zone)
            if moment < as_utc(recap_at):
                continue
            self.state["recap_done"][key] = True
            plan.recaps.append(
                RecapPlan(
                    day=key,
                    channel_id=int(self.settings.drop_channel_id),
                    text=hunt_copy.recap_text(
                        self.settings,
                        day,
                        self.state["scores"],
                        self.state["names"],
                        self.state["catches"],
                    ),
                )
            )
            changed = True
        return changed

    def _maybe_final(self, now: datetime, plan: LivePlan) -> bool:
        if self.state.get("final_done"):
            return False
        if as_utc(now) < as_utc(self.settings.final_at):
            return False
        plan.final_text = hunt_copy.winner_text(
            self.settings,
            self.state["scores"],
            self.state["names"],
            self.state["catches"],
            int(self.state.get("slips") or 0),
        )
        plan.final_channel_id = int(self.settings.drop_channel_id)
        plan.deletes.extend(self.leftover_deletes())
        return False

    def leftover_deletes(self) -> list[DeleteRef]:
        """Drops still on the table and daily recap posts. Caught lines and the winner stay."""
        refs: list[DeleteRef] = []
        winner = self.state.get("winner_message") or {}
        winner_id = winner.get("message_id")
        for drop in self.state["drops"].values():
            if drop.get("test") or drop.get("status") == "caught":
                continue
            ref = _message_ref(drop, "drop")
            if ref is None or ref.message_id == winner_id:
                continue
            if ref.channel_id in set(self.settings.cleanup_skip_channel_ids):
                continue
            refs.append(ref)
        for recap in self.state["recaps"].values():
            if not isinstance(recap, dict):
                continue
            try:
                ref = DeleteRef(int(recap["channel_id"]), int(recap["message_id"]), "recap")
            except (KeyError, TypeError, ValueError):
                continue
            if ref.message_id == winner_id:
                continue
            if ref.channel_id in set(self.settings.cleanup_skip_channel_ids):
                continue
            refs.append(ref)
        kickoff = self.state.get("kickoff_message") or {}
        kickoff_id = kickoff.get("message_id")
        return [ref for ref in refs if ref.message_id != kickoff_id]

    def note_message(self, drop_id: str, message_id: int, *, test: bool = False) -> None:
        bucket = self.state["test_drops"] if test else self.state["drops"]
        drop = bucket.get(drop_id)
        if not isinstance(drop, dict):
            return
        drop["message_id"] = int(message_id)
        self.save()

    def note_kickoff(self, channel_id: int, message_id: int) -> None:
        self.state["kickoff_message"] = {"channel_id": int(channel_id), "message_id": int(message_id)}
        self.save()

    def note_recap(self, day: str, channel_id: int, message_id: int) -> None:
        self.state["recaps"][str(day)] = {"channel_id": int(channel_id), "message_id": int(message_id)}
        self.save()

    def undo_kickoff(self) -> None:
        self.state["kickoff_done"] = False
        self.save()

    def undo_recap(self, day: str) -> None:
        self.state["recap_done"].pop(str(day), None)
        self.save()

    def revert_post(self, drop_id: str) -> None:
        self.state["drops"].pop(drop_id, None)
        for slot in self.state["schedule"]:
            if slot.get("id") == drop_id and slot.get("status") == "posted":
                slot["status"] = "pending"
        self.save()

    def note_avatar_attempted(self) -> None:
        self.state["avatar_apply_attempted"] = True
        self.save()

    def note_avatar_applied(self, *, had_global_avatar: bool, backup: str | None) -> None:
        self.state["avatar_apply_attempted"] = True
        self.state["avatar_applied"] = True
        self.state["had_global_avatar"] = bool(had_global_avatar)
        self.state["avatar_backup"] = backup
        self.save()

    def note_avatar_reverted(self) -> None:
        self.state["avatar_revert_attempted"] = True
        self.save()

    def commit_final(self, message_id: int, channel_id: int, now: datetime) -> bool:
        """Archive scores, reset the live book, and report whether the avatar should revert."""
        should_revert = bool(self.state.get("avatar_applied")) and not bool(self.state.get("avatar_revert_attempted"))
        previous = self.state
        payload = {
            "season": self.settings.final_at.astimezone(self.settings.zone).year,
            "archived_at": as_utc(now).isoformat(),
            "scores": previous.get("scores") or {},
            "names": previous.get("names") or {},
            "catches": previous.get("catches") or [],
            "slips": int(previous.get("slips") or 0),
            "schedule": previous.get("schedule") or [],
            "prize_line": self.settings.prize_line,
            "winner_message_id": int(message_id),
            "kickoff_message": previous.get("kickoff_message"),
        }
        self._write_archive(payload)
        shell = default_state()
        shell["schedule_built"] = True
        shell["final_done"] = True
        shell["closed"] = True
        shell["event_disabled"] = True
        shell["avatar_apply_attempted"] = bool(previous.get("avatar_apply_attempted"))
        shell["avatar_applied"] = bool(previous.get("avatar_applied"))
        shell["avatar_revert_attempted"] = bool(previous.get("avatar_revert_attempted"))
        shell["avatar_missing_logged"] = bool(previous.get("avatar_missing_logged"))
        shell["had_global_avatar"] = bool(previous.get("had_global_avatar"))
        shell["avatar_backup"] = previous.get("avatar_backup")
        shell["archive_written"] = True
        shell["winner_message"] = {"channel_id": int(channel_id), "message_id": int(message_id)}
        self.state = shell
        self.save()
        return should_revert

    def _write_archive(self, payload: dict) -> None:
        if not self.persist:
            return
        path = self.archive_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)

    def preview_schedule(self, rng: random.Random | None = None) -> list[dict]:
        return build_schedule(
            self.settings.day_plans(),
            rng or random.Random(),
            zone=self.settings.zone,
            target_total=self.settings.target_total,
        )

    def save_generated_schedule(self, rng: random.Random | None = None) -> str:
        if any(slot.get("status") == "posted" for slot in self.state["schedule"]):
            return "posted"
        self.state["schedule"] = self.preview_schedule(rng or self.rng)
        self.state["schedule_built"] = True
        self.save()
        return "saved"

    async def claim(
        self,
        drop_id: str,
        user_id: int,
        now: datetime,
        rng: random.Random,
        *,
        name: object,
        is_bot: bool = False,
    ) -> ClaimResult:
        async with self._lock:
            await asyncio.sleep(0)
            result, mutated = self._claim(drop_id, user_id, now, rng, name=name, is_bot=is_bot)
            if mutated:
                self.save()
            return result

    def _claim(
        self,
        drop_id: str,
        user_id: int,
        now: datetime,
        rng: random.Random,
        *,
        name: object,
        is_bot: bool,
    ) -> tuple[ClaimResult, bool]:
        drop, test = self._find_drop(drop_id)
        if drop is None:
            return ClaimResult("missing", "That turkey isn't here."), False
        if is_bot:
            return ClaimResult("bot", "Bots keep their wings off the table.", test=test), False
        status = drop.get("status")
        if status == "caught":
            return ClaimResult("taken", "Someone already caught that turkey.", test=test), False
        if status == "expired":
            return ClaimResult("expired", "That turkey already left the table.", test=test), False
        if status == "closed":
            return ClaimResult("closed", "The hunt is closed.", test=test), False
        if status != "open":
            return ClaimResult("missing", "That turkey isn't here.", test=test), False
        moment = as_utc(now)
        if not test and moment >= as_utc(self.settings.drops_end):
            drop["status"] = "closed"
            return ClaimResult("closed", "The hunt is closed.", test=test, delete=_message_ref(drop, "close")), True
        try:
            expires = as_utc(parse_dt(drop["expires_at"]))
        except (TypeError, ValueError):
            expires = moment
        if moment >= expires:
            drop["status"] = "expired"
            return (
                ClaimResult("expired", "That turkey already left the table.", test=test, delete=_message_ref(drop, "expire")),
                True,
            )
        item = self._item_from_drop(drop)
        if slipped(float(drop.get("grease_chance") or 0), rng):
            slip_key = "test_slips" if test else "slips"
            self.state[slip_key] = int(self.state.get(slip_key) or 0) + 1
            return ClaimResult("slip", hunt_copy.slip_text(item), test=test), True
        scores, names, catches = self._buckets(test)
        key = str(user_id)
        scores[key] = int(scores.get(key) or 0) + int(drop["points"])
        names[key] = hunt_copy.safe_label(name)
        catches.append(
            {
                "user_id": key,
                "drop_id": drop["id"],
                "type_key": drop["type_key"],
                "name": drop["name"],
                "points": int(drop["points"]),
                "at": now.isoformat(),
                "day": drop.get("day"),
            }
        )
        drop["status"] = "caught"
        drop["caught_by"] = int(user_id)
        total = int(scores[key])
        rank = self._rank(scores, key)
        text = hunt_copy.catch_public(names[key], item, total, test=test)
        dm = hunt_copy.catch_dm(names[key], item, total, rank, test=test)
        return ClaimResult("caught", text, dm, total, test), True

    def _find_drop(self, drop_id: str) -> tuple[dict | None, bool]:
        drop = self.state["drops"].get(drop_id)
        if isinstance(drop, dict):
            return drop, False
        test = self.state["test_drops"].get(drop_id)
        if isinstance(test, dict):
            return test, True
        return None, False

    def _buckets(self, test: bool) -> tuple[dict, dict, list]:
        if test:
            return self.state["test_scores"], self.state["test_names"], self.state["test_catches"]
        return self.state["scores"], self.state["names"], self.state["catches"]

    def _item_from_drop(self, drop: dict) -> HuntItem:
        return HuntItem(
            str(drop.get("type_key")),
            str(drop.get("name")),
            int(drop.get("points") or 0),
            0.0,
            float(drop.get("grease_chance") or 0),
            str(drop.get("emoji") or ""),
        )

    def _rank(self, scores: dict, user_key: str) -> int | None:
        seen = 0
        for _score, user_ids in hunt_copy.score_groups(scores):
            if user_key in user_ids:
                return seen + 1
            seen += len(user_ids)
        return None

    def start_test_session(
        self,
        *,
        guild_id: int,
        channel_id: int,
        minutes: int,
        interval: int,
        now: datetime,
    ) -> str:
        if int(guild_id) == int(self.settings.guild_id):
            return "mts"
        if int(channel_id) == int(self.settings.drop_channel_id):
            return "channel"
        if int(channel_id) in set(self.settings.cleanup_skip_channel_ids):
            return "channel"
        if not (
            self.settings.test_session_min_minutes
            <= int(minutes)
            <= self.settings.test_session_max_minutes
        ):
            return "invalid"
        if not (
            self.settings.test_session_min_interval
            <= int(interval)
            <= self.settings.test_session_max_interval
        ):
            return "invalid"
        current = self.state.get("test_session")
        if isinstance(current, dict):
            try:
                if as_utc(now) < as_utc(parse_dt(current["ends_at"])):
                    return "busy"
            except (TypeError, ValueError):
                pass
        self.state["test_session"] = {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "ends_at": (now + timedelta(minutes=int(minutes))).isoformat(),
            "next_at": now.isoformat(),
            "interval_minutes": int(interval),
            "drops_posted": 0,
        }
        self.save()
        return "started"

    def stop_test_session(self) -> bool:
        if not isinstance(self.state.get("test_session"), dict):
            return False
        self.state["test_session"] = None
        self.save()
        return True

    def poll_test_session(self, now: datetime) -> dict | None:
        session = self.state.get("test_session")
        if not isinstance(session, dict):
            return None
        try:
            ends = parse_dt(session["ends_at"])
            nxt = parse_dt(session["next_at"])
        except (KeyError, TypeError, ValueError):
            self.state["test_session"] = None
            self.save()
            return None
        if as_utc(now) >= as_utc(ends):
            self.state["test_session"] = None
            self.save()
            return None
        if as_utc(now) < as_utc(nxt):
            return None
        drop = self._open_test_drop(int(session["channel_id"]), now)
        if drop is None:
            return None
        interval = timedelta(minutes=int(session["interval_minutes"]))
        session["next_at"] = (now + interval).isoformat()
        session["drops_posted"] = int(session.get("drops_posted") or 0) + 1
        self.save()
        return drop

    def open_test_drop(self, channel_id: int, now: datetime, rng: random.Random | None = None) -> dict | None:
        if int(channel_id) == int(self.settings.drop_channel_id):
            return None
        if int(channel_id) in set(self.settings.cleanup_skip_channel_ids):
            return None
        drop = self._open_test_drop(int(channel_id), now, rng)
        if drop is not None:
            self.save()
        return drop

    def _open_test_drop(self, channel_id: int, now: datetime, rng: random.Random | None = None) -> dict | None:
        if int(channel_id) == int(self.settings.drop_channel_id):
            return None
        roller = rng or self.rng
        item = choose_item(self.settings.items(), roller)
        existing = set(self.state["drops"]) | set(self.state["test_drops"])
        from cogs.hunt.schedule import new_drop_id

        drop_id = new_drop_id(roller, existing)
        expires = now + self.settings.test_lifetime
        drop = self._new_drop(
            slot_id=drop_id,
            channel_id=channel_id,
            item=item,
            scheduled_at=now,
            expires_at=expires,
            day=now.astimezone(self.settings.zone).date().isoformat(),
            test=True,
            posted_at=now,
        )
        self.state["test_drops"][drop_id] = drop
        return drop

    def expire_tests(self, now: datetime) -> list[DeleteRef]:
        refs: list[DeleteRef] = []
        changed = False
        moment = as_utc(now)
        for drop in self.state["test_drops"].values():
            if drop.get("status") != "open":
                continue
            try:
                expires = as_utc(parse_dt(drop["expires_at"]))
            except (TypeError, ValueError):
                continue
            if moment < expires:
                continue
            drop["status"] = "expired"
            changed = True
            ref = _message_ref(drop, "expire")
            if ref is not None:
                refs.append(ref)
        if changed:
            self.save()
        return refs

    def forget_test_drop(self, drop_id: str) -> None:
        self.state["test_drops"].pop(drop_id, None)
        self.save()


def closed_shell_keeps_winner(state: dict) -> bool:
    winner = state.get("winner_message") or {}
    return bool(state.get("final_done")) and bool(winner.get("message_id")) and not state.get("scores")
