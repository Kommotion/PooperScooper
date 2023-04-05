import discord
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from cogs.utils.utils import create_json
from cogs.utils import utils
from discord import app_commands
from cogs.utils.constants import *
from typing import Optional
import logging
import datetime
import os
import json
from pprint import pprint


log = logging.getLogger(__name__)
MIDNIGHT = datetime.time()
FAILED = "failed"
REPLACED = "replaced"
ADDED = "added"
DONT_CARE = 1945
BIRTHDAY = "birthday"
ALREADY_SAID_HAPPY_BIRTHDAY = "birthday_announced"
SERVER_REQUESTED = "server_requested"


class BirthdayData:
    """A class containing the birthday data of MTS members. """
    def __init__(self):
        self.birthday_data = None
        if not os.path.isfile(BIRTHDAY_JSON):
            create_json(BIRTHDAY_JSON)
        self.load_json()

    def load_json(self) -> None:
        with open(BIRTHDAY_JSON, "r") as f:
            self.birthday_data = json.load(f)

    def dump_json(self) -> None:
        with open(BIRTHDAY_JSON, "w") as f:
            json.dump(self.birthday_data, f)

    def print_game_data(self) -> None:
        pprint(self.birthday_data)

    def add_birthday_data(self, user_id: discord.User.id, date: datetime.date, server: discord.Guild.id) -> str:
        log.debug(f"Adding date: {date} from user id:{user_id} in server id: {server}")

        try:
            self.birthday_data[user_id][BIRTHDAY] = date
            self.birthday_data[user_id][ALREADY_SAID_HAPPY_BIRTHDAY] = False
            self.birthday_data[user_id][SERVER_REQUESTED] = server
            log.debug(f"Replaced user's data in birthday list")
            return REPLACED
        except KeyError:
            log.debug(f"User doesn't have a current birthday, adding to dictionary")
            self.birthday_data[user_id] = dict()
            self.birthday_data[user_id][BIRTHDAY] = date
            self.birthday_data[user_id][ALREADY_SAID_HAPPY_BIRTHDAY] = False
            self.birthday_data[user_id][SERVER_REQUESTED] = server
            return ADDED

    def delete_birthday_data(self, user_id: discord.User.id) -> None:
        log.debug(f"Deleting birthday data for {user_id}")
        try:
            del self.birthday_data[user_id]
            self.dump_json()
        except KeyError:
            pass


class BirthdayTracker(Cog):
    """Commands and loop for tracking birthdays. """

    def __init__(self, bot):
        self.bot = bot
        self.birthdays = BirthdayData()
        self.check_for_birthdays.start()

    @tasks.loop(time=MIDNIGHT)
    async def check_for_birthdays(self):
        """Checks if there's any birthdays and sends a message if there is. """
        self.birthdays.load_json()

        # TODO
        # Check for if any birthdays are today, if yes, check if a birthday message has already been sent
        # If a message has already been sent, then don't send another one
        # Finally, if it's not the birthday, then ensure that message sent has been reset to False for next year

    @check_for_birthdays.before_loop
    async def before_gametime(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="birthday-add")
    async def birthday(self, interaction: discord.Interaction, month: int, day: int,
                       year: Optional[int] = DONT_CARE) -> None:
        """Adds your birthday to the birthday tracker. If it's your birthday, the server will be reminded.

        If your birthday has already been added, it'll be replaced.
        """
        birthday = datetime.date(year=year, month=month, day=day)
        status = self.birthdays.add_birthday_data(interaction.user.id, birthday, interaction.guild.id)

        if status == ADDED:
            response = "Added your birthday"
        elif status == REPLACED:
            response = "Replaced your birthday"
        else:
            response = "Honestly, I have no clue how you got to this point of the flow, wtf did you do?"

        await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="birthday-delete")
    async def birthday(self, interaction: discord.Interaction) -> None:
        """Deletes your birthday from the birthday tracker if it exists."""
        self.birthdays.delete_birthday_data(interaction.user.id)
        await interaction.response.send_message("Deleted your birthday", ephemeral=True)


async def setup(bot):
    await bot.add_cog(BirthdayTracker(bot))
