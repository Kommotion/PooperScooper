import discord
import json
import logging
import time
from cogs.utils.constants import *
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from pprint import pprint


log = logging.getLogger(__name__)

# Frequencies in seconds
UPDATE_FREQUENCY = 15
SAVE_FREQUENCY = 1807


class GameData:
    """A class containing the data of the players currently playing. """
    def __init__(self):
        self.game_data = None

    def load_json(self):
        with open(GAMETIME_JSON, 'r') as f:
            self.game_data = json.load(f)

    def dump_json(self):
        with open(GAMETIME_JSON, 'w') as f:
            json.dump(self.game_data, f)

    def increment_time_played(self, member_id, game, seconds=UPDATE_FREQUENCY):
        if self.game_data is None:
            log.critical("Tried incrementing game data before it was loaded from JSON!")
            return

        # Data loaded from JSON is a string
        member_id = str(member_id)

        # Try to increment the gametime and build the dictionary tree if game is missing
        # Kinda gross looking but this is EAFP
        try:
            self.game_data[member_id][game] = int(self.game_data[member_id][game]) + seconds
        except KeyError:
            log.debug("{} is a brand new game not played before by {}".format(game, member_id))
            try:
                self.game_data[member_id][game] = seconds
            except KeyError:
                log.debug("{} is {}'s first game played on record".format(game, member_id))
                self.game_data[member_id] = dict()
                self.game_data[member_id][game] = seconds

    def print_game_data(self):
        pprint(self.game_data)


class Gametime(Cog):
    """Commands for Gametime feature that tracks games played by each member. """

    def __init__(self, bot):
        self.bot = bot
        self.game_data = GameData()
        self.game_data.load_json()
        self.last_save_time = time.time()
        self.last_update_time = time.time()
        self.update_gametime.start()

    @tasks.loop(seconds=UPDATE_FREQUENCY)
    async def update_gametime(self):
        """Updates the gametime for each member on the server. """
        if time.time() - self.last_update_time < UPDATE_FREQUENCY - 1:
            log.debug("It hasn't been {} seconds since the last update, skipping this update".format(UPDATE_FREQUENCY))
            return

        already_updated = set()

        for guild in self.bot.guilds:
            for user in guild.members:
                if user.bot is True or user.id in already_updated:
                    continue

                for activity in user.activities:
                    if activity.type == discord.ActivityType.playing:
                        self.game_data.increment_time_played(user.id, activity.name)
                        already_updated.add(user.id)

        self.last_update_time = time.time()
        log.debug("Finished updating gametime data")
        self.save_game_data()

    @update_gametime.before_loop
    async def before_gametime(self):
        await self.bot.wait_until_ready()

    @update_gametime.after_loop
    async def save_gametime(self):
        """Saves the current gametime data to storage. """
        self.save_game_data(force=True)

    def save_game_data(self, force=False):
        time_now = time.time()
        if time_now - self.last_save_time >= SAVE_FREQUENCY or force:
            log.debug("Saving gamedata to Storage")
            self.game_data.dump_json()
            self.last_save_time = time_now

    @commands.command()
    async def played(self, ctx):
        """Prints the time played for every game Pooper has detected. """
        async with ctx.typing():
            member_id = str(ctx.author.id)
            try:
                member_data = self.game_data.game_data[member_id]
            except KeyError:
                await ctx.send("I scooped a lot but couldn't find any of your data!")
                return

            embed = discord.Embed(
                title='Time Played 🎮',
                colour=discord.Colour.blue()
            )

            for game in member_data:
                gametime = self.convert_seconds_to_string(int(member_data[game]))
                embed.add_field(name=game, value=gametime, inline=False)

            await ctx.send(embed=embed)

    def convert_seconds_to_string(self, seconds):
        hours, remainder = divmod(seconds, SECONDS_IN_HOUR)
        minutes, seconds = divmod(remainder, MINUTES_IN_HOUR)
        days, hours = divmod(hours, HOURS_IN_DAY)
        if days:
            fmt = '{d} days, {h} hours, {m} minutes, and {s} seconds'
        else:
            fmt = '{h} hours, {m} minutes, and {s} seconds'

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @commands.command()
    async def save(self, ctx):
        """Force saves the gametime data to storage. This is automatic. """
        self.update_gametime.cancel()
        self.update_gametime.restart()
        await ctx.message.add_reaction('👍')


def setup(bot):
    bot.add_cog(Gametime(bot))
