# Image Generation (ComfyUI) — Agent Guide

This document explains how PooperScooper's Discord image generation works, how to add new models, and what memory safeguards exist today.

**Primary files:**

| File | Role |
|------|------|
| `cogs/imagediffusion.py` | Discord commands, queue, UI modals, health watcher |
| `cogs/utils/image_models.py` | Model enum, workflow classes, prompt patching, generation |
| `cogs/utils/comfy_client.py` | ComfyUI process management, API calls, `/free` memory |
| `ComfyUI_New/` | **Unified** ComfyUI install (port **8188**) for Discord bot + Comfy Desktop |
| `ComfyUI_New/models/` | Single model store (checkpoints, diffusion_models, text_encoders, vae, …) |
| `ComfyUI_New/user/default/workflows/` | UI + API workflow JSON (Desktop workflows junction here too) |
| `config.json` → `comfyui` | `host`, `port`, `root`, `auto_start` for the bot |

### One ComfyUI, two clients

```
ComfyUI_New (server on :8188)
  ├── models/          ← only place to put weights
  └── user/.../workflows/  ← Desktop + bot both use this folder

Discord bot  ──HTTP──►  :8188   (starts server if down, or attaches if up)
Comfy Desktop ────────►  :8188   (launchArgs --port 8188; modelsDirs → ComfyUI_New/models)
Browser UI   ─────────►  http://127.0.0.1:8188
```

- **Do not** maintain a second model tree under `Documents\ComfyUI\models` for daily use.
- Desktop’s workflow list is a **junction** to `ComfyUI_New\user\default\workflows`.
- If Desktop rewrites `shared_model_paths.yaml`, set models dir back to `ComfyUI_New\models` only.
- Env overrides: `COMFYUI_HOST`, `COMFYUI_PORT`, `COMFYUI_ROOT`, `COMFYUI_AUTO_START`.

**Current models** (`/imagegen generate` order). Anime choices are labeled `[Anime][NSFW]`. SFW realism choices are labeled `[Realism][SFW]`. LUSTIFY is labeled `[Realism][NSFW]`. Anime comes first, then Realism. Inside Realism the order is Krea 2 Turbo, FLUX 3 Klein INT8, RedCraft, Qwen-Image 2.1, then LUSTIFY. Identity Edit is edit-only and is not a generate choice or a pref-view option.

| Enum value | Class | Workflow JSON | Notes |
|------------|-------|---------------|--------|
| `anime_wai_illustrious` | `ComfyWaiIllustriousModel` | `WAI-ILLUSTRIOUS-SDXL_API.json` | SDXL. Label `[Anime][NSFW] WAI Illustrious` |
| `anima_aesthetic` | `ComfyAnimaAestheticModel` | `ANIMA_AESTHETIC_API.json` | Base Anima aesthetic v1.1. Label `[Anime][NSFW] Anima Aesthetic` |
| `one_obsession_illustrious` | `ComfyOneObsessionIllustriousModel` | `ONE_OBSESSION_ILLUSTRIOUS_API.json` | Label `[Anime][NSFW] One Obsession Illustrious` |
| `one_obsession_anima` | `ComfyOneObsessionAnimaModel` | `ONE_OBSESSION_ANIMA_API.json` | One Obsession Anima v5.0 (v5 turbo). Label `[Anime][NSFW] One Obsession Anima` |
| `krea2_turbo` | `ComfyKrea2TurboModel` | `KREA2_TURBO_INT8_API.json` / `_ENHANCE` | INT8 Turbo. Label `[Realism][SFW] Krea 2 Turbo` |
| `flux3_klein_int8` | `ComfyFlux3KleinInt8Model` | `KLEIN_API.json` | Distilled INT8, 16 steps, cfg 1. No prompt upsampler (8 GB VRAM). Label `[Realism][SFW] FLUX 3 Klein INT8`. Before RedCraft. |
| `redcraft` | `ComfyRedCraftModel` | `REDCRAFT_KREA2_API.json` / `_ENHANCE` | RedCraft 2.3 Krea2 INT8/INT4/FP8. Label `[Realism][SFW] RedCraft` |
| `qwen_image_21` | `ComfyQwenImage21Model` | `QWEN_IMAGE_21_TURBO_INT8_API.json` | Official Comfy-Org turbo INT8, 8 steps, cfg 1, `TextEncodeQwenImage21`, w4a8 8B encoder, 2.1 VAE, no prompt enhancer, 8 GB VRAM. Label `[Realism][SFW] Qwen-Image 2.1`. Last SFW realism generate model. |
| `lustify` | `ComfyLustifyKrea2Model` | `LUSTIFY_KREA2_API.json` | LUSTIFY! v10 Krea 2 Turbo INT8, NSFW photoreal, 8 steps, cfg 1, same CLIP/VAE as Krea 2 Turbo (`qwen3vl_4b_fp8_scaled` + `qwen_image_vae`). Label `[Realism][NSFW] LUSTIFY`. Last generate model, after the SFW realism models. Not in the Krea LLM-enhance family. |
| `krea2_identity_edit` | `ComfyKrea2IdentityEditModel` | `KREA2_IDENTITY_EDIT_API.json` | Instruction edit + source image. Not a generate choice. |

