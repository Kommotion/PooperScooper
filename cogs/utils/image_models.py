import asyncio
import typing
import time
import logging
import json
import copy
import random
import os
from io import BytesIO

import discord
from typing import Literal, Optional
import functools
from enum import Enum, StrEnum

from PIL import Image

from cogs.utils import comfy_client
from cogs.utils.constants import *


WAI_ILLUSTRIOUS_PATH = r".\ComfyUI_New\models\checkpoints\waiIllustriousSDXL_v160.safetensors"
Z_IMAGE_TURBO_PATH = r".\ComfyUI_New\models\checkpoints\z-image-turbo-fp8-e4m3fn.safetensors"

Z_IMAGE_VAE = r".\ComfyUI_New\models\vae\zimage_turbo_fp8_vae.sft"

# ComfyUI API workflow JSON paths
WAI_ILLUSTRIOUS_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\WAI-ILLUSTRIOUS-SDXL_API.json"
Z_IMAGE_TURBO_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\Z_IMAGE_TURBO_FP8_API.json"
WAI_ANIMA_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\WAI-ANIMA_API.json"

WAI_ILLUSTRIOUS_POSITIVE_PROMPT = "masterpiece,best quality,amazing quality"
WAI_ILLUSTRIOUS_NEGATIVE_PROMPT = "bad quality,worst quality,worst detail,sketch,censor,"
WAI_ANIMA_POSITIVE_PROMPT = "masterpiece, best quality,score_9, score_8, score_7,"
WAI_ANIMA_NEGATIVE_PROMPT = "worst quality, low quality, score_1, score_2, score_3, artist name,blurry, jpeg artifacts, lowres,censor"

NO_DEFAULT_POSITIVE = ""
NO_DEFAULT_NEGATIVE = ""

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
    Z_IMAGE_TURBO_FP8 = "z_image_turbo_fp8"
    WAI_ANIMA = "anime_wai_anima"

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


class ComfyOutput:
    """Small wrapper to mimic diffusers output.images[0] interface."""

    def __init__(self, images: list[Image.Image]):
        self.images = images


