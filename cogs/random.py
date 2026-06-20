from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs.gamepicker import GAME_PICKER_JSON
from cogs.utils.guild_prompt_list import GuildPromptList

DICE_PATTERN = re.compile(r"^(\d{1,2})[dD](\d{1,4})$")


class ImpostorSelect(discord.ui.Select):
    def __init__(self, members):
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members
        ]
        super().__init__(
            placeholder="Select participants (or leave empty for everyone)",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.members = members
        self.chosen_ids: list[str] = []

    async def callback(self, interaction: discord.Interaction):
        self.chosen_ids = self.values
        await interaction.response.defer()


class ImpostorView(discord.ui.View):
    def __init__(self, members, author: discord.User, timeout=60):
        super().__init__(timeout=timeout)
        self.select = ImpostorSelect(members)
        self.add_item(self.select)
        self.result = None
        self.cancelled = False
        self.author = author

        self.continue_button = discord.ui.Button(label="Continue", style=discord.ButtonStyle.success)
        self.continue_button.callback = self.continue_button_callback
        self.add_item(self.continue_button)

        self.cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        self.cancel_button.callback = self.cancel_button_callback
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the command invoker can use this menu.",
                ephemeral=True,
            )
            return False
        return True

    async def continue_button_callback(self, interaction: discord.Interaction):
        if not self.select.chosen_ids:
            self.result = self.select.members
        else:
            self.result = [m for m in self.select.members if str(m.id) in self.select.chosen_ids]

        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        self.stop()

    async def cancel_button_callback(self, interaction: discord.Interaction):
        self.cancelled = True
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        self.stop()

    async def on_timeout(self):
        if not self.select.chosen_ids:
            self.result = self.select.members
        else:
            self.result = [m for m in self.select.members if str(m.id) in self.select.chosen_ids]


def _roll_dice(notation: str) -> tuple[int, list[int], int, int]:
    match = DICE_PATTERN.match(notation.strip())
    if not match:
        raise ValueError("Use dice notation like `2d6` or `1d20`.")

    count = int(match.group(1))
    sides = int(match.group(2))
    if count < 1 or count > 20:
        raise ValueError("Roll between 1 and 20 dice.")
    if sides < 2 or sides > 1000:
        raise ValueError("Dice must have between 2 and 1000 sides.")

    rolls = [random.randint(1, sides) for _ in range(count)]
    return sum(rolls), rolls, count, sides


class RandomGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name="random", description="Random fun commands")
        self.bot = bot
        self._game_list = GuildPromptList(GAME_PICKER_JSON)

    @app_commands.command(name="coinflip", description="Flip a coin: heads or tails.")
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Heads", "Tails"])
        embed = discord.Embed(
            title="Coin Flip",
            description=f"You flipped: **{result}**!",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="number", description="Pick a random number in a range.")
    @app_commands.describe(minimum="Lowest value.", maximum="Highest value.")
    async def number(
        self,
        interaction: discord.Interaction,
        minimum: app_commands.Range[int, -1_000_000, 1_000_000],
        maximum: app_commands.Range[int, -1_000_000, 1_000_000],
    ):
        if minimum > maximum:
            await interaction.response.send_message(
                "Minimum must be less than or equal to maximum.",
                ephemeral=True,
            )
            return

        result = random.randint(minimum, maximum)
        embed = discord.Embed(
            title="Random Number",
            description=f"Your number: **{result}** ({minimum}–{maximum})",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="dice", description="Roll dice using notation like 2d6 or 1d20.")
    @app_commands.describe(notation="Dice notation, e.g. 2d6")
    async def dice(self, interaction: discord.Interaction, notation: str):
        try:
            total, rolls, count, sides = _roll_dice(notation)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        rolls_text = ", ".join(str(roll) for roll in rolls)
        embed = discord.Embed(
            title="Dice Roll",
            description=f"**{notation}** → **{total}**\nRolls: {rolls_text}",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{count}d{sides}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="fromlist", description="Pick a random item from a comma-separated list.")
    @app_commands.describe(items="Separate your items with commas. Example: apple,banana,carrot")
    async def fromlist(self, interaction: discord.Interaction, items: str):
        item_list = [item.strip() for item in items.split(",") if item.strip()]
        if not item_list:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error",
                    description="You need to provide at least one item!",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        choice = random.choice(item_list)
        embed = discord.Embed(
            title="Random Pick",
            description=f"From your list: **{choice}**",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="game", description="Pick a random game from this server's gamepicker list.")
    async def game(self, interaction: discord.Interaction):
        games = self._game_list.get_list(interaction.guild_id)
        if not games:
            await interaction.response.send_message(
                "No games in this server's gamepicker list. Use `/gamepicker add` first.",
                ephemeral=True,
            )
            return

        chosen = random.choice(games)
        embed = discord.Embed(
            title="Random Game",
            description=f"**{chosen}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{len(games)} games in list")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="impostor", description="Randomly selects an impostor from your current voice call.")
    async def impostor(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This command is not allowed in DMs!", ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error",
                    description="You must be in a voice channel to use this command!",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        channel = interaction.user.voice.channel
        members = channel.members
        if not members:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Error",
                    description="No members found in your call!",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        view = ImpostorView(members, interaction.user)
        await interaction.response.send_message(
            "Select participants for the impostor game (or click Continue for everyone):",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.cancelled:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Cancelled",
                    description="Impostor selection was cancelled.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        participants = view.result or members
        participants = [member for member in participants if not member.bot]
        if len(participants) < 1:
            await interaction.followup.send("No eligible participants.", ephemeral=True)
            return

        impostor = random.choice(participants)
        voice_channel_mention = f"<#{channel.id}>"

        dm_embed = discord.Embed(
            title="You are the Impostor!",
            description=(
                f"Shh... 🤫\n\n"
                f"**Game requested by:** {interaction.user.mention}\n"
                f"**Voice Channel:** {voice_channel_mention}\n"
                f"**Text Channel:** <#{interaction.channel.id}>\n\n"
                "Your mission: blend in and avoid being caught!"
            ),
            color=discord.Color.dark_red(),
        )
        dm_embed.set_footer(text="Good luck!")

        dm_sent = True
        try:
            await impostor.send(embed=dm_embed)
        except discord.Forbidden:
            dm_sent = False
            await interaction.followup.send(
                embed=discord.Embed(
                    title="DM failed — fallback",
                    description=(
                        f"Couldn't DM {impostor.mention}. **Only you** can see this:\n"
                        f"The impostor is **{impostor.display_name}**."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        if dm_sent:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Impostor Chosen",
                    description=f"An impostor has been chosen from {voice_channel_mention}! (Only they know who)",
                    color=discord.Color.green(),
                ),
            )


class RandomCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(RandomGroup(bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(RandomCog(bot))