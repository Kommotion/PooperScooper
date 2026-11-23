"""Offline tests for inbox image jobs. No Discord connection and no ComfyUI."""

from __future__ import annotations

import asyncio
import json
import logging
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from PIL import Image as PILImage

from cogs.imagediffusion import (
    IMAGE_QUEUE_MAXSIZE,
    ImageDiffusion,
    ImageGenPrefView,
    PromptDetailButton,
    generation_caption,
    post_generation_result,
)
from cogs.utils.config import PROJECT_ROOT, BotConfig, MtsImageJobsConfig
from cogs.utils.image_models import (
    ONE_OBSESSION_ANIMA_WORKFLOW_PATH,
    WAI_ILLUSTRIOUS_WORKFLOW_PATH,
    BackgroundImage,
    ComfyOneObsessionAnimaModel,
    ComfyOutput,
    ComfyQwenImage21Model,
    ComfyWaiIllustriousModel,
    ImageCreation,
    Models,
)
from cogs import mts_image_jobs as jobs
from cogs.mts_image_jobs import (
    DEFAULT_TEST_CHANNEL_ID,
    GENERATE_ATTRIBUTION,
    INTERRUPTED_REASON,
    MAX_UPLOAD_BYTES,
    MTS_PRODUCTION_IMAGE_CHANNEL_ID,
    NO_MENTIONS,
    ChannelRefused,
    JobError,
    claim_ready_jobs,
    parse_job,
    quarantine_processing,
    resolve_post_channel,
)

ROOT = Path(__file__).resolve().parents[1]


def _inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return inbox


def _settings(inbox: Path, **overrides) -> MtsImageJobsConfig:
    data = {
        "enabled": True,
        "channel_id": DEFAULT_TEST_CHANNEL_ID,
        "inbox_dir": str(inbox),
        "default_model": "anime_wai_illustrious",
    }
    data.update(overrides)
    return MtsImageJobsConfig.model_validate(data)


def _drop(inbox: Path, name: str, payload: dict, *, marker: str | None = "json") -> Path:
    path = inbox / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    if marker == "json":
        (inbox / f"{name}.ready").write_text("", encoding="utf-8")
    elif marker == "stem":
        (inbox / f"{path.stem}.ready").write_text("", encoding="utf-8")
    return path


class Channel:
    def __init__(self, guild_id: int | None = None):
        self.calls = []
        self.guild = None if guild_id is None else SimpleNamespace(id=guild_id)

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=len(self.calls), edit=AsyncMock())


def _tiny_png() -> bytes:
    buffer = BytesIO()
    PILImage.new("RGB", (2, 2), (8, 16, 24)).save(buffer, format="PNG")
    return buffer.getvalue()


def _button(view, label: str):
    return next(child for child in view.children if getattr(child, "label", None) == label)


def _bot(channel, image_cog=None):
    return SimpleNamespace(
        get_channel=lambda channel_id: channel,
        get_cog=lambda name: image_cog if name == "ImageDiffusion" else None,
    )


class ReadyDiffusion:
    """Fulfills one queued prompt only after the caller awaits its future."""

    def __init__(self, maxsize: int = IMAGE_QUEUE_MAXSIZE):
        self.image_queue = asyncio.Queue(maxsize=maxsize)
        self.comfy_ready = asyncio.Event()
        self.comfy_ready.set()
        self.comfy_startup_error = None
        self.peak = 0
        self.created = []

    async def enqueue_background(self, image):
        if self.image_queue.full():
            return False
        await self.image_queue.put(image)
        self.peak = max(self.peak, self.image_queue.qsize())
        self.created.append(image)

        async def finish():
            await asyncio.sleep(0)
            await self.image_queue.get()
            png = _tiny_png()
            image.result_future.set_result(
                BackgroundImage(
                    png_bytes=png,
                    image=PILImage.new("RGB", (2, 2), (8, 16, 24)),
                    positive=f"positive:{image.prompt}",
                    negative="negative-prompt",
                    gen_time=1.25,
                )
            )

        asyncio.create_task(finish())
        return True


def _image_diffusion_models():
    """ImageDiffusion.models entries for these tests, without starting the cog."""
    return {
        Models.ANIME_WAI_ILLUSTRIOUS: ComfyWaiIllustriousModel(),
        Models.ONE_OBSESSION_ANIMA: ComfyOneObsessionAnimaModel(),
    }


def test_config_defaults_stay_off_and_example_uses_the_test_channel():
    config = BotConfig.model_validate({"token": "test-token", "client_id": "123"})
    section = config.mts_image_jobs
    assert section.enabled is False
    assert section.channel_id == DEFAULT_TEST_CHANNEL_ID == 1045149015756521493
    assert section.inbox_dir == "data/mts_image_jobs/inbox"
    assert section.default_model == "anime_wai_illustrious"
    assert section.resolved_inbox_dir() == (PROJECT_ROOT / "data" / "mts_image_jobs" / "inbox").resolve()
    assert section.channel_id != MTS_PRODUCTION_IMAGE_CHANNEL_ID

    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["mts_image_jobs"]["enabled"] is False
    assert example["mts_image_jobs"]["channel_id"] == 1045149015756521493
    assert example["mts_image_jobs"]["default_model"] == "anime_wai_illustrious"
    assert MTS_PRODUCTION_IMAGE_CHANNEL_ID == 1373140173067653120
    assert MAX_UPLOAD_BYTES == 10 * 1024 * 1024


