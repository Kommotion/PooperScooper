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
import time
from pprint import pprint


log = logging.getLogger(__name__)

# New
REQUESTER_ID = "requester_id"
DATE_CREATED = "date_created"
DEFAULT_EMOJIS = ['🇦', '🇧', '🇨', '🇩', '🇪', '🇫', '🇬', '🇭', '🇮', '🇯', '🇰', '🇱', '🇲', '🇳',
                  '🇴', '🇵', '🇶', '🇷', '🇸', '🇹', '🇺', '🇻', '🇼', '🇽', '🇾', '🇿']
class PollData:
    """Utility class for accessing poll data.

    Structure of Poll Data:
    {
        guild_id_1: {
            channel_id_1: {
                message_id_1: {
                    "requester_id": requester_id,
                    "date_created": date_created
                },
                message_id_2: {
                    "requester_id": requester_id,
                    "date_created": date_created
                }
            },
            channel_id_2 {
                message_id_1: {
                    "requester_id": requester_id,
                    "date_created": date_created
                },
                message_id_2: {
                    "requester_id": requester_id,
                    "date_created": date_created
                }
            }
        },
        guild_id_2: {
            channel_id_1: {
                message_id_1: {
                    "requester_id": requester_id,
                    "date_created": date_created
                },
                message_id_2: {
                    "requester_id": requester_id,
                    "date_created": date_created
                }
            },
            channel_id_2 {
                message_id_1: {
                    "requester_id": requester_id,
                    "date_created": date_created
                },
                message_id_2: {
                    "requester_id": requester_id,
                    "date_created": date_created
                }
            }
        },
    }
    """
    def __init__(self):
        self.poll_data = None
        if not os.path.isfile(POLL_JSON):
            create_json(POLL_JSON)
        self.load_json()

    def load_json(self) -> None:
        with open(POLL_JSON, "r") as f:
            self.poll_data = json.load(f)

    def dump_json(self) -> None:
        with open(POLL_JSON, "w") as f:
            json.dump(self.poll_data, f)

    def print_poll_data(self) -> None:
        pprint(self.poll_data)

    def add_poll_data(self, interaction: discord.Interaction) -> None:
        log.debug(f"Adding poll from {interaction.user.name} from {interaction.guild.name} in channel "
                  f"{interaction.channel.name} with message id {interaction.message.id}")
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        message_id = str(interaction.message.id)
        date = str(int(time.time()))

        # Create new poll to update in the old poll
        new_poll = dict()
        new_poll[guild_id] = dict()
        new_poll[guild_id][channel_id] = dict()
        new_poll[guild_id][channel_id][message_id] = dict()
        new_poll[guild_id][channel_id][message_id][REQUESTER_ID] = user_id
        new_poll[guild_id][channel_id][message_id][DATE_CREATED] = date
        self.poll_data.update(new_poll)
        self.dump_json()
        log.debug("Poll added")

    def remove_poll_data(self, interaction: discord.Interaction) -> None:
        log.debug(f"Deleting poll data for message id: {interaction.message.id}")
        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        message_id = str(interaction.message.id)
        try:
            self.load_json()
            del self.poll_data[guild_id][channel_id][message_id]
            self.dump_json()
        except KeyError:
            log.warning("We tried to delete poll data that didn't exist. How is that possible?")
            log.warning(f"Poll data to be deleted: message id {interaction.message.id} guild {interaction.guild.name}"
                        f"channel {interaction.channel.name}")
            pass
        log.debug("Poll data succesfully removed")


