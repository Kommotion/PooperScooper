# MTS image jobs

The bot can watch a folder for image jobs and post the results on its own. There is no approval step and no new slash command.

This is **off** unless `mts_image_jobs.enabled` is true. Jobs post only to the configured `channel_id`. The example and the default are the support test channel. MTS #image-diffusion (`1373140173067653120`) is used when `channel_id` is set to that id. Switching to production is a config change, not a code hard-block.

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
| `channel_id` | `1045149015756521493` | The only channel jobs post to. The example and the default are the support test channel. Set this to `1373140173067653120` to post in MTS #image-diffusion. |
| `inbox_dir` | `data/mts_image_jobs/inbox` | Relative to the repo root, or an absolute path. |
| `default_model` | `anime_wai_illustrious` | Used when a generate job omits `model`. WAI Illustrious. |

To turn it on: set `enabled` to `true` and restart the bot. No slash-command sync. The example above keeps `channel_id` on the support test channel `1045149015756521493`. To post in MTS #image-diffusion, change `channel_id` to `1373140173067653120`. That switch is a config change of `channel_id`, not a code hard-block.

The watcher reads the flag on each tick. The config loader keeps the file it read at startup, so a restart is how you enable it.

## Channels

- Support test channel (example and default): `1045149015756521493`
- Production MTS #image-diffusion: `1373140173067653120`

Jobs post only to the configured `channel_id`. That may be the support test channel or MTS #image-diffusion when config is set to `1373140173067653120`. The constant `MTS_PRODUCTION_IMAGE_CHANNEL_ID` in `cogs/mts_image_jobs.py` is that production id. Switching to production is a config change of `channel_id`, not a code hard-block.

A job that names the production id while config is still the test channel fails as a mismatch, and nothing is posted. A job that names the test id while config is the production channel fails the same way.

Jobs should omit `channel_id`. If a job includes one, it must be exactly the configured channel. Any other id fails the job and does not post.

## Generate jobs

Each prompt goes through the same ComfyUI path as `/imagegen`, on the shared `ImageDiffusion.image_queue` (max 30). The cog enqueues **one prompt at a time** and waits until that image is back before enqueueing the next. If the queue is full, it waits and retries. It does not dump the whole list into the queue, so `/imagegen` can interleave.

A generate job stays in the inbox until ComfyUI is ready. It is not claimed early, so a restart does not mark it interrupted before it has started. Upload jobs do not wait on ComfyUI.

`llm_prompt_enhance` is not a job field and stays off. `krea2_identity_edit` is rejected because it needs a source image. Any other model id that is not a canonical `/imagegen` id fails the job. The removed Z-Image id is not accepted here (it is not the canonical value).

`width` and `height` are optional and must be set together. Each must be a multiple of 64 from 64 through 2048. The apply path writes those numbers onto standard empty-latent nodes (`EmptyLatentImage`, `EmptySD3LatentImage`, `EmptyFlux2LatentImage`). If the graph has none (Qwen-Image 2.1 stores a single `resolution` instead), the cog logs that and uses the workflow's own size. The job still runs.

A later prompt failure fails the whole job and posts nothing. Images are posted only after every prompt has succeeded. Each image is its own Discord message, in the same shape as `/imagegen generate`:

- One PNG named `generated.png`
- The public line `**{preview} — `{model}` — Requested by MTS Party Prompts**`
- When the prompt is longer than 160 characters, a second line: `-# Full prompt in **See prompt details**`
- The same buttons as `/imagegen`: **See prompt details**, **Edit and Regenerate**, **Regenerate**, and **Delete**

A job `title` and `caption`, when present, go on their own lines above that line (title, then caption), on every image. The attribution is plain text. Nobody is mentioned. Delete is not tied to a Discord user (`author_id` is 0), so a random clicker cannot delete the post. An image-diffusion moderator for the destination guild still can.

See prompt details is an ephemeral embed (model, prompts, flags, NSFW level, LLM prompt enhance, generation time). Edit and Regenerate and Regenerate run as the person who clicked, on the normal `/imagegen` queue. `llm_prompt_enhance` stays off for the inbox job itself. One image over the 10 MiB cap fails the whole job and posts nothing.

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

Every inbox post uses `discord.AllowedMentions.none()`. Role, user, and `@everyone` text in the title or caption is not a mention.

Upload jobs send batches of at most 10 files. The title and caption go on the first message only, and there are no buttons.

Generate jobs send one `/imagegen`-style message per image, as described above. Mentions stay off on those messages too. The credit line says `Requested by MTS Party Prompts` and does not mention a user.

## Success, failure, and restarts

1. A ready, stable JSON is moved from `inbox\` to `processing\` before any post. That move is the claim.
2. Success: images are deleted, and the JSON is moved to `done\`. The inbox is not left holding those images.
3. Failure (bad JSON, bad schema, bad channel, missing image, oversize file, generation error, Discord error): the JSON goes to `failed\`, and `jobname.error.txt` says why. Images that were already collected are moved under `failed\` with the job so the inbox does not keep them.
4. On cog start, anything still in `processing\` is moved to `failed\` with an **interrupted** error and is **not** posted again. It may already have gone to Discord before the process stopped.

A disabled watcher does not claim inbox jobs. Turning the flag on later (and restarting) will pick up jobs that are still in the inbox with a ready marker.

If Discord accepts an earlier message and then errors, the job is failed and not retried, so that message is not sent a second time. A later upload batch, or a later generated image, may be missing. Check `failed\` and the channel before you drop the job again under a new file name.

Invalid JSON or a schema error never posts. Unknown fields are rejected, including `llm_prompt_enhance`.