def test_cog_is_registered_and_not_halloween_gated():
    import PooperScooper

    assert "cogs.mts_image_jobs" in PooperScooper.initial_extensions
    assert PooperScooper.initial_extensions.index("cogs.mts_image_jobs") > PooperScooper.initial_extensions.index(
        "cogs.imagediffusion"
    )
    assert "cogs.mts_image_jobs" not in PooperScooper._HALLOWEEN_EXTENSIONS
    assert "cogs.mts_image_jobs" in PooperScooper.extensions_to_load(SimpleNamespace())
    source = (ROOT / "cogs" / "mts_image_jobs.py").read_text(encoding="utf-8")
    assert "@app_commands" not in source
    assert "@commands.command" not in source


def test_schema_accepts_generate_and_upload():
    generated = parse_job(
        {
            "type": "generate",
            "prompts": ["a red panda", "a second panda"],
            "model": "anime_wai_illustrious",
            "width": 1024,
            "height": 1024,
            "title": "Red panda set",
            "caption": "Two poses",
        },
        default_model="anime_wai_illustrious",
    )
    assert generated.model is Models.ANIME_WAI_ILLUSTRIOUS
    assert generated.prompts == ("a red panda", "a second panda")
    assert generated.width == 1024 and generated.height == 1024
    assert generated.channel_id is None

    omitted = parse_job(
        {"type": "generate", "prompts": ["a ceramic cup"]},
        default_model="flux3_klein_int8",
    )
    assert omitted.model is Models.FLUX3_KLEIN_INT8
    assert omitted.width is None and omitted.height is None

    uploaded = parse_job(
        {
            "type": "upload",
            "images": ["panda-01.png", "extras/panda-02.png"],
            "title": "Finished pandas",
            "caption": "Posted as-is",
        },
        default_model="anime_wai_illustrious",
    )
    assert uploaded.images == ("panda-01.png", "extras/panda-02.png")


@pytest.mark.parametrize(
    ("payload", "snippet"),
    [
        ({"type": "generate", "prompts": []}, "prompts"),
        ({"type": "generate", "prompts": [""]}, "prompts"),
        ({"type": "generate", "prompts": ["ok", 1]}, "prompts"),
        ({"type": "generate"}, "prompts"),
        ({"type": "generate", "prompts": ["ok"], "model": "nope"}, "unknown model"),
        ({"type": "generate", "prompts": ["ok"], "model": "krea2_identity_edit"}, "identity-edit"),
        ({"type": "generate", "prompts": ["ok"], "model": "Z_IMAGE_TURBO_FP8"}, "unknown model"),
        ({"type": "generate", "prompts": ["ok"], "llm_prompt_enhance": True}, "unknown field"),
        ({"type": "generate", "prompts": ["ok"], "width": 1024}, "together"),
        ({"type": "generate", "prompts": ["ok"], "width": 100, "height": 100}, "multiple of"),
        ({"type": "upload"}, "images"),
        ({"type": "upload", "images": []}, "images"),
        ({"type": "upload", "images": ["../secret.png"]}, "job directory"),
        ({"type": "upload", "images": ["notes.txt"]}, "unsupported"),
        ({"type": "nope"}, "unknown type"),
        ([], "JSON object"),
    ],
)
def test_schema_rejects_bad_jobs(payload, snippet):
    with pytest.raises(JobError) as caught:
        parse_job(payload, default_model="anime_wai_illustrious")
    assert snippet in str(caught.value)


def test_default_model_must_be_a_real_generate_model():
    with pytest.raises(JobError) as caught:
        parse_job({"type": "generate", "prompts": ["ok"]}, default_model="krea2_identity_edit")
    assert "identity-edit" in str(caught.value)


def test_configured_channel_is_used_and_mismatches_are_refused():
    assert resolve_post_channel(MTS_PRODUCTION_IMAGE_CHANNEL_ID, None) == MTS_PRODUCTION_IMAGE_CHANNEL_ID
    assert (
        resolve_post_channel(MTS_PRODUCTION_IMAGE_CHANNEL_ID, MTS_PRODUCTION_IMAGE_CHANNEL_ID)
        == MTS_PRODUCTION_IMAGE_CHANNEL_ID
    )
    assert resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, None) == DEFAULT_TEST_CHANNEL_ID
    assert resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, DEFAULT_TEST_CHANNEL_ID) == DEFAULT_TEST_CHANNEL_ID

    with pytest.raises(ChannelRefused) as test_config_prod_job:
        resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, MTS_PRODUCTION_IMAGE_CHANNEL_ID)
    assert "does not match" in str(test_config_prod_job.value)
    assert "not allowed" not in str(test_config_prod_job.value)

    with pytest.raises(ChannelRefused) as prod_config_test_job:
        resolve_post_channel(MTS_PRODUCTION_IMAGE_CHANNEL_ID, DEFAULT_TEST_CHANNEL_ID)
    assert "does not match" in str(prod_config_test_job.value)
    assert "not allowed" not in str(prod_config_test_job.value)

    with pytest.raises(ChannelRefused) as mismatch:
        resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, 999)
    assert "does not match" in str(mismatch.value)


def test_ready_marker_and_stability_gate_the_claim(tmp_path):
    inbox = _inbox(tmp_path)
    processing = tmp_path / "processing"
    path = _drop(inbox, "job.json", {"type": "upload", "images": ["a.png"]}, marker=None)
    state: dict = {}

    assert claim_ready_jobs(inbox, processing, state, now=0.0, stability_seconds=1.0) == []
    assert path.is_file()

    (inbox / "job.json.ready").write_text("", encoding="utf-8")
    assert claim_ready_jobs(inbox, processing, state, now=0.4, stability_seconds=1.0) == []
    assert path.is_file()

    path.write_text(json.dumps({"type": "upload", "images": ["a.png"], "caption": "x"}), encoding="utf-8")
    assert claim_ready_jobs(inbox, processing, state, now=5.0, stability_seconds=1.0) == []
    assert path.is_file()

    claimed = claim_ready_jobs(inbox, processing, state, now=6.1, stability_seconds=1.0)
    assert [item.name for item in claimed] == ["job.json"]
    assert not path.exists()
    assert (processing / "job.json").is_file()
    assert not (inbox / "job.json.ready").exists()
    assert claim_ready_jobs(inbox, processing, state, now=9.0, stability_seconds=1.0) == []