class Poll(Cog):
    """Poll commands. """
    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.active_polls = PollData()
        self.ctx_menu = app_commands.ContextMenu(
            name="Close Poll",
            callback=self.close_poll
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.auto_close_poll.start()

    @tasks.loop(hours=1)
    async def auto_close_poll(self) -> None:
        """Auto close poll if it has been 24 hours since creation. """
        log.info("Checking for polls to auto close")
        self.active_polls.load_json()
        current_date = int(time.time())

        for guild in self.active_polls.poll_data:
            for channel in self.active_polls.poll_data[guild]:
                for message in self.active_polls.poll_data[guild][channel]:
                    poll_created_date = int(self.active_polls.poll_data[guild][channel][message][DATE_CREATED])

                    if current_date - poll_created_date > SECONDS_IN_DAY:
                        log.debug(f"Poll {message} has been open for more than 24 hours, closing poll")
                        user_id = self.active_polls.poll_data[guild][channel][message][REQUESTER_ID]
                        await self._close_poll(guild, channel, message, user_id, from_auto_close=True)

        log.info("Finished auto closing polls")

    @auto_close_poll.before_loop
    async def before_poll_check(self) -> None:
        await self.bot.wait_until_ready()

    async def close_poll(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Closes the given poll. """
        await interaction.response.send_message("to be implemented!", ephemeral=True)

    async def _close_poll(self, guild_id: discord.Guild.id, channel: discord.Message.id, message_id: discord.Message.id,
                          user_id: discord.User.id, from_auto_close=False):
        """Updates the poll message with results and removes from tracked polls. """
        pass

    @app_commands.command(name="poll")
    async def poll(self, interaction: discord.Interaction, question: str, choice_1: Optional[str] = None,
                   choice_2: Optional[str] = None, choice_3: Optional[str] = None, choice_4: Optional[str] = None,
                   choice_5: Optional[str] = None, choice_6: Optional[str] = None, choice_7: Optional[str] = None,
                   choice_8: Optional[str] = None, choice_9: Optional[str] = None, choice_10: Optional[str] = None,
                   choice_11: Optional[str] = None, choice_12: Optional[str] = None, choice_13: Optional[str] = None
                   ) -> None:
        """Create a new poll.

            Created by a hot fucking milf with tig ol bitties and RICH AS FUCK
        """
        choices = [choice_1, choice_2, choice_3, choice_4, choice_5, choice_6, choice_7, choice_8, choice_9, choice_10,
                   choice_11, choice_12, choice_13]
        valid_choices = [choice for choice in choices if choice is not None]

        if not valid_choices:
            # Simple poll with one question
            await self.simple_poll(interaction, question)
        else:
            # No longer a simple Poll with just yes/no question
            await self.complex_poll(interaction, question, valid_choices)

        # TODO uncomment the line below
        # self.active_polls.add_poll_data(interaction)
        # TODO remove ephemeral after testing is done

    async def simple_poll(self, interaction: discord.Interaction, question: str) -> None:
        """Yes or no simple poll. """
        # Simple Poll with just one question
        # embed = discord.Embed(title=f"📋 {question}", colour=discord.Colour.blue())
        question = f"📋 **{question}**"
        # await interaction.response.send_message(embed=embed)
        await interaction.response.send_message(content=question)
        message = await interaction.original_response()
        await message.add_reaction("👍")
        await message.add_reaction("👎")

    async def complex_poll(self, interaction: discord.Interaction, question: str, valid_choices: list):
        """Poll with multiple choices.

        If an emoji is in front of the choice, then we will use that emoji as an option
        """
        emojis_to_add = list()
        question = f"📋 **{question}**"
        description = ""
        iterator = 0

        for choice in valid_choices:
            # If the message starts with an emoji, create custom emoji
            # Otherwise, use a default emoji
            if ord(choice[0]) in range(0x1F600, 0x1F64F + 1):
                emojis_to_add.append(choice[0])
                description += f"{choice[0]} {choice}\n"
            else:
                emojis_to_add.append(DEFAULT_EMOJIS[iterator])
                description += f"{DEFAULT_EMOJIS[iterator]} {choice}\n"

            iterator += 1

        embed = discord.Embed(description=description, colour=discord.Colour.blue())
        await interaction.response.send_message(content=question, embed=embed)
        message = await interaction.original_response()

        for emoji in emojis_to_add:
            await message.add_reaction(emoji)


async def setup(bot) -> None:
    await bot.add_cog(Poll(bot))
