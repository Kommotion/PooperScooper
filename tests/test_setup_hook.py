"""Owner detection during one-time bot setup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PooperScooper import PooperScooper, initial_extensions


def _bot(app_info: SimpleNamespace) -> PooperScooper:
    bot = PooperScooper(client_id="123456789012345678")
    bot.application_info = AsyncMock(return_value=app_info)
    bot.load_extension = AsyncMock()
    return bot


def test_setup_hook_team_app_uses_member_ids_and_clears_owner_id():
    async def run():
        app = SimpleNamespace(
            team=SimpleNamespace(members=[SimpleNamespace(id=11), SimpleNamespace(id=22)]),
            owner=SimpleNamespace(id=999),
        )
        bot = _bot(app)

        await bot.setup_hook()

        assert bot.bot_app_info is app
        assert bot.owner_id is None
        assert bot.owner_ids == {11, 22}
        assert bot.commands_executed == 0
        assert bot.extensions == {}
        assert [call.args[0] for call in bot.load_extension.await_args_list] == initial_extensions

    asyncio.run(run())


def test_setup_hook_user_owned_app_uses_owner_id():
    async def run():
        app = SimpleNamespace(team=None, owner=SimpleNamespace(id=42))
        bot = _bot(app)

        await bot.setup_hook()

        assert bot.bot_app_info is app
        assert bot.owner_id == 42
        assert bot.owner_ids == set()
        assert bot.commands_executed == 0
        assert bot.extensions == {}
        assert [call.args[0] for call in bot.load_extension.await_args_list] == initial_extensions

    asyncio.run(run())
