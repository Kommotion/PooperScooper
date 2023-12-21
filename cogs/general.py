import typing
from typing import Literal

import discord
from discord import app_commands
import os
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.constants import *


class General(Cog):
    """Commands for utilities related to Discord or the Bot itself. """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener('on_command')
    async def update_command_count(self, ctx) -> None:
        self.bot.commands_executed += 1

    def _get_bot_uptime(self):
        """Returns the uptime of the bot."""
        now = discord.utils.utcnow()
        delta = now - self.bot.uptime
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        if days:
            fmt = '{d} days, {h} hours, {m} minutes, and {s} seconds'
        else:
            fmt = '{h} hours, {m} minutes, and {s} seconds'

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @commands.is_owner()
    @commands.command()
    async def servers(self, ctx):
        """Servers that the bot is currently connected to."""
        if len(self.bot.guilds) > 25:
            embed = discord.Embed(title=f'Servers connected to', description=str(self.bot.guilds),
                                  colour=discord.Colour.blue())
        else:
            embed = discord.Embed(title=f'Servers Connected', colour=discord.Colour.blue())
            for server in self.bot.guilds:
                msg = f"Owner: {server.owner}\n"
                msg += f"Members: {len(server.members)}"
                embed.add_field(name=server.name, value=msg, inline=False)
        await ctx.send(embed=embed)

    @app_commands.command(name="command-1")
    async def my_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Hello from command 1!", ephemeral=True)

    @commands.is_owner()
    @commands.guild_only()
    @commands.command()
    async def sync(self, ctx: commands.Context, guilds: commands.Greedy[discord.Object], spec: typing.Optional[Literal["~", "*", "^"]] = None) -> None:
        """Owner-only. Remember: Has usage restrictions."""
        if not guilds:
            if spec == "~":
                synced = await ctx.bot.tree.sync(guild=ctx.guild)
            elif spec == "*":
                ctx.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await ctx.bot.tree.sync(guild=ctx.guild)
            elif spec == "^":
                ctx.bot.tree.clear_commands(guild=ctx.guild)
                await ctx.bot.tree.sync(guild=ctx.guild)
                synced = []
            else:
                synced = await ctx.bot.tree.sync()

            await ctx.send(
                f"Synced {len(synced)} commands {'globally' if spec is None else 'to the current guild.'}"
            )
            return

        ret = 0
        for guild in guilds:
            try:
                await ctx.bot.tree.sync(guild=guild)
            except discord.HTTPException:
                pass
            else:
                ret += 1

    @commands.command()
    async def stats(self, ctx):
        """Prints out the stats of the bot."""
        async with ctx.typing():
            embed = discord.Embed(
                title='Pooper Stats 📝',
                colour=discord.Colour.blue()
            )
            name = 'Commands Processed'
            value = self.bot.commands_executed
            embed.add_field(name=name, value=value, inline=True)

            name = 'Members Connected'
            value = str(len(self.bot.users))
            embed.add_field(name=name, value=value, inline=True)

            name = 'Total Uptime'
            value = self._get_bot_uptime()
            embed.add_field(name=name, value=value, inline=True)

            name = 'Cogs Loaded'
            value = '{}: '.format(len(self.bot.cogs))
            for cog in self.bot.cogs:
                value += '{}, '.format(cog)
            embed.add_field(name=name, value=value.rstrip(', '), inline=True)

            await ctx.send(embed=embed)

    @commands.command()
    async def uptime(self, ctx):
        """Prints out how long the bot has been online."""
        await ctx.send('Uptime: **{}**'.format(self._get_bot_uptime()))

    @commands.command()
    async def about(self, ctx):
        """Displays information about the bot."""
        async with ctx.typing():
            pics_path = utils.get_pics_path()
            pooper_scooper_author_path = os.path.join(pics_path, POOPERSCOOPER_PICTURE)
            pooper_scooper_thumbnail_path = os.path.join(pics_path, COOKS_AND_MOCHA)
            pooper_scooper_image_path = os.path.join(pics_path, GOOD_BOYS_AND_GIRLS)

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

            attachment = 'attachment://{}'.format(pooper_scooper_author_name)
            embed.set_author(name='Want to know more about me?', icon_url=attachment)
            embed.set_thumbnail(url='attachment://{}'.format(pooper_scooper_thumbnail_name))
            embed.set_image(url='attachment://{}'.format(pooper_scooper_image_name))

            name = 'Version'
            value = 'PooperScooper ' + VERSION
            embed.add_field(name=name, value=value, inline=True)

            name = 'Author'
            value = BOT_AUTHOR
            embed.add_field(name=name, value=value, inline=True)

            name = 'Github Link'
            value = 'https://github.com/Kommotion/PooperScooper'
            embed.add_field(name=name, value=value, inline=False)

            name = 'Library'
            value = '[Discord.py](https://github.com/Rapptz/discord.py)'
            embed.add_field(name=name, value=value, inline=True)

            name = 'Language'
            value = 'Python'
            embed.add_field(name=name, value=value, inline=True)

            embed.set_footer(text='In memory of all the good boys and girls - Junior, Cole, and Shirley.')

            await ctx.send(files=file_list, embed=embed)


async def setup(bot):
    await bot.add_cog(General(bot))
