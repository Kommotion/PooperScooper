# Img2Img & Image Editing Guide (ComfyUI)

Test **in ComfyUI first** (Desktop or browser on `:8188`). Do **not** wire Discord until you’re happy with quality.

## Two different things people call “image editing”

| Approach | What it is | Best for | Your GPU (RTX A4000 8GB) |
|----------|------------|----------|---------------------------|
| **Classic img2img** | Same checkpoint as t2i; VAE-encode photo → add noise (`denoise`) → sample with a prompt | Restyle, outfit/lighting change, “same vibe but…” | **Fits well** with Illustrious / Anima |
| **Edit / instruction models** | Models **trained to edit** (image + text instruction) | “Change only the jacket”, multi-image reference, keep identity hard | Possible with **heavy quants**; slower; extra downloads |

You were right: **edit-specialized models exist**. They are usually **not** the same as Illustrious/Krea t2i checkpoints.

### Edit-specialized families (for later)

- **Flux.1 Kontext** — strong instruction editing / identity; needs GGUF or offload on 8GB.
- **Qwen-Image Edit** — excellent “change this, keep that”; 8GB only with small GGUF + Lightning LoRA (softer quality).
- **InstructPix2Pix** — older SD1.5-style instruction edit (lighter, weaker than modern edit models).

These need **new weights + different graphs**. Start with **classic img2img on models you already have** — that’s what the workflows below do, and it’s usually what people mean by “prompt + image” without downloading another 10–20GB stack.

---

## Workflows created for you

Location: `ComfyUI_New/user/default/workflows/`

| File | Models | Notes |
|------|--------|--------|
| **`IMG2IMG_SDXL_ILLUSTRIOUS_UI.json`** | One Obsession Illustrious (default), WAI Illustrious, WAI NSFW Illustrious | Best first try on 8GB |
| **`IMG2IMG_ANIMA_UI.json`** | Anima Aesthetic / One Obsession Anima + Qwen CLIP/VAE | Anime aesthetic stack |

Both are **native Comfy nodes only** (no custom node pack required).

Improvements vs a “naive” Illustrious img2img that often looks bad:

1. **`ImageScale`** before encode (huge sources → mush / VRAM death).
2. **`denoise` default 0.5** (not 0.87–1.0).
3. Illustrious-friendly sampler defaults + a **Note** on the canvas.
4. Prompt placeholders that describe the **full result**, not only the change.

---

## How to test (step by step)

### A. SDXL Illustrious family (recommended first)

1. Open ComfyUI (`http://127.0.0.1:8188` or Comfy Desktop on port 8188).
2. **Workflows → Open**  
   `ComfyUI_New/user/default/workflows/IMG2IMG_SDXL_ILLUSTRIOUS_UI.json`
3. On **Load Image**: upload a clear photo/art (face + body readable).
4. On **Checkpoint**, pick one:
   - `oneObsession_v23.safetensors` — NSFW-capable Illustrious (default)
   - `waiIllustriousSDXL_v170.safetensors` — cleaner anime Illustrious
   - `waiNSFWIllustrious_v150.safetensors` — NSFW Illustrious
5. Set **ImageScale** size to roughly match the photo aspect (multiples of 64, ~1 megapixel):
   - Portrait: `832 × 1216` or `896 × 1152`
   - Square: `1024 × 1024`
   - Landscape: `1216 × 832`
   - Crop `center` is fine for testing; change width/height to avoid chopping heads.
6. Write a **positive** prompt that includes **what stays + what changes**:
   - Bad: `make the jacket red`
   - Good: `masterpiece, best quality, same girl and pose, detailed face, red leather jacket, soft studio light`
7. Leave **denoise = 0.5**, steps **28**, CFG **5**, `euler_ancestral` / `normal`.
8. **Queue Prompt**. Expect ~15–40s on A4000 if VRAM is free (unload Krea if stuck).

#### Denoise ladder (run 4 seeds at each if you want science)

| Denoise | What you should see |
|---------|---------------------|
| 0.30 | Light polish; composition almost locked |
| **0.50** | Default restyle sweet spot |
| 0.65 | Stronger restyle; face may drift |
| 0.80 | Heavy rewrite |
| 1.00 | Basically txt2img (image ignored) |

If identity melts: **lower denoise**, keep more of the original description in the prompt, fix scale/aspect.

### B. Anima family

1. Open `IMG2IMG_ANIMA_UI.json`.
2. Checkpoint: `anima_aestheticV11` or `oneObsessionAnima_v20` (NSFW).
3. Keep CLIP `qwen_3_06b_base` type `qwen_image` and VAE `qwen_image_vae`.
4. Same denoise ladder; CFG often **3–4.5** for Anima.

### C. What **not** to expect from classic img2img

- Surgical “only change the logo on the shirt” → needs **inpaint** or an **edit model**.
- Perfect face lock at denoise 0.7+ → needs lower denoise, **IP-Adapter**, or edit models.
- Krea 2 / RedCraft turbo as img2img → often **worse** (CFG 1, distilled); use SDXL/Anima first.

---

## Why your earlier WAI Illustrious img2img probably felt weak

Common failure modes:

1. **Denoise too high** → identity loss / “different person.”
2. **No resize** → odd latent scale, soft mess, or OOM.
3. **Prompt only describes the edit** → model invents a new scene.
4. **Wrong CFG/sampler** for Illustrious (too high CFG = plastic; wrong scheduler = mush).
5. Comparing to **Kontext / Qwen Edit** online — those are a different product class.

This workflow targets (1)–(4). If you still want Kontext-level obedience later, we download an edit stack separately.

---

## Optional next upgrades (after basic img2img feels good)

| Upgrade | Why |
|---------|-----|
| **Inpaint** (mask + denoise) | Edit one region only |
| **IP-Adapter Face / style** | Stronger identity or style lock (extra models) |
| **ControlNet** (tile / softedge) | Structure lock during restyle |
| **Qwen Image Edit / Flux Kontext** | True instruction editing (new models) |

---

## When you’re ready for Discord

Once you have a denoise + prompt recipe you like:

1. Export/save an **API format** graph (or we convert the UI graph).
2. Bot gets: image attachment + prompt + denoise slider.
3. Start with **One Obsession Illustrious / WAI Illustrious** only.

Tell me which checkpoint + denoise range you land on after testing, and we can wire that first.