def test_stem_ready_marker_claims_with_a_zero_window(tmp_path):
    inbox = _inbox(tmp_path)
    processing = tmp_path / "processing"
    _drop(inbox, "batch.json", {"type": "upload", "images": ["a.png"]}, marker="stem")
    claimed = claim_ready_jobs(inbox, processing, {}, now=0.0, stability_seconds=0)
    assert [item.name for item in claimed] == ["batch.json"]
    assert not (inbox / "batch.ready").exists()


def test_claim_is_idempotent_and_processing_leftovers_are_not_posted(tmp_path):
    inbox = _inbox(tmp_path)
    processing = tmp_path / "processing"
    processing.mkdir()
    (processing / "stuck.json").write_text("{}", encoding="utf-8")
    (processing / "stuck").mkdir()
    (processing / "stuck" / "pic.png").write_bytes(b"png")
    channel = Channel()

    async def run():
        handled = await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel),
            state={},
            now=0.0,
            stability_seconds=0,
        )
        assert handled == 0
        assert channel.calls == []
        assert (processing / "stuck.json").is_file()

    asyncio.run(run())

    moved = quarantine_processing(processing, tmp_path / "failed")
    assert moved == ["stuck"]
    assert not (processing / "stuck.json").exists()
    assert (tmp_path / "failed" / "stuck.json").is_file()
    assert (tmp_path / "failed" / "stuck" / "pic.png").is_file()
    text = (tmp_path / "failed" / "stuck.error.txt").read_text(encoding="utf-8")
    assert "interrupted" in text.lower()
    assert text.strip() == INTERRUPTED_REASON
    assert channel.calls == []


def test_disabled_watcher_leaves_jobs_and_rechecks_the_flag(monkeypatch, tmp_path):
    inbox = _inbox(tmp_path)
    (inbox / "pic.png").write_bytes(b"\x89PNG")
    _drop(inbox, "job.json", {"type": "upload", "images": ["pic.png"], "caption": "hi"})
    holder = {"enabled": False}

    def load_config():
        return SimpleNamespace(mts_image_jobs=_settings(inbox, enabled=holder["enabled"]))

    monkeypatch.setattr(jobs, "load_config", load_config)
    channel = Channel()

    async def run():
        state: dict = {}
        assert await jobs.watcher_tick(bot=_bot(channel), state=state, now=0.0, stability_seconds=0) == 0
        assert (inbox / "job.json").is_file()
        assert (inbox / "pic.png").is_file()
        assert channel.calls == []

        holder["enabled"] = True
        assert await jobs.watcher_tick(bot=_bot(channel), state=state, now=1.0, stability_seconds=0) == 1

    asyncio.run(run())
    assert not (inbox / "job.json").exists()
    assert not (inbox / "pic.png").exists()
    assert (tmp_path / "done" / "job.json").is_file()
    assert len(channel.calls) == 1
    assert channel.calls[0]["content"] == "hi"
    assert channel.calls[0]["allowed_mentions"] is NO_MENTIONS


def test_bad_json_and_bad_channel_go_to_failed_without_a_post(tmp_path):
    inbox = _inbox(tmp_path)
    (inbox / "bad.json").write_text("{", encoding="utf-8")
    (inbox / "bad.json.ready").write_text("", encoding="utf-8")
    _drop(
        inbox,
        "prod.json",
        {"type": "upload", "images": ["missing.png"], "channel_id": MTS_PRODUCTION_IMAGE_CHANNEL_ID},
    )
    _drop(inbox, "other.json", {"type": "upload", "images": ["missing.png"], "channel_id": 999})
    channel = Channel()

    async def run():
        state: dict = {}
        settings = _settings(inbox)
        for _ in range(3):
            await jobs.process_once(settings, bot=_bot(channel), state=state, now=10.0, stability_seconds=0)
        prod_settings = _settings(inbox, channel_id=MTS_PRODUCTION_IMAGE_CHANNEL_ID)
        _drop(inbox, "cfg.json", {"type": "upload", "images": ["missing.png"]})
        await jobs.process_once(prod_settings, bot=_bot(channel), state=state, now=11.0, stability_seconds=0)

    asyncio.run(run())
    assert channel.calls == []
    failed = tmp_path / "failed"
    assert "invalid JSON" in (failed / "bad.error.txt").read_text(encoding="utf-8")
    prod_error = (failed / "prod.error.txt").read_text(encoding="utf-8")
    assert "does not match" in prod_error
    assert "not allowed" not in prod_error
    assert "does not match" in (failed / "other.error.txt").read_text(encoding="utf-8")
    cfg_error = (failed / "cfg.error.txt").read_text(encoding="utf-8")
    assert "missing image" in cfg_error
    assert "not allowed" not in cfg_error
    assert "forbidden" not in cfg_error.lower()
    assert not (inbox / "bad.json").exists()
    assert not (inbox / "prod.json").exists()
    assert not (inbox / "cfg.json").exists()


def test_missing_image_fails_the_job(tmp_path):
    inbox = _inbox(tmp_path)
    (inbox / "here.png").write_bytes(b"\x89PNG")
    _drop(inbox, "job.json", {"type": "upload", "images": ["here.png", "gone.png"], "caption": "nope"})
    channel = Channel()

    async def run():
        await jobs.process_once(_settings(inbox), bot=_bot(channel), state={}, now=0.0, stability_seconds=0)

    asyncio.run(run())
    assert channel.calls == []
    error = (tmp_path / "failed" / "job.error.txt").read_text(encoding="utf-8")
    assert "missing image: gone.png" in error
    assert not (inbox / "here.png").exists()
    assert (tmp_path / "failed" / "job" / "here.png").is_file()


