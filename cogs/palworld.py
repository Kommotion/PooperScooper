from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from enum import Enum
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from loguru import logger

from cogs.utils.checks import is_menace_guild
from cogs.utils.constants import (
    MENACES_TO_SOBRIETY_SERVER_ID,
    ONE_HOUR_IN_SECONDS,
    PALWORLD_JSON,
    PALWORLD_UTIL_PATH,
    SECONDS_IN_HOUR,
    THIRTY_SECONDS,
    THUMBS_UP_EMOJI,
)
from cogs.utils.json_store import LockedJsonFile
from cogs.utils.palworld_settings import load_palworld_settings
from cogs.utils.palworld_utils.palworld_util import PalworldUtil
from cogs.utils.palworld_utils.util import check_for_process
from cogs.utils.server_config import get_guild_config
from cogs.utils.utils import calculate_hours_elapsed

log = logging.getLogger(__name__)

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils", "palworld_utils", "logs")
SERVER_TIMES_FILENAME = os.path.join(PALWORLD_UTIL_PATH, "server_times.json")
PALWORLD_JSON_WITH_PATH = os.path.join(PALWORLD_UTIL_PATH, PALWORLD_JSON)
LAST_RESTART = "last_restart"
LAST_BACKUP = "last_backup"


class State(Enum):
    OFF = 0
    ON = 1
    UNKNOWN = 2

class ServerTimes:
    def __init__(self, settings):
        self._store = LockedJsonFile(SERVER_TIMES_FILENAME, default=dict, indent=2)
        self.last_restart: float | None = None
        self.last_backup: float | None = None
        self.read_from_json()
        self.verify_valid_times()
        self.log_initial_timers(settings)

    def verify_valid_times(self) -> None:
        bad_data = False
        if not self.last_restart:
            self.last_restart = time.time()
            bad_data = True
            logger.warning("Last Restart data was bad!")
        if not self.last_backup:
            self.last_backup = time.time()
            bad_data = True
            logger.warning("Last backup data was bad!")
        if bad_data:
            self.dump_server_times()

    def dump_server_times(self) -> None:
        self._store.write({
            LAST_RESTART: self.last_restart,
            LAST_BACKUP: self.last_backup,
        })
        logger.info("Finished updating the server JSON")

    def read_from_json(self) -> None:
        data = self._store.read()
        self.last_restart = float(data[LAST_RESTART]) if data.get(LAST_RESTART) else None
        self.last_backup = float(data[LAST_BACKUP]) if data.get(LAST_BACKUP) else None

    def log_initial_timers(self, settings) -> None:
        last_restart = datetime.datetime.fromtimestamp(self.last_restart).strftime("%c")
        last_backup = datetime.datetime.fromtimestamp(self.last_backup).strftime("%c")
        logger.info("Last Restart: %s Last Backup: %s", last_restart, last_backup)
        if settings.automatic_restart:
            logger.info("Server configured to restart every: %s hours", settings.automatic_restart_every_x_hours)
        if settings.backup_every_x_hours > 0:
            logger.info("Server configured to backup every: %s hours", settings.backup_every_x_hours)

    async def update_last_restart(self) -> None:
        self.last_restart = time.time()
        self.dump_server_times()

    async def update_last_backup(self) -> None:
        self.last_backup = time.time()
        self.dump_server_times()


