from __future__ import annotations

import logging
import random

import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Cog

from cogs.utils.constants import BINGO_JSON
from cogs.utils.guild_prompt_list import GuildPromptList, build_list_embeds
from cogs.utils.server_config import get_guild_config

log = logging.getLogger(__name__)
BINGO_CARD_SIZE = 16
BINGO_GRID_COLS = 4


def wrap_text(text: str, max_line_length: int) -> str:
    words = text.split()
    lines: list[str] = []
    line = ""

    for word in words:
        if len(line) + len(word) + 1 <= max_line_length:
            line += f"{word} "
        else:
            lines.append(line.strip())
            line = f"{word} "

    if line.strip():
        lines.append(line.strip())
    return "\n".join(lines)


class Bingo(Cog):
    """Server bingo card commands."""

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.prompt_list = GuildPromptList(BINGO_JSON)

    bingo_group = app_commands.Group(name="bingo", description="MTS Bingo Card commands.")

    @bingo_group.command(name="add", description="Add a prompt to the bingo list.")
    @app_commands.describe(prompt="Bingo prompt to add.")
    async def add_bingo(self, interaction: discord.Interaction, prompt: str) -> None:
        result, reason = await self.prompt_list.add(interaction.guild_id, prompt)
        if result:
            title = "Added your prompt to the bingo list"
            message = prompt
        else:
            title = "ERROR"
            message = f"Unable to add your prompt because:\n{reason}"

        embed = discord.Embed(title=title, description=message, colour=discord.Colour.blue())
        await interaction.response.send_message(embed=embed)

    @bingo_group.command(name="list", description="List bingo prompts for this server.")
    async def list_bingo(self, interaction: discord.Interaction) -> None:
        prompts = self.prompt_list.get_list(interaction.guild_id)
        if not prompts:
            await interaction.response.send_message("**No bingo options exist for your server.**")
            return

        embeds = build_list_embeds("Prompts for your server's Bingo Card", prompts)
        await interaction.response.send_message(embed=embeds[0])
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed)

    @bingo_group.command(name="remove", description="Remove a prompt from the bingo list.")
    @app_commands.describe(prompt="Bingo prompt to remove.")
    async def remove_bingo(self, interaction: discord.Interaction, prompt: str) -> None:
        result = await self.prompt_list.remove(interaction.guild_id, prompt)
        if result:
            title = "Removed your prompt from the bingo list"
            message = prompt
        else:
            title = "Unable to remove your prompt"
            message = "Use `/bingo list` to see what exists."

        embed = discord.Embed(title=title, description=message, colour=discord.Colour.blue())
        await interaction.response.send_message(embed=embed)

    @remove_bingo.autocomplete("prompt")
    async def remove_bingo_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self.prompt_list.autocomplete_choices(interaction.guild_id, current)

    @bingo_group.command(name="generate", description="Generate a 4x4 bingo card.")
    async def generate_bingo_card(self, interaction: discord.Interaction) -> None:
        prompts = self.prompt_list.get_list(interaction.guild_id)
        if not prompts:
            await interaction.response.send_message(
                "**No bingo options exist for your server.**",
                ephemeral=True,
            )
            return
        if len(prompts) < BINGO_CARD_SIZE:
            await interaction.response.send_message(
                f"**Need at least {BINGO_CARD_SIZE} prompts for a 4x4 bingo card.**",
                ephemeral=True,
            )
            return

        config = get_guild_config(interaction.guild_id)
        card_title = config.bingo_card_title if config else "BINGO"
        bingo_card = self._generate_bingo_card(prompts)
        filename = self._save_bingo_card_as_image(bingo_card, title=card_title)
        await interaction.response.send_message(file=discord.File(filename), ephemeral=True)

    def _generate_bingo_card(self, prompts: list[str]) -> list[list[str]]:
        selected_items = random.sample(prompts, BINGO_CARD_SIZE)
        wrapped_items = [wrap_text(item, 20) for item in selected_items]
        return [wrapped_items[i:i + BINGO_GRID_COLS] for i in range(0, BINGO_CARD_SIZE, BINGO_GRID_COLS)]

    def _save_bingo_card_as_image(
        self,
        card: list[list[str]],
        *,
        title: str = "BINGO",
        filename: str = "bingo_card.png",
    ) -> str:
        df = pd.DataFrame(card)
        background_color = "#1e1e2f"
        title_color = "#bdb722"

        fig, ax = plt.subplots(figsize=(10, 10), facecolor=background_color)
        plt.title(
            title,
            fontsize=36,
            color=title_color,
            weight="bold",
            pad=20,
            backgroundcolor=background_color,
            alpha=0.9,
        )

        table = ax.table(cellText=df.values, cellLoc="center", loc="center", edges="closed")
        table.auto_set_font_size(False)
        table.set_fontsize(21)
        table.scale(2.1, 10)

        background_colors = ["#f0f8ff", "#e6f0fa"]
        edge_color = "#2c2f33"
        text_color = "#000000"

        for i in range(len(df)):
            for j in range(len(df.columns)):
                cell = table[i, j]
                cell.set_facecolor(background_colors[(i + j) % 2])
                cell.set_edgecolor(edge_color)
                cell.set_linewidth(2)
                cell.set_text_props(fontsize=21, color=text_color, weight="bold", ha="center", va="center")
                cell.get_text().set_path_effects([
                    path_effects.withStroke(linewidth=3, foreground="#696969", alpha=0.3)
                ])

        table.set_zorder(10)
        table_bbox = table.get_window_extent().transformed(ax.transData.inverted())
        border = Rectangle(
            (table_bbox.x0 - 0.05, table_bbox.y0 - 0.05),
            table_bbox.width + 0.1,
            table_bbox.height + 0.1,
            fill=False,
            edgecolor=title_color,
            linewidth=4,
            zorder=5,
        )
        ax.add_patch(border)
        ax.axis("off")
        ax.set_facecolor(background_color)

        plt.savefig(filename, bbox_inches="tight", pad_inches=0.2, dpi=300, facecolor=fig.get_facecolor())
        plt.close()
        return filename


async def setup(bot) -> None:
    cog = Bingo(bot)
    await cog.prompt_list.load()
    await bot.add_cog(cog)