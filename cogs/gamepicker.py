from __future__ import annotations
import random
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from cogs.utils.utils import create_json
from discord import app_commands
import logging
import os
import json
from pprint import pprint
from difflib import SequenceMatcher

log = logging.getLogger(__name__)
GAME_PICKER_JSON = 'game_picker.json'
SIMILARITY_THRESHOLD = 0.85


class GamePickerData:
    """Utility class for accessing Choice data."""
    def __init__(self):
        self.game_picker_data = None
        if not os.path.isfile(GAME_PICKER_JSON):
            create_json(GAME_PICKER_JSON)
        self.load_json()

    def load_json(self) -> None:
        try:
            with open(GAME_PICKER_JSON, "r") as f:
                self.game_picker_data = json.load(f)
        except json.JSONDecodeError:
            log.error(f"{GAME_PICKER_JSON} corrupted. Recreating.")
            create_json(GAME_PICKER_JSON)
            self.game_picker_data = {}

    def dump_json(self) -> None:
        with open(GAME_PICKER_JSON, "w") as f:
            json.dump(self.game_picker_data, f, indent=4)

    async def get_gamepicker_list(self, guild_id: discord.Guild.id) -> list:
        return self.game_picker_data.get(str(guild_id), [])

    async def _prompt_already_exists(self, gamepicker_data_lowercase: list, new_prompt: str) -> (bool, str):
        """Checks if the prompt already exists.
        :param gamepicker_data_lowercase: The gamepicker data in lowercase
        :param new_prompt: The prompt that is going to be added
        :return: True if already exists else False, Prompt that matches if True else None
        """
        for existing_prompt in gamepicker_data_lowercase:
            similarity_ratio = SequenceMatcher(None, existing_prompt, new_prompt).ratio()
            if similarity_ratio >= SIMILARITY_THRESHOLD:
                return True, existing_prompt
        return False, None

    async def add_gamepicker_data(self, guild_id: discord.Guild.id, prompt: str) -> (bool, str|None):
        guild_id = str(guild_id)
        prompt = prompt.strip()
        lowercase_prompt = prompt.lower()
        reason = ''

        try:
            gamepicker_data_lowercase = [string.lower() for string in self.game_picker_data[guild_id]]
            # If prompt already exists, don't add it
            already_exists, matching_prompt = await self._prompt_already_exists(gamepicker_data_lowercase, lowercase_prompt)
            if already_exists:
                reason = f"A matching prompt already exists:\n{matching_prompt}"
                return False, reason
            self.game_picker_data[guild_id].append(prompt)
        except KeyError:
            # This is the first time that a prompt has been added to the guild
            # Therefore we don't need to check for duplicates
            new_gamepicker = {
                guild_id: [prompt]
            }
            self.game_picker_data.update(new_gamepicker)
        except Exception as e:
            log.exception(f"Unable to add game to the list because: {e}")
            reason = "An unknown error occurred."
            return False, reason

        self.dump_json()
        log.debug("gamepicker added")
        return True, reason

    async def remove_gamepicker_data(self, guild_id: discord.Guild.id, prompt: str) -> bool:
        guild_id = str(guild_id)
        prompt = prompt.strip()
        lowercase_prompt = prompt.lower()
        log.debug(f"Deleting gamepicker data for prompt: {prompt}")

        try:
            gamepicker_data_lowercase = [string.lower() for string in self.game_picker_data[guild_id]]
            prompt_index = gamepicker_data_lowercase.index(lowercase_prompt)
            del self.game_picker_data[guild_id][prompt_index]
        except KeyError:
            log.warning(f"guild_id is not currently in gamepicker data: {guild_id}")
            return False
        except ValueError:
            log.warning(f"The prompt was not found in the gamepicker list:\n {prompt}")
            return False

        self.dump_json()
        log.debug("gamepicker data succesfully removed")
        return True


