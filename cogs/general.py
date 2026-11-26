import typing
from typing import Literal
import logging
import discord
from discord import app_commands
import os
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.checks import is_admin_or_illuminaughty
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

    @commands.is_owner()
    @commands.command()
    async def reload_music(self, ctx):
        """Owner-only. Reload the music cog. Drops the current voice connection."""
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

    def _status_dot(self, kind: str) -> str:
        if kind == "healthy":
            return "🟢"
        if kind == "down":
            return "🔴"
        return "⚪"

    def _health_colour(self, unhealthy: bool) -> discord.Colour:
        if unhealthy:
            return discord.Colour.from_rgb(243, 156, 18)
        return discord.Colour.from_rgb(46, 204, 113)

    def _set_status_thumbnail(self, embed: discord.Embed) -> None:
        user = getattr(self.bot, "user", None)
        if user is None:
            return
        avatar = getattr(user, "display_avatar", None)
        url = getattr(avatar, "url", None) if avatar is not None else None
        if url:
            embed.set_thumbnail(url=url)

    def _show_status_value(self, value) -> str:
        if value is None or value == "":
            return "n/a"
        return str(value)

    def _format_health_check(self, value) -> str:
        if value is None or value == "":
            return "n/a"
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            return "n/a"
        from datetime import datetime, timezone
        try:
            datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return "n/a"
        return f"<t:{int(stamp)}:R>"

    def _yes_no(self, value) -> str:
        if value is None or value == "":
            return "n/a"
        return "Yes" if value else "No"

    def _endpoint(self, host, port) -> str:
        host_missing = host is None or host == ""
        port_missing = port is None or port == ""
        if host_missing and port_missing:
            return "n/a"
        return f"{self._show_status_value(host)}:{self._show_status_value(port)}"

    def _fenced_status_error(self, text: str) -> str:
        prefix = "```text\n"
        suffix = "\n```"
        budget = 1024 - len(prefix) - len(suffix)
        raw = str(text)
        if len(raw) > budget:
            raw = raw[:max(budget, 0)]
        value = prefix + raw + suffix
        if len(value) > 1024:
            value = value[:1024]
        return value

    def _read_comfy_status(self) -> dict:
        from cogs.utils.comfy_client import get_comfy_status
        status = get_comfy_status()
        return status if isinstance(status, dict) else {}

    def _wavelink_connection(self) -> str:
        try:
            import wavelink
        except Exception:
            return "unknown"
        try:
            nodes = wavelink.Pool.nodes
        except Exception:
            return "unknown"
        if nodes:
            return "connected"
        return "not connected"

    def _lavalink_origin(self, server) -> str:
        raw = getattr(server, "_started_by_bot", None)
        if raw is None:
            raw = getattr(server, "started_by_bot", False)
        return "bot-managed" if raw else "external"

    async def _image_facts(self, *, details: bool) -> dict:
        image_cog = self.bot.get_cog("ImageDiffusion")
        loaded = image_cog is not None
        facts = {
            "loaded": loaded,
            "running": False,
            "queue_depth": None,
            "queue_max": None,
            "startup_error": None,
            "comfy": None,
        }
        if loaded:
            queue = getattr(image_cog, "image_queue", None)
            if queue is not None:
                facts["queue_depth"] = queue.qsize()
                facts["queue_max"] = getattr(queue, "maxsize", None)
            if details:
                facts["startup_error"] = getattr(image_cog, "comfy_startup_error", None)
        if loaded or details:
            comfy = self._read_comfy_status()
            facts["running"] = bool(comfy.get("running"))
            if details:
                facts["comfy"] = comfy
        return facts

    def _lavalink_facts(self) -> dict:
        settings = getattr(self.bot, "lavalink_settings", None)
        enabled = bool(settings and getattr(settings, "enabled", False))
        facts = {
            "enabled": enabled,
            "online": False,
            "manager": False,
            "host": None,
            "port": None,
            "origin": None,
            "wavelink": "disabled",
        }
        if not enabled:
            return facts
        server = getattr(self.bot, "lavalink_server", None)
        facts["manager"] = server is not None
        if server is not None:
            facts["online"] = bool(getattr(server, "running", False))
            facts["origin"] = self._lavalink_origin(server)
            server_settings = getattr(server, "settings", None)
            if server_settings is not None:
                facts["host"] = getattr(server_settings, "host", None)
                facts["port"] = getattr(server_settings, "port", None)
        facts["wavelink"] = self._wavelink_connection()
        return facts

    async def _palworld_facts(self) -> dict:
        pal_cog = self.bot.get_cog("PalWorld")
        if pal_cog is None:
            return {"loaded": False, "online": False, "players": 0}
        online = bool(await pal_cog.is_server_on())
        players = await pal_cog.get_player_count() if online else 0
        return {"loaded": True, "online": online, "players": players}

    def _status_unhealthy(self, image: dict, lavalink: dict, pal: dict) -> bool:
        if image["loaded"] and not image["running"]:
            return True
        if lavalink["enabled"] and not lavalink["online"]:
            return True
        if lavalink["enabled"] and lavalink["wavelink"] != "connected":
            return True
        if pal["loaded"] and not pal["online"]:
            return True
        return False

    def _public_image_line(self, image: dict) -> str:
        if not image["loaded"]:
            return "⚪ **Image Gen** — Not loaded"
        if not image["running"]:
            return "🔴 **Image Gen** — Offline"
        depth = image["queue_depth"]
        maximum = image["queue_max"]
        if depth is None or maximum is None:
            queue = "Queue n/a"
        else:
            queue = f"Queue {depth}/{maximum}"
        return f"🟢 **Image Gen** — Online · {queue}"

    def _public_music_line(self, lavalink: dict) -> str:
        if not lavalink["enabled"]:
            return "⚪ **Music** — Disabled"
        if not lavalink["online"]:
            return "🔴 **Music** — Offline"
        if lavalink["wavelink"] == "connected":
            return "🟢 **Music** — Online · Wavelink connected"
        return f"🟡 **Music** — Degraded · Wavelink {lavalink['wavelink']}"

    def _public_pal_line(self, pal: dict) -> str:
        if not pal["loaded"]:
            return "⚪ **Palworld** — Not loaded"
        if not pal["online"]:
            return "🔴 **Palworld** — Offline"
        return f"🟢 **Palworld** — Online · {pal['players']} players"

    def _admin_queue(self, image: dict) -> str:
        if not image["loaded"]:
            return "n/a"
        depth = image["queue_depth"]
        maximum = image["queue_max"]
        if depth is None or maximum is None:
            return "n/a"
        return f"{depth}/{maximum}"

    def _admin_comfy_value(self, image: dict) -> str:
        comfy = image["comfy"] if isinstance(image.get("comfy"), dict) else None
        lines = []
        if image["loaded"] or comfy is not None:
            if comfy is not None and "running" in comfy and comfy.get("running") is not None:
                if comfy.get("running"):
                    dot = self._status_dot("healthy")
                    label = "Running"
                else:
                    dot = self._status_dot("down")
                    label = "Down"
            else:
                dot = self._status_dot("disabled")
                label = "N/A"
            ready = self._yes_no(None if comfy is None else comfy.get("ready"))
            owned = self._yes_no(None if comfy is None else comfy.get("bot_owns_process"))
            queue = self._admin_queue(image)
            lines.append(
                f"{dot} **{label}** (Ready: `{ready}` · Bot-managed: `{owned}` · Queue: `{queue}`)"
            )
        host = None if comfy is None else comfy.get("host")
        port = None if comfy is None else comfy.get("port")
        pid = self._show_status_value(None if comfy is None else comfy.get("pid"))
        source = self._show_status_value(None if comfy is None else comfy.get("source"))
        root = self._show_status_value(None if comfy is None else comfy.get("root"))
        checked = self._format_health_check(None if comfy is None else comfy.get("last_health_check"))
        lines.append(
            f"Endpoint: `{self._endpoint(host, port)}`  •  PID: `{pid}`  •  Source: `{source}`"
        )
        lines.append(f"Root: `{root}`")
        lines.append(f"Last check: {checked}")
        return "\n".join(lines)

    def _admin_music_value(self, lavalink: dict) -> str:
        if not lavalink["enabled"]:
            return f"{self._status_dot('disabled')} Disabled"
        if lavalink["online"]:
            lav_dot = self._status_dot("healthy")
            lav_label = "Online"
        else:
            lav_dot = self._status_dot("down")
            lav_label = "Offline"
        origin = lavalink.get("origin") or "n/a"
        wave = lavalink["wavelink"]
        wave_dot = self._status_dot("healthy" if wave == "connected" else "down")
        endpoint = self._endpoint(lavalink.get("host"), lavalink.get("port"))
        return (
            f"Lavalink: {lav_dot} **{lav_label}** ({origin})  •  "
            f"Wavelink: {wave_dot} **{wave}**\n"
            f"Endpoint: `{endpoint}`"
        )

    def _admin_pal_value(self, pal: dict, image: dict) -> str:
        cogs = len(self.bot.cogs)
        image_state = "loaded" if image["loaded"] else "not loaded"
        tail = f"Cogs: **{cogs}**  •  ImageDiffusion: **{image_state}**"
        if not pal["loaded"]:
            return f"Palworld: {self._status_dot('disabled')} **Not loaded**  •  {tail}"
        if pal["online"]:
            state = f"{self._status_dot('healthy')} **Online** ({pal['players']} players)"
        else:
            state = f"{self._status_dot('down')} **Offline** ({pal['players']} players)"
        return f"Palworld: {state}  •  {tail}"

    async def build_public_status_embed(self) -> discord.Embed:
        image = await self._image_facts(details=False)
        lavalink = self._lavalink_facts()
        pal = await self._palworld_facts()
        unhealthy = self._status_unhealthy(image, lavalink, pal)
        if unhealthy:
            banner = "🟡 **Some services degraded**"
        else:
            banner = "🟢 **All systems operational**"
        uptime = self._get_bot_uptime()
        description = (
            f"{banner}\n"
            f"v{VERSION}  •  Up {uptime}  •  [GitHub](https://github.com/Kommotion/PooperScooper)"
        )
        embed = discord.Embed(
            title="PooperScooper Status",
            description=description,
            colour=self._health_colour(unhealthy),
        )
        embed.timestamp = discord.utils.utcnow()
        self._set_status_thumbnail(embed)
        overview = (
            f"Servers: **{len(self.bot.guilds)}**  •  "
            f"Members: **{len(self.bot.users)}**  •  "
            f"Commands: **{self.bot.commands_executed}**  •  "
            f"Cogs: **{len(self.bot.cogs)}**"
        )
        services = "\n".join((
            self._public_image_line(image),
            self._public_music_line(lavalink),
            self._public_pal_line(pal),
        ))
        embed.add_field(name="📊 Overview", value=overview, inline=False)
        embed.add_field(name="⚙️ Services", value=services, inline=False)
        embed.set_footer(text=f"PooperScooper v{VERSION}")
        return embed

    async def build_admin_status_embed(self) -> discord.Embed:
        image = await self._image_facts(details=True)
        lavalink = self._lavalink_facts()
        pal = await self._palworld_facts()
        from cogs.utils.server_config import get_server_config_manager
        configured = get_server_config_manager().list_configured_guild_ids()
        count = len(configured)
        noun = "config" if count == 1 else "configs"
        uptime = self._get_bot_uptime()
        description = f"v{VERSION}  •  Up {uptime}  •  {count} server {noun}"
        embed = discord.Embed(
            title="PooperScooper Admin Diagnostics",
            description=description,
            colour=discord.Colour.from_rgb(88, 101, 242),
        )
        embed.timestamp = discord.utils.utcnow()
        self._set_status_thumbnail(embed)
        embed.add_field(name="🎨 ComfyUI", value=self._admin_comfy_value(image), inline=False)
        startup_error = image["startup_error"]
        if startup_error:
            embed.add_field(
                name="⚠️ ComfyUI Startup Error",
                value=self._fenced_status_error(startup_error),
                inline=False,
            )
        embed.add_field(
            name="🎵 Lavalink & Wavelink",
            value=self._admin_music_value(lavalink),
            inline=False,
        )
        embed.add_field(
            name="🦖 Palworld & Bot",
            value=self._admin_pal_value(pal, image),
            inline=False,
        )
        embed.set_footer(text=f"PooperScooper v{VERSION} • Admin Diagnostics")
        return embed

    @commands.group(name="status", invoke_without_command=True)
    async def status(self, ctx: commands.Context):
        """Show bot health: uptime, version, and service status."""
        async with ctx.typing():
            embed = await self.build_public_status_embed()
            await ctx.send(embed=embed)

    @status.command(name="admin")
    @is_admin_or_illuminaughty()
    async def status_admin(self, ctx: commands.Context):
        """Admin-only. Show internal diagnostics, including local paths and process details."""
        async with ctx.typing():
            embed = await self.build_admin_status_embed()
            await ctx.send(embed=embed)

    @commands.command(name="gay")
    async def gay(self, ctx):
        """Tell Wolfy he's wrong about his waifus."""
        await ctx.send("Wolfy you're wrong about your waifus")


async def setup(bot):
    await bot.add_cog(General(bot))