class BaseComfyWorkflowModel:
    def __init__(self, name: Models, workflow_path: str, default_pos: str, default_neg: str, 
        comfy_host: str = comfy_client.DEFAULT_COMFY_HOST,
        comfy_port: int = comfy_client.DEFAULT_COMFY_PORT,
    ):
        self.name = name
        self.workflow_path = workflow_path
        self.default_pos = default_pos
        self.default_neg = default_neg
        self.comfy_host = comfy_host
        self.comfy_port = comfy_port
        
        self._ui_workflow_template: Optional[dict] = None   # ← new: keep original UI format
        self._api_workflow_template: Optional[dict] = None   # optional cache, but we won't use it
        self.log = log

    async def load_pipeline(self):
            if self._ui_workflow_template is not None:
                return

            def _load():
                with open(self.workflow_path, "r", encoding="utf-8") as f:
                    return json.load(f)

            raw_ui = await asyncio.to_thread(_load)
            self._ui_workflow_template = raw_ui
            
            self.log.info(f"Loaded UI workflow for {self.name} from {self.workflow_path}")

    def unload_pipeline(self):
        """Nothing heavy to unload for ComfyUI workflows; keep API shape for cog compatibility."""
        self._ui_workflow_template = None

    def _compose_prompts(self, image: ImageCreation) -> tuple[str, str]:
        default_negative = self.default_neg if image.use_default_negative else ""
        default_positive = self.default_pos if image.use_default_positive else ""

        positive_prompt = image.prompt
        if image.nsfw != NsfwLevel.NOT_SPECIFIED:
            positive_prompt += f", {image.nsfw}"
        if default_positive:
            positive_prompt = f"{positive_prompt}, {default_positive}" if positive_prompt else default_positive

        negative_prompt = image.negative_prompt or ""
        if default_negative:
            if negative_prompt:
                negative_prompt = f"{negative_prompt}, {default_negative}"
            else:
                negative_prompt = default_negative

        return positive_prompt, negative_prompt

    def _apply_prompts_to_workflow(
            self,
            workflow: dict,
            positive_prompt: str,
            negative_prompt: str,
            image: ImageCreation,
        ) -> dict:
            # Make a copy
            workflow = copy.deepcopy(workflow)

            # Patch positive (node 6 in your example)
            if "6" in workflow and workflow["6"].get("class_type") == "CLIPTextEncode":
                workflow["6"]["inputs"]["text"] = positive_prompt

            # Patch negative (node 7 in your example)
            if "7" in workflow and workflow["7"].get("class_type") == "CLIPTextEncode":
                workflow["7"]["inputs"]["text"] = negative_prompt

            # Randomize the seed
            if "3" in workflow and workflow["3"].get("class_type") == "KSampler":
                workflow["3"]["inputs"]["seed"] = random.getrandbits(64)

            return workflow

    @to_thread
    def generate(self, image: ImageCreation):
        positive_prompt, negative_prompt = self._compose_prompts(image)

        # Get modified API prompt
        workflow_api = self._apply_prompts_to_workflow(
            workflow=self._ui_workflow_template,
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            image=image,
        )

        gen_start = time.time()
        log.debug(f"Sending prompt to ComfyUI: {json.dumps(workflow_api, indent=1)}")
        result = comfy_client.post_prompt(
            prompt=workflow_api,
            host=self.comfy_host,
            port=self.comfy_port
        )
        prompt_id = result.get("prompt_id")

        image_infos = comfy_client.wait_for_result(prompt_id, host=self.comfy_host, port=self.comfy_port)
        images: list[Image.Image] = []
        for info in image_infos:
            data = comfy_client.get_image_bytes(
                filename=info["filename"],
                subfolder=info.get("subfolder", ""),
                image_type=info.get("type", "output"),
                host=self.comfy_host,
                port=self.comfy_port,
            )
            img = Image.open(BytesIO(data))
            images.append(img.convert("RGB"))

        gen_time = time.time() - gen_start
        self.log.info(f"ComfyUI generation completed for prompt: {positive_prompt} in {gen_time:.2f} seconds")

        output = ComfyOutput(images=images)
        return output, positive_prompt, negative_prompt, gen_time


class ComfyWaiIllustriousModel(BaseComfyWorkflowModel):
    """ComfyUI workflow-backed WAI Illustrious SDXL model."""

    def __init__(self):
        super().__init__(
            name=Models.ANIME_WAI_ILLUSTRIOUS,
            workflow_path=WAI_ILLUSTRIOUS_WORKFLOW_PATH,
            default_pos=WAI_ILLUSTRIOUS_POSITIVE_PROMPT,
            default_neg=WAI_ILLUSTRIOUS_NEGATIVE_PROMPT,
        )


class ComfyZImageTurboModel(BaseComfyWorkflowModel):
    """ComfyUI workflow-backed Z Image Turbo FP8 model."""

    def __init__(self):
        super().__init__(
            name=Models.Z_IMAGE_TURBO_FP8,
            workflow_path=Z_IMAGE_TURBO_WORKFLOW_PATH,
            default_pos=NO_DEFAULT_POSITIVE,
            default_neg=NO_DEFAULT_NEGATIVE,
        )


class ComfyWaiAnimaModel(BaseComfyWorkflowModel):
    """ComfyUI workflow-backed WAI ANIMA model."""

    def __init__(self):
        super().__init__(
            name=Models.WAI_ANIMA,
            workflow_path=WAI_ANIMA_WORKFLOW_PATH,
            default_pos=WAI_ANIMA_POSITIVE_PROMPT,
            default_neg=WAI_ANIMA_NEGATIVE_PROMPT,
        )
