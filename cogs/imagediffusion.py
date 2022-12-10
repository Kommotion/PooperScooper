import asyncio
from io import BytesIO

import discord
import torch
from diffusers import StableDiffusionPipeline
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from torch import autocast

WAIFU_DIFFUSION = "hakurei/waifu-diffusion"
STABLE_DIFFUSION = "CompVis/stable-diffusion-v1-4"
CUDA = 'cuda'


class ImageCreation:
    """Class that contains image creation metadata. """
    def __init__(self, ctx, prompt, allow_nsfw, model):
        self.ctx = ctx
        self.prompt = prompt
        self.allow_nsfw = allow_nsfw
        self.model = model


class ImageDiffusion(Cog):
    """Commands for image diffusion. """

    def __init__(self, bot):
        self.bot = bot
        self.image_queue = asyncio.Queue()
        self.next_image = asyncio.Event()
        self.image_generation.start()

    @commands.group(invoke_without_command=True)
    async def waifu(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queues up a image generation using Waifu trained AI. Best for WAIFUs. """
        await self.schedule_generation(ctx, prompt, WAIFU_DIFFUSION)

    @waifu.command(name="nsfw")
    async def waifu_nsfw(self, ctx: commands.Context, *, prompt: str) -> None:
        """Subcommand for Waifu command. NSFW Channel ONLY"""
        await self.schedule_generation(ctx, prompt, WAIFU_DIFFUSION, allow_nsfw=True)

    # @commands.group(invoke_without_command=True)
    # async def stable(self, ctx: commands.Context, *, prompt: str) -> None:
    #     """Queues up a regular image generation using AI. Best for generic image generation. """
    #     await self.schedule_generation(ctx, prompt, STABLE_DIFFUSION, allow_nsfw=False)
    #
    # @stable.command(name="nsfw")
    # async def stable_nsfw(self, ctx: commands.Context, *, prompt: str) -> None:
    #     """Subcommand for stable command. NSFW Channel ONLY"""
    #     await self.schedule_generation(ctx, prompt, STABLE_DIFFUSION, allow_nsfw=True)

    # @app_commands.command(name="waifu")
    # async def generate_waifu_quietly(self, interaction: discord.Interaction) -> None:
    #     await self._generate_waifu(interaction)
    #
    # @app_commands.command(name="waifu")
    # async def generate_waifu_quietly(self, interaction: discord.Interaction) -> None:
    #     await self._generate_waifu(interaction)

    async def schedule_generation(self, ctx, prompt, model, allow_nsfw=False) -> None:
        entry = ImageCreation(ctx, prompt, allow_nsfw, model)
        await self.image_queue.put(entry)
        await ctx.message.add_reaction("👍")

    @tasks.loop(seconds=5)
    async def image_generation(self) -> None:
        self.next_image.clear()
        image = await self.image_queue.get()
        ctx = image.ctx
        prompt = image.prompt
        allow_nsfw = image.allow_nsfw
        model = image.model

        if allow_nsfw and not ctx.message.channel.is_nsfw():
            return await ctx.reply("This command only works in NSFW channels!")

        pipe = StableDiffusionPipeline.from_pretrained(model, torch_dtype=torch.float16).to(CUDA)

        if allow_nsfw:
            pipe.safety_checker = lambda images, clip_input: (images, False)

        with autocast(CUDA):
            output = pipe(prompt)

            # Because we are mega confused as to why nsfw_content_detected can be a list or a bool, do some hacks
            if type(output.nsfw_content_detected) == list:
                nsfw_content_detected = output.nsfw_content_detected[0]
            else:
                nsfw_content_detected = output.nsfw_content_detected

            if nsfw_content_detected and not allow_nsfw:
                return await ctx.reply("NSFW content was detected! Please retry with a different prompt")

            image = output.images[0]

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        output_image = discord.File(buffer, filename=f"{prompt}.png")

        await ctx.reply(file=output_image)
        self.bot.loop.call_soon_threadsafe(self.next_image.set)
        await self.next_image.wait()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
