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
ANIMA_AESTHETIC_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\ANIMA_AESTHETIC_API.json"
ONE_OBSESSION_ILLUSTRIOUS_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\ONE_OBSESSION_ILLUSTRIOUS_API.json"
ONE_OBSESSION_ANIMA_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\ONE_OBSESSION_ANIMA_API.json"
KREA2_TURBO_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\KREA2_TURBO_INT8_API.json"
KREA2_TURBO_ENHANCE_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\KREA2_TURBO_INT8_API_ENHANCE.json"
REDCRAFT_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\REDCRAFT_KREA2_API.json"
REDCRAFT_ENHANCE_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\REDCRAFT_KREA2_API_ENHANCE.json"

# System prompt used by the official Krea UI "prompt enhance" path (shortened slightly for Discord UX).
KREA2_LLM_ENHANCE_SYSTEM_PROMPT = (
    "You are an expert prompt engineer for text-to-image models. Your task is to expand the user's prompt "
    "into a highly effective image-generation prompt.\n\n"
    "Think step by step about the request before writing the answer:\n"
    "- What is the subject and mood?\n"
    "- What visual styles, mediums, and lighting options would fit?\n"
    "- What composition, framing, and grounded details will help the text-to-image model?\n\n"
    "Then output a single expanded prompt paragraph.\n\n"
    "Follow these rules strictly:\n"
    "1. Preserve all original subjects, actions, colors, and spatial relationships. Do not invent new major objects.\n"
    "2. Write a prompt a text-to-image model can parse cleanly. One cohesive paragraph. No bullets, JSON, or markdown.\n"
    "3. Do not emit planning tags or wrappers in the answer body.\n"
    "4. If the user requests visible text, specify exact words in quotes.\n"
    "5. Do not invent highly specific clothing, colors, or scene details unless the input supports them.\n"
    "6. If the user's prompt is already detailed, lightly polish rather than heavily expanding.\n"
    "7. When the user requests a medium (photo, illustration, anime, etc.), honor it.\n\n"
    "User's Input:\n\n"
)

WAI_ILLUSTRIOUS_POSITIVE_PROMPT = "masterpiece,best quality,amazing quality"
WAI_ILLUSTRIOUS_NEGATIVE_PROMPT = "bad quality,worst quality,worst detail,sketch,censor,"
# Base Anima Aesthetic: official guide says avoid score_* (can push into slop).
ANIMA_AESTHETIC_POSITIVE = "masterpiece, best quality,"
ANIMA_AESTHETIC_NEGATIVE = "worst quality, low quality, blurry, jpeg artifacts, lowres, censor, chromatic aberration"
# One Obsession Anima creator recommended tags.
ONE_OBSESSION_ANIMA_POSITIVE = "masterpiece, best quality, score_9, score_8, score_7, absurdres, newest, very aesthetic, amazing quality, highres,"
ONE_OBSESSION_ANIMA_NEGATIVE = "worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, lowres, censor,"
# One Obsession Illustrious (SDXL).
ONE_OBSESSION_ILLUSTRIOUS_POSITIVE = "masterpiece, best quality, amazing quality,"
ONE_OBSESSION_ILLUSTRIOUS_NEGATIVE = "worst quality, bad hands, bad quality, bad anatomy, jpeg artifacts, signature, watermark,"

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
    ANIMA_AESTHETIC = "anima_aesthetic"
    ONE_OBSESSION_ILLUSTRIOUS = "one_obsession_illustrious"
    ONE_OBSESSION_ANIMA = "one_obsession_anima"
    KREA2_TURBO = "krea2_turbo"
    REDCRAFT = "redcraft"

    @classmethod
    def _missing_(cls, value):
        # Check against name (case-insensitive)
        for member in cls:
            if member.name.lower() == value.lower():
                return member
        # Back-compat: old WAI ANIMA enum value
        if str(value).lower() in {"anime_wai_anima", "wai_anima"}:
            return cls.ANIMA_AESTHETIC
        raise ValueError(f"{value} is not a valid {cls.__name__}")


