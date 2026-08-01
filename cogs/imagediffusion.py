import asyncio
import functools
from io import BytesIO
import time
import typing
from typing import Literal, Optional
import discord
import logging
from enum import Enum, StrEnum
from discord.ext import commands, tasks
from discord.ext.commands import Cog
from discord import app_commands, ui
from cogs.utils.constants import *
from cogs.utils.image_models import (
    Models,
    NsfwLevel,
    ImageCreation,
    ComfyWaiIllustriousModel,
    ComfyZImageTurboModel,
    ComfyAnimaAestheticModel,
    ComfyOneObsessionIllustriousModel,
    ComfyOneObsessionAnimaModel,
    ComfyKrea2TurboModel,
    ComfyRedCraftModel,
)
from cogs.utils import comfy_client
from cogs.utils.server_config import get_guild_config

log = logging.getLogger(__name__)

# asyncio / Comfy wait budgets (seconds). LLM TextGenerate on Krea-family can run long.
GENERATION_TIMEOUT_SEC = 600
GENERATION_TIMEOUT_LLM_ENHANCE_SEC = 1800  # 30 min — enhance + sample
# Max chars of the user prompt shown above the image (full text is in "See prompt details").
PROMPT_PREVIEW_MAX_CHARS = 160
# Discord embed field value limit.
DISCORD_EMBED_FIELD_MAX = 1024


def _preview_prompt(text: str, max_chars: int = PROMPT_PREVIEW_MAX_CHARS) -> str:
    """Short one-line-ish preview for the public channel message."""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


