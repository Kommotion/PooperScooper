import asyncio
import typing
import functools
from io import BytesIO
import gc
import time
import os

import discord
import torch
import logging
from diffusers import EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline
from discord.ext import commands, tasks
from discord.ext.commands import Cog

log = logging.getLogger(__name__)
CUDA = "cuda"


ANI_PONY = r'.\models\ani-pony\waiANINSFWPONYXL_v130.safetensors'
WAI_ILLUSTRIOUS = r".\models\wai_illustrious\waiNSFWIllustrious_v130.safetensors"
PONY_REALISM = r'.\models\pony-realism\ponyRealism_V22.safetensors'

ANI_PONY_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, source_anime"
ANI_PONY_NEGATIVE_PROMPT = "worst quality, bad quality, jpeg artifacts, source_cartoon, \
                            3d, (censor), monochrome, blurry, lowres,watermark,"

WAI_ILLUSTRIOUS_POSITIVE_PROMPT = "masterpiece,best quality,amazing quality,"
WAI_ILLUSTRIOUS_NEGATIVE_PROMPT = "bad quality,worst quality,worst detail,sketch,censor"

PONY_REALISM_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, BREAK"
PONY_REALISM_NEGATIVE_PROMPT = "score_4, score_5, score_6"


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


def in_allowed_channels():
    async def predicate(ctx):
        return ctx.channel.id in [1365847564196249620, 1045149015756521493]
    return commands.check(predicate)


class ImageCreation:
    def __init__(self, ctx, prompt, model):
        self.ctx: commands.Context = ctx
        self.prompt: str = prompt
        self.model: str = model


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
            WAI_ILLUSTRIOUS: self.generate_image_wai_illustrious,
            PONY_REALISM: self.generate_image_pony,
            ANI_PONY: self.generate_image_ani_pony
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
                ).to(CUDA)
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
        """Queue image using WAI Illustrious v12.

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, WAI_ILLUSTRIOUS)

    @commands.group(invoke_without_command=True)
    @in_allowed_channels()
    async def pony(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using Pony Realism V2.2.

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, PONY_REALISM)

    @commands.group(invoke_without_command=True)
    @in_allowed_channels()
    async def anipony(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using WAI Pony.

        Arguments:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, ANI_PONY)

    async def schedule_generation(self, ctx, prompt, model) -> None:
        if self.image_queue.full():
            await ctx.reply("Queue is full! Please wait and try again.")
            return
        entry = ImageCreation(ctx, prompt, model)
        await self.image_queue.put(entry)
        await ctx.message.add_reaction("👍")
        log.info(f"Added prompt to queue: {prompt} for model: {model}")

    @tasks.loop(seconds=1)
    async def image_generation(self) -> None:
        if self.image_queue.empty():
            return

        log.debug("Processing next queued image...")
        self.next_image.clear()
        image = await self.image_queue.get()
        timeout = 600

        try:
            image_gen = self.model_gen_method[image.model]
        except KeyError:
            log.warning("There was somehow a model queued up that does not exist.")
            self.bot.loop.call_soon_threadsafe(self.next_image.set)
            await self.next_image.wait()
            return

        try:
            output = await asyncio.wait_for(image_gen(image.model, image.prompt), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Image generation timed out")
            return await image.ctx.reply("Image generation timed out. Try again with a simpler prompt.")
        except discord.HTTPException:
            await image.ctx.send("Error generating prompt: {prompt}.")
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            torch.cuda.empty_cache()
            return await image.ctx.reply(f"Image generation failed: {str(e)}")

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
            logging.error(f"Error: {e}")
            await image.ctx.send(f"Some error occurred while image generation from {image.ctx.author.name}")
        log.debug("Image sent to Discord")
        self.bot.loop.call_soon_threadsafe(self.next_image.set)
        await self.next_image.wait()

    @to_thread
    def generate_image_ani_pony(self, model_path, prompt):
        """Generate an image given a model path and a prompt."""
        pipe = self.pipelines.get(model_path)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {model_path}")

        log.info(f"Starting ani pony generation for prompt: {prompt}")
        gen_start = time.time()
        prompt = f"{prompt}, {PONY_REALISM_POSITIVE_PROMPT}"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=25,
                guidance_scale=8,
                height=1024,
                width=1024,
                negative_prompt=PONY_REALISM_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_pony(self, model_path, prompt):
        pipe = self.pipelines.get(model_path)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {model_path}")

        log.info(f"Starting pony generation for prompt: {prompt}")
        gen_start = time.time()
        prompt = f"{prompt}, {PONY_REALISM_POSITIVE_PROMPT}"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=25,
                guidance_scale=8,
                height=1024,
                width=1024,
                negative_prompt=PONY_REALISM_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_wai_illustrious(self, model_path, prompt):
        pipe = self.pipelines.get(model_path)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {model_path}")

        log.info(f"Starting wai illustrious generation for prompt: {prompt}")
        gen_start = time.time()
        prompt = f"{prompt}, {WAI_ILLUSTRIOUS_POSITIVE_PROMPT}"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=20,
                guidance_scale=7,
                height=1024,
                width=1024,
                negative_prompt=WAI_ILLUSTRIOUS_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    def cog_unload(self):
        self.image_generation.cancel()
        gc.collect()
        torch.cuda.empty_cache()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
