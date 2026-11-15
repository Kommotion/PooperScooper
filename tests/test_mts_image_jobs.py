"""Offline tests for inbox image jobs. No Discord connection and no ComfyUI."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.imagediffusion import IMAGE_QUEUE_MAXSIZE, ImageDiffusion
from cogs.utils.config import PROJECT_ROOT, BotConfig, MtsImageJobsConfig
from cogs.utils.image_models import (
    BackgroundImage,
    ComfyOutput,
    ComfyQwenImage21Model,
    ComfyWaiIllustriousModel,
    ImageCreation,
    Models,
)
from cogs import mts_image_jobs as jobs
from cogs.mts_image_jobs import (
    DEFAULT_TEST_CHANNEL_ID,
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
    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=len(self.calls))


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
            image.result_future.set_result(b"\x89PNG" + image.prompt.encode())

        asyncio.create_task(finish())
        return True


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


def test_channel_refusal_covers_production_and_mismatches():
    with pytest.raises(ChannelRefused) as production_config:
        resolve_post_channel(MTS_PRODUCTION_IMAGE_CHANNEL_ID, None)
    assert "1373140173067653120" in str(production_config.value)
    assert "not allowed" in str(production_config.value)

    with pytest.raises(ChannelRefused) as production_job:
        resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, MTS_PRODUCTION_IMAGE_CHANNEL_ID)
    assert "1373140173067653120" in str(production_job.value)

    with pytest.raises(ChannelRefused) as mismatch:
        resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, 999)
    assert "does not match" in str(mismatch.value)

    assert resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, None) == DEFAULT_TEST_CHANNEL_ID
    assert resolve_post_channel(DEFAULT_TEST_CHANNEL_ID, DEFAULT_TEST_CHANNEL_ID) == DEFAULT_TEST_CHANNEL_ID


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
    assert "1373140173067653120" in (failed / "prod.error.txt").read_text(encoding="utf-8")
    assert "does not match" in (failed / "other.error.txt").read_text(encoding="utf-8")
    assert "1373140173067653120" in (failed / "cfg.error.txt").read_text(encoding="utf-8")
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
    channel = Channel()

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
        assert image.result_future is not None
        assert image.output_width == 768
        assert image.output_height == 1280
        assert image.model is Models.ANIME_WAI_ILLUSTRIOUS
    assert len(channel.calls) == 1
    assert len(channel.calls[0]["files"]) == 3
    assert channel.calls[0]["content"] == "Set\nqueued"
    assert channel.calls[0]["allowed_mentions"] is NO_MENTIONS
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
        assert pngs == [("job-1.png", b"png-bytes")]
        assert calls["enqueue"] == 1

    asyncio.run(run())


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