def _add_long_embed_field(embed: discord.Embed, name: str, value: str, *, max_total: int = 4000) -> None:
    """Add a field, splitting long text across continuation fields (Discord 1024/field)."""
    value = value if value else "None"
    if len(value) <= DISCORD_EMBED_FIELD_MAX:
        embed.add_field(name=name, value=value, inline=False)
        return
    clipped = value[:max_total]
    chunks = [
        clipped[i : i + DISCORD_EMBED_FIELD_MAX]
        for i in range(0, len(clipped), DISCORD_EMBED_FIELD_MAX)
    ]
    for i, chunk in enumerate(chunks):
        field_name = name if i == 0 else f"{name} (cont. {i + 1})"
        embed.add_field(name=field_name, value=chunk, inline=False)
    if len(value) > max_total:
        embed.add_field(
            name=f"{name} (truncated)",
            value=f"…and {len(value) - max_total} more characters not shown.",
            inline=False,
        )


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
                 llm_prompt_enhance: DefaultChoice = DefaultChoice.NO,
                 initial_prompt: str = "",
                 initial_negative_prompt: str = ""):
        super().__init__(timeout=300)
        self.cog = cog

        self.model = default_model
        self.default_pos = default_pos
        self.default_neg = default_neg
        self.nsfw = nsfw_level
        self.llm_prompt_enhance = llm_prompt_enhance
        self.initial_prompt = initial_prompt
        self.initial_negative_prompt = initial_negative_prompt

        self.model_select = ui.Select(
            placeholder="Select a model (required)",
            options=[
                discord.SelectOption(
                    label="Anime WAI Illustrious",
                    value=Models.ANIME_WAI_ILLUSTRIOUS,
                    description="WAI Illustrious SDXL anime",
                    default=(default_model == Models.ANIME_WAI_ILLUSTRIOUS),
                ),
                discord.SelectOption(
                    label="Z Image Turbo",
                    value=Models.Z_IMAGE_TURBO_FP8,
                    description="Z-Image Turbo FP8",
                    default=(default_model == Models.Z_IMAGE_TURBO_FP8),
                ),
                discord.SelectOption(
                    label="Anima Aesthetic",
                    value=Models.ANIMA_AESTHETIC,
                    description="Base Anima aesthetic v1.1",
                    default=(default_model == Models.ANIMA_AESTHETIC),
                ),
                discord.SelectOption(
                    label="One Obsession Illustrious (NSFW)",
                    value=Models.ONE_OBSESSION_ILLUSTRIOUS,
                    description="One Obsession v23 Illustrious",
                    default=(default_model == Models.ONE_OBSESSION_ILLUSTRIOUS),
                ),
                discord.SelectOption(
                    label="One Obsession Anima (NSFW)",
                    value=Models.ONE_OBSESSION_ANIMA,
                    description="One Obsession Anima v2.0",
                    default=(default_model == Models.ONE_OBSESSION_ANIMA),
                ),
                discord.SelectOption(
                    label="Krea 2 Turbo",
                    value=Models.KREA2_TURBO,
                    description="Krea 2 Turbo INT8 (~8 steps)",
                    default=(default_model == Models.KREA2_TURBO),
                ),
                discord.SelectOption(
                    label="RedCraft",
                    value=Models.REDCRAFT,
                    description="RedCraft 2.3 Krea2 INT8/INT4/FP8",
                    default=(default_model == Models.REDCRAFT),
                ),
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

        # Discord allows 5 action rows: 4 selects above + 1 button row with Continue + LLM toggle.
        self._llm_enhance_on = llm_prompt_enhance == DefaultChoice.YES
        self.llm_toggle_button = ui.Button(
            label=self._llm_enhance_label(),
            style=discord.ButtonStyle.secondary if not self._llm_enhance_on else discord.ButtonStyle.primary,
            row=4,
        )
        self.llm_toggle_button.callback = self.llm_toggle_callback
        self.add_item(self.llm_toggle_button)

        self.continue_button = ui.Button(
            label="Continue",
            style=discord.ButtonStyle.green,
            row=4,
        )
        self.continue_button.callback = self.continue_callback
        self.add_item(self.continue_button)

    def _llm_enhance_label(self) -> str:
        state = "ON" if self._llm_enhance_on else "OFF"
        return f"LLM prompt enhance: {state}"

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

    async def llm_toggle_callback(self, interaction: discord.Interaction):
        self._llm_enhance_on = not self._llm_enhance_on
        self.llm_prompt_enhance = DefaultChoice.YES if self._llm_enhance_on else DefaultChoice.NO
        self.llm_toggle_button.label = self._llm_enhance_label()
        self.llm_toggle_button.style = (
            discord.ButtonStyle.primary if self._llm_enhance_on else discord.ButtonStyle.secondary
        )
        await interaction.response.edit_message(view=self)

    async def continue_callback(self, interaction: discord.Interaction):
        if not self.model:
            await interaction.response.send_message("Please select a model before continuing.", ephemeral=True)
            return
        image_gen_modal = ImageGenPromptModal(
            cog=self.cog,
            model=self.model,
            add_default_positive=self.default_pos,
            add_default_negative=self.default_neg,
            nsfw_level=self.nsfw,
            llm_prompt_enhance=self._llm_enhance_on,
            initial_prompt=self.initial_prompt,
            initial_negative_prompt=self.initial_negative_prompt,
        )
        await interaction.response.send_modal(image_gen_modal)
        await image_gen_modal.wait()
        for child in self.children:
            if isinstance(child, (ui.Button, ui.Select)):
                child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass


class ImageGenPromptModal(ui.Modal, title="🖼️ Enter Prompt for Image Generation"):
    def __init__(self, cog: 'ImageDiffusion', model: Models, add_default_positive: DefaultChoice,
                 add_default_negative: DefaultChoice, nsfw_level: NsfwLevel,
                 llm_prompt_enhance: bool = False,
                 initial_prompt: str = "", initial_negative_prompt: str = "", view: ui.View = None,
                 original_response=None):
        super().__init__()
        self.cog = cog
        self.model = model
        self.add_default_positive = add_default_positive
        self.add_default_negative = add_default_negative
        self.nsfw_level = nsfw_level
        self.llm_prompt_enhance = llm_prompt_enhance
        self.view = view
        self.ephemeral_message = original_response

        self.prompt = ui.TextInput(
            label="Prompt",
            placeholder="e.g., A futuristic cyberpunk city at night",
            required=True,
            max_length=1000,
            style=discord.TextStyle.paragraph,
            default=initial_prompt,
        )

        self.negative_prompt = ui.TextInput(
            label="Negative Prompt (Optional)",
            placeholder="e.g., blurry, out of focus",
            required=False,
            max_length=1000,
            default=initial_negative_prompt,
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
            nsfw=self.nsfw_level,
            llm_prompt_enhance=self.llm_prompt_enhance,
        )


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


class PromptDetailButton(ui.View):
    def __init__(self, user_prompt: str, user_negative: str, positive: str, negative: str,
                 image: ImageCreation, author_id: int, gen_time: float, guild_id: int):
        super().__init__(timeout=900)

        self.user_prompt = user_prompt
        self.user_negative = user_negative
        self.positive = positive
        self.negative = negative

        self.model = image.model
        self.use_default_positive = image.use_default_positive
        self.use_default_negative = image.use_default_negative
        self.nsfw_level = image.nsfw
        self.llm_prompt_enhance = image.llm_prompt_enhance
        self.author_id = author_id
        self.gen_time = gen_time
        self.guild_id = guild_id
        self.already_clicked_prompt_details = set()
        self._cooldown = {}
        self._cooldown_seconds = 30

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

        _add_long_embed_field(embed, "User Prompt", self.user_prompt)
        _add_long_embed_field(embed, "Positive Prompt", self.positive)
        _add_long_embed_field(embed, "Negative Prompt", self.negative or "None")
        embed.add_field(name="Added default positive prompt", value=str(self.use_default_positive), inline=False)
        embed.add_field(name="Added default negative prompt", value=str(self.use_default_negative), inline=False)
        embed.add_field(name="Nsfw Level", value=str(self.nsfw_level), inline=False)
        embed.add_field(name="LLM prompt enhance", value=str(self.llm_prompt_enhance), inline=False)
        embed.add_field(name="Generation time", value=f"{self.gen_time:.2f} seconds", inline=False)
        self.already_clicked_prompt_details.add(interaction.user.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="Edit and Regenerate", style=discord.ButtonStyle.green)
    async def edit_regenerate_button(self, interaction: discord.Interaction, button: ui.Button):
        """Opens the same editor as before to tweak prompts/settings before regenerating."""
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
            llm_prompt_enhance=DefaultChoice.YES if self.llm_prompt_enhance else DefaultChoice.NO,
            initial_prompt=self.user_prompt,
            initial_negative_prompt=self.user_negative
        )

        await interaction.response.send_message(
            "Edit your options before regenerating:", view=view, ephemeral=True
        )

    @ui.button(label="Regenerate", style=discord.ButtonStyle.gray)
    async def regenerate_button(self, interaction: discord.Interaction, button: ui.Button):
        """Regenerates the image with the exact same settings (no editing)."""
        now = time.time()
        last_used = self._cooldown.get(interaction.user.id, 0)
        if now - last_used < self._cooldown_seconds:
            await interaction.response.send_message(
                f"You're using this too quickly! Try again in {int(self._cooldown_seconds - (now - last_used))}s.",
                ephemeral=True
            )
            return

        self._cooldown[interaction.user.id] = now

        # Reuse the exact same creation parameters
        image_entry = ImageCreation(
            prompt=self.user_prompt,
            model=self.model,
            interaction=interaction,
            negative_prompt=self.user_negative,
            use_default_negative=self.use_default_negative,
            use_default_positive=self.use_default_positive,
            nsfw=self.nsfw_level,
            llm_prompt_enhance=self.llm_prompt_enhance,
        )

        cog = interaction.client.get_cog("ImageDiffusion")
        await cog.enqueue_image(
            image_entry,
            interaction,
            queued_message="Queued a new generation with the same settings!",
            ephemeral=True,
        )

    @ui.button(label="Delete", style=discord.ButtonStyle.red)
    async def delete_button(self, interaction: discord.Interaction, button: ui.Button):
        is_author = interaction.user.id == self.author_id
        moderator_role_id = None
        guild_config = get_guild_config(self.guild_id)
        if guild_config is not None:
            moderator_role_id = guild_config.image_diffusion_moderator_role_id
        is_moderator = (
            moderator_role_id is not None
            and any(role.id == moderator_role_id for role in interaction.user.roles)
        )

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

        self.active_model: Optional[Models] = None

        self.models_to_load_on_boot = [
            Models.ANIME_WAI_ILLUSTRIOUS,
            Models.Z_IMAGE_TURBO_FP8,
            Models.ANIMA_AESTHETIC,
            Models.ONE_OBSESSION_ILLUSTRIOUS,
            Models.ONE_OBSESSION_ANIMA,
            Models.KREA2_TURBO,
            Models.REDCRAFT,
        ]

        # Map front-end choices to ComfyUI workflows
        self.models = {
            Models.ANIME_WAI_ILLUSTRIOUS: ComfyWaiIllustriousModel(),
            Models.Z_IMAGE_TURBO_FP8: ComfyZImageTurboModel(),
            Models.ANIMA_AESTHETIC: ComfyAnimaAestheticModel(),
            Models.ONE_OBSESSION_ILLUSTRIOUS: ComfyOneObsessionIllustriousModel(),
            Models.ONE_OBSESSION_ANIMA: ComfyOneObsessionAnimaModel(),
            Models.KREA2_TURBO: ComfyKrea2TurboModel(),
            Models.REDCRAFT: ComfyRedCraftModel(),
        }

        self.comfy_ready = asyncio.Event()
        self.comfy_startup_error: Optional[str] = None
        self._generation_in_progress = False

        self.bot.loop.create_task(self._startup())
        self.image_generation.start()
        self.comfy_health_watcher.start()

    def _is_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        config = get_guild_config(guild_id)
        if config is None or not config.enabled:
            return False
        return config.is_image_diffusion_channel(channel_id)

    async def _startup(self):
        await self.bot.wait_until_ready()
        try:
            comfy_client.apply_config_defaults()
            await asyncio.to_thread(comfy_client.ensure_comfy_running)
            await self.load_pipelines()
        except Exception as e:
            self.comfy_startup_error = str(e)
            log.exception("ComfyUI startup failed")
        finally:
            self.comfy_ready.set()

    async def load_pipelines(self):
        """Preload all specified ComfyUI workflows into memory."""
        log.info("Loading all ComfyUI workflows...")
        for model in self.models_to_load_on_boot:
            await self.models[model].load_pipeline()
        log.info("All specified workflows loaded successfully.")

    imagegen_group = app_commands.Group(name="imagegen", description="Image Generation Commands.")

    @imagegen_group.command(name="status", description="Show ComfyUI health (owner only).")
    @app_commands.guild_only()
    async def comfy_status(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return

        status = comfy_client.get_comfy_status()
        queue_depth = self.image_queue.qsize()
        lines = [
            f"**ComfyUI status**",
            f"Running: {status['running']}",
            f"Source: {status['source']}",
            f"Address: {status['host']}:{status['port']}",
            f"Root: {status.get('root', 'n/a')}",
            f"PID (bot-started): {status['pid'] or 'n/a'}",
            f"Queue depth: {queue_depth}/{self.image_queue.maxsize}",
        ]
        if self.comfy_startup_error:
            lines.append(f"Startup error: {self.comfy_startup_error}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @imagegen_group.command(name="show_default_prompts", description='Print the default negative or positive prompts for the given model.')
    @app_commands.describe(
        model=f'The model you want to see the default prompt of.',
        prompt_type='Whether you want to see the default positive or negative prompt.'
    )
    @app_commands.guild_only()
    async def default_prompt(self, interaction: discord.Interaction,
                             model: Models,
                             prompt_type: PromptType
        ) -> None:

        if not self._is_allowed_channel(interaction.guild_id, interaction.channel_id):
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

    @imagegen_group.command(name="generate", description='Generate an image using text to image model.')
    @app_commands.describe(
        model='Choose the generation model.',
        prompt='Prompt to generate the image.',
        negative_prompt="(Optional) Avoid these elements in the image.",
        add_default_negative="(Optional) Whether to include the default negative prompt (default: Yes).",
        add_default_positive="(Optional) Whether to include the default positive prompt (default: Yes).",
        nsfw_level='(Optional) General, sensitive, explicit, nsfw, or Not Specified (default: Not Specified).',
        llm_prompt_enhance='(Optional) Expand prompt with LLM first — Krea family only (default: No).',
    )
    @app_commands.choices(model=[
        app_commands.Choice(name="Anime WAI Illustrious", value=Models.ANIME_WAI_ILLUSTRIOUS.value),
        app_commands.Choice(name="Z Image Turbo", value=Models.Z_IMAGE_TURBO_FP8.value),
        app_commands.Choice(name="Anima Aesthetic", value=Models.ANIMA_AESTHETIC.value),
        app_commands.Choice(name="One Obsession Illustrious (NSFW)", value=Models.ONE_OBSESSION_ILLUSTRIOUS.value),
        app_commands.Choice(name="One Obsession Anima (NSFW)", value=Models.ONE_OBSESSION_ANIMA.value),
        app_commands.Choice(name="Krea 2 Turbo", value=Models.KREA2_TURBO.value),
        app_commands.Choice(name="RedCraft", value=Models.REDCRAFT.value),
    ])
    @app_commands.guild_only()
    async def generate(self, interaction: discord.Interaction,
                       model: str,
                       prompt: str,
                       negative_prompt: Optional[str] = '',
                       add_default_negative: Optional[DefaultChoice] = DefaultChoice.YES,
                       add_default_positive: Optional[DefaultChoice] = DefaultChoice.YES,
                       nsfw_level: Optional[NsfwLevel] = NsfwLevel.NOT_SPECIFIED,
                       llm_prompt_enhance: Optional[DefaultChoice] = DefaultChoice.NO,
                       ) -> None:
        if not self._is_allowed_channel(interaction.guild_id, interaction.channel_id):
            await interaction.response.send_message("This command is not allowed in this channel!", ephemeral=True)
            return

        try:
            model_enum = Models(model)
        except ValueError:
            await interaction.response.send_message("Invalid model selection.", ephemeral=True)
            return

        add_default_positive = True if add_default_positive == DefaultChoice.YES else False
        add_default_negative = True if add_default_negative == DefaultChoice.YES else False
        enhance = True if llm_prompt_enhance == DefaultChoice.YES else False

        await self.schedule_generation(
            prompt,
            model_enum,
            interaction=interaction,
            negative_prompt=negative_prompt,
            use_default_negative=add_default_negative,
            use_default_positive=add_default_positive,
            nsfw=nsfw_level,
            llm_prompt_enhance=enhance,
        )

    async def enqueue_image(
        self,
        image_entry: ImageCreation,
        interaction: discord.Interaction,
        *,
        queued_message: str = "Added your prompt to the generation queue",
        ephemeral: bool = False,
    ) -> bool:
        if not self.comfy_ready.is_set():
            await interaction.response.send_message(
                "ComfyUI is still starting up. Please try again in a moment.",
                ephemeral=True,
            )
            return False

        if self.comfy_startup_error:
            await interaction.response.send_message(
                f"Image generation is unavailable: {self.comfy_startup_error}",
                ephemeral=True,
            )
            return False

        if self.image_queue.full():
            await interaction.response.send_message(
                "Queue is full! Please wait and try again.",
                ephemeral=ephemeral,
            )
            return False

        await self.image_queue.put(image_entry)
        await interaction.response.send_message(queued_message, ephemeral=ephemeral)
        return True

    async def schedule_generation(self, prompt: str, model: Models,
                                  interaction: discord.Interaction = None, negative_prompt='', use_default_negative=True,
                                  use_default_positive=True, nsfw: NsfwLevel = NsfwLevel.NOT_SPECIFIED,
                                  llm_prompt_enhance: bool = False) -> None:
        # LLM enhance is only implemented for Krea-family graphs (Krea 2 Turbo, RedCraft).
        krea_family = {Models.KREA2_TURBO, Models.REDCRAFT}
        if llm_prompt_enhance and model not in krea_family:
            log.info(
                "llm_prompt_enhance requested for %s but only Krea-family models support it; ignoring",
                model,
            )
            llm_prompt_enhance = False

        image_entry = ImageCreation(
            prompt,
            model,
            interaction=interaction,
            negative_prompt=negative_prompt,
            use_default_negative=use_default_negative,
            use_default_positive=use_default_positive,
            nsfw=nsfw,
            llm_prompt_enhance=llm_prompt_enhance,
        )

        if await self.enqueue_image(image_entry, interaction):
            log.info(
                f"Added prompt to queue: {prompt} for model: {model} "
                f"(llm_prompt_enhance={llm_prompt_enhance})"
            )

    @tasks.loop(seconds=1)
    async def image_generation(self) -> None:
        if self.image_queue.empty():
            return
        if self.comfy_startup_error:
            return
        log.debug("Processing next queued image...")
        self.next_image.clear()

        image: ImageCreation = await self.image_queue.get()
        if self.active_model != image.model:
            comfy_client.free_memory()
        self.active_model = image.model

        self._generation_in_progress = True
        try:
            await self._image_generation_interaction(image)
        finally:
            self._generation_in_progress = False

        self.bot.loop.call_soon_threadsafe(self.next_image.set)
        await self.next_image.wait()

    @image_generation.before_loop
    async def before_image_generation(self) -> None:
        await self.bot.wait_until_ready()
        await self.comfy_ready.wait()

    @tasks.loop(seconds=30)
    async def comfy_health_watcher(self) -> None:
        if self._generation_in_progress:
            return
        if comfy_client.is_comfy_running():
            if self.comfy_startup_error:
                self.comfy_startup_error = None
            return
        log.warning("ComfyUI is not responding; attempting to ensure it is running")
        try:
            await asyncio.to_thread(comfy_client.ensure_comfy_running)
            self.comfy_startup_error = None
        except Exception as e:
            self.comfy_startup_error = str(e)
            log.exception("ComfyUI health watcher failed to restore ComfyUI")

    @comfy_health_watcher.before_loop
    async def before_comfy_health_watcher(self) -> None:
        await self.bot.wait_until_ready()
        await self.comfy_ready.wait()

    async def _image_generation_interaction(self, image: ImageCreation) -> None:
        """Handles image generation but with an interaction."""
        # LLM enhance needs a longer budget (TextGenerate + sampling).
        timeout = (
            GENERATION_TIMEOUT_LLM_ENHANCE_SEC
            if image.llm_prompt_enhance
            else GENERATION_TIMEOUT_SEC
        )
        model = self.models[image.model]

        async def _send_generation_error(message: str):
            # Discord message content limit is 2000 chars; keep room for prefix.
            safe_message = message if len(message) <= 1800 else f"{message[:1800]}... [truncated]"
            try:
                await image.interaction.channel.send(
                    content=safe_message,
                    allowed_mentions=discord.AllowedMentions(users=True)
                )
            except Exception as send_error:
                log.error(f"Failed to send generation error message: {send_error}")

        if not comfy_client.is_comfy_running():
            try:
                await asyncio.to_thread(comfy_client.ensure_comfy_running)
                self.comfy_startup_error = None
            except Exception as e:
                await _send_generation_error(f"ComfyUI is not available: {e}")
                return

        try:
            output, positive, negative, gen_time = await asyncio.wait_for(
                model.generate(image), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.error("Image generation timed out after %ss (llm_enhance=%s)", timeout, image.llm_prompt_enhance)
            await _send_generation_error(
                f"Image generation timed out after {timeout // 60} minutes. "
                "Try again without LLM enhance or with a simpler prompt."
            )
            return
        except discord.HTTPException:
            await _send_generation_error(
                f"Error generating prompt: {_preview_prompt(image.prompt, 200)}."
            )
            return
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            await _send_generation_error(f"Image generation failed: {str(e)}")
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
            gen_time=gen_time,
            guild_id=image.interaction.guild_id,
        )

        raw_prompt = (image.prompt or "").strip()
        preview = _preview_prompt(raw_prompt)
        content = f"**{preview} — `{image.model}` — {image.interaction.user.mention}**"
        if len(raw_prompt) > PROMPT_PREVIEW_MAX_CHARS:
            content += "\n-# Full prompt in **See prompt details**"

        try:
            prompt_details.message = await image.interaction.channel.send(
                content=content,
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
        self.comfy_health_watcher.cancel()
        comfy_client.shutdown_if_bot_started()
        for model in self.models.values():
            model.unload_pipeline()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