def test_upload_splits_eleven_files_and_disables_mentions(tmp_path):
    inbox = _inbox(tmp_path)
    names = [f"img{i}.png" for i in range(11)]
    for index, name in enumerate(names):
        (inbox / name).write_bytes(b"\x89PNG" + bytes([index]))
    _drop(
        inbox,
        "batch.json",
        {"type": "upload", "images": names, "title": "Batch", "caption": "@everyone hello"},
    )
    channel = Channel()

    async def run():
        await jobs.process_once(_settings(inbox), bot=_bot(channel), state={}, now=0.0, stability_seconds=0)

    asyncio.run(run())
    assert len(channel.calls) == 2
    assert len(channel.calls[0]["files"]) == 10
    assert len(channel.calls[1]["files"]) == 1
    assert channel.calls[0]["content"] == "Batch\n@everyone hello"
    assert channel.calls[1]["content"] is None
    for call in channel.calls:
        mentions = call["allowed_mentions"]
        assert mentions is NO_MENTIONS
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert mentions.replied_user is False
    assert not any((inbox / name).exists() for name in names)
    assert (tmp_path / "done" / "batch.json").is_file()
    assert not (tmp_path / "processing" / "batch").exists()


def test_oversize_upload_rejects_the_whole_job(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "MAX_UPLOAD_BYTES", 8)
    inbox = _inbox(tmp_path)
    (inbox / "ok.png").write_bytes(b"1234")
    (inbox / "big.png").write_bytes(b"123456789")
    _drop(inbox, "job.json", {"type": "upload", "images": ["ok.png", "big.png"]})
    channel = Channel()

    async def run():
        await jobs.process_once(_settings(inbox), bot=_bot(channel), state={}, now=0.0, stability_seconds=0)

    asyncio.run(run())
    assert channel.calls == []
    error = (tmp_path / "failed" / "job.error.txt").read_text(encoding="utf-8")
    assert "upload cap" in error
    assert "big.png" in error
    assert not (inbox / "ok.png").exists()
    assert not (inbox / "big.png").exists()
    assert (tmp_path / "failed" / "job" / "big.png").is_file()


def test_generate_uses_the_shared_queue_one_prompt_at_a_time(tmp_path):
    inbox = _inbox(tmp_path)
    _drop(
        inbox,
        "gen.json",
        {
            "type": "generate",
            "prompts": ["first panda", "second panda", "third panda"],
            "width": 768,
            "height": 1280,
            "title": "Set",
            "caption": "queued",
        },
    )
    diffusion = ReadyDiffusion()
    channel = Channel(guild_id=5150)

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
            retry_delay=0.01,
        )

    asyncio.run(run())
    assert diffusion.peak == 1
    assert len(diffusion.created) == 3
    assert [image.prompt for image in diffusion.created] == ["first panda", "second panda", "third panda"]
    for image in diffusion.created:
        assert image.llm_prompt_enhance is False
        assert image.interaction is None
        assert image.guild_id == 5150
        assert image.result_future is not None
        assert image.output_width == 768
        assert image.output_height == 1280
        assert image.model is Models.ANIME_WAI_ILLUSTRIOUS
    prompts = ["first panda", "second panda", "third panda"]
    assert len(channel.calls) == 3
    for call, prompt in zip(channel.calls, prompts):
        assert "files" not in call
        assert call["file"].filename == "generated.png"
        assert call["allowed_mentions"] is NO_MENTIONS
        assert isinstance(call["view"], PromptDetailButton)
        assert call["view"].author_id == 0
        assert call["view"].guild_id == 5150
        assert call["view"].message is not None
        assert call["view"].llm_prompt_enhance is False
        assert call["content"] == (
            "Set\nqueued\n"
            f"**{prompt} \u2014 `anime_wai_illustrious` \u2014 Requested by MTS Party Prompts**"
        )
    assert (tmp_path / "done" / "gen.json").is_file()


def test_generate_waits_instead_of_filling_a_full_queue():
    diffusion = ReadyDiffusion(maxsize=1)
    diffusion.image_queue.put_nowait("discord-user")
    calls = {"enqueue": 0}

    async def enqueue_background(image):
        calls["enqueue"] += 1
        if diffusion.image_queue.full():
            return False
        await diffusion.image_queue.put(image)
        image.result_future.set_result(b"png-bytes")
        return True

    diffusion.enqueue_background = enqueue_background
    job = parse_job(
        {"type": "generate", "prompts": ["one panda"]},
        default_model="anime_wai_illustrious",
    )

    async def run():
        task = asyncio.create_task(
            jobs.generate_pngs(job, diffusion, stem="job", retry_delay=0.01, wait_seconds=5)
        )
        await asyncio.sleep(0.05)
        assert calls["enqueue"] == 0
        assert not task.done()
        assert diffusion.image_queue.qsize() == 1
        await diffusion.image_queue.get()
        pngs = await asyncio.wait_for(task, timeout=1)
        assert [(item.name, item.png_bytes) for item in pngs] == [("job-1.png", b"png-bytes")]
        assert pngs[0].positive == ""
        assert pngs[0].negative == ""
        assert pngs[0].gen_time == 0
        assert calls["enqueue"] == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("model_id", "expected_model", "workflow_path", "workflow_name"),
    [
        (
            "one_obsession_anima",
            Models.ONE_OBSESSION_ANIMA,
            ONE_OBSESSION_ANIMA_WORKFLOW_PATH,
            "ONE_OBSESSION_ANIMA_API.json",
        ),
        (
            "anime_wai_illustrious",
            Models.ANIME_WAI_ILLUSTRIOUS,
            WAI_ILLUSTRIOUS_WORKFLOW_PATH,
            "WAI-ILLUSTRIOUS-SDXL_API.json",
        ),
    ],
)
def test_generate_job_model_reaches_image_creation(
    model_id, expected_model, workflow_path, workflow_name
):
    other_default = (
        "anime_wai_illustrious" if model_id == "one_obsession_anima" else "one_obsession_anima"
    )
    job = parse_job(
        {"type": "generate", "prompts": ["a red panda"], "model": model_id},
        default_model=other_default,
    )
    diffusion = ReadyDiffusion()

    async def run():
        return await jobs.generate_pngs(job, diffusion, stem="job")

    pngs = asyncio.run(run())
    assert len(pngs) == 1
    assert len(diffusion.created) == 1
    creation = diffusion.created[0]
    assert isinstance(creation, ImageCreation)
    assert creation is pngs[0].creation
    assert creation.model is expected_model
    entry = _image_diffusion_models()[creation.model]
    assert entry.workflow_path == workflow_path
    assert entry.workflow_path.endswith(workflow_name)


