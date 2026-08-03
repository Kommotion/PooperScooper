# Krea 2 Identity Edit LoRA — Local Test Guide

Instruction-based, identity-preserving edits using **Krea 2 Turbo INT8** + the **Identity Edit v1.2** LoRA.

**Not wired into Discord yet** — test in ComfyUI first.

## What was installed

| Piece | Location |
|-------|----------|
| Custom nodes | `ComfyUI_New/custom_nodes/comfyui-krea2edit/` |
| LoRA v1.2 | `ComfyUI_New/models/loras/Krea2/krea2_identity_edit_v1_2.safetensors` (~1.83 GB) |
| Workflow | `ComfyUI_New/user/default/workflows/KREA2_IDENTITY_EDIT_UI.json` |
| Base UNet (yours) | `diffusion_models/krea2_turbo_int8_convrot.safetensors` |
| CLIP | `text_encoders/qwen3vl_4b_fp8_scaled.safetensors` (type **krea2**) |
| VAE | `vae/qwen_image_vae.safetensors` |

Sources:
- Nodes: https://github.com/lbouaraba/comfyui-krea2edit  
- LoRA: https://huggingface.co/conradlocke/krea2-identity-edit  

## Before you start

1. **Stop the Discord bot** (free 8GB VRAM).
2. **Restart ComfyUI** so it loads `comfyui-krea2edit` (Desktop or `python main.py` once).
3. Confirm the node pack appears: search for **Krea2EditModelPatch** / **Krea2EditGroundedEncode**.

## Load the workflow

1. Open ComfyUI on port **8188**.
2. **Workflows → Open** → `KREA2_IDENTITY_EDIT_UI.json`  
   (or open from the custom_nodes pack under `comfyui-krea2edit/workflows/`).
3. Check loaders:
   - **UNETLoader** → `krea2_turbo_int8_convrot.safetensors` (already set for you)
   - **LoraLoaderModelOnly** → `Krea2/krea2_identity_edit_v1_2.safetensors` @ strength **1.0**
   - **CLIPLoader** → `qwen3vl_4b_fp8_scaled`, type **krea2**
   - **VAELoader** → `qwen_image_vae`

## Quick test

1. **Load Image** — clear photo of a person (or object).
2. Write an **instruction** (plain English), e.g.:
   - `Change her jacket to a red leather jacket.`
   - `Relight the scene with warm golden hour sunlight.`
   - `Place this person sitting at a cafe table holding coffee.`
3. Keep **Turbo** defaults for most edits:
   - **steps 8–10**, **CFG 1**, euler/simple (workflow defaults)
4. Output size: stay **≤ ~2 megapixels** (e.g. 1024×1024 or 832×1216). Higher can bleed/duplicate.
5. **Queue Prompt**.

### 8GB tips

- One job at a time; unload other models first.
- Prefer **1024×1024** if you OOM.
- Keep **target_latent** wired (workflow should already) so VAE encode doesn’t thrash VRAM mid-sample.
- If still OOM: lower resolution, or try smaller LoRA rank later (`v1_2_r64` / `r128` on HF) — quality tradeoff.

### When Turbo is weak

**Removals / “delete the object”** often fail on Turbo CFG 1.  
Authors recommend **Krea 2 Raw** at **CFG ~3, ~20 steps** for that class of edit (you don’t have Raw downloaded yet — Turbo first).

## Dials that matter

| Control | Effect |
|---------|--------|
| **Instruction text** | What to change (describe the edit, not a full novel scene dump) |
| **grounding_px** | Lower (~512) = stronger edit adherence; higher (~1024) = stronger likeness |
| **ref_boost** | \>1 cling to reference look; \<1 freer |
| **fit_mode** | `fit` for v1.2 (default); `crop` only for older LoRAs |
| **LoRA strength** | Start at **1.0** |

## Two-image edits (person into scene)

The shipped workflow has a second group (person + scene). Un-bypass it and wire **image_b** / **source_latent_b** per the notes on the canvas.

## Responsible use

The LoRA author states it is trained on **SFW** identity restaging and asks users **not** to use it for non-consensual deepfakes of real people. Respect that.

## Discord bot

Slash command (restart bot + sync commands first):

```
/imagegen edit  image:<attachment>  instruction:<plain English>
```

- API workflow: `user/default/workflows/KREA2_IDENTITY_EDIT_API.json`
- Base: Krea2 Turbo INT8 + Identity Edit LoRA v1.2
- Output ~1MP matching source aspect (capped ~2MP)
- Bot uploads the attachment via ComfyUI `/upload/image`

The **bot-started** Comfy process must load `comfyui-krea2edit` from `ComfyUI_New/custom_nodes/` (already installed). If nodes are missing in bot mode, restart Comfy fully (stop bot, stop Desktop, start bot again).
