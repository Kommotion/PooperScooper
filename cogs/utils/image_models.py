import torch
import asyncio
import typing
import time
import gc
import logging
import discord
from typing import Literal, Optional
import functools
from enum import Enum, StrEnum
from diffusers import EulerAncestralDiscreteScheduler, StableDiffusionXLPipeline
from cogs.utils.constants import *


ANI_PONY_PATH = r'.\ComfyUI\models\checkpoints\waiANINSFWPONYXL_v140.safetensors'
WAI_ILLUSTRIOUS_PATH = r".\ComfyUI\models\checkpoints\waiNSFWIllustrious_v150.safetensors"
PONY_REALISM_PATH = r'.\ComfyUI\models\checkpoints\ponyRealism_V23.safetensors'
CYBER_PONY_PATH = r".\ComfyUI\models\checkpoints\cyberrealisticPony_v120.safetensors"

ANI_PONY_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, source_anime"
ANI_PONY_NEGATIVE_PROMPT = "worst quality, bad quality, jpeg artifacts, source_cartoon, \
3d, (censor), monochrome, blurry, lowres, watermark,"

WAI_ILLUSTRIOUS_POSITIVE_PROMPT = "masterpiece,best quality,amazing quality"
WAI_ILLUSTRIOUS_NEGATIVE_PROMPT = "bad quality,worst quality,worst detail,sketch,censor,"

PONY_REALISM_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, BREAK"
PONY_REALISM_NEGATIVE_PROMPT = "score_4, score_5, score_6"

CYBER_PONY_POSITIVE_PROMPT = "score_9, score_8_up, score_7_up, (SUBJECT), "
CYBER_PONY_NEGATIVE_PROMPT = "score_6, score_5, score_4, (worst quality:1.2), (low quality:1.2), (normal quality:1.2), \
                               lowres, bad anatomy, bad hands, signature, watermarks, ugly, imperfect eyes, \
                              skewed eyes, unnatural face, unnatural body, error, extra limb, missing limbs"

log = logging.getLogger(__name__)

def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


class NsfwLevel(StrEnum):
    GENERAL = 'general'
    SENSITIVE = 'sensitive'
    NSFW = 'nsfw'
    EXPLICIT = 'explicit'
    NOT_SPECIFIED = 'not_specified'

    @classmethod
    def _missing_(cls, value):
        # Check against name (case-insensitive)
        for member in cls:
            if member.name.lower() == value.lower():
                return member
        raise ValueError(f"{value} is not a valid {cls.__name__}")


class Models(StrEnum):
    ANIME_WAI_ILLUSTRIOUS = "anime_wai_illustrious"
    ANIME_WAI_PONY = "wai_anime_pony"
    PONY_REALISM = "pony_realism"
    CYBER_REALISTIC_PONY = "cyber_realistic_pony"

    @classmethod
    def _missing_(cls, value):
        # Check against name (case-insensitive)
        for member in cls:
            if member.name.lower() == value.lower():
                return member
        raise ValueError(f"{value} is not a valid {cls.__name__}")


class ImageCreation:
    def __init__(self, prompt: Models, model, interaction=None, negative_prompt='', use_default_negative=True,
                 use_default_positive=True, nsfw: NsfwLevel=NsfwLevel.NOT_SPECIFIED):
        self.interaction: discord.Interaction = interaction
        self.negative_prompt = negative_prompt
        self.use_default_negative = use_default_negative
        self.use_default_positive = use_default_positive
        self.prompt: str = prompt
        self.model: Models = model
        self.nsfw: NsfwLevel = nsfw


class BaseDiffusionModel:
    def __init__(self, name, model_path, default_pos, default_neg, num_steps=25, guidance=8,
                 height=1024, width=1024):
        self.name = name
        self.model_path = model_path
        self.default_pos = default_pos
        self.default_neg = default_neg
        self.num_steps = num_steps
        self.guidance = guidance
        self.pipe = None
        self.log = log
        self.height = height
        self.width = width

    async def load_pipeline(self):
        pipe = StableDiffusionXLPipeline.from_single_file(
            self.model_path,
            torch_dtype=torch.float16,
            use_safetensors=True,
            load_safety_checker=None,
        )
        pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
        pipe.enable_xformers_memory_efficient_attention()
        pipe.enable_model_cpu_offload()
        self.pipe = pipe

    def unload_pipeline(self):
        """Frees memory associated with the pipeline, even if CPU offload is enabled."""
        if self.pipe:
            # Step 1: Offload model (detach all model weights)
            try:
                del self.pipe
            except Exception as e:
                self.log.warning(f"Error deleting pipe for model {self.name}: {e}")
            self.pipe = None

        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
        self.log.info(f"Successfully unloaded model: {self.name}")

    @to_thread
    def generate(self, image: ImageCreation):
        if self.pipe is None:
            raise ValueError(f"No pipeline loaded for model {self.name}")

        default_negative = self.default_neg if image.use_default_negative else ''
        default_positive = self.default_pos if image.use_default_positive else ''

        positive_prompt = image.prompt
        if image.nsfw != NsfwLevel.NOT_SPECIFIED:
            positive_prompt += f", {image.nsfw}"
        if default_positive:
            positive_prompt += f", {default_positive}"

        negative_prompt = f"{image.negative_prompt}, {default_negative}" if image.negative_prompt else default_negative

        with torch.autocast(CUDA, dtype=torch.float16):
            gen_start = time.time()
            result = self.pipe(
                positive_prompt,
                num_inference_steps=self.num_steps,
                guidance_scale=self.guidance,
                height=self.height,
                width=self.width,
                negative_prompt=negative_prompt
            )
            gen_time = time.time() - gen_start
        self.log.info(f"Generation completed for prompt: {positive_prompt} in {gen_time:.2f} seconds")
        return result, positive_prompt, negative_prompt, gen_time


class WaiAnimePonyModel(BaseDiffusionModel):
    def __init__(self):
        super().__init__(
            name=Models.PONY_REALISM,
            model_path=ANI_PONY_PATH,
            default_pos=ANI_PONY_POSITIVE_PROMPT,
            default_neg=ANI_PONY_NEGATIVE_PROMPT,
            num_steps=25,
            guidance=8
        )


class CyberRealisticPonyModel(BaseDiffusionModel):
    def __init__(self):
        super().__init__(
            name=Models.CYBER_REALISTIC_PONY,
            model_path=CYBER_PONY_PATH,
            default_pos=CYBER_PONY_POSITIVE_PROMPT,
            default_neg=CYBER_PONY_NEGATIVE_PROMPT,
            num_steps=25,
            height=1024,
            width=1024,
            guidance=7
        )


class WaiAnimeIllustriousModel(BaseDiffusionModel):
    def __init__(self):
        super().__init__(
            name=Models.ANIME_WAI_ILLUSTRIOUS,
            model_path=WAI_ILLUSTRIOUS_PATH,
            default_pos=WAI_ILLUSTRIOUS_POSITIVE_PROMPT,
            default_neg=WAI_ILLUSTRIOUS_NEGATIVE_PROMPT,
            num_steps=20,
            guidance=7
        )


class PonyRealismModel(BaseDiffusionModel):
    def __init__(self):
        super().__init__(
            name=Models.PONY_REALISM,
            model_path=PONY_REALISM_PATH,
            default_pos=PONY_REALISM_POSITIVE_PROMPT,
            default_neg=PONY_REALISM_NEGATIVE_PROMPT,
            num_steps=25,
            guidance=7
        )
