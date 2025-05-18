import asyncio
import typing
import functools
from ftplib import error_perm
from io import BytesIO
import gc
import time
import os
import typing
from typing import Literal, Optional
import discord
import torch
import logging
from enum import Enum, StrEnum
from diffusers import EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from discord import app_commands, ui


from cogs.utils.constants import MENACES_TO_SOBRIETY_SERVER_ID, POOPER_SCOOPER_SUPPORT_SERVER_ID

log = logging.getLogger(__name__)
CUDA = "cuda"


ANI_PONY = r'.\models\ani-pony\waiANINSFWPONYXL_v140.safetensors'
WAI_ILLUSTRIOUS = r".\models\wai_illustrious\waiNSFWIllustrious_v140.safetensors"
PONY_REALISM = r'.\models\pony-realism\ponyRealism_V23.safetensors'

ANI_PONY_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, source_anime"
ANI_PONY_NEGATIVE_PROMPT = "worst quality, bad quality, jpeg artifacts, source_cartoon, \
3d, (censor), monochrome, blurry, lowres, watermark,"

WAI_ILLUSTRIOUS_POSITIVE_PROMPT = "masterpiece,best quality,amazing quality"
WAI_ILLUSTRIOUS_NEGATIVE_PROMPT = "bad quality,worst quality,worst detail,sketch,censor,"

PONY_REALISM_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, BREAK"
PONY_REALISM_NEGATIVE_PROMPT = "score_4, score_5, score_6"

NSFW_IMAGE_DIFFUSION_CHANNEL = 1373140173067653120
IMAGE_DIFFUSION_CHANNEL = 1365847564196249620
TEST_CHANNEL = 1045149015756521493
ALlOWED_CHANNELS = [NSFW_IMAGE_DIFFUSION_CHANNEL, TEST_CHANNEL, IMAGE_DIFFUSION_CHANNEL]

ANIME_DESCRIPTION = 'Anime (WAI-NSFW-illustrious-SDXL v14)'
ANIPONY_DESCRIPTION = 'Anipony (WAI-ANI-PONYXL v14.0.)'
PONY_DESCRIPTION = 'Pony (Pony Realism v23)'


class Models(StrEnum):
    ANIME = "anime"
    ANIPONY = "anipony"
    PONY = "pony"

    @classmethod
    def _missing_(cls, value):
        # Check against name (case-insensitive)
        for member in cls:
            if member.name.lower() == value.lower():
                return member
        raise ValueError(f"{value} is not a valid {cls.__name__}")

class PromptType(StrEnum):
    POSITIVE = 'positive'
    NEGATIVE = 'negative'

    @classmethod
    def _missing_(cls, value):
        # Check against name (case-insensitive)
        for member in cls:
            if member.name.lower() == value.lower():
                return member
        raise ValueError(f"{value} is not a valid {cls.__name__}")

class DefaultChoice(StrEnum):
    YES = 'yes'
    NO = 'No'

class NsfwLevel(StrEnum):
    GENERAL = 'general'
    SENSITIVE = 'sensitive'
    EXPLICIT = 'explicit'
    NSFW = 'nsfw'
    NOT_SPECIFIED = ''

YES = 'yes'
NO = 'No'


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


def in_allowed_channels():
    def predicate(ctx: commands.Context) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        return ctx.channel.id in ALlOWED_CHANNELS
    return commands.check(predicate)


class ImageCreation:
    def __init__(self, prompt: Models, model, ctx=None, interaction=None, negative_prompt='', use_default_negative=True,
                 use_default_positive=True, nsfw: NsfwLevel=NsfwLevel.NOT_SPECIFIED):
        self.ctx: commands.Context = ctx
        self.interaction: discord.Interaction = interaction
        self.negative_prompt = negative_prompt
        self.use_default_negative = use_default_negative
        self.use_default_positive = use_default_positive
        self.prompt: str = prompt
        self.model: Models = model
        self.nsfw: NsfwLevel = nsfw


