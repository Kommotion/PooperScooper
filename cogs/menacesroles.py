import discord
from discord.ext import commands
from discord.ext.commands import Cog
from cogs.utils import utils
from cogs.utils.constants import *
import logging

MENACES_TO_SOBRIETY = 932057681307512922
ROLES_SELECTION = 1039778596836868208
log = logging.getLogger(__name__)


class MenacesRoles(Cog):
    """Commands and event listener for granting roles on the Menaces to Sobriety server. """

    def __init__(self, bot):
        self.bot = bot
        self.reaction_role_map = {
            '🧼': 1037114560961851553,  # CoD
            '🍻': 1040117797084213248,  # VRChat
            '🤓': 1040118067579076628,  # League of Legends
            '📺': 1040118203554205716,  # Jackbox
            '👻': 1040118299314356294,  # Phasmophobia
            '👟': 1040149691297443871,  # "Kick it" in voice chat
            '🔫': 1040149753121480725,  # Squad
            '🛒': 1026082898098540544,  # Mario Kart
            '🥳': 1040474269836128266,  # Mario Party
            '🍙': 1042655669955866664,  # Pokemon
            'he': 1043434842999750706,
            'she': 1043434950222938172,
            'they': 1043435084331614279,
            'nb': 1043435221313388554,
            '❓': 1044064871148429312
        }

    def _ensure_guild_and_channel(self, guild_id, channel_id) -> bool:
        if guild_id != MENACES_TO_SOBRIETY or channel_id != ROLES_SELECTION:
            return False
        return True

    @commands.Cog.listener('on_raw_reaction_add')
    async def add_role(self, payload) -> None:
        if not self._ensure_guild_and_channel(payload.guild_id, payload.channel_id):
            return

        try:
            target_role_id = self.reaction_role_map[payload.emoji.name]
        except KeyError:
            return

        target_role = payload.member.guild.get_role(target_role_id)
        await payload.member.add_roles(target_role, reason=f'User reacted with {payload.emoji.name}')

    @commands.Cog.listener('on_raw_reaction_remove')
    async def remove_role(self, payload) -> None:
        if not self._ensure_guild_and_channel(payload.guild_id, payload.channel_id):
            return

        try:
            target_role_id = self.reaction_role_map[payload.emoji.name]
        except KeyError:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        target_role = member.guild.get_role(target_role_id)
        await member.remove_roles(target_role, reason=f'User reacted with {payload.emoji.name}')


async def setup(bot):
    await bot.add_cog(MenacesRoles(bot))