class GamePicker(Cog):
    """Game Picker commands."""

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.game_picker_data = GamePickerData()

    gamepicker_group = app_commands.Group(name="gamepicker", description="Manage your game list.")

    async def game_remove_autocomplete(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete existing games for removal."""
        game_list = await self.game_picker_data.get_gamepicker_list(interaction.guild_id)
        if not game_list:
            return []

        # Filter games by what user typed (`current`)
        choices = [
            app_commands.Choice(name=game, value=game)
            for game in game_list
            if current.lower() in game.lower()
        ]

        # Discord allows max 25 choices for autocomplete
        return choices[:25]

    @gamepicker_group.command(name="add", description="Add a game to the picker list.")
    @app_commands.describe(prompt="Add game to the game picker list.")
    async def add_gamepicker(self, interaction: discord.Interaction, prompt: str) -> None:
        log.info(f"Adding game from {interaction.user.name} from {interaction.guild.name}")
        result, reason = await self.game_picker_data.add_gamepicker_data(interaction.guild_id, prompt)
        if result:
            title = 'Added your game to the gamepicker list'
            message = prompt
        else:
            title = 'ERROR'
            message = f'Unable to add your prompt to the list because:\n{reason}'

        embed = discord.Embed(
            title=title,
            description=message,
            colour=discord.Colour.blue()
        )

        await interaction.response.send_message(embed=embed)

    @gamepicker_group.command(name="list", description="Show all games to pick from.")
    async def gamepicker_list(self, interaction: discord.Interaction) -> None:
        gamepicker_list = await self.game_picker_data.get_gamepicker_list(interaction.guild_id)
        if not gamepicker_list:
            await interaction.response.send_message(f'**No Games options exist for your server.**')
            return

        log.debug(f'Prompts from {interaction.guild_id}: {gamepicker_list}')
        MAX_CHARS = 4096
        embed_title = 'List of games to choose from:'
        embed_color = discord.Color.blue()
        embeds = []
        current_description = ""

        sorted_gamepicker_list = sorted(gamepicker_list, key=str.lower)

        for prompt in sorted_gamepicker_list:
            new_description = f'{current_description}{prompt}\n'

            if len(new_description) > MAX_CHARS:
                embed = discord.Embed(title=embed_title, description=current_description, color=embed_color)
                embeds.append(embed)
                current_description = f'{prompt}\n'
            else:
                current_description = new_description

        # create a new embed with the remaining description (if any)
        if current_description:
            embed = discord.Embed(title=embed_title, description=current_description, color=embed_color)
            embeds.append(embed)

        # reply to the interaction with the first embed
        await interaction.response.send_message(embed=embeds[0])

        # send the remaining embeds as followups
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed)

    @gamepicker_group.command(name="remove", description="Remove a game from the picker list.")
    @app_commands.describe(prompt="Choose a game to remove")
    async def game_picker_remove(
            self,
            interaction: discord.Interaction,
            prompt: str
    ) -> None:
        log.debug(f"Removing game from {interaction.user.name} from {interaction.guild.name}")
        result = await self.game_picker_data.remove_gamepicker_data(interaction.guild_id, prompt)
        if result:
            title = "Removed your game from the game list"
            message = prompt
        else:
            title = "Unable to remove your game"
            message = "Use my /game-list command to see if it even exists."

        embed = discord.Embed(
            title=title,
            description=message,
            colour=discord.Colour.blue()
        )

        await interaction.response.send_message(embed=embed)

    @game_picker_remove.autocomplete("prompt")
    async def game_remove_autocomplete_wrapper(
            self,
            interaction: discord.Interaction,
            current: str
    ) -> list[app_commands.Choice[str]]:
        return await self.game_remove_autocomplete(interaction, current)

    @gamepicker_group.command(name="roll", description="Pick a random game!")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def game_picker_roll(self, interaction: discord.Interaction) -> None:
        full_gamepicker_list = await self.game_picker_data.get_gamepicker_list(interaction.guild_id)

        if not full_gamepicker_list:
            await interaction.response.send_message(
                '**No game options exist for your server.**',
                ephemeral=True
            )
            return

        # Choose a random game
        chosen_game = random.choice(full_gamepicker_list)

        # Create embed
        embed = discord.Embed(
            title="🎲 Game Picker",
            description=f"Your randomly chosen game is:\n\n**{chosen_game}**",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Total games in list: {len(full_gamepicker_list)}")

        # Send embed
        await interaction.response.send_message(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(GamePicker(bot))
