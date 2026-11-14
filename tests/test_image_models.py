"""Image-model registry: Anima v5, FLUX 3 Klein INT8, Qwen-Image 2.1, and LUSTIFY v10. No ComfyUI."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from cogs.imagediffusion import IMAGE_QUEUE_MAXSIZE, ImageDiffusion, ImageGenPrefView
from cogs.utils.image_models import (
    KLEIN_WORKFLOW_PATH,
    LUSTIFY_KREA2_WORKFLOW_PATH,
    NO_DEFAULT_NEGATIVE,
    NO_DEFAULT_POSITIVE,
    ONE_OBSESSION_ANIMA_NEGATIVE,
    ONE_OBSESSION_ANIMA_POSITIVE,
    ONE_OBSESSION_ANIMA_WORKFLOW_PATH,
    QWEN_IMAGE_21_WORKFLOW_PATH,
    BaseComfyWorkflowModel,
    ComfyFlux3KleinInt8Model,
    ComfyLustifyKrea2Model,
    ComfyOneObsessionAnimaModel,
    ComfyQwenImage21Model,
    ImageCreation,
    Models,
)

_EXISTING_MODEL_VALUES = (
    "anime_wai_illustrious",
    "anima_aesthetic",
    "one_obsession_illustrious",
    "one_obsession_anima",
    "krea2_turbo",
    "redcraft",
    "krea2_identity_edit",
    "qwen_image_21",
    "lustify",
)

_GENERATE_LABELS = (
    "[Anime][NSFW] WAI Illustrious",
    "[Anime][NSFW] Anima Aesthetic",
    "[Anime][NSFW] One Obsession Illustrious",
    "[Anime][NSFW] One Obsession Anima",
    "[Realism][SFW] Krea 2 Turbo",
    "[Realism][SFW] FLUX 3 Klein INT8",
    "[Realism][SFW] RedCraft",
    "[Realism][SFW] Qwen-Image 2.1",
    "[Realism][NSFW] LUSTIFY",
)

_GENERATE_VALUES = (
    Models.ANIME_WAI_ILLUSTRIOUS,
    Models.ANIMA_AESTHETIC,
    Models.ONE_OBSESSION_ILLUSTRIOUS,
    Models.ONE_OBSESSION_ANIMA,
    Models.KREA2_TURBO,
    Models.FLUX3_KLEIN_INT8,
    Models.REDCRAFT,
    Models.QWEN_IMAGE_21,
    Models.LUSTIFY,
)


def test_models_enum_keeps_existing_values_and_adds_klein():
    assert Models.FLUX3_KLEIN_INT8 == "flux3_klein_int8"
    assert Models.FLUX3_KLEIN_INT8.value == "flux3_klein_int8"
    assert Models("flux3_klein_int8") is Models.FLUX3_KLEIN_INT8
    assert Models("FLUX3_KLEIN_INT8") is Models.FLUX3_KLEIN_INT8

    assert Models.QWEN_IMAGE_21 == "qwen_image_21"
    assert Models.QWEN_IMAGE_21.value == "qwen_image_21"
    assert Models("qwen_image_21") is Models.QWEN_IMAGE_21
    assert Models("QWEN_IMAGE_21") is Models.QWEN_IMAGE_21

    assert Models.LUSTIFY == "lustify"
    assert Models.LUSTIFY.value == "lustify"
    assert Models("lustify") is Models.LUSTIFY
    assert Models("LUSTIFY") is Models.LUSTIFY

    assert Models("one_obsession_anima") is Models.ONE_OBSESSION_ANIMA
    assert Models("ONE_OBSESSION_ANIMA") is Models.ONE_OBSESSION_ANIMA
    assert Models("anime_wai_anima") is Models.ANIMA_AESTHETIC
    assert Models("wai_anima") is Models.ANIMA_AESTHETIC

    for value in _EXISTING_MODEL_VALUES:
        member = Models(value)
        assert member.value == value
        assert Models(member.name) is member

    with pytest.raises(ValueError):
        Models("not_a_real_model")


def test_retired_z_image_turbo_maps_to_wai_illustrious():
    assert not hasattr(Models, "Z_IMAGE_TURBO_FP8")
    assert "Z_IMAGE_TURBO_FP8" not in Models.__members__
    with pytest.raises(KeyError):
        Models["Z_IMAGE_TURBO_FP8"]

    assert Models("z_image_turbo_fp8") is Models.ANIME_WAI_ILLUSTRIOUS
    assert Models("Z_IMAGE_TURBO_FP8") is Models.ANIME_WAI_ILLUSTRIOUS
    assert Models("Z_Image_Turbo_FP8") is Models.ANIME_WAI_ILLUSTRIOUS


def test_one_obsession_anima_docs_say_v5_and_tags_stay_untagged():
    doc = ComfyOneObsessionAnimaModel.__doc__ or ""
    assert "v2.0" not in doc
    assert "v5.0" in doc
    assert "One Obsession Anima v5 turbo" in doc

    model = ComfyOneObsessionAnimaModel()
    assert model.name is Models.ONE_OBSESSION_ANIMA
    assert model.workflow_path == ONE_OBSESSION_ANIMA_WORKFLOW_PATH
    assert model.default_pos == ONE_OBSESSION_ANIMA_POSITIVE
    assert model.default_neg == ONE_OBSESSION_ANIMA_NEGATIVE
    assert "v2" not in model.default_pos and "v5" not in model.default_pos
    assert "v2" not in model.default_neg and "v5" not in model.default_neg


def test_klein_model_uses_empty_defaults_and_the_klein_workflow():
    model = ComfyFlux3KleinInt8Model()
    assert model.name is Models.FLUX3_KLEIN_INT8
    assert model.workflow_path == KLEIN_WORKFLOW_PATH
    assert model.workflow_path.endswith("KLEIN_API.json")
    assert model.default_pos == NO_DEFAULT_POSITIVE == ""
    assert model.default_neg == NO_DEFAULT_NEGATIVE == ""
    assert "_apply_prompts_to_workflow" not in ComfyFlux3KleinInt8Model.__dict__
    assert (
        ComfyFlux3KleinInt8Model._apply_prompts_to_workflow
        is BaseComfyWorkflowModel._apply_prompts_to_workflow
    )

    doc = ComfyFlux3KleinInt8Model.__doc__ or ""
    assert "distilled" in doc.lower()
    assert "INT8" in doc
    assert "16 steps" in doc
    assert "cfg 1" in doc
    assert "upsampler" in doc.lower()
    assert "8 GB" in doc

    image = ImageCreation(
        "a mountain path at dusk",
        Models.FLUX3_KLEIN_INT8,
        use_default_positive=True,
        use_default_negative=True,
    )
    positive, negative = model._compose_prompts(image)
    assert positive == "a mountain path at dusk"
    assert negative == ""

    workflow = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old positive"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "old negative"}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 16, "cfg": 1}},
    }
    patched = model._apply_prompts_to_workflow(workflow, positive, negative, image)
    assert patched["6"]["inputs"]["text"] == positive
    assert patched["7"]["inputs"]["text"] == negative
    assert isinstance(patched["3"]["inputs"]["seed"], int)
    assert workflow["6"]["inputs"]["text"] == "old positive"


def test_klein_workflow_file_is_the_distilled_int8_graph():
    path = (
        Path(__file__).resolve().parents[1]
        / "ComfyUI_New"
        / "user"
        / "default"
        / "workflows"
        / "KLEIN_API.json"
    )
    graph = json.loads(path.read_text(encoding="utf-8"))
    assert graph["6"]["class_type"] == "CLIPTextEncode"
    assert graph["7"]["class_type"] == "CLIPTextEncode"
    assert graph["3"]["class_type"] == "KSampler"
    assert graph["3"]["inputs"]["steps"] == 16
    assert float(graph["3"]["inputs"]["cfg"]) == 1.0
    assert graph["10"]["inputs"]["unet_name"] == "flux3-image-klein-int8-convrot.safetensors"
    assert graph["11"]["inputs"]["clip_name"] == "qwen3vl_4b_int8_convrot.safetensors"
    assert graph["11"]["inputs"]["type"] == "flux3"
    dumped = json.dumps(graph)
    assert "TextGenerate" not in dumped
    assert "upsampler" not in dumped.lower()


def test_qwen_image_21_model_uses_empty_defaults_and_patches_qwen_encoder(monkeypatch):
    monkeypatch.setattr("cogs.utils.image_models.random.getrandbits", lambda bits: 4242)
    model = ComfyQwenImage21Model()
    assert model.name is Models.QWEN_IMAGE_21
    assert model.workflow_path == QWEN_IMAGE_21_WORKFLOW_PATH
    assert model.workflow_path.endswith("QWEN_IMAGE_21_TURBO_INT8_API.json")
    assert model.default_pos == NO_DEFAULT_POSITIVE == ""
    assert model.default_neg == NO_DEFAULT_NEGATIVE == ""
    assert "_apply_prompts_to_workflow" in ComfyQwenImage21Model.__dict__
    assert (
        ComfyQwenImage21Model._apply_prompts_to_workflow
        is not BaseComfyWorkflowModel._apply_prompts_to_workflow
    )

    doc = ComfyQwenImage21Model.__doc__ or ""
    assert "turbo" in doc.lower()
    assert "INT8" in doc
    assert "8 steps" in doc
    assert "cfg 1" in doc
    assert "8 GB" in doc

    image = ImageCreation(
        "a red fox in fresh snow",
        Models.QWEN_IMAGE_21,
        use_default_positive=True,
        use_default_negative=True,
    )
    positive, negative = model._compose_prompts(image)
    assert positive == "a red fox in fresh snow"
    assert negative == ""

    workflow = {
        "20": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {"prompt": "old positive", "negative_prompt": "old negative"},
        },
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "cfg": 1}},
    }
    patched = model._apply_prompts_to_workflow(workflow, positive, negative, image)
    assert patched["20"]["inputs"]["prompt"] == positive
    assert patched["20"]["inputs"]["negative_prompt"] == negative
    assert patched["3"]["inputs"]["seed"] == 4242
    assert workflow["20"]["inputs"]["prompt"] == "old positive"
    assert workflow["20"]["inputs"]["negative_prompt"] == "old negative"
    assert workflow["3"]["inputs"]["seed"] == 1


def test_qwen_workflow_file_is_the_turbo_int8_graph():
    path = (
        Path(__file__).resolve().parents[1]
        / "ComfyUI_New"
        / "user"
        / "default"
        / "workflows"
        / "QWEN_IMAGE_21_TURBO_INT8_API.json"
    )
    if not path.is_file():
        pytest.skip("QWEN_IMAGE_21_TURBO_INT8_API.json is not in this checkout")

    graph = json.loads(path.read_text(encoding="utf-8"))
    nodes = [node for node in graph.values() if isinstance(node, dict)]
    unet = next(node for node in nodes if node.get("class_type") == "UNETLoader")
    clip = next(node for node in nodes if node.get("class_type") == "CLIPLoader")
    vae = next(node for node in nodes if node.get("class_type") == "VAELoader")
    sampler = next(node for node in nodes if node.get("class_type") == "KSampler")
    assert unet["inputs"]["unet_name"] == "qwen_image_2.1_turbo_int8_convrot.safetensors"
    assert clip["inputs"]["clip_name"] == "qwen3vl_8b_w4a8.safetensors"
    assert clip["inputs"]["type"] == "qwen_image"
    assert vae["inputs"]["vae_name"] == "qwen_image_2.1_vae_bf16.safetensors"
    assert sampler["inputs"]["steps"] == 8
    assert float(sampler["inputs"]["cfg"]) == 1.0
    assert any(node.get("class_type") == "TextEncodeQwenImage21" for node in nodes)
    dumped = json.dumps(graph)
    assert "TextGenerate" not in dumped
    assert "upsampler" not in dumped.lower()


def test_lustify_model_uses_empty_defaults_and_patches_clip_and_seed(monkeypatch):
    monkeypatch.setattr("cogs.utils.image_models.random.getrandbits", lambda bits: 4242)
    model = ComfyLustifyKrea2Model()
    assert model.name is Models.LUSTIFY
    assert model.workflow_path == LUSTIFY_KREA2_WORKFLOW_PATH
    assert model.workflow_path.endswith("LUSTIFY_KREA2_API.json")
    assert model.default_pos == NO_DEFAULT_POSITIVE == ""
    assert model.default_neg == NO_DEFAULT_NEGATIVE == ""
    assert "_apply_prompts_to_workflow" not in ComfyLustifyKrea2Model.__dict__
    assert (
        ComfyLustifyKrea2Model._apply_prompts_to_workflow
        is BaseComfyWorkflowModel._apply_prompts_to_workflow
    )

    doc = ComfyLustifyKrea2Model.__doc__ or ""
    assert "LUSTIFY" in doc
    assert "Turbo" in doc
    assert "INT8" in doc
    assert "8 steps" in doc
    assert "8 GB" in doc

    image = ImageCreation(
        "a portrait in window light",
        Models.LUSTIFY,
        use_default_positive=True,
        use_default_negative=True,
    )
    positive, negative = model._compose_prompts(image)
    assert positive == "a portrait in window light"
    assert negative == ""

    workflow = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old positive"}},
        "7": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["6", 0]},
        },
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 8, "cfg": 1}},
    }
    patched = model._apply_prompts_to_workflow(workflow, positive, negative, image)
    assert patched["6"]["inputs"]["text"] == positive
    assert patched["7"]["class_type"] == "ConditioningZeroOut"
    assert "text" not in patched["7"]["inputs"]
    assert patched["7"]["inputs"]["conditioning"] == ["6", 0]
    assert patched["3"]["inputs"]["seed"] == 4242
    assert workflow["6"]["inputs"]["text"] == "old positive"
    assert workflow["7"]["class_type"] == "ConditioningZeroOut"
    assert workflow["3"]["inputs"]["seed"] == 1


def test_lustify_workflow_file_is_the_krea2_turbo_int8_graph():
    path = (
        Path(__file__).resolve().parents[1]
        / "ComfyUI_New"
        / "user"
        / "default"
        / "workflows"
        / "LUSTIFY_KREA2_API.json"
    )
    if not path.is_file():
        pytest.skip("LUSTIFY_KREA2_API.json is not in this checkout")

    graph = json.loads(path.read_text(encoding="utf-8"))
    assert graph["6"]["class_type"] == "CLIPTextEncode"
    assert graph["7"]["class_type"] == "ConditioningZeroOut"
    assert graph["10"]["class_type"] == "UNETLoader"
    assert graph["10"]["inputs"]["unet_name"] == "lustify-v10-krea-turbo-int8_convrot.safetensors"
    assert graph["11"]["class_type"] == "CLIPLoader"
    assert graph["11"]["inputs"]["clip_name"] == "qwen3vl_4b_fp8_scaled.safetensors"
    assert graph["11"]["inputs"]["type"] == "krea2"
    assert graph["12"]["class_type"] == "VAELoader"
    assert graph["12"]["inputs"]["vae_name"] == "qwen_image_vae.safetensors"
    assert graph["3"]["class_type"] == "KSampler"
    assert graph["3"]["inputs"]["steps"] == 8
    assert float(graph["3"]["inputs"]["cfg"]) == 1.0
    dumped = json.dumps(graph)
    assert "TextGenerate" not in dumped


def test_generate_choices_and_pref_view_list_anime_then_realism():
    generate = ImageDiffusion.imagegen_group.get_command("generate")
    choices = generate.get_parameter("model").choices
    choice_names = [choice.name for choice in choices]
    choice_values = [choice.value for choice in choices]
    assert choice_names == list(_GENERATE_LABELS)
    assert choice_values == [model.value for model in _GENERATE_VALUES]
    assert choice_names[choice_names.index("[Realism][SFW] Krea 2 Turbo") + 1] == (
        "[Realism][SFW] FLUX 3 Klein INT8"
    )
    assert choice_names[choice_names.index("[Realism][SFW] FLUX 3 Klein INT8") + 1] == (
        "[Realism][SFW] RedCraft"
    )
    qwen_index = choice_names.index("[Realism][SFW] Qwen-Image 2.1")
    assert choice_names[qwen_index + 1] == "[Realism][NSFW] LUSTIFY"
    assert choice_names[-1] == "[Realism][NSFW] LUSTIFY"
    assert choice_values[-1] == Models.LUSTIFY.value == "lustify"
    klein_choice = next(
        choice for choice in choices if choice.name == "[Realism][SFW] FLUX 3 Klein INT8"
    )
    assert klein_choice.value == Models.FLUX3_KLEIN_INT8.value
    anima_choice = next(
        choice for choice in choices if choice.value == Models.ONE_OBSESSION_ANIMA.value
    )
    assert anima_choice.name == "[Anime][NSFW] One Obsession Anima"
    assert "v2.0" not in anima_choice.name
    assert all(choice.value != Models.KREA2_IDENTITY_EDIT.value for choice in choices)

    view = ImageGenPrefView(None)
    options = view.model_select.options
    labels = [option.label for option in options]
    assert labels == list(_GENERATE_LABELS)
    assert [option.value for option in options] == list(_GENERATE_VALUES)
    last_anime = labels.index("[Anime][NSFW] One Obsession Anima")
    assert labels[last_anime + 1:last_anime + 6] == [
        "[Realism][SFW] Krea 2 Turbo",
        "[Realism][SFW] FLUX 3 Klein INT8",
        "[Realism][SFW] RedCraft",
        "[Realism][SFW] Qwen-Image 2.1",
        "[Realism][NSFW] LUSTIFY",
    ]
    klein = next(option for option in options if option.label == "[Realism][SFW] FLUX 3 Klein INT8")
    assert klein.value == Models.FLUX3_KLEIN_INT8
    assert klein.description == "FLUX 3 Klein distilled INT8 (16 steps)"
    anima = next(option for option in options if option.value == Models.ONE_OBSESSION_ANIMA)
    assert anima.label == "[Anime][NSFW] One Obsession Anima"
    assert anima.description == "One Obsession Anima v5.0"
    assert "v2.0" not in anima.description
    qwen = next(option for option in options if option.value == Models.QWEN_IMAGE_21)
    assert qwen.description == "Qwen-Image 2.1 turbo INT8 (8 steps)"
    lustify = next(option for option in options if option.value == Models.LUSTIFY)
    assert lustify.label == "[Realism][NSFW] LUSTIFY"
    assert lustify.description == "LUSTIFY v10 Krea 2 Turbo INT8 (8 steps)"
    assert labels[labels.index("[Realism][SFW] Qwen-Image 2.1") + 1] == lustify.label
    assert labels[-1] == lustify.label
    assert all(option.value != Models.KREA2_IDENTITY_EDIT for option in options)

    stale = ImageGenPrefView(None, default_model=Models("z_image_turbo_fp8"))
    assert stale.model is Models.ANIME_WAI_ILLUSTRIOUS
    marked = [option for option in stale.model_select.options if option.default]
    assert len(marked) == 1
    assert marked[0].label == "[Anime][NSFW] WAI Illustrious"
    assert marked[0].value == Models.ANIME_WAI_ILLUSTRIOUS

    edit_only = ImageGenPrefView(None, default_model=Models.KREA2_IDENTITY_EDIT)
    assert edit_only.model is Models.ANIME_WAI_ILLUSTRIOUS
    edit_marked = [option for option in edit_only.model_select.options if option.default]
    assert len(edit_marked) == 1
    assert edit_marked[0].value == Models.ANIME_WAI_ILLUSTRIOUS


def test_boot_registry_orders_realism_then_identity_edit_outside_krea_enhance():
    init_source = inspect.getsource(ImageDiffusion.__init__)
    boot_source, models_source = init_source.split("self.models =", 1)
    assert "Z_IMAGE" not in init_source
    assert "ComfyZImageTurboModel" not in init_source
    assert "ComfyQwenImage21Model()" in models_source
    assert "ComfyLustifyKrea2Model()" in models_source
    assert models_source.index("ComfyQwenImage21Model()") < models_source.index(
        "ComfyLustifyKrea2Model()"
    )
    assert models_source.index("ComfyLustifyKrea2Model()") < models_source.index(
        "ComfyKrea2IdentityEditModel()"
    )
    for anime in (
        "Models.ANIME_WAI_ILLUSTRIOUS",
        "Models.ANIMA_AESTHETIC",
        "Models.ONE_OBSESSION_ILLUSTRIOUS",
        "Models.ONE_OBSESSION_ANIMA",
    ):
        assert boot_source.index(anime) < boot_source.index("Models.KREA2_TURBO")
        assert models_source.index(anime) < models_source.index("Models.KREA2_TURBO")
    realism_order = (
        "Models.KREA2_TURBO",
        "Models.FLUX3_KLEIN_INT8",
        "Models.REDCRAFT",
        "Models.QWEN_IMAGE_21",
        "Models.LUSTIFY",
        "Models.KREA2_IDENTITY_EDIT",
    )
    boot_indexes = [boot_source.index(name) for name in realism_order]
    model_indexes = [models_source.index(name) for name in realism_order]
    assert boot_indexes == sorted(boot_indexes)
    assert model_indexes == sorted(model_indexes)

    schedule_source = inspect.getsource(ImageDiffusion.schedule_generation)
    family_start = schedule_source.index("krea_family")
    family = schedule_source[family_start:schedule_source.index("}", family_start)]
    assert "KREA2_TURBO" in family
    assert "REDCRAFT" in family
    assert "FLUX3_KLEIN_INT8" not in family
    assert "QWEN_IMAGE_21" not in family
    assert "LUSTIFY" not in family


def test_image_queue_uses_image_queue_maxsize():
    assert IMAGE_QUEUE_MAXSIZE == 30
    init_source = inspect.getsource(ImageDiffusion.__init__)
    assert "asyncio.Queue(maxsize=IMAGE_QUEUE_MAXSIZE)" in init_source
