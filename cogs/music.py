import datetime
import discord
import os
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.constants import *


class Music(Cog):
    """Commands for playing music in voice chat. """

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def play(self, ctx):
        pass

    @commands.command()
    async def stop(self, ctx):
        pass

    @commands.command()
    async def skip(self, ctx):
        pass


def setup(bot):
    bot.add_cog(Music(bot))
