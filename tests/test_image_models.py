"""Image-model registry: Anima v5 display text and FLUX 3 Klein INT8. No ComfyUI."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from cogs.imagediffusion import ImageDiffusion, ImageGenPrefView
from cogs.utils.image_models import (
    KLEIN_WORKFLOW_PATH,
    NO_DEFAULT_NEGATIVE,
    NO_DEFAULT_POSITIVE,
    ONE_OBSESSION_ANIMA_NEGATIVE,
    ONE_OBSESSION_ANIMA_POSITIVE,
    ONE_OBSESSION_ANIMA_WORKFLOW_PATH,
    BaseComfyWorkflowModel,
    ComfyFlux3KleinInt8Model,
    ComfyOneObsessionAnimaModel,
    ImageCreation,
    Models,
)

_EXISTING_MODEL_VALUES = (
    "anime_wai_illustrious",
    "z_image_turbo_fp8",
    "anima_aesthetic",
    "one_obsession_illustrious",
    "one_obsession_anima",
    "krea2_turbo",
    "redcraft",
    "krea2_identity_edit",
)


def test_models_enum_keeps_existing_values_and_adds_klein():
    assert Models.FLUX3_KLEIN_INT8 == "flux3_klein_int8"
    assert Models.FLUX3_KLEIN_INT8.value == "flux3_klein_int8"
    assert Models("flux3_klein_int8") is Models.FLUX3_KLEIN_INT8
    assert Models("FLUX3_KLEIN_INT8") is Models.FLUX3_KLEIN_INT8

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


def test_generate_choices_and_pref_view_list_klein_after_redcraft():
    generate = ImageDiffusion.imagegen_group.get_command("generate")
    choices = generate.get_parameter("model").choices
    choice_names = [choice.name for choice in choices]
    assert choice_names[choice_names.index("RedCraft") + 1] == "FLUX 3 Klein INT8"
    klein_choice = next(choice for choice in choices if choice.name == "FLUX 3 Klein INT8")
    assert klein_choice.value == Models.FLUX3_KLEIN_INT8.value
    anima_choice = next(
        choice for choice in choices if choice.value == Models.ONE_OBSESSION_ANIMA.value
    )
    assert anima_choice.name == "One Obsession Anima (NSFW)"
    assert "v2.0" not in anima_choice.name
    assert all(choice.value != Models.KREA2_IDENTITY_EDIT.value for choice in choices)

    view = ImageGenPrefView(None)
    options = view.model_select.options
    labels = [option.label for option in options]
    assert labels[labels.index("RedCraft") + 1] == "FLUX 3 Klein INT8"
    klein = next(option for option in options if option.label == "FLUX 3 Klein INT8")
    assert klein.value == Models.FLUX3_KLEIN_INT8
    assert klein.description == "FLUX 3 Klein distilled INT8 (16 steps)"
    anima = next(option for option in options if option.value == Models.ONE_OBSESSION_ANIMA)
    assert anima.label == "One Obsession Anima (NSFW)"
    assert anima.description == "One Obsession Anima v5.0"
    assert "v2.0" not in anima.description
    assert all(option.value != Models.KREA2_IDENTITY_EDIT for option in options)


def test_boot_registry_places_klein_after_redcraft_and_outside_krea_enhance():
    init_source = inspect.getsource(ImageDiffusion.__init__)
    boot_source, models_source = init_source.split("self.models =", 1)
    assert (
        boot_source.index("Models.REDCRAFT")
        < boot_source.index("Models.FLUX3_KLEIN_INT8")
        < boot_source.index("Models.KREA2_IDENTITY_EDIT")
    )
    assert "ComfyFlux3KleinInt8Model()" in models_source
    assert (
        models_source.index("Models.REDCRAFT")
        < models_source.index("Models.FLUX3_KLEIN_INT8")
        < models_source.index("Models.KREA2_IDENTITY_EDIT")
    )

    schedule_source = inspect.getsource(ImageDiffusion.schedule_generation)
    family_start = schedule_source.index("krea_family")
    family = schedule_source[family_start:schedule_source.index("}", family_start)]
    assert "KREA2_TURBO" in family
    assert "REDCRAFT" in family
    assert "FLUX3_KLEIN_INT8" not in family
