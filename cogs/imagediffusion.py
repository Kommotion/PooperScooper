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
from cogs.utils.constants import *
from discord import app_commands, ui, NSFWLevel
from cogs.utils.image_models import Models, NsfwLevel, BaseDiffusionModel, WaiAnimePonyModel, WaiAnimeIllustriousModel, CyberRealisticPonyModel, ImageCreation
from cogs.utils.constants import MENACES_TO_SOBRIETY_SERVER_ID, POOPER_SCOOPER_SUPPORT_SERVER_ID

log = logging.getLogger(__name__)

NSFW_IMAGE_DIFFUSION_CHANNEL = 1373140173067653120
IMAGE_DIFFUSION_CHANNEL = 1365847564196249620
TEST_CHANNEL = 1045149015756521493
SHEPHERD_CHANNEL = 1061073360999698483
ALlOWED_CHANNELS = [NSFW_IMAGE_DIFFUSION_CHANNEL, TEST_CHANNEL, IMAGE_DIFFUSION_CHANNEL, SHEPHERD_CHANNEL]

ANIME_DESCRIPTION = 'Anime (WAI-NSFW-illustrious-SDXL v14)'
ANIPONY_DESCRIPTION = 'Anipony (WAI-ANI-PONYXL v14.0.)'
PONY_DESCRIPTION = 'Pony (Pony Realism v23)'


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


class ImageGenPrefView(ui.View):
    def __init__(self, cog: 'ImageDiffusion',
                 default_model: Optional[Models] = None,
                 default_pos: DefaultChoice = DefaultChoice.YES,
                 default_neg: DefaultChoice = DefaultChoice.YES,
                 nsfw_level: NsfwLevel = NsfwLevel.NOT_SPECIFIED,
                 initial_prompt: str = "",
                 initial_negative_prompt: str = ""):
        super().__init__(timeout=300)
        self.cog = cog

        self.model = default_model
        self.default_pos = default_pos
        self.default_neg = default_neg
        self.nsfw = nsfw_level
        self.initial_prompt = initial_prompt
        self.initial_negative_prompt = initial_negative_prompt

        self.model_select = ui.Select(
            placeholder="Select a model (required)",
            options=[
                discord.SelectOption(label="Anime", value=Models.ANIME, description="WAI Illustrious anime-style images",
                                     default=(default_model == Models.ANIME)),
                discord.SelectOption(label="Anipony", value=Models.ANIPONY, description="ANI Pony anime-style pony characters",
                                     default=(default_model == Models.ANIPONY)),
                discord.SelectOption(label="Pony", value=Models.PONY, description="Pony Realism realistic pony images",
                                     default=(default_model == Models.PONY)),
            ]
        )
        self.model_select.callback = self.model_callback
        self.add_item(self.model_select)

        self.default_pos_select = ui.Select(
            placeholder="Include default positive prompt?",
            options=[
                discord.SelectOption(label="Yes", value=DefaultChoice.YES,
                                     description="Include model's default positive prompt", default=(default_pos == DefaultChoice.YES)),
                discord.SelectOption(label="No", value=DefaultChoice.NO,
                                     description="Exclude model's default positive prompt", default=(default_pos == DefaultChoice.NO))
            ]
        )
        self.default_pos_select.callback = self.default_pos_callback
        self.add_item(self.default_pos_select)

        self.default_neg_select = ui.Select(
            placeholder="Include default negative prompt?",
            options=[
                discord.SelectOption(label="Yes", value=DefaultChoice.YES,
                                     description="Include model's default negative prompt", default=(default_neg == DefaultChoice.YES)),
                discord.SelectOption(label="No", value=DefaultChoice.NO,
                                     description="Exclude model's default negative prompt", default=(default_neg == DefaultChoice.NO))
            ]
        )
        self.default_neg_select.callback = self.default_neg_callback
        self.add_item(self.default_neg_select)

        self.nsfw_select = ui.Select(
            placeholder="Select NSFW level",
            options=[
                discord.SelectOption(label="General", value=NsfwLevel.GENERAL, default=(nsfw_level == NsfwLevel.GENERAL)),
                discord.SelectOption(label="Sensitive", value=NsfwLevel.SENSITIVE, default=(nsfw_level == NsfwLevel.SENSITIVE)),
                discord.SelectOption(label="NSFW", value=NsfwLevel.NSFW, default=(nsfw_level == NsfwLevel.NSFW)),
                discord.SelectOption(label="Explicit", value=NsfwLevel.EXPLICIT, default=(nsfw_level == NsfwLevel.EXPLICIT)),
                discord.SelectOption(label="Not Specified", value=NsfwLevel.NOT_SPECIFIED, default=(nsfw_level == NsfwLevel.NOT_SPECIFIED))
            ]
        )
        self.nsfw_select.callback = self.nsfw_callback
        self.add_item(self.nsfw_select)

    async def model_callback(self, interaction: discord.Interaction):
        self.model = self.model_select.values[0]
        await interaction.response.defer()

    async def default_pos_callback(self, interaction: discord.Interaction):
        self.default_pos = self.default_pos_select.values[0]
        await interaction.response.defer()

    async def default_neg_callback(self, interaction: discord.Interaction):
        self.default_neg = self.default_neg_select.values[0]
        await interaction.response.defer()

    async def nsfw_callback(self, interaction: discord.Interaction):
        self.nsfw = self.nsfw_select.values[0]
        await interaction.response.defer()

    @ui.button(label="Continue", style=discord.ButtonStyle.green)
    async def continue_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.model:
            await interaction.response.send_message("Please select a model before continuing.", ephemeral=True)
            return
        image_gen_modal = ImageGenPromptModal(
            cog=self.cog,
            model=self.model,
            add_default_positive=self.default_pos,
            add_default_negative=self.default_neg,
            nsfw_level=self.nsfw,
            initial_prompt=self.initial_prompt,
            initial_negative_prompt=self.initial_negative_prompt,
        )
        await interaction.response.send_modal(image_gen_modal)
        await image_gen_modal.wait()
        # Disable all buttons in the view
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
            if isinstance(child, ui.Select):
                child.disabled = True
        await interaction.edit_original_response(view=self)