class ImageCreation:
    def __init__(self, prompt: Models, model, interaction=None, negative_prompt='', use_default_negative=True,
                 use_default_positive=True, nsfw: NsfwLevel=NsfwLevel.NOT_SPECIFIED,
                 llm_prompt_enhance: bool = False):
        self.interaction: discord.Interaction = interaction
        self.negative_prompt = negative_prompt
        self.use_default_negative = use_default_negative
        self.use_default_positive = use_default_positive
        self.prompt: str = prompt
        self.model: Models = model
        self.nsfw: NsfwLevel = nsfw
        # When True and the model supports it (Krea-family), run LLM prompt expansion first.
        self.llm_prompt_enhance: bool = bool(llm_prompt_enhance)


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


class ComfyAnimaAestheticModel(BaseComfyWorkflowModel):
    """Base Anima aesthetic v1.1 — diffusion-only + Qwen3 0.6B CLIP + Qwen-Image VAE."""

    def __init__(self):
        super().__init__(
            name=Models.ANIMA_AESTHETIC,
            workflow_path=ANIMA_AESTHETIC_WORKFLOW_PATH,
            default_pos=ANIMA_AESTHETIC_POSITIVE,
            default_neg=ANIMA_AESTHETIC_NEGATIVE,
        )


class ComfyOneObsessionIllustriousModel(BaseComfyWorkflowModel):
    """One Obsession v23 (Illustrious / SDXL full checkpoint: model+CLIP+VAE baked)."""

    def __init__(self):
        super().__init__(
            name=Models.ONE_OBSESSION_ILLUSTRIOUS,
            workflow_path=ONE_OBSESSION_ILLUSTRIOUS_WORKFLOW_PATH,
            default_pos=ONE_OBSESSION_ILLUSTRIOUS_POSITIVE,
            default_neg=ONE_OBSESSION_ILLUSTRIOUS_NEGATIVE,
        )


class ComfyOneObsessionAnimaModel(BaseComfyWorkflowModel):
    """One Obsession Anima v2.0 — diffusion-only Anima + Qwen3 0.6B CLIP + Qwen-Image VAE."""

    def __init__(self):
        super().__init__(
            name=Models.ONE_OBSESSION_ANIMA,
            workflow_path=ONE_OBSESSION_ANIMA_WORKFLOW_PATH,
            default_pos=ONE_OBSESSION_ANIMA_POSITIVE,
            default_neg=ONE_OBSESSION_ANIMA_NEGATIVE,
        )


