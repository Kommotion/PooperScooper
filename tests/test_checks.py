"""Admin and illuminaughty prefix check, without Discord."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.utils.checks import check_admin_or_illuminaughty, is_admin_or_illuminaughty
from cogs.utils.constants import MTS_ILLUMINAUGHTY_ROLE_ID


def _role(role_id: int, name: str):
    return SimpleNamespace(id=role_id, name=name)


def _ctx(*, owner=False, guild=True, administrator=False, roles=None, is_owner=True):
    async def owner_check(user):
        return owner

    author = SimpleNamespace(
        id=1,
        guild_permissions=SimpleNamespace(administrator=administrator),
        roles=list(roles or []),
    )
    bot = SimpleNamespace()
    if is_owner:
        bot.is_owner = owner_check
    return SimpleNamespace(
        author=author,
        bot=bot,
        guild=SimpleNamespace(id=123) if guild else None,
    )


def test_owner_is_allowed_in_dms():
    ctx = _ctx(owner=True, guild=False)
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is True


def test_administrator_is_allowed():
    ctx = _ctx(owner=False, administrator=True)
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is True


def test_illuminaughty_role_id_is_allowed():
    assert MTS_ILLUMINAUGHTY_ROLE_ID == 1083514560155222086
    ctx = _ctx(roles=[_role(1083514560155222086, "other")])
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is True


def test_illuminaughty_role_name_is_allowed():
    ctx = _ctx(roles=[_role(5, "Illuminaughty")])
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is True


def test_ordinary_member_is_denied():
    ctx = _ctx(roles=[_role(5, "Member")])
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is False


def test_non_owner_in_dms_is_denied():
    ctx = _ctx(owner=False, guild=False, administrator=True, roles=[_role(1083514560155222086, "Illuminaughty")])
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is False


def test_missing_or_noncallable_owner_check_is_denied():
    missing = _ctx(guild=False, is_owner=False)
    assert asyncio.run(check_admin_or_illuminaughty(missing)) is False
    broken = _ctx(guild=False)
    broken.bot.is_owner = None
    assert asyncio.run(check_admin_or_illuminaughty(broken)) is False


def test_decorator_uses_predicate():
    @is_admin_or_illuminaughty()
    async def sample(ctx):
        return None

    predicate = sample.__commands_checks__[0]
    assert "is_admin_or_illuminaughty" in predicate.__qualname__
    ctx = _ctx(owner=True, guild=False)
    assert asyncio.run(predicate(ctx)) is True
    assert asyncio.run(check_admin_or_illuminaughty(ctx)) is True
