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

log = logging.getLogger(__name__)

PALWORLD_UTIL_PATH = "palworld_util_path"
SERVER_WATCHER_IDENTIFIER = "palworld"


class State(Enum):
    OFF = 0
    ON = 1


class PalWorldUtil:
    """Interface to send palworld utility commands. """
    def __init__(self, util_path: str):
        self.path = util_path
        self.rcon_file_with_path = os.path.join(util_path, "palworld_rcon", "source_rcon.py")
        self.server_watcher_file_with_path = os.path.join(util_path, "server_watcher.py")
        self.watcher_proc: subprocess.Popen = None
        self.state = self.refresh_server_state()

    @staticmethod
    def find_watcher_pid():
        """Attempts to find the server watcher PID by finding python.exe with palworld identifier in cmd line.

        For some reason, psutil no longer gets the full cmdline. Probably some Windows update. No need to find the
        pid since we already have it when we launched the server watcher. Leaving this here just in case.
        """
        for process in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if process.info['name'] == 'python.exe' and SERVER_WATCHER_IDENTIFIER in process.info['cmdline']:
                    return process.pid
            except psutil.AccessDenied as e:
                logging.error(e)
                raise e

        logging.info("No server_watcher process was found!")
        return None

    def is_server_watcher_still_active(self) -> bool:
        """Attempts to find if the server watcher PID is still active or not."""
        if self.watcher_proc is None:
            return False    # There is no server watcher process
        else:
            status = self.watcher_proc.poll()
            return True if status is None else False

    def refresh_server_state(self):
        server_status = self.is_server_watcher_still_active()
        self.state = State.ON if server_status else State.OFF
        return self.state

    async def is_server_empty(self) -> bool:
        """Returns True if server is empty else False. """
        response = await self._send_rcon_command("-cmd ShowPlayers")
        lines = response.split("\n")
        # If there are more than 1 lines, that means that there are players in the server
        return False if len(lines) > 1 else True

    async def show_players(self) -> str:
        """Returns the output of the ShowPlayers RCON command. """
        if not await self.is_server_on():
            return "Server is off"
        return await self._send_rcon_command("-cmd ShowPlayers")

    async def is_server_on(self) -> bool:
        """Returns True if server is still on and off if the server is off. """
        self.refresh_server_state()
        logging.debug(f"Server state: {self.state}")
        return self.state == State.ON

    async def start_server(self) -> None:
        """Attempts to start the server_watcher if it is not already on. """
        if await self.is_server_on():
            logging.debug("The state was already ON when attempting to start it")
            return

        # Run the server_watcher.py which in turn should start the Palworld server
        command = ["python", f"{self.server_watcher_file_with_path}", f"{SERVER_WATCHER_IDENTIFIER}"]
        logging.debug(command)
        self.watcher_proc = subprocess.Popen(command, cwd=self.path, preexec_fn=os.setsid)
        logging.debug(f"Server watcher stdout: {self.watcher_proc.stdout}")
        logging.debug(f"Server watcher stderr: {self.watcher_proc.stderr}")
        await asyncio.sleep(2)

    async def terminate_watcher(self) -> None:
        self.watcher_proc.terminate()
        self.watcher_proc.wait(timeout=5)  # Wait for it to exit
        # If it refuses to exit, force kill it
        if self.watcher_proc.poll() is None:
            self.watcher_proc.kill()  # Forceful kill (SIGKILL)

    async def stop_server(self) -> None:
        """Stops the server and kills the Server Watcher PID. """
        if not await self.is_server_on():
            logging.debug("The state was already OFF when attempting to stop it")
            return

        # Kill the server_watcher PID and update the State
        await self.terminate_watcher()
        self.state = State.OFF

        # Shut down the Palworld Server
        await self._send_rcon_command("-cmd Save")
        await asyncio.sleep(1)
        await self._send_rcon_command("-cmd Shutdown")
        await asyncio.sleep(30)

    async def save(self) -> bool:
        if not await self.is_server_on():
            return False
        await self._send_rcon_command("-cmd Save")
        return True

    async def _send_rcon_command(self, args: str) -> str:
        command_line = f"python {self.rcon_file_with_path} {args}"
        try:
            process = subprocess.run(command_line, capture_output=True, text=True)
            return process.stdout
        except subprocess.CalledProcessError as e:
            logging.error(e)
            raise e


class PalWorld(Cog):
    """PalWorld Server commands for MTS Server. """

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot

        try:
            credentials = load_credentials()
            palworld_util_path = credentials[PALWORLD_UTIL_PATH]
        except KeyError as e:
            logging.error(f"Palworld Util in config is not detected. Ensure that {PALWORLD_UTIL_PATH} is defined in "
                          f"config")
            raise e

        self.palworld = PalWorldUtil(palworld_util_path)
        self.auto_shutdown_server_if_idle.start()

    @tasks.loop(hours=8)
    async def auto_shutdown_server_if_idle(self) -> None:
        """Automatically shuts down the server if the server is idle. """
        logging.info("Checking if Palworld server is idle to stop it")

        if not await self.palworld.is_server_on():
            logging.debug("Palworld Watcher State is OFF. Skipping Idle Check")
            return

        if not await self.palworld.is_server_empty():
            logging.info("Palworld server is not empty. Finishing auto shutdown check")
            return

        await self.palworld.stop_server()
        logging.info("Finished Palworld server stop")

    @auto_shutdown_server_if_idle.before_loop
    async def before_poll_check(self) -> None:
        await self.bot.wait_until_ready()

    @commands.group(invoke_without_command=True)
    @is_menace_guild()
    async def palworld(self, ctx: commands.Context) -> None:
        """Do "!help palworld" for subcommands. """
        await ctx.send('Do "!help palworld" for subcommands.')

    @palworld.command(name="start_server")
    @is_menace_guild()
    async def palworld_start(self, ctx: commands.Context):
        """Starts the Palworld server if it is off. """
        await self.palworld.start_server()
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="stop_server")
    @is_menace_guild()
    async def palworld_stop(self, ctx: commands.Context):
        """Stops the Palworld server if it is on. """
        await self.palworld.stop_server()
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="restart")
    @is_menace_guild()
    async def palworld_restart(self, ctx: commands.Context):
        """Restarts the Palworld Server. """
        await self.palworld.stop_server()
        await asyncio.sleep(15)
        await self.palworld.start_server()
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)

    @palworld.command(name="players")
    @is_menace_guild()
    async def palworld_players(self, ctx: commands.Context):
        """Shows the output of the players connected to the server. """
        msg = await self.palworld.show_players()
        await ctx.send(msg)

    @palworld.command(name="state")
    @is_menace_guild()
    async def palworld_state(self, ctx: commands.Context):
        """Shows if the Palworld server is currently on or off. """
        state = await self.palworld.is_server_on()
        if state:
            await ctx.send("The server is currently on.")
        else:
            await ctx.send("The server is currently off.")

    @palworld.command(name="save_server")
    @is_menace_guild()
    async def palworld_save(self, ctx: commands.Context):
        """Saves the current state of the server. """
        response = await self.palworld.save()
        if not response:
            await ctx.send("Server is not on, cannot save.")
            return
        await ctx.message.add_reaction(THUMBS_UP_EMOJI)


async def setup(bot) -> None:
    await bot.add_cog(PalWorld(bot))
