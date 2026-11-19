# Changelog

This is the first versioned changelog for PooperScooper, covering about Oct 14–Nov 15, 2026.

## v2.7.3 — 2026-11-16

### Background

- Image buttons no longer error when a deleted message's timer runs out, or when a click arrives after the interaction expired.

## v2.7.2 — 2026-11-15

### New stuff

- Inbox / Party Prompt image jobs can now post in the MTS image channel, in the same style as /imagegen, credited to MTS Party Prompts. Mentions stay off.

### Background

- Removed the hard block on the production MTS image channel. Jobs still post only to the configured channel, and a job that names a different channel is refused.

## v2.7.1 — 2026-11-15

### New stuff

- Inbox generate jobs now post with the same caption, prompt details, and buttons as /imagegen generate, attributed to MTS Party Prompts.

## v2.7.0 — 2026-11-15

### New stuff

- The bot can pick up image jobs dropped in an inbox folder and post them to a configured channel. This stays off unless `mts_image_jobs.enabled` is turned on. The default channel is the support test channel, not MTS #image-diffusion.

## v2.6.1 — 2026-11-15

### New stuff

- Image edits with huge photos no longer time out

## v2.6.0 — 2026-11-14

### New stuff

- new image models Qwen-Image 2.1, FLUX 3 Klein (realism), LUSTIFY (realism, NSFW)
- /imagegen models tagged [Anime][NSFW]/[Realism][SFW]
- upgraded One Obsession Anima v5, One Obsession Illustrious v24, better Identity Edit
- image queue now 30 (was 5)
- removed Z-Image Turbo (defaults move to WAI Illustrious)
- new /grok and /remind
- Malfoyd 5 shared daily rerolls and attachment fallback for broken previews
- playlists keep playing when a song errors
- hidden easter eggs
- Thanksgiving turkey hunt engine (coming soon, off by default)

### Background

- ComfyUI engine upgrade to 0.45.3
- music/Lavalink reliability
- voice status
- Shepherd's Farm server config
