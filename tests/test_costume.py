"""Costume schedule, asset loading, and one-shot profile edits. No network."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from cogs.costume import (
    APPLY_AT,
    APPLY_REASON,
    AVATAR_ASSET,
    BIO_ASSET,
    MTS_GUILD_ID,
    PT,
    REVERT_AT,
    REVERT_REASON,
    STATE_KEYS,
    Costume,
    _costume_interaction_check,
    apply_is_open,
    as_utc,
    avatar_hash_from_payload,
    bio_from_member_payload,
    block_message,
    default_state,
    due_action,
    format_pt,
    load_avatar_bytes,
    load_bio_text,
    normalize_state,
    record_apply_success,
    record_revert_success,
    render_status,
    result_message,
    revert_is_open,
    revert_payload,
    select_guild_avatar_asset,
    should_snapshot,
)
from PooperScooper import initial_extensions

BEFORE = APPLY_AT - timedelta(seconds=1)
MID = datetime(2026, 10, 31, 15, 0, tzinfo=PT)
AFTER = datetime(2026, 11, 2, 8, 0, tzinfo=PT)


def _state(**overrides) -> dict:
    state = default_state()
    state.update(overrides)
    return state


class FakeAsset:
    def __init__(self, data: bytes, key: str = "abc"):
        self.data = data
        self.key = key
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self.data


class FakeHTTP:
    def __init__(self, payload: dict, probe: dict | None = None, probe_error: BaseException | None = None):
        self.payload = payload
        self.probe = probe
        self.probe_error = probe_error
        self.gets = 0
        self.probes = 0

    async def get_member(self, guild_id: int, member_id: int) -> dict:
        self.gets += 1
        self.seen = (guild_id, member_id)
        return dict(self.payload)

    async def edit_my_member(self, guild_id: int, **kwargs):
        self.probes += 1
        self.probe_kwargs = kwargs
        if self.probe_error is not None:
            raise self.probe_error
        return {} if self.probe is None else dict(self.probe)


class FakeMember:
    def __init__(self, *, avatar=None, fail: int = 0):
        self.guild_avatar = avatar
        self.id = 4242
        self.guild = SimpleNamespace(id=MTS_GUILD_ID)
        self._state = None
        self.edits: list[dict] = []
        self.disk_during_edit: list[dict] = []
        self.fail = fail
        self.cog = None

    async def edit(self, **kwargs):
        if self.cog is not None:
            self.disk_during_edit.append(json.loads(self.cog.store.path.read_text(encoding="utf-8")))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("HTTP 429")
        self.edits.append(kwargs)


class FakeGuild:
    def __init__(self, member: FakeMember):
        self.me = member
        self.id = MTS_GUILD_ID
        self.fetch_calls = 0

    async def fetch_member(self, user_id: int):
        self.fetch_calls += 1
        raise AssertionError("cached member should be used")


class FakeBot:
    def __init__(self, guild, http):
        self._guild = guild
        self.http = http
        self.user = SimpleNamespace(id=4242)

    def get_guild(self, guild_id: int):
        if self._guild is None or guild_id != MTS_GUILD_ID:
            return None
        return self._guild

    async def wait_until_ready(self):
        return None

    async def is_owner(self, user):
        return getattr(user, "id", None) == 1


def _cog(tmp_path, *, member: FakeMember | None = None, http: FakeHTTP | None = None, guild=True, **kwargs):
    avatar_path = tmp_path / "avatar.png"
    bio_path = tmp_path / "bio.txt"
    if not avatar_path.exists():
        avatar_path.write_bytes(kwargs.pop("avatar_bytes", b"PNG-COSTUME"))
    else:
        kwargs.pop("avatar_bytes", None)
    if not bio_path.exists():
        bio_path.write_text(kwargs.pop("bio_text", "Count Pooper\n"), encoding="utf-8")
    else:
        kwargs.pop("bio_text", None)
    member = member if member is not None else FakeMember()
    http = http if http is not None else FakeHTTP({"avatar": None, "bio": None})
    bot = FakeBot(FakeGuild(member) if guild else None, http)
    cog = Costume(
        bot,
        path=tmp_path / "costume.json",
        avatar_path=avatar_path,
        bio_path=bio_path,
        backup_path=tmp_path / "costume_backup_avatar.png",
        **kwargs,
    )
    member.cog = cog
    return cog, member, http


def _interaction(user_id: int = 1, *, owner: bool = True):
    async def is_owner(user):
        return owner and getattr(user, "id", None) == user_id

    sent = {"done": False}

    async def send_message(*args, **kwargs):
        sent["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            defer=AsyncMock(),
            is_done=lambda: sent["done"],
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        client=SimpleNamespace(is_owner=is_owner),
    )


def test_state_schema_and_normalize():
    assert set(default_state()) == set(STATE_KEYS)
    cleaned = normalize_state(
        {"apply_attempted": "yes", "applied_at": 5, "had_guild_bio": True, "original_bio": "x", "extra": 1}
    )
    assert cleaned["apply_attempted"] is False
    assert cleaned["applied_at"] is None
    assert cleaned["had_guild_bio"] is True
    assert cleaned["original_bio"] == "x"
    assert "extra" not in cleaned
    assert normalize_state(["nope"]) == default_state()


def test_schedule_actions():
    fresh = default_state()
    attempted = _state(apply_attempted=True)
    failed = _state(apply_attempted=True, apply_error="HTTP 429")
    applied = _state(apply_attempted=True, applied_at=as_utc(MID).isoformat())
    applied_no_flag = _state(applied_at=as_utc(MID).isoformat())
    reverted = _state(
        apply_attempted=True,
        applied_at=as_utc(MID).isoformat(),
        revert_attempted=True,
        reverted_at=as_utc(AFTER).isoformat(),
    )
    revert_failed = _state(
        apply_attempted=True,
        applied_at=as_utc(MID).isoformat(),
        revert_attempted=True,
        revert_error="nope",
    )

    assert due_action(BEFORE, fresh) == "none"
    assert due_action(APPLY_AT, fresh) == "apply"
    assert due_action(APPLY_AT.astimezone(timezone.utc), fresh) == "apply"
    assert due_action(MID, fresh) == "apply"
    assert due_action(REVERT_AT - timedelta(seconds=1), fresh) == "apply"
    assert due_action(MID, attempted) == "none"
    assert due_action(MID, failed) == "none"
    assert due_action(MID, applied) == "none"
    assert due_action(MID, applied_no_flag) == "none"
    assert due_action(MID, attempted, force=True) == "none"
    assert due_action(REVERT_AT, fresh) == "none"
    assert due_action(AFTER, fresh) == "none"
    assert due_action(AFTER, failed) == "none"
    assert due_action(REVERT_AT, applied) == "revert"
    assert due_action(AFTER, applied) == "revert"
    assert due_action(AFTER, reverted) == "none"
    assert due_action(AFTER, revert_failed) == "none"

    assert due_action(BEFORE, fresh, manual="apply") == "apply"
    assert due_action(BEFORE, attempted, manual="apply") == "none"
    assert due_action(BEFORE, failed, manual="apply", force=True) == "apply"
    assert due_action(AFTER, fresh, manual="revert") == "none"
    assert due_action(MID, applied, manual="revert") == "revert"
    assert due_action(MID, reverted, manual="revert") == "none"
    assert due_action(MID, revert_failed, manual="revert", force=True) == "revert"
    assert due_action(AFTER, fresh, manual="revert", force=True) == "revert"

    with pytest.raises(ValueError):
        due_action(datetime(2026, 10, 31, 0, 0), fresh)
    with pytest.raises(ValueError):
        due_action(MID, fresh, manual="dance")


def test_success_records_let_the_window_revert_once():
    applied = record_apply_success(
        _state(had_guild_avatar=True, had_guild_bio=True, original_bio="old"),
        MID,
        revert_at=REVERT_AT,
    )
    assert applied["apply_attempted"] is True
    assert applied["applied_at"]
    assert applied["revert_attempted"] is False
    assert applied["had_guild_avatar"] is True
    assert applied["original_bio"] == "old"
    assert due_action(MID, applied) == "none"
    assert due_action(AFTER, applied) == "revert"

    late = record_apply_success(default_state(), AFTER, revert_at=REVERT_AT)
    assert late["revert_attempted"] is True
    assert due_action(AFTER, late) == "none"

    reverted = record_revert_success(applied, AFTER)
    assert reverted["reverted_at"]
    assert reverted["applied_at"] == applied["applied_at"]
    assert due_action(AFTER, reverted) == "none"
    assert due_action(MID, reverted) == "none"
    assert apply_is_open(reverted, force=False) is False
    assert revert_is_open(reverted, force=False) is False
    assert revert_is_open(reverted, force=True) is True


def test_messages_and_status_use_pacific_time():
    assert format_pt(APPLY_AT) == "2026-10-31 00:00 PT"
    assert format_pt(REVERT_AT) == "2026-11-01 00:00 PT"
    text = render_status(
        _state(apply_error="boom"),
        now=BEFORE,
        avatar_ready=True,
        bio_ready=False,
        backup_ready=False,
    )
    assert "2026-10-31 00:00 PT" in text
    assert "2026-11-01 00:00 PT" in text
    assert "Due now: none" in text
    assert "apply_error: boom" in text
    assert "avatar.png: present" in text
    assert "bio.txt: missing" in text
    assert "Menaces to Sobriety" in text
    failed = _state(apply_attempted=True, apply_error="HTTP 429")
    assert "force" in block_message(failed, manual="apply", force=False).lower()
    assert "never applied" in block_message(default_state(), manual="revert", force=False)
    assert "won't retry" in result_message("apply", "error", "HTTP 429")


def test_real_costume_assets_load():
    bio = load_bio_text(BIO_ASSET)
    assert not bio.startswith("\ufeff")
    assert bio.startswith("🎃")
    assert "Halloween" in bio
    avatar = load_avatar_bytes(AVATAR_ASSET)
    assert avatar.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(avatar)) as image:
        assert image.size == (512, 512)


def test_bio_loader_strips_bom_in_tmp(tmp_path):
    path = tmp_path / "bio.txt"
    path.write_bytes("\ufeff\n  spooky \n".encode("utf-8"))
    assert load_bio_text(path) == "spooky"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_bio_text(path)
    with pytest.raises(FileNotFoundError):
        load_avatar_bytes(tmp_path / "missing.png")


def test_payload_helpers_and_revert_bytes(tmp_path):
    assert bio_from_member_payload({"bio": "  hi  "}) == "hi"
    assert bio_from_member_payload({"bio": "   "}) is None
    assert bio_from_member_payload({"bio": None}) is None
    assert bio_from_member_payload([]) is None
    assert avatar_hash_from_payload({"avatar": "abc"}) == "abc"
    assert avatar_hash_from_payload({"avatar": None}) is None

    backup = tmp_path / "backup.png"
    backup.write_bytes(b"ORIGINAL")
    avatar, bio = revert_payload(
        _state(had_guild_avatar=True, had_guild_bio=True, original_bio="old bio"),
        backup,
    )
    assert avatar == b"ORIGINAL"
    assert bio == "old bio"
    avatar, bio = revert_payload(default_state(), backup)
    assert avatar is None and bio is None
    with pytest.raises(FileNotFoundError):
        revert_payload(_state(had_guild_avatar=True), tmp_path / "gone.png")

    cached = FakeAsset(b"stale", key="old")
    member = FakeMember(avatar=cached)
    assert select_guild_avatar_asset(member, {"avatar": None}) is None
    assert select_guild_avatar_asset(member, {"avatar": "old"}) is cached
    assert should_snapshot(default_state()) is True
    assert should_snapshot(_state(applied_at="2026-10-31T00:00:00+00:00")) is False
    assert should_snapshot(_state(apply_attempted=True, had_guild_avatar=True)) is False


def test_commands_are_registered():
    # The module stays importable, but it is not loaded, so a restart cannot edit the avatar or bio.
    assert "cogs.costume" not in initial_extensions
    group = Costume.costume
    assert group.name == "costume"
    assert group.guild_only is True
    assert group.interaction_check is _costume_interaction_check
    names = sorted(command.name for command in group.commands)
    assert names == ["apply_now", "preview", "revert_now", "status"]
    assert all(command.guild_only for command in group.commands)
    assert [param.name for param in Costume.costume_apply_now.parameters] == ["force"]
    assert [param.name for param in Costume.costume_revert_now.parameters] == ["force"]
    assert Costume.costume_status.parameters == []
    assert Costume.costume_preview.parameters == []
    assert Costume.costume_loop.seconds == 60


def test_owner_gate_is_ephemeral():
    async def run():
        stranger = _interaction(5, owner=False)
        assert await _costume_interaction_check(stranger) is False
        message = stranger.response.send_message.await_args.args[0]
        assert message == "Only the bot owner can use costume commands."
        assert stranger.response.send_message.await_args.kwargs["ephemeral"] is True

        owner = _interaction(1, owner=True)
        assert await _costume_interaction_check(owner) is True
        owner.response.send_message.assert_not_awaited()

    asyncio.run(run())


def test_apply_once_mid_window_and_not_again(tmp_path):
    async def run():
        cog, member, http = _cog(tmp_path)
        assert await cog.tick(now=BEFORE) == "none"
        assert member.edits == []
        assert http.gets == 0

        assert await cog.tick(now=MID) == "applied"
        assert len(member.edits) == 1
        assert member.edits[0]["avatar"] == b"PNG-COSTUME"
        assert member.edits[0]["bio"] == "Count Pooper"
        assert member.edits[0]["reason"] == APPLY_REASON
        during = member.disk_during_edit[0]
        assert during["apply_attempted"] is True
        assert during["applied_at"] is None
        assert cog.state["applied_at"]
        assert cog.state["apply_error"] is None
        assert set(json.loads(cog.store.path.read_text(encoding="utf-8"))) == set(STATE_KEYS)
        assert http.probes == 0

        assert await cog.tick(now=MID) == "none"
        assert len(member.edits) == 1
        assert http.gets == 1

    asyncio.run(run())


def test_failed_apply_is_not_retried(tmp_path):
    async def run():
        member = FakeMember(fail=1)
        cog, member, _http = _cog(tmp_path, member=member)
        assert await cog.tick(now=MID) == "error"
        assert member.edits == []
        assert member.disk_during_edit[0]["apply_attempted"] is True
        assert member.disk_during_edit[0]["applied_at"] is None
        assert cog.state["apply_attempted"] is True
        assert cog.state["applied_at"] is None
        assert "429" in cog.state["apply_error"]
        assert await cog.tick(now=MID) == "none"
        assert member.edits == []

        message = await cog._manual("apply", force=True)
        assert message.startswith("Halloween costume is on")
        assert len(member.edits) == 1

    asyncio.run(run())


def test_missing_guild_does_not_burn_the_attempt(tmp_path):
    async def run():
        cog, member, http = _cog(tmp_path, guild=False)
        assert await cog.tick(now=MID) == "unavailable"
        assert cog.state["apply_attempted"] is False
        assert http.gets == 0
        assert await cog.tick(now=MID) == "unavailable"
        assert member.edits == []

    asyncio.run(run())


def test_late_start_never_applied_does_nothing(tmp_path):
    async def run():
        cog, member, http = _cog(tmp_path)
        assert await cog.tick(now=AFTER) == "none"
        assert member.edits == []
        assert http.gets == 0
        assert cog.state == default_state()

    asyncio.run(run())


def test_existing_profile_is_backed_up_and_restored(tmp_path):
    async def run():
        original = b"GIF89a-original"
        asset = FakeAsset(original, key="hash1")
        member = FakeMember(avatar=asset)
        http = FakeHTTP({"avatar": "hash1", "bio": "  old bio  "})
        cog, member, http = _cog(tmp_path, member=member, http=http)

        assert await cog.tick(now=MID) == "applied"
        assert cog.backup_path.read_bytes() == original
        assert asset.reads == 1
        assert cog.state["had_guild_avatar"] is True
        assert cog.state["had_guild_bio"] is True
        assert cog.state["original_bio"] == "old bio"
        assert http.probes == 0
        assert member.edits[0]["avatar"] == b"PNG-COSTUME"

        assert await cog.tick(now=AFTER) == "reverted"
        assert member.edits[1]["avatar"] == original
        assert member.edits[1]["bio"] == "old bio"
        assert member.edits[1]["reason"] == REVERT_REASON
        during = member.disk_during_edit[1]
        assert during["revert_attempted"] is True
        assert during["reverted_at"] is None
        assert cog.state["reverted_at"]
        assert await cog.tick(now=AFTER) == "none"
        assert len(member.edits) == 2

    asyncio.run(run())


def test_empty_profile_revert_clears_overrides(tmp_path):
    async def run():
        cog, member, _http = _cog(tmp_path)
        assert await cog.tick(now=MID) == "applied"
        assert not cog.backup_path.exists()
        assert await cog.tick(now=AFTER) == "reverted"
        assert member.edits[1]["avatar"] is None
        assert member.edits[1]["bio"] is None

    asyncio.run(run())


def test_stale_cached_avatar_is_not_treated_as_current(tmp_path):
    async def run():
        asset = FakeAsset(b"stale", key="old")
        member = FakeMember(avatar=asset)
        http = FakeHTTP({"avatar": None, "bio": None})
        cog, member, _http = _cog(tmp_path, member=member, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert asset.reads == 0
        assert not cog.backup_path.exists()
        assert cog.state["had_guild_avatar"] is False

    asyncio.run(run())


def test_avatar_hash_falls_back_to_cdn(tmp_path):
    async def run():
        member = FakeMember(avatar=None)
        member._state = SimpleNamespace(http=SimpleNamespace(get_from_cdn=AsyncMock(return_value=b"from-cdn")))
        http = FakeHTTP({"avatar": "cafebabe", "bio": None})
        cog, member, _http = _cog(tmp_path, member=member, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert cog.backup_path.read_bytes() == b"from-cdn"
        assert cog.state["had_guild_avatar"] is True
        url = member._state.http.get_from_cdn.await_args.args[0]
        assert "cafebabe" in url

    asyncio.run(run())


def test_missing_bio_key_is_probed_once(tmp_path):
    async def run():
        http = FakeHTTP({"avatar": None}, probe={"bio": "  saved bio  "})
        cog, member, http = _cog(tmp_path, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert http.probes == 1
        assert cog.state["original_bio"] == "saved bio"
        assert cog.state["had_guild_bio"] is True
        assert len(member.edits) == 1
        assert await cog.tick(now=MID) == "none"
        assert http.probes == 1

    asyncio.run(run())


def test_bio_probe_failure_still_applies_once(tmp_path):
    async def run():
        http = FakeHTTP({"avatar": None}, probe_error=RuntimeError("unread"))
        cog, member, http = _cog(tmp_path, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert http.probes == 1
        assert cog.state["had_guild_bio"] is False
        assert cog.state["original_bio"] is None
        assert len(member.edits) == 1

    asyncio.run(run())


def test_missing_backup_fails_revert_once(tmp_path):
    async def run():
        asset = FakeAsset(b"ORIGINAL", key="hash1")
        member = FakeMember(avatar=asset)
        http = FakeHTTP({"avatar": "hash1", "bio": None})
        cog, member, _http = _cog(tmp_path, member=member, http=http)
        assert await cog.tick(now=MID) == "applied"
        cog.backup_path.unlink()
        assert await cog.tick(now=AFTER) == "error"
        assert len(member.edits) == 1
        assert cog.state["revert_attempted"] is True
        assert "Backup guild avatar" in cog.state["revert_error"]
        assert await cog.tick(now=AFTER) == "none"

    asyncio.run(run())


def test_missing_asset_fails_once_without_discord(tmp_path):
    async def run():
        cog, member, http = _cog(tmp_path)
        cog.avatar_path.unlink()
        assert await cog.tick(now=MID) == "error"
        assert member.edits == []
        assert http.gets == 0
        assert cog.state["apply_attempted"] is True
        assert await cog.tick(now=MID) == "none"

    asyncio.run(run())


def test_preview_and_status_do_not_edit(tmp_path):
    async def run():
        cog, member, http = _cog(tmp_path)
        preview = _interaction()
        await Costume.costume_preview.callback(cog, preview)
        kwargs = preview.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["content"] == "Count Pooper"
        assert kwargs["file"].filename == "halloween_avatar.png"
        assert member.edits == []
        assert http.gets == 0

        status = _interaction()
        await Costume.costume_status.callback(cog, status)
        text = status.response.send_message.await_args.args[0]
        assert "2026-10-31 00:00 PT" in text
        assert "2026-11-01 00:00 PT" in text
        assert status.response.send_message.await_args.kwargs["ephemeral"] is True
        assert member.edits == []

    asyncio.run(run())


def test_manual_commands_respect_flags(tmp_path):
    async def run():
        cog, member, _http = _cog(tmp_path)
        early = _interaction()
        await Costume.costume_revert_now.callback(cog, early)
        assert early.response.defer.await_args.kwargs["ephemeral"] is True
        revert_message = early.followup.send.await_args.args[0]
        assert "never applied" in revert_message
        assert early.followup.send.await_args.kwargs["ephemeral"] is True
        assert member.edits == []

        apply = _interaction()
        await Costume.costume_apply_now.callback(cog, apply)
        assert "Halloween costume is on" in apply.followup.send.await_args.args[0]
        assert len(member.edits) == 1

        again = _interaction()
        await Costume.costume_apply_now.callback(cog, again)
        assert "already" in again.followup.send.await_args.args[0].lower()
        assert len(member.edits) == 1

        forced = _interaction()
        await Costume.costume_apply_now.callback(cog, forced, force=True)
        assert len(member.edits) == 2
        assert member.edits[1]["avatar"] == b"PNG-COSTUME"

    asyncio.run(run())


def test_loop_callback_swallows_errors(tmp_path):
    async def run():
        cog, _member, _http = _cog(tmp_path)

        async def boom(now=None):
            raise RuntimeError("boom")

        cog.tick = boom
        await Costume.costume_loop.coro(cog)

    asyncio.run(run())


def test_loop_waits_until_ready(tmp_path):
    async def run():
        cog, member, _http = _cog(
            tmp_path,
            apply_at=datetime(2099, 1, 1, tzinfo=PT),
            revert_at=datetime(2099, 1, 2, tzinfo=PT),
        )
        await cog.cog_load()
        try:
            assert cog.costume_loop.is_running()
            await cog.before_costume_loop()
        finally:
            cog.costume_loop.cancel()
            task = cog.costume_loop.get_task()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        assert member.edits == []

    asyncio.run(run())
