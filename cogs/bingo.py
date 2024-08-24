from __future__ import annotations

import pandas as pd
import random
import matplotlib.pyplot as plt
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from cogs.utils.utils import create_json
from discord import app_commands
import logging
import os
import json
from pprint import pprint

log = logging.getLogger(__name__)
BINGO_JSON = 'bingo.json'


def wrap_text(text, max_line_length):
    words = text.split()
    wrapped_text = ""
    line = ""

    for word in words:
        if len(line) + len(word) + 1 <= max_line_length:
            line += (word + " ")
        else:
            wrapped_text += line.strip() + "\n"
            line = word + " "

    wrapped_text += line.strip()
    return wrapped_text


class BingoData:
    """Utility class for accessing Bingo data."""
    def __init__(self):
        self.bingo_data = None
        if not os.path.isfile(BINGO_JSON):
            create_json(BINGO_JSON)
        self.load_json()

    def load_json(self) -> None:
        with open(BINGO_JSON, "r") as f:
            self.bingo_data = json.load(f)

    def dump_json(self) -> None:
        with open(BINGO_JSON, "w") as f:
            json.dump(self.bingo_data, f)

    def print_bingo_data(self) -> None:
        pprint(self.bingo_data)


    async def get_bingo_list(self, guild_id: discord.Guild.id) -> list | None:
        try:
            return self.bingo_data[str(guild_id)]
        except KeyError:
            return None

    async def add_bingo_data(self, guild_id: discord.Guild.id, prompt: str) -> bool:
        guild_id = str(guild_id)
        try:
            self.bingo_data[guild_id].append(prompt.strip())
        except KeyError:
            new_bingo = {
                guild_id: [prompt.strip()]
            }
            self.bingo_data.update(new_bingo)
        except Exception as e:
            log.debug(f"Unable to add bingo card because: {e}")
            return False

        self.dump_json()
        log.debug("bingo added")
        return True

    async def remove_bingo_data(self, guild_id: discord.Guild.id, prompt: str) -> bool:
        guild_id = str(guild_id)
        prompt = prompt.strip()
        log.debug(f"Deleting bingo data for prompt: {prompt}")

        try:
            self.bingo_data[guild_id].remove(prompt)
        except KeyError:
            log.warning(f"guild_id is not currently in bingo data: {guild_id}")
            return False
        except ValueError:
            log.warning(f"The prompt was not found in the bingo list:\n {prompt}")
            return False

        self.dump_json()
        log.debug("bingo data succesfully removed")
        return True


