import asyncio
import discord
import logging
import argparse
import pathlib
import os
from logging.handlers import RotatingFileHandler
from discord.ext import commands
from cogs.music import IDLE_STATUS_NAME
from cogs.utils.config import load_config
from cogs.utils.constants import LEGACY_DATA_FILES, PROJECT_ROOT
from cogs.utils.json_store import migrate_legacy_data_files

log = logging.getLogger(__name__)

description = """
Yo, this PooperScooper. Need any poop scooped? '!' me dawg. These my commands.
"""

initial_extensions = [
    'cogs.general',
    'cogs.gametime',
    'cogs.menacesroles',
    'cogs.eastereggs',
    'cogs.imagediffusion',
    'cogs.birthdaytracker',
    'cogs.reminders',
    'cogs.palworld',
    'cogs.bingo',
    'cogs.gamepicker',
    'cogs.random',
    'cogs.music',
    'cogs.grok',
    'cogs.candyhunt',
    'cogs.costume',
    'cogs.malfoyd',
]

# Candy hunt and costume are the Halloween cogs. Malfoyd stays either way.
_HALLOWEEN_EXTENSIONS = frozenset({'cogs.candyhunt', 'cogs.costume'})


def extensions_to_load(cfg) -> list[str]:
    """Cog names to load. Halloween cogs stay out unless halloween_enabled is true.

    A missing config object or a missing flag counts as off. ``cogs.malfoyd`` always stays.
    """
    if bool(getattr(cfg, 'halloween_enabled', False)):
        return list(initial_extensions)
    return [name for name in initial_extensions if name not in _HALLOWEEN_EXTENSIONS]


def _config_for_startup():
    """Return bot config, or None when it cannot be read. None leaves Halloween off."""
    try:
        return load_config()
    except Exception:
        log.error('Could not read config while choosing extensions; Halloween stays off')
        return None


async def sync_retired_halloween_commands(bot) -> None:
    """Publish the current tree for each candy guild, then sync globally.

    ``tree.sync(guild=...)`` replaces that guild's Discord commands with the
    guild commands already on the tree. It does not call ``clear_commands`` or
    ``copy_global_to``, so copied or other guild commands already on the tree
    stay. Global ``tree.sync()`` drops /halloweenify when that command was left
    off the tree. One failed sync is logged and skipped. This never raises.
    The guild ids come from ``cogs.candyhunt.CANDY_GUILD_IDS`` without loading
    that cog.
    """
    tree = getattr(bot, 'tree', None)
    sync = getattr(tree, 'sync', None)
    if not callable(sync):
        log.error('halloween command sync failed error=no_tree')
        return

    guild_ids: tuple[int, ...] = ()
    try:
        from cogs.candyhunt import CANDY_GUILD_IDS
        guild_ids = tuple(CANDY_GUILD_IDS)
    except Exception:
        log.exception('halloween command sync failed error=no_guild_ids')

    for gid in guild_ids:
        try:
            synced = await sync(guild=discord.Object(id=gid))
            synced = list(synced or [])
            names = ','.join(str(getattr(command, 'name', '')) for command in synced)
            log.info('halloween command sync guild=%s count=%s names=%s', gid, len(synced), names)
        except Exception:
            log.exception('halloween command sync failed guild=%s', gid)

    try:
        synced = await sync()
        synced = list(synced or [])
        names = ','.join(str(getattr(command, 'name', '')) for command in synced)
        log.info('halloween command sync scope=global count=%s names=%s', len(synced), names)
    except Exception:
        log.exception('halloween command sync failed scope=global')

LOG_FILE = "pooperscooper.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 9


def configure_logging(*, debug: bool = False) -> logging.Logger:
    """Configure rotating file logs (10 files max) and console output."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    file_handler = RotatingFileHandler(
        filename=LOG_FILE,
        encoding='utf-8',
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    return root


class PooperScooper(commands.AutoShardedBot):
    def __init__(self, *, client_id: str):
        allowed_mentions = discord.AllowedMentions(roles=False, everyone=False, users=True)
        intents = discord.Intents(
            guilds=True,
            members=True,
            bans=True,
            emojis=True,
            voice_states=True,
            messages=True,
            reactions=True,
            presences=True,
            message_content=True
        )
        super().__init__(
            command_prefix=['!'],
            description=description,
            pm_help=None,
            intents=intents,
            allowed_mentions=allowed_mentions,
            help_attrs=dict(hidden=True),
            help_command=commands.DefaultHelpCommand(show_parameter_descriptions=False)
        )

        self.client_id: str = client_id
        self.commands_executed = None
        self._halloween_sunset_synced = False

    async def on_ready(self) -> None:
        """Event that occurs when PooperScooper is ready"""
        if not hasattr(self, 'uptime'):
            self.uptime = discord.utils.utcnow()

        log.info('Logging in as:')
        log.info('Username: {}'.format(self.user.name))
        log.info('ID: {}'.format(self.user.id))
        music = self.get_cog('Music')
        if music is not None:
            await music.refresh_music_presence()
        else:
            activity = discord.Activity(name=IDLE_STATUS_NAME, type=discord.ActivityType.watching)
            await self.change_presence(activity=activity)

    async def close(self) -> None:
        music = self.get_cog('Music')
        if music is not None:
            await music.shutdown_lavalink()
        await super().close()

    async def setup_hook(self) -> None:
        """Sets up the bot one time."""
        self.bot_app_info = await self.application_info()
        if self.bot_app_info.team:
            self.owner_ids = {m.id for m in self.bot_app_info.team.members}
            self.owner_id = None
        else:
            self.owner_id = self.bot_app_info.owner.id
        self.commands_executed = 0

        cfg = _config_for_startup()
        for extension in extensions_to_load(cfg):
            try:
                await self.load_extension(extension)
            except Exception as e:
                log.exception('Failed to load extension {}\n{}'.format(extension, e))

        if not bool(getattr(cfg, 'halloween_enabled', False)):
            await self._retire_halloween_commands_once()

    async def _retire_halloween_commands_once(self) -> None:
        """Drop stale Halloween slash commands once per process. Never raises."""
        if self._halloween_sunset_synced:
            return
        self._halloween_sunset_synced = True
        try:
            await sync_retired_halloween_commands(self)
        except Exception:
            log.exception('halloween command sync failed')


async def run_bot(token: str, *, client_id: str):
    async with PooperScooper(client_id=client_id) as bot:
        await bot.start(token, reconnect=True)


if __name__ == '__main__':
    args = argparse.ArgumentParser()
    msg = 'If set, logging level is now DEBUG'
    args.add_argument('-d', '--debug', action='store_true', default=False, help=msg, required=False)
    parsed_args = args.parse_args()

    base_file_path = pathlib.Path(__file__).parent.resolve()
    os.chdir(base_file_path)

    log = configure_logging(debug=parsed_args.debug)
    if parsed_args.debug:
        logging.debug('Enabling Debug Level Logging')

    migrate_legacy_data_files(PROJECT_ROOT, LEGACY_DATA_FILES)

    config = load_config()
    token = config.token.get_secret_value()

    asyncio.run(run_bot(token, client_id=config.client_id))

    handlers = log.handlers[:]
    for hdlr in handlers:
        hdlr.close()
        log.removeHandler(hdlr)