import discord
import datetime
import json
import logging
import argparse
from discord.ext import commands

description = """
Yo, this PooperScooper. Need any poop scooped? '!' me dawg. These my commands.
"""

initial_extensions = [
    'cogs.general',
    'cogs.music'
]

# Set up logging
log = logging.getLogger()
log.setLevel(logging.INFO)
handler = logging.FileHandler(filename='pooperscooper.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
log.addHandler(handler)

# discord.ext.commands.Bot setup
help_attrs = dict(hidden=True)
pooper_bot = commands.Bot(command_prefix=['!'], description=description, pm_help=False, help_attrs=help_attrs)
pooper_bot.commands_executed = 0


# TODO
# @pooper_bot.event
# async def on_command_error(error, ctx):
#     if isinstance(error, commands.NoPrivateMessage):
#         await bot.say(ctx.message.author, 'This command cannot be used in private messages.')
#     elif isinstance(error, commands.DisabledCommand):
#         await bot.send_message(ctx.message.author, 'Sorry. This command is disabled and cannot be used.')


@pooper_bot.event
async def on_ready():
    """Event that occurs when pooper_bot is ready"""
    log.info('Logging in as:')
    log.info('Username: {}'.format(pooper_bot.user.name))
    log.info('ID: {}'.format(pooper_bot.user.id))
    pooper_bot.uptime = datetime.datetime.utcnow()


@pooper_bot.event
async def on_resumed():
    """Event that occurs when pooper_bot has resumed"""
    log.info('pooper_bot has resumed...')


@pooper_bot.event
async def on_command(ctx):
    """Event that occurs when pooper_bot detects a command

    Logs the command detected.
    """
    pooper_bot.commands_executed += 1
    message = ctx.message
    if isinstance(message.channel, discord.abc.PrivateChannel):
        destination = 'Private Message'
    else:
        destination = '#{0.channel.name} ({0.guild.name})'.format(message)

    log.info('{0.created_at}: {0.author.name} in {1}: {0.content}'.format(message, destination))


@pooper_bot.event
async def on_message(message):
    """Event that happens on every message

    Filters out the bot's own messages. This might not be needed according to the documentation:
    https://discordpy.readthedocs.io/en/latest/api.html?highlight=on_message#discord.on_message
    """
    if message.author.bot:
        logging.debug('Detected my own message')
        return

    await pooper_bot.process_commands(message)


def load_credentials():
    with open('config.json') as f:
        return json.load(f)


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
    pooper_bot.client_id = credentials['client_id']

    for extension in initial_extensions:
        try:
            pooper_bot.load_extension(extension)
        except Exception as e:
            log.critical('Failed to load extension {}\n{}: {}'.format(extension, type(e).__name__, e))

    pooper_bot.run(token)

    handlers = log.handlers[:]
    for hdlr in handlers:
        hdlr.close()
        log.removeHandler(hdlr)
