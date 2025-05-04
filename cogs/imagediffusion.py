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
from diffusers import (StableDiffusionPipeline, StableDiffusion3Pipeline, EulerDiscreteScheduler,
                       FlowMatchEulerDiscreteScheduler, EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline)
from diffusers.models import AutoencoderKL
from discord.ext import commands, tasks
from discord.ext.commands import Cog

log = logging.getLogger(__name__)
WAIFU_DIFFUSION = "./models/wd-1-5-beta3"
WAIFU_VAE = "./models/vae/kl-f8-anime2_clean.ckpt"
ANYTHING_V5 = "./models/anything-v5/AnythingXL_v50.safetensors"
ANYTHING_V5_VAE = "./models/anything-v5/vae/Anything-V3.0.vae.safetensors"
ANYTHING_V5_PATH = "./models/anything-v5/"
ANI_PONY = r'.\models\ani-pony\waiANINSFWPONYXL_v130.safetensors'
PONY_REALISM = r'.\models\pony-realism\ponyRealism_V22.safetensors'
CUDA = "cuda"

WAIFU_POSITIVE_PROMPT = "(exceptional, best aesthetic, new, newest, best quality, masterpiece, extremely detailed, " \
                         "anime, waifu:1.2)"
WAIFU_NEGATIVE_PROMPT = "lowres, ((bad anatomy)), ((bad hands)), missing finger, extra digits, fewer digits, blurry, " \
                         "((mutated hands and fingers)), (poorly drawn face), ((mutation)), ((deformed face)), (ugly), " \
                         "((bad proportions)), ((extra limbs)), extra face, (double head), (extra head), ((extra feet))," \
                         " monster, logo, cropped, worst quality, jpeg, humpbacked, long body, long neck," \
                         " ((jpeg artifacts)), deleted, old, oldest, ((censored)), ((bad aesthetic))," \
                         " (mosaic censoring, bar censor, blur censor)"

ANI_PONY_NEGATIVE_PROMPT = "worst quality, bad quality, jpeg artifacts, source_cartoon, \
                            3d, (censor), monochrome, blurry, lowres,watermark,"
ANI_PONY_POSITIVE_PROMPT = "(score_9, score_8_up, score_7_up, source_anime)"

STABLE_NEGATIVE_PROMPT = "(ugly, disfigured, deformed, blurry, low quality, poorly drawn, poorly lit, out of focus," \
                          " overexposed, underexposed, malformed, uncoherent, low resolution, bad anatomy," \
                          " bad proportions, poorly rendered, unrealistic, text, watermark, signature, over-saturated)"
# PONY_REALISM_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, realistic, photorealistic, detailed background, " \
#                                 "vivid colors, masterpiece:1.2"
PONY_REALISM_POSITIVE_PROMPT = "(score_9, score_8_up, score_7_up, BREAK)"
PONY_REALISM_NEGATIVE_PROMPT = "score_4, score_5, score_6"
# PONY_REALISM_NEGATIVE_PROMPT = "score_6, score_5, score_4, lowres, bad anatomy, bad hands, text, error," \
#                                 " missing fingers, extra digit, fewer digits, cropped, worst quality, low quality," \
#                                 " jpeg artifacts, signature, watermark, blurry"


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper

class ImageCreation:
    def __init__(self, ctx, prompt, model):
        self.ctx = ctx
        self.prompt = prompt
        self.model = model

