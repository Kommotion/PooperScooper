import asyncio
import discord
import logging
import argparse
import pathlib
import os
from discord.ext import commands
from cogs.utils.utils import load_credentials


description = """
Yo, this PooperScooper. Need any poop scooped? '!' me dawg. These my commands.
"""

initial_extensions = [
    'cogs.general',
    'cogs.music',
    'cogs.gametime',
    #'cogs.grammarpolice',
    'cogs.menacesroles',
    'cogs.imagediffusion',
    'cogs.birthdaytracker',
    'cogs.poll',
    'cogs.palworld',
    'cogs.bingo',
    'cogs.gamepicker',
    'cogs.random'
]

# Set up logging
base_file_path = pathlib.Path(__file__).parent.resolve()
os.chdir(base_file_path)
log = logging.getLogger()
log.setLevel(logging.INFO)
handler = logging.FileHandler(filename='pooperscooper.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
log.addHandler(handler)


class PooperScooper(commands.AutoShardedBot):
    def __init__(self):
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

        self.client_id: str = credentials['client_id']
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
        task = getattr(self, 'lavalink_bootstrap_task', None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(self, 'lavalink_server'):
            await self.lavalink_server.stop()
        await super().close()

    async def _bootstrap_lavalink(self) -> None:
        """Start Lavalink and connect Wavelink without blocking bot/cog startup."""
        from cogs.utils.lavalink_client import connect_lavalink_with_retry

        if not self.lavalink_settings.enabled:
            return

        if self.lavalink_settings.auto_start:
            try:
                started = await self.lavalink_server.start()
                if not started:
                    log.warning(
                        'Lavalink auto-start failed; will still try connecting in case another instance is running.'
                    )
            except Exception as e:
                log.exception('Failed to start Lavalink: %s', e)

        connected = await connect_lavalink_with_retry(self, credentials)
        if connected:
            log.info('Lavalink bootstrap finished; music is available.')
        else:
            log.warning('Lavalink bootstrap finished without a Wavelink connection; music is unavailable.')

    async def setup_hook(self) -> None:
        """Sets up the bot one time."""
        self.bot_app_info = await self.application_info()
        self.owner_id = self.bot_app_info.owner.id
        self.commands_executed = 0

        from cogs.utils.lavalink_server import LavalinkServerManager, load_lavalink_settings

        self.lavalink_settings = load_lavalink_settings(credentials)
        self.lavalink_server = LavalinkServerManager(self.lavalink_settings, credentials)

        for extension in initial_extensions:
            try:
                await self.load_extension(extension)
            except Exception as e:
                log.exception('Failed to load extension {}\n{}'.format(extension, e))

        if self.lavalink_settings.enabled:
            self.lavalink_bootstrap_task = asyncio.create_task(self._bootstrap_lavalink())
            log.info('Lavalink bootstrap started in background; bot will come online immediately.')


async def run_bot():
    async with PooperScooper() as bot:
        await bot.start(token, reconnect=True)


if __name__ == '__main__':
    args = argparse.ArgumentParser()
    msg = 'If set, logging level is now DEBUG'
    args.add_argument('-d', '--debug', action='store_true', default=False, help=msg, required=False)
    parsed_args = args.parse_args()

    if parsed_args.debug is True:
        log.setLevel(logging.DEBUG)
        logging.debug('Enabling Debug Level Logging')

    credentials = load_credentials()
    token = credentials['token']

    asyncio.run(run_bot())

    handlers = log.handlers[:]
    for hdlr in handlers:
        hdlr.close()
        log.removeHandler(hdlr)