class ImageGenPromptModal(ui.Modal, title="🖼️ Enter Prompt for Image Generation"):
    def __init__(self, cog: 'ImageDiffusion', model: Models, add_default_positive: DefaultChoice,
                 add_default_negative: DefaultChoice, nsfw_level: NsfwLevel,
                 initial_prompt: str = "", initial_negative_prompt: str = "", view: ui.View=None,
                 original_response = None):
        super().__init__()
        self.cog = cog
        self.model = model
        self.add_default_positive = add_default_positive
        self.add_default_negative = add_default_negative
        self.nsfw_level = nsfw_level
        self.view = view
        self.ephemeral_message = original_response

        self.prompt = ui.TextInput(
            label="Prompt",
            placeholder="e.g., A futuristic cyberpunk city at night",
            required=True,
            max_length=500,
            style=discord.TextStyle.paragraph,
            default=initial_prompt
        )

        self.negative_prompt = ui.TextInput(
            label="Negative Prompt (Optional)",
            placeholder="e.g., blurry, out of focus",
            required=False,
            max_length=300,
            default=initial_negative_prompt
        )
        self.add_item(self.prompt)
        self.add_item(self.negative_prompt)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.schedule_generation(
            prompt=self.prompt.value,
            model=self.model,
            interaction=interaction,
            negative_prompt=self.negative_prompt.value,
            use_default_positive=self.add_default_positive == DefaultChoice.YES,
            use_default_negative=self.add_default_negative == DefaultChoice.YES,
            nsfw=self.nsfw_level
        )


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


