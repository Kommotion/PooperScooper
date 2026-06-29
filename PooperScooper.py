import asyncio
import discord
import logging
import argparse
import pathlib
import os
from logging.handlers import RotatingFileHandler
from discord.ext import commands
from cogs.utils.config import load_config
from cogs.utils.constants import LEGACY_DATA_FILES, PROJECT_ROOT
from cogs.utils.json_store import migrate_legacy_data_files


description = """
Yo, this PooperScooper. Need any poop scooped? '!' me dawg. These my commands.
"""

initial_extensions = [
    'cogs.general',
    'cogs.gametime',
    'cogs.menacesroles',
    'cogs.imagediffusion',
    'cogs.birthdaytracker',
    'cogs.palworld',
    'cogs.bingo',
    'cogs.gamepicker',
    'cogs.random',
    'cogs.music',
]

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

    async def on_ready(self) -> None:
        """Event that occurs when PooperScooper is ready"""
        if not hasattr(self, 'uptime'):
            self.uptime = discord.utils.utcnow()

        log.info('Logging in as:')
        log.info('Username: {}'.format(self.user.name))
        log.info('ID: {}'.format(self.user.id))
        activity = discord.Activity(name='humans scoop 💩', type=discord.ActivityType.watching)
        await self.change_presence(activity=activity)

    async def close(self) -> None:
        music = self.get_cog('Music')
        if music is not None:
            await music.shutdown_lavalink()
        await super().close()

    async def setup_hook(self) -> None:
        """Sets up the bot one time."""
        self.bot_app_info = await self.application_info()
        self.owner_id = self.bot_app_info.owner.id
        self.commands_executed = 0

        for extension in initial_extensions:
            try:
                await self.load_extension(extension)
            except Exception as e:
                log.exception('Failed to load extension {}\n{}'.format(extension, e))


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