from discord.ext import tasks
from discord.ext.commands import Cog
import logging
import os
import asyncio
import psutil
import subprocess
from enum import Enum
from cogs.utils.checks import *
from cogs.utils.utils import load_credentials
from cogs.utils.utils import *

# Palworld utils related imports
from cogs.utils.palworld_utils.palworld_util import PalworldUtil
from cogs.utils.palworld_utils.util import check_for_process
import sys
import time
import json
import datetime
from pathlib import Path
from loguru import logger

log = logging.getLogger(__name__)

SERVER_WATCHER_IDENTIFIER = "palworld"

# User variables
AUTOMATIC_RESTART = True  # Automatically restart the server if the process isn't found.
WAIT_BEFORE_RESTART_SECONDS = 300  # Seconds to wait/warn before restart process.
AUTOMATIC_RESTART_EVERY_X_HOURS = 6  # -1 if you don't want to restart on a timer.
BACKUP_ON_RESTART = False  # Save a backup when the server restarts.
BACKUP_EVERY_X_HOURS = 4  # -1 if you don't want to backup on a timer.
ROTATE_AFTER_X_BACKUPS = 20  # -1 if you don't want to rotate backups.
ROTATE_LOGS_EVERY_X_RUNS = 10  # -1 if you don't want to log to file.
LOG_LEVEL = "INFO"
base_dir = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(base_dir, "utils", "palworld_utils", "logs")
OPERATING_SYSTEM = "windows"  # Change to "linux" if needed.
SECONDS_IN_HOUR = 3600
LOOP_SLEEP = 30

base_dir = os.path.dirname(os.path.abspath(__file__))
SERVER_TIMES_FILENAME = os.path.join(PALWORLD_UTIL_PATH, 'server_times.json')
PALWORLD_JSON_WITH_PATH = os.path.join(PALWORLD_UTIL_PATH, PALWORLD_JSON)
LAST_RESTART = 'last_restart'
LAST_BACKUP = 'last_backup'


class State(Enum):
    OFF = 0
    ON = 1
    UNKNOWN = 2

class ServerTimes:
    def __init__(self):
        self.last_restart = None
        self.last_backup = None
        self.read_from_json()
        self.verify_valid_times()
        self.log_initial_timers()

    def verify_valid_times(self):
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

    def dump_server_times(self):
        with open(SERVER_TIMES_FILENAME, 'w') as server_file:
            data = dict()
            data[LAST_RESTART] = self.last_restart
            data[LAST_BACKUP] = self.last_backup
            json.dump(data, server_file)
            logger.info("Finished updating the server JSON")

    def read_from_json(self):
        try:
            with open(SERVER_TIMES_FILENAME, 'r') as file:
                data = json.load(file)
                self.last_restart = data.get(LAST_RESTART, None)
                self.last_backup = data.get(LAST_BACKUP, None)
                # Convert to integers if data was valid
                self.last_restart = int(self.last_restart) if self.last_restart else None
                self.last_backup = int(self.last_backup) if self.last_backup else None
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f'Error reading Server times JSON file: {e}')
            raise e

    def log_initial_timers(self) -> None:
        last_restart = datetime.datetime.fromtimestamp(self.last_restart).strftime('%c')
        last_backup = datetime.datetime.fromtimestamp(self.last_restart).strftime('%c')
        logger.info(f"Last Restart: {last_restart} Last Backup: {last_backup}")
        if AUTOMATIC_RESTART:
            logger.info(f"Server configured to restart every: {AUTOMATIC_RESTART_EVERY_X_HOURS} hours")
        if BACKUP_EVERY_X_HOURS > 0:
            logger.info(f"Server configured to backup every: {BACKUP_EVERY_X_HOURS} hours")

    async def update_last_restart(self) -> None:
        self.last_restart = time.time()
        self.dump_server_times()

    async def update_last_backup(self) -> None:
        self.last_backup = time.time()
        self.dump_server_times()