class PromptDetailButton(ui.View):
    def __init__(self, user_prompt: str, user_negative: str, positive: str, negative: str,
                 image: ImageCreation, author_id: int, gen_time: float):
        super().__init__(timeout=900)

        self.user_prompt = user_prompt
        self.user_negative = user_negative
        self.positive = positive
        self.negative = negative

        self.model = image.model
        self.use_default_positive = image.use_default_positive
        self.use_default_negative = image.use_default_negative
        self.nsfw_level = image.nsfw
        self.author_id = author_id
        self.gen_time = gen_time
        self.already_clicked_prompt_details = set()
        self._cooldown = {}
        self._cooldown_seconds = 30
        self.moderator_role_id = 1083514560155222086

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

        await self.message.edit(view=self)

    @ui.button(label="See prompt details", style=discord.ButtonStyle.blurple)
    async def show_prompts(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id in self.already_clicked_prompt_details:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return

        embed = discord.Embed(
            title="🖼️ Image Generation Details",
            description=f"**Model:** `{self.model}`",
            color=discord.Color.blurple()
        )

        embed.add_field(name="Positive Prompt", value=self.positive[:1024], inline=False)
        embed.add_field(name="Negative Prompt", value=self.negative[:1024] or 'None', inline=False)
        embed.add_field(name="Added default positive prompt", value=self.use_default_positive, inline=False)
        embed.add_field(name="Added default negative prompt", value=self.use_default_negative, inline=False)
        embed.add_field(name="Nsfw Level", value=self.nsfw_level, inline=False)
        embed.add_field(name="Generation time", value=f"{self.gen_time:.2f} seconds", inline=False)
        self.already_clicked_prompt_details.add(interaction.user.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="Resubmit", style=discord.ButtonStyle.green)
    async def resubmit_button(self, interaction: discord.Interaction, button: ui.Button):
        now = time.time()
        last_used = self._cooldown.get(interaction.user.id, 0)
        if now - last_used < self._cooldown_seconds:
            await interaction.response.send_message(
                f"You're using this too quickly! Try again in {int(self._cooldown_seconds - (now - last_used))}s.",
                ephemeral=True
            )
            return

        self._cooldown[interaction.user.id] = now

        view = ImageGenPrefView(
            cog=interaction.client.get_cog("ImageDiffusion"),
            default_model=self.model,
            default_pos=DefaultChoice.YES if self.use_default_positive else DefaultChoice.NO,
            default_neg=DefaultChoice.YES if self.use_default_negative else DefaultChoice.NO,
            nsfw_level=self.nsfw_level,
            initial_prompt=self.user_prompt,
            initial_negative_prompt=self.user_negative
        )

        await interaction.response.send_message(
            "Edit your options before resubmitting:", view=view, ephemeral=True
        )

    @ui.button(label="Delete", style=discord.ButtonStyle.red)
    async def delete_button(self, interaction: discord.Interaction, button: ui.Button):
        is_author = interaction.user.id == self.author_id
        is_moderator = self.moderator_role_id and any(role.id == self.moderator_role_id for role in interaction.user.roles)

        if not (is_author or is_moderator):
            await interaction.response.send_message("You don't have permission to delete this message.", ephemeral=True)
            return

        await interaction.message.delete()


class ImageDiffusion(Cog):
    """Optimized Commands for image diffusion."""
    def __init__(self, bot):
        self.bot = bot
        self.image_queue = asyncio.Queue(maxsize=5)
        self.next_image = asyncio.Event()

        self.models_to_load_on_boot = [
            Models.ANIME,
            Models.ANIPONY,
            Models.PONY
        ]

        self.models: dict[Models, BaseDiffusionModel] = {
            Models.ANIME: WaiAnimeIllustriousModel(),
            Models.ANIPONY: WaiAnimePonyModel(),
            Models.PONY: CyberRealisticPonyModel(), # TODO update this later, but for now we will only use that one
            Models.CYBER_PONY: CyberRealisticPonyModel()
        }

        self.bot.loop.create_task(self.load_pipelines())
        self.image_generation.start()

    async def load_pipelines(self):
        """Preload all specified models into memory."""
        log.info("Loading all pipelines...")
        for model in self.models_to_load_on_boot:
            await self.models[model].load_pipeline()
        log.info("All specified pipelines loaded successfully.")


    @app_commands.command(name="imagegen_default_prompts", description='Print the default negative or positive prompts for the given model.')
    @app_commands.describe(
        model=f'The model you want to see the default prompt of.',
        prompt_type='Whether you want to see the default positive or negative prompt.'
    )
    @app_commands.guild_only()
    async def default_prompt(self, interaction: discord.Interaction,
                             model: Models,
                             prompt_type: PromptType
        ) -> None:

        if interaction.channel_id not in ALlOWED_CHANNELS:
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        try:
            model_class = self.models[model]
        except KeyError:
            await interaction.response.send_message("You did not specify a correct model.", ephemeral=True)
            return

        if prompt_type == PromptType.POSITIVE:
            default_prompt = model_class.default_pos
        else:  # Default Negative
            default_prompt = model_class.default_neg

        await interaction.response.send_message(f"The default {prompt_type.value} prompt for {model.value} is:\n{default_prompt}")

    @app_commands.command(name="imagegen_form", description='Open a form to generate an image with prompt options.')
    @app_commands.guild_only()
    async def generate_modal(self, interaction: discord.Interaction) -> None:
        if interaction.channel_id not in ALlOWED_CHANNELS:
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        await interaction.response.send_message(content="Select options to generate image:", view=ImageGenPrefView(self), ephemeral=True)

    @app_commands.command(name="imagegen", description='Generate an image using text to image model.')
    @app_commands.describe(
        model=f'Choose one of these models: {ANIME_DESCRIPTION}, {PONY_DESCRIPTION}, {ANIPONY_DESCRIPTION}',
        prompt='Prompt to generate the image.',
        negative_prompt="(Optional) Avoid these elements in the image.",
        add_default_negative="(Optional) Whether to include the default negative prompt (default: Yes).",
        add_default_positive="(Optional) Whether to include the default positive prompt (default: Yes).",
        nsfw_level='(Optional) General, sensitive, explicit, nsfw, or Not Specified (default: Not Specified).'
    )
    @app_commands.guild_only()
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

    async def schedule_generation(self, prompt: str, model: Models,
                                  interaction: discord.Interaction =None, negative_prompt='', use_default_negative=True,
                                  use_default_positive=True, nsfw: NsfwLevel=NsfwLevel.NOT_SPECIFIED) -> None:
        if self.image_queue.full():
            await interaction.response.send_message("Queue is full! Please wait and try again.")
            return

        # Non-nsfw channels will default to less nsfw and explicit things
        channel_id = interaction.channel.id
        if channel_id in [IMAGE_DIFFUSION_CHANNEL, SHEPHERD_CHANNEL] and nsfw == NsfwLevel.NOT_SPECIFIED:
            if negative_prompt:
                negative_prompt += ', '
            negative_prompt += 'nsfw, nude, nudity, naked, lingerie, underwear, cleavage, erotic, lewd, sexual,'\
                                ' exposed, suggestive, inappropriate, skimpy, pornographic, uncensored'

        image_entry = ImageCreation(prompt, model, interaction=interaction, negative_prompt=negative_prompt,
                              use_default_negative=use_default_negative, use_default_positive=use_default_positive,
                              nsfw=nsfw)

        await self.image_queue.put(image_entry)
        await interaction.response.send_message("Added your prompt to the generation queue")

        log.info(f"Added prompt to queue: {prompt} for model: {model}")

    @tasks.loop(seconds=1)
    async def image_generation(self) -> None:
        if self.image_queue.empty():
            return
        log.debug("Processing next queued image...")
        self.next_image.clear()
        image: ImageCreation = await self.image_queue.get()
        await self._image_generation_interaction(image)
        self.bot.loop.call_soon_threadsafe(self.next_image.set)
        await self.next_image.wait()

    async def _image_generation_interaction(self, image: ImageCreation) -> None:
        """Handles image generation but with an interaction."""
        timeout = 600
        model = self.models[image.model]

        try:
            output, positive, negative, gen_time = await asyncio.wait_for(model.generate(image), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Image generation timed out")
            await image.interaction.channel.send(content="Image generation timed out. Try again with a simpler prompt.",
                                                  allowed_mentions=discord.AllowedMentions(users=True))
            return
        except discord.HTTPException:
            await image.interaction.channel.send(content=f"Error generating prompt: {image.prompt}.",
                                                  allowed_mentions=discord.AllowedMentions(users=True))
            return
        except RuntimeError as e:
            log.error(f"RuntimeError during generation: {e}")
            if "CUDNN_STATUS_INTERNAL_ERROR" in str(e) or "allocation failed" in str(e).lower():
                log.error("Trying gc.collect and stuff")
                gc.collect()
                torch.cuda.ipc_collect()
                torch.cuda.empty_cache()
            await image.interaction.channel.send(content=f"Image generation failed: {str(e)}",
                                                 allowed_mentions=discord.AllowedMentions(users=True))
            return
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            await image.interaction.channel.send(content=f"Image generation failed: {str(e)}",
                                                  allowed_mentions=discord.AllowedMentions(users=True))
            return

        img = output.images[0]
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        file = discord.File(buffer, filename="generated.png")
        author_id = image.interaction.user.id

        prompt_details = PromptDetailButton(
            user_prompt=image.prompt,
            user_negative=image.negative_prompt,
            positive=positive,
            negative=negative,
            image=image,
            author_id=author_id,
            gen_time=gen_time
        )

        try:
            prompt_details.message = await image.interaction.channel.send(
                content=f"**{image.prompt} - {image.model} model - {image.interaction.user.mention}**",
                file=file,
                view=prompt_details,
                allowed_mentions=discord.AllowedMentions(users=True, replied_user=True)
            )
        except Exception as e:
            log.error(f"Error: {e}")
            await image.interaction.channel.send(content=f"Some error occurred while image generation:\n{e}",
                                                  allowed_mentions=discord.AllowedMentions(users=True))

        log.debug("Image sent to Discord")


    def cog_unload(self):
        self.image_generation.cancel()
        for model in self.models.values():
            model.unload_pipeline()
        gc.collect()
        torch.cuda.empty_cache()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