def test_generate_job_without_model_uses_config_default(tmp_path):
    settings = _settings(tmp_path, default_model="one_obsession_anima")
    assert settings.default_model == "one_obsession_anima"
    job = parse_job(
        {"type": "generate", "prompts": ["a ceramic cup"]},
        default_model=settings.default_model,
    )
    diffusion = ReadyDiffusion()

    async def run():
        return await jobs.generate_pngs(job, diffusion, stem="job")

    pngs = asyncio.run(run())
    assert len(pngs) == 1
    assert len(diffusion.created) == 1
    creation = diffusion.created[0]
    assert isinstance(creation, ImageCreation)
    assert creation is pngs[0].creation
    assert creation.model is Models.ONE_OBSESSION_ANIMA
    entry = _image_diffusion_models()[creation.model]
    assert entry.workflow_path == ONE_OBSESSION_ANIMA_WORKFLOW_PATH
    assert entry.workflow_path.endswith("ONE_OBSESSION_ANIMA_API.json")


def test_generate_stays_in_the_inbox_until_comfy_is_ready(tmp_path):
    inbox = _inbox(tmp_path)
    _drop(inbox, "gen.json", {"type": "generate", "prompts": ["a cup"]})
    diffusion = ReadyDiffusion()
    diffusion.comfy_ready.clear()
    channel = Channel()

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
        )
        assert (inbox / "gen.json").is_file()
        assert channel.calls == []
        diffusion.comfy_ready.set()
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=2.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    assert not (inbox / "gen.json").exists()
    assert len(channel.calls) == 1
    assert channel.calls[0]["allowed_mentions"] is NO_MENTIONS
    assert "files" not in channel.calls[0]
    assert channel.calls[0]["file"].filename == "generated.png"
    assert isinstance(channel.calls[0]["view"], PromptDetailButton)
    assert "Requested by MTS Party Prompts" in channel.calls[0]["content"]


def test_enqueue_background_shares_the_image_queue():
    async def run():
        queue = asyncio.Queue(maxsize=IMAGE_QUEUE_MAXSIZE)
        ready = asyncio.Event()
        ready.set()
        cog = SimpleNamespace(comfy_ready=ready, comfy_startup_error=None, image_queue=queue)
        future = asyncio.get_running_loop().create_future()
        image = ImageCreation("cat", Models.ANIME_WAI_ILLUSTRIOUS, result_future=future)
        assert await ImageDiffusion.enqueue_background(cog, image) is True
        assert queue.qsize() == 1
        assert queue.get_nowait() is image

        for index in range(IMAGE_QUEUE_MAXSIZE):
            queue.put_nowait(index)
        blocked = ImageCreation(
            "cat",
            Models.ANIME_WAI_ILLUSTRIOUS,
            result_future=asyncio.get_running_loop().create_future(),
        )
        assert await ImageDiffusion.enqueue_background(cog, blocked) is False
        assert queue.qsize() == IMAGE_QUEUE_MAXSIZE

        ready.clear()
        assert await ImageDiffusion.enqueue_background(cog, blocked) is False
        with pytest.raises(ValueError):
            await ImageDiffusion.enqueue_background(
                cog,
                ImageCreation("cat", Models.ANIME_WAI_ILLUSTRIOUS),
            )

    asyncio.run(run())


def test_background_generation_returns_png_bytes_without_posting(monkeypatch):
    monkeypatch.setattr("cogs.imagediffusion.comfy_client.is_comfy_running", lambda: True)

    class Model:
        async def generate(self, image):
            from PIL import Image as PILImage

            img = PILImage.new("RGB", (4, 4), (10, 20, 30))
            return ComfyOutput([img]), "positive", "", 0.2

    async def run():
        channel = SimpleNamespace(send=AsyncMock())
        future = asyncio.get_running_loop().create_future()
        image = ImageCreation(
            "a cat",
            Models.ANIME_WAI_ILLUSTRIOUS,
            interaction=SimpleNamespace(channel=channel, user=SimpleNamespace(id=1, mention="<@1>")),
            result_future=future,
            llm_prompt_enhance=False,
        )
        cog = ImageDiffusion.__new__(ImageDiffusion)
        cog.models = {Models.ANIME_WAI_ILLUSTRIOUS: Model()}
        cog.comfy_startup_error = None
        await ImageDiffusion._image_generation_interaction(cog, image)
        result = future.result()
        assert isinstance(result, BackgroundImage)
        assert result.png_bytes.startswith(b"\x89PNG")
        assert result.positive == "positive"
        assert result.negative == ""
        assert result.gen_time == 0.2
        channel.send.assert_not_called()

    asyncio.run(run())