class ComfyKrea2TurboModel(BaseComfyWorkflowModel):
    """Krea 2 Turbo INT8 (native Comfy quant). CFG=1 distilled; no real negative.

    Default path is fast (no LLM). Optional llm_prompt_enhance uses TextGenerate on the Krea CLIP.
    """

    def __init__(self):
        super().__init__(
            name=Models.KREA2_TURBO,
            workflow_path=KREA2_TURBO_WORKFLOW_PATH,
            default_pos=NO_DEFAULT_POSITIVE,
            default_neg=NO_DEFAULT_NEGATIVE,
        )
        self.workflow_enhance_path = KREA2_TURBO_ENHANCE_WORKFLOW_PATH
        self._enhance_workflow_template: Optional[dict] = None

    async def load_pipeline(self):
        await super().load_pipeline()
        if self._enhance_workflow_template is not None:
            return

        def _load():
            with open(self.workflow_enhance_path, "r", encoding="utf-8") as f:
                return json.load(f)

        self._enhance_workflow_template = await asyncio.to_thread(_load)
        self.log.info(
            f"Loaded enhance workflow for {self.name} from {self.workflow_enhance_path}"
        )

    def unload_pipeline(self):
        super().unload_pipeline()
        self._enhance_workflow_template = None

    def _apply_prompts_to_workflow(
            self,
            workflow: dict,
            positive_prompt: str,
            negative_prompt: str,
            image: ImageCreation,
        ) -> dict:
        workflow = copy.deepcopy(workflow)
        seed = random.getrandbits(64)

        if image.llm_prompt_enhance:
            # Node 16 = TextGenerate; CLIP encode reads its string output via link.
            if "16" in workflow and workflow["16"].get("class_type") == "TextGenerate":
                enhance_prompt = f"{KREA2_LLM_ENHANCE_SYSTEM_PROMPT}{positive_prompt}"
                workflow["16"]["inputs"]["prompt"] = enhance_prompt
                sm = workflow["16"]["inputs"].get("sampling_mode")
                if isinstance(sm, dict):
                    sm["seed"] = seed
            # Do not overwrite node 6 text (it is linked to TextGenerate output).
        else:
            if "6" in workflow and workflow["6"].get("class_type") == "CLIPTextEncode":
                # Only set plain string when not linked to another node.
                text_in = workflow["6"]["inputs"].get("text")
                if not isinstance(text_in, list):
                    workflow["6"]["inputs"]["text"] = positive_prompt

        if "3" in workflow and workflow["3"].get("class_type") == "KSampler":
            workflow["3"]["inputs"]["seed"] = seed

        return workflow

    @to_thread
    def generate(self, image: ImageCreation):
        positive_prompt, negative_prompt = self._compose_prompts(image)

        if image.llm_prompt_enhance:
            if self._enhance_workflow_template is None:
                raise RuntimeError("Krea enhance workflow not loaded; call load_pipeline first")
            template = self._enhance_workflow_template
            self.log.info("Krea2 generation with LLM prompt enhance enabled")
        else:
            if self._ui_workflow_template is None:
                raise RuntimeError("Krea workflow not loaded; call load_pipeline first")
            template = self._ui_workflow_template

        workflow_api = self._apply_prompts_to_workflow(
            workflow=template,
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            image=image,
        )

        gen_start = time.time()
        log.debug(f"Sending prompt to ComfyUI: {json.dumps(workflow_api, indent=1)}")
        result = comfy_client.post_prompt(
            prompt=workflow_api,
            host=self.comfy_host,
            port=self.comfy_port,
        )
        prompt_id = result.get("prompt_id")

        # LLM TextGenerate can take several minutes; sampling is shorter.
        wait_timeout = 1800.0 if image.llm_prompt_enhance else 600.0
        image_infos = comfy_client.wait_for_result(
            prompt_id,
            host=self.comfy_host,
            port=self.comfy_port,
            timeout_sec=wait_timeout,
        )
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
        enhance_note = " (llm_prompt_enhance)" if image.llm_prompt_enhance else ""
        self.log.info(
            f"ComfyUI generation completed for prompt: {positive_prompt}{enhance_note} "
            f"in {gen_time:.2f} seconds"
        )

        return ComfyOutput(images), positive_prompt, negative_prompt, gen_time


class ComfyRedCraftModel(ComfyKrea2TurboModel):
    """RedCraft 2.3 INT8/INT4/FP8 Krea2 fine-tune (same graph as Krea 2 Turbo).

    CFG=1 distilled; optional llm_prompt_enhance via TextGenerate.
    Weights: redcraft23INT8INT4FP8_30Krea2.safetensors in diffusion_models.
    """

    def __init__(self):
        BaseComfyWorkflowModel.__init__(
            self,
            name=Models.REDCRAFT,
            workflow_path=REDCRAFT_WORKFLOW_PATH,
            default_pos=NO_DEFAULT_POSITIVE,
            default_neg=NO_DEFAULT_NEGATIVE,
        )
        self.workflow_enhance_path = REDCRAFT_ENHANCE_WORKFLOW_PATH
        self._enhance_workflow_template: Optional[dict] = None