class ImageDiffusion(Cog):
    """Optimized Commands for image diffusion."""
    def __init__(self, bot):
        self.bot = bot
        self.image_queue = asyncio.Queue(maxsize=5)
        self.next_image = asyncio.Event()
        self.pipelines = {}
        self.bot.loop.create_task(self.load_pipelines())
        self.image_generation.start()

    async def load_pipelines(self):
        """Preload all models into memory."""
        log.info("Loading all pipelines...")
        # self.pipelines[WAIFU_DIFFUSION] = await self.load_pipeline(WAIFU_DIFFUSION)
        # self.pipelines[ANYTHING_V5] = await self.load_pipeline(ANYTHING_V5)
        self.pipelines[ANI_PONY] = await self.load_pipeline(ANI_PONY)
        self.pipelines[PONY_REALISM] = await self.load_pipeline(PONY_REALISM)
        log.info("All pipelines loaded successfully.")

    async def load_pipeline(self, model_path):
        """Helper to load one model."""
        if model_path == WAIFU_DIFFUSION:
            pipe = StableDiffusionPipeline.from_single_file(
                os.path.join(model_path, 'wd-illusion-fp16.safetensors'),
                torch_dtype=torch.float16,
                use_safetensors=True,
                load_safety_checker=None,
                vae=AutoencoderKL.from_single_file(WAIFU_VAE, use_safetensors=False, torch_dtype=torch.float16).to("cuda")
            ).to(CUDA)
            pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
            pipe.enable_xformers_memory_efficient_attention()

        elif model_path == ANYTHING_V5:
            vae = AutoencoderKL.from_single_file(ANYTHING_V5_VAE, torch_dtype=torch.float16)
            pipe = StableDiffusionPipeline.from_single_file(
                model_path,
                torch_dtype=torch.float16,
                use_safetensors=True,
                vae=vae,
                load_safety_checker=None,
                original_config_file=f"{ANYTHING_V5_PATH}/config/v1-inference.yaml"
            ).to(CUDA)
            pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
            pipe.enable_xformers_memory_efficient_attention()

        elif model_path == PONY_REALISM:
            pipe = StableDiffusionXLPipeline.from_single_file(
                PONY_REALISM,
                torch_dtype=torch.float16,
                use_safetensors=True,
                load_safety_checker=None,
            ).to(CUDA)
            pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
            pipe.enable_xformers_memory_efficient_attention()
            pipe.enable_model_cpu_offload()

        elif model_path == ANI_PONY:
            pipe = StableDiffusionXLPipeline.from_single_file(
                ANI_PONY,
                torch_dtype=torch.float16,
                use_safetensors=True,
                load_safety_checker=None,
            )
            pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
            pipe.enable_xformers_memory_efficient_attention()
            pipe.enable_model_cpu_offload()

        return pipe

    @commands.group(invoke_without_command=True)
    async def waifu(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using Waifu Diffusion v1.5 Beta 3.

        Parameters:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, WAIFU_DIFFUSION)

    @commands.group(invoke_without_command=True)
    async def anime(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using Anything V5.0 anime model.

        Parameters:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, ANI_PONY)

    @commands.group(invoke_without_command=True)
    async def pony(self, ctx: commands.Context, *, prompt: str) -> None:
        """Queue image using Pony Realism V2.2.

        Parameters:
        - prompt (str): The prompt for the image generation.
        """
        await self.schedule_generation(ctx, prompt, PONY_REALISM)

    async def schedule_generation(self, ctx, prompt, model) -> None:
        if self.image_queue.full():
            await ctx.reply("Queue is full! Please wait and try again.")
            return
        entry = ImageCreation(ctx, prompt, model)
        await self.image_queue.put(entry)
        await ctx.message.add_reaction("👍")
        log.info(f"Added prompt to queue: {prompt}")

    @tasks.loop(seconds=1)
    async def image_generation(self) -> None:
        if self.image_queue.empty():
            return

        log.info("Processing next queued image...")
        self.next_image.clear()
        image = await self.image_queue.get()
        ctx = image.ctx
        prompt = image.prompt
        model_path = image.model
        timeout = 600

        if image.model == WAIFU_DIFFUSION:
            image_gen = self.generate_image_waifu
        elif image.model == ANYTHING_V5:
            image_gen = self.generate_image_anything
        elif image.model == PONY_REALISM:
            image_gen = self.generate_image_pony
        elif image.model == ANI_PONY:
            image_gen = self.generate_image_ani_pony
        else:
            log.warning("There was somehow a model queued up that does not exist.")
            return

        try:
            output = await asyncio.wait_for(image_gen(model_path, prompt), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Image generation timed out")
            return await ctx.reply("Image generation timed out. Try again with a simpler prompt.")
        except Exception as e:
            log.error(f"Generation failed: {str(e)}")
            return await ctx.reply(f"Image generation failed: {str(e)}")

        img = output.images[0]
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        file = discord.File(buffer, filename="generated.png")

        await ctx.reply(file=file)
        log.info("Image sent to Discord")
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
        prompt = f"{prompt}, {ANI_PONY_POSITIVE_PROMPT}"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=30,
                guidance_scale=7.5,
                height=768,
                width=768,
                negative_prompt=ANI_PONY_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_anything(self, model_path, prompt):
        """Generate an image given a model path and a prompt."""
        pipe = self.pipelines.get(model_path)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {model_path}")

        log.info(f"Starting generation for prompt: {prompt}")
        gen_start = time.time()
        prompt = f"{prompt}, (exceptional, best aesthetic, new, newest, best quality, masterpiece, extremely detailed)"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=28,
                guidance_scale=7.5,
                height=768,
                width=768,
                negative_prompt=WAIFU_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    @to_thread
    def generate_image_waifu(self, model_path, prompt):
        pipe = self.pipelines.get(model_path)
        if pipe is None:
            raise ValueError(f"No pipeline loaded for model {model_path}")

        log.info(f"Starting waifu generation for prompt: {prompt}")
        gen_start = time.time()
        prompt = f"{prompt}, {WAIFU_POSITIVE_PROMPT}"
        with torch.autocast(CUDA, dtype=torch.float16):
            result = pipe(
                prompt,
                num_inference_steps=28,
                guidance_scale=7.5,
                height=768,
                width=768,
                negative_prompt=WAIFU_NEGATIVE_PROMPT
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
                num_inference_steps=30,
                guidance_scale=7.5,
                height=768,
                width=768,
                negative_prompt=PONY_REALISM_NEGATIVE_PROMPT
            )
        gen_time = time.time() - gen_start
        log.info(f"Generation completed for prompt: {prompt} in {gen_time:.2f} seconds")
        return result

    def cog_unload(self):
        self.image_generation.cancel()
        torch.cuda.empty_cache()


async def setup(bot):
    await bot.add_cog(ImageDiffusion(bot))
