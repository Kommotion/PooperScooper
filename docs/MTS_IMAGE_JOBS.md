# MTS image jobs

The bot can watch a folder for image jobs and post the results on its own. There is no approval step and no new slash command.

This is **off** unless `mts_image_jobs.enabled` is true. The default channel is the support test channel. It does **not** post in MTS #image-diffusion.

## Inbox

On this machine the inbox is:

`C:\Users\jacqu\Desktop\PooperScooper\data\mts_image_jobs\inbox\`

That path is the default `inbox_dir` (`data/mts_image_jobs/inbox`) resolved from the repo root. The cog creates these siblings if they are missing:

| Folder | Role |
| --- | --- |
| `inbox\` | New jobs. Only a finished JSON with a ready marker is claimed. |
| `processing\` | Claimed jobs, moved here **before** any Discord post. |
| `done\` | JSON after a successful post. Image files are deleted. |
| `failed\` | JSON, an error file, and any images kept for debugging. |

Job payloads are gitignored (`data/mts_image_jobs/.gitignore`). The `.gitkeep` files in those folders can stay in git. Do not commit JSON, images, `.ready` markers, or `*.error.txt` files.

Job file names are a single file in the inbox, not in a subfolder: letters, digits, `.`, `_`, and `-`, ending in `.json`. Example: `red-pandas.json`.

## How a producer should drop a job

Copy from the shared box in this order:

1. Copy the image files into the inbox (or into a subfolder of the inbox) first.
2. Copy the finished JSON file.
3. Copy the ready marker **last**.

The marker is either `red-pandas.json.ready` or `red-pandas.ready` (an empty file is fine). The job is ignored until one of those exists.

The watcher also waits until the JSON size and modification time have stayed still for about a second (`STABILITY_SECONDS`). A file that is still being copied is left in the inbox. Writing the marker last is the signal that the copy is finished. Do not reuse a job file name that still has an old marker sitting next to it.

The cog looks about every 2 seconds. It claims at most one job per look.

## Enable

Add this to `config.json` (the live file is not in git; `config.example.json` shows the same keys with the feature off):

```json
"mts_image_jobs": {
  "enabled": true,
  "channel_id": 1045149015756521493,
  "inbox_dir": "data/mts_image_jobs/inbox",
  "default_model": "anime_wai_illustrious"
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Missing key or `false`: the cog loads, and the watcher does not claim jobs. |
| `channel_id` | `1045149015756521493` | Support test channel. Every job posts here. |
| `inbox_dir` | `data/mts_image_jobs/inbox` | Relative to the repo root, or an absolute path. |
| `default_model` | `anime_wai_illustrious` | Used when a generate job omits `model`. WAI Illustrious. |

To turn it on: set `enabled` to `true` and restart the bot. No slash-command sync. Leave `channel_id` on `1045149015756521493` until Nick explicitly allows the production channel.

The watcher reads the flag on each tick. The config loader keeps the file it read at startup, so a restart is how you enable it.

## Channels

- Test channel: `1045149015756521493`
- Production MTS #image-diffusion: `1373140173067653120`

Production is hard-refused. The constant is `MTS_PRODUCTION_IMAGE_CHANNEL_ID` in `cogs/mts_image_jobs.py`, with a comment that production can be enabled later. A config `channel_id` of that value, or a job that names it, fails the job (or is refused before post) and nothing is sent there.

Jobs should omit `channel_id`. If a job includes one, it must be exactly the configured channel. Any other id fails the job and does not post.

## Generate jobs

Each prompt goes through the same ComfyUI path as `/imagegen`, on the shared `ImageDiffusion.image_queue` (max 30). The cog enqueues **one prompt at a time** and waits until that image is back before enqueueing the next. If the queue is full, it waits and retries. It does not dump the whole list into the queue, so `/imagegen` can interleave.

A generate job stays in the inbox until ComfyUI is ready. It is not claimed early, so a restart does not mark it interrupted before it has started. Upload jobs do not wait on ComfyUI.

`llm_prompt_enhance` is not a job field and stays off. `krea2_identity_edit` is rejected because it needs a source image. Any other model id that is not a canonical `/imagegen` id fails the job. The removed Z-Image id is not accepted here (it is not the canonical value).

`width` and `height` are optional and must be set together. Each must be a multiple of 64 from 64 through 2048. The apply path writes those numbers onto standard empty-latent nodes (`EmptyLatentImage`, `EmptySD3LatentImage`, `EmptyFlux2LatentImage`). If the graph has none (Qwen-Image 2.1 stores a single `resolution` instead), the cog logs that and uses the workflow's own size. The job still runs.

A later prompt failure fails the whole job and posts nothing. Images are posted only after every prompt has succeeded, in batches of at most 10.

```json
{
  "type": "generate",
  "prompts": [
    "a red panda in a raincoat, studio lighting",
    "the same red panda asleep on a stack of books"
  ],
  "model": "anime_wai_illustrious",
  "width": 1024,
  "height": 1024,
  "title": "Red panda set",
  "caption": "Two poses from the shared inbox."
}
```

Minimal generate (default model, workflow size, no caption):

```json
{
  "type": "generate",
  "prompts": ["a ceramic cup of tea on a wooden table"]
}
```

## Upload jobs

Images are posted as-is. No ComfyUI. Names are relative to the inbox (the job file's directory): `panda-01.png` or `extras/panda-02.png`. Absolute paths, `..`, and symlinks are rejected. Allowed types: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`.

Discord gets at most 10 attachments per message. Eleven images become two messages (10, then 1). The title and caption go on the first message.

The per-file cap is 10 MiB (`MAX_UPLOAD_BYTES`, 10485760), the default bot upload limit. One file over that cap, or one missing or empty file, **fails the whole job**. Nothing is posted.

```json
{
  "type": "upload",
  "images": [
    "panda-01.png",
    "extras/panda-02.png"
  ],
  "title": "Finished pandas",
  "caption": "Posted as-is from the inbox."
}
```

## Posts

Every post uses `discord.AllowedMentions.none()`. Role, user, and `@everyone` pings in the title or caption are not mentions.

## Success, failure, and restarts

1. A ready, stable JSON is moved from `inbox\` to `processing\` before any post. That move is the claim.
2. Success: images are deleted, and the JSON is moved to `done\`. The inbox is not left holding those images.
3. Failure (bad JSON, bad schema, bad channel, missing image, oversize file, generation error, Discord error): the JSON goes to `failed\`, and `jobname.error.txt` says why. Images that were already collected are moved under `failed\` with the job so the inbox does not keep them.
4. On cog start, anything still in `processing\` is moved to `failed\` with an **interrupted** error and is **not** posted again. It may already have gone to Discord before the process stopped.

A disabled watcher does not claim inbox jobs. Turning the flag on later (and restarting) will pick up jobs that are still in the inbox with a ready marker.

If Discord accepts the first batch of a multi-message post and then errors, the job is failed and not retried, so the first batch is not sent a second time. A later batch may be missing. Check `failed\` and the channel before you drop the job again under a new file name.

Invalid JSON or a schema error never posts. Unknown fields are rejected, including `llm_prompt_enhance`.