def test_output_size_patches_latent_nodes_and_leaves_qwen_on_the_workflow_default(caplog):
    sized = ImageCreation(
        "a fox",
        Models.ANIME_WAI_ILLUSTRIOUS,
        output_width=768,
        output_height=1280,
    )
    workflow = {
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "13": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "82": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
    }
    patched = ComfyWaiIllustriousModel()._apply_prompts_to_workflow(workflow, "pos", "neg", sized)
    assert patched["5"]["inputs"]["width"] == 768
    assert patched["5"]["inputs"]["height"] == 1280
    assert patched["13"]["inputs"]["width"] == 768
    assert patched["82"]["inputs"]["height"] == 1280
    assert workflow["5"]["inputs"]["width"] == 1024

    plain = ImageCreation("a fox", Models.ANIME_WAI_ILLUSTRIOUS)
    untouched = ComfyWaiIllustriousModel()._apply_prompts_to_workflow(workflow, "pos", "neg", plain)
    assert untouched["5"]["inputs"]["width"] == 1024
    assert untouched["13"]["inputs"]["height"] == 512

    qwen_image = ImageCreation("a fox", Models.QWEN_IMAGE_21, output_width=768, output_height=768)
    graph = {
        "20": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {"prompt": "old", "negative_prompt": "", "resolution": 1024},
        }
    }
    with caplog.at_level(logging.INFO, logger="cogs.utils.image_models"):
        patched_qwen = ComfyQwenImage21Model()._apply_prompts_to_workflow(graph, "pos", "", qwen_image)
    assert patched_qwen["20"]["inputs"]["resolution"] == 1024
    assert "no standard latent size node" in caplog.text
    assert "workflow default" in caplog.text


def test_generation_caption_uses_a_mention_or_plain_attribution():
    slash = ImageCreation(
        "a ceramic cup",
        Models.ANIME_WAI_ILLUSTRIOUS,
        interaction=SimpleNamespace(user=SimpleNamespace(id=42, mention="<@42>")),
    )
    assert generation_caption(slash, "a ceramic cup") == (
        "**a ceramic cup \u2014 `anime_wai_illustrious` \u2014 <@42>**"
    )

    class Boom:
        @property
        def user(self):
            raise AssertionError("inbox captions must not read interaction.user")

    inbox = ImageCreation("a ceramic cup", Models.ANIME_WAI_ILLUSTRIOUS, interaction=Boom())
    caption = generation_caption(inbox, "a ceramic cup", GENERATE_ATTRIBUTION)
    assert caption == (
        "**a ceramic cup \u2014 `anime_wai_illustrious` \u2014 Requested by MTS Party Prompts**"
    )
    assert "<@" not in caption
    assert "@" not in caption
    assert GENERATE_ATTRIBUTION == "Requested by MTS Party Prompts"

    async def run():
        mentions = discord.AllowedMentions(users=True, replied_user=True)
        channel = Channel(guild_id=9)
        image = PILImage.new("RGB", (2, 2), (1, 2, 3))
        await post_generation_result(
            channel,
            slash,
            result_image=image,
            positive="pos",
            negative="neg",
            gen_time=3.5,
            author_id=42,
            guild_id=9,
            allowed_mentions=mentions,
        )
        call = channel.calls[0]
        assert call["content"] == "**a ceramic cup \u2014 `anime_wai_illustrious` \u2014 <@42>**"
        assert call["allowed_mentions"] is mentions
        assert call["allowed_mentions"].users is True
        assert call["allowed_mentions"].replied_user is True
        assert call["file"].filename == "generated.png"
        assert call["view"].author_id == 42
        assert call["view"].guild_id == 9
        assert call["view"].message is not None

        long_prompt = "p" * 161
        long_image = ImageCreation(long_prompt, Models.ANIME_WAI_ILLUSTRIOUS, interaction=None)
        long_channel = Channel()
        await post_generation_result(
            long_channel,
            long_image,
            png_bytes=_tiny_png(),
            positive="pos",
            negative="",
            gen_time=1.5,
            author_id=0,
            guild_id=0,
            allowed_mentions=NO_MENTIONS,
            attribution=GENERATE_ATTRIBUTION,
            content_prefix="Title\nCaption",
        )
        posted = long_channel.calls[0]["content"]
        assert posted.startswith("Title\nCaption\n")
        assert posted.endswith("-# Full prompt in **See prompt details**")
        assert "Requested by MTS Party Prompts" in posted
        assert "<@" not in posted
        assert "@" not in posted.split("\n", 2)[-1]

    asyncio.run(run())


def test_slash_generation_posts_through_the_shared_helper(monkeypatch):
    monkeypatch.setattr("cogs.imagediffusion.comfy_client.is_comfy_running", lambda: True)

    class Model:
        async def generate(self, image):
            return ComfyOutput([PILImage.new("RGB", (2, 2), (1, 2, 3))]), "positive text", "negative text", 4.5

    async def run():
        channel = Channel()
        user = SimpleNamespace(id=42, mention="<@42>")
        image = ImageCreation(
            "a mountain",
            Models.ANIME_WAI_ILLUSTRIOUS,
            interaction=SimpleNamespace(channel=channel, user=user, guild_id=77),
        )
        cog = ImageDiffusion.__new__(ImageDiffusion)
        cog.models = {Models.ANIME_WAI_ILLUSTRIOUS: Model()}
        cog.comfy_startup_error = None
        await ImageDiffusion._image_generation_interaction(cog, image)
        call = channel.calls[0]
        assert call["content"] == "**a mountain \u2014 `anime_wai_illustrious` \u2014 <@42>**"
        assert call["file"].filename == "generated.png"
        assert call["view"].author_id == 42
        assert call["view"].guild_id == 77
        assert call["view"].positive == "positive text"
        assert call["view"].negative == "negative text"
        assert call["view"].gen_time == 4.5
        assert call["view"].message is not None
        assert call["allowed_mentions"].users is True
        assert call["allowed_mentions"].replied_user is True
        labels = [child.label for child in call["view"].children]
        assert labels == ["See prompt details", "Edit and Regenerate", "Regenerate", "Delete"]

    asyncio.run(run())