class PalWorld(Cog):
    """PalWorld Server commands for MTS Server. """

    palworld_app = app_commands.Group(
        name="palworld",
        description="Palworld server info",
        guild_ids=[MENACES_TO_SOBRIETY_SERVER_ID],
    )

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.last_idle_check = time.time()
        self.settings = load_palworld_settings()

        try:
            palworld_credentials = self.get_json()
            steamcmd_dir = palworld_credentials["STEAMCMD_DIR"]
            server_name = palworld_credentials["SERVER_NAME"]
            server_ip = palworld_credentials["SERVER_IP"]
            rcon_password = palworld_credentials["RCON_PASSWORD"]
            rcon_port = int(palworld_credentials["RCON_PORT"])
        except KeyError as e:
            log.error("Palworld JSON config is missing required keys.")
            raise e

        if self.settings.rotate_logs_every_x_runs > 0:
            logs_path = Path(LOGS_DIR)
            if not os.path.exists(logs_path):
                logger.info("Creating logs dir: %s", logs_path)
                logs_path.mkdir(exist_ok=True)
            logger.add(
                logs_path / "log_{time}.txt",
                level=self.settings.log_level,
                colorize=False,
                backtrace=True,
                diagnose=True,
                retention=self.settings.rotate_logs_every_x_runs,
            )

        self.pal = PalworldUtil(
            steamcmd_dir,
            server_name,
            server_ip,
            rcon_port,
            rcon_password,
            operating_system=self.settings.operating_system,
        )

        if self.settings.rotate_after_x_backups > 0:
            self.pal.rotate_after_x_backups = self.settings.rotate_after_x_backups
        else:
            self.pal.rotate_backups = False
        self.pal.wait_before_restart_seconds = self.settings.wait_before_restart_seconds
        self.server_times = ServerTimes(self.settings)

        # Default server state should be whatever state the server is in, whether it's on or off
        # If someone starts server, set the desired state to ON
        # If someone shuts down the server, set the desired state to OFF
        # If server is idle after some time, set the desired state to OFF after shutting down
        self.desired_server_state = State.UNKNOWN
        self.server_state = State.UNKNOWN
        self.palworld_server_watcher_loop.start()
        log.info("Started palworld server watcher")

    @tasks.loop(seconds=THIRTY_SECONDS)
    async def palworld_server_watcher_loop(self) -> None:
        # Get the current server state
        server_state = await self.get_server_state()

        # Restart the server if the server is off but should be on
        if server_state == State.OFF and self.desired_server_state == State.ON:
            log.info(f"Server process not found while server should be on, restarting...")
            logger.info(f"Server process not found while server should be on, restarting...")
            await self.pal.launch_server()
            await self.server_times.update_last_restart()
            logger.info("Next server restart in: %s hours", self.settings.automatic_restart_every_x_hours)

        settings = self.settings

        if (
            0 < settings.backup_every_x_hours <= calculate_hours_elapsed(self.server_times.last_backup)
            and self.desired_server_state == State.ON
        ):
            logger.info("Taking server backup...")
            await self.pal.take_server_backup()
            await self.server_times.update_last_backup()
            logger.info("Next backup in: %s hours", settings.backup_every_x_hours)

        if settings.automatic_restart and self.desired_server_state == State.ON:
            hours_since_last_restart = calculate_hours_elapsed(self.server_times.last_restart)
            if hours_since_last_restart >= settings.automatic_restart_every_x_hours:
                await self.pal.log_and_broadcast(f"Restarting server after {hours_since_last_restart} hours...")
                if not check_for_process(self.pal.palworld_server_proc_name):
                    logger.info("Server process not found, restarting...")
                    await self.pal.launch_server()
                    await self.server_times.update_last_restart()
                    logger.info("Next server restart in: %s hours", settings.automatic_restart_every_x_hours)
                else:
                    await self.pal.restart_server(backup_server=settings.backup_on_restart)
                    await self.server_times.update_last_restart()
                    await self.pal.log_and_broadcast(
                        f"Next server restart in: {settings.automatic_restart_every_x_hours} hours"
                    )

        # Stop the server if the server is idle (And it has been 8 hours). The function already checks if the
        # Server is on or not before doing anything
        time_now = time.time()
        if time_now - self.last_idle_check > ONE_HOUR_IN_SECONDS:
            await self.auto_shutdown_server_if_idle()
            self.last_idle_check = time.time()

    def get_json(self):
        with open(PALWORLD_JSON_WITH_PATH, "r") as f:
            return json.load(f)
    async def auto_shutdown_server_if_idle(self) -> None:
        """Automatically shuts down the server if the server is idle. """
        log.info("Checking if Palworld server is idle to stop it")

        if not await self.is_server_on():
            log.debug("Palworld Watcher State is OFF. Skipping Idle Check")
            return

        if not await self.is_server_empty():
            log.info("Palworld server is not empty. Finishing auto shutdown check")
            return

        await self.stop_server()
        log.info("Finished Palworld server stop")

    async def get_server_state(self):
        """Returns State.OFF if the server process is not found, else State.ON"""
        if not await check_for_process(self.pal.palworld_server_proc_name):
            self.server_state = State.OFF
            return State.OFF
        self.server_state = State.ON
        return State.ON

    @palworld_server_watcher_loop.before_loop
    async def before_server_watcher(self) -> None:
        await self.bot.wait_until_ready()
        # We don't know the desired server state, so let's see if it's on or off and
        # set that as the desired server state
        self.desired_server_state = await self.get_server_state()
        log.info(f"Current Palworld Server State: {self.desired_server_state}")

    @commands.group(invoke_without_command=True)
    @is_menace_guild()
    async def palworld(self, ctx: commands.Context) -> None:
        """Do "!help palworld" for subcommands. """
        await ctx.send('Do "!help palworld" for subcommands.')

    @palworld.command(name="start")
    @is_menace_guild()
    async def palworld_start(self, ctx: commands.Context):
        """Starts the Palworld server if it is off. """
        await ctx.send('Attempting to start Palworld server... this might take a minute.')

        try:
            await self.start_server()
        except:
            await ctx.send('An error occurred while starting the Palworld server... sorry!')
            return

        if await self.is_server_on():
            await ctx.send('Palworld server is on!')
            await ctx.message.add_reaction(THUMBS_UP_EMOJI)
        else:
            await ctx.send('Palworld server start process executed successfully, but server did not start... uhhh...')


    @palworld.command(name="stop")
    @is_menace_guild()
    async def palworld_stop(self, ctx: commands.Context):
        """Stops the Palworld server if it is on. """
        await ctx.send('Attempting to stop Palworld server... this might take a minute.')

        try:
            await self.stop_server()
        except:
            await ctx.send('An error occurred while stopping the Palworld server... sorry!')
            return

        if await self.is_server_on():
            await ctx.send('The Palworld server is somehow still running... uhhhhh...')
        else:
            await ctx.send('The Palworld server is off!')
            await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="restart")
    @is_menace_guild()
    async def palworld_restart(self, ctx: commands.Context):
        """Restarts the Palworld Server. """
        await ctx.send("Initiating server restart process... this might take a minute.")
        if await self.get_server_state() == State.OFF:
            await self.start_server()
        else:
            await self.pal.restart_server()
        await ctx.send("Server restart process completed.")
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="players")
    @is_menace_guild()
    async def palworld_players(self, ctx: commands.Context):
        """Shows the output of the players connected to the server. """
        msg = await self.show_players()
        await ctx.send(msg)

    @palworld.command(name="state")
    @is_menace_guild()
    async def palworld_state(self, ctx: commands.Context):
        """Shows if the Palworld server is currently on or off. """
        state = await self.is_server_on()
        if state:
            await ctx.send("The server is currently on.")
        else:
            await ctx.send("The server is currently off.")

    @palworld.command(name="status")
    @is_menace_guild()
    async def palworld_status(self, ctx: commands.Context):
        """Shows a detailed Palworld server status embed."""
        embed = await self.build_status_embed()
        await ctx.send(embed=embed)

    @palworld_app.command(name="players", description="Show players currently on the Palworld server.")
    async def app_palworld_players(self, interaction: discord.Interaction):
        msg = await self.show_players()
        await interaction.response.send_message(msg)

    @palworld_app.command(name="state", description="Show whether the Palworld server is on or off.")
    async def app_palworld_state(self, interaction: discord.Interaction):
        online = await self.is_server_on()
        await interaction.response.send_message(
            "The server is currently **on**." if online else "The server is currently **off**.",
        )

    @palworld_app.command(name="status", description="Detailed Palworld server status.")
    async def app_palworld_status(self, interaction: discord.Interaction):
        embed = await self.build_status_embed()
        await interaction.response.send_message(embed=embed)

    @palworld.command(name="save")
    @is_menace_guild()
    async def palworld_save(self, ctx: commands.Context):
        """Saves the current state of the server. """
        response = await self.save()
        if not response:
            await ctx.send("Server is not on, cannot save.")
            return
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="is_empty")
    @commands.is_owner()
    async def palworld_empty(self, ctx: commands.Context):
        """Is the server empty? Debug command, owner only. """
        response = await self.pal.rcon.send_command("ShowPlayers", [])
        log.info(response)
        lines = response.strip().split("\n")
        log.info(lines)
        log.info(True if len(lines) <= 1 else False)

    async def is_server_empty(self) -> bool:
        """Returns True if server is empty else False. """
        response = await self.pal.rcon.send_command("ShowPlayers", [])
        lines = response.strip().split("\n")
        # If there are more than 1 lines, that means that there are players in the server
        return True if len(lines) <= 1 else False

    async def get_player_count(self) -> int:
        if not await self.is_server_on():
            return 0
        response = await self.pal.rcon.send_command("ShowPlayers", [])
        lines = response.strip().split("\n")
        return max(0, len(lines) - 1)

    async def build_status_embed(self) -> discord.Embed:
        settings = self.settings
        online = await self.is_server_on()
        player_count = await self.get_player_count() if online else 0
        hours_since_restart = calculate_hours_elapsed(self.server_times.last_restart)
        hours_since_backup = calculate_hours_elapsed(self.server_times.last_backup)
        restart_in = max(0, settings.automatic_restart_every_x_hours - hours_since_restart)
        backup_in = max(0, settings.backup_every_x_hours - hours_since_backup) if settings.backup_every_x_hours > 0 else None

        colour = discord.Colour.green() if online else discord.Colour.red()
        embed = discord.Embed(
            title="Palworld Server Status",
            colour=colour,
        )
        embed.add_field(name="Process", value="Online" if online else "Offline", inline=True)
        embed.add_field(name="Desired state", value=self.desired_server_state.name, inline=True)
        embed.add_field(name="Players", value=str(player_count), inline=True)
        embed.add_field(
            name="Next scheduled restart",
            value=f"~{restart_in:.1f}h" if settings.automatic_restart else "Disabled",
            inline=True,
        )
        if backup_in is not None:
            embed.add_field(name="Next scheduled backup", value=f"~{backup_in:.1f}h", inline=True)
        embed.set_footer(text=f"Auto-restart every {settings.automatic_restart_every_x_hours}h")
        return embed

    async def show_players(self) -> str:
        """Returns the output of the ShowPlayers RCON command. """
        if not await self.is_server_on():
            return "The Server is off so there are no players online."

        response = await self.pal.rcon.send_command("ShowPlayers", [])
        lines = response.strip().split("\n")

        if len(lines) <= 1:  # If only the header exists, no players are online
            return "No players are currently online."

        players = [line.split(",")[0] for line in lines[1:]]
        player_count = len(players)
        player_list = ", ".join(players)
        return f"```Players online: {player_count}\n{player_list}\n```"

    async def is_server_on(self) -> bool:
        """Returns True if server is still on and off if the server is off. """
        await self.get_server_state()
        log.debug(f"Server state: {self.server_state}")
        return self.server_state == State.ON

    async def start_server(self) -> None:
        """Attempts to start the server if it is not already on. """
        if await self.is_server_on():
            log.debug("The state was already ON when attempting to start it")
            return

        await self.pal.launch_server()
        self.desired_server_state = State.ON
        await self.server_times.update_last_restart()
        log.info("Palworld Server Started")

    async def stop_server(self) -> None:
        """Stops the server. """
        self.desired_server_state = State.OFF

        if not await self.is_server_on():
            log.debug("The state was already OFF when attempting to stop it")
            return

        # Shut down the Palworld Server
        wait_time = self.settings.wait_before_restart_seconds
        shutdown_warning_msg = f"SERVER SHUTDOWN INCOMING. Waiting {wait_time} seconds before starting shutdown process."
        await self.pal.log_and_broadcast(shutdown_warning_msg)
        await asyncio.sleep(wait_time)
        await self.pal.log_and_broadcast("Starting server shutdown process.")
        await self.pal.save_server_state()
        await asyncio.sleep(1)
        await self.pal.rcon.send_command("Shutdown")
        await asyncio.sleep(60)
        await self.get_server_state()  # Refresh the server state
        log.info("Palworld Server Stopped")

    async def save(self) -> bool:
        """"Saves the server state. """
        if not await self.is_server_on():
            return False
        return await self.pal.save_server_state()


async def setup(bot) -> None:
    await bot.add_cog(PalWorld(bot))
