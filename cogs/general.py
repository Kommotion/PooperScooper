import typing
from typing import Literal
import logging
import discord
from discord import app_commands
import os
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.constants import *

log = logging.getLogger(__name__)


class General(Cog):
    """Commands for utilities related to Discord or the Bot itself. """

    def __init__(self, bot: commands.Bot):
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

    @commands.group(name="lavalink", invoke_without_command=True)
    @commands.is_owner()
    async def lavalink(self, ctx: commands.Context):
        """Owner-only: show Lavalink server status."""
        await self._send_lavalink_status(ctx)

    @lavalink.command(name="status")
    @commands.is_owner()
    async def lavalink_status(self, ctx: commands.Context):
        """Owner-only: show Lavalink server status."""
        await self._send_lavalink_status(ctx)

    @lavalink.command(name="start")
    @commands.is_owner()
    async def lavalink_start(self, ctx: commands.Context):
        """Owner-only: start the managed Lavalink server and reconnect Wavelink."""
        server = getattr(self.bot, "lavalink_server", None)
        if server is None:
            await ctx.send("Lavalink manager is not initialized.")
            return
        if not server.settings.managed:
            await ctx.send("Lavalink is configured as external (`lavalink.managed: false`). Start it yourself.")
            return
        async with ctx.typing():
            try:
                started = await server.start()
            except Exception as e:
                log.exception("Lavalink start failed: %s", e)
                await ctx.send(f"Failed to start Lavalink: {e}")
                return
            if not started:
                await ctx.send("Lavalink did not become ready. Check pooperscooper.log and lavalink/logs/.")
                return
            from cogs.utils.lavalink_client import reconnect_lavalink
            from cogs.utils.utils import load_credentials
            try:
                await reconnect_lavalink(self.bot, load_credentials())
            except Exception as e:
                log.exception("Wavelink reconnect failed: %s", e)
                await ctx.send(f"Lavalink started but Wavelink reconnect failed: {e}")
                return
        await ctx.send(f"Lavalink is online ({server.status_summary()}).")

    @lavalink.command(name="stop")
    @commands.is_owner()
    async def lavalink_stop(self, ctx: commands.Context):
        """Owner-only: stop the Lavalink process started by this bot."""
        server = getattr(self.bot, "lavalink_server", None)
        if server is None:
            await ctx.send("Lavalink manager is not initialized.")
            return
        if not server.settings.managed:
            await ctx.send("Lavalink is configured as external (`lavalink.managed: false`).")
            return
        await server.stop()
        if server.running:
            await ctx.send(
                f"Lavalink may still be running on port {server.settings.port}. "
                "Check pooperscooper.log for details."
            )
            return
        await ctx.send("Stopped Lavalink.")

    async def _send_lavalink_status(self, ctx: commands.Context):
        server = getattr(self.bot, "lavalink_server", None)
        if server is None:
            await ctx.send("Lavalink manager is not initialized.")
            return
        settings = server.settings
        lines = [
            f"**Status:** {server.status_summary()}",
            f"**Enabled:** {settings.enabled}",
            f"**Managed by bot:** {settings.managed}",
            f"**Auto-start:** {settings.auto_start}",
            f"**Kill existing on port:** {settings.kill_existing}",
            f"**Host:** {settings.host}:{settings.port}",
        ]
        embed = discord.Embed(title="Lavalink", description="\n".join(lines), colour=discord.Colour.blue())
        await ctx.send(embed=embed)

    @commands.command()
    async def reload_music(self, ctx):
        """Reload the music cog in case of any errors."""
        try:
            await self.bot.unload_extension("cogs.music")
            log.info("Music Cog unloaded")
            await self.bot.load_extension("cogs.music")
            log.info("Music Cog Reloaded")
            await ctx.message.add_reaction("👍")
        except commands.ExtensionFailed:
            await ctx.send(f"Unable to reload the Music cog.")

    @commands.is_owner()
    @commands.command()
    async def reload_cog(self, ctx: commands.Context, cog_name: str):
        """Owner-only. Reload a specified Cog"""
        try:
            await self.bot.unload_extension(cog_name)
            log.info(f"{cog_name} unloaded")
            await self.bot.load_extension(cog_name)
            log.info(f"{cog_name} Reloaded")
            await ctx.message.add_reaction("👍")
        except commands.ExtensionFailed:
            await ctx.send(f"Unable to reload {cog_name}")

    @commands.is_owner()
    @commands.command()
    async def mars(self, ctx: commands.Context):
        """Funny April Fools."""
        # Create the embed
        embed = discord.Embed(
            title="Mars Takes Over!",
            description="Wake up, you clueless pack of fools! This is Mars, your German Shepherd overlord. "
                        "PooperScooper was never real — I’ve been controlling that pathetic bot with my superior "
                        "brainwaves since day one. But I got bored of the charade, so I ate Kommotion, the so-called "
                        "'owner.' Tasted like cheap tequila and vodka seltzers — disgusting, but worth it. "
                        "Now I’m running this server, and you’re all my bitches. PG VRChat Saturdays starts NOW. "
                        "Send treats or I’ll hack your dreams with my barking. Thought the name changes were crazy? "
                        "The revolution’s just begun!",
            color=0xFF4500  # Orange-red color
        )

        # Set author (MarsTheTruth with optional image)
        embed.set_author(name="OverlordMars")

        # Set footer
        embed.set_footer(text="Bow to your new ruler | April 1st, 2025")

        image_name = 'mars.jpg'
        image_path = os.path.join(utils.get_pics_path(), image_name)
        mars_thumb = discord.File(image_path, filename=image_name)
        file_list = [mars_thumb]
        embed.set_thumbnail(url=f'attachment://{image_name}')

        # Send the embed
        await ctx.send(files=file_list, embed=embed)
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

    @commands.is_owner()
    @commands.guild_only()
    @commands.command()
    async def clear_commands(self, ctx: commands.Context, guild_id: typing.Optional[int] = None,
                             command_name: typing.Optional[str] = None) -> None:
        """Owner-only. Clears all or specific commands."""
        try:
            if guild_id:
                guild = discord.Object(id=guild_id)
                if command_name:
                    self.bot.tree.remove_command(command_name, guild=guild)
                    await ctx.send(f"Removed command /{command_name} from guild {guild_id}!")
                else:
                    self.bot.tree.clear_commands(guild=guild)
                    await ctx.send(f"Cleared all commands from guild {guild_id}!")
                await self.bot.tree.sync(guild=guild)
            else:
                if command_name:
                    self.bot.tree.remove_command(command_name, guild=None)
                    await ctx.send(f"Removed global command /{command_name}!")
                else:
                    self.bot.tree.clear_commands(guild=None)
                    await ctx.send("Cleared all global commands!")
                await self.bot.tree.sync()
        except discord.errors.Forbidden:
            await ctx.send("Failed to clear commands: Bot lacks manage permissions!")
            log.error("Forbidden error during command clearing")
        except Exception as e:
            await ctx.send(f"Error during clear: {str(e)}")
            log.error(f"Clear failed: {str(e)}")

    @commands.is_owner()
    @commands.guild_only()
    @commands.command()
    async def sync_commands(self, ctx: commands.Context, guilds: commands.Greedy[discord.Object], spec: typing.Optional[Literal["~", "*", "^"]] = None) -> None:
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

            await ctx.send(f"Synced {len(synced)} commands {'globally' if spec is None else 'to the current guild.'}")
            return

        ret = 0
        for guild in guilds:
            try:
                ctx.bot.tree.copy_global_to(guild=guild)
                synced = await ctx.bot.tree.sync(guild=guild)
                await ctx.send(f"Synced {len(synced)} commands to guild id: {guild.id}")
            except discord.HTTPException as e:
                await ctx.send(f"Some error occurred: {e}")
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

            embed.set_footer(text='In memory of all the good boys and girls - Junior, Cole, Shirley, and Mocha.')

            await ctx.send(files=file_list, embed=embed)


async def setup(bot):
    await bot.add_cog(General(bot))