def _one_generate(tmp_path: Path, payload: dict, *, guild_id: int | None = None):
    inbox = _inbox(tmp_path)
    _drop(inbox, "gen.json", payload)
    diffusion = ReadyDiffusion()
    channel = Channel(guild_id=guild_id)

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    return channel


def test_generate_job_posts_the_four_prompt_buttons(tmp_path):
    channel = _one_generate(
        tmp_path,
        {"type": "generate", "prompts": ["a cup of tea"], "title": "Tea", "caption": "hot"},
        guild_id=12345,
    )
    assert len(channel.calls) == 1
    view = channel.calls[0]["view"]
    assert isinstance(view, PromptDetailButton)
    assert [child.label for child in view.children] == [
        "See prompt details",
        "Edit and Regenerate",
        "Regenerate",
        "Delete",
    ]
    assert view.author_id == 0
    assert view.guild_id == 12345
    assert view.message is not None
    assert view.timeout == 900
    assert channel.calls[0]["content"] == (
        "Tea\nhot\n"
        "**a cup of tea \u2014 `anime_wai_illustrious` \u2014 Requested by MTS Party Prompts**"
    )


def test_see_prompt_details_works_without_a_requesting_user(tmp_path):
    channel = _one_generate(tmp_path, {"type": "generate", "prompts": ["a cup of tea"]})
    view = channel.calls[0]["view"]
    assert view.author_id == 0

    async def run():
        clicker = SimpleNamespace(
            user=SimpleNamespace(id=88),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
        )
        await _button(view, "See prompt details").callback(clicker)
        kwargs = clicker.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        embed = kwargs["embed"]
        assert embed.title == "🖼️ Image Generation Details"
        assert "anime_wai_illustrious" in embed.description
        fields = {field.name: field.value for field in embed.fields}
        assert "a cup of tea" in fields["User Prompt"]
        assert fields["Positive Prompt"] == "positive:a cup of tea"
        assert fields["Negative Prompt"] == "negative-prompt"
        assert fields["Generation time"] == "1.25 seconds"
        assert fields["LLM prompt enhance"] == "False"

    asyncio.run(run())


def test_delete_on_a_job_post_needs_the_moderator_role(monkeypatch, tmp_path):
    seen = []

    def fake_config(guild_id):
        seen.append(guild_id)
        return SimpleNamespace(image_diffusion_moderator_role_id=42)

    monkeypatch.setattr("cogs.imagediffusion.get_guild_config", fake_config)
    channel = _one_generate(
        tmp_path,
        {"type": "generate", "prompts": ["a cup of tea"]},
        guild_id=12345,
    )
    view = channel.calls[0]["view"]
    assert view.author_id == 0

    async def run():
        denied_delete = AsyncMock()
        stranger = SimpleNamespace(
            user=SimpleNamespace(id=555, roles=[SimpleNamespace(id=1)]),
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(delete=denied_delete),
        )
        await _button(view, "Delete").callback(stranger)
        denied_delete.assert_not_awaited()
        assert stranger.response.send_message.await_args.kwargs["ephemeral"] is True
        assert "permission" in stranger.response.send_message.await_args.args[0].lower()

        allowed_delete = AsyncMock()
        moderator = SimpleNamespace(
            user=SimpleNamespace(id=555, roles=[SimpleNamespace(id=42)]),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            message=SimpleNamespace(delete=allowed_delete),
        )
        await _button(view, "Delete").callback(moderator)
        moderator.response.defer.assert_awaited()
        allowed_delete.assert_awaited()
        moderator.response.send_message.assert_not_awaited()

    asyncio.run(run())
    assert seen == [12345, 12345]