class Bingo(Cog):
    """Bingo commands. """

    def __init__(self, bot: commands.AutoShardedBot):
        self.bot = bot
        self.bingo_data = BingoData()

    @app_commands.command(name="bingo-add")
    async def add_bingo(self, interaction: discord.Interaction, prompt: str) -> None:
        """Add to the list of bingo cards.

        Parameters
        -----------
        prompt: str
            The Bingo prompt to add to the list of bingo prompts. Example: Angel in a Queen Avi
        """
        log.debug(f"Adding bingo from {interaction.user.name} from {interaction.guild.name}")
        result = await self.bingo_data.add_bingo_data(interaction.guild_id, prompt)
        if result:
            message = f'Added your prompt to the bingo list: \n{prompt}'
        else:
            message = 'ERROR: Unable to add your prompt to the list for some unknown reason'
        await interaction.response.send_message(f'**{message}**')

    @app_commands.command(name="bingo-list")
    async def list_bingo(self, interaction: discord.Interaction) -> None:
        """Returns the list of the bingo cards for the server. """
        bingo_list = await self.bingo_data.get_bingo_list(interaction.guild_id)
        if not bingo_list:
            await interaction.response.send_message(f'**No Bingo options exist for your server.**')
            return

        log.debug(f'Prompts from {interaction.guild_id}: {bingo_list}')

        prompts = "Here are the prompts for your Bingo Card:\n"
        for prompt in bingo_list:
            prompts += f'{prompt}\n'

        await interaction.response.send_message(prompts, ephemeral=True)

    @app_commands.command(name="bingo-remove")
    async def remove_bingo(self, interaction: discord.Interaction, prompt: str) -> None:
        """Returns the list of the bingo cards for the server.

        Parameters
        -----------
        prompt: str
            The Bingo prompt to remove from the list of bingo prompts. Example: Angel rapping
        """
        log.debug(f"Removing bingo from {interaction.user.name} from {interaction.guild.name}")
        result = await self.bingo_data.remove_bingo_data(interaction.guild_id, prompt)
        if result:
            message = f"**Removed your prompt from the bingo list: \n{prompt}**"
        else:
            message = "**Unable to remove your prompt. Use my /list_bingo command to see if it even exists.**"
        await interaction.response.send_message(f"**{message}**")

    @app_commands.command(name="bingo-generate")
    async def generate_bingo_card(self, interaction: discord.Interaction) -> None:
        """Generates a 5x5 Bingo card. """
        full_bingo_list = await self.bingo_data.get_bingo_list(interaction.guild_id)
        if not full_bingo_list:
            await interaction.response.send_message(f'**No Bingo options exist for your server.**', ephemeral=True)
            return
        elif len(full_bingo_list) < 25:
            await interaction.response.send_message("**There are not at least 25 options for a 5x5 Bingo card.**",
                                                    ephemeral=True)
            return

        # Generate the Bingo card from the full bingo list
        bingo_card = await self._generate_bingo_card(full_bingo_list)

        # Print the Bingo card as a table
        bingo_card_name = await self.save_bingo_card_as_image(bingo_card)
        await interaction.response.send_message(file=discord.File(bingo_card_name), ephemeral=True)

    async def _generate_bingo_card(self, full_bingo_list: list) -> list:
        # Ensure we have enough items to fill the Bingo card (24 spaces)


        # Randomly select 25 items from the list
        selected_items = random.sample(full_bingo_list, 25)

        # Wrap text by inserting newlines at spaces near the max_line_length
        wrapped_items = [wrap_text(item, 20) for item in selected_items]

        # Arrange items in a 5x5 grid
        bingo_card = [wrapped_items[i:i + 5] for i in range(0, 25, 5)]

        return bingo_card

    async def save_bingo_card_as_image(self, card, filename="bingo_card.png") -> str:
        df = pd.DataFrame(card, columns=["MTS BINGO"] * 5)

        # Set up the plot
        fig, ax = plt.subplots(figsize=(10, 10))

        # Add a table
        table = ax.table(cellText=df.values, colLabels=df.columns, cellLoc='center', loc='center', edges='closed')

        # Customize table appearance
        table.auto_set_font_size(False)
        table.set_fontsize(16)
        table.scale(2, 9)  # Adjust scale to make the table bigger and more readable

        # Use a color palette for the cells
        background_color = '#f0f8ff'  # Alice Blue
        header_color = '#ff4500'  # Orange Red
        free_space_color = '#ffd700'  # Gold
        edge_color = '#696969'  # Dim Gray
        text_color = '#000000'  # Black

        # Color cells
        for i in range(len(df)):
            for j in range(len(df.columns)):
                cell = table[i + 1, j]
                cell.set_text_props(fontsize=16, color=text_color, weight='bold')
                cell.set_facecolor(background_color)
                cell.set_edgecolor(edge_color)

        # Highlight the "Free" space
        # table[3, 2].set_facecolor(free_space_color)

        # Color header
        for j in range(len(df.columns)):
            header_cell = table[0, j]
            header_cell.set_facecolor(header_color)
            header_cell.set_text_props(color='white', weight='bold', fontsize=20)

        # Hide axes
        ax.axis('off')

        # Save the figure
        plt.savefig(filename, bbox_inches='tight', pad_inches=0.1, dpi=300)
        plt.close()
        return filename


async def setup(bot) -> None:
    await bot.add_cog(Bingo(bot))
