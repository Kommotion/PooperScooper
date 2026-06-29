from __future__ import annotations

import logging
import random

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Cog

from cogs.utils.constants import GAME_PICKER_JSON
from cogs.utils.guild_prompt_list import GuildPromptList, build_list_embeds

log = logging.getLogger(__name__)


class GamePicker(Cog):
    """Pick a random game from a server-maintained list."""

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.prompt_list = GuildPromptList(GAME_PICKER_JSON)

    gamepicker_group = app_commands.Group(name="gamepicker", description="Manage your game list.")

    @gamepicker_group.command(name="add", description="Add a game to the picker list.")
    @app_commands.describe(prompt="Game to add.")
    async def add_gamepicker(self, interaction: discord.Interaction, prompt: str) -> None:
        result, reason = await self.prompt_list.add(interaction.guild_id, prompt)
        if result:
            title = "Added your game to the gamepicker list"
            message = prompt
        else:
            title = "ERROR"
            message = f"Unable to add your game because:\n{reason}"

        embed = discord.Embed(title=title, description=message, colour=discord.Colour.blue())
        await interaction.response.send_message(embed=embed)

    @gamepicker_group.command(name="list", description="Show all games to pick from.")
    async def gamepicker_list(self, interaction: discord.Interaction) -> None:
        games = self.prompt_list.get_list(interaction.guild_id)
        if not games:
            await interaction.response.send_message("**No game options exist for your server.**")
            return

        embeds = build_list_embeds("List of games to choose from:", games)
        await interaction.response.send_message(embed=embeds[0])
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed)

    @gamepicker_group.command(name="remove", description="Remove a game from the picker list.")
    @app_commands.describe(prompt="Choose a game to remove.")
    async def game_picker_remove(self, interaction: discord.Interaction, prompt: str) -> None:
        result = await self.prompt_list.remove(interaction.guild_id, prompt)
        if result:
            title = "Removed your game from the game list"
            message = prompt
        else:
            title = "Unable to remove your game"
            message = "Use `/gamepicker list` to see what exists."

        embed = discord.Embed(title=title, description=message, colour=discord.Colour.blue())
        await interaction.response.send_message(embed=embed)

    @game_picker_remove.autocomplete("prompt")
    async def game_remove_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self.prompt_list.autocomplete_choices(interaction.guild_id, current)

    @gamepicker_group.command(name="roll", description="Pick a random game!")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def game_picker_roll(self, interaction: discord.Interaction) -> None:
        games = self.prompt_list.get_list(interaction.guild_id)
        if not games:
            await interaction.response.send_message(
                "**No game options exist for your server.**",
                ephemeral=True,
            )
            return

        chosen_game = random.choice(games)
        embed = discord.Embed(
            title="🎲 Game Picker",
            description=f"Your randomly chosen game is:\n\n**{chosen_game}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Total games in list: {len(games)}")
        await interaction.response.send_message(embed=embed)


async def setup(bot) -> None:
    cog = GamePicker(bot)
    await cog.prompt_list.load()
    await bot.add_cog(cog)