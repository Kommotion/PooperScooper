"""Malfoyd queue, buttons, poll, and owner prefix commands. No network."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from cogs.malfoyd import (
    GUILD_ID,
    HEADING,
    MAX_REROLLS_PER_DAY,
    POLL_SECONDS,
    QUEUE_PATH,
    ROLE_NAME,
    SETTINGS_PATH,
    THREAD_ID,
    TZ,
    Malfoyd,
    build_content,
    current_candidate,
    is_valid_candidate,
    make_view,
    read_json,
    today_pt,
    valid_candidates,
    write_json,
    default_queue,
)
from cogs.utils.constants import DATA_DIR
from PooperScooper import initial_extensions

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)
TODAY = "2026-10-30"
VX = "https://vxtwitter.com/user/status/"


def _candidate(number: int, *, url: str | None = None, **extra) -> dict:
    item = {
        "id": str(number),
        "handle": f"user{number}",
        "vx_url": url if url is not None else f"{VX}{number}",
        "like_count": number,
    }
    item.update(extra)
    return item


def _queue(**overrides) -> dict:
    queue = {
        "date": TODAY,
        "posted": False,
        "message_id": 5001,
        "index": 0,
        "rerolls_used": 0,
        "candidates": [_candidate(1), _candidate(2), _candidate(3), _candidate(4)],
    }
    queue.update(overrides)
    return queue


def _http(status: int, reason: str, text: str = "nope"):
    response = SimpleNamespace(status=status, reason=reason)
    if status == 403:
        return discord.Forbidden(response, text)
    return discord.HTTPException(response, text)


class FakeRole:
    def __init__(self, role_id: int, name: str = ROLE_NAME):
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(self, user_id: int, guild, roles=None):
        self.id = user_id
        self.guild = guild
        self.roles = list(roles or [])
        self.added = []
        self.removed = []
        self.fail = None

    async def add_roles(self, *roles, reason=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.added.append((roles, reason))
        for role in roles:
            if not any(getattr(item, "id", None) == role.id for item in self.roles):
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.removed.append((roles, reason))
        remove_ids = {role.id for role in roles}
        self.roles = [item for item in self.roles if getattr(item, "id", None) not in remove_ids]


class FakeThread:
    def __init__(self, *, archived: bool = False, fail_edit: BaseException | None = None, fail_send: BaseException | None = None):
        self.id = THREAD_ID
        self.archived = archived
        self.sent = []
        self.edits = []
        self.archived_at_send = []
        self.fail_edit = fail_edit
        self.fail_send = fail_send
        self._next = 5000

    async def edit(self, **kwargs):
        if self.fail_edit is not None:
            raise self.fail_edit
        self.edits.append(kwargs)
        if "archived" in kwargs:
            self.archived = kwargs["archived"]

    async def send(self, content=None, **kwargs):
        if self.fail_send is not None:
            raise self.fail_send
        self.archived_at_send.append(self.archived)
        self._next += 1
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=self._next)


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID):
        self.id = guild_id
        self.roles = []
        self.thread = None
        self.members = {}
        self.created = []
        self.thread_lookups = 0
        self.fail_create = None

    def get_role(self, role_id: int):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_thread(self, thread_id: int):
        self.thread_lookups += 1
        if self.thread is not None and thread_id == THREAD_ID:
            return self.thread
        return None

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int):
        member = self.members.get(user_id)
        if member is None:
            raise discord.NotFound(_http(404, "Not Found").response, "unknown member")
        return member

    async def create_role(self, **kwargs):
        if self.fail_create is not None:
            raise self.fail_create
        role = FakeRole(8000 + len(self.created), kwargs.get("name", ROLE_NAME))
        self.created.append(kwargs)
        self.roles.append(role)
        return role


class FakeResponse:
    def __init__(self, fail_edit: BaseException | None = None):
        self.sent = []
        self.edits = []
        self.done = False
        self.fail_edit = fail_edit

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        if self.fail_edit is not None:
            raise self.fail_edit
        self.done = True
        self.edits.append(kwargs)


class FakeBot:
    def __init__(self, guild=None):
        self.guild = guild
        self.channel = None
        self.fetched = None
        self.fetch_calls = 0
        self.channel_calls = 0
        self.views = []
        self.is_owner = AsyncMock(return_value=False)
        self._before_invoke = None
        self._after_invoke = None
        self._cog = None
        self.user = SimpleNamespace(id=1)

    def get_guild(self, guild_id: int):
        if self.guild is not None and guild_id == self.guild.id:
            return self.guild
        return None

    def get_channel(self, channel_id: int):
        self.channel_calls += 1
        if channel_id == THREAD_ID:
            return self.channel
        return None

    async def fetch_channel(self, channel_id: int):
        self.fetch_calls += 1
        if channel_id != THREAD_ID or self.fetched is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown channel")
        return self.fetched

    async def fetch_guild(self, guild_id: int):
        return self.get_guild(guild_id)

    def add_view(self, view, *, message_id=None):
        self.views.append((view, message_id))

    def get_cog(self, name):
        if name == "Malfoyd":
            return self._cog
        return None

    async def can_run(self, ctx, *, call_once=False):
        return True

    async def wait_until_ready(self):
        return None


def _cog(tmp_path, queue=None, settings=None, *, clock=None, guild=None, thread=None):
    queue_path = tmp_path / "malfoyd_queue.json"
    settings_path = tmp_path / "malfoyd_settings.json"
    if queue is not None:
        write_json(queue_path, queue)
    if settings is not None:
        write_json(settings_path, settings)
    if guild is None:
        guild = FakeGuild()
    if thread is not None:
        guild.thread = thread
    bot = FakeBot(guild)
    cog = Malfoyd(
        bot,
        queue_path=queue_path,
        settings_path=settings_path,
        clock=clock or (lambda: NOW),
    )
    bot._cog = cog
    for command in cog.walk_commands():
        command.cog = cog
    return cog


def _interaction(cog, *, user, message_id, guild=None, fail_edit=None):
    return SimpleNamespace(
        user=user,
        guild=guild if guild is not None else cog.bot.guild,
        client=cog.bot,
        message=SimpleNamespace(id=message_id),
        response=FakeResponse(fail_edit=fail_edit),
    )


def _ctx(cog, user_id: int, *, owner: bool, command=None):
    cog.bot.is_owner = AsyncMock(return_value=owner)
    author = SimpleNamespace(id=user_id, bot=False, send=AsyncMock())
    ctx = SimpleNamespace(
        author=author,
        guild=cog.bot.guild,
        channel=None,
        bot=cog.bot,
        cog=cog,
        reply=AsyncMock(),
        send=AsyncMock(),
        prefix="!",
        view=StringView(""),
        command=command or cog.malfoyd_post,
    )
    ctx.message = SimpleNamespace(id=1, author=author, guild=ctx.guild, channel=None)
    return ctx


def _saved(cog) -> dict:
    return json.loads(cog.queue_path.read_text(encoding="utf-8-sig"))


def _button(view, custom_id: str):
    return next(item for item in view.children if item.custom_id == custom_id)


def _emoji(button) -> str:
    emoji = button.emoji
    return getattr(emoji, "name", str(emoji))


def test_extension_follows_costume_and_constants_match():
    costume_at = initial_extensions.index("cogs.costume")
    assert initial_extensions[costume_at + 1] == "cogs.malfoyd"
    source = Path(__file__).resolve().parents[1].joinpath("PooperScooper.py").read_text(encoding="utf-8")
    assert source.split("cogs.costume", 1)[1].lstrip("',\n ").startswith("cogs.malfoyd")
    assert GUILD_ID == 932057681307512922
    assert THREAD_ID == 1565459865134960811
    assert ROLE_NAME == "Malfoyd Ping"
    assert QUEUE_PATH == DATA_DIR / "malfoyd_queue.json"
    assert SETTINGS_PATH == DATA_DIR / "malfoyd_settings.json"
    assert MAX_REROLLS_PER_DAY == 5
    assert POLL_SECONDS == 60
    assert TZ.key == "America/Los_Angeles"
    assert Malfoyd.poll_loop.seconds == 60
    assert today_pt(datetime(2026, 10, 31, 6, 30, tzinfo=timezone.utc)) == "2026-10-30"
    assert today_pt(datetime(2026, 10, 31, 8, 30, tzinfo=timezone.utc)) == "2026-10-31"


def test_candidate_schema_skips_invalid_and_keeps_extra_fields():
    good = {"id": "a", "handle": "sam", "vx_url": f"{VX}9", "like_count": 3, "campaign": "fall"}
    assert is_valid_candidate(good) is True
    assert is_valid_candidate({"vx_url": f"{VX}only"}) is True
    assert is_valid_candidate({"vx_url": ""}) is False
    assert is_valid_candidate({"vx_url": "https://vxtwitter.com"}) is False
    assert is_valid_candidate({"vx_url": "https://vxtwitter.com.evil/x"}) is False
    assert is_valid_candidate({"vx_url": "https://twitter.com/a/status/1"}) is False
    assert is_valid_candidate({"vx_url": " http://vxtwitter.com/a"}) is False
    assert is_valid_candidate({"vx_url": None}) is False
    assert is_valid_candidate({"vx_url": 5}) is False
    assert is_valid_candidate("https://vxtwitter.com/a") is False
    assert is_valid_candidate(None) is False

    raw = [good, {"vx_url": "https://x.com/nope", "id": "skip"}, {"note": True}, _candidate(2)]
    chosen = valid_candidates(raw)
    assert [item["vx_url"] for item in chosen] == [good["vx_url"], f"{VX}2"]
    assert chosen[0]["campaign"] == "fall"
    assert valid_candidates(None) == []
    assert valid_candidates({"vx_url": f"{VX}1"}) == []

    queue = {"index": 0, "candidates": raw}
    assert current_candidate(queue)["vx_url"] == good["vx_url"]
    assert current_candidate({"index": 1, "candidates": raw})["vx_url"] == f"{VX}2"
    assert current_candidate({"index": 2, "candidates": raw}) is None
    assert current_candidate({"candidates": []}) is None
    assert current_candidate([]) is None


def test_build_content_mention_is_optional_first_line():
    candidate = _candidate(7)
    with_role = build_content(candidate, 42, True)
    assert with_role == f"<@&42>\n{HEADING}\n{candidate['vx_url']}"
    without = build_content(candidate, 42, False)
    assert without == f"{HEADING}\n{candidate['vx_url']}"
    assert "<@&" not in without
    assert build_content(candidate, None, True) == without
    assert "embed" not in with_role.lower()


def test_view_custom_ids_timeout_and_reroll_label():
    full = make_view(MAX_REROLLS_PER_DAY)
    one = make_view(1)
    empty = make_view(0)
    assert full.timeout is None
    assert one.timeout is None
    assert empty.timeout is None
    assert full.is_persistent()
    assert empty.is_persistent()
    assert not full.is_finished()

    notify = _button(full, "malfoyd:notify")
    reroll = _button(full, "malfoyd:reroll")
    assert notify.label == "Notify me"
    assert _emoji(notify) == "🔔"
    assert notify.disabled is False
    assert reroll.label == "Reroll (5 left)"
    assert _emoji(reroll) == "🎲"
    assert reroll.disabled is False
    assert _button(one, "malfoyd:reroll").label == "Reroll (1 left)"
    assert _button(one, "malfoyd:reroll").disabled is False
    assert _button(empty, "malfoyd:reroll").label == "Reroll (0 left)"
    assert _button(empty, "malfoyd:reroll").disabled is True
    assert _button(full, "malfoyd:notify").custom_id == _button(empty, "malfoyd:notify").custom_id
    assert _button(full, "malfoyd:reroll").custom_id == _button(empty, "malfoyd:reroll").custom_id
    assert {item.custom_id for item in full.children} == {"malfoyd:notify", "malfoyd:reroll"}
    assert full.reroll.disabled is False


def test_json_write_is_atomic_utf8_without_bom(tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr("cogs.malfoyd.os.replace", spy)
    path = tmp_path / "nested" / "queue.json"
    write_json(path, {"date": TODAY, "handle": "Mälfoy", "posted": False})
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").startswith("{")
    assert "Mälfoy".encode("utf-8") in raw
    assert "\\u" not in raw.decode("utf-8")
    assert calls and calls[0][1] == path
    assert not path.with_name(f".{path.name}.tmp").exists()
    loaded = read_json(path, default_queue)
    assert loaded.status == "ok"
    assert loaded.data["handle"] == "Mälfoy"


def test_json_reads_bom_and_tolerates_missing_or_corrupt(tmp_path, caplog):
    missing = tmp_path / "gone.json"
    with caplog.at_level(logging.WARNING, logger="cogs.malfoyd"):
        first = read_json(missing, default_queue)
        second = read_json(missing, default_queue)
    assert first.status == "missing"
    assert second.status == "missing"
    assert first.data["candidates"] == []
    assert first.data is not second.data
    assert "is missing" in caplog.text

    bom = tmp_path / "bom.json"
    payload = {"date": TODAY, "posted": False, "message_id": None, "index": 0, "rerolls_used": 0, "candidates": []}
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    loaded = read_json(bom, default_queue)
    assert loaded.status == "ok"
    assert loaded.data["date"] == TODAY

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="cogs.malfoyd"):
        assert read_json(broken, default_queue).status == "corrupt"
        assert read_json(array, default_queue).status == "corrupt"
    assert caplog.text.count("is corrupt") >= 2


def test_poll_posts_only_when_today_and_not_posted(tmp_path):
    async def run():
        thread = FakeThread()
        role = FakeRole(77)
        guild = FakeGuild()
        guild.roles.append(role)
        cog = _cog(
            tmp_path,
            _queue(candidates=[_candidate(1, campaign="keep"), {"vx_url": "nope"}, _candidate(2)], source="cron"),
            {"role_id": 77, "note": "stay"},
            guild=guild,
            thread=thread,
        )
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert cog.bot.channel_calls == 0
        assert cog.bot.fetch_calls == 0
        assert guild.thread_lookups == 1
        content, kwargs = thread.sent[0]
        assert content == build_content(_candidate(1, campaign="keep"), 77, True)
        assert "embed" not in kwargs and "embeds" not in kwargs
        allowed = kwargs["allowed_mentions"]
        assert allowed.everyone is False
        assert allowed.users is False
        assert [item.id for item in allowed.roles] == [77]
        assert kwargs["view"].timeout is None
        assert _button(kwargs["view"], "malfoyd:reroll").label == "Reroll (5 left)"
        saved = _saved(cog)
        assert saved["posted"] is True
        assert saved["message_id"] == 5001
        assert saved["index"] == 0
        assert saved["rerolls_used"] == 0
        assert saved["source"] == "cron"
        assert saved["candidates"][0]["campaign"] == "keep"
        assert saved["candidates"][1]["vx_url"] == "nope"
        assert not cog.queue_path.read_bytes().startswith(b"\xef\xbb\xbf")

        assert await cog.poll_once() is None
        assert len(thread.sent) == 1

        posted = _saved(cog)
        posted["date"] = "2026-10-29"
        posted["posted"] = False
        write_json(cog.queue_path, posted)
        assert await cog.poll_once() is None
        assert len(thread.sent) == 1

        utc_evening = datetime(2026, 10, 31, 6, 30, tzinfo=timezone.utc)
        boundary = _cog(
            tmp_path / "boundary",
            _queue(date="2026-10-31", posted=False, message_id=None),
            clock=lambda: utc_evening,
            thread=FakeThread(),
        )
        assert await boundary.poll_once() is None
        assert boundary.bot.guild.thread.sent == []
        late = _cog(
            tmp_path / "late",
            _queue(date="2026-10-30", posted=False, message_id=None, candidates=[_candidate(9)]),
            clock=lambda: utc_evening,
            thread=FakeThread(),
        )
        assert await late.poll_once() == "Posted Malfoyd of the day (5001)."

        empty = _cog(tmp_path / "empty", _queue(candidates=[{"vx_url": "https://x.com/no"}]), thread=FakeThread())
        assert await empty.poll_once() is None
        assert empty.bot.guild.thread.sent == []

    asyncio.run(run())


def test_poll_channel_fallback_and_unarchive(tmp_path):
    async def run():
        cached = FakeThread()
        cog = _cog(tmp_path / "channel", _queue(message_id=None))
        cog.bot.guild.thread = None
        cog.bot.channel = cached
        assert await cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert cog.bot.guild.thread_lookups == 1
        assert cog.bot.channel_calls == 1
        assert cog.bot.fetch_calls == 0
        assert len(cached.sent) == 1

        fetched = FakeThread(archived=True)
        fetch_cog = _cog(tmp_path / "fetch", _queue(message_id=None, candidates=[_candidate(3)]))
        fetch_cog.bot.guild.thread = None
        fetch_cog.bot.fetched = fetched
        assert await fetch_cog.poll_once() == "Posted Malfoyd of the day (5001)."
        assert fetch_cog.bot.fetch_calls == 1
        assert fetched.edits == [{"archived": False}]
        assert fetched.archived_at_send == [False]
        content, kwargs = fetched.sent[0]
        assert content == f"{HEADING}\n{_candidate(3)['vx_url']}"
        assert kwargs["allowed_mentions"].roles is False
        assert "<@&" not in content

        blocked = FakeThread(archived=True, fail_edit=_http(403, "Forbidden"))
        blocked_cog = _cog(tmp_path / "blocked", _queue(message_id=None), thread=blocked)
        assert await blocked_cog.poll_once() is None
        assert blocked.sent == []
        assert _saved(blocked_cog)["posted"] is False

        missing = _cog(tmp_path / "missing", _queue(message_id=None))
        missing.bot.guild.thread = None
        assert await missing.poll_once() is None
        assert _saved(missing)["posted"] is False

    asyncio.run(run())


def test_reroll_limit_owner_and_skipped_invalid(tmp_path):
    async def run():
        candidates = [
            _candidate(1),
            {"vx_url": "https://x.com/skip", "id": "bad"},
            _candidate(2),
            _candidate(3),
            _candidate(4),
            _candidate(5),
            _candidate(6),
            _candidate(7),
        ]
        cog = _cog(tmp_path, _queue(candidates=candidates, message_id=5001))
        view = make_view(MAX_REROLLS_PER_DAY)

        async def click(user_id: int, *, owner: bool = False):
            cog.bot.is_owner = AsyncMock(return_value=owner)
            user = FakeMember(user_id, cog.bot.guild)
            interaction = _interaction(cog, user=user, message_id=5001)
            await _button(view, "malfoyd:reroll").callback(interaction)
            return interaction

        first = await click(11)
        assert first.response.sent == []
        edit = first.response.edits[0]
        assert edit["content"] == build_content(_candidate(2), None, False)
        assert "<@&" not in edit["content"]
        assert edit["allowed_mentions"].roles is False
        assert edit["allowed_mentions"].everyone is False
        assert _button(edit["view"], "malfoyd:reroll").label == "Reroll (4 left)"
        assert _button(edit["view"], "malfoyd:reroll").disabled is False
        assert _saved(cog)["index"] == 1
        assert _saved(cog)["rerolls_used"] == 1
        assert len(_saved(cog)["candidates"]) == 8

        for number in range(3, 6):
            step = await click(10 + number)
            assert _candidate(number)["vx_url"] in step.response.edits[0]["content"]
            assert _saved(cog)["rerolls_used"] == number - 1
            assert _saved(cog)["index"] == number - 1

        last = await click(16)
        assert _button(last.response.edits[0]["view"], "malfoyd:reroll").label == "Reroll (0 left)"
        assert _button(last.response.edits[0]["view"], "malfoyd:reroll").disabled is True
        assert _candidate(6)["vx_url"] in last.response.edits[0]["content"]
        assert _saved(cog)["rerolls_used"] == 5
        assert _saved(cog)["index"] == 5

        before = cog.queue_path.read_bytes()
        blocked = await click(17)
        assert blocked.response.edits == []
        assert blocked.response.sent[0][0] == "No rerolls left today"
        assert blocked.response.sent[0][1]["ephemeral"] is True
        assert cog.queue_path.read_bytes() == before

        owner = await click(99, owner=True)
        assert _candidate(7)["vx_url"] in owner.response.edits[0]["content"]
        assert _saved(cog)["rerolls_used"] == 5
        assert _saved(cog)["index"] == 6
        stuck = cog.queue_path.read_bytes()
        again = await click(99, owner=True)
        assert again.response.sent[0][0] == "No more picks today"
        assert cog.queue_path.read_bytes() == stuck

    asyncio.run(run())


def test_no_more_picks_does_not_consume(tmp_path):
    async def run():
        cog = _cog(tmp_path, _queue(candidates=[_candidate(1), {"vx_url": "https://example.com/no"}], message_id=5001))
        before = cog.queue_path.read_bytes()
        interaction = _interaction(cog, user=FakeMember(4, cog.bot.guild), message_id=5001)
        await cog.handle_reroll(interaction)
        assert interaction.response.sent[0][0] == "No more picks today"
        assert interaction.response.edits == []
        assert cog.queue_path.read_bytes() == before

        last = _cog(tmp_path / "last", _queue(candidates=[_candidate(1), _candidate(2)], index=1, message_id=5001))
        snapshot = last.queue_path.read_bytes()
        interaction = _interaction(last, user=FakeMember(4, last.bot.guild), message_id=5001)
        await last.handle_reroll(interaction)
        assert interaction.response.sent[0][0] == "No more picks today"
        assert last.queue_path.read_bytes() == snapshot

    asyncio.run(run())


def test_expired_pick_does_not_change_the_queue(tmp_path):
    async def run():
        cog = _cog(tmp_path, _queue(message_id=5001))
        before = cog.queue_path.read_bytes()
        wrong = _interaction(cog, user=FakeMember(4, cog.bot.guild), message_id=999)
        await cog.handle_reroll(wrong)
        assert wrong.response.sent[0][0] == "That pick has expired."
        assert wrong.response.sent[0][1]["ephemeral"] is True
        assert cog.queue_path.read_bytes() == before

        stale = _cog(tmp_path / "stale", _queue(date="2026-10-29", message_id="5001"))
        snapshot = stale.queue_path.read_bytes()
        interaction = _interaction(stale, user=FakeMember(4, stale.bot.guild), message_id=5001)
        await stale.handle_reroll(interaction)
        assert interaction.response.sent[0][0] == "That pick has expired."
        assert stale.queue_path.read_bytes() == snapshot

        text_id = _cog(tmp_path / "text", _queue(message_id="5001", candidates=[_candidate(1), _candidate(2)]))
        interaction = _interaction(text_id, user=FakeMember(4, text_id.bot.guild), message_id=5001)
        await text_id.handle_reroll(interaction)
        assert interaction.response.edits
        assert _saved(text_id)["index"] == 1

    asyncio.run(run())


def test_reroll_callback_ignores_the_button_label(tmp_path):
    async def run():
        cog = _cog(tmp_path, _queue(rerolls_used=5, message_id=5001))
        before = cog.queue_path.read_bytes()
        view = make_view(2)
        assert _button(view, "malfoyd:reroll").label == "Reroll (2 left)"
        assert _button(view, "malfoyd:reroll").disabled is False
        interaction = _interaction(cog, user=FakeMember(8, cog.bot.guild), message_id=5001)
        await _button(view, "malfoyd:reroll").callback(interaction)
        assert interaction.response.sent[0][0] == "No rerolls left today"
        assert cog.queue_path.read_bytes() == before

    asyncio.run(run())


def test_reroll_lock_serializes_the_shared_counter(tmp_path):
    async def run():
        cog = _cog(tmp_path, _queue(message_id=5001))
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        async def is_owner(user):
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                await release.wait()
            return False

        cog.bot.is_owner = is_owner
        first = _interaction(cog, user=FakeMember(1, cog.bot.guild), message_id=5001)
        second = _interaction(cog, user=FakeMember(2, cog.bot.guild), message_id=5001)
        task1 = asyncio.create_task(cog.handle_reroll(first))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task2 = asyncio.create_task(cog.handle_reroll(second))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task2.done()
        assert _saved(cog)["rerolls_used"] == 0
        assert _saved(cog)["index"] == 0
        release.set()
        await asyncio.wait_for(task1, timeout=2)
        await asyncio.wait_for(task2, timeout=2)
        assert _saved(cog)["rerolls_used"] == 2
        assert _saved(cog)["index"] == 2
        assert _candidate(2)["vx_url"] in first.response.edits[0]["content"]
        assert _candidate(3)["vx_url"] in second.response.edits[0]["content"]

    asyncio.run(run())


def test_reroll_edit_failure_reverts_the_queue(tmp_path, caplog):
    async def run():
        cog = _cog(tmp_path, _queue(message_id=5001, candidates=[_candidate(1), _candidate(2)]))
        before = cog.queue_path.read_bytes()
        interaction = _interaction(
            cog,
            user=FakeMember(4, cog.bot.guild),
            message_id=5001,
            fail_edit=_http(500, "Internal Server Error"),
        )
        with caplog.at_level(logging.ERROR, logger="cogs.malfoyd"):
            await cog.handle_reroll(interaction)
        assert interaction.response.sent[0][0] == "Couldn't update that pick."
        assert interaction.response.sent[0][1]["ephemeral"] is True
        assert cog.queue_path.read_bytes() == before
        assert "Malfoyd reroll edit failed" in caplog.text

    asyncio.run(run())


def test_notify_toggles_role_by_id_and_by_name(tmp_path):
    async def run():
        role = FakeRole(55, ROLE_NAME)
        guild = FakeGuild()
        guild.roles.append(role)
        member = FakeMember(9, guild)
        cog = _cog(tmp_path, settings={"role_id": 55}, guild=guild)
        interaction = _interaction(cog, user=member, message_id=1)
        await _button(make_view(2), "malfoyd:notify").callback(interaction)
        assert interaction.response.sent[0][0] == "You'll be pinged for Malfoyd of the day 🐍"
        assert interaction.response.sent[0][1]["ephemeral"] is True
        assert member.added[0][0] == (role,)

        again = _interaction(cog, user=member, message_id=1)
        await cog.handle_notify(again)
        assert again.response.sent[0][0] == "You won't be pinged anymore."
        assert member.removed[0][0] == (role,)
        assert role not in member.roles

        named = FakeRole(70, ROLE_NAME)
        other = FakeRole(71, "Someone Else")
        name_guild = FakeGuild()
        name_guild.roles.extend([other, named])
        person = FakeMember(3, name_guild)
        name_cog = _cog(tmp_path / "name", guild=name_guild)
        name_hit = _interaction(name_cog, user=person, message_id=1)
        await name_cog.handle_notify(name_hit)
        assert person.added[0][0] == (named,)

        stale = FakeRole(80, ROLE_NAME)
        stale_guild = FakeGuild()
        stale_guild.roles.append(stale)
        stale_member = FakeMember(4, stale_guild)
        stale_cog = _cog(tmp_path / "stale", settings={"role_id": 12345}, guild=stale_guild)
        stale_hit = _interaction(stale_cog, user=stale_member, message_id=1)
        await stale_cog.handle_notify(stale_hit)
        assert stale_member.added[0][0] == (stale,)

    asyncio.run(run())


def test_notify_unset_forbidden_and_http_exception(tmp_path, caplog):
    async def run():
        cog = _cog(tmp_path)
        member = FakeMember(9, cog.bot.guild)
        missing = _interaction(cog, user=member, message_id=1)
        await cog.handle_notify(missing)
        assert missing.response.sent[0][0] == "Signups aren't set up yet."
        assert member.added == []

        role = FakeRole(55)
        cog.bot.guild.roles.append(role)
        write_json(cog.settings_path, {"role_id": 55})
        for status, reason in ((403, "Forbidden"), (500, "Server Error")):
            person = FakeMember(status, cog.bot.guild)
            person.fail = _http(status, reason)
            interaction = _interaction(cog, user=person, message_id=1)
            with caplog.at_level(logging.ERROR, logger="cogs.malfoyd"):
                await cog.handle_notify(interaction)
            assert interaction.response.sent[0][0] == "Couldn't update your notify role."
            assert interaction.response.sent[0][1]["ephemeral"] is True
            assert person.roles == []
        assert "Malfoyd notify failed" in caplog.text

    asyncio.run(run())


def test_owner_commands_reply_and_non_owners_stay_silent(tmp_path):
    async def run():
        thread = FakeThread()
        cog = _cog(tmp_path, _queue(posted=True, message_id=111, candidates=[_candidate(8)]), thread=thread)
        stranger = _ctx(cog, 5, owner=False, command=cog.malfoyd_post)
        with pytest.raises(commands.NotOwner):
            await cog.malfoyd_post.can_run(stranger)
        await cog.malfoyd_post.callback(cog, stranger)
        await cog.cog_command_error(stranger, commands.NotOwner("You do not own this bot."))
        try:
            await cog.malfoyd_post.invoke(stranger)
        except commands.CheckFailure as exc:
            await cog.cog_command_error(stranger, exc)
        else:
            raise AssertionError("non-owner invoke should fail the owner check")
        stranger.reply.assert_not_awaited()
        stranger.send.assert_not_awaited()
        assert thread.sent == []

        setup_stranger = _ctx(cog, 5, owner=False, command=cog.malfoyd_setup)
        await cog.malfoyd_setup.callback(cog, setup_stranger)
        await cog.cog_command_error(setup_stranger, commands.CheckFailure("no"))
        setup_stranger.reply.assert_not_awaited()
        setup_stranger.send.assert_not_awaited()
        assert cog.bot.guild.created == []

        owner = _ctx(cog, 9, owner=True, command=cog.malfoyd_post)
        assert await cog.malfoyd_post.can_run(owner) is True
        await cog.malfoyd_post.callback(cog, owner)
        assert len(thread.sent) == 1
        assert _saved(cog)["posted"] is True
        assert _saved(cog)["message_id"] == 5001
        reply = owner.reply.await_args
        assert reply.args[0] == "Posted Malfoyd of the day (5001)."
        assert reply.kwargs["mention_author"] is False

        setup = _ctx(cog, 9, owner=True, command=cog.malfoyd_setup)
        write_json(cog.settings_path, {"note": "keep"})
        await cog.malfoyd_setup.callback(cog, setup)
        created = cog.bot.guild.created[0]
        assert created["name"] == ROLE_NAME
        assert created["mentionable"] is True
        assert created["permissions"].value == 0
        role_id = cog.bot.guild.roles[-1].id
        assert setup.reply.await_args.args[0] == str(role_id)
        stored = json.loads(cog.settings_path.read_text(encoding="utf-8-sig"))
        assert stored["role_id"] == role_id
        assert stored["note"] == "keep"
        assert not cog.settings_path.read_bytes().startswith(b"\xef\xbb\xbf")

        existing = FakeRole(4242, ROLE_NAME)
        reuse_guild = FakeGuild()
        reuse_guild.roles.append(existing)
        reuse = _cog(tmp_path / "reuse", guild=reuse_guild)
        reuse_ctx = _ctx(reuse, 9, owner=True, command=reuse.malfoyd_setup)
        await reuse.malfoyd_setup.callback(reuse, reuse_ctx)
        assert reuse_guild.created == []
        assert reuse_ctx.reply.await_args.args[0] == "4242"
        assert json.loads(reuse.settings_path.read_text(encoding="utf-8"))["role_id"] == 4242

        source = Path(__file__).resolve().parents[1].joinpath("cogs", "malfoyd.py").read_text(encoding="utf-8")
        assert "@commands.is_owner()" in source
        assert Malfoyd.malfoyd_post.hidden is True
        assert Malfoyd.malfoyd_setup.hidden is True
        assert list(Malfoyd.__cog_app_commands__) == []

    asyncio.run(run())


def test_cog_load_registers_persistent_view_and_unload_cancels(tmp_path):
    async def run():
        cog = _cog(tmp_path / "load")
        await cog.cog_load()
        try:
            assert cog.poll_loop.is_running()
            view, message_id = cog.bot.views[0]
            assert message_id is None
            assert view.timeout is None
            assert view.is_persistent()
            assert {item.custom_id for item in view.children} == {"malfoyd:notify", "malfoyd:reroll"}
            assert _button(view, "malfoyd:reroll").label == "Reroll (5 left)"
        finally:
            await cog.cog_unload()
            task = cog.poll_loop.get_task()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        assert not cog.poll_loop.is_running()

    asyncio.run(run())
