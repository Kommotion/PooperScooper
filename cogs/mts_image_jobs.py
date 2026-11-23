"""Watch an inbox for image jobs and post the results with no approval step.

Inbox on this machine (the default ``inbox_dir`` resolved from the repo root):

    C:\\Users\\jacqu\\Desktop\\PooperScooper\\data\\mts_image_jobs\\inbox\\

Sibling folders, next to that inbox, are ``processing\\``, ``done\\``, and
``failed\\``. The cog creates them if they are missing.

The watcher loads with the bot but does nothing until ``mts_image_jobs.enabled``
is true. Set that in config.json and restart. Posting goes to the configured
``channel_id``, which may be the support test channel or MTS #image-diffusion
when that id is configured.

Producers copy images first, then the JSON, then a ``.ready`` marker last.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Callable

import discord
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.imagediffusion import post_generation_result
from cogs.utils.config import MtsImageJobsConfig, load_config
from cogs.utils.image_models import ImageCreation, Models

log = logging.getLogger(__name__)

# MTS #image-diffusion channel id. Posting there is allowed when
# config.channel_id is set to it.
MTS_PRODUCTION_IMAGE_CHANNEL_ID = 1373140173067653120

# Support test channel. Same id as candyhunt.SUPPORT_TEST_CHANNEL_ID.
DEFAULT_TEST_CHANNEL_ID = 1045149015756521493

POLL_SECONDS = 2
STABILITY_SECONDS = 1.0
QUEUE_RETRY_SECONDS = 1.0
# A claimed generate job waits this long for a free queue slot, then fails.
QUEUE_WAIT_SECONDS = 6 * 60 * 60
DISCORD_ATTACHMENT_BATCH = 10
# Discord's default upload limit for a bot without a boosted server.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_JOB_BYTES = 1024 * 1024
MAX_PROMPTS = 30
MAX_IMAGES = 50
MAX_PROMPT_CHARS = 4000
MAX_TEXT_CHARS = 2000
MIN_LATENT = 64
MAX_LATENT = 2048
LATENT_MULTIPLE = 64

_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_GENERATE_KEYS = frozenset({"type", "prompts", "model", "width", "height", "title", "caption", "channel_id"})
_UPLOAD_KEYS = frozenset({"type", "images", "title", "caption", "channel_id"})
_GENERATE_MODEL_VALUES = frozenset(
    member.value for member in Models if member is not Models.KREA2_IDENTITY_EDIT
)

NO_MENTIONS = discord.AllowedMentions.none()
# Plain text in the mention slot of an /imagegen-style generate post.
GENERATE_ATTRIBUTION = "Requested by MTS Party Prompts"
# Not a Discord snowflake. Delete will not treat a clicker as the author.
INBOX_AUTHOR_ID = 0

INTERRUPTED_REASON = (
    "interrupted: this job was still in processing when the cog started, "
    "so it was not posted again"
)


class JobError(Exception):
    """A job cannot be posted. The caller moves it to failed/."""


class ChannelRefused(JobError):
    """Config or the job asked for a channel this cog will not post in."""


@dataclass(frozen=True)
class GenerateJob:
    kind: str
    prompts: tuple[str, ...]
    model: Models
    width: int | None
    height: int | None
    title: str
    caption: str
    channel_id: int | None


@dataclass
class GeneratedImage:
    """One finished prompt. Posted only after every prompt in the job succeeds."""

    name: str
    png_bytes: bytes
    creation: ImageCreation
    positive: str = ""
    negative: str = ""
    gen_time: float = 0.0


@dataclass(frozen=True)
class UploadJob:
    kind: str
    images: tuple[str, ...]
    title: str
    caption: str
    channel_id: int | None


def current_settings() -> MtsImageJobsConfig:
    """Return the inbox settings. A missing or unreadable config stays off."""
    try:
        config = load_config()
    except Exception:
        log.error("Could not read mts_image_jobs config; watcher stays off")
        return MtsImageJobsConfig(enabled=False)
    section = getattr(config, "mts_image_jobs", None)
    if isinstance(section, MtsImageJobsConfig):
        return section
    return MtsImageJobsConfig(enabled=False)


def ensure_job_dirs(inbox: Path) -> dict[str, Path]:
    """Create inbox, processing, done, and failed next to each other."""
    inbox = Path(inbox)
    root = inbox.parent
    dirs = {
        "inbox": inbox,
        "processing": root / "processing",
        "done": root / "done",
        "failed": root / "failed",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def message_content(title: str, caption: str) -> str:
    parts = [part for part in ((title or "").strip(), (caption or "").strip()) if part]
    text = "\n".join(parts)
    if len(text) > 2000:
        return text[:1997] + "..."
    return text


def resolve_post_channel(config_channel_id: int, job_channel_id: int | None) -> int:
    """Return the only channel a job may use, or raise ChannelRefused.

    Jobs normally omit channel_id. A job that names a different channel than
    config is refused. The configured channel is the one used, including the
    production MTS channel when that id is configured.
    """
    if isinstance(config_channel_id, bool) or not isinstance(config_channel_id, int) or config_channel_id <= 0:
        raise ChannelRefused("refused: configured channel_id is invalid")
    if job_channel_id is not None and job_channel_id != config_channel_id:
        raise ChannelRefused(
            "refused: job channel_id does not match the configured channel "
            f"({config_channel_id})"
        )
    return int(config_channel_id)


def parse_generate_model(value: object) -> Models:
    """Accept a canonical model id. Identity Edit and unknown ids fail."""
    if not isinstance(value, str) or not value.strip():
        raise JobError("schema: model must be a model id")
    token = value.strip()
    if token == Models.KREA2_IDENTITY_EDIT.value:
        raise JobError("schema: identity-edit needs a source image and cannot be a generate model")
    if token not in _GENERATE_MODEL_VALUES:
        raise JobError(f"schema: unknown model: {token}")
    return Models(token)


def parse_job(payload: object, *, default_model: str) -> GenerateJob | UploadJob:
    """Validate one job object. Unknown keys and empty lists are rejected."""
    if not isinstance(payload, dict):
        raise JobError("schema: job must be a JSON object")
    job_type = payload.get("type")
    if job_type == "generate":
        _reject_unknown(payload, _GENERATE_KEYS)
        return _parse_generate(payload, default_model=default_model)
    if job_type == "upload":
        _reject_unknown(payload, _UPLOAD_KEYS)
        return _parse_upload(payload)
    if not isinstance(job_type, str):
        raise JobError("schema: type must be generate or upload")
    raise JobError(f"schema: unknown type: {job_type}")


def _reject_unknown(payload: dict, allowed: frozenset[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise JobError(f"schema: unknown field: {unknown[0]}")


def _parse_generate(payload: dict, *, default_model: str) -> GenerateJob:
    if "prompts" not in payload:
        raise JobError("schema: prompts must be a non-empty list")
    prompts = _parse_prompts(payload.get("prompts"))
    if "model" in payload and payload.get("model") is not None:
        model = parse_generate_model(payload.get("model"))
    else:
        model = parse_generate_model(default_model)
    width, height = _parse_size(payload)
    return GenerateJob(
        kind="generate",
        prompts=prompts,
        model=model,
        width=width,
        height=height,
        title=_optional_text(payload, "title"),
        caption=_optional_text(payload, "caption"),
        channel_id=_optional_channel(payload),
    )


def _parse_upload(payload: dict) -> UploadJob:
    if "images" not in payload:
        raise JobError("schema: images must be a non-empty list")
    return UploadJob(
        kind="upload",
        images=_parse_image_names(payload.get("images")),
        title=_optional_text(payload, "title"),
        caption=_optional_text(payload, "caption"),
        channel_id=_optional_channel(payload),
    )


def _parse_prompts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise JobError("schema: prompts must be a non-empty list")
    if len(value) > MAX_PROMPTS:
        raise JobError(f"schema: prompts list is longer than {MAX_PROMPTS}")
    prompts: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise JobError("schema: prompts must be non-empty strings")
        text = item.strip()
        if len(text) > MAX_PROMPT_CHARS:
            raise JobError("schema: prompt is too long")
        prompts.append(text)
    return tuple(prompts)


def _parse_image_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise JobError("schema: images must be a non-empty list")
    if len(value) > MAX_IMAGES:
        raise JobError(f"schema: images list is longer than {MAX_IMAGES}")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise JobError("schema: images must be relative file names")
        name = item.strip().replace("\\", "/")
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise JobError("schema: image path must be relative to the job directory")
        parts = PurePosixPath(name).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise JobError("schema: image path must stay inside the job directory")
        suffix = PurePosixPath(parts[-1]).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            raise JobError(f"schema: unsupported image type: {name}")
        names.append("/".join(parts))
    if len(set(names)) != len(names):
        raise JobError("schema: duplicate image name")
    return tuple(names)


def _parse_size(payload: dict) -> tuple[int | None, int | None]:
    has_width = "width" in payload and payload.get("width") is not None
    has_height = "height" in payload and payload.get("height") is not None
    if not has_width and not has_height:
        return None, None
    if has_width != has_height:
        raise JobError("schema: width and height must be set together")
    return _size_value(payload.get("width"), "width"), _size_value(payload.get("height"), "height")


def _size_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JobError(f"schema: {name} must be an integer")
    if value < MIN_LATENT or value > MAX_LATENT or value % LATENT_MULTIPLE:
        raise JobError(
            f"schema: {name} must be a multiple of {LATENT_MULTIPLE} "
            f"between {MIN_LATENT} and {MAX_LATENT}"
        )
    return value


def _optional_text(payload: dict, key: str) -> str:
    if key not in payload or payload.get(key) is None:
        return ""
    value = payload.get(key)
    if not isinstance(value, str):
        raise JobError(f"schema: {key} must be a string")
    text = value.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise JobError(f"schema: {key} is too long")
    return text


def _optional_channel(payload: dict) -> int | None:
    if "channel_id" not in payload or payload.get("channel_id") is None:
        return None
    value = payload.get("channel_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JobError("schema: channel_id must be an integer")
    return value


def read_job_payload(path: Path) -> object:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise JobError(f"invalid JSON: {exc}") from exc
    if size > MAX_JOB_BYTES:
        raise JobError("invalid JSON: job file is too large")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise JobError(f"invalid JSON: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JobError(f"invalid JSON: {exc.msg}") from exc


def peek_job_type(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload["type"]
    return None


def has_ready_marker(inbox: Path, json_name: str) -> bool:
    """True when ``name.json.ready`` or ``name.ready`` is a real file."""
    for candidate in _marker_paths(inbox, json_name):
        try:
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                return True
        except OSError:
            continue
    return False


def remove_ready_markers(inbox: Path, json_name: str) -> None:
    for candidate in _marker_paths(inbox, json_name):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidate.unlink()
        except OSError:
            log.warning("mts image jobs could not remove ready marker %s", candidate.name)


def _marker_paths(inbox: Path, json_name: str) -> tuple[Path, Path]:
    stem = json_name[:-5] if json_name.endswith(".json") else json_name
    return (inbox / f"{json_name}.ready", inbox / f"{stem}.ready")


def note_stability(state: dict, path: Path, now: float, window: float) -> bool:
    """Remember size and mtime. True only after they have stopped changing.

    A non-positive window claims on the first look, and still skips a later
    look whose size or mtime changed. That is how tests avoid sleeping.
    """
    try:
        stat = path.stat()
    except OSError:
        return False
    signature = (stat.st_size, stat.st_mtime_ns)
    previous = state.get(path.name)
    if previous is None:
        state[path.name] = (signature, now)
        return window <= 0
    if previous[0] != signature:
        state[path.name] = (signature, now)
        return False
    return (now - previous[1]) >= window


def claim_ready_jobs(
    inbox: Path,
    processing: Path,
    state: dict,
    *,
    now: float,
    stability_seconds: float,
    limit: int = 1,
    allow: Callable[[Path], bool] | None = None,
) -> list[Path]:
    """Move ready, stable JSON files from inbox to processing.

    The move happens before any Discord post. Files without a ready marker,
    and files whose size or mtime is still changing, stay in the inbox.
    """
    if not inbox.is_dir():
        return []
    try:
        entries = sorted(Path(inbox).iterdir(), key=lambda item: item.name)
    except OSError:
        log.exception("mts image jobs inbox could not be listed")
        return []

    present: set[str] = set()
    claimed: list[Path] = []
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            if entry.is_symlink() or not entry.is_file() or entry.suffix.lower() != ".json":
                continue
        except OSError:
            continue
        if not _JOB_NAME_RE.fullmatch(name):
            log.warning("mts image jobs ignored unsafe job name %s", name)
            continue
        present.add(name)
        stable = note_stability(state, entry, now, stability_seconds)
        if not has_ready_marker(inbox, name) or not stable:
            continue
        if len(claimed) >= limit:
            continue
        if allow is not None and not allow(entry):
            continue
        destination = processing / name
        try:
            processing.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                log.warning(
                    "mts image jobs left %s in the inbox because processing already has that name",
                    name,
                )
                continue
            entry.replace(destination)
        except OSError:
            log.exception("mts image jobs could not claim %s", name)
            continue
        remove_ready_markers(inbox, name)
        state.pop(name, None)
        claimed.append(destination)

    for stale in list(state):
        if stale not in present:
            state.pop(stale, None)
    return claimed


def relocate_file(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if target.exists():
        target = dest_dir / f"{src.stem}-{uuid.uuid4().hex[:8]}{src.suffix}"
    src.replace(target)
    return target


def relocate_directory(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    if dest.exists():
        dest = dest.with_name(f"{dest.name}-{uuid.uuid4().hex[:8]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)


def write_error_file(failed: Path, stem: str, reason: str) -> Path:
    failed.mkdir(parents=True, exist_ok=True)
    path = failed / f"{stem}.error.txt"
    path.write_text(reason.strip() + "\n", encoding="utf-8")
    return path


def fail_job(job_json: Path, failed: Path, reason: str, staged: Path | None = None) -> None:
    """Move the job (and any staged images) to failed/ and write the reason."""
    stem = job_json.stem
    if job_json.is_file():
        relocate_file(job_json, failed)
    if staged is not None and staged.exists():
        relocate_directory(staged, failed / staged.name)
    write_error_file(failed, stem, reason)


def finish_success(job_json: Path, done: Path, staged: Path | None) -> None:
    """Delete staged images and keep the JSON under done/."""
    if staged is not None and staged.exists():
        try:
            shutil.rmtree(staged)
        except OSError:
            log.exception("mts image jobs could not delete staged images for %s", job_json.stem)
    if job_json.is_file():
        relocate_file(job_json, done)


def quarantine_processing(processing: Path, failed: Path) -> list[str]:
    """Move leftover processing jobs to failed/ without posting them.

    A restart must not retry these. The worker may already have posted.
    """
    if not processing.is_dir():
        return []
    try:
        entries = sorted(processing.iterdir(), key=lambda item: item.name)
    except OSError:
        log.exception("mts image jobs could not list processing")
        return []
    moved: list[str] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if not _JOB_NAME_RE.fullmatch(entry.name):
                continue
        except OSError:
            continue
        stem = entry.stem
        sidecar = processing / stem
        try:
            relocate_file(entry, failed)
            if sidecar.is_dir() and not sidecar.is_symlink():
                relocate_directory(sidecar, failed / stem)
            write_error_file(failed, stem, INTERRUPTED_REASON)
            moved.append(stem)
        except OSError:
            log.exception("mts image jobs could not quarantine %s", entry.name)
    return moved


def resolve_image_file(inbox: Path, relative: str) -> Path:
    inbox_resolved = inbox.resolve()
    candidate = inbox.joinpath(*PurePosixPath(relative).parts).resolve()
    if candidate != inbox_resolved and inbox_resolved not in candidate.parents:
        raise JobError(f"image path escapes the job directory: {relative}")
    return candidate


def stage_upload(inbox: Path, staged: Path, names: tuple[str, ...]) -> list[tuple[str, bytes]]:
    """Move upload files under processing and return their bytes.

    One missing file, symlink, empty file, or file over the 10 MiB cap fails
    the whole job. Nothing is posted. Files that were present are parked in
    ``staged`` so the caller can move them to failed/ with the error.
    """
    resolved: list[tuple[str, Path]] = []
    problems: list[str] = []
    for name in names:
        try:
            path = resolve_image_file(inbox, name)
        except JobError as exc:
            problems.append(str(exc))
            continue
        try:
            if path.is_symlink() or not path.is_file():
                problems.append(f"missing image: {name}")
                continue
            size = path.stat().st_size
        except OSError:
            problems.append(f"missing image: {name}")
            continue
        if size <= 0:
            problems.append(f"image is empty: {name}")
        elif size > MAX_UPLOAD_BYTES:
            problems.append(
                f"image exceeds the {MAX_UPLOAD_BYTES} byte upload cap: {name} ({size} bytes)"
            )
        resolved.append((name, path))

    if problems:
        _park_files(resolved, staged)
        raise JobError("upload rejected: " + "; ".join(problems))

    blobs: list[tuple[str, bytes]] = []
    for name, path in resolved:
        destination = staged.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
        data = destination.read_bytes()
        if len(data) > MAX_UPLOAD_BYTES:
            raise JobError(
                f"image exceeds the {MAX_UPLOAD_BYTES} byte upload cap: {name} ({len(data)} bytes)"
            )
        blobs.append((name, data))
    return blobs


def _park_files(found: list[tuple[str, Path]], staged: Path) -> None:
    for name, path in found:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            destination = staged.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.replace(destination)
        except OSError:
            log.warning("mts image jobs could not park %s", name)


def discord_filename(name: str, index: int) -> str:
    relative = name.replace("\\", "/").replace("/", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", relative).strip("._")
    if not cleaned:
        cleaned = f"image-{index}.png"
    if len(cleaned) > 80:
        suffix = PurePosixPath(cleaned).suffix[:8]
        cleaned = f"{cleaned[:70]}{suffix}"
    return cleaned


def png_from_result(result: object) -> bytes:
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    png = getattr(result, "png_bytes", None)
    if isinstance(png, (bytes, bytearray)):
        return bytes(png)
    raise JobError("background generation returned no image bytes")


def generation_details(result: object) -> tuple[str, str, float]:
    """Positive prompt, negative prompt, and generation time from a worker result.

    Raw bytes (tests, older callers) have none of these. Empty and zero are fine.
    """
    if isinstance(result, (bytes, bytearray)):
        return "", "", 0.0
    positive = getattr(result, "positive", "")
    negative = getattr(result, "negative", "")
    gen_time = getattr(result, "gen_time", 0.0)
    if not isinstance(positive, str):
        positive = "" if positive is None else str(positive)
    if not isinstance(negative, str):
        negative = "" if negative is None else str(negative)
    try:
        gen_time = float(0.0 if gen_time is None else gen_time)
    except (TypeError, ValueError):
        gen_time = 0.0
    return positive, negative, gen_time


def channel_guild_id(channel) -> int:
    """Guild of the destination channel, or 0 when the channel has none.

    Moderator delete looks the role up from this id. A missing guild must not
    become a real server's id.
    """
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
        return 0
    return guild_id


async def post_image_batches(channel, blobs: list[tuple[str, bytes]], content: str) -> None:
    """Post at most 10 attachments per message. Mentions are disabled."""
    if not blobs:
        raise JobError("nothing to post")
    for start in range(0, len(blobs), DISCORD_ATTACHMENT_BATCH):
        batch = blobs[start:start + DISCORD_ATTACHMENT_BATCH]
        files = []
        for offset, (name, data) in enumerate(batch):
            if len(data) > MAX_UPLOAD_BYTES:
                raise JobError(
                    f"image exceeds the {MAX_UPLOAD_BYTES} byte upload cap: {name} ({len(data)} bytes)"
                )
            files.append(
                discord.File(BytesIO(data), filename=discord_filename(name, start + offset + 1))
            )
        message = content if start == 0 and content else None
        await channel.send(content=message, files=files, allowed_mentions=NO_MENTIONS)


async def generate_pngs(
    job: GenerateJob,
    image_cog,
    *,
    stem: str,
    retry_delay: float = QUEUE_RETRY_SECONDS,
    wait_seconds: float = QUEUE_WAIT_SECONDS,
    sleep: Callable = asyncio.sleep,
    guild_id: int | None = None,
) -> list[GeneratedImage]:
    """Enqueue one prompt at a time on the shared ImageDiffusion queue.

    The next prompt is not queued until this one has finished, so a batch
    cannot fill the cap ahead of /imagegen. A full queue waits and retries.
    ``llm_prompt_enhance`` stays off. This does not post. The caller posts
    only after every prompt has succeeded.
    """
    if not hasattr(image_cog, "enqueue_background") or not hasattr(image_cog, "image_queue"):
        raise JobError("ImageDiffusion cog has no shared image queue")

    loop = asyncio.get_running_loop()
    started = loop.time()
    pngs: list[GeneratedImage] = []
    for index, prompt in enumerate(job.prompts):
        future = loop.create_future()
        creation = ImageCreation(
            prompt,
            job.model,
            interaction=None,
            negative_prompt="",
            use_default_negative=True,
            use_default_positive=True,
            llm_prompt_enhance=False,
            result_future=future,
            output_width=job.width,
            output_height=job.height,
            guild_id=guild_id,
        )
        while True:
            if loop.time() - started > wait_seconds:
                raise JobError("timed out waiting for the shared image queue")
            startup_error = getattr(image_cog, "comfy_startup_error", None)
            if startup_error:
                raise JobError(f"Image generation is unavailable: {startup_error}")
            ready = getattr(image_cog, "comfy_ready", None)
            if ready is not None and not ready.is_set():
                await sleep(retry_delay)
                continue
            if image_cog.image_queue.full():
                await sleep(retry_delay)
                continue
            accepted = await image_cog.enqueue_background(creation)
            if not accepted:
                await sleep(retry_delay)
                continue
            break
        try:
            result = await future
        except Exception as exc:
            raise JobError(f"image generation failed: {exc}") from exc
        png = png_from_result(result)
        if len(png) > MAX_UPLOAD_BYTES:
            raise JobError(
                f"generated image exceeds the {MAX_UPLOAD_BYTES} byte upload cap "
                f"({len(png)} bytes)"
            )
        positive, negative, gen_time = generation_details(result)
        pngs.append(
            GeneratedImage(
                name=f"{stem}-{index + 1}.png",
                png_bytes=png,
                creation=creation,
                positive=positive,
                negative=negative,
                gen_time=gen_time,
            )
        )
        log.info(
            "mts image job generated %s prompt %s/%s model=%s",
            stem,
            index + 1,
            len(job.prompts),
            job.model.value,
        )
    return pngs


async def post_generated_images(channel, job: GenerateJob, generated: list[GeneratedImage]) -> None:
    """Post one /imagegen-style message per image. Mentions stay off.

    Nothing is sent until every image is known to fit the upload cap. A later
    failure therefore posts nothing from this function when the check fails
    first. Title and caption, when present, sit above the /imagegen line on
    every message.
    """
    if not generated:
        raise JobError("nothing to post")
    for item in generated:
        if len(item.png_bytes) > MAX_UPLOAD_BYTES:
            raise JobError(
                f"generated image exceeds the {MAX_UPLOAD_BYTES} byte upload cap "
                f"({len(item.png_bytes)} bytes)"
            )
    prefix = message_content(job.title, job.caption)
    guild_id = channel_guild_id(channel)
    for item in generated:
        await post_generation_result(
            channel,
            item.creation,
            png_bytes=item.png_bytes,
            positive=item.positive,
            negative=item.negative,
            gen_time=item.gen_time,
            author_id=INBOX_AUTHOR_ID,
            guild_id=guild_id,
            allowed_mentions=NO_MENTIONS,
            attribution=GENERATE_ATTRIBUTION,
            content_prefix=prefix,
        )


def _image_cog(bot):
    getter = getattr(bot, "get_cog", None)
    if getter is None:
        return None
    return getter("ImageDiffusion")


def _generation_can_start(image_cog) -> bool:
    if image_cog is None:
        return True
    ready = getattr(image_cog, "comfy_ready", None)
    if ready is not None and not ready.is_set():
        return False
    if getattr(image_cog, "comfy_startup_error", None):
        return False
    return True


async def _open_channel(bot, channel_id: int, fetch_channel: Callable | None):
    if fetch_channel is not None:
        channel = fetch_channel(channel_id)
        if asyncio.iscoroutine(channel):
            channel = await channel
        return channel
    getter = getattr(bot, "get_channel", None)
    channel = getter(channel_id) if getter else None
    if channel is not None:
        return channel
    fetch = getattr(bot, "fetch_channel", None)
    if fetch is None:
        raise JobError(f"channel {channel_id} is not available")
    return await fetch(channel_id)


async def execute_claimed_job(
    job_path: Path,
    dirs: dict[str, Path],
    settings: MtsImageJobsConfig,
    bot,
    *,
    fetch_channel: Callable | None = None,
    retry_delay: float = QUEUE_RETRY_SECONDS,
    wait_seconds: float = QUEUE_WAIT_SECONDS,
    sleep: Callable = asyncio.sleep,
) -> None:
    """Validate, generate or read files, post, then archive. Never posts twice."""
    staged = dirs["processing"] / job_path.stem
    try:
        payload = read_job_payload(job_path)
        job = parse_job(payload, default_model=settings.default_model)
        channel_id = resolve_post_channel(int(settings.channel_id), job.channel_id)
        channel = await _open_channel(bot, channel_id, fetch_channel)
        if channel is None:
            raise JobError(f"channel {channel_id} is not available")
        if isinstance(job, GenerateJob):
            image_cog = _image_cog(bot)
            if image_cog is None:
                raise JobError("ImageDiffusion cog is not loaded")
            generated = await generate_pngs(
                job,
                image_cog,
                stem=job_path.stem,
                retry_delay=retry_delay,
                wait_seconds=wait_seconds,
                sleep=sleep,
                guild_id=channel_guild_id(channel),
            )
            await post_generated_images(channel, job, generated)
            posted_count = len(generated)
        else:
            blobs = stage_upload(dirs["inbox"], staged, job.images)
            await post_image_batches(channel, blobs, message_content(job.title, job.caption))
            posted_count = len(blobs)
        finish_success(job_path, dirs["done"], staged if staged.exists() else None)
        log.info(
            "mts image job posted name=%s type=%s files=%s",
            job_path.stem,
            job.kind,
            posted_count,
        )
    except JobError as exc:
        log.warning("mts image job failed name=%s reason=%s", job_path.stem, exc)
        fail_job(job_path, dirs["failed"], str(exc), staged if staged.exists() else None)
    except Exception as exc:
        log.exception("mts image job failed name=%s", job_path.name)
        fail_job(
            job_path,
            dirs["failed"],
            f"unexpected error: {exc}",
            staged if staged.exists() else None,
        )


async def process_once(
    settings: MtsImageJobsConfig,
    *,
    bot,
    state: dict,
    now: float,
    stability_seconds: float = STABILITY_SECONDS,
    retry_delay: float = QUEUE_RETRY_SECONDS,
    wait_seconds: float = QUEUE_WAIT_SECONDS,
    sleep: Callable = asyncio.sleep,
    fetch_channel: Callable | None = None,
) -> int:
    """Handle at most one ready inbox job. Disabled settings claim nothing."""
    dirs = ensure_job_dirs(settings.resolved_inbox_dir())
    if not settings.enabled:
        log.debug("mts image jobs watcher is off")
        return 0

    def allow(path: Path) -> bool:
        if peek_job_type(path) != "generate":
            return True
        image_cog = _image_cog(bot)
        if _generation_can_start(image_cog):
            return True
        log.info("mts image jobs left %s in the inbox until image generation is available", path.name)
        return False

    claimed = claim_ready_jobs(
        dirs["inbox"],
        dirs["processing"],
        state,
        now=now,
        stability_seconds=stability_seconds,
        limit=1,
        allow=allow,
    )
    if not claimed:
        return 0
    await execute_claimed_job(
        claimed[0],
        dirs,
        settings,
        bot,
        fetch_channel=fetch_channel,
        retry_delay=retry_delay,
        wait_seconds=wait_seconds,
        sleep=sleep,
    )
    return 1


async def watcher_tick(bot, state: dict, **kwargs) -> int:
    """Re-read config, then process one job. Used by the cog and by tests."""
    return await process_once(current_settings(), bot=bot, state=state, **kwargs)


class MtsImageJobs(Cog):
    """Posts inbox image jobs. The loop no-ops while the feature is disabled."""

    def __init__(self, bot):
        self.bot = bot
        self._state: dict = {}
        self._quarantined = False
        self.watch_inbox.start()

    def cog_unload(self) -> None:
        self.watch_inbox.cancel()

    async def cog_load(self) -> None:
        self.quarantine_leftovers()

    def quarantine_leftovers(self) -> None:
        try:
            settings = current_settings()
            dirs = ensure_job_dirs(settings.resolved_inbox_dir())
            quarantine_processing(dirs["processing"], dirs["failed"])
            self._quarantined = True
        except Exception:
            log.exception("mts image jobs could not quarantine processing")

    @tasks.loop(seconds=POLL_SECONDS)
    async def watch_inbox(self) -> None:
        try:
            if not self._quarantined:
                self.quarantine_leftovers()
            await watcher_tick(self.bot, self._state, now=time.monotonic())
        except Exception:
            log.exception("mts image jobs tick failed")

    @watch_inbox.before_loop
    async def before_watch_inbox(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MtsImageJobs(bot))