**Removed from Discord:** WAI ANIMA (`anime_wai_anima`) — replaced by base **Anima Aesthetic**. Saved values `anime_wai_anima` and `wai_anima` still map to Anima Aesthetic. Z-Image Turbo (`z_image_turbo_fp8`, formerly `ComfyZImageTurboModel`) was removed from Discord `/imagegen`. A saved or remembered value `z_image_turbo_fp8` (and the old member name `Z_IMAGE_TURBO_FP8`) maps to WAI Illustrious (`anime_wai_illustrious`). The leftover gitignored workflow `Z_IMAGE_TURBO_FP8_API.json` may still exist on disk but is unused by the bot.

**Krea LLM enhance:** slash `/imagegen generate` option `llm_prompt_enhance` (default **No**). Pref-view button still available for regenerate flow. When Yes + Krea-family (Krea 2 Turbo, RedCraft), uses `TextGenerate` before sampling. FLUX 3 Klein, Qwen-Image 2.1, and LUSTIFY are not in that set. LUSTIFY has no enhance workflow.

**FLUX 3 Klein INT8:** `/imagegen generate` choice after Krea 2 Turbo and before RedCraft. Natural-language captions (empty default positive and negative). Workflow `KLEIN_API.json` is the official distilled INT8 path: 16 steps, cfg 1, euler/simple. The optional official 9B prompt upsampler is omitted so the model fits in 8 GB VRAM.

**Qwen-Image 2.1:** `/imagegen generate` choice among SFW realism models (after RedCraft, before LUSTIFY). Natural-language captions (empty default positive and negative). Workflow `QWEN_IMAGE_21_TURBO_INT8_API.json` is the official Comfy-Org turbo INT8 path: 8 steps, cfg 1, euler/simple, `TextEncodeQwenImage21`, optional `QwenImage21Cache`, CLIP type `qwen_image`, `qwen3vl_8b_w4a8` encoder, `qwen_image_2.1` VAE. No 9B prompt enhancer, so it fits in 8 GB VRAM. Qwen is not in the Krea LLM-enhance family.

**LUSTIFY! v10:** `/imagegen generate` choice `[Realism][NSFW] LUSTIFY`, last in the generate dropdown, after every SFW realism model (immediately after `[Realism][SFW] Qwen-Image 2.1`). Natural-language captions (empty default positive and negative). Workflow `LUSTIFY_KREA2_API.json` is the Krea 2 Turbo INT8 graph with the UNET swapped to `lustify-v10-krea-turbo-int8_convrot.safetensors`: NSFW photoreal, 8 steps, cfg 1, euler/simple. Same CLIP/VAE as Krea 2 Turbo (`qwen3vl_4b_fp8_scaled`, type `krea2`, `qwen_image_vae`). Node 6 is `CLIPTextEncode` (positive). Node 7 is `ConditioningZeroOut`, so the base prompt patcher leaves it alone. No enhance workflow, so LUSTIFY is not in the Krea LLM-enhance family. Fits 8 GB VRAM (smoke-tested). `/imagegen edit` stays Identity Edit only.

**RedCraft weights:** `ComfyUI_New/models/diffusion_models/redcraft23INT8INT4FP8_30Krea2.safetensors` (same CLIP/VAE as Krea 2: `qwen3vl_4b_fp8_scaled` + `qwen_image_vae`).

