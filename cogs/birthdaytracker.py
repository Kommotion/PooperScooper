from __future__ import annotations

import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.constants import BIRTHDAY_JSON

NO_YEAR = 1945
from cogs.utils.json_store import LockedJsonFile
from cogs.utils.server_config import get_guild_config

log = logging.getLogger(__name__)

FAILED = "failed"
REPLACED = "replaced"
ADDED = "added"
BIRTHDAY = "birthday"
BIRTHDAY_ALREADY_ANNOUNCED = "birthday_announced"
SERVER_REQUESTED = "server_requested"


class BirthdayData:
    def __init__(self):
        self._store = LockedJsonFile(BIRTHDAY_JSON, default=dict, indent=2)
        self.birthday_data = self._store.read()

    def load_json(self) -> None:
        self.birthday_data = self._store.read()

    def dump_json(self) -> None:
        self._store.write(self.birthday_data)

    def add_birthday_data(self, user_id: int, date: datetime.date, server: int) -> str:
        user_key = str(user_id)
        try:
            self.birthday_data[user_key][BIRTHDAY] = date.isoformat()
            self.birthday_data[user_key][BIRTHDAY_ALREADY_ANNOUNCED] = False
            self.birthday_data[user_key][SERVER_REQUESTED] = server
            status = REPLACED
        except KeyError:
            self.birthday_data[user_key] = {
                BIRTHDAY: date.isoformat(),
                BIRTHDAY_ALREADY_ANNOUNCED: False,
                SERVER_REQUESTED: server,
            }
            status = ADDED

        self.dump_json()
        return status

    def delete_birthday_data(self, user_id: int) -> None:
        try:
            self.load_json()
            del self.birthday_data[str(user_id)]
            self.dump_json()
        except KeyError:
            pass

    def list_for_guild(self, guild_id: int) -> list[tuple[int, datetime.date, bool]]:
        entries: list[tuple[int, datetime.date, bool]] = []
        for user_key, record in self.birthday_data.items():
            if int(record.get(SERVER_REQUESTED, 0)) != guild_id:
                continue
            birthday = datetime.date.fromisoformat(record[BIRTHDAY])
            announced = bool(record.get(BIRTHDAY_ALREADY_ANNOUNCED, False))
            entries.append((int(user_key), birthday, announced))
        entries.sort(key=lambda item: (item[1].month, item[1].day))
        return entries


class BirthdayTracker(Cog):
    """Opt-in birthday tracking and announcements."""

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.birthdays = BirthdayData()
        self.check_for_birthdays.start()

    birthday_group = app_commands.Group(name="birthday", description="Track your birthday.")

    @tasks.loop(hours=1)
    async def check_for_birthdays(self) -> None:
        self.birthdays.load_json()
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        for guild in self.bot.guilds:
            config = get_guild_config(guild.id)
            if config is None or not config.enabled:
                continue

            local_now = now_utc.astimezone(config.get_birthday_zone())
            if local_now.hour != 0:
                continue

            await self._announce_guild_birthdays(guild, local_now.date())

        self.birthdays.dump_json()
        log.info("Finished checking for birthdays")

    async def _announce_guild_birthdays(self, guild: discord.Guild, today: datetime.date) -> None:
        config = get_guild_config(guild.id)
        if config is None:
            return

        bday_channel = config.resolve_birthday_channel(guild)
        if bday_channel is None:
            log.warning("No birthday announce channel configured for guild %s", guild.id)
            return

        for user_key, record in self.birthdays.birthday_data.items():
            if int(record.get(SERVER_REQUESTED, 0)) != guild.id:
                continue

            birthday = datetime.date.fromisoformat(record[BIRTHDAY])
            if birthday.day != today.day or birthday.month != today.month:
                record[BIRTHDAY_ALREADY_ANNOUNCED] = False
                continue

            if record.get(BIRTHDAY_ALREADY_ANNOUNCED):
                continue

            bday_user = guild.get_member(int(user_key))
            if bday_user is None:
                log.warning("Birthday user %s not found in guild %s", user_key, guild.id)
                continue

            bday_message = f"EVERYBODY WISH {bday_user.mention} A HAPPY BIRTHDAY!"
            if birthday.year != NO_YEAR:
                years_old = today.year - birthday.year
                bday_message += f"\nWelcome to being {years_old} years old 😊"

            embed = discord.Embed(
                title=f"HAPPY BIRTHDAY {bday_user.name} 🎉🎂".upper(),
                description=bday_message,
                colour=discord.Colour.blue(),
            )
            await bday_channel.send(embed=embed)
            record[BIRTHDAY_ALREADY_ANNOUNCED] = True

    @check_for_birthdays.before_loop
    async def before_birthday(self) -> None:
        await self.bot.wait_until_ready()

    @birthday_group.command(name="add")
    async def birthday_add(
        self,
        interaction: discord.Interaction,
        month: int,
        day: int,
        year: Optional[int] = NO_YEAR,
    ) -> None:
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
            response = "Something went wrong adding your birthday."

        await interaction.response.send_message(response, ephemeral=True)

    @birthday_group.command(name="delete")
    async def birthday_delete(self, interaction: discord.Interaction) -> None:
        self.birthdays.delete_birthday_data(interaction.user.id)
        await interaction.response.send_message("Your birthday has been removed from the tracker", ephemeral=True)

    @birthday_group.command(name="list", description="List registered birthdays for this server.")
    @app_commands.default_permissions(manage_guild=True)
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server to view the birthday list.", ephemeral=True)
            return

        entries = self.birthdays.list_for_guild(interaction.guild_id)
        if not entries:
            await interaction.response.send_message("No birthdays registered for this server.", ephemeral=True)
            return

        lines = []
        for user_id, birthday, _announced in entries:
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            if birthday.year != NO_YEAR:
                date_text = birthday.strftime("%B %d, %Y")
            else:
                date_text = birthday.strftime("%B %d")
            lines.append(f"**{name}** — {date_text}")

        embed = discord.Embed(
            title="Registered Birthdays 🎂",
            description="\n".join(lines[:25]),
            colour=discord.Colour.blurple(),
        )
        if len(lines) > 25:
            embed.set_footer(text=f"Showing 25 of {len(lines)} entries")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BirthdayTracker(bot))