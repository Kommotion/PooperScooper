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
NO_YEAR = 1945
BIRTHDAY = "birthday"
BIRTHDAY_ALREADY_ANNOUNCED = "birthday_announced"
SERVER_REQUESTED = "server_requested"
GENERAL = "general"


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
            self.birthday_data[user_id][BIRTHDAY] = date.isoformat()
            self.birthday_data[user_id][BIRTHDAY_ALREADY_ANNOUNCED] = False
            self.birthday_data[user_id][SERVER_REQUESTED] = server
            log.debug(f"Replaced user's data in birthday list")
            status = REPLACED
        except KeyError:
            log.debug(f"User doesn't have a current birthday, adding to dictionary")
            self.birthday_data[user_id] = dict()
            self.birthday_data[user_id][BIRTHDAY] = date.isoformat()
            self.birthday_data[user_id][BIRTHDAY_ALREADY_ANNOUNCED] = False
            self.birthday_data[user_id][SERVER_REQUESTED] = server
            status = ADDED

        self.dump_json()
        return status

    def delete_birthday_data(self, user_id: discord.User.id) -> None:
        log.debug(f"Deleting birthday data for {user_id}")
        try:
            del self.birthday_data[str(user_id)]
            self.dump_json()
        except KeyError:
            pass


class BirthdayTracker(Cog):
    """Commands and loop for tracking birthdays. """

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.birthdays = BirthdayData()
        self.check_for_birthdays.start()

    @tasks.loop(hours=8)
    async def check_for_birthdays(self):
        """Checks if there's any birthdays and sends a message if there is. """
        log.info("Checking for birthdays")
        self.birthdays.load_json()
        today = datetime.date.today()

        for user in self.birthdays.birthday_data:
            birthday = datetime.date.fromisoformat(self.birthdays.birthday_data[user][BIRTHDAY])

            if birthday.day == today.day and birthday.month == today.month:
                if self.birthdays.birthday_data[user][BIRTHDAY_ALREADY_ANNOUNCED]:
                    log.debug(f"It's {user}'s birthday but we've already said happy birthday")
                    continue

                # Find the proper guild
                requested_guild_id = int(self.birthdays.birthday_data[user][SERVER_REQUESTED])
                bday_guild = discord.utils.get(self.bot.guilds, id=requested_guild_id)
                if not bday_guild:
                    log.warning(f"We've somehow found a guild ID that is not in the bot's list: {requested_guild_id}")
                    continue

                log.debug(f"Found guild: {bday_guild.name}, {bday_guild.id}")

                # Find the proper channel from the guild
                bday_channel = discord.utils.get(bday_guild.text_channels, name=GENERAL)
                if not bday_channel:
                    bday_channel = bday_guild.system_channel

                log.debug(f"Found channel: {bday_channel.name}, {bday_channel.id}")

                # Find the person that we're mentioning
                bday_user = discord.utils.get(bday_guild.members, id=int(user))
                if not bday_user:
                    log.warning(f"We've somehow found a guild member that's not in the targeted guild")
                    continue

                # Find out if the person has given their year as a birthday
                bday_message = f"EVERYBODY WISH {bday_user.mention} A HAPPY BIRTHDAY!"
                if birthday.year != NO_YEAR:
                    years_old = today.year - birthday.year
                    bday_message += f"\nWelcome to being {years_old} years old 😊"

                # Sending out the happy birthday
                embed = discord.Embed(title=f"HAPPY BIRTHDAY {bday_user.name} 🎉🎂".upper(), description=bday_message,
                                      colour=discord.Colour.blue())
                await bday_channel.send(embed=embed)
                self.birthdays.birthday_data[user][BIRTHDAY_ALREADY_ANNOUNCED] = True

            # Birthday is not today, let's make sure we reset birthday announcement
            else:
                self.birthdays.birthday_data[user][BIRTHDAY_ALREADY_ANNOUNCED] = False

        self.birthdays.dump_json()
        log.info("Finished checking for birthdays")

    @check_for_birthdays.before_loop
    async def before_birthday(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="birthday-add")
    async def birthday_add(self, interaction: discord.Interaction, month: int, day: int,
                           year: Optional[int] = NO_YEAR) -> None:
        """Adds your birthday to the birthday tracker. If it's your birthday, the server will be reminded.

        If your birthday has already been added, it'll be replaced. Cannot do this in multiple servers!
        """
        birthday = datetime.date(year=year, month=month, day=day)
        if birthday.year != NO_YEAR:
            birthday_string = f"{birthday.month}-{birthday.day}-{birthday.year}"
        else:
            birthday_string = f"{birthday.month}-{birthday.day}"
        status = self.birthdays.add_birthday_data(interaction.user.id, birthday, interaction.guild.id)

        if status == ADDED:
            response = f"Your birthday has been added to the tracker as {birthday_string}"
        elif status == REPLACED:
            response = f"Your previous birthday has been replaced in the tracker as {birthday_string}"
        else:
            response = "Honestly, I have no clue how you got to this point of the flow, wtf did you do?"

        await interaction.response.send_message(response, ephemeral=True)

    @app_commands.command(name="birthday-delete")
    async def birthday_delete(self, interaction: discord.Interaction) -> None:
        """Deletes your birthday from the birthday tracker if it exists."""
        self.birthdays.delete_birthday_data(interaction.user.id)
        await interaction.response.send_message("Your birthday has been removed from the tracker", ephemeral=True)


async def setup(bot):
    await bot.add_cog(BirthdayTracker(bot))
