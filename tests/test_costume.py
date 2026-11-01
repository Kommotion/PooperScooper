"""Costume schedule, announcement, retries, and owner prefix commands. No network."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView
from PIL import Image

from cogs.costume import (
    ANNOUNCEMENT_ASSET,
    ANNOUNCEMENT_LIMIT,
    APPLY_AT,
    APPLY_REASON,
    AVATAR_ASSET,
    BIO_ASSET,
    BIO_LIMIT,
    COSTUME_AVATAR_FILE,
    COSTUME_GUILD_IDS,
    COSTUME_USAGE,
    MTS_GUILD_ID,
    NICK_ID,
    PT,
    REVERT_AT,
    REVERT_REASON,
    STATE_KEYS,
    SUPPORT_GUILD_ID,
    Costume,
    _costume_allowed,
    announce_is_due,
    apply_is_open,
    as_utc,
    avatar_hash_from_payload,
    backoff_delay,
    bio_from_member_payload,
    block_message,
    default_state,
    due_action,
    find_announcement_channel,
    format_pt,
    iso,
    load_announcement_text,
    load_avatar_bytes,
    load_bio_text,
    normalize_state,
    parse_apply_spec,
    rate_limit_retry_after,
    record_apply_success,
    record_revert_success,
    render_status,
    resolve_avatar_file,
    result_message,
    revert_is_open,
    revert_payload,
    select_guild_avatar_asset,
    should_snapshot,
)
from PooperScooper import initial_extensions

BEFORE = APPLY_AT - timedelta(seconds=1)
MID = datetime(2026, 10, 31, 15, 0, tzinfo=PT)
CATCH_UP = APPLY_AT + timedelta(hours=8)
AFTER = datetime(2026, 11, 1, 8, 0, tzinfo=PT)
ANNOUNCE_CHANNEL_ID = 555001
EXPECTED_BIO = (
    "Good boy by day, spooky shepherd by night. Candy drops in MTS until 6 AM - "
    "first click wins. /candy to check your stash. /halloweenify to join the costume party."
)


def _state(**overrides) -> dict:
    state = default_state()
    state.update(overrides)
    return state


def _http_429(retry_after: str = "7200") -> discord.HTTPException:
    response = SimpleNamespace(
        status=429,
        reason="Too Many Requests",
        headers={"Retry-After": retry_after},
    )
    return discord.HTTPException(
        response,
        {"message": "You are being rate limited.", "retry_after": float(retry_after)},
    )


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


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, *, error: BaseException | None = None):
        self.id = channel_id
        self.name = name
        self.sent: list[tuple] = []
        self.error = error

    async def send(self, content=None, **kwargs):
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=9000 + len(self.sent))


class FakeMember:
    def __init__(self, *, avatar=None, fail: int = 0, error: BaseException | None = None):
        self.guild_avatar = avatar
        self.id = 4242
        self.guild = SimpleNamespace(id=MTS_GUILD_ID)
        self._state = None
        self.edits: list[dict] = []
        self.disk_during_edit: list[dict] = []
        self.fail = fail
        self.error = error
        self.cog = None

    async def edit(self, **kwargs):
        if self.cog is not None:
            self.disk_during_edit.append(json.loads(self.cog.store.path.read_text(encoding="utf-8")))
        if self.fail:
            self.fail -= 1
            raise self.error if self.error is not None else RuntimeError("HTTP 429")
        self.edits.append(kwargs)


class FakeGuild:
    def __init__(self, member: FakeMember, channels: list[FakeTextChannel]):
        self.me = member
        self.id = MTS_GUILD_ID
        self.text_channels = list(channels)
        self.fetch_calls = 0

    async def fetch_member(self, user_id: int):
        self.fetch_calls += 1
        raise AssertionError("cached member should be used")


class FakeBot:
    def __init__(self, guild, http):
        self._guild = guild
        self.http = http
        self.user = SimpleNamespace(id=4242)
        self._before_invoke = None
        self._after_invoke = None
        self.is_owner = AsyncMock(return_value=False)

    def get_guild(self, guild_id: int):
        if self._guild is None or guild_id != MTS_GUILD_ID:
            return None
        return self._guild

    def get_cog(self, name):
        return None

    async def can_run(self, ctx, *, call_once=False):
        return True

    async def wait_until_ready(self):
        return None


def _cog(tmp_path, *, member: FakeMember | None = None, http: FakeHTTP | None = None, guild=True, channels=None, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    avatar_path = tmp_path / "avatar.png"
    bio_path = tmp_path / "bio.txt"
    announcement_path = tmp_path / "announcement.md"
    if not avatar_path.exists():
        avatar_path.write_bytes(kwargs.pop("avatar_bytes", b"PNG-COSTUME"))
    else:
        kwargs.pop("avatar_bytes", None)
    if not bio_path.exists():
        bio_path.write_text(kwargs.pop("bio_text", "Count Pooper\n"), encoding="utf-8")
    else:
        kwargs.pop("bio_text", None)
    if not announcement_path.exists():
        announcement_path.write_text(
            kwargs.pop("announcement_text", "Hunt is on.\n/candy and /halloweenify\n"),
            encoding="utf-8",
        )
    else:
        kwargs.pop("announcement_text", None)
    if channels is None:
        channels = [FakeTextChannel(ANNOUNCE_CHANNEL_ID, "announcements")]
    channel = next((item for item in channels if item.name == "announcements"), None)
    if channel is None and channels:
        channel = channels[0]
    member = member if member is not None else FakeMember()
    http = http if http is not None else FakeHTTP({"avatar": None, "bio": None})
    fake_guild = FakeGuild(member, channels) if guild else None
    if fake_guild is not None:
        member.guild = fake_guild
    bot = FakeBot(fake_guild, http)
    cog = Costume(
        bot,
        path=tmp_path / "costume.json",
        avatar_path=avatar_path,
        bio_path=bio_path,
        announcement_path=announcement_path,
        backup_path=tmp_path / "costume_backup_avatar.png",
        announcement_channel_id=kwargs.pop("announcement_channel_id", None),
        announcement_channel_name=kwargs.pop("announcement_channel_name", "announcements"),
        **kwargs,
    )
    member.cog = cog
    return cog, member, http, channel


def _bind(cog: Costume) -> None:
    for command in cog.walk_commands():
        command.cog = cog


def _ctx(cog: Costume, user_id: int = NICK_ID, *, guild_id: int | None = MTS_GUILD_ID):
    author = SimpleNamespace(id=user_id, bot=False, send=AsyncMock())
    ctx = SimpleNamespace(
        author=author,
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        channel=None,
        bot=cog.bot,
        cog=cog,
        reply=AsyncMock(),
        send=AsyncMock(),
        command=cog.costume,
        prefix="!",
        invoked_with="costume",
        invoked_parents=[],
        invoked_subcommand=None,
        subcommand_passed=None,
        args=[],
        kwargs={},
        view=StringView(""),
    )
    ctx.message = SimpleNamespace(attachments=[], author=author, guild=ctx.guild, channel=None, id=1)
    return ctx


def _replied(ctx):
    args, kwargs = ctx.reply.await_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def _assert_silent(ctx) -> None:
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()


def _mentions_are_off(kwargs: dict) -> None:
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


def test_state_schema_and_normalize():
    assert set(default_state()) == set(STATE_KEYS)
    cleaned = normalize_state(
        {
            "apply_attempted": "yes",
            "applied_at": 5,
            "had_guild_bio": True,
            "original_bio": "x",
            "apply_failures": 2,
            "announce_message_id": 99,
            "extra": 1,
        }
    )
    assert cleaned["apply_attempted"] is False
    assert cleaned["applied_at"] is None
    assert cleaned["had_guild_bio"] is True
    assert cleaned["original_bio"] == "x"
    assert cleaned["apply_failures"] == 2
    assert cleaned["announce_message_id"] == "99"
    assert "extra" not in cleaned
    assert normalize_state(["nope"]) == default_state()


def test_schedule_decisions():
    fresh = default_state()
    applied = record_apply_success(fresh, MID)
    reverted = record_revert_success(applied, AFTER)
    waiting = _state(
        apply_error="HTTP 429",
        apply_failures=1,
        apply_retry_at=iso(MID + timedelta(minutes=5)),
    )
    stale_failure = _state(
        apply_attempted=True,
        apply_error="nope",
        apply_failures=1,
        apply_retry_at=iso(MID),
    )

    assert due_action(BEFORE, fresh) == "none"
    assert announce_is_due(BEFORE, fresh) is False

    assert due_action(APPLY_AT, fresh) == "apply"
    assert announce_is_due(APPLY_AT, fresh) is True
    assert due_action(APPLY_AT.astimezone(timezone.utc), fresh) == "apply"
    assert due_action(MID, fresh) == "apply"
    assert announce_is_due(MID, fresh) is True
    assert due_action(CATCH_UP, fresh) == "apply"
    assert announce_is_due(CATCH_UP, fresh) is True
    assert due_action(REVERT_AT - timedelta(seconds=1), fresh) == "apply"

    assert due_action(MID, waiting) == "none"
    assert due_action(MID + timedelta(minutes=5), waiting) == "apply"
    # A crash after the attempt flag, with no retry time, is retried inside the window.
    assert due_action(MID, _state(apply_attempted=True)) == "apply"

    assert due_action(REVERT_AT, fresh) == "none"
    assert announce_is_due(REVERT_AT, fresh) is False
    assert due_action(AFTER, fresh) == "none"
    assert announce_is_due(AFTER, fresh) is False
    assert due_action(AFTER, stale_failure) == "none"
    assert announce_is_due(AFTER, stale_failure) is False

    assert due_action(REVERT_AT, applied) == "revert"
    assert announce_is_due(REVERT_AT, applied) is False
    assert due_action(AFTER, applied) == "revert"
    assert due_action(AFTER, reverted) == "none"
    assert due_action(MID, reverted) == "none"
    assert announce_is_due(MID, reverted) is False
    assert apply_is_open(reverted, force=True) is False
    assert due_action(MID, reverted, manual="apply", force=True) == "none"
    assert revert_is_open(reverted, ignore_backoff=True) is False
    assert revert_is_open(applied, now=AFTER) is True

    assert due_action(BEFORE, fresh, manual="apply") == "apply"
    assert due_action(AFTER, fresh, manual="revert") == "none"
    assert due_action(MID, applied, manual="revert") == "revert"
    assert due_action(MID, reverted, manual="revert") == "none"

    with pytest.raises(ValueError):
        due_action(datetime(2026, 10, 31, 0, 0), fresh)
    with pytest.raises(ValueError):
        due_action(MID, fresh, manual="dance")


def test_backoff_and_rate_limit_delay():
    assert backoff_delay(1) == timedelta(minutes=5)
    assert backoff_delay(2) == timedelta(minutes=15)
    assert backoff_delay(3) == timedelta(minutes=30)
    assert backoff_delay(4) == timedelta(minutes=60)
    assert backoff_delay(9) == timedelta(minutes=60)
    assert backoff_delay(1, 10) == timedelta(minutes=5)
    assert backoff_delay(1, 7200) == timedelta(seconds=7200)

    limited = discord.RateLimited(7200)
    assert rate_limit_retry_after(limited) == 7200.0
    http_exc = _http_429("7200")
    assert rate_limit_retry_after(http_exc) == 7200.0
    other = discord.HTTPException(SimpleNamespace(status=500, reason="Error", headers={}), "nope")
    assert rate_limit_retry_after(other) is None
    assert parse_apply_spec("Force ANNOUNCE") == (True, True)
    assert parse_apply_spec("") == (False, False)
    assert parse_apply_spec("dance") is None


def test_success_records_keep_restore_and_block_reapply():
    applied = record_apply_success(
        _state(had_guild_avatar=True, had_guild_bio=True, original_bio="old"),
        MID,
    )
    assert applied["apply_attempted"] is True
    assert applied["applied_at"]
    assert applied["revert_attempted"] is False
    assert applied["had_guild_avatar"] is True
    assert applied["original_bio"] == "old"
    assert due_action(MID, applied) == "none"
    assert due_action(AFTER, applied) == "revert"

    reverted = record_revert_success(applied, AFTER)
    assert reverted["reverted_at"]
    assert reverted["applied_at"] == applied["applied_at"]
    assert due_action(AFTER, reverted) == "none"
    assert due_action(MID, reverted) == "none"
    assert apply_is_open(reverted, force=True) is False


def test_messages_and_status_use_pacific_time():
    assert format_pt(APPLY_AT) == "2026-10-31 10:00 PT"
    assert format_pt(REVERT_AT) == "2026-11-01 06:00 PT"
    text = render_status(
        _state(apply_error="boom"),
        now=BEFORE,
        avatar_name="gsd_costume_1.png",
        avatar_ready=True,
        avatar_note="default",
        bio_ready=False,
        announcement_ready=True,
        backup_ready=False,
        announcement_channel="#announcements (42)",
    )
    assert "2026-10-31 10:00 PT" in text
    assert "2026-11-01 06:00 PT" in text
    assert "Due now: none" in text
    assert "apply_error: boom" in text
    assert "gsd_costume_1.png (present, default)" in text
    assert "bio.txt: missing" in text
    assert "announcement.md: present" in text
    assert "#announcements (42)" in text
    assert "Menaces to Sobriety" in text
    assert "won't apply it again" in block_message(_state(reverted_at="x", applied_at="y"), manual="apply", force=True)
    assert "never applied" in block_message(default_state(), manual="revert", force=False)
    assert "backoff" in result_message("apply", "error", "HTTP 429")


def test_real_costume_assets_and_limits():
    raw_bio = BIO_ASSET.read_bytes()
    assert not raw_bio.startswith(b"\xef\xbb\xbf")
    bio_text = raw_bio.decode("utf-8")
    assert "\ufeff" not in bio_text
    assert "\ufffd" not in bio_text
    assert len(bio_text) <= BIO_LIMIT
    assert load_bio_text(BIO_ASSET) == EXPECTED_BIO
    assert len(EXPECTED_BIO) <= BIO_LIMIT

    raw_announcement = ANNOUNCEMENT_ASSET.read_bytes()
    assert not raw_announcement.startswith(b"\xef\xbb\xbf")
    announcement_text = raw_announcement.decode("utf-8")
    assert "\ufffd" not in announcement_text
    posted = load_announcement_text(ANNOUNCEMENT_ASSET)
    assert posted == announcement_text.strip()
    assert len(posted) <= ANNOUNCEMENT_LIMIT
    assert len(posted) <= 1200
    assert "/candy" in posted
    assert "/halloweenify" in posted
    assert "<#1373140173067653120>" in posted
    folded = posted.casefold()
    assert "@everyone" not in folded
    assert "@here" not in folded
    assert COSTUME_AVATAR_FILE == "gsd_costume_1.png"
    assert AVATAR_ASSET.name == COSTUME_AVATAR_FILE
    if AVATAR_ASSET.is_file():
        avatar = load_avatar_bytes(AVATAR_ASSET)
        assert avatar.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(avatar)) as image:
            assert image.size[0] > 0 and image.size[1] > 0


def test_announcement_length_guard(tmp_path):
    path = tmp_path / "announcement.md"
    path.write_text("x" * ANNOUNCEMENT_LIMIT, encoding="utf-8")
    assert load_announcement_text(path) == "x" * ANNOUNCEMENT_LIMIT
    path.write_text("y" * (ANNOUNCEMENT_LIMIT + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="2000"):
        load_announcement_text(path)


def test_bio_loader_strips_bom_and_rejects_empty_or_long(tmp_path):
    path = tmp_path / "bio.txt"
    path.write_bytes("\ufeff\n  spooky \n".encode("utf-8"))
    assert load_bio_text(path) == "spooky"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_bio_text(path)
    path.write_text("z" * (BIO_LIMIT + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="190"):
        load_bio_text(path)
    with pytest.raises(FileNotFoundError):
        load_avatar_bytes(tmp_path / "missing.png")


def test_avatar_settings_fall_back(tmp_path, caplog):
    halloween = tmp_path / "halloween"
    halloween.mkdir()
    (halloween / "gsd_costume_1.png").write_bytes(b"one")
    (halloween / "gsd_costume_3.png").write_bytes(b"three")
    settings = tmp_path / "settings.json"
    assert resolve_avatar_file(settings, halloween, "gsd_costume_1.png") == "gsd_costume_1.png"

    settings.write_bytes(b'\xef\xbb\xbf{"avatar": "gsd_costume_3.png"}')
    assert resolve_avatar_file(settings, halloween, "gsd_costume_1.png") == "gsd_costume_3.png"

    settings.write_text('{"avatar": "gsd_costume_4.png"}', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert resolve_avatar_file(settings, halloween, "gsd_costume_1.png") == "gsd_costume_1.png"
    assert "missing" in caplog.text.lower()

    for raw in ('{"avatar": "../secret.png"}', '{"avatar": "gsd_costume_3.png/../../x.png"}', "{", "[]", '{"avatar": ""}'):
        caplog.clear()
        settings.write_text(raw, encoding="utf-8")
        assert resolve_avatar_file(settings, halloween, "gsd_costume_1.png") == "gsd_costume_1.png"

    settings.write_text('{"avatar": "gsd_costume_3.png"}', encoding="utf-8")
    bot = FakeBot(None, FakeHTTP({}))
    cog = Costume(
        bot,
        path=tmp_path / "costume.json",
        bio_path=tmp_path / "bio.txt",
        announcement_path=tmp_path / "announcement.md",
        backup_path=tmp_path / "backup.png",
        settings_path=settings,
        halloween_dir=halloween,
        announcement_channel_id=None,
        announcement_channel_name="announcements",
    )
    assert cog.avatar_path.name == "gsd_costume_3.png"
    assert cog.avatar_from_settings is True
    assert cog.avatar_path.read_bytes() == b"three"


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


def test_prefix_group_is_hidden_and_has_no_app_commands(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "PooperScooper.py").read_text(encoding="utf-8")
    assert "cogs.costume" in initial_extensions
    assert "cogs.costume" in source
    assert "app_commands" not in Path(Costume.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8") if False else True
    costume_source = Path(__file__).resolve().parents[1].joinpath("cogs", "costume.py").read_text(encoding="utf-8")
    assert "app_commands" not in costume_source

    group = Costume.costume
    assert isinstance(group, commands.Group)
    assert not isinstance(group, discord.app_commands.Group)
    assert group.name == "costume"
    assert group.hidden is True
    assert group.invoke_without_command is True
    assert group.case_insensitive is True
    names = {command.name for command in group.commands}
    assert names == {"status", "preview", "apply", "restore"}
    assert all(command.hidden for command in group.commands)
    assert group.checks[0] is _costume_allowed
    assert all(command.checks[0] is _costume_allowed for command in group.commands)
    assert list(Costume.__cog_app_commands__) == []

    cog, _member, _http, _channel = _cog(tmp_path)
    assert cog.get_app_commands() == []
    assert list(cog.walk_app_commands()) == []
    assert {command.name for command in cog.costume.commands} == {"status", "preview", "apply", "restore"}
    assert NICK_ID == 87324314084331520
    assert SUPPORT_GUILD_ID == 1045144253627637830
    assert COSTUME_GUILD_IDS == (MTS_GUILD_ID, SUPPORT_GUILD_ID)
    assert Costume.costume_loop.seconds == 60


def test_owner_gate_is_silent(tmp_path):
    async def run():
        cog, _member, _http, _channel = _cog(tmp_path)
        _bind(cog)

        nick = _ctx(cog, NICK_ID, guild_id=MTS_GUILD_ID)
        assert await _costume_allowed(nick) is True
        assert await cog.costume.can_run(nick) is True
        cog.bot.is_owner.assert_not_awaited()

        support = _ctx(cog, NICK_ID, guild_id=SUPPORT_GUILD_ID)
        assert await cog.costume_status.can_run(support) is True
        dm = _ctx(cog, NICK_ID, guild_id=None)
        assert await cog.costume_preview.can_run(dm) is True

        cog.bot.is_owner = AsyncMock(return_value=True)
        bot_owner = _ctx(cog, 9, guild_id=MTS_GUILD_ID)
        assert await cog.costume_apply.can_run(bot_owner) is True

        cog.bot.is_owner = AsyncMock(return_value=False)
        stranger = _ctx(cog, 5, guild_id=MTS_GUILD_ID)
        assert await cog.costume_status.can_run(stranger) is False
        await cog.costume_status.callback(cog, stranger)
        await cog.costume_apply.callback(cog, stranger, spec="force")
        await cog.costume.callback(cog, stranger)
        await cog.cog_command_error(stranger, commands.CheckFailure("no"))
        _assert_silent(stranger)
        stranger.author.send.assert_not_awaited()

        elsewhere = _ctx(cog, NICK_ID, guild_id=1)
        assert await cog.costume.can_run(elsewhere) is False
        await cog.costume_status.callback(cog, elsewhere)
        _assert_silent(elsewhere)

        stranger.view = StringView("status")
        try:
            await cog.costume.invoke(stranger)
        except commands.CheckFailure as exc:
            await cog.cog_command_error(stranger, exc)
        else:
            raise AssertionError("non-owner invoke should fail the owner check")
        _assert_silent(stranger)

    asyncio.run(run())


def test_non_costume_check_failure_is_not_swallowed(tmp_path, caplog):
    async def run():
        cog, _member, _http, _channel = _cog(tmp_path)
        ctx = _ctx(cog)
        ctx.command = SimpleNamespace(name="ping", parent=None)
        with caplog.at_level(logging.ERROR):
            await cog.cog_command_error(ctx, commands.CheckFailure("no"))
        assert "Costume command failed" in caplog.text
        _assert_silent(ctx)

    asyncio.run(run())


def test_apply_and_announce_once_inside_the_window(tmp_path):
    async def run():
        cog, member, http, channel = _cog(tmp_path)
        assert await cog.tick(now=BEFORE) == "none"
        assert member.edits == []
        assert channel.sent == []
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
        assert len(channel.sent) == 1
        text, kwargs = channel.sent[0]
        assert text == "Hunt is on.\n/candy and /halloweenify"
        _mentions_are_off(kwargs)
        assert cog.state["announce_attempted"] is True
        assert cog.state["announced_at"]
        assert cog.state["announce_message_id"] == "9001"
        assert cog.state["announce_error"] is None
        saved = json.loads(cog.store.path.read_text(encoding="utf-8"))
        assert set(saved) == set(STATE_KEYS)
        assert saved["announce_message_id"] == "9001"
        assert http.probes == 0

        assert await cog.tick(now=MID) == "none"
        assert len(member.edits) == 1
        assert len(channel.sent) == 1
        assert http.gets == 1

    asyncio.run(run())


def test_catch_up_after_downtime_applies_and_announces(tmp_path, monkeypatch):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        assert CATCH_UP < REVERT_AT
        assert await cog.tick(now=CATCH_UP) == "applied"
        assert len(member.edits) == 1
        assert len(channel.sent) == 1

        fresh, fresh_member, _http2, fresh_channel = _cog(tmp_path / "loop")
        monkeypatch.setattr("cogs.costume.discord.utils.utcnow", lambda: MID)
        await Costume.costume_loop.coro(fresh)
        assert len(fresh_member.edits) == 1
        assert len(fresh_channel.sent) == 1

    asyncio.run(run())


def test_past_revert_never_applied_skips_both(tmp_path, monkeypatch):
    async def run():
        cog, member, http, channel = _cog(tmp_path)
        assert await cog.tick(now=AFTER) == "none"
        assert member.edits == []
        assert channel.sent == []
        assert http.gets == 0
        assert cog.state == default_state()

        loop_cog, loop_member, _http, loop_channel = _cog(tmp_path / "loop")
        monkeypatch.setattr("cogs.costume.discord.utils.utcnow", lambda: AFTER)
        await Costume.costume_loop.coro(loop_cog)
        assert loop_member.edits == []
        assert loop_channel.sent == []

    asyncio.run(run())


def test_revert_is_due_and_catches_up(tmp_path, monkeypatch):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        assert await cog.tick(now=MID) == "applied"
        assert await cog.tick(now=REVERT_AT) == "reverted"
        assert member.edits[1]["avatar"] is None
        assert member.edits[1]["bio"] is None
        assert member.edits[1]["reason"] == REVERT_REASON
        assert len(channel.sent) == 1
        assert await cog.tick(now=AFTER) == "none"

        late, late_member, _http2, late_channel = _cog(tmp_path / "late")
        assert await late.tick(now=MID) == "applied"
        monkeypatch.setattr("cogs.costume.discord.utils.utcnow", lambda: AFTER)
        await Costume.costume_loop.coro(late)
        assert late.state["reverted_at"]
        assert len(late_member.edits) == 2
        assert len(late_channel.sent) == 1

    asyncio.run(run())


def test_announcement_posts_once_across_restarts(tmp_path):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        assert await cog.tick(now=MID) == "applied"
        message_id = cog.state["announce_message_id"]
        assert len(channel.sent) == 1

        cog2, member2, _http2, channel2 = _cog(tmp_path)
        assert cog2.state["announced_at"]
        assert cog2.state["announce_attempted"] is True
        assert cog2.state["announce_message_id"] == message_id
        assert cog2.state["applied_at"]
        assert await cog2.tick(now=MID) == "none"
        assert member2.edits == []
        assert channel2.sent == []
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_announcement_is_read_at_post_time(tmp_path):
    async def run():
        cog, member, _http, _channel = _cog(tmp_path, channels=[])
        cog.announcement_path.write_text("stale /candy /halloweenify\n", encoding="utf-8")
        assert await cog.tick(now=MID) == "applied"
        assert member.edits
        assert cog.state["announced_at"] is None
        assert cog.state["announce_attempted"] is False
        cog.announcement_path.write_text("fresh /candy /halloweenify\n", encoding="utf-8")
        channel = FakeTextChannel(77, "announcements")
        cog._guild().text_channels.append(channel)
        assert await cog.tick(now=MID) == "announced"
        assert channel.sent[0][0] == "fresh /candy /halloweenify"
        _mentions_are_off(channel.sent[0][1])
        assert await cog.tick(now=MID) == "none"
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_overlong_announcement_is_not_posted(tmp_path):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        cog.announcement_path.write_text("x" * (ANNOUNCEMENT_LIMIT + 1), encoding="utf-8")
        assert await cog.tick(now=MID) == "applied"
        assert member.edits
        assert channel.sent == []
        assert cog.state["announce_attempted"] is False
        assert cog.state["announced_at"] is None
        assert "2000" in cog.state["announce_error"]
        assert await cog.tick(now=MID) == "error"
        assert channel.sent == []

        cog.announcement_path.write_text("Ready /candy /halloweenify", encoding="utf-8")
        assert await cog.tick(now=MID) == "announced"
        assert channel.sent[0][0] == "Ready /candy /halloweenify"
        assert "@everyone" not in channel.sent[0][0].casefold()
        assert "@here" not in channel.sent[0][0].casefold()

    asyncio.run(run())


def test_apply_failure_still_announces_once_and_retries_with_backoff(tmp_path):
    async def run():
        member = FakeMember(fail=4)
        cog, member, _http, channel = _cog(tmp_path, member=member)
        now = MID
        for minutes in (5, 15, 30, 60):
            assert await cog.tick(now=now) == "error"
            retry_at = datetime.fromisoformat(cog.state["apply_retry_at"])
            assert as_utc(retry_at) - as_utc(now) == timedelta(minutes=minutes)
            assert cog.state["applied_at"] is None
            assert await cog.tick(now=retry_at - timedelta(seconds=1)) == "none"
            now = retry_at
        assert len(channel.sent) == 1
        assert await cog.tick(now=now) == "applied"
        assert len(member.edits) == 1
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_revert_retries_with_backoff(tmp_path):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        assert await cog.tick(now=MID) == "applied"
        member.fail = 1
        member.error = RuntimeError("revert nope")
        assert await cog.tick(now=AFTER) == "error"
        assert cog.state["reverted_at"] is None
        assert cog.state["revert_failures"] == 1
        retry_at = datetime.fromisoformat(cog.state["revert_retry_at"])
        assert as_utc(retry_at) - as_utc(AFTER) == timedelta(minutes=5)
        assert await cog.tick(now=retry_at - timedelta(seconds=1)) == "none"
        assert len(member.edits) == 1
        assert await cog.tick(now=retry_at) == "reverted"
        assert len(member.edits) == 2
        assert len(channel.sent) == 1
        assert await cog.tick(now=AFTER) == "none"

    asyncio.run(run())


def test_rate_limit_retry_after_is_respected(tmp_path, caplog):
    async def run():
        cases = (_http_429("7200"), discord.RateLimited(7200))
        for index, exc in enumerate(cases):
            folder = tmp_path / str(index)
            folder.mkdir()
            member = FakeMember(fail=1, error=exc)
            cog, member, _http, channel = _cog(folder, member=member)
            with caplog.at_level(logging.WARNING, logger="cogs.costume"):
                assert await cog.tick(now=MID) == "error"
            assert "retry_after=7200" in caplog.text
            assert "rate limited" in caplog.text.lower()
            caplog.clear()
            retry_at = datetime.fromisoformat(cog.state["apply_retry_at"])
            assert as_utc(retry_at) - as_utc(MID) == timedelta(seconds=7200)
            assert await cog.tick(now=MID + timedelta(minutes=30)) == "none"
            assert member.edits == []
            assert await cog.tick(now=AFTER) == "none"
            assert member.edits == []
            assert await cog.tick(now=MID + timedelta(seconds=7200)) == "applied"
            assert len(member.edits) == 1
            assert len(channel.sent) == 1

    asyncio.run(run())


def test_never_reapply_after_restore(tmp_path):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        assert await cog.tick(now=MID) == "applied"
        assert await cog.tick(now=AFTER) == "reverted"
        assert await cog.tick(now=MID) == "none"
        assert await cog.tick(now=APPLY_AT) == "none"
        message = await cog._manual("apply", force=True, announce=True, now=MID)
        assert "won't apply it again" in message
        assert "won't post the announcement" in message
        assert len(member.edits) == 2
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_missing_guild_does_not_burn_the_attempt(tmp_path):
    async def run():
        cog, member, http, _channel = _cog(tmp_path, guild=False)
        assert await cog.tick(now=MID) == "unavailable"
        assert cog.state["apply_attempted"] is False
        assert cog.state["announce_attempted"] is False
        assert http.gets == 0
        assert await cog.tick(now=MID) == "unavailable"
        assert member.edits == []

    asyncio.run(run())


def test_existing_profile_is_backed_up_and_restored(tmp_path):
    async def run():
        original = b"GIF89a-original"
        asset = FakeAsset(original, key="hash1")
        member = FakeMember(avatar=asset)
        http = FakeHTTP({"avatar": "hash1", "bio": "  old bio  "})
        cog, member, http, channel = _cog(tmp_path, member=member, http=http)

        assert await cog.tick(now=MID) == "applied"
        assert cog.backup_path.read_bytes() == original
        assert asset.reads == 1
        assert cog.state["had_guild_avatar"] is True
        assert cog.state["had_guild_bio"] is True
        assert cog.state["original_bio"] == "old bio"
        assert http.probes == 0
        assert member.edits[0]["avatar"] == b"PNG-COSTUME"
        assert len(channel.sent) == 1

        backup = cog.backup_path.read_bytes()
        await cog._manual("apply", force=True, now=MID)
        assert cog.backup_path.read_bytes() == backup
        assert len(member.edits) == 2

        assert await cog.tick(now=AFTER) == "reverted"
        assert member.edits[2]["avatar"] == original
        assert member.edits[2]["bio"] == "old bio"
        assert member.edits[2]["reason"] == REVERT_REASON
        during = member.disk_during_edit[2]
        assert during["revert_attempted"] is True
        assert during["reverted_at"] is None
        assert cog.state["reverted_at"]
        assert await cog.tick(now=AFTER) == "none"
        assert len(member.edits) == 3
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_empty_profile_revert_clears_overrides(tmp_path):
    async def run():
        cog, member, _http, _channel = _cog(tmp_path)
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
        cog, member, _http, _channel = _cog(tmp_path, member=member, http=http)
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
        cog, member, _http, _channel = _cog(tmp_path, member=member, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert cog.backup_path.read_bytes() == b"from-cdn"
        assert cog.state["had_guild_avatar"] is True
        url = member._state.http.get_from_cdn.await_args.args[0]
        assert "cafebabe" in url

    asyncio.run(run())


def test_missing_bio_key_is_probed_once(tmp_path):
    async def run():
        http = FakeHTTP({"avatar": None}, probe={"bio": "  saved bio  "})
        cog, member, http, _channel = _cog(tmp_path, http=http)
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
        cog, member, http, _channel = _cog(tmp_path, http=http)
        assert await cog.tick(now=MID) == "applied"
        assert http.probes == 1
        assert cog.state["had_guild_bio"] is False
        assert cog.state["original_bio"] is None
        assert len(member.edits) == 1

    asyncio.run(run())


def test_missing_backup_retries_then_restores(tmp_path):
    async def run():
        asset = FakeAsset(b"ORIGINAL", key="hash1")
        member = FakeMember(avatar=asset)
        http = FakeHTTP({"avatar": "hash1", "bio": None})
        cog, member, _http, _channel = _cog(tmp_path, member=member, http=http)
        assert await cog.tick(now=MID) == "applied"
        cog.backup_path.unlink()
        assert await cog.tick(now=AFTER) == "error"
        assert len(member.edits) == 1
        assert cog.state["revert_attempted"] is True
        assert "Backup guild avatar" in cog.state["revert_error"]
        assert await cog.tick(now=AFTER) == "none"
        cog.backup_path.write_bytes(b"ORIGINAL")
        retry_at = datetime.fromisoformat(cog.state["revert_retry_at"])
        assert await cog.tick(now=retry_at) == "reverted"
        assert member.edits[1]["avatar"] == b"ORIGINAL"

    asyncio.run(run())


def test_missing_asset_retries_without_burning_the_window(tmp_path):
    async def run():
        cog, member, http, channel = _cog(tmp_path)
        cog.avatar_path.unlink()
        assert await cog.tick(now=MID) == "error"
        assert member.edits == []
        assert http.gets == 0
        assert cog.state["apply_attempted"] is True
        assert len(channel.sent) == 1
        assert await cog.tick(now=MID) == "none"
        cog.avatar_path.write_bytes(b"PNG-COSTUME")
        retry_at = datetime.fromisoformat(cog.state["apply_retry_at"])
        assert await cog.tick(now=retry_at) == "applied"
        assert len(member.edits) == 1
        assert len(channel.sent) == 1

    asyncio.run(run())


def test_manual_apply_does_not_announce_unless_asked(tmp_path):
    async def run():
        cog, member, _http, channel = _cog(tmp_path)
        quiet = await cog._manual("apply", now=BEFORE)
        assert quiet == "Halloween costume is on in Menaces to Sobriety."
        assert "Announcement" not in quiet
        assert len(member.edits) == 1
        assert channel.sent == []

        assert await cog.tick(now=MID) == "announced"
        assert len(member.edits) == 1
        assert len(channel.sent) == 1

        early, early_member, _http2, early_channel = _cog(tmp_path / "early")
        posted = await early._manual("apply", announce=True, now=BEFORE)
        assert posted.endswith("Announcement posted.")
        assert len(early_channel.sent) == 1
        assert await early.tick(now=MID) == "none"
        assert len(early_member.edits) == 1
        assert len(early_channel.sent) == 1

    asyncio.run(run())


def test_announcement_channel_uses_candy_hunt_finder(tmp_path, monkeypatch):
    async def run():
        general = FakeTextChannel(7, "general")
        news = FakeTextChannel(42, "town-crier")
        named = FakeTextChannel(8, "announcements")
        cog, _member, _http, _channel = _cog(
            tmp_path,
            channels=[general, news, named],
            announcement_channel_id=None,
            announcement_channel_name=None,
        )
        # Both overrides are explicit, so this call uses the name "announcements".
        cog._channel_id_override = cog._channel_id_override
        seen = {}
        real = find_announcement_channel

        def spy(channels, *, channel_id, name):
            seen["channels"] = list(channels)
            seen["channel_id"] = channel_id
            seen["name"] = name
            return real(channels, channel_id=channel_id, name=name)

        monkeypatch.setattr("cogs.costume.find_announcement_channel", spy)
        cog.bot.get_cog = lambda name: SimpleNamespace(
            settings=SimpleNamespace(announcement_channel_id=42, announcement_channel_name="town-crier")
        )
        cog._channel_id_override = type(cog)._channel_id_override if False else cog._channel_id_override
        # Clear the test overrides so the candy-hunt settings are consulted.
        from cogs.costume import _UNSET

        cog._channel_id_override = _UNSET
        cog._channel_name_override = _UNSET
        assert await cog.tick(now=MID) == "applied"
        assert seen["channel_id"] == 42
        assert seen["name"] == "town-crier"
        assert news.sent
        assert general.sent == []
        assert named.sent == []
        _mentions_are_off(news.sent[0][1])

    asyncio.run(run())


def test_preview_status_and_prefix_commands(tmp_path):
    async def run():
        cog, member, http, channel = _cog(tmp_path)
        _bind(cog)

        preview = _ctx(cog)
        await cog.costume_preview.callback(cog, preview)
        send_args, send_kwargs = preview.author.send.await_args
        body = send_args[0]
        assert "Count Pooper" in body
        assert "/candy and /halloweenify" in body
        assert send_kwargs["file"].filename == "avatar.png"
        _mentions_are_off(send_kwargs)
        reply, reply_kwargs = _replied(preview)
        assert reply == "Sent you a preview in DMs"
        assert reply_kwargs["mention_author"] is False
        assert member.edits == []
        assert channel.sent == []
        assert http.gets == 0

        closed = _ctx(cog)
        closed.author.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "Cannot send messages to this user",
        )
        await cog.costume_preview.callback(cog, closed)
        failure, _kwargs = _replied(closed)
        assert failure != "Sent you a preview in DMs"
        assert "DM" in failure
        assert member.edits == []

        status = _ctx(cog)
        await cog.costume_status.callback(cog, status)
        text, status_kwargs = _replied(status)
        assert "2026-10-31 10:00 PT" in text
        assert "2026-11-01 06:00 PT" in text
        assert "avatar.png" in text
        assert f"#announcements ({ANNOUNCE_CHANNEL_ID})" in text
        assert status_kwargs["mention_author"] is False
        assert member.edits == []

        bare = _ctx(cog)
        bare.view = StringView("")
        await cog.costume.invoke(bare)
        usage, usage_kwargs = _replied(bare)
        assert usage == COSTUME_USAGE
        assert usage_kwargs["mention_author"] is False

        unknown = _ctx(cog)
        unknown.view = StringView("nope")
        await cog.costume.invoke(unknown)
        usage, _kwargs = _replied(unknown)
        assert usage == COSTUME_USAGE
        assert "nope" not in usage

        bad = _ctx(cog)
        bad.view = StringView("apply dance")
        await cog.costume.invoke(bad)
        usage, _kwargs = _replied(bad)
        assert usage == COSTUME_USAGE
        assert member.edits == []
        assert channel.sent == []

        apply = _ctx(cog)
        apply.view = StringView("apply")
        await cog.costume.invoke(apply)
        message, message_kwargs = _replied(apply)
        assert message == "Halloween costume is on in Menaces to Sobriety."
        assert message_kwargs["mention_author"] is False
        assert len(member.edits) == 1
        assert channel.sent == []

        again = _ctx(cog)
        again.view = StringView("apply")
        await cog.costume.invoke(again)
        message, _kwargs = _replied(again)
        assert "already applied" in message.lower()
        assert len(member.edits) == 1

        forced = _ctx(cog)
        forced.view = StringView("apply FORCE")
        await cog.costume.invoke(forced)
        assert len(member.edits) == 2
        assert channel.sent == []

        announced = _ctx(cog)
        announced.view = StringView("apply announce")
        await cog.costume.invoke(announced)
        message, _kwargs = _replied(announced)
        assert "already applied" in message.lower()
        assert message.endswith("Announcement posted.")
        assert len(channel.sent) == 1
        _mentions_are_off(channel.sent[0][1])
        assert len(member.edits) == 2

        duplicate = _ctx(cog)
        duplicate.view = StringView("apply force announce")
        await cog.costume.invoke(duplicate)
        message, _kwargs = _replied(duplicate)
        assert "Announcement was already posted." in message
        assert len(channel.sent) == 1
        assert len(member.edits) == 3

        early_restore = _cog(tmp_path / "restore")
        restore_cog, restore_member, _http, restore_channel = early_restore
        _bind(restore_cog)
        too_soon = _ctx(restore_cog)
        too_soon.view = StringView("restore")
        await restore_cog.costume.invoke(too_soon)
        message, _kwargs = _replied(too_soon)
        assert "never applied" in message
        assert restore_member.edits == []

        await restore_cog._manual("apply", now=BEFORE)
        restore = _ctx(restore_cog)
        restore.view = StringView("restore")
        await restore_cog.costume.invoke(restore)
        message, _kwargs = _replied(restore)
        assert message == "Per-server profile restored in Menaces to Sobriety."
        assert len(restore_member.edits) == 2
        assert restore_channel.sent == []

    asyncio.run(run())


def test_loop_callback_swallows_errors(tmp_path):
    async def run():
        cog, _member, _http, _channel = _cog(tmp_path)

        async def boom(now=None):
            raise RuntimeError("boom")

        cog.tick = boom
        await Costume.costume_loop.coro(cog)

    asyncio.run(run())


def test_loop_waits_until_ready(tmp_path):
    async def run():
        cog, member, _http, _channel = _cog(
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