**Identity Edit:** `/imagegen edit` — attach image + instruction. Needs custom node pack `comfyui-krea2edit` and LoRA `models/loras/Krea2/krea2_identity_edit_v1_2.safetensors`. Bot uploads the attachment to ComfyUI `/upload/image`, then runs Turbo INT8 + Identity Edit LoRA (10 steps, CFG 1). See `docs/KREA2_IDENTITY_EDIT_GUIDE.md`.

**Privacy / retention:** User edit uploads and all bot-generated Comfy `input`/`output` files are **deleted from disk** after the job (download → Discord). Unique temp upload names (`bot_edit_<uuid>.png`). Source bytes are not kept for Identity Edit regenerate — re-upload via `/imagegen edit`. Images live on Discord only after send.

---

## Architecture overview

```
Discord /imagegen → imagediffusion.py (queue)
                         ↓
              image_models.py (patch workflow JSON, post prompt)
                         ↓
              comfy_client.py → ComfyUI HTTP API (:8188)
                         ↓
              ComfyUI loads weights, runs graph, returns image
```

- Workflows are **JSON graphs** exported from ComfyUI in **API format** (not the UI canvas format).
- The bot does **not** load GPU weights itself; ComfyUI does all inference.
- Generations run **one at a time** through an `asyncio.Queue` (max 30 pending jobs).

---

## Adding a new model

### Step 1 — Install weights in ComfyUI

Place files under `ComfyUI_New/models/`:

| Asset | Folder |
|-------|--------|
| Checkpoint / diffusion model | `models/checkpoints/` |
| VAE | `models/vae/` |
| CLIP / text encoder | `models/text_encoders/` |
| LoRA | `models/loras/` |
| Upscaler | `models/upscale_models/` |

Verify the workflow runs manually in the ComfyUI web UI before wiring it into the bot.

### Step 2 — Export an API workflow

1. Build the pipeline in ComfyUI (`http://127.0.0.1:8188`).
2. **File → Save (API Format)** (or Export API).
3. Save to `ComfyUI_New/user/default/workflows/MY_MODEL_API.json`.

#### Required node IDs (important)

`BaseComfyWorkflowModel._apply_prompts_to_workflow()` in `image_models.py` patches these nodes by ID:

| Node ID | `class_type` | What the bot sets |
|---------|--------------|-------------------|
| `3` | `KSampler` | Random seed each run |
| `6` | `CLIPTextEncode` | Positive prompt |
| `7` | `CLIPTextEncode` | Negative prompt |

**If your workflow uses different node IDs**, either renumber nodes in ComfyUI before export, or subclass `BaseComfyWorkflowModel` and override `_apply_prompts_to_workflow()`.

Existing workflows for reference:

- `WAI-ILLUSTRIOUS-SDXL_API.json` — SDXL checkpoint loader, nodes 3/6/7
- `WAI-ANIMA_API.json` — separate CLIP + VAE loaders, nodes 3/6/7
- `Z_IMAGE_TURBO_FP8_API.json` — leftover/unused by the bot (removed from Discord `/imagegen`; the gitignored file may still be on disk)
- `QWEN_IMAGE_21_TURBO_INT8_API.json` — `TextEncodeQwenImage21` for the prompt and negative prompt (not CLIPTextEncode nodes 6/7); KSampler is still node 3
- `KLEIN_API.json` — CLIPTextEncode nodes 6/7, distilled INT8, no prompt upsampler
- `LUSTIFY_KREA2_API.json` — CLIPTextEncode node 6 (positive); node 7 is `ConditioningZeroOut` (no negative encode); KSampler node 3; UNET `lustify-v10-krea-turbo-int8_convrot.safetensors`; same CLIP/VAE as Krea 2 Turbo

### Step 3 — Register in `cogs/utils/image_models.py`

```python
# Constants (top of file)
MY_MODEL_WORKFLOW_PATH = r".\ComfyUI_New\user\default\workflows\MY_MODEL_API.json"
MY_MODEL_POSITIVE = "masterpiece, best quality"
MY_MODEL_NEGATIVE = "worst quality, low quality"

# Models enum
class Models(StrEnum):
    ...
    MY_MODEL = "my_model_slug"   # value used in slash commands

# Model class
class ComfyMyModel(BaseComfyWorkflowModel):
    def __init__(self):
        super().__init__(
            name=Models.MY_MODEL,
            workflow_path=MY_MODEL_WORKFLOW_PATH,
            default_pos=MY_MODEL_POSITIVE,
            default_neg=MY_MODEL_NEGATIVE,
        )
```