class ImageDiffusion(Cog):
    """Optimized Commands for image diffusion."""
    def __init__(self, bot):
        self.bot = bot
        self.image_queue = asyncio.Queue(maxsize=5)
        self.next_image = asyncio.Event()
        self.pipelines = {}
        self.models_to_load = [
            WAI_ILLUSTRIOUS,
            PONY_REALISM,
            ANI_PONY
        ]

        self.model_gen_method = {
            Models.ANIME: self.generate_image_wai_illustrious,
            Models.PONY: self.generate_image_pony,
            Models.ANIPONY: self.generate_image_ani_pony
        }

        self.bot.loop.create_task(self.load_pipelines())
        self.image_generation.start()

    async def load_pipelines(self):
        """Preload all models into memory."""
        log.info("Loading all pipelines...")
        for model in self.models_to_load:
            self.pipelines[model] = await self.load_pipeline(model)
        log.info("All pipelines loaded successfully.")

    async def load_single_pipeline(self, model: str):
        log.info(f"Loading specific model: {model}")
        self.pipelines[model] = await self.load_pipeline(model)
        log.info("Model loaded successfully.")

    async def load_pipeline(self, model: str):
        """Helper to load one model."""
        if model == PONY_REALISM:
                pipe = StableDiffusionXLPipeline.from_single_file(
                    PONY_REALISM,
                    torch_dtype=torch.float16,
                    use_safetensors=True,
                    load_safety_checker=None,
                )
                pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
                pipe.enable_xformers_memory_efficient_attention()
                pipe.enable_model_cpu_offload()

        elif model == ANI_PONY:
                pipe = StableDiffusionXLPipeline.from_single_file(
                    ANI_PONY,
                    torch_dtype=torch.float16,
                    use_safetensors=True,
                    load_safety_checker=None,
                )
                pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
                pipe.enable_xformers_memory_efficient_attention()
                pipe.enable_model_cpu_offload()

        elif model == WAI_ILLUSTRIOUS:
                pipe = StableDiffusionXLPipeline.from_single_file(
                    WAI_ILLUSTRIOUS,
                    torch_dtype=torch.float16,
                    use_safetensors=True,
                    load_safety_checker=None,
                )
                pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
                pipe.enable_xformers_memory_efficient_attention()
                pipe.enable_model_cpu_offload()

        else:
            log.error(f"Tried to load an unsupported model: {model}")
            raise ValueError(f"Unsupported model: {model}")

        return pipe


    @commands.group(invoke_without_command=True)
    @in_allowed_channels()
    async def anime(self, ctx: commands.Context, *, prompt: str) -> None:
        """Generate text to image using Anime (WAI-NSFW-illustrious-SDXL v14).

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(prompt, Models.ANIME, ctx=ctx)

    @commands.group(invoke_without_command=True)
    @in_allowed_channels()
    async def pony(self, ctx: commands.Context, *, prompt: str) -> None:
        """Generate text to image using Pony (Pony Realism v23).

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(prompt, Models.PONY, ctx=ctx)

    @commands.group(invoke_without_command=True)
    @in_allowed_channels()
    async def anipony(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using Anipony (WAI-ANI-PONYXL v14.0.).

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(prompt, Models.ANIPONY, ctx=ctx)

    @app_commands.command(name="imagegen_default_prompts", description='Print the default negative or positive prompts for the given model.')
    @app_commands.describe(
        model=f'The model you want to see the default prompt of.',
        prompt_type='Whether you want to see the default positive or negative prompt.'
    )
    @app_commands.guilds(MENACES_TO_SOBRIETY_SERVER_ID, POOPER_SCOOPER_SUPPORT_SERVER_ID)
    async def default_prompt(self, interaction: discord.Interaction,
                             model: Models,
                             prompt_type: PromptType
        ) -> None:

        if interaction.channel_id not in ALlOWED_CHANNELS:
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        default_prompt = None

        if model == Models.ANIPONY:
            if prompt_type == PromptType.POSITIVE:
                default_prompt = ANI_PONY_POSITIVE_PROMPT
            elif prompt_type == PromptType.NEGATIVE:
                default_prompt = ANI_PONY_NEGATIVE_PROMPT
        elif model == Models.PONY:
            if prompt_type == PromptType.POSITIVE:
                default_prompt = PONY_REALISM_POSITIVE_PROMPT
            elif prompt_type == PromptType.NEGATIVE:
                default_prompt = PONY_REALISM_NEGATIVE_PROMPT
        elif model == Models.ANIME:
            if prompt_type == PromptType.POSITIVE:
                default_prompt = WAI_ILLUSTRIOUS_POSITIVE_PROMPT
            elif prompt_type == PromptType.NEGATIVE:
                default_prompt = WAI_ILLUSTRIOUS_NEGATIVE_PROMPT
        else:
            await interaction.response.send_message("You did not specify a correct model.", ephemeral=True)
            return

        await interaction.response.send_message(f"The default {prompt_type.value} prompt for {model.value} is:\n{default_prompt}")

    @app_commands.command(name="imagegen_form", description='Open a form to generate an image with prompt options.')
    @app_commands.describe(
        model=f'Choose one of these models: {ANIME_DESCRIPTION}, {PONY_DESCRIPTION}, {ANIPONY_DESCRIPTION}',
        prompt='Prompt to generate the image.',
        negative_prompt="(Optional) Avoid these elements in the image.",
        add_default_negative="W(Optional) hether to include the default negative prompt (default: Yes).",
        add_default_positive="(Optional) Whether to include the default positive prompt (default: Yes).",
        nsfw_level='(Optional) General, sensitive, explicit, nsfw, or Not Specified'
    )
    @app_commands.guilds(MENACES_TO_SOBRIETY_SERVER_ID, POOPER_SCOOPER_SUPPORT_SERVER_ID)
    async def generate_modal(self, interaction: discord.Interaction,
                       model: Models,
                       prompt: str,
                       negative_prompt: Optional[str] = None,
                       add_default_negative: Optional[DefaultChoice] = DefaultChoice.YES,
                       add_default_positive: Optional[DefaultChoice] = DefaultChoice.YES,
                       nsfw_level: Optional[NsfwLevel] = NsfwLevel.NOT_SPECIFIED
                       ) -> None:
        if interaction.channel_id not in ALlOWED_CHANNELS:
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        await interaction.response.send_message('To be implemented...', ephemeral=True)

    @app_commands.command(name="imagegen", description='Generate an image using text to image model.')
    @app_commands.describe(
        model=f'Choose one of these models: {ANIME_DESCRIPTION}, {PONY_DESCRIPTION}, {ANIPONY_DESCRIPTION}',
        prompt='Prompt to generate the image.',
        negative_prompt="(Optional) Avoid these elements in the image.",
        add_default_negative="(Optional) Whether to include the default negative prompt (default: Yes).",
        add_default_positive="(Optional) Whether to include the default positive prompt (default: Yes).",
        nsfw_level='(Optional) General, sensitive, explicit, nsfw, or Not Specified (default: Not Specified).'
    )
    @app_commands.guilds(MENACES_TO_SOBRIETY_SERVER_ID, POOPER_SCOOPER_SUPPORT_SERVER_ID)
    async def generate(self, interaction: discord.Interaction,
                       model: Models,
                       prompt: str,
                       negative_prompt: Optional[str] = '',
                       add_default_negative: Optional[DefaultChoice] = DefaultChoice.YES,
                       add_default_positive: Optional[DefaultChoice] = DefaultChoice.YES,
                       nsfw_level: Optional[NsfwLevel] = NsfwLevel.NOT_SPECIFIED
                       ) -> None:
        if interaction.channel_id not in ALlOWED_CHANNELS:
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        add_default_positive = True if add_default_positive == DefaultChoice.YES else False
        add_default_negative = True if add_default_negative == DefaultChoice.YES else False

        await self.schedule_generation(prompt, model, interaction=interaction, negative_prompt=negative_prompt,
                                       use_default_negative=add_default_negative,
                                       use_default_positive=add_default_positive,
                                       nsfw=nsfw_level)

    async def schedule_generation(self, prompt: str, model: Models, ctx: commands.Context = None,
                                  interaction: discord.Interaction =None, negative_prompt='', use_default_negative=True,
                                  use_default_positive=True, nsfw: NsfwLevel=NsfwLevel.NOT_SPECIFIED) -> None:
        if self.image_queue.full():
            if ctx:
                await ctx.reply("Queue is full! Please wait and try again.")
            elif interaction:
                await interaction.response.send_message("Queue is full! Please wait and try again.")
            return

        image_entry = ImageCreation(prompt, model, ctx=ctx, interaction=interaction, negative_prompt=negative_prompt,
                              use_default_negative=use_default_negative, use_default_positive=use_default_positive,
                              nsfw=nsfw)
        await self.image_queue.put(image_entry)

        if ctx:
            await ctx.message.add_reaction("👍")
        elif interaction:
            await interaction.response.defer(thinking=True)

        log.info(f"Added prompt to queue: {prompt} for model: {model}")

    @tasks.loop(seconds=1)
    async def image_generation(self) -> None:
        if self.image_queue.empty():
            return

        log.debug("Processing next queued image...")
        self.next_image.clear()
        image: ImageCreation = await self.image_queue.get()

        # If the image is ctx based, then use image.ctx path for this, otherwise use the image.interaction path
        if image.ctx:
            await self._image_generation_context(image)
        elif image.interaction:
            await self._image_generation_interaction(image)

        self.bot.loop.call_soon_threadsafe(self.next_image.set)
        await self.next_image.wait()

    async def _image_generation_context(self, image: ImageCreation) -> None:
        """Handles image generation but with a context. This is the original path to image generation. """
        timeout = 600

        try:
            image_gen = self.model_gen_method[image.model]
        except KeyError:
            log.warning("There was somehow a model queued up that does not exist.")
            await image.ctx.reply("You somehow queued up a model that did not exist. Naughty you!")
            return

        try:
            output = await asyncio.wait_for(image_gen(image), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Image generation timed out")
            await image.ctx.reply("Image generation timed out. Try again with a simpler prompt.")
            return
        except discord.HTTPException:
            await image.ctx.send(f"Error generating prompt: {image.prompt}.")
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            torch.cuda.empty_cache()
            await image.ctx.reply(f"Image generation failed: {str(e)}")
            return

        img = output.images[0]
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        file = discord.File(buffer, filename="generated.png")

        try:
            await image.ctx.reply(file=file)
        except discord.HTTPException:
            log.warning("Hit case where was unable to do ctx.reply in image generation.")
            await image.ctx.send(file=file)
        except Exception as e:
            log.error(f"Error: {e}")
            await image.ctx.send(f"Some error occurred while image generation from {image.ctx.author.name}")
        log.debug("Image sent to Discord")

    async def _image_generation_interaction(self, image: ImageCreation) -> None:
        """Handles image generation but with an interaction."""
        timeout = 600

        try:
            image_gen = self.model_gen_method[image.model]
        except KeyError:
            log.warning("There was somehow a model queued up that does not exist.")
            await image.interaction.followup.send(content="You somehow queued up a model that did not exist. Naughty you!")
            return

        try:
            output = await asyncio.wait_for(image_gen(image), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Image generation timed out")
            await image.interaction.followup.send(content="Image generation timed out. Try again with a simpler prompt.")
            return
        except discord.HTTPException:
            await image.interaction.followup.send(content=f"Error generating prompt: {image.prompt}.")
            return
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            torch.cuda.empty_cache()
            await image.interaction.followup.send(content=f"Image generation failed: {str(e)}")
            return

        img = output.images[0]
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        file = discord.File(buffer, filename="generated.png")

        try:
            await image.interaction.followup.send(file=file)
        except discord.HTTPException as e:
            log.error(f"Hit case where was unable to do ctx.reply in image generation.: {e}")
        except Exception as e:
            log.error(f"Error: {e}")
            await image.interaction.followup.send(content=f"Some error occurred while image generation", ephemeral=True)
        log.debug("Image sent to Discord")

    @to_thread
    def generate_image_ani_pony(self, image: ImageCreation):
        """Generate an image given a model path and a prompt."""
        pipe = self.pipelines.get(ANI_PONY)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {image.model}")

        default_negative = ANI_PONY_NEGATIVE_PROMPT if image.use_default_negative else ''
        default_positive = ANI_PONY_POSITIVE_PROMPT if image.use_default_positive else ''

        positive_prompt = image.prompt
        if image.nsfw != NsfwLevel.NOT_SPECIFIED:
            positive_prompt += f", {image.nsfw}"
        if default_positive:
            positive_prompt += f", {default_positive}"

        negative_prompt = f"{image.negative_prompt}, {default_negative}" if image.negative_prompt else default_negative

        log.info(f"Starting {image.model} generation for prompt: {positive_prompt}\nnegative: {negative_prompt}")
        gen_start = time.time()
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                positive_prompt,
                num_inference_steps=25,
                guidance_scale=8,
                height=1024,
                width=1024,
                negative_prompt=negative_prompt
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {positive_prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_pony(self, image: ImageCreation):
        pipe = self.pipelines.get(PONY_REALISM)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {image.model}")

        default_negative = PONY_REALISM_NEGATIVE_PROMPT if image.use_default_negative else ''
        default_positive = PONY_REALISM_POSITIVE_PROMPT if image.use_default_positive else ''

        positive_prompt = image.prompt
        if image.nsfw != NsfwLevel.NOT_SPECIFIED:
            positive_prompt += f", {image.nsfw}"
        if default_positive:
            positive_prompt += f", {default_positive}"

        negative_prompt = f"{image.negative_prompt}, {default_negative}" if image.negative_prompt else default_negative

        log.info(f"Starting {image.model} generation for prompt: {positive_prompt}\nnegative: {negative_prompt}")
        gen_start = time.time()
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                positive_prompt,
                num_inference_steps=25,
                guidance_scale=8,
                height=1024,
                width=1024,
                negative_prompt=negative_prompt
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {positive_prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_wai_illustrious(self, image: ImageCreation):
        pipe = self.pipelines.get(WAI_ILLUSTRIOUS)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {image.model}")

        default_negative = WAI_ILLUSTRIOUS_NEGATIVE_PROMPT if image.use_default_negative else ''
        default_positive = WAI_ILLUSTRIOUS_POSITIVE_PROMPT if image.use_default_positive else ''

        positive_prompt = image.prompt
        if image.nsfw != NsfwLevel.NOT_SPECIFIED:
            positive_prompt += f", {image.nsfw}"
        if default_positive:
            positive_prompt += f", {default_positive}"

        negative_prompt = f"{image.negative_prompt}, {default_negative}" if image.negative_prompt else default_negative

        log.info(f"Starting {image.model} generation for prompt: {positive_prompt}\nnegative: {negative_prompt}")
        gen_start = time.time()
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                positive_prompt,
                num_inference_steps=20,
                guidance_scale=7,
                height=1024,
                width=1024,
                negative_prompt=negative_prompt
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {positive_prompt} in {gen_time:.2f} seconds")
        return result

    def cog_unload(self):
        self.image_generation.cancel()
        gc.collect()
        torch.cuda.empty_cache()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
