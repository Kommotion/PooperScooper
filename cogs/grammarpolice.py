import discord
import json
import language_tool_python
import logging
from discord.ext import commands
from discord.ext.commands import Cog
from typing import Union


log = logging.getLogger(__name__)
GRAMMARPOLICE_JSON = 'grammarpolice.json'
GRAMMAR_POLICE = 'Grammar Police 🚨'
YOUR = 'your'
YOURE = 'you\'re'
YOURRE = 'youre'
YOUR_LIST = [YOUR, YOURE, YOURRE]


class GrammarErrors:
    def __init__(self):
        self.grammar_errors = None
        self.load_json()

    def load_json(self):
        with open(GRAMMARPOLICE_JSON, 'r') as f:
            self.grammar_errors = json.load(f)

    def dump_json(self):
        with open(GRAMMARPOLICE_JSON, 'w') as f:
            json.dump(self.grammar_errors, f)

    def increment_error(self, user_id, amount=1):
        try:
            self.grammar_errors[user_id] = int(self.grammar_errors[user_id]) + amount
        except KeyError:
            self.grammar_errors[user_id] = 1
        finally:
            self.dump_json()

    def get_error_count(self, user_id):
        try:
            return self.grammar_errors[user_id]
        except KeyError:
            return None

    def add_member(self, user_id):
        self.grammar_errors[user_id] = 0
        self.dump_json()

    def remove_member(self, user_id):
        try:
            del self.grammar_errors[user_id]
            self.dump_json()
            return True
        except KeyError:
            return False


class GrammarPolice(Cog):
    """Commands for monitoring grammar mistakes. """

    def __init__(self, bot):
        self.bot = bot
        self.spell_checker = language_tool_python.LanguageToolPublicAPI('en-US')
        self.grammar = GrammarErrors()

    @commands.Cog.listener('on_message')
    async def emron_checker(self, message):
        if str(message.author.id) in self.grammar.grammar_errors:
            content = message.content.lower()
            if any(your in content for your in YOUR_LIST):
                matches = self.spell_checker.check(content)
                log.debug('Spell Checker Matches: {}'.format(matches))
                for match in matches:
                    if any(your in match.replacements for your in YOUR_LIST):
                        self.grammar.increment_error(str(message.author.id))
                        await self.send_error_message(match, message)

    async def send_error_message(self, match, message):
        await message.add_reaction('❌')
        embed = discord.Embed(
            title=GRAMMAR_POLICE,
            description=match.message,
            colour=discord.Colour.blue()
        )
        await message.reply(embed=embed)

    @commands.command()
    async def errors(self, ctx, *, user: Union[discord.Member, discord.User] = None):
        """Prints the number of grammar errors. Can be another user."""
        async with ctx.typing():
            member = user if user else ctx.message.author
            member_id = str(member.id)
            errors = self.grammar.get_error_count(member_id)

            if errors is None:
                description = '{} has not opted into error checking!'.format(member.mention)
            elif errors == 0:
                description = '{} has not made any errors!'.format(member.mention)
            else:
                description = '{} Your vs You\'re Error Count: {}'.format(member.mention, errors)

            embed = discord.Embed(
                title=GRAMMAR_POLICE,
                description=description,
                colour=discord.Colour.blue()
            )
            await ctx.send(embed=embed)

    @commands.command()
    async def opt_in(self, ctx):
        """Opts in of Grammar Police monitoring. """
        async with ctx.typing():
            member_id = str(ctx.message.author.id)
            if member_id in self.grammar.grammar_errors:
                await ctx.send('You are already opted in!')
                return

            self.grammar.add_member(member_id)
            await ctx.message.add_reaction('👍')

    @commands.command()
    async def opt_out(self, ctx):
        """Opts out. WARNING: Deletes all of your grammar mistake data! """
        async with ctx.typing():
            member_id = str(ctx.message.author.id)
            self.grammar.remove_member(member_id)
            await ctx.message.add_reaction('👍')


async def setup(bot):
    await bot.add_cog(GrammarPolice(bot))