class PalWorld(Cog):
    """PalWorld Server commands for MTS Server. """

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.last_idle_check = time.time()

        try:
            palworld_credentials = self.get_json()
            steamcmd_dir = palworld_credentials["STEAMCMD_DIR"]
            server_name = palworld_credentials["SERVER_NAME"]
            server_ip = palworld_credentials["SERVER_IP"]
            rcon_password = palworld_credentials["RCON_PASSWORD"]
            rcon_port = int(palworld_credentials["RCON_PORT"])
        except KeyError as e:
            logging.error(f"Palworld JSON config is not detected!. Requirements within: ")
            raise e

        if ROTATE_LOGS_EVERY_X_RUNS > 0:
            logs_path = Path(LOGS_DIR)
            if not os.path.exists(logs_path):
                logger.info(f"Creating logs dir: {logs_path}")
                logs_path.mkdir(exist_ok=True)
            # Add logging sink to file and rotate every ROTATE_LOGS_EVERY_X_RUNS runs/logs.
            logger.add(
                logs_path / "log_{time}.txt",
                level=LOG_LEVEL,
                colorize=False,
                backtrace=True,
                diagnose=True,
                retention=ROTATE_LOGS_EVERY_X_RUNS,
            )

        # Create PalworldUtil instance with required vars only.
        self.pal = PalworldUtil(
            steamcmd_dir,
            server_name,
            server_ip,
            rcon_port,
            rcon_password,
            operating_system=OPERATING_SYSTEM,
        )

        if ROTATE_AFTER_X_BACKUPS > 0:
            self.pal.rotate_after_x_backups = ROTATE_AFTER_X_BACKUPS
        else:
            self.pal.rotate_backups = False
        self.pal.wait_before_restart_seconds = WAIT_BEFORE_RESTART_SECONDS
        self.server_times = ServerTimes()

        # Default server state should be whatever state the server is in, whether it's on or off
        # If someone starts server, set the desired state to ON
        # If someone shuts down the server, set the desired state to OFF
        # If server is idle after some time, set the desired state to OFF after shutting down
        self.desired_server_state = State.UNKNOWN
        self.server_state = State.UNKNOWN
        self.palworld_server_watcher_loop.start()
        logging.info("Started palworld server watcher")

    @tasks.loop(seconds=THIRTY_SECONDS)
    async def palworld_server_watcher_loop(self) -> None:
        # Get the current server state
        server_state = await self.get_server_state()

        # Restart the server if the server is off but should be on
        if server_state == State.OFF and self.desired_server_state == State.ON:
            logging.info(f"Server process not found while server should be on, restarting...")
            logger.info(f"Server process not found while server should be on, restarting...")
            await self.pal.launch_server()
            await self.server_times.update_last_restart()
            logger.info(f"Next server restart in: {AUTOMATIC_RESTART_EVERY_X_HOURS} hours")

        # Automatic Backups
        if 0 < BACKUP_EVERY_X_HOURS <= calculate_hours_elapsed(self.server_times.last_backup) and self.desired_server_state == State.ON:
            logger.info("Taking server backup...")
            await self.pal.take_server_backup()
            await self.server_times.update_last_backup()
            logger.info(f"Next backup in: {BACKUP_EVERY_X_HOURS} hours")

        # Automatic restart
        if AUTOMATIC_RESTART and self.desired_server_state == State.ON:
            hours_since_last_restart = calculate_hours_elapsed(self.server_times.last_restart)
            if hours_since_last_restart >= AUTOMATIC_RESTART_EVERY_X_HOURS:
                await self.pal.log_and_broadcast(f"Restarting server after {hours_since_last_restart} hours...")
                if not check_for_process(self.pal.palworld_server_proc_name):
                    logger.info(f"Server process not found, restarting...")
                    await self.pal.launch_server()
                    await self.server_times.update_last_restart()
                    logger.info(f"Next server restart in: {AUTOMATIC_RESTART_EVERY_X_HOURS} hours")
                else:
                    await self.pal.restart_server(backup_server=BACKUP_ON_RESTART)
                    await self.server_times.update_last_restart()
                    await self.pal.log_and_broadcast(f"Next server restart in: {AUTOMATIC_RESTART_EVERY_X_HOURS} hours")

        # Stop the server if the server is idle (And it has been 8 hours). The function already checks if the
        # Server is on or not before doing anything
        time_now = time.time()
        if self.last_idle_check - time_now > EIGHT_HOURS_IN_SECONDS:
            await self.auto_shutdown_server_if_idle()
            self.last_idle_check = time.time()

    def get_json(self):
        with open(PALWORLD_JSON_WITH_PATH, "r") as f:
            return json.load(f)

    async def auto_shutdown_server_if_idle(self) -> None:
        """Automatically shuts down the server if the server is idle. """
        logging.info("Checking if Palworld server is idle to stop it")

        if not await self.is_server_on():
            logging.debug("Palworld Watcher State is OFF. Skipping Idle Check")
            return

        if not await self.is_server_empty():
            logging.info("Palworld server is not empty. Finishing auto shutdown check")
            return

        await self.stop_server()
        logging.info("Finished Palworld server stop")

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
        logging.info(f"Current Palworld Server State: {self.desired_server_state}")

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
        if self.get_server_state() == State.OFF:
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

    @palworld.command(name="save")
    @is_menace_guild()
    async def palworld_save(self, ctx: commands.Context):
        """Saves the current state of the server. """
        response = await self.save()
        if not response:
            await ctx.send("Server is not on, cannot save.")
            return
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    async def is_server_empty(self) -> bool:
        """Returns True if server is empty else False. """
        response = await self.pal.rcon.send_command("ShowPlayers", [])
        lines = response.split("\n")
        # If there are more than 1 lines, that means that there are players in the server
        return False if len(lines) > 1 else True

    async def show_players(self) -> str:
        """Returns the output of the ShowPlayers RCON command. """
        if not await self.is_server_on():
            return "The Server is off."

        response = "```\n"
        response += await self.pal.rcon.send_command("ShowPlayers", [])
        response += "\n```"
        return response

    async def is_server_on(self) -> bool:
        """Returns True if server is still on and off if the server is off. """
        await self.get_server_state()
        logging.debug(f"Server state: {self.server_state}")
        return self.server_state == State.ON

    async def start_server(self) -> None:
        """Attempts to start the server if it is not already on. """
        if await self.is_server_on():
            logging.debug("The state was already ON when attempting to start it")
            return

        await self.pal.launch_server()
        self.desired_server_state = State.ON
        await self.server_times.update_last_restart()
        logging.info("Palworld Server Started")

    async def stop_server(self) -> None:
        """Stops the server. """
        self.desired_server_state = State.OFF

        if not await self.is_server_on():
            logging.debug("The state was already OFF when attempting to stop it")
            return

        # Shut down the Palworld Server
        wait_time = 60
        shutdown_warning_msg = f"SERVER SHUTDOWN INCOMING. Waiting {wait_time} seconds before starting shutdown process."
        await self.pal.log_and_broadcast(shutdown_warning_msg)
        await asyncio.sleep(wait_time)
        await self.pal.log_and_broadcast("Starting server shutdown process.")
        await self.pal.save_server_state()
        await asyncio.sleep(1)
        await self.pal.rcon.send_command("Shutdown")
        await asyncio.sleep(60)
        await self.get_server_state()  # Refresh the server state
        logging.info("Palworld Server Stopped")

    async def save(self) -> bool:
        """"Saves the server state. """
        if not await self.is_server_on():
            return False
        return await self.pal.save_server_state()


async def setup(bot) -> None:
    await bot.add_cog(PalWorld(bot))
