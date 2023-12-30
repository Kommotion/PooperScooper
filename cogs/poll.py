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
import re
from pprint import pprint

log = logging.getLogger(__name__)

# New
REQUESTER_ID = "requester_id"
DATE_CREATED = "date_created"
GUILD_ID = "guild_id"
CHANNEL_ID = "channel_id"
DEFAULT_EMOJIS = ['🇦', '🇧', '🇨', '🇩', '🇪', '🇫', '🇬', '🇭', '🇮', '🇯', '🇰', '🇱', '🇲', '🇳',
                  '🇴', '🇵', '🇶', '🇷', '🇸', '🇹', '🇺', '🇻', '🇼', '🇽', '🇾', '🇿']
TEMP_MESSAGE_ID = 'TEMP_MESSAGE'
OPEN_POLL = "📋 Open: Poll Question: "
CLOSED_POLL = f"🔒 Closed: Poll Results for: "


class PollData:
    """Utility class for accessing poll data."""
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

    async def add_poll_data(self, interaction: discord.Interaction) -> None:
        original_response = await interaction.original_response()
        original_response_id = original_response.id
        log.debug(f"Adding poll from {interaction.user.name} from {interaction.guild.name} in channel "
                  f"{interaction.channel.name} with message id {original_response_id}")
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        message_id = str(original_response_id)
        date = str(int(time.time()))

        new_poll = dict()
        new_poll[message_id] = dict()
        new_poll[message_id][CHANNEL_ID] = channel_id
        new_poll[message_id][GUILD_ID] = guild_id
        new_poll[message_id][DATE_CREATED] = date
        new_poll[message_id][REQUESTER_ID] = user_id
        self.poll_data.update(new_poll)
        self.dump_json()
        log.debug("Poll added")

    async def remove_poll_data(self, guild_id: discord.Guild.id, channel_id: discord.Message.id,
                               message_id: discord.Message.id) -> None:
        log.debug(f"Deleting poll data for message id: {message_id}")
        try:
            self.load_json()
            del self.poll_data[message_id]
            self.dump_json()
        except KeyError:
            log.warning("We tried to delete poll data that didn't exist. How is that possible?")
            log.warning(f"Poll data to be deleted: message id {message_id} guild {guild_id}"
                        f"channel {channel_id}")
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

        for message in self.active_polls.poll_data:
            poll_created_date = int(self.active_polls.poll_data[message][DATE_CREATED])
            if current_date - poll_created_date > SECONDS_IN_DAY:
                log.debug(f"Poll {message} has been open for more than 24 hours, closing poll")
                user_id = self.active_polls.poll_data[message][REQUESTER_ID]
                guild = self.active_polls.poll_data[message][GUILD_ID]
                channel = self.active_polls.poll_data[message][CHANNEL_ID]
                await self._close_poll(guild, channel, message, user_id)

        log.info("Finished auto closing polls")

    @auto_close_poll.before_loop
    async def before_poll_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _calculate_reaction_results(self, reactions: discord.Message.reactions) -> list:
        def calculate_percentage_votes(votes):
            total_votes = sum(votes)
            if total_votes == 0:
                return [0] * len(votes)  # Avoid division by zero
            percent = [int((vote / total_votes) * 100) for vote in votes]
            return percent

        # Replace these with the actual vote counts for each choice
        votes_for_choices = list()
        for reaction in reactions:
            votes_for_choices.append(reaction.count)

        percentages = calculate_percentage_votes(votes_for_choices)
        return percentages

    async def close_poll(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Closes the given poll. """
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        message_id = str(message.id)

        try:
            if user_id != self.active_polls.poll_data[message_id][REQUESTER_ID]:
                msg = "**ERROR: Only the creator of the poll can close polls!**"
                await interaction.response.send_message(msg, ephemeral=True)
                return
        except KeyError:
            msg = "**ERROR: The message you are trying to close is not a poll!**"
            await interaction.response.send_message(msg, ephemeral=True)
            return

        await self._close_poll(guild_id, channel_id, message_id, user_id, message=message)
        await interaction.response.send_message("**The poll has been closed!**", ephemeral=True)

    async def _close_poll(self, guild_id: discord.Guild.id, channel_id: discord.Message.id,
                          message_id: discord.Message.id, user_id: discord.User.id,
                          message: discord.Message = None) -> None:
        """Updates the poll message with results and removes from tracked polls. """
        # If no interaction, this was probably from an auto-close, so get the message
        # TODO
        if not message:
            pass

        await self.active_polls.remove_poll_data(guild_id, channel_id, message_id)

        # Calculate the reaction results
        reaction_results = await self._calculate_reaction_results(message.reactions)

        # Number of votes minus the default votes from the bot
        number_of_votes = 0
        for reaction in message.reactions:
            number_of_votes += (reaction.count - 1)
        footer = f"Number of votes: {number_of_votes}"

        # Complex message vs simple message handling
        if message.embeds:
            embed = message.embeds[0]
            new_title = embed.title.replace(f"{OPEN_POLL}", f"{CLOSED_POLL}")
            new_description = ""
            split_og_description = embed.description.split("\n")
            for iterator in range(len(message.reactions)):
                new_description += f"{reaction_results[iterator]}% | {split_og_description[iterator]}\n"

            # Calculate winner of complex message
            max_value = max(reaction_results)
            max_index = reaction_results.index(max_value)
            if reaction_results.count(max_value) == 1:
                clean_up = split_og_description[max_index].split(" ", 1)[1]
                winner = f"The winner is: {clean_up}"
            else:
                winner = "It's a tie!"
        else:
            # Simple message
            new_title = message.content.replace(f"{OPEN_POLL}", f"{CLOSED_POLL}")
            new_description = f"{reaction_results[0]}% | 👍\n{reaction_results[1]}% | 👎"

            # Calculate winner of the simple message
            if reaction_results[0] > reaction_results[1]:
                winner = "The winner is: 👍"
            elif reaction_results[0] < reaction_results[1]:
                winner = "The winner is: 👎"
            else:
                winner = "It's a tie!"

        footer += f"\n{winner}"
        embed = discord.Embed(title=new_title, description=new_description, colour=discord.Colour.blue())
        embed.set_footer(text=footer)
        await message.edit(embed=embed)

        try:
            await message.clear_reactions()
        except:
            log.debug("Errored out while trying to clear the reactions")
            pass  # Tried to clear the reactions, but could not


    @app_commands.command(name="poll")
    async def poll(self, interaction: discord.Interaction, question: str, choice_1: Optional[str] = None,
                   choice_2: Optional[str] = None, choice_3: Optional[str] = None, choice_4: Optional[str] = None,
                   choice_5: Optional[str] = None, choice_6: Optional[str] = None, choice_7: Optional[str] = None,
                   choice_8: Optional[str] = None, choice_9: Optional[str] = None, choice_10: Optional[str] = None,
                   choice_11: Optional[str] = None, choice_12: Optional[str] = None, choice_13: Optional[str] = None
                   ) -> None:
        """Create a new poll with either yes/no or multiple choices.

        Parameters
        -----------
        question: str
            Type in your poll's question. Example: Will Saint meet Angel last?
        choice_1: Optional[str]
            Choice for the poll (Optional). Example: Yes
        choice_2: Optional[str]
            Choice for the poll (Optional). Example: No
        choice_3: Optional[str]
            Choice for the poll (Optional). Example: Probably
        choice_4: Optional[str]
            Choice for the poll (Optional).
        choice_5: Optional[str]
            Choice for the poll (Optional).
        choice_6: Optional[str]
            Choice for the poll (Optional).
        choice_7: Optional[str]
            Choice for the poll (Optional).
        choice_8: Optional[str]
            Choice for the poll (Optional).
        choice_9: Optional[str]
            Choice for the poll (Optional).
        choice_10: Optional[str]
            Choice for the poll (Optional).
        choice_11: Optional[str]
            Choice for the poll (Optional).
        choice_12: Optional[str]
            Choice for the poll (Optional).
        choice_13: Optional[str]
            Choice for the poll (Optional).
        """
        choices = [choice_1, choice_2, choice_3, choice_4, choice_5, choice_6, choice_7, choice_8, choice_9, choice_10,
                   choice_11, choice_12, choice_13]
        valid_choices = [choice for choice in choices if choice is not None]

        if not valid_choices:
            # Simple poll with one question
            await self.simple_poll(interaction, question)
        else:
            # No longer a simple Poll with just yes/no question
            result = await self.complex_poll(interaction, question, valid_choices)
            if not result:
                return

        await self.active_polls.add_poll_data(interaction)

    async def simple_poll(self, interaction: discord.Interaction, question: str) -> None:
        """Yes or no simple poll. """
        # Simple Poll with just one question
        question = f"{OPEN_POLL}**{question}**"
        await interaction.response.send_message(content=question)
        message = await interaction.original_response()
        await message.add_reaction("👍")
        await message.add_reaction("👎")

    async def get_regular_emoji(self, message: str):
        """Returns the regular emoji the message contains it. """
        for char in message:
            if 0x1F300 <= ord(char) <= 0x1F6FF or 0x1F700 <= ord(char) <= 0x1F77F or 0x1F780 <= ord(char) <= 0x1F7FF:
                return char
        return None

    async def get_custom_emoji(self, message: str):
        """Returns the custom emoji if the message contains it. """
        custom_emoji_pattern = re.compile(r'<a?:\w+:\d+>')
        custom_emojis = custom_emoji_pattern.findall(message)
        return custom_emojis[0] if custom_emojis else None

    async def complex_poll(self, interaction: discord.Interaction, question: str, valid_choices: list) -> bool:
        """Poll with multiple choices.

        If an emoji is in front of the choice, then we will use that emoji as an option
        """
        emojis_to_add = list()
        question = f"{OPEN_POLL}**{question}**"
        description = ""
        iterator = 0  # For Default emojis

        for choice in valid_choices:
            # If the message has an emoji, use the emoji as the vote option
            # Otherwise, use default letter
            regular_emoji = await self.get_regular_emoji(choice)
            if regular_emoji:
                modified_choice = choice.replace(regular_emoji, "")
                description += f"{regular_emoji}  {modified_choice}\n"
                emojis_to_add.append(regular_emoji)
                continue

            # Custom emoji
            custom_emoji = await self.get_custom_emoji(choice)
            if custom_emoji:
                modified_choice = choice.replace(custom_emoji, "")
                description += f"{custom_emoji}  {modified_choice}\n"
                emojis_to_add.append(custom_emoji)
                continue

            # no emoji
            emojis_to_add.append(DEFAULT_EMOJIS[iterator])
            description += f"{DEFAULT_EMOJIS[iterator]}  {choice}\n"

            iterator += 1

        embed = discord.Embed(title=question, description=description, colour=discord.Colour.blue())
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        for emoji in emojis_to_add:
            try:
                await message.add_reaction(emoji)
            except:
                await message.edit(content="**ERROR Creating your poll! Perhaps you used a custom emoji that I cannot "
                                           "process**", embed=None)
                return False

        return True


async def setup(bot) -> None:
    await bot.add_cog(Poll(bot))
