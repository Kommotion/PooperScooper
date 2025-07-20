import random
import discord
from discord.ext import commands
from discord import app_commands

class RandomGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="random", description="Random fun commands")

    @app_commands.command(name="coinflip", description="Flip a coin: heads or tails.")
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Heads", "Tails"])
        embed = discord.Embed(
            title="🪙 Coin Flip",
            description=f"You flipped: **{result}**!",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="fromlist", description="Pick a random item from a list.")
    @app_commands.describe(items="Separate your items with commas. Example: apple,banana,carrot")
    async def fromlist(self, interaction: discord.Interaction, items: str):
        item_list = [item.strip() for item in items.split(",") if item.strip()]
        if not item_list:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description="You need to provide at least one item!",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        choice = random.choice(item_list)
        embed = discord.Embed(
            title="🎲 Random Pick",
            description=f"From your list: **{choice}**",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed)

class RandomCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(RandomGroup())

async def setup(bot: commands.Bot):
    await bot.add_cog(RandomCog(bot))
