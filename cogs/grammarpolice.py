import discord
import json
import language_tool_python
from discord.ext import commands
from discord.ext.commands import Cog


EMRON_ID = 786822389610577921
YOUR = "your"
YOURE = "you're"
YOUR_NN = 'YOUR_NN'
YOUR_UPPERCASE = "YOUR"
GRAMMARPOLICE_JSON = 'grammarpolice.json'
GRAMMAR_POLICE = "Grammar Police 🚨"


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


class GrammarPolice(Cog):
    """Commands for monitor grammar mistakes. """

    def __init__(self, bot):
        self.bot = bot
        self.spell_checker = language_tool_python.LanguageToolPublicAPI('en-US')
        self.grammar_errors = GrammarErrors()

    @commands.Cog.listener('on_message')
    async def emron_checker(self, message):
        if message.author.id == EMRON_ID:
            content = message.content.lower()
            if YOUR in content or YOURE in content:
                matches = self.spell_checker.check(content)
                for match in matches:
                    if match.ruleId in [YOUR_NN, YOUR_UPPERCASE]:
                        self.grammar_errors.increment_error(message.author.id)
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
    async def emron_errors(self, ctx):
        """Prints out the number of grammar errors Emron has made. """
        async with ctx.typing():
            try:
                errors = self.grammar_errors.grammar_errors[EMRON_ID]
                description = "Emron's Your vs You're Error Count: {}".format(errors)
            except KeyError:
                description = "Emron hasn't made an error yet!"

            embed = discord.Embed(
                title=GRAMMAR_POLICE,
                description=description,
                colour=discord.Colour.blue()
            )
            await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(GrammarPolice(bot))