def test_job_post_buttons_use_the_clicker_not_an_inbox_user(tmp_path):
    channel = _one_generate(tmp_path, {"type": "generate", "prompts": ["a cup of tea"]})
    view = channel.calls[0]["view"]
    enqueued = []

    async def enqueue_image(entry, interaction, **kwargs):
        enqueued.append((entry, interaction, kwargs))
        return True

    clicker_cog = SimpleNamespace(enqueue_image=enqueue_image)

    async def run():
        clicker = SimpleNamespace(
            user=SimpleNamespace(id=77, mention="<@77>", roles=[]),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
            client=SimpleNamespace(get_cog=lambda name: clicker_cog if name == "ImageDiffusion" else None),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        await _button(view, "Regenerate").callback(clicker)
        assert len(enqueued) == 1
        entry, interaction, kwargs = enqueued[0]
        assert interaction is clicker
        assert entry.interaction is clicker
        assert entry.prompt == "a cup of tea"
        assert entry.model is Models.ANIME_WAI_ILLUSTRIOUS
        assert kwargs["ephemeral"] is True

        editor_cog = SimpleNamespace()
        editor = SimpleNamespace(
            user=SimpleNamespace(id=88, mention="<@88>"),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
            client=SimpleNamespace(get_cog=lambda name: editor_cog if name == "ImageDiffusion" else None),
        )
        await _button(view, "Edit and Regenerate").callback(editor)
        opened = editor.response.send_message.await_args.kwargs
        assert opened["ephemeral"] is True
        assert isinstance(opened["view"], ImageGenPrefView)
        assert opened["view"].cog is editor_cog
        assert opened["view"].initial_prompt == "a cup of tea"

        missing = SimpleNamespace(
            user=SimpleNamespace(id=90),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
            client=SimpleNamespace(get_cog=lambda name: None),
        )
        await _button(view, "Edit and Regenerate").callback(missing)
        assert missing.response.send_message.await_args.kwargs["ephemeral"] is True
        assert "unavailable" in missing.response.send_message.await_args.args[0].lower()

        regen_missing = SimpleNamespace(
            user=SimpleNamespace(id=91),
            response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
            client=SimpleNamespace(get_cog=lambda name: None),
        )
        await _button(view, "Regenerate").callback(regen_missing)
        assert regen_missing.response.send_message.await_args.kwargs["ephemeral"] is True
        assert "unavailable" in regen_missing.response.send_message.await_args.args[0].lower()
        assert len(enqueued) == 1

    asyncio.run(run())


def test_generate_refuses_a_channel_mismatch(tmp_path):
    inbox = _inbox(tmp_path)
    _drop(
        inbox,
        "gen.json",
        {
            "type": "generate",
            "prompts": ["a panda"],
            "channel_id": MTS_PRODUCTION_IMAGE_CHANNEL_ID,
        },
    )
    diffusion = ReadyDiffusion()
    channel = Channel()

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    assert channel.calls == []
    assert diffusion.created == []
    error = (tmp_path / "failed" / "gen.error.txt").read_text(encoding="utf-8")
    assert "does not match" in error
    assert "not allowed" not in error
    assert not (tmp_path / "done" / "gen.json").exists()


def test_generate_posts_when_the_configured_channel_is_production(tmp_path):
    inbox = _inbox(tmp_path)
    _drop(inbox, "gen.json", {"type": "generate", "prompts": ["a panda in the mts channel"]})
    diffusion = ReadyDiffusion()
    channel = Channel()
    seen: list[int] = []

    def get_channel(channel_id):
        seen.append(channel_id)
        return channel

    bot = SimpleNamespace(
        get_channel=get_channel,
        get_cog=lambda name: diffusion if name == "ImageDiffusion" else None,
    )

    async def run():
        await jobs.process_once(
            _settings(inbox, channel_id=MTS_PRODUCTION_IMAGE_CHANNEL_ID),
            bot=bot,
            state={},
            now=0.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    assert seen == [MTS_PRODUCTION_IMAGE_CHANNEL_ID]
    assert len(channel.calls) == 1
    call = channel.calls[0]
    assert call["allowed_mentions"] is NO_MENTIONS
    mentions = call["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    assert "Requested by MTS Party Prompts" in call["content"]
    assert (tmp_path / "done" / "gen.json").is_file()
    assert not (tmp_path / "failed" / "gen.error.txt").exists()
    assert len(diffusion.created) == 1


def test_generate_posts_disable_mentions_when_the_text_pings_everyone(tmp_path):
    channel = _one_generate(
        tmp_path,
        {
            "type": "generate",
            "prompts": ["a panda"],
            "title": "@everyone party",
            "caption": "<@999> look",
        },
    )
    call = channel.calls[0]
    assert call["allowed_mentions"] is NO_MENTIONS
    mentions = call["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False
    lines = call["content"].split("\n")
    assert lines == [
        "@everyone party",
        "<@999> look",
        "**a panda \u2014 `anime_wai_illustrious` \u2014 Requested by MTS Party Prompts**",
    ]
    assert "<@" not in lines[2]
    assert "@" not in lines[2]


def test_later_generate_failure_posts_nothing(tmp_path):
    inbox = _inbox(tmp_path)
    _drop(inbox, "gen.json", {"type": "generate", "prompts": ["first", "second"]})

    class SecondPromptFails(ReadyDiffusion):
        async def enqueue_background(self, image):
            if self.image_queue.full():
                return False
            await self.image_queue.put(image)
            self.peak = max(self.peak, self.image_queue.qsize())
            self.created.append(image)

            async def finish():
                await asyncio.sleep(0)
                await self.image_queue.get()
                if image.prompt == "second":
                    image.result_future.set_exception(RuntimeError("comfy down"))
                    return
                image.result_future.set_result(
                    BackgroundImage(
                        png_bytes=_tiny_png(),
                        image=PILImage.new("RGB", (2, 2), (1, 2, 3)),
                        positive="pos",
                        negative="neg",
                        gen_time=0.5,
                    )
                )

            asyncio.create_task(finish())
            return True

    diffusion = SecondPromptFails()
    channel = Channel()

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    assert channel.calls == []
    assert [image.prompt for image in diffusion.created] == ["first", "second"]
    error = (tmp_path / "failed" / "gen.error.txt").read_text(encoding="utf-8")
    assert "image generation failed" in error
    assert not (tmp_path / "done" / "gen.json").exists()


def test_oversize_generate_posts_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "MAX_UPLOAD_BYTES", 8)
    inbox = _inbox(tmp_path)
    _drop(inbox, "gen.json", {"type": "generate", "prompts": ["a panda", "another panda"]})
    diffusion = ReadyDiffusion()
    channel = Channel()

    async def run():
        await jobs.process_once(
            _settings(inbox),
            bot=_bot(channel, diffusion),
            state={},
            now=0.0,
            stability_seconds=0,
        )

    asyncio.run(run())
    assert channel.calls == []
    error = (tmp_path / "failed" / "gen.error.txt").read_text(encoding="utf-8")
    assert "upload cap" in error
    assert not (tmp_path / "done" / "gen.json").exists()
