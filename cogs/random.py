import random
import discord
from discord.ext import commands
from discord import app_commands


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
            options=options
        )
        self.members = members
        self.chosen_ids = []

    async def callback(self, interaction: discord.Interaction):
        self.chosen_ids = self.values
        await interaction.response.defer()  # acknowledge silently


class ImpostorView(discord.ui.View):
    def __init__(self, members, author: discord.User, timeout=60):
        super().__init__(timeout=timeout)
        self.select = ImpostorSelect(members)
        self.add_item(self.select)  # dropdown first
        self.result = None
        self.cancelled = False
        self.author = author

        # Continue button
        self.continue_button = discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.success
        )
        self.continue_button.callback = self.continue_button_callback
        self.add_item(self.continue_button)

        # Cancel button
        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger
        )
        self.cancel_button.callback = self.cancel_button_callback
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Restrict to the command invoker
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Only the command invoker can use this menu.",
                ephemeral=True
            )
            return False
        return True

    async def continue_button_callback(self, interaction: discord.Interaction):
        if not self.select.chosen_ids:
            self.result = self.select.members
        else:
            self.result = [
                m for m in self.select.members if str(m.id) in self.select.chosen_ids
            ]

        await interaction.response.defer(ephemeral=True)

        # Disable all buttons in the view
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
            if isinstance(child, discord.ui.Select):
                child.disabled = True
        await interaction.edit_original_response(view=self)

        self.stop()  # let the main command continue

    async def cancel_button_callback(self, interaction: discord.Interaction):
        self.cancelled = True
        await interaction.response.defer(ephemeral=True)

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
            if isinstance(child, discord.ui.Select):
                child.disabled = True
        await interaction.edit_original_response(view=self)

        self.stop()

    async def on_timeout(self):
        if not self.select.chosen_ids:
            self.result = self.select.members
        else:
            self.result = [
                m for m in self.select.members if str(m.id) in self.select.chosen_ids
            ]


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

    @app_commands.command(name="impostor", description="Randomly selects an impostor from your current voice call.")
    async def impostor(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description="You must be in a voice channel to use this command!",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        channel = interaction.user.voice.channel
        members = channel.members

        if not members:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Error",
                    description="No members found in your call!",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        view = ImpostorView(members, interaction.user)
        await interaction.response.send_message(
            "🎭 Select the participants for the impostor game (or click Continue for everyone):",
            view=view,
            ephemeral=True
        )

        await view.wait()

        if view.cancelled:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🚫 Cancelled",
                    description="Impostor selection was cancelled.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        participants = view.result or members

        impostor = random.choice(participants)

        try:
            voice_channel_mention = f"<#{channel.id}>"  # Clickable link to the voice channel
            text_channel_mention = f"<#{interaction.channel.id}>"  # Clickable link to the text channel

            embed = discord.Embed(
                title="🎭 You are the Impostor!",
                description=(
                    f"Shh... 🤫\n\n"
                    f"**Game requested by:** {interaction.user.mention}\n"
                    f"**Voice Channel:** {voice_channel_mention}\n"
                    f"**Text Channel:** {text_channel_mention}\n\n"
                    "Your mission: blend in and avoid being caught!\n"
                    "Only you know who the impostor is..."
                ),
                color=discord.Color.dark_red()
            )
            embed.set_footer(text="Good luck! 🕵️‍♂️")

            await impostor.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Warning",
                    description=f"Couldn’t DM {impostor.display_name}. They might have DMs disabled.",
                    color=discord.Color.orange()
                ),
                ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Impostor Chosen",
                description=f"An impostor has been chosen from {voice_channel_mention}! (Only they know who)",
                color=discord.Color.green()
            ),
        )


class RandomCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(RandomGroup())


async def setup(bot: commands.Bot):
    await bot.add_cog(RandomCog(bot))