The `Models` enum is used as the Discord slash-command choice type. New enum members appear in `/imagegen generate` automatically after reload.

### Step 4 — Wire up in `cogs/imagediffusion.py`

**A. Boot preload list** (`models_to_load_on_boot`):

```python
self.models_to_load_on_boot = [
    ...
    Models.MY_MODEL,
]
```

**B. Model registry** (`self.models` dict):

```python
self.models = {
    ...
    Models.MY_MODEL: ComfyMyModel(),
}
```

**C. Modal dropdown** (class `ImageGenModal`, ~line 72):

```python
discord.SelectOption(
    label="My Model Display Name",
    value=Models.MY_MODEL,
    description="Short description for users",
    default=(default_model == Models.MY_MODEL),
),
```

### Step 5 — Reload

```
!reload_cog imagediffusion
```

Or restart the bot. ComfyUI does **not** need a restart for a new workflow JSON, but it **does** need the weight files present before the first generation.

### Checklist for agents

- [ ] Weights exist under `ComfyUI_New/models/`
- [ ] Workflow runs in ComfyUI UI
- [ ] API JSON saved to `user/default/workflows/`
- [ ] Nodes 3, 6, 7 are KSampler + two CLIPTextEncode (or override patching)
- [ ] `Models` enum entry added
- [ ] `Comfy*Model` class added
- [ ] `models_to_load_on_boot`, `self.models`, and modal `SelectOption` updated
- [ ] Cog reloaded / bot restarted
- [ ] Test via `/imagegen generate` and check `ComfyUI_New/user/comfyui_8188.log`

---

## Memory and VRAM management

Target GPU in production: **NVIDIA RTX A4000 Laptop (8 GB VRAM)**. ComfyUI runs in **NORMAL_VRAM** mode with async weight offloading.

### What the bot does today

| Mechanism | Location | Behavior |
|-----------|----------|----------|
| **Unload on model switch** | `imagediffusion.py` → `comfy_client.free_memory()` | Before processing a queued job whose model differs from `active_model`, calls ComfyUI `POST /free` with `unload_models: true` and `free_memory: true` |
| **Serial queue** | `image_generation` task loop | Only one generation runs at a time |
| **Queue cap** | `asyncio.Queue(maxsize=IMAGE_QUEUE_MAXSIZE)` (30) | Rejects new jobs with "Queue is full" when 30 are pending |
| **Generation timeout** | `_image_generation_interaction` | 600 s `asyncio.wait_for` around `model.generate()` |
| **Health watcher** | `comfy_health_watcher` (30 s) | Restarts ComfyUI if `/system_stats` fails (skipped while `_generation_in_progress`) |
| **Hybrid ComfyUI launch** | `comfy_client.ensure_comfy_running()` | Uses external instance on `:8188` if already up; otherwise bot starts `ComfyUI_New/main.py` |

Model-switch unload logic:

```python
# cogs/imagediffusion.py (image_generation loop)
if self.active_model != image.model:
    comfy_client.free_memory()
self.active_model = image.model
```

### What ComfyUI does on its own

- Partial model loading / offloading to CPU (`lowvram patches` in logs)
- Per-job weight load and unload driven by the graph
- VAE, CLIP, and UNet loaded only when nodes require them

### What is NOT implemented

- No VRAM usage monitoring or pre-flight checks before queueing
- No automatic resolution or step reduction under memory pressure
- No model compatibility matrix (which models can follow which without OOM)
- No queue prioritization by estimated VRAM cost

### Practical guidance (8 GB VRAM)

- Switching between SDXL (Illustrious) and Anima works because of `/free`, but each swap costs **~10–20 s** of reload time.
- ANIMA runs ~**45 s** per 1024×1024 / 30-step job; Illustrious ~**15 s**.
- Prefer official **INT8 / W4A8** quants when available (FLUX 3 Klein, Krea 2, Qwen-Image 2.1, LUSTIFY v10).
- Avoid heavy upscale nodes in the default workflow unless needed (`RealESRGAN` custom node requires `basicsr`).
- Keep resolution at **1024×1024** or lower for new models unless tested.

