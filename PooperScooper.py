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
    'cogs.grammarpolice',
    'cogs.menacesroles',
    'cogs.imagediffusion',
    'cogs.birthdaytracker'
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
            help_attrs=dict(hidden=True)
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
