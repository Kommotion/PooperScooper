import datetime
import discord
import os
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.constants import *


class General(Cog):
    """Commands for utilities related to Discord or the Bot itself. """

    def __init__(self, bot):
        self.bot = bot

    def _get_bot_uptime(self):
        """ Returns the uptime of the bot """
        now = datetime.datetime.utcnow()
        delta = now - self.bot.uptime
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        if days:
            fmt = '{d} days, {h} hours, {m} minutes, and {s} seconds'
        else:
            fmt = '{h} hours, {m} minutes, and {s} seconds'

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @commands.command()
    async def stats(self, ctx):
        """Prints out the stats of the bot """
        await ctx.send('To be implemented')

    @commands.command()
    async def uptime(self, ctx):
        """Prints out how long the bot has been online """
        await ctx.send('Uptime: **{}**'.format(self._get_bot_uptime()))

    @commands.command()
    async def about(self, ctx):
        """Prints out information about the bot along with invite link"""
        pooper_scooper_author_path = os.path.join(utils.get_pics_path(), POOPERSCOOPER_PICTURE)
        pooper_scooper_thumbnail_path = os.path.join(utils.get_pics_path(), COOKS_AND_MOCHA)
        pooper_scooper_image_path = os.path.join(utils.get_pics_path(), GOOD_BOYS_AND_GIRLS)

        pooper_scooper_author_name = 'pooper.png'
        pooper_scooper_thumbnail_name = 'thumb.png'
        pooper_scooper_image_name = 'image.png'

        pooper_scooper_author = discord.File(pooper_scooper_author_path, filename=pooper_scooper_author_name)
        pooper_scooper_thumb = discord.File(pooper_scooper_thumbnail_path, filename=pooper_scooper_thumbnail_name)
        pooper_scooper_image = discord.File(pooper_scooper_image_path, filename=pooper_scooper_image_name)
        file_list = list([pooper_scooper_author, pooper_scooper_thumb, pooper_scooper_image])

        embed = discord.Embed(
            title='PooperScooper',
            description='A personal bot made for fun and scooping your poop.',
            colour=discord.Colour.blue()
        )

        embed.set_author(name='Want to know more about me?', icon_url='attachment://{}'.format(pooper_scooper_author_name))
        embed.set_thumbnail(url='attachment://{}'.format(pooper_scooper_thumbnail_name))
        embed.set_image(url='attachment://{}'.format(pooper_scooper_image_name))

        name = 'Version'
        value = 'PooperScooper ' + VERSION
        embed.add_field(name=name, value=value, inline=True)

        name = 'Author'
        value = BOT_AUTHOR
        embed.add_field(name=name, value=value, inline=True)

        name = 'Github Link'
        value = '[https://github.com/Kommotion/PooperScooper](https://github.com/Kommotion/PooperScooper)'
        embed.add_field(name=name, value=value, inline=False)

        name = 'Library'
        value = '[Discord.py](https://github.com/Rapptz/discord.py)'
        embed.add_field(name=name, value=value, inline=True)

        name = 'Language'
        value = 'Python'
        embed.add_field(name=name, value=value, inline=True)

        embed.set_footer(text='In memory of all the good boys and girls - Junior, Cole, and Shirley.')

        await ctx.send(files=file_list, embed=embed)


def setup(bot):
    bot.add_cog(General(bot))