### Owner diagnostics

`/imagegen status` (bot owner only) reports:

- ComfyUI running / source (`external` vs `bot_started`)
- Host, port, PID (if bot-started)
- Queue depth (`current/30`)

Logs:

- `ComfyUI_New/user/comfyui_8188.log` — ComfyUI runtime (model loads, errors)
- `ComfyUI_New/comfyui_bot.log` — stderr when bot starts ComfyUI
- `pooperscooper.log` — bot queue and generation timing

---

## ComfyUI operations (for agents)

### Default paths and port

- Root: `ComfyUI_New/`
- Port: **8188**
- Entry: `ComfyUI_New/main.py`

### Starting / recovering ComfyUI

The bot calls `ensure_comfy_running()` on startup and every 30 s when down.

`start_comfy()` sets `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` to avoid Windows `cp1252` crashes when custom nodes log emoji. If `comfyui_bot.log` is locked, it falls back to a timestamped log file.

**Known issue:** If an old ComfyUI process survives on `:8188`, hybrid mode treats it as `external` and will **not** replace it. Stale processes may run outdated Python code (e.g. before Anima backport). Kill the old `python.exe` in Task Manager and restart the bot.

### WAI ANIMA notes

`waiANIMA_v10.safetensors` is a diffusion-only checkpoint. The workflow loads CLIP (`qwen_3_06b_base.safetensors`) and VAE (`qwen_image_vae.safetensors`) separately.

ComfyUI **0.3.75** required a local backport so the UNet loads as `Anima` (not `CosmosPredict2`):

- `ComfyUI_New/comfy/model_detection.py` — `llm_adapter` → `image_model: anima`
- `ComfyUI_New/comfy/ldm/anima/` — `LLMAdapter` model
- `ComfyUI_New/comfy/model_base.py` — `class Anima`, `get_dtype_inference()`
- `ComfyUI_New/comfy/supported_models.py` — `class Anima`
- `ComfyUI_New/comfy/text_encoders/anima.py` — tokenizer + `t5xxl_ids`

**Healthy ANIMA log lines:**

```
Requested to load Anima
model_type FLOW
```

**Broken (garbage / square output):**

```
Requested to load CosmosPredict2
model_type FLOW_COSMOS
unet unexpected: ['llm_adapter.blocks...', ...]
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Colored squares / lattice output (ANIMA) | Wrong model class (`CosmosPredict2`) | Restart ComfyUI after Anima backport; confirm log shows `Requested to load Anima` |
| `clip input is invalid: None` | CLIP not wired in workflow | Ensure CLIPLoader node is connected to CLIPTextEncode nodes 6/7 |
| Generation never finishes | ComfyUI down or queue stuck | Check `:8188/system_stats`; read `comfyui_8188.log` |
| Bot can't restart ComfyUI | Log file locked or Unicode crash | Kill stale python processes; ensure UTF-8 env vars in `start_comfy()` |
| "Queue is full" | 30 jobs already pending | Wait for queue to drain |
| Upscale step fails | Missing `basicsr` for RealESRGAN node | `pip install basicsr` or remove upscale from workflow |
| Slow ANIMA after Illustrious | Expected model swap | `/free` + full Anima load ~45 s total |

---

## Quick test (without Discord)

```python
import importlib.util

spec = importlib.util.spec_from_file_location("cc", "cogs/utils/comfy_client.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

# Load workflow JSON and submit (see image_models.py for full graph structure)
cc.free_memory(port=8188)
resp = cc.post_prompt(prompt_dict, port=8188)
images = cc.wait_for_result(resp["prompt_id"], port=8188, timeout_sec=300)
print(images)
```

Or test model detection directly:

```python
import comfy.model_detection as md
import comfy.utils

sd, _ = comfy.utils.load_torch_file("models/checkpoints/waiANIMA_v10.safetensors", return_metadata=True)
prefix = md.unet_prefix_from_state_dict(sd)
print(md.detect_unet_config(sd, prefix).get("image_model"))  # should print: anima
```

Run from `ComfyUI_New/` with that repo on `PYTHONPATH` / as cwd.